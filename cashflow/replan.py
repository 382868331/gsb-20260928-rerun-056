"""调拨延误后的修订重规划。

输入：原场景、原计划、截至 cutoff_date 的实际执行记录（含延误/部分到账/费率调整）。
输出：一份新的修订 Plan（revision_of 指向原计划），其中：

  - 已发起的资金保持在途（fixed_transit），重放时只补到账、不再从原账户扣一次；
  - 未发起的计划调拨全部作废，剩余需求按当前余额/在途/收入重新求解；
  - 到账时间与费率变动后，逐笔重新评估每笔支付的期限与费用；
  - 允许「推迟可选支付（P3）」或「更贵但及时的路径」，决策与理由逐条记录；
  - 仍无法满足的部分列入 remaining_gaps / unmet 决策。

实际执行记录（Execution）与修订计划（Plan.revision_of 非空）分开持久化，
重开后可用 combined_replay 从期初一路对清资金去向。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from .engine import ReplayResult, add_days, daterange, replay, solve
from .ledger import Ledger, TransferLeg
from .models import FlowKind, Priority, money
from .plan import ActualInflow, Execution, Plan, TransferOrder
from .scenario import Scenario


@dataclass(frozen=True)
class FeeAmendment:
    """费率调整：自 effective_date 起新发起的调拨按新费率收费。"""

    source: str
    target: str
    effective_date: str
    fee_rate: Decimal | None = None    # None 表示不变
    fee_fixed: Decimal | None = None

    def to_dict(self) -> dict:
        return {
            "source": self.source, "target": self.target,
            "effective_date": self.effective_date,
            "fee_rate": None if self.fee_rate is None else str(self.fee_rate),
            "fee_fixed": None if self.fee_fixed is None else str(self.fee_fixed),
        }


@dataclass
class ReplanResult:
    plan: Plan                 # 修订计划（新单号 R***）
    residual_replay: ReplayResult   # 残差场景（cutoff 次日起）重放结果
    prefix_replay: ReplayResult     # 实际执行前缀（期初..cutoff）重放结果


def _amend_rules(scenario: Scenario, amendments: list[FeeAmendment],
                 horizon_start: str) -> Scenario:
    rules = []
    applied: dict[tuple[str, str], FeeAmendment] = {}
    for a in amendments:
        if a.effective_date <= horizon_start:
            applied[(a.source, a.target)] = a
    for r in scenario.transfer_rules:
        a = applied.get((r.source, r.target))
        if a is None:
            rules.append(r)
        else:
            object.__setattr__(r, "fee_rate",
                               r.fee_rate if a.fee_rate is None else a.fee_rate)
            object.__setattr__(r, "fee_fixed",
                               r.fee_fixed if a.fee_fixed is None else a.fee_fixed)
            rules.append(r)
    scenario.transfer_rules = rules
    return scenario


def _actual_map(execution: Execution) -> dict[str, ActualInflow]:
    return {f.flow_id: f for f in execution.actual_inflows}


def build_prefix(scenario: Scenario, execution: Execution,
                 cutoff_date: str) -> ReplayResult:
    """按实际执行重放 期初..cutoff，得到真实余额、在途、已付与已到收入。"""
    actuals = _actual_map(execution)

    # 构造前缀场景：往期收入按实际到账金额/日期改写，往期支出保留应付额。
    from .scenario import Scenario as _Sc
    from .models import CashFlow
    prefix_flows = []
    for f in scenario.flows:
        if f.date > cutoff_date:
            continue
        if f.kind == FlowKind.INFLOW:
            act = actuals.get(f.flow_id)
            if act is None:
                # 无实际记录的往期收入视为未到账（部分收入未到的核心场景）
                continue
            if act.actual_date > cutoff_date or act.amount == 0:
                continue  # 延迟到 cutoff 之后，或确认未到
            prefix_flows.append(CashFlow(
                f.flow_id, f.account_id, act.actual_date, f.kind, act.amount,
                f.priority, f.memo))
        else:
            prefix_flows.append(f)
    prefix_sc = _Sc(
        scenario.scenario_id + "@prefix", scenario.start_date, cutoff_date,
        list(scenario.accounts), prefix_flows,
        list(scenario.transfer_rules), scenario.description)

    legs = []
    seen = set()
    for t in execution.actual_transfers:
        if t.status == "cancelled":
            continue
        if t.order_id in seen:
            raise ValueError(f"实际调拨单号重复: {t.order_id}")
        seen.add(t.order_id)
        if t.actual_initiate_date > cutoff_date:
            continue
        legs.append(TransferLeg(
            t.order_id, t.source, t.target,
            t.actual_initiate_date, t.actual_arrival_date,
            money(t.actual_principal), money(t.actual_fee)))
    return replay(prefix_sc, legs=legs, snapshot_daily=True)


def _residual_scenario(scenario: Scenario, prefix: ReplayResult,
                       execution: Execution, cutoff_date: str,
                       amendments: list[FeeAmendment],
                       inflow_updates: dict[str, tuple[str, Decimal]],
                       outflow_shifts: dict[str, str],
                       keep_windows: bool = False) -> tuple[Scenario, dict]:
    """以 cutoff 日终真实状态为起点，构造残差推演场景。

    返回 (残差场景, 辅助信息)；辅助信息含往期部分支付额、原始期限等。
    """
    from .models import Account, CashFlow
    first_day = add_days(cutoff_date, 1)
    led = prefix.ledger
    paid_map = {p.flow_id: p.paid for p in led.payments}
    due_map = {p.flow_id: (p.due, p.date, p.priority) for p in led.payments}
    actuals = _actual_map(execution)

    residual_flows = []
    carry_info = {}
    for f in scenario.flows:
        if f.kind == FlowKind.OUTFLOW:
            if f.date <= cutoff_date:
                unpaid = f.amount - paid_map.get(f.flow_id, Decimal("0"))
                if unpaid <= 0:
                    continue
                # 过期未付：残差首日必须补付，记录原期限已错过
                new_date = outflow_shifts.get(f.flow_id, first_day)
                if new_date < first_day:
                    new_date = first_day
                window_end = f.window_end if keep_windows and f.window_end >= first_day else ""
                if window_end and window_end < new_date:
                    window_end = new_date
                carry_info[f.flow_id] = {"original_date": f.date,
                                         "residual_date": new_date}
                residual_flows.append(CashFlow(
                    f.flow_id, f.account_id, new_date, f.kind, money(unpaid),
                    f.priority,
                    f"{f.memo}（{f.date} 到期，延误后补付）",
                    window_end))
            else:
                new_date = outflow_shifts.get(f.flow_id, f.date)
                window_end = f.window_end if keep_windows else ""
                residual_flows.append(CashFlow(
                    f.flow_id, f.account_id, new_date, f.kind, f.amount,
                    f.priority, f.memo, window_end))
        else:  # 收入
            act = actuals.get(f.flow_id)
            if f.date <= cutoff_date:
                # 往期延迟到账的收入，放入残差实际到账日
                if act is not None and act.actual_date >= first_day and act.amount > 0:
                    residual_flows.append(CashFlow(
                        f.flow_id, f.account_id, act.actual_date, f.kind,
                        act.amount, f.priority, f.memo))
                continue
            new_date, new_amount = f.date, f.amount
            if f.flow_id in inflow_updates:
                new_date, new_amount = inflow_updates[f.flow_id]
            if new_amount > 0:
                residual_flows.append(CashFlow(
                    f.flow_id, f.account_id, new_date, f.kind, money(new_amount),
                    f.priority, f.memo))

    residual = Scenario(
        scenario.scenario_id + "@residual", first_day, scenario.end_date,
        [Account(a.account_id, a.name, led.available(a.account_id))
         for a in scenario.accounts],
        residual_flows, list(scenario.transfer_rules), scenario.description)
    _amend_rules(residual, amendments, first_day)
    residual.validate()
    return residual, {"carry": carry_info, "paid": paid_map, "due": due_map}


def _fixed_transit_orders(prefix: ReplayResult) -> list[TransferOrder]:
    """cutoff 时仍在途的调拨：本金已扣，残差中只等到账，绝不再扣一次。"""
    orders = []
    for oid, leg in prefix.ledger.in_transit.items():
        orders.append(TransferOrder(
            f"FIXED-{oid}", leg.source, leg.target,
            leg.initiate_date, leg.arrival_date, leg.principal, Decimal("0"),
            purpose="延误后仍在途，保持不动"))
    return orders


def _decisions_for(scenario: Scenario, result: ReplayResult,
                   carry: dict[str, dict], chain_quote,
                   deferred: dict[str, str]) -> list[dict]:
    """逐笔支付给出决策：met（准时/补付）、deferred、unmet，并写明理由。"""
    paid = {p.flow_id: p for p in result.ledger.payments}
    decisions = []
    for f in sorted(
            [x for x in scenario.flows if x.kind == FlowKind.OUTFLOW],
            key=lambda x: (x.date, int(x.priority))):
        p = paid.get(f.flow_id)
        info = carry.get(f.flow_id)
        base = {"flow_id": f.flow_id, "account_id": f.account_id,
                "priority": int(f.priority), "due": str(f.amount),
                "scheduled_date": f.date,
                "original_due_date": (info or {}).get("original_date", f.date),
                "memo": f.memo}
        if p is None or p.shortfall > 0:
            shortfall = f.amount if p is None else p.shortfall
            status = "unmet"
            reason = ("余额、在途资金与后续收入均无法在推演期内覆盖，"
                      "保持缺口，不重复占用已发起资金")
            decisions.append({**base, "status": status,
                              "paid": str(Decimal("0") if p is None else p.paid),
                              "shortfall": str(money(shortfall)),
                              "reason": reason})
            continue
        if info is not None:
            status = "late_paid"
            reason = (f"原期限 {info['original_date']} 已错过：在途调拨延误，"
                      f"cutoff 前无可用资金；{f.date} 补付")
        elif f.flow_id in deferred:
            status = "deferred"
            reason = deferred[f.flow_id]
        else:
            status = "on_time"
            reason = "到期日余额足额，按原期限支付"
        quote = chain_quote.get(f.flow_id)
        if quote is not None and status == "on_time" and quote["extra_fee"] > 0:
            reason = (f"准时路径最低费用 {quote['timely_fee']}，而最便宜路径 "
                      f"{quote['cheap_fee']} 在延误后到账晚于 {f.date}；"
                      f"采用更贵但及时的路径，多付 {quote['extra_fee']}")
        decisions.append({**base, "status": status, "paid": str(p.paid),
                          "shortfall": "0.00", "reason": reason})
    return decisions


def _try_defer_p3(scenario: Scenario, fixed: list[TransferOrder],
                  plan_result) -> tuple[list[TransferOrder], ReplayResult, dict, dict]:
    """对仍有缺口的 P3 可选支付，尝试推迟到后续有资金的日期；取目标元组最优。"""
    best_orders, best_result, best_score = (
        plan_result.plan.orders, plan_result.replay,
        tuple(plan_result.plan.objective))
    shifts: dict[str, str] = {}
    reasons: dict[str, str] = {}
    p3_gaps = [g for g in best_result.gaps if g.priority == Priority.P3_OPTIONAL]
    for g in p3_gaps:
        flow = next(f for f in scenario.flows
                    if f.flow_id == g.flow_id and f.kind == FlowKind.OUTFLOW)
        for day in daterange(add_days(g.date, 1), scenario.end_date):
            shifted = Scenario(
                scenario.scenario_id, scenario.start_date, scenario.end_date,
                list(scenario.accounts),
                [f if f.flow_id != flow.flow_id else
                 type(f)(f.flow_id, f.account_id, day, f.kind, f.amount,
                         f.priority, f.memo) for f in scenario.flows],
                list(scenario.transfer_rules), scenario.description)
            trial = solve(shifted, fixed_orders=fixed, id_prefix="R")
            score = tuple(trial.plan.objective)
            if score < best_score:
                best_orders, best_result, best_score = (
                    trial.plan.orders, trial.replay, score)
                shifts[flow.flow_id] = day
                reasons[flow.flow_id] = (
                    f"可选支付，{g.date} 可用资金不足（缺口 {money(g.shortfall)}）；"
                    f"推迟至 {day}，等待后续资金到账后支付，避免高费率紧急筹资")
                scenario = shifted
                break
    return best_orders, best_result, shifts, reasons


def replan(scenario: Scenario, original_plan: Plan, execution: Execution,
           cutoff_date: str,
           amendments: list[FeeAmendment] | None = None,
           inflow_updates: dict[str, tuple[str, Decimal]] | None = None,
           outflow_shifts: dict[str, str] | None = None,
           allow_defer_p3: bool = True,
           chain_quote: dict | None = None,
           window_strategy: str | None = None,
           record_basis: bool = True) -> ReplanResult:
    """根据实际执行与参数变动，生成可执行的剩余（修订）计划。

    window_strategy 非空时（strict_due/fastest_feasible/lowest_fee），
    在支出允许支付窗口内搜索可执行安排，并对缺口给出
    truly_unmet / strategy_not_used / no_solution_found 分类；
    此时旧的 P3 无条件推迟逻辑停用，全部改期受支付窗口约束。
    """
    amendments = amendments or []
    inflow_updates = inflow_updates or {}
    outflow_shifts = outflow_shifts or {}
    chain_quote = chain_quote or {}

    prefix = build_prefix(scenario, execution, cutoff_date)
    residual, aux = _residual_scenario(
        scenario, prefix, execution, cutoff_date, amendments,
        inflow_updates, outflow_shifts,
        keep_windows=window_strategy is not None)
    fixed = _fixed_transit_orders(prefix)

    residual_scenario = residual
    deferred: dict[str, str] = {}
    window_decisions: list[dict] = []
    window_schedule: dict[str, str] = {}
    if window_strategy is not None:
        from .scheduling import run_strategy
        outcome = run_strategy(residual, window_strategy,
                               fixed_orders=fixed, id_prefix="R")
        orders, result = outcome.plan.orders, outcome.replay
        window_decisions = outcome.decisions
        window_schedule = outcome.schedule
    else:
        first = solve(residual, fixed_orders=fixed, id_prefix="R")
        orders, result = first.plan.orders, first.replay
    if window_strategy is None and allow_defer_p3:
        orders, result, shifts, reasons = _try_defer_p3(residual, fixed, first)
        if shifts:
            residual_scenario, _ = _residual_scenario(
                scenario, prefix, execution, cutoff_date, amendments,
                inflow_updates, {**outflow_shifts, **shifts})
            deferred = reasons
    preloaded_ids = {o.order_id for o in fixed}
    if window_strategy is not None:
        from .scheduling import shifted_scenario
        residual_scenario = shifted_scenario(residual, window_schedule, "@final")
    final = replay(residual_scenario, fixed + orders, snapshot_daily=True,
                   preloaded_ids=preloaded_ids)
    if window_strategy is not None:
        decisions = window_decisions
    else:
        decisions = _decisions_for(residual_scenario, final,
                                   aux["carry"], chain_quote, deferred)

    by_pri = final.gap_total_by_priority()
    total_fee = money(sum((o.fee for o in orders), Decimal("0")))
    expected = [
        {"flow_id": f.flow_id, "account_id": f.account_id,
         "expected_date": f.date, "amount": str(f.amount), "memo": f.memo}
        for f in residual_scenario.flows if f.kind == FlowKind.INFLOW
    ]
    basis = {}
    if record_basis:
        basis = {
            "scenario": scenario.to_dict(),
            "cutoff_date": cutoff_date,
            "revision_of": original_plan.created_at,
            "inflow_updates": {k: [v[0], str(v[1])]
                               for k, v in inflow_updates.items()},
            "outflow_shifts": dict(outflow_shifts),
            "amendments": [a.to_dict() for a in amendments],
            "window_strategy": window_strategy or "",
        }
    revised = Plan(
        scenario_id=scenario.scenario_id,
        created_at=datetime.now().isoformat(timespec="seconds"),
        orders=sorted(orders, key=lambda o: (o.initiate_date, o.order_id)),
        remaining_gaps=[
            {"flow_id": g.flow_id, "account_id": g.account_id, "date": g.date,
             "priority": g.priority, "due": str(g.due), "paid": str(g.paid),
             "shortfall": str(money(g.shortfall))}
            for g in sorted(final.gaps, key=lambda g: (g.date, g.priority))],
        total_fee=total_fee,
        objective=(int(by_pri[int(Priority.P1_CRITICAL)] * 100),
                   int(by_pri[int(Priority.P2_NORMAL)] * 100),
                   int(by_pri[int(Priority.P3_OPTIONAL)] * 100),
                   int(total_fee * 100)),
        solver=(f"window-{window_strategy}" if window_strategy is not None
                else "greedy-replan"),
        notes=("延误后修订：已发起资金保持在途不再重扣；按当前在途/收入/费率"
               + ("；在允许支付窗口内搜索排期" if window_strategy else "")),
        revision_of=original_plan.created_at,
        revised_at=datetime.now().isoformat(timespec="seconds"),
        cutoff_date=cutoff_date,
        fixed_transit=sorted(fixed, key=lambda o: o.arrival_date),
        payment_decisions=decisions,
        rule_amendments=[a.to_dict() for a in amendments],
        expected_inflows=expected,
        strategy=window_strategy or "",
        payment_schedule=window_schedule,
        basis=basis,
    )
    return ReplanResult(plan=revised, residual_replay=final, prefix_replay=prefix)


def combined_replay(scenario: Scenario, execution: Execution, revised: Plan,
                    inflow_updates: dict[str, tuple[str, Decimal]] | None = None
                    ) -> ReplayResult:
    """重开后从期初一路对清：实际执行前缀 + 修订计划，单本账资金守恒。

    - 往期收入按实际到账（部分/延误/未到）；未来收入按修订时刻确认值；
    - 实际调拨（含延误）用实际日期/费用；修订新增调拨用新计划；
    - 已在途本金不会被扣第二次：FIXED 单只在到账日入账。
    """
    from .models import CashFlow
    inflow_updates = inflow_updates or {}
    actuals = _actual_map(execution)
    cutoff = revised.cutoff_date
    first_day = add_days(cutoff, 1)

    flows = []
    for f in scenario.flows:
        if f.kind == FlowKind.INFLOW:
            act = actuals.get(f.flow_id)
            if f.date <= cutoff:
                if act is None or act.amount == 0:
                    continue
                flows.append(CashFlow(f.flow_id, f.account_id, act.actual_date,
                                      f.kind, act.amount, f.priority, f.memo))
            elif f.flow_id in inflow_updates:
                nd, amt = inflow_updates[f.flow_id]
                if amt > 0:
                    flows.append(CashFlow(f.flow_id, f.account_id, nd,
                                          f.kind, amt, f.priority, f.memo))
            else:
                flows.append(f)
        else:
            flows.append(f)
    full = Scenario(
        scenario.scenario_id + "@combined", scenario.start_date,
        scenario.end_date, list(scenario.accounts), flows,
        list(scenario.transfer_rules), scenario.description)

    legs: list[TransferLeg] = []
    for t in execution.actual_transfers:
        if t.status == "cancelled":
            continue
        legs.append(TransferLeg(
            t.order_id, t.source, t.target,
            t.actual_initiate_date, t.actual_arrival_date,
            money(t.actual_principal), money(t.actual_fee)))
    fixed_ids = {f"FIXED-{o.order_id.split('-', 1)[1]}"
                 for o in revised.fixed_transit}
    # 修订新增订单（在 cutoff 之后发起/到账）
    for o in revised.orders:
        legs.append(TransferLeg(
            o.order_id, o.source, o.target, o.initiate_date, o.arrival_date,
            o.principal, o.fee))
    return replay(full, legs=legs, snapshot_daily=True,
                  preloaded_ids=fixed_ids)
