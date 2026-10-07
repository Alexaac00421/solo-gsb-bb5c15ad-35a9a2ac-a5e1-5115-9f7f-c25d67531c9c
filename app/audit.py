"""授权额度巡检（只读对账）：在同一数据库快照内核验授权计数的可追溯性。

巡检在单个 **REPEATABLE READ READ ONLY** 事务内完成全部读取，事务里的
每条 SELECT 共用同一个快照，故一次巡检绝不会混用不同提交时刻的数据
（并发发行 / 撤销 / 预留确认等在巡检期间的提交对本次结果整体不可见，
要么全在快照前、要么全在快照后）。

同一快照内读取四个来源：

1. 授权当前三项计数（``used_count`` / ``reserved_count`` / ``max_count``）；
2. **未撤销发行明细**（归属该授权、申请状态 ``approved`` 的逐项明细）；
3. **状态仍为待确认（``pending``）的预留明细**——含已到期但尚未惰性
   结算者：巡检只读数、**不触发任何到期结算 / 候补处理 / 写库**，
   这些明细仍按占额计入（``settlement_due`` 标记已到期未结算）；
4. 该授权的**期初余额与全部逐笔流水**（不可改写账本）。

检查顺序（首个异常即按此顺序定位）：

1. ``ledger_opening``        期初行存在；
2. ``ledger_entry_seq``      流水序号自 1 起严格连续；
3. ``ledger_balance_chain``  首笔变化前值 == 期初，其后每笔变化前值
                             == 上一笔变化后值；
4. ``ledger_counter_invariant`` 每笔变化后值满足
                             ``0 <= used``、``0 <= reserved``、
                             ``used + reserved <= max``；
5. ``used_details_vs_counter``     未撤销发行明细占额 == used_count；
6. ``reserved_details_vs_counter`` 待确认预留明细占额（含到期未结算）
                             == reserved_count；
7. ``ledger_tail_used_vs_counter`` /
   ``ledger_tail_reserved_vs_counter`` /
   ``ledger_tail_max_vs_counter``  流水末值（无流水时取期初）逐项
                             == 授权当前计数。

巡检**只报告、不改写任何数据**：事务以 READ ONLY 开启并以回滚结束。
"""
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app import models

# 各检查项的固定顺序：checks 与 first_anomaly 均以此为准。
CHECK_OPENING = "ledger_opening"
CHECK_ENTRY_SEQ = "ledger_entry_seq"
CHECK_BALANCE_CHAIN = "ledger_balance_chain"
CHECK_COUNTER_INVARIANT = "ledger_counter_invariant"
CHECK_USED_DETAILS = "used_details_vs_counter"
CHECK_RESERVED_DETAILS = "reserved_details_vs_counter"
CHECK_TAIL_USED = "ledger_tail_used_vs_counter"
CHECK_TAIL_RESERVED = "ledger_tail_reserved_vs_counter"
CHECK_TAIL_MAX = "ledger_tail_max_vs_counter"

CHECK_ORDER = (
    CHECK_OPENING,
    CHECK_ENTRY_SEQ,
    CHECK_BALANCE_CHAIN,
    CHECK_COUNTER_INVARIANT,
    CHECK_USED_DETAILS,
    CHECK_RESERVED_DETAILS,
    CHECK_TAIL_USED,
    CHECK_TAIL_RESERVED,
    CHECK_TAIL_MAX,
)

COUNTER_KEYS = ("used_count", "reserved_count", "max_count")


@dataclass(frozen=True)
class AuditSnapshot:
    """同一快照下四个来源的原始读数。"""

    authorization: models.Authorization
    opening: models.QuotaLedgerOpening | None
    entries: list[models.QuotaLedgerEntry]
    distribution_items: list[tuple[models.DistributionItem, models.Distribution]]
    reservation_items: list[
        tuple[models.ReservationItem, models.Reservation]
    ]
    now: datetime
    transaction_id: str
    isolation_level: str


