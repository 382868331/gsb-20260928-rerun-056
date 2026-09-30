"""调拨延误后的剩余计划重规划（第二轮核心）。

在某个截断日 cutoff 的早晨（当日收入/调拨/支出均尚未过账），根据
实际执行记录重建：

1) 已发起资金保持在途——截断前已扣本金，重规划不得再从原账户动用；
2) 部分/延迟收入、生效日之后的手续费上调，重新评估每笔支付的期限与费用；
3) 允许推迟可选支付（P3）或选用更贵但及时的路径；
4) 实际执行记录与修订计划分开保存，重开后可用全程重放对清资金去向。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from itertools import combinations

from .engine import (ReplayResult, add_days, daterange, replay, solve)
from .ledger import Ledger, TransferLeg
from .models import CashFlow, FlowKind, Priority, money
from .plan import (
    ActualTransfer, Execution, Plan, RevisedPlan, RuleChange, TransferOrder,
)
from .scenario import Scenario


@dataclass
class ReplanResult:
    revision: RevisedPlan
    replay: ReplayResult        # 剩余窗口（截断日起）重放
    before: ReplayResult        # 截断前实际重放
    deferrals: dict[str, str]   # flow_id -> 推迟后日期


def _actual_legs(execution: Execution) -> list[TransferLeg]:
    return Ledger.legs_from_orders(execution.actual_transfers)


def replay_to_cutoff(scenario: Scenario, execution: Execution,
                     cutoff: str) -> ReplayResult:
    """重放 [start, cutoff-1] 的实际执行，得到截断日早晨的真实状态。"""
    before_end = add_days(cutoff, -1)
    sub = replace(scenario, start_date=scenario.start_date, end_date=before_end)
    sub.validate(check_dates=False)
    inflow_overrides = {
        f.flow_id: (f.actual_date, f.actual_amount)
        for f in execution.actual_inflows
    }
    legs = [leg for leg in _actual_legs(execution)
            if leg.initiate_date <= before_end]
    return replay(sub, legs=legs, inflow_overrides=inflow_overrides,
                  snapshot_daily=True)


def apply_rule_changes(scenario: Scenario, changes: list[RuleChange],
                       cutoff: str) -> Scenario:
    """把生效日 <= cutoff 的规则变化落到场景上（影响截断后新发起的调拨）。

    在途调拨沿用发起时条件，不受影响。
    """
    rules = []
    for r in scenario.transfer_rules:
        applicable = [c for c in changes
                      if c.source == r.source and c.target == r.target
                      and c.effective_date <= cutoff]
        applicable.sort(key=lambda c: c.effective_date)
        nr = r
        for c in applicable:
            nr = replace(
                nr,
                lag_days=c.lag_days if c.lag_days is not None else nr.lag_days,
                fee_rate=c.fee_rate if c.fee_rate is not None else nr.fee_rate,
                fee_fixed=c.fee_fixed if c.fee_fixed is not None else nr.fee_fixed,
                max_amount=(c.max_amount if c.max_amount is not None
                            else nr.max_amount),
            )
        rules.append(nr)
    return replace(scenario, transfer_rules=rules)


def build_continuation(scenario: Scenario, before: ReplayResult,
                       execution: Execution, cutoff: str,
                       changes: list[RuleChange]) -> Scenario:
    """构造截断日起的剩余场景：真实现金为 opening，规则取生效后版本。"""
    accounts = []
    for a in scenario.accounts:
        accounts.append(replace(a, opening_balance=before.ledger.available(
            a.account_id)))

    inflow_ids_seen = {f.flow_id for f in execution.actual_inflows
                       if f.actual_date < cutoff}
    flows = []
    for f in scenario.flows:
        if f.kind == FlowKind.INFLOW:
            actual = next((x for x in execution.actual_inflows
                           if x.flow_id == f.flow_id), None)
            if actual is not None:
                if actual.actual_date < cutoff:
                    continue  # 截断前已处理
                if actual.actual_date > scenario.end_date:
                    continue  # 超出推演窗口，视为未到账
                flows.append(CashFlow(
                    f.flow_id, f.account_id, actual.actual_date, f.kind,
                    actual.actual_amount, f.priority,
                    f.memo + f"（实际部分到账 {actual.actual_amount}）"))
            else:
                flows.append(f)
        else:
            if f.date < cutoff:
                continue
            flows.append(f)

    cont = replace(scenario, start_date=cutoff, accounts=accounts, flows=flows)
    return apply_rule_changes(cont, changes, cutoff)


def continuation_overrides(execution: Execution, cutoff: str) -> dict:
    overrides = {
        f.flow_id: (f.actual_date, f.actual_amount)
        for f in execution.actual_inflows if f.actual_date >= cutoff
    }
    # 窗口结束仍未到账的部分收入：标记为不到账（actual_amount 已在场景外处理）
    return overrides


def _optional_outflows(scenario: Scenario, cutoff: str) -> list[CashFlow]:
    return [f for f in scenario.flows
            if f.kind == FlowKind.OUTFLOW
            and int(f.priority) == int(Priority.P3_OPTIONAL)
            and f.date >= cutoff]


def revise_plan(scenario: Scenario, base_plan: Plan, execution: Execution,
                cutoff: str, revision_no: int = 1,
                allow_defer: bool = True,
                max_defer_combos: int = 64) -> ReplanResult:
    """基于已发生事实，给出截断日起的可执行剩余计划。"""
    scenario.validate()
    before = replay_to_cutoff(scenario, execution, cutoff)

    # 截断日早晨仍在途的已发起调拨：本金已离开原账户，只等到账，不再动用
    in_transit = [t for t in execution.actual_transfers
                  if t.status != "cancelled" and t.actual_initiate_date < cutoff
                  and t.actual_arrival_date >= cutoff]
    committed = [t for t in execution.actual_transfers
                 if t.status != "cancelled" and t.actual_initiate_date < cutoff]
    preload = Ledger.legs_from_orders(in_transit)

    cont = build_continuation(scenario, before, execution, cutoff,
                              execution.rule_changes)
    overrides = continuation_overrides(execution, cutoff)

    # 候选推迟方案：枚举可选（P3）支出是否推迟到窗口末日（资金最宽松）
    optional = _optional_outflows(cont, cutoff) if allow_defer else []
    defer_options: list[dict[str, str]] = [{}]
    if optional:
        idxs = list(range(len(optional)))
        for size in range(1, len(optional) + 1):
            for combo in combinations(idxs, size):
                mapping = {optional[i].flow_id: cont.end_date for i in combo}
                defer_options.append(mapping)
                if len(defer_options) >= max_defer_combos:
                    break
            if len(defer_options) >= max_defer_combos:
                break

    best = None
    best_result = None
    best_defer: dict[str, str] = {}
    for defer_map in defer_options:
        solved = solve(cont, solver_name="greedy-revise",
                       preload_legs=preload,
                       order_prefix=f"R{revision_no:02d}",
                       inflow_overrides=overrides, outflow_overrides=defer_map)
        score = tuple(solved.plan.objective)
        # 仅当推迟确实减少缺口金额才接受推迟（不改变 P1/P2）。
        if best is None or score < best:
            best, best_result, best_defer = score, solved, dict(defer_map)

    assert best_result is not None
    new_orders = best_result.plan.orders

    # 逐条记录选择理由：与原计划同 source/target 对比费用；更贵路径需注明
    choices = _explain_choices(scenario, cont, base_plan, execution,
                               cutoff, new_orders, best_defer)

    deferrals = [
        {"flow_id": fid, "original_date": next(
             f.date for f in scenario.flows if f.flow_id == fid),
         "deferred_to": d, "priority": 3,
         "reason": "P3 可选支付：当前资金优先保 P1/P2，推迟以避免高费用/断裂，"
                   "于窗口末日资金最宽松时再付"}
        for fid, d in sorted(best_defer.items())
    ]

    # 总费用：已发生（截断前实际手续费）+ 在途预载手续费 + 新增调拨手续费
    fee_committed = money(sum((t.actual_fee for t in committed), Decimal("0")))
    fee_new = money(sum((o.fee for o in new_orders), Decimal("0")))
    total_fee = money(fee_committed + fee_new)

    by_pri = best_result.replay.gap_total_by_priority()
    revision = RevisedPlan(
        scenario_id=scenario.scenario_id,
        base_plan_created_at=base_plan.created_at,
        revision_no=revision_no,
        created_at=datetime.now().isoformat(timespec="seconds"),
        cutoff_date=cutoff,
        committed_orders=sorted(committed, key=lambda t: t.actual_initiate_date),
        revised_orders=sorted(new_orders,
                              key=lambda o: (o.initiate_date, o.order_id)),
        deferrals=deferrals,
        choices=choices,
        remaining_gaps=best_result.plan.remaining_gaps,
        total_fee=total_fee,
        objective=best_result.plan.objective,
        notes=(
            f"截断日 {cutoff} 早晨重规划。已发起 {len(committed)} 笔"
            f"（其中在途 {len(in_transit)} 笔，本金不重复占用）；"
            f"新增调拨 {len(new_orders)} 笔手续费 {fee_new}，"
            f"已发生手续费 {fee_committed}；目标序 "
            f"(P1缺口,P2缺口,P3缺口,新增手续费) 字典序最小。"
        ),
    )
    return ReplanResult(revision=revision, replay=best_result,
                        before=before, deferrals=best_defer)


def _explain_choices(scenario: Scenario, cont: Scenario, base_plan: Plan,
                     execution: Execution, cutoff: str,
                     new_orders: list[TransferOrder],
                     defer_map: dict[str, str]) -> list[dict]:
    choices: list[dict] = []
    base_rule = {(r.source, r.target): r for r in scenario.transfer_rules}
    for o in new_orders:
        rule = cont.rule_between(o.source, o.target)
        old = base_rule.get((o.source, o.target))
        reason_parts = []
        if old is not None and (rule.fee_rate > old.fee_rate
                                or rule.fee_fixed > old.fee_fixed
                                or rule.lag_days < old.lag_days):
            reason_parts.append(
                f"更贵但及时：{o.source}->{o.target} 费率由 "
                f"{old.fee_rate}(+{old.fee_fixed}) 调整为 "
                f"{rule.fee_rate}(+{rule.fee_fixed})，到账 {rule.lag_days} 天")
        else:
            cheapest = _cheapest_feasible(cont, o)
            if cheapest and cheapest != (o.source, o.target):
                reason_parts.append(
                    f"更便宜路径 {cheapest[0]}->{cheapest[1]} 受时延/在途延误影响"
                    f"无法在 {o.arrival_date} 前到账，故选当前路径")
        if not reason_parts:
            reason_parts.append("当前规则下费用最低且能按期到账的可行路径")
        choices.append({
            "order_id": o.order_id, "source": o.source,
            "target": o.target, "initiate_date": o.initiate_date,
            "arrival_date": o.arrival_date, "principal": str(o.principal),
            "fee": str(o.fee), "reason": "；".join(reason_parts),
        })
    return choices


def _cheapest_feasible(cont: Scenario, order: TransferOrder):
    cands = [r for r in cont.transfer_rules
             if r.target == order.target and r.source != order.source]
    cands.sort(key=lambda r: (r.fee_rate, r.fee_fixed))
    return (cands[0].source, cands[0].target) if cands else None


def replay_full(scenario: Scenario, execution: Execution,
                revision: RevisedPlan) -> ReplayResult:
    """用 实际前缀 + 修订后缀 全程重放，重开后对清资金去向。"""
    inflow_overrides = {
        f.flow_id: (f.actual_date, f.actual_amount)
        for f in execution.actual_inflows
    }
    outflow_overrides = {d["flow_id"]: d["deferred_to"]
                         for d in revision.deferrals}
    before_legs = [leg for leg in _actual_legs(execution)
                   if leg.initiate_date < revision.cutoff_date]
    after_legs = Ledger.legs_from_orders(revision.revised_orders)
    return replay(scenario, legs=before_legs + after_legs,
                  inflow_overrides=inflow_overrides,
                  outflow_overrides=outflow_overrides, snapshot_daily=True)
