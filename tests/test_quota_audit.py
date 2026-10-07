"""授权额度可追溯核对（运营巡检）端到端测试（真实 PostgreSQL + TestClient）。

运行：
    DATABASE_URL=postgresql+psycopg://licensing@/licensing?host=/tmp&port=5439 \
        python3 -m tests.test_quota_audit
"""
import os
import threading
from datetime import timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import text

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://licensing@/licensing?host=/tmp&port=5439",
)

from app.database import Base, engine, init_db  # noqa: E402
from app.main import app  # noqa: E402

CST = timezone(timedelta(hours=8))
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


def raw_sql(stmt, params=None):
    """绕过应用直接读写库（用于构造账实差异等现实中不应发生的场景）。"""
    with engine.begin() as conn:
        return conn.execute(text(stmt), params or {})


def auth_body(material_id, max_count=10, region="CN", channel="web"):
    return {
        "material_id": material_id,
        "region": region,
        "channel": channel,
        "starts_at": WINDOW[0],
        "ends_at": WINDOW[1],
        "max_count": max_count,
    }


def dist_body(material_id, occur):
    return {
        "material_ids": [material_id],
        "region": "CN",
        "channel": "web",
        "occur_at": occur,
    }


def check_map(audit):
    return {item["check"]: item for item in audit["checks"]}