def load_audit_snapshot(
    db: Session, authorization_id: int
) -> AuditSnapshot:
    """在已开启的 REPEATABLE READ READ ONLY 事务内读取全部来源。

    所有 SELECT 都在调用方开启的同一事务/同一快照中执行。
    """
    authorization = db.get(models.Authorization, authorization_id)

    opening = db.scalar(
        select(models.QuotaLedgerOpening).where(
            models.QuotaLedgerOpening.authorization_id == authorization_id
        )
    )
    entries = list(
        db.scalars(
            select(models.QuotaLedgerEntry)
            .where(
                models.QuotaLedgerEntry.authorization_id
                == authorization_id
            )
            .order_by(
                models.QuotaLedgerEntry.entry_seq.asc(),
                models.QuotaLedgerEntry.id.asc(),
            )
        )
    )
    # 未撤销（approved）发行明细：revoked 申请的明细不再占额。
    distribution_items = list(
        db.execute(
            select(models.DistributionItem, models.Distribution)
            .join(
                models.Distribution,
                models.Distribution.id
                == models.DistributionItem.distribution_id,
            )
            .where(
                models.DistributionItem.authorization_id
                == authorization_id,
                models.Distribution.status == "approved",
            )
            .order_by(models.DistributionItem.id.asc())
        )
    )
    # 库内状态仍为 pending 的预留明细：即使 expires_at <= now（已到期但
    # 尚未惰性结算）也照样读出并计入占额，巡检不触发结算。
    reservation_items = list(
        db.execute(
            select(models.ReservationItem, models.Reservation)
            .join(
                models.Reservation,
                models.Reservation.id
                == models.ReservationItem.reservation_id,
            )
            .where(
                models.ReservationItem.authorization_id
                == authorization_id,
                models.Reservation.status == "pending",
            )
            .order_by(models.ReservationItem.id.asc())
        )
    )
    # now() / xid / 隔离级别均取自本事务：snapshot_at 即快照时刻，
    # transaction_id 用于佐证全部读数出自同一事务。
    now, transaction_id, isolation_level = db.execute(
        text(
            "SELECT now(), pg_current_xact_id()::text, "
            "current_setting('transaction_isolation')"
        )
    ).first()

    return AuditSnapshot(
        authorization=authorization,
        opening=opening,
        entries=entries,
        distribution_items=distribution_items,
        reservation_items=reservation_items,
        now=now,
        transaction_id=transaction_id,
        isolation_level=isolation_level,
    )


# ---------------------------------------------------------------- 序列化
def _balance(used: int, reserved: int, max_count: int) -> dict:
    return {
        "used_count": used,
        "reserved_count": reserved,
        "max_count": max_count,
    }


def _entry_location(entry: models.QuotaLedgerEntry) -> dict:
    return {
        "entry_seq": entry.entry_seq,
        "ledger_entry_id": entry.id,
        "event_type": entry.event_type,
        "ref_type": entry.ref_type,
        "ref_id": entry.ref_id,
        "counterparty_authorization_id": entry.counterparty_authorization_id,
        "occurred_at": entry.occurred_at,
    }


def _entry_out(entry: models.QuotaLedgerEntry) -> dict:
    return {
        "entry_seq": entry.entry_seq,
        "event_type": entry.event_type,
        "ref_type": entry.ref_type,
        "ref_id": entry.ref_id,
        "counterparty_authorization_id": entry.counterparty_authorization_id,
        "before_balance": _balance(
            entry.before_used_count,
            entry.before_reserved_count,
            entry.before_max_count,
        ),
        "after_balance": _balance(
            entry.after_used_count,
            entry.after_reserved_count,
            entry.after_max_count,
        ),
        "occurred_at": entry.occurred_at,
    }


