"""支付窗口内的可执行安排枚举与优先级方案对照。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from itertools import product

from .engine import SolveResult, daterange, solve
from .models import CashFlow, FlowKind, Priority, money
from .plan import Plan, TransferOrder
from .scenario import Scenario


MAX_SCHEDULE_COMBINATIONS = 5000


@dataclass(frozen=True)
class ScheduledScenario:
    scenario: Scenario
    dates: dict[str, str]


def outflows_with_windows(scenario: Scenario) -> list[CashFlow]:
    return [f for f in scenario.flows
            if f.kind == FlowKind.OUTFLOW and
            (f.window_start is not None or f.window_end is not None)]


def allowed_dates(flow: CashFlow, strategy: str) -> list[str]:
    start = flow.payment_start
    end = flow.payment_end
    if strategy == "strict_due":
        return [flow.date]
    if strategy == "defer_optional":
        if int(flow.priority) == int(Priority.P3_OPTIONAL):
            return list(daterange(flow.date, end))
        return [flow.date]
    if strategy == "window_priority":
        return list(daterange(max(start, flow.date), end))
    raise ValueError(f"未知支付优先级方案: {strategy}")


def _replace_dates(scenario: Scenario, dates: dict[str, str]) -> Scenario:
    flows = []
    for f in scenario.flows:
        new_date = dates.get(f.flow_id, f.date)
        if f.flow_id in dates:
            f = CashFlow(f.flow_id, f.account_id, new_date, f.kind, f.amount,
                         f.priority, f.memo, f.window_start, f.window_end)
        flows.append(f)
    return Scenario(scenario.scenario_id, scenario.start_date, scenario.end_date,
                    list(scenario.accounts), flows,
                    list(scenario.transfer_rules), scenario.description)


def _iter_schedules(scenario: Scenario, strategy: str,
                    fixed_orders: list[TransferOrder] | None,
                    locked_orders: list[TransferOrder] | None,
                    id_prefix: str):
    window_flows = outflows_with_windows(scenario)
    if not window_flows:
        shifted = _replace_dates(scenario, {})
        result = solve(shifted, fixed_orders=fixed_orders,
                       locked_orders=locked_orders, id_prefix=id_prefix,
                       schedule_windows=False)
        yield ScheduledScenario(shifted, {}), result
        return

    domains = {f.flow_id: allowed_dates(f, strategy) for f in window_flows}
    for chosen in product(*(domains[f.flow_id] for f in window_flows)):
        dates = dict(zip(domains.keys(), chosen))
        shifted = _replace_dates(scenario, dates)
        result = solve(shifted, fixed_orders=fixed_orders,
                       locked_orders=locked_orders, id_prefix=id_prefix,
                       schedule_windows=False)
        yield ScheduledScenario(shifted, dates), result


def _option_summary(strategy: str, dates: dict[str, str], scenario: Scenario,
                    result: SolveResult, exhaustive: bool, rank: int) -> dict:
    original = {f.flow_id: f.date for f in scenario.flows
                if f.kind == FlowKind.OUTFLOW}
    gaps = {}
    for g in result.plan.remaining_gaps:
        fid = g.flow_id if not isinstance(g, dict) else g["flow_id"]
        shortfall = g.shortfall if not isinstance(g, dict) else g["shortfall"]
        gaps[fid] = money(shortfall)
    gap_total = money(sum(gaps.values(), Decimal("0")))
    max_delay = max((0, *(sum(1 for _ in daterange(original[fid], day)) - 1
                          for fid, day in dates.items() if day != original[fid])))
    feasible = gap_total == 0
    if feasible:
        status = "feasible"
    elif exhaustive:
        status = "true_shortfall"
    else:
        status = "search_limit_exceeded"
    return {
        "strategy": strategy,
        "rank": rank,
        "status": status,
        "rescheduled_date": dict(sorted(dates.items())),
        "original_due_date": {fid: original[fid] for fid in sorted(dates)},
        "total_fee": str(result.plan.total_fee),
        "final_gap": str(gap_total),
        "gap_by_flow": {k: str(v) for k, v in sorted(gaps.items())},
        "objective": list(result.plan.objective),
        "max_delay_days": max_delay,
        "exhaustive": exhaustive,
    }


def solve_with_windows(scenario: Scenario,
                       fixed_orders: list[TransferOrder] | None = None,
                       locked_orders: list[TransferOrder] | None = None,
                       id_prefix: str = "T",
                       strategies: list[str] | None = None,
                       eligible_strategies: list[str] | None = None,
                       max_combinations: int = MAX_SCHEDULE_COMBINATIONS
                       ) -> tuple[SolveResult, list[dict], str, dict]:
    scenario.validate()
    strategies = strategies or ["strict_due", "defer_optional", "window_priority"]
    eligible_strategies = eligible_strategies or strategies
    options: list[dict] = []
    trials: list[tuple[ScheduledScenario, SolveResult, dict]] = []
    limited = False
    flow_ids = [f.flow_id for f in scenario.flows
                if f.kind == FlowKind.OUTFLOW]
    exhaustive_by_flow = dict.fromkeys(flow_ids, True)

    for strategy in strategies:
        window_flows = outflows_with_windows(scenario)
        count = 1
        for f in window_flows:
            count *= len(allowed_dates(f, strategy))
        exhaustive = count <= max_combinations
        limited = limited or not exhaustive
        if not exhaustive:
            for f in window_flows:
                exhaustive_by_flow[f.flow_id] = False
        for rank, (scheduled, result) in enumerate(
                _iter_schedules(scenario, strategy, fixed_orders,
                                locked_orders, id_prefix)):
            if rank >= max_combinations:
                break
            summary = _option_summary(strategy, scheduled.dates, scenario,
                                      result, exhaustive, rank)
            options.append(summary)
            trials.append((scheduled, result, summary))

    def preference(item):
        scheduled, result, summary = item
        gaps = tuple(result.plan.objective[:3])
        strategy_rank = strategies.index(summary["strategy"])
        priority_dates = tuple(
            summary["rescheduled_date"].get(flow.flow_id, flow.date)
            for flow in sorted(
                [f for f in scenario.flows
                 if f.kind == FlowKind.OUTFLOW],
                key=lambda f: (f.priority, f.flow_id)))
        return (gaps, summary["max_delay_days"], result.plan.total_fee,
                strategy_rank, priority_dates,
                tuple(sorted(scheduled.dates.items())))

    eligible_trials = [x for x in trials if x[2]["strategy"] in eligible_strategies]
    scheduled, chosen, best_summary = min(eligible_trials, key=preference)
    gap_total = sum(chosen.plan.objective[:3])
    if gap_total == 0:
        feasibility = "feasible"
    elif any(o["status"] == "feasible" for o in options):
        feasibility = "strategy_not_adopted"
    elif not limited:
        feasibility = "true_shortfall"
    else:
        feasibility = "search_limit_exceeded"

    decision_status = dict.fromkeys(flow_ids, "true_shortfall")
    for o in options:
        if not o["exhaustive"]:
            for fid in flow_ids:
                if decision_status[fid] == "true_shortfall":
                    decision_status[fid] = "search_incomplete"
        for fid, gap in o["gap_by_flow"].items():
            if money(gap) == 0 and decision_status.get(fid) != "feasible":
                decision_status[fid] = "feasible"
    chosen_gaps = {g["flow_id"]: money(g["shortfall"])
                   for g in chosen.plan.remaining_gaps}
    for fid in flow_ids:
        if chosen_gaps.get(fid, Decimal("0")) == 0:
            decision_status[fid] = "feasible"
        elif decision_status.get(fid) == "feasible":
            decision_status[fid] = "strategy_not_adopted"

    plan = chosen.plan
    plan.selected_strategy = best_summary["strategy"]
    plan.payment_strategy_options = options
    plan.feasibility_status = feasibility
    plan.schedule_basis = {
        "original_dates_preserved": True,
        "scheduled_date": best_summary["rescheduled_date"],
        "original_due_date": best_summary["original_due_date"],
        "searched_at": datetime.now().isoformat(timespec="seconds"),
        "combination_limit": max_combinations,
        "flow_feasibility": decision_status,
    }
    return chosen, options, feasibility, plan.schedule_basis
