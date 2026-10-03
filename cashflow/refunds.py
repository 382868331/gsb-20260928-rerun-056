"""退回与拒付：实际记录去重、预计/已到账区分、部分使用与方案对照。

要点：
- 退回可能只退一部分、分多次到账，原手续费未必退回；
- 重复通知或同一笔款的更正不会形成第二份余额（按 refund_id 去重）；
- 预计退回（cutoff 后到账）不作为可用资金参与求解，只在计划中列示；
- 已到账退回可以只把一部分用于尚未确认的付款（refund_allocations）；
- compare_refund_usage 对照「继续等待退回」与「替代账户安排」的剩余缺口与费用。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from decimal import Decimal

from .engine import solve
from .models import CashFlow, FlowKind, Priority, money
from .plan import Execution, Plan, RefundRecord


def normalize_refunds(records: list[RefundRecord],
                      ) -> tuple[list[RefundRecord], list[dict]]:
    """按 refund_id 去重：重复通知忽略，更正以最新一条为准。

    同一笔退回的重复通知或更正不能增加第二份余额。
    """
    effective: dict[str, RefundRecord] = {}
    notes: list[dict] = []

    def content(r: RefundRecord) -> tuple:
        return (r.target, r.ref_id, r.kind, r.amount, r.arrival_date,
                r.credit_account, r.fee_refunded)

    for r in records:
        old = effective.get(r.refund_id)
        if old is None:
            effective[r.refund_id] = r
            continue
        if content(r) == content(old):
            notes.append({"refund_id": r.refund_id, "action": "duplicate_ignored",
                          "reason": "重复通知，内容一致，不重复入账"})
            continue
        effective[r.refund_id] = r
        notes.append({"refund_id": r.refund_id, "action": "corrected",
                      "reason": (f"同一笔退回的更正：金额 {old.amount} -> {r.amount}，"
                                 f"到账日 {old.arrival_date} -> {r.arrival_date}，"
                                 "以最新记录为准，不形成第二份余额")})
    return list(effective.values()), notes


def split_refunds(records: list[RefundRecord], cutoff_date: str,
                  ) -> tuple[list[RefundRecord], list[RefundRecord]]:
    """区分已到账（可用依据）与预计退回（不可提前当作可用资金）。"""
    arrived = [r for r in records if r.arrival_date <= cutoff_date]
    expected = [r for r in records if r.arrival_date > cutoff_date]
    return arrived, expected


def refund_view(execution: Execution, cutoff_date: str) -> dict:
    """按原付款汇总：已退本金/手续费、到账状态、分次到账情况。"""
    effective, notes = normalize_refunds(execution.refunds)
    arrived, expected = split_refunds(effective, cutoff_date)
    by_ref: dict[str, dict] = {}
    for r in arrived:
        slot = by_ref.setdefault(r.ref_id, {
            "ref_id": r.ref_id, "target": r.target,
            "refunded_arrived": Decimal("0"), "fee_refunded": Decimal("0"),
            "expected": Decimal("0"), "installments": 0,
            "credit_accounts": set()})
        slot["refunded_arrived"] += r.amount
        slot["fee_refunded"] += r.fee_refunded
        slot["installments"] += 1
        slot["credit_accounts"].add(r.credit_account)
    for r in expected:
        slot = by_ref.setdefault(r.ref_id, {
            "ref_id": r.ref_id, "target": r.target,
            "refunded_arrived": Decimal("0"), "fee_refunded": Decimal("0"),
            "expected": Decimal("0"), "installments": 0,
            "credit_accounts": set()})
        slot["expected"] += r.amount
    view = {}
    for ref_id, slot in by_ref.items():
        view[ref_id] = {
            **{k: v for k, v in slot.items() if k != "credit_accounts"},
            "refunded_arrived": str(money(slot["refunded_arrived"])),
            "fee_refunded": str(money(slot["fee_refunded"])),
            "expected": str(money(slot["expected"])),
            "credit_accounts": sorted(slot["credit_accounts"]),
        }
    return {"cutoff_date": cutoff_date, "by_payment": view,
            "normalization_notes": notes,
            "arrived": [r.to_dict() for r in arrived],
            "expected": [r.to_dict() for r in expected]}


def allocate_arrived_refund(plan: Plan, execution: Execution,
                            cutoff_date: str, refund_id: str,
                            uses: list[tuple[str, Decimal]]) -> Plan:
    """把已到账退回的一部分用于尚未确认的付款；可只使用一部分。

    uses 为 (flow_id, amount) 列表。预计退回（cutoff 后到账）不能提前使用。
    """
    effective, _ = normalize_refunds(execution.refunds)
    record = next((r for r in effective if r.refund_id == refund_id), None)
    if record is None:
        raise ValueError(f"退回记录不存在: {refund_id}")
    if record.arrival_date > cutoff_date:
        raise ValueError(
            f"退回 {refund_id} 预计 {record.arrival_date} 才到账，"
            "到账前不能当作可用资金")
    already = sum((Decimal(a["amount"]) for a in plan.refund_allocations
                   if a["refund_id"] == refund_id), Decimal("0"))
    new_total = sum((money(a) for _, a in uses), Decimal("0"))
    if already + new_total > record.amount:
        raise ValueError(
            f"退回 {refund_id} 可分配余额不足：已分配 {already}，"
            f"本次 {new_total}，实际到账 {record.amount}")
    allocations = list(plan.refund_allocations)
    for flow_id, amount in uses:
        allocations.append({
            "refund_id": refund_id, "flow_id": flow_id,
            "amount": str(money(amount)),
            "allocated_at": datetime.now().isoformat(timespec="seconds"),
            "basis": "arrived",
            "note": "已到账退回余额的部分使用，仅用于尚未确认的付款"})
    return replace(plan, refund_allocations=allocations)


def compare_refund_usage(scenario, execution: Execution, cutoff_date: str,
                         amendments=None, inflow_updates=None,
                         locked_orders=None) -> list[dict]:
    """对照「继续等待退回」与「替代账户安排」的剩余缺口与费用。

    - alternative：不指望预计退回，用现有账户/调拨能力安排剩余付款；
    - wait：把预计退回计入其到账日，缺口付款顺延到退回到账后。
    """
    from .replan import _fixed_transit_orders, _residual_scenario, build_prefix
    amendments = amendments or []
    inflow_updates = inflow_updates or {}
    effective, norm_notes = normalize_refunds(execution.refunds)
    _, expected = split_refunds(effective, cutoff_date)
    prefix = build_prefix(scenario, execution, cutoff_date)
    residual, _ = _residual_scenario(
        scenario, prefix, execution, cutoff_date, amendments,
        inflow_updates, {})
    fixed = _fixed_transit_orders(prefix)
    locked = list(locked_orders or [])

    alt = solve(residual, fixed_orders=fixed, locked_orders=locked,
                id_prefix="ALT")
    alt_gaps = {g.flow_id: g for g in alt.replay.gaps}

    options = [{
        "name": "alternative_account",
        "basis": "不使用预计退回，按现有账户与调拨能力安排",
        "expected_refund_ids": [],
        "remaining_gap": str(money(sum(
            (g.shortfall for g in alt.replay.gaps), Decimal("0")))),
        "gap_by_flow": {fid: str(money(g.shortfall))
                        for fid, g in alt_gaps.items()},
        "total_fee": str(money(alt.plan.total_fee)),
        "delayed_payments": {},
        "orders": [o.to_dict() for o in alt.plan.orders],
    }]

    if expected:
        arrival = min(r.arrival_date for r in expected)
        refund_inflows = [
            CashFlow(f"EXP-{r.refund_id}", r.credit_account, r.arrival_date,
                     FlowKind.INFLOW, r.amount, Priority.P2_NORMAL,
                     f"预计退回 {r.ref_id}（{r.kind}）")
            for r in expected
        ]
        delayed = {}
        wait_flows = []
        for f in residual.flows:
            if (f.kind == FlowKind.OUTFLOW and f.flow_id in alt_gaps
                    and f.date < arrival):
                wait_flows.append(CashFlow(
                    f.flow_id, f.account_id, arrival, f.kind, f.amount,
                    f.priority, f.memo, arrival, residual.end_date))
                delayed[f.flow_id] = arrival
            else:
                wait_flows.append(f)
        from .scenario import Scenario as _Sc
        wait_sc = _Sc(residual.scenario_id + "@wait", residual.start_date,
                      residual.end_date, list(residual.accounts),
                      wait_flows + refund_inflows,
                      list(residual.transfer_rules), residual.description)
        wait = solve(wait_sc, fixed_orders=fixed, locked_orders=locked,
                     id_prefix="WAIT")
        options.append({
            "name": "wait_for_refund",
            "basis": ("等待预计退回到账后再付款；退回到账前不作为可用资金，"
                      "受影响付款顺延"),
            "expected_refund_ids": [r.refund_id for r in expected],
            "expected_arrival": arrival,
            "remaining_gap": str(money(sum(
                (g.shortfall for g in wait.replay.gaps), Decimal("0")))),
            "gap_by_flow": {g.flow_id: str(money(g.shortfall))
                            for g in wait.replay.gaps},
            "total_fee": str(money(wait.plan.total_fee)),
            "delayed_payments": {fid: day for fid, day in delayed.items()},
            "orders": [o.to_dict() for o in wait.plan.orders],
        })
    if len(options) == 2:
        fees = [Decimal(o["total_fee"]) for o in options]
        best = 0 if fees[0] <= fees[1] else 1
        for i, o in enumerate(options):
            o["recommended"] = i == best
        options[0]["normalization_notes"] = norm_notes
    return options