# ---------------------------------------------------------------- 对账
def reconcile(snapshot: AuditSnapshot) -> dict:
    """对同一快照读数执行全部检查，返回各来源数值、逐项检查与首个异常。

    纯函数：不触碰数据库、不做任何改写。
    """
    auth = snapshot.authorization
    opening = snapshot.opening
    entries = snapshot.entries

    checks: dict[str, dict] = {
        name: {"name": name, "ok": True} for name in CHECK_ORDER
    }

    def fail(name: str, reason: str, message: str, **extra) -> None:
        """登记一项检查失败（首个失败原因保留在 anomaly 字段）。"""
        checks[name] = {
            "name": name,
            "ok": False,
            "reason": reason,
            "message": message,
            **extra,
        }

    # ---- 1) 期初余额 -------------------------------------------------
    opening_balance = None
    if opening is not None:
        opening_balance = _balance(
            opening.used_count, opening.reserved_count, opening.max_count
        )
    else:
        fail(
            CHECK_OPENING,
            "opening_balance_missing",
            "额度账本缺少期初余额行（quota_ledger_openings），"
            "流水衔接与末值核对无法以期初为基准",
            location={
                "source": "quota_ledger_openings",
                "authorization_id": auth.id,
            },
        )

    # ---- 2) 流水序号严格连续（自 1 起） ------------------------------
    for position, entry in enumerate(entries, start=1):
        if entry.entry_seq != position:
            fail(
                CHECK_ENTRY_SEQ,
                "entry_seq_gap",
                f"流水序号不连续：第 {position} 笔应为 entry_seq={position}，"
                f"实际 entry_seq={entry.entry_seq}",
                location=_entry_location(entry),
                expected=position,
                actual=entry.entry_seq,
            )
            break

    # ---- 3) 前后余额衔接（期初 → 首笔 before → … → 末笔 after） ------
    if opening is not None:
        previous = opening_balance
        for entry in entries:
            before = _balance(
                entry.before_used_count,
                entry.before_reserved_count,
                entry.before_max_count,
            )
            if before != previous:
                fail(
                    CHECK_BALANCE_CHAIN,
                    "balance_chain_broken",
                    "流水前后余额不衔接：该笔变化前值与上一笔变化后值"
                    "（首笔应与期初余额）不一致",
                    location=_entry_location(entry),
                    expected=previous,
                    actual=before,
                )
                break
            previous = _balance(
                entry.after_used_count,
                entry.after_reserved_count,
                entry.after_max_count,
            )
    # 无期初行时衔接检查无法进行：首个异常已由 ledger_opening 给出。

    # ---- 4) 每笔变化后值满足额度不变量 -------------------------------
    for entry in entries:
        after = (
            entry.after_used_count,
            entry.after_reserved_count,
            entry.after_max_count,
        )
        violated = (
            after[0] < 0
            or after[1] < 0
            or after[0] + after[1] > after[2]
        )
        if violated:
            fail(
                CHECK_COUNTER_INVARIANT,
                "counter_invariant_violated",
                "流水变化后值违反额度不变量：须满足 used_count >= 0、"
                "reserved_count >= 0、used_count + reserved_count "
                "<= max_count",
                location=_entry_location(entry),
                expected="used>=0, reserved>=0, used+reserved<=max",
                actual=_balance(*after),
            )
            break

    # ---- 来源一：授权当前计数 ----------------------------------------
    current = _balance(auth.used_count, auth.reserved_count, auth.max_count)

    # ---- 来源二：未撤销发行明细占额 ----------------------------------
    distribution_refs = []
    for item, dist in snapshot.distribution_items:
        distribution_refs.append(
            {
                "distribution_item_id": item.id,
                "distribution_id": dist.id,
                "item_index": item.item_index,
                "material_id": item.material_id,
                "occur_at": dist.occur_at,
                "created_at": dist.created_at,
            }
        )
    used_from_details = len(distribution_refs)
    checks[CHECK_USED_DETAILS].update(
        detail_count=used_from_details,
        counter_value=auth.used_count,
    )
    if used_from_details != auth.used_count:
        fail(
            CHECK_USED_DETAILS,
            "used_count_mismatch",
            "未撤销发行明细占额与授权当前 used_count 不一致",
            location={
                "source": "distribution_items",
                "condition": "distribution.status = 'approved'",
                "authorization_id": auth.id,
            },
            expected=auth.used_count,
            actual=used_from_details,
        )

    # ---- 来源三：待确认预留明细占额（含已到期未结算） ----------------
    reservation_refs = []
    expired_unsettled = 0
    for item, resv in snapshot.reservation_items:
        settlement_due = resv.expires_at <= snapshot.now
        if settlement_due:
            expired_unsettled += 1
        reservation_refs.append(
            {
                "reservation_item_id": item.id,
                "reservation_id": resv.id,
                "item_index": item.item_index,
                "material_id": item.material_id,
                "expires_at": resv.expires_at,
                "created_at": resv.created_at,
                # 已到期但库内仍 pending（尚未惰性结算）：巡检不结算，
                # 仍按占额计入，仅在此明确标记。
                "settlement_due": settlement_due,
            }
        )
    reserved_from_details = len(reservation_refs)
    checks[CHECK_RESERVED_DETAILS].update(
        detail_count=reserved_from_details,
        counter_value=auth.reserved_count,
        expired_unsettled_count=expired_unsettled,
    )
    if reserved_from_details != auth.reserved_count:
        fail(
            CHECK_RESERVED_DETAILS,
            "reserved_count_mismatch",
            "待确认预留明细占额（含已到期尚未惰性结算者）与授权当前 "
            "reserved_count 不一致",
            location={
                "source": "reservation_items",
                "condition": "reservation.status = 'pending'",
                "authorization_id": auth.id,
                "expired_unsettled_count": expired_unsettled,
            },
            expected=auth.reserved_count,
            actual=reserved_from_details,
        )

    # ---- 来源四：流水末值（无流水时以期初为末值） --------------------
    if entries:
        tail = _balance(
            entries[-1].after_used_count,
            entries[-1].after_reserved_count,
            entries[-1].after_max_count,
        )
        tail_location = _entry_location(entries[-1])
    elif opening is not None:
        tail = opening_balance
        tail_location = {
            "source": "quota_ledger_openings",
            "authorization_id": auth.id,
            "note": "无流水，末值取期初余额",
        }
    else:
        tail = None
        tail_location = {
            "source": "quota_ledger",
            "authorization_id": auth.id,
            "note": "无期初行且无流水，无法确定末值",
        }

    tail_check_specs = (
        (CHECK_TAIL_USED, "used_count", "used_count_mismatch"),
        (CHECK_TAIL_RESERVED, "reserved_count", "reserved_count_mismatch"),
        (CHECK_TAIL_MAX, "max_count", "max_count_mismatch"),
    )
    for check_name, key, reason in tail_check_specs:
        checks[check_name].update(
            counter_value=current[key],
            tail_value=tail[key] if tail is not None else None,
        )
        if tail is None or tail[key] != current[key]:
            fail(
                check_name,
                reason,
                f"流水末值 {key} 与授权当前计数不一致"
                + ("（无期初行且无流水，无法确定末值）" if tail is None else ""),
                location=tail_location,
                expected=current[key],
                actual=None if tail is None else tail[key],
            )

    # ---- 汇总：首个异常按固定检查顺序定位 ----------------------------
    ordered_checks = [checks[name] for name in CHECK_ORDER]
    first_failed = next((c for c in ordered_checks if not c["ok"]), None)
    first_anomaly = None
    if first_failed is not None:
        first_anomaly = {
            "check": first_failed["name"],
            "reason": first_failed["reason"],
            "message": first_failed["message"],
            "location": first_failed.get("location"),
            "expected": first_failed.get("expected"),
            "actual": first_failed.get("actual"),
        }

    return {
        "authorization_id": auth.id,
        "checked_at": snapshot.now,
        "snapshot": {
            "isolation_level": snapshot.isolation_level,
            "read_only": True,
            "transaction_id": snapshot.transaction_id,
        },
        "consistent": first_anomaly is None,
        "sources": {
            "authorization_counts": current,
            "opening_balance": opening_balance,
            "ledger": {
                "entry_count": len(entries),
                "first_entry_seq": entries[0].entry_seq if entries else None,
                "last_entry_seq": entries[-1].entry_seq if entries else None,
                # 无流水时末值即期初；两者皆无则为 null（异常情形）。
                "tail_balance": tail,
                "entries": [_entry_out(e) for e in entries],
            },
            "active_distribution_items": {
                "count": used_from_details,
                "items": distribution_refs,
            },
            "pending_reservation_items": {
                "count": reserved_from_details,
                "expired_unsettled_count": expired_unsettled,
                "items": reservation_refs,
            },
        },
        "checks": ordered_checks,
        "first_anomaly": first_anomaly,
    }
