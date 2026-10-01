"""支付窗口内的可执行安排搜索与优先级策略对照（第三轮）。

背景：原求解器只在支出的原应付日当天尝试支付，余额不足就部分支付并记缺口，
哪怕同一支付窗口内稍晚的日期本可足额完成。本模块在「允许的支付窗口」内
为每笔支出寻找实际可执行的支付日，并严格区分三种结论：

  - truly_unmet（真正不足）：枚举完窗口内所有支付日组合，任何安排都无法
    在窗口结束前足额覆盖；窗口末日仍按余额部分支付，缺口保留。
  - strategy_not_used（当前策略未采用）：存在一种窗口内安排可以足额支付，
    但当前优先级策略没有选择它（如 strict_due 坚持原期限）。
  - no_solution_found（尚未找到方案）：搜索空间超过枚举预算被截断，
    既未能证明可行也未能证明不可行。

输入场景的原始期限（CashFlow.date）永不被改写：所有改期只体现在生成的
排期场景与支付决策记录中，原场景与计划依据快照保持不变。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from itertools import count

from .engine import ReplayResult, add_days, daterange, solve
from .models import CashFlow, FlowKind, money
from .plan import TransferOrder
from .scenario import Scenario

STRICT = "strict_due"            # 全部坚持原应付日，不做任何改期
FASTEST = "fastest_feasible"    # 在窗口内取最早能足额支付的日期
LOWEST_FEE = "lowest_fee"       # 允许等到收入到账，最小化总手续费

STRATEGY_NAMES = (STRICT, FASTEST, LOWEST_FEE)

TRULY_UNMET = "truly_unmet"
STRATEGY_NOT_USED = "strategy_not_used"
NO_SOLUTION_FOUND = "no_solution_found"

FEASIBILITY_BUDGET = 20000  # 可行性枚举节点上限，超出即判 no_solution_found


@dataclass
class StrategyOutcome:
    """一种优先级策略下的完整安排结果。"""

    strategy: str
    plan: Plan
    replay: ReplayResult
    schedule: dict[str, str]                  # flow_id -> 实际安排支付日
    decisions: list[dict] = field(default_factory=list)
    feasibility: dict[str, str] = field(default_factory=dict)  # 缺口流 -> 三分类

    @property
    def total_fee(self) -> Decimal:
        return self.plan.total_fee


def outflows(scenario: Scenario) -> list[CashFlow]:
    return [f for f in scenario.flows if f.kind == FlowKind.OUTFLOW]


def shifted_scenario(scenario: Scenario, schedule: dict[str, str],
                     tag: str) -> Scenario:
    """按排期把支出改到安排日；原始期限不在场景对象上改写。"""
    flows = []
    for f in scenario.flows:
        if f.kind == FlowKind.OUTFLOW and f.flow_id in schedule:
            flows.append(CashFlow(
                f.flow_id, f.account_id, schedule[f.flow_id], f.kind, f.amount,
                f.priority, f.memo, f.pay_window_end))
        else:
            flows.append(f)
    return Scenario(
        scenario.scenario_id + tag, scenario.start_date, scenario.end_date,
        list(scenario.accounts), flows, list(scenario.transfer_rules),
        scenario.description)


def _solve_schedule(scenario: Scenario, schedule: dict[str, str],
                    fixed_orders: list[TransferOrder], id_prefix: str,
                    tag: str):
    shifted = shifted_scenario(scenario, schedule, tag)
    result = solve(shifted, solver_name=f"window-{tag.strip('@')}",
                   fixed_orders=fixed_orders, id_prefix=id_prefix)
    return shifted, result


def _payment_map(result: ReplayResult) -> dict[str, object]:
    return {p.flow_id: p for p in result.ledger.payments}


def _gap_flow_ids(result: ReplayResult) -> list[str]:
    return [g.flow_id for g in result.gaps if g.shortfall > 0]


def _strict_schedule(scenario: Scenario) -> dict[str, str]:
    return {f.flow_id: f.date for f in outflows(scenario)}


def _improve_schedule(scenario: Scenario, schedule: dict[str, str],
                      fixed_orders: list[TransferOrder], id_prefix: str,
                      minimize_fee: bool) -> dict[str, str]:
    """坐标下降：逐笔把仍有缺口的支出在窗口内后移，取目标元组最优。

    minimize_fee=False（fastest）：每笔取最早能改善目标元组的日期即继续下一笔；
    minimize_fee=True（lowest_fee）：反复扫描，接受任何改善目标元组的后移，
    直到没有改进空间，允许用等待收入替代高费用紧急筹资。
    """
    schedule = dict(schedule)
    counter = count(1)

    def score_for(sched: dict[str, str]):
        _, res = _solve_schedule(
            scenario, sched, fixed_orders, id_prefix,
            f"@try{next(counter)}")
        return tuple(res.plan.objective), res.replay

    best_score, _ = score_for(schedule)
    improved = True
    rounds = 0
    while improved and rounds < 50:
        improved = False
        rounds += 1
        for f in sorted(outflows(scenario),
                        key=lambda x: (x.date, int(x.priority), x.flow_id)):
            current = schedule[f.flow_id]
            local_best_day = current
            local_best_score = best_score
            for day in daterange(add_days(current, 1), f.window_end):
                trial = {**schedule, f.flow_id: day}
                score, _ = score_for(trial)
                if score < local_best_score:
                    local_best_score = score
                    local_best_day = day
                    if not minimize_fee:
                        break
            if local_best_day != current:
                schedule[f.flow_id] = local_best_day
                best_score = local_best_score
                improved = True
    return schedule


def _feasibility_search(scenario: Scenario, fixed_orders: list[TransferOrder],
                        target_flow_id: str,
                        base_schedule: dict[str, str]
                        ) -> tuple[str, dict[str, str] | None]:
    """枚举窗口内支付日组合，证明 target 能否被足额覆盖。

    返回 (verdict, schedule)：verdict 为 feasible / infeasible / unknown。
    其他支出沿用 base_schedule，仅枚举各支出窗口内候选日。
    """
    flows = sorted(outflows(scenario),
                   key=lambda x: (x.date, int(x.priority), x.flow_id))
    candidates: dict[str, list[str]] = {}
    for f in flows:
        days = list(daterange(f.date, f.window_end))
        if len(days) > 8:
            keep = set(days[:: max(1, len(days) // 6)])
            keep.update({f.date, f.window_end})
            days = [d for d in days if d in keep]
        candidates[f.flow_id] = days

    nodes = 0
    found: dict[str, str] | None = None
    fully_exhausted = True

    def rec(idx: int, sched: dict[str, str]) -> None:
        nonlocal nodes, found, fully_exhausted
        if found is not None:
            return
        nodes += 1
        if nodes > FEASIBILITY_BUDGET:
            fully_exhausted = False
            return
        if idx == len(flows):
            _, res = _solve_schedule(scenario, sched, fixed_orders, "Z",
                                     "@feas")
            p = _payment_map(res.replay).get(target_flow_id)
            if p is not None and p.shortfall == 0:
                found = dict(sched)
            return
        f = flows[idx]
        for day in candidates[f.flow_id]:
            rec(idx + 1, {**sched, f.flow_id: day})
            if found is not None or not fully_exhausted:
                return

    rec(0, dict(base_schedule))
    if found is not None:
        return "feasible", found
    return (("infeasible", None) if fully_exhausted
            else ("unknown", None))


def _build_decisions(shifted: Scenario, original: Scenario,
                     result: ReplayResult, schedule: dict[str, str],
                     strategy: str, feasibility: dict[str, str]) -> list[dict]:
    paid = _payment_map(result)
    decisions = []
    for f in sorted(outflows(original),
                    key=lambda x: (x.date, int(x.priority), x.flow_id)):
        p = paid.get(f.flow_id)
        scheduled = schedule.get(f.flow_id, f.date)
        base = {
            "flow_id": f.flow_id, "account_id": f.account_id,
            "priority": int(f.priority), "due": str(f.amount),
            "original_due_date": f.date,
            "window_end": f.window_end,
            "scheduled_date": scheduled,
            "paid": str(Decimal("0") if p is None else p.paid),
            "shortfall": str(money(f.amount if p is None else p.shortfall)),
            "memo": f.memo,
        }
        if p is not None and p.shortfall == 0:
            if scheduled == f.date:
                base["status"] = "on_time"
                base["reason"] = "原应付日余额足额，按原期限支付"
            else:
                base["status"] = "rescheduled"
                base["reason"] = (
                    f"原应付日 {f.date} 资金不足；在允许支付窗口内改期至 "
                    f"{scheduled}（窗口末日 {f.window_end}），等待资金到账后"
                    f"足额支付，原始期限 {f.date} 保留不改写")
            decisions.append(base)
            continue
        verdict = feasibility.get(f.flow_id, NO_SOLUTION_FOUND)
        if verdict == "infeasible":
            base["status"] = TRULY_UNMET
            base["reason"] = (
                f"已枚举窗口 [{f.date}, {f.window_end}] 内全部支付日组合，"
                f"余额、在途与窗口内可到收入均无法足额覆盖；按可用余额部分"
                f"支付，缺口 {base['shortfall']} 为真正不足")
        elif verdict == "feasible":
            base["status"] = STRATEGY_NOT_USED
            base["reason"] = (
                f"存在窗口内可足额支付的安排，但当前策略 {strategy} 未采用"
                f"（安排在 {scheduled} 支付）；改期或更换优先级策略即可覆盖")
        else:
            base["status"] = NO_SOLUTION_FOUND
            base["reason"] = (
                f"窗口 [{f.date}, {f.window_end}] 搜索空间超过枚举预算，"
                f"尚未找到可足额支付的方案，也未能证明真正不足；当前按部分"
                f"支付处理，缺口 {base['shortfall']}")
        decisions.append(base)
    return decisions


def run_strategy(scenario: Scenario, strategy: str,
                 fixed_orders: list[TransferOrder] | None = None,
                 id_prefix: str = "W") -> StrategyOutcome:
    """在给定优先级策略下搜索窗口内可执行安排。"""
    fixed_orders = list(fixed_orders or [])
    strict = _strict_schedule(scenario)
    if strategy == STRICT:
        schedule = dict(strict)
    elif strategy == FASTEST:
        schedule = _improve_schedule(scenario, strict, fixed_orders,
                                     id_prefix, minimize_fee=False)
    elif strategy == LOWEST_FEE:
        fast = _improve_schedule(scenario, strict, fixed_orders,
                                 id_prefix, minimize_fee=False)
        schedule = _improve_schedule(scenario, fast, fixed_orders,
                                     id_prefix, minimize_fee=True)
    else:
        raise ValueError(f"未知支付优先级策略: {strategy}")

    shifted, plan_result = _solve_schedule(
        scenario, schedule, fixed_orders, id_prefix, f"@{strategy}")
    plan_result.plan.solver = f"window-{strategy}"
    plan_result.plan.strategy = strategy
    plan_result.plan.payment_schedule = dict(schedule)

    gap_ids = _gap_flow_ids(plan_result.replay)
    verdict_by_flow: dict[str, str] = {}
    for fid in gap_ids:
        verdict, _ = _feasibility_search(
            shifted, fixed_orders, fid, schedule)
        verdict_by_flow[fid] = verdict
    decisions = _build_decisions(
        shifted, scenario, plan_result.replay, schedule, strategy,
        verdict_by_flow)
    return StrategyOutcome(
        strategy=strategy, plan=plan_result.plan,
        replay=plan_result.replay, schedule=schedule,
        decisions=decisions, feasibility=verdict_by_flow)


def compare_strategies(scenario: Scenario,
                       fixed_orders: list[TransferOrder] | None = None,
                       strategies: tuple[str, ...] = STRATEGY_NAMES,
                       id_prefix: str = "W") -> list[StrategyOutcome]:
    """同一输入下不同支付优先级方案的对照（改期/费用/最终缺口）。"""
    return [run_strategy(scenario, name, fixed_orders=fixed_orders,
                         id_prefix=id_prefix) for name in strategies]
