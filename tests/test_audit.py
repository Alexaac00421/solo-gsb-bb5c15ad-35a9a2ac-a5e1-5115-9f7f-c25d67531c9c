"""授权额度巡检端到端测试（真实 PostgreSQL + TestClient）。

运行：
    DATABASE_URL=postgresql+psycopg://licensing@/licensing?host=/tmp&port=5439 \
        python3 -m tests.test_audit
"""
import os
import sys
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import text

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://licensing@/licensing?host=/tmp&port=5439",
)

from app.database import Base, SessionLocal, engine, init_db  # noqa: E402
from app.main import app  # noqa: E402

CST = timezone(timedelta(hours=8))
OCCUR = "2026-12-01T12:00:00+08:00"
WINDOW = (
    "2026-01-01T00:00:00+08:00",
    "2027-01-01T00:00:00+08:00",
)

passed = 0
failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def auth_body(material_id, max_count=10, region="CN", channel="web"):
    return {
        "material_id": material_id,
        "region": region,
        "channel": channel,
        "starts_at": WINDOW[0],
        "ends_at": WINDOW[1],
        "max_count": max_count,
    }


def audit(client, auth_id):
    r = client.get(f"/api/v1/authorizations/{auth_id}/audit")
    assert r.status_code == 200, r.text
    return r.json()


def check_by_name(report, name):
    return next(c for c in report["checks"] if c["name"] == name)


def make_expired_pending(reservation_id, *, expired_seconds=10):
    """直接把库内 pending 预留的 expires_at 改到过去（绕过惰性结算）。

    用于构造「已到期但尚未惰性结算」的真实占额状态；该路径只改测试库，
    不经由任何业务接口。
    """
    db = SessionLocal()
    try:
        db.execute(
            text(
                "UPDATE reservations SET expires_at = now() - :sec "
                "WHERE id = :rid AND status = 'pending'"
            ),
            {"sec": timedelta(seconds=expired_seconds), "rid": reservation_id},
        )
        db.commit()
    finally:
        db.close()