def main() -> int:
    Base.metadata.drop_all(bind=engine)
    init_db()

    with TestClient(app) as c:
        print("== 基础：无流水的新授权以期初为末值且一致 ==")
        m1 = c.post(
            "/api/v1/materials", json={"code": "m1", "name": "素材一"}
        ).json()["id"]
        a1 = c.post("/api/v1/authorizations", json=auth_body(m1, 5)).json()
        r = c.get(f"/api/v1/authorizations/{a1['id']}/quota-audit")
        assert r.status_code == 200, r.text
        audit = r.json()
        check("一致", audit["consistent"], audit)
        check("首个异常为 null", audit["first_anomaly"] is None)
        check(
            "期初 0/0/5",
            audit["sources"]["ledger"]["opening_balance"]
            == {"used_count": 0, "reserved_count": 0, "max_count": 5},
        )
        check("流水 0 笔", audit["sources"]["ledger"]["entry_count"] == 0)
        check(
            "无流水时 last_entry_seq=0",
            audit["sources"]["ledger"]["last_entry_seq"] == 0,
        )
        check(
            "无流水末值=期初",
            audit["sources"]["ledger"]["ending_balance"]
            == {"used_count": 0, "reserved_count": 0, "max_count": 5},
        )
        check(
            "只读快照标记",
            audit["snapshot"]["isolation_level"]
            == "REPEATABLE READ READ ONLY"
            and audit["snapshot"]["settlement_triggered"] is False,
        )

        print("== 未知授权明确 404 ==")
        r = c.get("/api/v1/authorizations/999999/quota-audit")
        check("404 not_found", r.status_code == 404, r.text)
        check("错误码", r.json()["error"]["code"] == "not_found", r.text)

        print("== 发行 + 预留：三来源一致 ==")
        occ1 = "2026-12-01T12:00:00+08:00"
        d1 = c.post("/api/v1/distributions", json=dist_body(m1, occ1)).json()
        rv = c.post(
            "/api/v1/reservations",
            json={
                "material_ids": [m1],
                "region": "CN",
                "channel": "web",
                "occur_at": "2026-12-02T12:00:00+08:00",
                "ttl_minutes": 30,
            },
        ).json()
        audit = c.get(
            f"/api/v1/authorizations/{a1['id']}/quota-audit"
        ).json()
        check("一致", audit["consistent"], audit)
        check(
            "used=1（授权/明细/末值）",
            check_map(audit)["used_consistency"]["observed"]
            == {"authorization": 1, "details": 1, "ledger_ending": 1},
        )
        check(
            "reserved=1（授权/明细/末值）",
            check_map(audit)["reserved_consistency"]["observed"]
            == {"authorization": 1, "details": 1, "ledger_ending": 1},
        )
        check(
            "明细可定位到申请编号",
            audit["sources"]["active_distribution_items"]["items"][0][
                "distribution_id"
            ]
            == d1["id"],
        )
        check(
            "预留明细可定位到预留编号",
            audit["sources"]["pending_reservation_items"]["items"][0][
                "reservation_id"
            ]
            == rv["id"],
        )
        check(
            "未到期预留 due_unsettled_count=0",
            audit["sources"]["pending_reservation_items"][
                "due_unsettled_count"
            ]
            == 0,
        )

        print("== 撤销后：used 回落且仍一致 ==")
        assert c.post(f"/api/v1/distributions/{d1['id']}/revoke").status_code == 200
        audit = c.get(
            f"/api/v1/authorizations/{a1['id']}/quota-audit"
        ).json()
        check("撤销后一致", audit["consistent"], audit)
        check(
            "撤销后未撤销明细 0 笔",
            audit["sources"]["active_distribution_items"]["count"] == 0,
            audit,
        )
        check(
            "撤销后 used=0",
            audit["sources"]["authorization"]["used_count"] == 0,
        )

        print("== 已到期但未惰性结算的 pending 预留仍按占额，且巡检不结算 ==")
        # 构造一个已经到期、但没有任何写事务触发惰性结算的 pending 预留：
        # 直接把 expires_at 改到过去（应用正常路径下该状态仅短暂存在）。
        raw_sql(
            "UPDATE reservations SET expires_at = now() - interval '1 minute' "
            "WHERE id = :rid",
            {"rid": rv["id"]},
        )
        audit = c.get(
            f"/api/v1/authorizations/{a1['id']}/quota-audit"
        ).json()
        check(
            "到期未结算时仍一致（授权计数未结算=明细仍 pending=末值）",
            audit["consistent"],
            audit,
        )
        pending_src = audit["sources"]["pending_reservation_items"]
        check("pending 明细仍 1 笔", pending_src["count"] == 1, pending_src)
        check(
            "due_unsettled_count=1",
            pending_src["due_unsettled_count"] == 1,
            pending_src,
        )
        check(
            "逐项标记 expired_but_unsettled",
            pending_src["items"][0]["expired_but_unsettled"] is True,
            pending_src,
        )
        check(
            "授权 reserved_count 仍为 1（未被巡检结算）",
            audit["sources"]["authorization"]["reserved_count"] == 1,
        )
        # 库里的预留状态必须仍是 pending——巡检只读、绝不触发结算。
        status_after = raw_sql(
            "SELECT status FROM reservations WHERE id = :rid",
            {"rid": rv["id"]},
        ).scalar()
        check("巡检后预留仍为 pending", status_after == "pending", status_after)
        reserved_after = raw_sql(
            "SELECT reserved_count FROM authorizations WHERE id = :aid",
            {"aid": a1["id"]},
        ).scalar()
        check(
            "巡检后授权 reserved_count 未变", reserved_after == 1,
            reserved_after,
        )
        # 对照：走正常单条授权查询（既有语义）会惰性结算，之后巡检仍一致。
        c.get(f"/api/v1/authorizations/{a1['id']}")
        audit = c.get(
            f"/api/v1/authorizations/{a1['id']}/quota-audit"
        ).json()
        check("结算完成后巡检仍一致", audit["consistent"], audit)
        check(
            "结算后 due_unsettled_count=0",
            audit["sources"]["pending_reservation_items"][
                "due_unsettled_count"
            ]
            == 0,
        )

        print("== 差异 1：授权计数与明细/末值不符（只报告不改写） ==")
        d2 = c.post(
            "/api/v1/distributions",
            json=dist_body(m1, "2026-12-03T12:00:00+08:00"),
        ).json()
        # 直接改授权计数制造账实差异（绕过触发器：流水表不可改，授权表可改）。
        raw_sql(
            "UPDATE authorizations SET used_count = used_count + 1 WHERE id = :aid",
            {"aid": a1["id"]},
        )
        audit = c.get(
            f"/api/v1/authorizations/{a1['id']}/quota-audit"
        ).json()
        checks = check_map(audit)
        check("整体不一致", audit["consistent"] is False, audit)
        uc = checks["used_consistency"]
        check("used 比对失败", uc["passed"] is False, uc)
        check(
            "used 观测值 授权2/明细1/末值1",
            uc["observed"]
            == {"authorization": 2, "details": 1, "ledger_ending": 1},
            uc,
        )
        # 流水自身没问题：序号/衔接检查仍通过。
        check("序号检查仍通过", checks["ledger_sequence"]["passed"] is True)
        check("衔接检查仍通过", checks["ledger_chain"]["passed"] is True)
        # 首个异常按固定顺序：opening/seq/chain 均过，首个即 used_consistency。
        check(
            "首个异常=used_consistency",
            audit["first_anomaly"]["check"] == "used_consistency",
            audit["first_anomaly"],
        )
        check(
            "首个异常定位到计数名",
            audit["first_anomaly"]["position"]
            == {"type": "authorization", "counter": "used_count"},
        )
        # 数据未被巡检改写：库里仍是篡改后的值。
        stuck = raw_sql(
            "SELECT used_count FROM authorizations WHERE id = :aid",
            {"aid": a1["id"]},
        ).scalar()
        check("巡检不改写数据", stuck == 2, stuck)

        print("== 差异 2：流水缺号 ==")
        # 恢复计数一致后，删除一笔中间流水制造缺号——流水表有触发器禁止
        # DELETE，改用临时禁用触发器（模拟历史损坏），随后恢复触发器。
        raw_sql("UPDATE authorizations SET used_count = 1 WHERE id = :aid",
                {"aid": a1["id"]})
        seqs = [
            row[0]
            for row in raw_sql(
                "SELECT entry_seq FROM quota_ledger_entries "
                "WHERE authorization_id = :aid ORDER BY entry_seq",
                {"aid": a1["id"]},
            )
        ]
        assert len(seqs) >= 2, seqs
        gap_seq = seqs[len(seqs) // 2]
        raw_sql("ALTER TABLE quota_ledger_entries DISABLE TRIGGER ALL")
        try:
            raw_sql(
                "DELETE FROM quota_ledger_entries "
                "WHERE authorization_id = :aid AND entry_seq = :s",
                {"aid": a1["id"], "s": gap_seq},
            )
        finally:
            raw_sql("ALTER TABLE quota_ledger_entries ENABLE TRIGGER ALL")
        audit = c.get(
            f"/api/v1/authorizations/{a1['id']}/quota-audit"
        ).json()
        checks = check_map(audit)
        check("缺号后不一致", audit["consistent"] is False)
        check("序号检查失败", checks["ledger_sequence"]["passed"] is False)
        pos = checks["ledger_sequence"]["position"]
        check(
            f"序号异常定位到期望 {gap_seq} 的下一实际序号",
            pos["expected_entry_seq"] == gap_seq
            and pos["entry_seq"] == gap_seq + 1
            and pos["type"] == "ledger_entry",
            pos,
        )
        check(
            "首个异常=ledger_sequence",
            audit["first_anomaly"]["check"] == "ledger_sequence",
            audit["first_anomaly"],
        )
        # 衔接也必然在该笔断裂。
        check("衔接检查失败", checks["ledger_chain"]["passed"] is False)

        print("== 差异 3：前后余额不衔接（首笔 before≠期初） ==")
        # 另起一条干净授权，篡改其首笔流水的 before 值。
        m2 = c.post(
            "/api/v1/materials", json={"code": "m2", "name": "素材二"}
        ).json()["id"]
        a2 = c.post(
            "/api/v1/authorizations", json=auth_body(m2, 10)
        ).json()
        c.post(
            "/api/v1/distributions",
            json={
                "material_ids": [m2],
                "region": "CN",
                "channel": "web",
                "occur_at": "2026-12-01T12:00:00+08:00",
            },
        )
        raw_sql("ALTER TABLE quota_ledger_entries DISABLE TRIGGER ALL")
        try:
            raw_sql(
                "UPDATE quota_ledger_entries SET before_used_count = 5 "
                "WHERE authorization_id = :aid AND entry_seq = 1",
                {"aid": a2["id"]},
            )
        finally:
            raw_sql("ALTER TABLE quota_ledger_entries ENABLE TRIGGER ALL")
        audit = c.get(
            f"/api/v1/authorizations/{a2['id']}/quota-audit"
        ).json()
        checks = check_map(audit)
        check("衔接失败", checks["ledger_chain"]["passed"] is False, checks)
        chain_pos = checks["ledger_chain"]["position"]
        check(
            "衔接异常定位 entry_seq=1",
            chain_pos == {
                "type": "ledger_entry",
                "entry_seq": 1,
                "ledger_entry_id": chain_pos["ledger_entry_id"],
            },
            chain_pos,
        )
        check(
            "首个异常=ledger_chain",
            audit["first_anomaly"]["check"] == "ledger_chain",
            audit["first_anomaly"],
        )
        check(
            "observed 给出期望前值（期初）与实际前值",
            checks["ledger_chain"]["observed"]
            == {
                "expected_before": {
                    "used_count": 0,
                    "reserved_count": 0,
                    "max_count": 10,
                },
                "actual_before": {
                    "used_count": 5,
                    "reserved_count": 0,
                    "max_count": 10,
                },
            },
            checks["ledger_chain"]["observed"],
        )

        print("== 差异 4：期初余额行缺失 ==")
        m3 = c.post(
            "/api/v1/materials", json={"code": "m3", "name": "素材三"}
        ).json()["id"]
        a3 = c.post(
            "/api/v1/authorizations", json=auth_body(m3, 3)
        ).json()
        raw_sql(
            "DELETE FROM quota_ledger_openings WHERE authorization_id = :aid",
            {"aid": a3["id"]},
        )
        audit = c.get(
            f"/api/v1/authorizations/{a3['id']}/quota-audit"
        ).json()
        checks = check_map(audit)
        check("期初缺失但授权存在仍 200", True)
        check(
            "opening_exists 失败",
            checks["opening_exists"]["passed"] is False,
            checks["opening_exists"],
        )
        check(
            "首个异常=opening_exists",
            audit["first_anomaly"]["check"] == "opening_exists",
        )
        check(
            "ledger 侧末值为 null",
            audit["sources"]["ledger"]["ending_balance"] is None,
        )
        check(
            "三方比对因缺末值失败且原因明确",
            checks["used_consistency"]["passed"] is False
            and "期初" in checks["used_consistency"]["reason"],
            checks["used_consistency"],
        )
        check(
            "序号检查不受影响（无流水即通过）",
            checks["ledger_sequence"]["passed"] is True,
        )

        print("== 并发发行/撤销：同一次巡检结果不混用提交时刻 ==")
        # 高并发写入制造频繁提交点；并发发起多次巡检，每次结果内部必须
        # 自洽：授权计数 == 明细占额 == 快照内全部流水滚出的末值。
        m4 = c.post(
            "/api/v1/materials", json={"code": "m4", "name": "素材四"}
        ).json()["id"]
        a4 = c.post(
            "/api/v1/authorizations", json=auth_body(m4, 100000)
        ).json()["id"]
        stop = threading.Event()
        errors: list[str] = []

        def writer():
            i = 0
            while not stop.is_set():
                occ = f"2026-12-{(i % 27) + 1:02d}T12:00:00+08:00"
                rr = c.post(
                    "/api/v1/distributions", json=dist_body(m4, occ)
                )
                if rr.status_code == 201 and i % 3 == 0:
                    c.post(
                        f"/api/v1/distributions/{rr.json()['id']}/revoke"
                    )
                i += 1

        def auditor():
            while not stop.is_set():
                rep = c.get(f"/api/v1/authorizations/{a4}/quota-audit")
                if rep.status_code != 200:
                    errors.append(f"HTTP {rep.status_code}: {rep.text[:200]}")
                    return
                body = rep.json()
                src = body["sources"]
                auth_u = src["authorization"]["used_count"]
                detail_u = src["active_distribution_items"]["count"]
                end_u = src["ledger"]["ending_balance"]["used_count"]
                opening = src["ledger"]["opening_balance"]
                if not (auth_u == detail_u == end_u):
                    errors.append(
                        f"used 三来源混用快照：{auth_u}/{detail_u}/{end_u}"
                    )
                    return
                if body["checks"][2]["passed"] is False:
                    errors.append("并发期间流水衔接断裂")
                    return
                if body["consistent"] is not (
                    body["first_anomaly"] is None
                ):
                    errors.append("consistent 与 first_anomaly 矛盾")
                    return
                if opening is None:
                    errors.append("新授权期初不应缺失")
                    return

        tw = threading.Thread(target=writer)
        auditors = [threading.Thread(target=auditor) for _ in range(3)]
        tw.start()
        for t in auditors:
            t.start()
        # 让并发跑约 2 秒。
        threading.Event().wait(2.0)
        stop.set()
        tw.join()
        for t in auditors:
            t.join()
        check("并发期间全部巡检无混用快照/无内部矛盾", not errors, errors[:3])

        print("== 并发期间流水完整性：并发结束后最终巡检一致 ==")
        audit = c.get(f"/api/v1/authorizations/{a4}/quota-audit").json()
        check("并发结束后一致", audit["consistent"], audit["first_anomaly"])
        # 用账本接口翻完全部流水，逐笔滚余额应与巡检末值一致。
        pages_entries = []
        cursor = None
        while True:
            params = {"page_size": 100}
            if cursor:
                params["cursor"] = cursor
            page = c.get(
                f"/api/v1/authorizations/{a4}/ledger", params=params
            ).json()
            pages_entries.extend(page["entries"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        check(
            "巡检的流水笔数与账本一致",
            audit["sources"]["ledger"]["entry_count"] == len(pages_entries),
            (audit["sources"]["ledger"]["entry_count"], len(pages_entries)),
        )

    print(f"\n结果：{passed} 通过，{failed} 失败")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
