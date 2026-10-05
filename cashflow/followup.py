"""第六轮：依赖退回资金的后续付款方案研究。

退回资金到位时间不确定时，研究者需要回答「后续付款能否按时完成」。
本模块在第四/五轮（退回记录、在途拒绝）与第二轮（修订重规划）之上提供：

- 多组假设对照（compare_refund_studies）：同一执行记录下，用多个
  预计退回日期与费用假设分别重规划，比较每笔付款能否按时完成；
  假设仅用于对照方案，预计退回绝不直接进入可用余额（实际余额只认
  actual_date，见 replan._refund_flows）。
- 实际到账驱动的付款执行表（payment_execution_schedule）：多笔实际
  退回分批进入不同账户时，只按实际到账、调拨时延与不可跨账户透支
  约束，给出每笔付款 可执行/仍需等待 与最早可执行日期。
- 调整未发出付款（adjust_followup_payments）：允许调整尚未发出付款
  的优先级或取消其安排；已发出/已到账的事实（执行记录、在途调拨、
  已付支出）不因预测变化而撤销，试图调整已付支出会直接报错。
- 按实际重算受影响部分（recompute_affected）：实际退回金额/日期与
  预测不一致时，只重算受影响的未执行部分（已发起订单自动进入
  fixed_transit 不再重扣），原方案对象与文件保持不动，供核对。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .models import CashFlow, FlowKind, money
from .plan import Execution, Plan, RefundRecord
from .refund import record_refunds
from .replan import (FeeAmendment, ReplanResult, build_prefix, replan)
from .scenario import Scenario


@dataclass(frozen=True)
class RefundStudyCase:
    """一组研究假设：预计退回的假设到账日 + 费用假设（仅用于对照）。

    refund_assumptions: refund_id -> 假设到账日；只在对照方案里显式入账，
    不会改变任何实际余额。amendments: 该假设下新发起调拨的费率。
    """

    name: str
    refund_assumptions: dict[str, str] = field(default_factory=dict)
    amendments: tuple[FeeAmendment, ...] = ()
    note: str = ""


def _decision_map(plan: Plan) -> dict[str, dict]:
    return {d["flow_id"]: d for d in plan.payment_decisions}


def _original_dates(scenario: Scenario) -> dict[str, str]:
    return {f.flow_id: f.date for f in scenario.flows
            if f.kind == FlowKind.OUTFLOW}


def _study_summary(case_name: str, note: str, assumptions: dict[str, str],
                   amendments, result: ReplanResult,
                   original_dates: dict[str, str]) -> dict:
    plan = result.plan
    gap = sum((Decimal(g["shortfall"]) for g in plan.remaining_gaps),
              Decimal("0"))
    payments = []
    for d in plan.payment_decisions:
        payments.append({
            "flow_id": d["flow_id"],
            "account_id": d["account_id"],
            "priority": d["priority"],
            "due": d["due"],
            "original_due_date": original_dates.get(
                d["flow_id"], d["original_due_date"]),
            "status": d["status"],
            "scheduled_date": d["scheduled_date"],
            "on_time": d["status"] == "on_time",
            "shortfall": d["shortfall"],
        })
    return {
        "name": case_name,
        "note": note,
        "assumption_only": True,
        "assumed_refunds": [
            {"refund_id": rid, "assumed_date": day}
            for rid, day in sorted(assumptions.items())],
        "fee_amendments": [a.to_dict() for a in amendments],
        "remaining_gap": str(money(gap)),
        "total_fee": str(plan.total_fee),
        "payments": payments,
        "all_on_time": all(p["status"] in ("on_time", "cancelled")
                           for p in payments),
        "plan": plan,
        "result": result,
    }


def compare_refund_studies(scenario: Scenario, original_plan: Plan,
                           execution: Execution, cutoff_date: str,
                           cases: list[RefundStudyCase],
                           **replan_kwargs) -> list[dict]:
    """用多组预计退回日期/费用假设分别重规划，比较付款能否按时完成。

    始终先输出「仅实际到账」基线（不启用任何假设），再逐组对照；
    任何预计退回都不会直接成为可用余额——只有实际到账（actual_date）
    才入账，假设入账只发生在各自的对照方案内。
    """
    original_dates = _original_dates(scenario)
    base = replan(scenario, original_plan, execution, cutoff_date,
                  **replan_kwargs)
    options = [_study_summary("仅实际到账（不依赖任何预计退回）",
                              "基线：预计退回不作为可用资金", {}, [], base,
                              original_dates)]
    for case in cases:
        result = replan(scenario, original_plan, execution, cutoff_date,
                        amendments=list(case.amendments),
                        refund_assumptions=dict(case.refund_assumptions),
                        **replan_kwargs)
        options.append(_study_summary(case.name, case.note,
                                      case.refund_assumptions,
                                      list(case.amendments), result,
                                      original_dates))
    return options


def payment_execution_schedule(scenario: Scenario, original_plan: Plan,
                               execution: Execution, cutoff_date: str,
                               **replan_kwargs) -> dict:
    """只按实际到账给出后续付款执行表（不使用任何预计退回）。

    多笔实际退回分批进入不同账户时，调拨时延与「账户余额不得为负」
    由账本重放强制保证（不足即报错，绝不跨账户透支）。返回每笔付款：
    可执行/仍需等待、最早可执行日期、所等待的实际退回批次；并列出
    争用同一笔退回现金的付款。
    """
    result = replan(scenario, original_plan, execution, cutoff_date,
                    **replan_kwargs)
    plan = result.plan

    # cutoff 之后实际到达的退回批次（可按日期/账户分批进入不同账户）
    batches = []
    for r in execution.refunds:
        if r.arrived and r.actual_date > cutoff_date:
            batches.append({
                "refund_id": r.refund_id,
                "original_flow_id": r.original_flow_id,
                "return_account": r.return_account,
                "actual_date": r.actual_date,
                "amount": str(money(r.amount)),
            })
    batches.sort(key=lambda b: (b["actual_date"], b["refund_id"]))

    original_dates = _original_dates(scenario)
    payments = []
    for d in plan.payment_decisions:
        covered = d["status"] in ("on_time", "deferred", "late_paid")
        waiting_on = []
        if covered:
            waiting_on = [b["refund_id"] for b in batches
                          if b["actual_date"] <= d["scheduled_date"]]
        payments.append({
            "flow_id": d["flow_id"],
            "account_id": d["account_id"],
            "priority": d["priority"],
            "due": d["due"],
            "original_due_date": original_dates.get(
                d["flow_id"], d["original_due_date"]),
            "status": d["status"],
            "executable": covered,
            "earliest_executable_date": d["scheduled_date"] if covered else None,
            "waiting_on_refunds": waiting_on,
            "reason": d["reason"],
        })

    # 争用同一笔退回现金的付款：按 支付日、优先级 顺序分配批次金额
    contention = []
    users: dict[str, list[dict]] = {}
    for p in payments:
        for rid in p["waiting_on_refunds"]:
            users.setdefault(rid, []).append(p)
    for rid, ps in sorted(users.items()):
        if len(ps) < 2:
            continue
        batch = next(b for b in batches if b["refund_id"] == rid)
        ordered = sorted(ps, key=lambda p: (
            p["earliest_executable_date"] or "9999-12-31", p["priority"]))
        remaining = Decimal(batch["amount"])
        winners, losers = [], []
        for p in ordered:
            need = Decimal(p["due"])
            if remaining >= need:
                winners.append(p["flow_id"])
                remaining -= need
            else:
                losers.append(p["flow_id"])
        contention.append({
            "refund_id": rid,
            "amount": batch["amount"],
            "return_account": batch["return_account"],
            "payments": [p["flow_id"] for p in ordered],
            "covered_first": winners,
            "must_wait": losers,
            "note": "同一笔现金按 支付日、优先级 分配；未分到的付款需等待"
                    "后续资金，账户不得透支",
        })

    return {
        "cutoff_date": cutoff_date,
        "assumptions_used": False,
        "note": "仅按实际到账（actual_date）入账；预计退回不作为可用资金",
        "refund_batches": batches,
        "payments": payments,
        "contention": contention,
        "plan": plan,
        "result": result,
    }


def _adjusted_scenario(scenario: Scenario,
                       priority_overrides: dict[str, int],
                       cancelled_flow_ids: list[str]) -> Scenario:
    flows = []
    for f in scenario.flows:
        if f.kind == FlowKind.OUTFLOW and f.flow_id in cancelled_flow_ids:
            continue
        if f.kind == FlowKind.OUTFLOW and f.flow_id in priority_overrides:
            f = CashFlow(f.flow_id, f.account_id, f.date, f.kind, f.amount,
                         priority_overrides[f.flow_id], f.memo,
                         f.window_start, f.window_end)
        flows.append(f)
    return Scenario(scenario.scenario_id, scenario.start_date,
                    scenario.end_date, list(scenario.accounts), flows,
                    list(scenario.transfer_rules), scenario.description)


def adjust_followup_payments(scenario: Scenario, original_plan: Plan,
                             execution: Execution, cutoff_date: str,
                             priority_overrides: dict[str, int] | None = None,
                             cancelled_flow_ids: list[str] | None = None,
                             **replan_kwargs) -> dict:
    """调整尚未发出的付款：改优先级或取消安排，再重算剩余计划。

    已发出/已到账的事实不可撤销：已（部分）支付或已到期过账的支出
    不允许调整，直接报错；已发起的调拨由 replan 自动进入
    fixed_transit，不因预测变化撤销。返回修订结果与调整后的场景
    （对账重放需使用该场景）。
    """
    priority_overrides = dict(priority_overrides or {})
    cancelled_flow_ids = list(cancelled_flow_ids or [])
    prefix = build_prefix(scenario, execution, cutoff_date)
    paid_map = {p.flow_id: p.paid for p in prefix.ledger.payments}
    flow_map = {f.flow_id: f for f in scenario.flows
                if f.kind == FlowKind.OUTFLOW}
    for fid in [*priority_overrides, *cancelled_flow_ids]:
        f = flow_map.get(fid)
        if f is None:
            raise ValueError(f"未知支出: {fid}")
        if f.date <= cutoff_date or paid_map.get(fid, Decimal("0")) > 0:
            raise ValueError(
                f"{fid} 已发出/已到账（原期限 {f.date}，已付 "
                f"{money(paid_map.get(fid, Decimal('0')))}），"
                "事实不能因预测变化撤销，仅可调整尚未发出的付款")

    adjusted = _adjusted_scenario(scenario, priority_overrides,
                                  cancelled_flow_ids)
    result = replan(adjusted, original_plan, execution, cutoff_date,
                    **replan_kwargs)
    for fid in cancelled_flow_ids:
        f = flow_map[fid]
        result.plan.payment_decisions.append({
            "flow_id": fid, "account_id": f.account_id,
            "priority": int(f.priority), "due": str(f.amount),
            "scheduled_date": f.date, "original_due_date": f.date,
            "memo": f.memo, "status": "cancelled",
            "paid": "0.00", "shortfall": "0.00",
            "reason": "尚未发出的付款安排已取消；已发出/已到账的事实不受影响",
        })
    result.plan.schedule_basis["followup_adjustments"] = {
        "priority_overrides": {k: int(v) for k, v in
                               sorted(priority_overrides.items())},
        "cancelled_flow_ids": sorted(cancelled_flow_ids),
    }
    return {"result": result, "plan": result.plan,
            "adjusted_scenario": adjusted,
            "cancelled_flow_ids": sorted(cancelled_flow_ids),
            "priority_overrides": {k: int(v) for k, v in
                                   sorted(priority_overrides.items())}}


def recompute_affected(scenario: Scenario, studied_plan: Plan,
                       execution: Execution, cutoff_date: str,
                       actual_records: list[RefundRecord],
                       **replan_kwargs) -> dict:
    """实际退回与预测不一致时，只重算受影响的未执行部分。

    actual_records 按 refund_id 登记（重复通知忽略、更正覆盖）；
    已发起订单保持 fixed_transit 不再重扣，只有尚未执行的部分重新
    求解。原方案 studied_plan 不被修改，仍可供核对（新方案
    revision_of 指向它）。返回逐笔付款的前后差异与预测核对。
    """
    before = _decision_map(studied_plan)
    snapshot = studied_plan.to_dict()  # 核对用：重算不得改动原方案

    notes = record_refunds(execution, actual_records)
    result = replan(scenario, studied_plan, execution, cutoff_date,
                    **replan_kwargs)
    new_plan = result.plan

    changed, unchanged = [], []
    for d in new_plan.payment_decisions:
        old = before.get(d["flow_id"])
        if old is None or (old["status"], old["scheduled_date"]) != \
                (d["status"], d["scheduled_date"]):
            changed.append({
                "flow_id": d["flow_id"],
                "was": None if old is None else {
                    "status": old["status"],
                    "scheduled_date": old["scheduled_date"]},
                "now": {"status": d["status"],
                        "scheduled_date": d["scheduled_date"]},
            })
        else:
            unchanged.append(d["flow_id"])

    forecast_check = []
    assumptions = studied_plan.schedule_basis.get("refund_assumptions", {})
    actual_by_id = {r.refund_id: r for r in execution.refunds}
    for rid, assumed_date in sorted(assumptions.items()):
        rec = actual_by_id.get(rid)
        forecast_check.append({
            "refund_id": rid,
            "assumed_date": assumed_date,
            "actual_date": None if rec is None else rec.actual_date,
            "actual_amount": None if rec is None or not rec.arrived
                             else str(money(rec.amount)),
            "matches_forecast": bool(
                rec is not None and rec.arrived
                and rec.actual_date == assumed_date),
        })

    assert studied_plan.to_dict() == snapshot, "原方案被重算改动"
    return {
        "plan": new_plan,
        "result": result,
        "refund_notes": notes,
        "affected_payments": changed,
        "unaffected_payments": unchanged,
        "forecast_vs_actual": forecast_check,
        "original_plan_kept": studied_plan.created_at,
        "note": "已发起/在途/已到账保持不变；仅未执行部分按实际重算",
    }