def main() -> int:
    Base.metadata.drop_all(bind=engine)
    init_db()

    with TestClient(app) as c:
        print("== 未知授权 ==")
        r = c.get("/api/v1/authorizations/9999/audit")
        check("未知授权 404 not_found", r.status_code == 404, r.text)

        print("== 空授权（无流水，末值=期初）==")
        m1 = c.post(
            "/api/v1/materials", json={"code": "m1", "name": "素材一"}
        ).json()["id"]
        a1 = c.post(
            "/api/v1/authorizations", json=auth_body(m1, 5)
        ).json()
        a1_id = a1["id"]
        rep = audit(c, a1_id)
        check("空授权 consistent", rep["consistent"] is True, rep)
        check(
            "当前计数 0/0/5",
            rep["sources"]["authorization_counts"]
            == {"used_count": 0, "reserved_count": 0, "max_count": 5},
            rep["sources"],
        )
        check(
            "期初 0/0/5",
            rep["sources"]["opening_balance"]
            == {"used_count": 0, "reserved_count": 0, "max_count": 5},
            rep["sources"],
        )
        check("无流水 entry_count=0", rep["sources"]["ledger"]["entry_count"] == 0)
        check(
            "无流水末值=期初",
            rep["sources"]["ledger"]["tail_balance"]
            == rep["sources"]["opening_balance"],
            rep["sources"]["ledger"],
        )
        check(
            "明细占额均为 0",
            rep["sources"]["active_distribution_items"]["count"] == 0
            and rep["sources"]["pending_reservation_items"]["count"] == 0,
            rep["sources"],
        )
        check(
            "快照为 REPEATABLE READ 只读事务",
            rep["snapshot"]["isolation_level"] == "repeatable read"
            and rep["snapshot"]["read_only"] is True
            and rep["snapshot"]["transaction_id"],
            rep["snapshot"],
        )
        check("无首个异常", rep["first_anomaly"] is None, rep["first_anomaly"])

        print("== 直接发行 / 撤销后巡检 ==")
        d1 = c.post(
            "/api/v1/distributions",
            json={
                "material_ids": [m1],
                "region": "CN",
                "channel": "web",
                "occur_at": OCCUR,
            },
        )
        check("直接发行 201", d1.status_code == 201, d1.text)
        d1_id = d1.json()["id"]
        rep = audit(c, a1_id)
        check("发行后 consistent", rep["consistent"] is True, rep)
        check(
            "used 当前计数=1",
            rep["sources"]["authorization_counts"]["used_count"] == 1,
            rep["sources"]["authorization_counts"],
        )
        check(
            "未撤销明细占额=1 且指向申请",
            rep["sources"]["active_distribution_items"]["count"] == 1
            and rep["sources"]["active_distribution_items"]["items"][0][
                "distribution_id"
            ]
            == d1_id,
            rep["sources"]["active_distribution_items"],
        )
        check(
            "流水末值 used=1",
            rep["sources"]["ledger"]["tail_balance"]["used_count"] == 1,
            rep["sources"]["ledger"]["tail_balance"],
        )
        seq_list = [e["entry_seq"] for e in rep["sources"]["ledger"]["entries"]]
        check("流水序号 1..n 连续", seq_list == list(range(1, len(seq_list) + 1)), seq_list)
        for i, e in enumerate(rep["sources"]["ledger"]["entries"]):
            prev = rep["sources"]["opening_balance"] if i == 0 else \
                rep["sources"]["ledger"]["entries"][i - 1]["after_balance"]
            check(
                f"前后余额衔接 seq={e['entry_seq']}",
                e["before_balance"] == prev,
                (e, prev),
            )

        rv = c.post(f"/api/v1/distributions/{d1_id}/revoke")
        check("撤销 200", rv.status_code == 200, rv.text)
        rep = audit(c, a1_id)
        check("撤销后 consistent", rep["consistent"] is True, rep)
        check(
            "撤销后未撤销明细占额=0",
            rep["sources"]["active_distribution_items"]["count"] == 0,
            rep["sources"]["active_distribution_items"],
        )
        check(
            "撤销后 used 计数=0",
            rep["sources"]["authorization_counts"]["used_count"] == 0,
            rep["sources"]["authorization_counts"],
        )

        print("== 待确认预留（含已到期未惰性结算）==")
        rsv = c.post(
            "/api/v1/reservations",
            json={
                "material_ids": [m1],
                "region": "CN",
                "channel": "web",
                "occur_at": OCCUR,
                "ttl_minutes": 15,
            },
        )
        check("预留 201", rsv.status_code == 201, rsv.text)
        rsv_id = rsv.json()["id"]
        rep = audit(c, a1_id)
        check("预留后 consistent", rep["consistent"] is True, rep)
        check(
            "reserved 当前计数=1",
            rep["sources"]["authorization_counts"]["reserved_count"] == 1,
            rep["sources"]["authorization_counts"],
        )
        pend = rep["sources"]["pending_reservation_items"]
        check(
            "待确认预留明细占额=1 且指向预留",
            pend["count"] == 1
            and pend["items"][0]["reservation_id"] == rsv_id,
            pend,
        )
        check("未到期时 settlement_due=false",
              pend["items"][0]["settlement_due"] is False, pend)

        # 直接改库把到期时刻挪到过去：模拟「已到期但尚未惰性结算」。
        make_expired_pending(rsv_id)
        rep = audit(c, a1_id)
        check(
            "到期未结算仍 consistent（仍按占额）",
            rep["consistent"] is True,
            rep,
        )
        pend = rep["sources"]["pending_reservation_items"]
        check(
            "到期未结算明细仍计入 count=1",
            pend["count"] == 1
            and pend["expired_unsettled_count"] == 1
            and pend["items"][0]["settlement_due"] is True,
            pend,
        )
        check(
            "到期未结算 reserved_count 仍为 1（巡检不触发结算）",
            rep["sources"]["authorization_counts"]["reserved_count"] == 1,
            rep["sources"]["authorization_counts"],
        )
        # 巡检本身不得改变库内状态：预留仍 pending、计数仍 1。
        db = SessionLocal()
        try:
            row = db.execute(
                text(
                    "SELECT status FROM reservations WHERE id=:rid"
                ),
                {"rid": rsv_id},
            ).first()
            rc = db.execute(
                text(
                    "SELECT reserved_count FROM authorizations WHERE id=:aid"
                ),
                {"aid": a1_id},
            ).first()
        finally:
            db.close()
        check(
            "巡检后预留仍 pending（未触发结算）",
            row[0] == "pending" and rc[0] == 1,
            (row, rc),
        )
        # 业务接口读取时惰性结算照旧工作（原语义不变）。
        got = c.get(f"/api/v1/reservations/{rsv_id}")
        check(
            "业务接口惰性结算后 expired",
            got.json()["status"] == "expired",
            got.text,
        )
        rep = audit(c, a1_id)
        check("结算后巡检 consistent", rep["consistent"] is True, rep)
        check(
            "结算后待确认明细=0、reserved=0",
            rep["sources"]["pending_reservation_items"]["count"] == 0
            and rep["sources"]["authorization_counts"]["reserved_count"] == 0,
            rep["sources"],
        )

        print("== 混合业务后全量账实相符 ==")
        # 再来：2 笔发行 + 1 笔预留（多素材触发另一授权）
        m2 = c.post(
            "/api/v1/materials", json={"code": "m2", "name": "素材二"}
        ).json()["id"]
        a2 = c.post(
            "/api/v1/authorizations", json=auth_body(m2, 4)
        ).json()
        a2_id = a2["id"]
        c.post(
            "/api/v1/distributions",
            json={
                "material_ids": [m1, m2],
                "region": "CN",
                "channel": "web",
                "occur_at": OCCUR,
            },
        )
        c.post(
            "/api/v1/distributions",
            json={
                "material_ids": [m2],
                "region": "CN",
                "channel": "web",
                "occur_at": OCCUR,
            },
        )
        rsv2 = c.post(
            "/api/v1/reservations",
            json={
                "material_ids": [m1, m2],
                "region": "CN",
                "channel": "web",
                "occur_at": OCCUR,
                "ttl_minutes": 20,
            },
        )
        check("第二笔预留 201", rsv2.status_code == 201, rsv2.text)
        for aid, expect_used, expect_resv in ((a1_id, 1, 1), (a2_id, 2, 1)):
            rep = audit(c, aid)
            counts = rep["sources"]["authorization_counts"]
            check(
                f"授权 {aid} 计数 {expect_used}/{expect_resv}",
                counts["used_count"] == expect_used
                and counts["reserved_count"] == expect_resv,
                counts,
            )
            check(
                f"授权 {aid} 明细占额与计数一致",
                rep["sources"]["active_distribution_items"]["count"]
                == expect_used
                and rep["sources"]["pending_reservation_items"]["count"]
                == expect_resv,
                rep["sources"],
            )
            check(f"授权 {aid} consistent", rep["consistent"] is True, rep)
            check(
                f"授权 {aid} 末值==当前计数",
                rep["snapshot"] and rep["sources"]["ledger"]["tail_balance"]
                == counts,
                rep["sources"]["ledger"]["tail_balance"],
            )
            chk = check_by_name(rep, "ledger_entry_seq")
            check(f"授权 {aid} seq 检查 ok", chk["ok"], chk)
            chk = check_by_name(rep, "ledger_balance_chain")
            check(f"授权 {aid} 衔接检查 ok", chk["ok"], chk)
            chk = check_by_name(rep, "ledger_counter_invariant")
            check(f"授权 {aid} 不变量检查 ok", chk["ok"], chk)

        print("== 人为制造差异：首个异常定位（只报告不改写） ==")
        db = SessionLocal()
        try:
            # 1) used_count 与明细不符：直接把 a2 的 used_count 调小，
            #    但不补/不删流水（制造计数↔明细、计数↔末值双重差异）。
            db.execute(
                text(
                    "UPDATE authorizations SET used_count = used_count - 1 "
                    "WHERE id = :aid"
                ),
                {"aid": a2_id},
            )
            db.commit()
        finally:
            db.close()
        rep = audit(c, a2_id)
        check(
            "篡改 used 后 inconsistent",
            rep["consistent"] is False,
            rep,
        )
        fa = rep["first_anomaly"]
        # 检查顺序：明细占额比对在末值比对之前，首个异常应为明细不符。
        check(
            "首个异常定位 used_details_vs_counter",
            fa["check"] == "used_details_vs_counter"
            and fa["reason"] == "used_count_mismatch"
            and fa["location"]["source"] == "distribution_items"
            and fa["expected"] == 1
            and fa["actual"] == 2,
            fa,
        )
        names_bad = [x["name"] for x in rep["checks"] if not x["ok"]]
        check(
            "同次巡检同时报出末值不符（但首个异常仍为明细项）",
            "ledger_tail_used_vs_counter" in names_bad,
            names_bad,
        )
        # 只报告：数据不被巡检修正。
        db = SessionLocal()
        try:
            val = db.execute(
                text(
                    "SELECT used_count FROM authorizations WHERE id=:aid"
                ),
                {"aid": a2_id},
            ).scalar()
        finally:
            db.close()
        check("巡检未改写 used_count（仍为篡改值 1）", val == 1, val)

        # 还原计数，改用流水缺口制造账本异常（直接写流水表会被触发器拦截，
        # 这里验证「只报告」路径通过临时停用触发器构造，再恢复触发器）。
        db = SessionLocal()
        try:
            db.execute(
                text(
                    "UPDATE authorizations SET used_count = used_count + 1 "
                    "WHERE id = :aid"
                ),
                {"aid": a2_id},
            )
            db.execute(
                text("ALTER TABLE quota_ledger_entries DISABLE TRIGGER ALL")
            )
            # 删除中间一笔（seq=2）：seq 出现缺口且前后余额不再衔接。
            deleted = db.execute(
                text(
                    "DELETE FROM quota_ledger_entries "
                    "WHERE authorization_id=:aid AND entry_seq=2"
                ),
                {"aid": a2_id},
            ).rowcount
            db.commit()
        finally:
            db.close()
        check("构造账本缺口（临时禁用触发器）成功删除 1 笔", deleted == 1)
        rep = audit(c, a2_id)
        check("流水缺口后 inconsistent", rep["consistent"] is False, rep)
        fa = rep["first_anomaly"]
        check(
            "首个异常定位 ledger_entry_seq",
            fa["check"] == "ledger_entry_seq"
            and fa["reason"] == "entry_seq_gap"
            and fa["location"]["entry_seq"] == 3
            and fa["expected"] == 2
            and fa["actual"] == 3,
            fa,
        )
        # 恢复触发器并补回缺口，避免影响后续用例（仅测试库修复）。
        db = SessionLocal()
        try:
            db.execute(
                text("ALTER TABLE quota_ledger_entries ENABLE TRIGGER ALL")
            )
            db.commit()
        finally:
            db.close()

        print("== 巡检只读：并发写期间不混用快照 ==")
        # 快照一致性：巡检事务开启后并发提交的发行，不得混入本次结果。
        # 通过两条连接模拟：先在连接 X 开启 REPEATABLE READ 只读事务并读
        # 一次（固定快照），连接 Y 提交一笔新发行，再在 X 内继续读取，
        # 计数/明细/末值必须仍是提交前的值且彼此一致。
        from app import audit as audit_mod

        s1 = SessionLocal()
        s2 = SessionLocal()
        try:
            s1.execute(
                text(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ "
                    "READ ONLY"
                )
            )
            snap_before = audit_mod.load_audit_snapshot(s1, a1_id)
            before_counts = (
                snap_before.authorization.used_count,
                snap_before.authorization.reserved_count,
            )
            before_items = len(snap_before.distribution_items)
            before_entries = len(snap_before.entries)

            s2.execute(
                text(
                    "UPDATE authorizations SET used_count = used_count + 1 "
                    "WHERE id = :aid"
                ),
                {"aid": a1_id},
            )
            s2.commit()

            snap_after = audit_mod.load_audit_snapshot(s1, a1_id)
            check(
                "并发提交不改变巡检快照内计数",
                (
                    snap_after.authorization.used_count,
                    snap_after.authorization.reserved_count,
                )
                == before_counts,
                (before_counts, snap_after.authorization.used_count),
            )
            check(
                "并发提交不混入明细",
                len(snap_after.distribution_items) == before_items,
                (before_items, len(snap_after.distribution_items)),
            )
            check(
                "并发提交不混入流水",
                len(snap_after.entries) == before_entries,
                (before_entries, len(snap_after.entries)),
            )
            report = audit_mod.reconcile(snap_after)
            # 快照内部仍以它固定时刻的计数为准（篡改的计数已回滚后，
            # 这里 s2 的纯计数更新会造成快照视角下“末值≠计数”，但这正是
            # 同一快照内的读数；关键断言是不混用，而不是跨时刻一致）。
            check(
                "同一快照内明细与流水读数自洽（同事务重读不变）",
                report["sources"]["active_distribution_items"]["count"]
                == before_items
                and report["sources"]["ledger"]["entry_count"]
                == before_entries,
                report["sources"],
            )
            s1.rollback()
        finally:
            s1.close()
            s2.close()
        # 回滚 s2 的纯计数篡改（无对应明细/流水），恢复账实相符。
        # 注意 s2 已 commit；这里显式改回。
        db = SessionLocal()
        try:
            db.execute(
                text(
                    "UPDATE authorizations SET used_count = used_count - 1 "
                    "WHERE id = :aid"
                ),
                {"aid": a1_id},
            )
            db.commit()
        finally:
            db.close()
        rep = audit(c, a1_id)
        check("恢复后授权1 consistent", rep["consistent"] is True, rep)

        print("== 巡检不触发预留到期结算（库内 pending 已过期） ==")
        # 再建一笔预留并直接改到期时刻；连续两次巡检后仍 pending。
        rsv3 = c.post(
            "/api/v1/reservations",
            json={
                "material_ids": [m1],
                "region": "CN",
                "channel": "web",
                "occur_at": OCCUR,
                "ttl_minutes": 30,
            },
        )
        rsv3_id = rsv3.json()["id"]
        make_expired_pending(rsv3_id)
        audit(c, a1_id)
        audit(c, a1_id)
        db = SessionLocal()
        try:
            status, resv_count = db.execute(
                text(
                    "SELECT status, (SELECT reserved_count FROM authorizations "
                    "WHERE id=:aid) FROM reservations WHERE id=:rid"
                ),
                {"aid": a1_id, "rid": rsv3_id},
            ).first()
        finally:
            db.close()
        check(
            "连续巡检后仍 pending 且仍占额（未触发结算）",
            status == "pending" and resv_count >= 1,
            (status, resv_count),
        )

        print("== HTTP 级并发：巡检与发行并发，单次结果绝不混用 ==")
        # 另开一条大容量授权供并发抢占；巡检线程与发行线程同时跑。
        # 若一次巡检的四来源取自不同提交时刻，used 计数、未撤销明细数与
        # 流水末值会对不上 -> consistent=false。正确实现（四来源同一
        # REPEATABLE READ 快照）下，每次巡检都必须自洽。
        m3 = c.post(
            "/api/v1/materials", json={"code": "m3", "name": "素材三"}
        ).json()["id"]
        a3 = c.post(
            "/api/v1/authorizations", json=auth_body(m3, 100000)
        ).json()
        a3_id = a3["id"]

        import threading

        stop = threading.Event()
        audit_bad: list[dict] = []
        audit_runs = 0
        audit_lock = threading.Lock()

        def worker_issue():
            local = TestClient(app)
            while not stop.is_set():
                r = local.post(
                    "/api/v1/distributions",
                    json={
                        "material_ids": [m3],
                        "region": "CN",
                        "channel": "web",
                        "occur_at": OCCUR,
                    },
                )
                if r.status_code != 201:
                    with audit_lock:
                        audit_bad.append({"issue": r.status_code, "body": r.text})

        def worker_audit():
            nonlocal audit_runs
            local = TestClient(app)
            while not stop.is_set():
                r = local.get(f"/api/v1/authorizations/{a3_id}/audit")
                with audit_lock:
                    audit_runs += 1
                    if r.status_code != 200:
                        audit_bad.append({"http": r.status_code, "body": r.text})
                        continue
                    rep = r.json()
                    if not rep["consistent"]:
                        audit_bad.append(
                            {"inconsistent": rep["first_anomaly"]}
                        )

        issuers = [threading.Thread(target=worker_issue) for _ in range(4)]
        auditors = [threading.Thread(target=worker_audit) for _ in range(2)]
        for t in issuers + auditors:
            t.start()
        # 并发跑约 1.5 秒后收工。
        threading.Event().wait(1.5)
        stop.set()
        for t in issuers + auditors:
            t.join(timeout=10)

        final = c.get(f"/api/v1/authorizations/{a3_id}/audit").json()
        check(
            "并发期间无失败请求",
            not audit_bad,
            audit_bad[:3],
        )
        check(
            f"并发期间多次巡检均自洽（运行 {audit_runs} 次）",
            audit_runs > 0 and not audit_bad,
            (audit_runs, audit_bad[:3]),
        )
        check(
            "并发结束后授权3 consistent",
            final["consistent"] is True,
            final.get("first_anomaly"),
        )
        check(
            "并发结束后计数/明细/末值三者相等",
            final["sources"]["authorization_counts"]["used_count"]
            == final["sources"]["active_distribution_items"]["count"]
            == final["sources"]["ledger"]["tail_balance"]["used_count"],
            final["sources"],
        )

    print(f"\n{'='*60}\n结果：{passed} 通过，{failed} 失败")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
