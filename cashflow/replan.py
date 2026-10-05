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
from .models import TransferRule
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
            rules.append(TransferRule(
                r.source, r.target, r.lag_days,
                r.fee_rate if a.fee_rate is None else a.fee_rate,
                r.fee_fixed if a.fee_fixed is None else a.fee_fixed,
                r.min_amount, r.max_amount))
    scenario.transfer_rules = rules
    return scenario


def _actual_map(execution: Execution) -> dict[str, ActualInflow]:
    return {f.flow_id: f for f in execution.actual_inflows}


def _refund_flows(execution: Execution, lo: str, hi: str,
                  assumptions: dict[str, str | dict] | None = None,
                  ) -> tuple[list, list[dict]]:
    """把 [lo, hi] 区间内实际到达（或显式假设到达）的退回转成收入流水。

    预计退回（actual_date 为 None）默认不产生流水——不能提前当作可用资金；
    仅当 refund_assumptions 显式给出假设到账日时才入账（用于对照方案）。
    返回 (流水列表, 入账依据列表)。
    """
    from .models import CashFlow
    assumptions = assumptions or {}
    flows, basis = [], []
    for r in getattr(execution, "refunds", []):
        assumed = False
        day = r.actual_date
        amount = r.amount
        return_account = r.return_account
        fee_refunded = r.fee_refunded
        override = assumptions.get(r.refund_id)
        if day is None:
            if isinstance(override, dict):
                day = override.get("date")
                amount = money(override.get("amount", r.amount))
                return_account = override.get("return_account",
                                              r.return_account)
                fee_refunded = money(override.get("fee_refunded",
                                                  r.fee_refunded))
            else:
                day = override
            assumed = day is not None
        if day is None or not (lo <= day <= hi):
            continue
        label = "拒付" if r.kind == "chargeback" else "退回"
        flows.append(CashFlow(
            f"REFUND-{r.refund_id}", return_account, day,
            FlowKind.INFLOW, amount,
            memo=f"{label} {r.original_flow_id}（原付款账户 {r.payer_account}）"))
        if fee_refunded > 0:
            flows.append(CashFlow(
                f"REFUNDFEE-{r.refund_id}", return_account, day,
                FlowKind.INFLOW, fee_refunded,
                memo=f"{label} {r.original_flow_id} 的手续费退回"))
        basis.append({"refund_id": r.refund_id, "original_flow_id": r.original_flow_id,
                      "return_account": return_account, "date": day,
                      "amount": str(amount),
                      "fee_refunded": str(fee_refunded),
                      "status": "forecast" if assumed else "arrived"})
    return flows, basis


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
                f.priority, f.memo, f.window_start, f.window_end))
        else:
            prefix_flows.append(f)
    # cutoff 前已实际到达的退回/拒付：按实际到账日入退回账户（可跨账户）
    refund_flows, _ = _refund_flows(execution, scenario.start_date, cutoff_date)
    prefix_flows.extend(refund_flows)
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
                       refund_assumptions: dict[str, str] | None = None
                       ) -> tuple[Scenario, dict]:
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
                carry_info[f.flow_id] = {"original_date": f.date,
                                         "residual_date": new_date}
                residual_flows.append(CashFlow(
                    f.flow_id, f.account_id, new_date, f.kind, money(unpaid),
                    f.priority,
                    f"{f.memo}（{f.date} 到期，延误后补付）",
                    new_date, new_date))
            else:
                new_date = outflow_shifts.get(f.flow_id, f.date)
                residual_flows.append(CashFlow(
                    f.flow_id, f.account_id, new_date, f.kind, f.amount,
                    f.priority, f.memo, f.window_start, f.window_end))
        else:  # 收入
            act = actuals.get(f.flow_id)
            if f.date <= cutoff_date:
                # 往期延迟到账的收入，放入残差实际到账日
                if act is not None and act.actual_date >= first_day and act.amount > 0:
                    residual_flows.append(CashFlow(
                        f.flow_id, f.account_id, act.actual_date, f.kind,
                        act.amount, f.priority, f.memo,
                        f.window_start, f.window_end))
                continue
            new_date, new_amount = f.date, f.amount
            if f.flow_id in inflow_updates:
                new_date, new_amount = inflow_updates[f.flow_id]
            if new_amount > 0:
                residual_flows.append(CashFlow(
                    f.flow_id, f.account_id, new_date, f.kind, money(new_amount),
                    f.priority, f.memo, f.window_start, f.window_end))

    # cutoff 后实际到达的退回直接入账；预计退回仅在显式假设下入账
    refund_flows, refund_basis = _refund_flows(
        execution, first_day, scenario.end_date, refund_assumptions)
    residual_flows.extend(refund_flows)

    residual = Scenario(
        scenario.scenario_id + "@residual", first_day, scenario.end_date,
        [Account(a.account_id, a.name, led.available(a.account_id))
         for a in scenario.accounts],
        residual_flows, list(scenario.transfer_rules), scenario.description)
    _amend_rules(residual, amendments, first_day)
    residual.validate()
    return residual, {"carry": carry_info, "paid": paid_map, "due": due_map,
                      "refund_basis": refund_basis}


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
                   deferred: dict[str, str],
                   feasibility_status: str = "",
                   flow_status: dict[str, str] | None = None,
                   original_due_dates: dict[str, str] | None = None) -> list[dict]:
    """逐笔支付给出决策：met（准时/补付）、deferred、unmet，并写明理由。"""
    paid = {p.flow_id: p for p in result.ledger.payments}
    decisions = []
    for f in sorted(
            [x for x in scenario.flows if x.kind == FlowKind.OUTFLOW],
            key=lambda x: (x.date, int(x.priority))):
        p = paid.get(f.flow_id)
        info = carry.get(f.flow_id)
        original_due = (original_due_dates or {}).get(f.flow_id, f.date)
        base = {"flow_id": f.flow_id, "account_id": f.account_id,
                "priority": int(f.priority), "due": str(f.amount),
                "scheduled_date": f.date,
                "original_due_date": (info or {}).get("original_date",
                                                       original_due),
                "memo": f.memo}
        if p is None or p.shortfall > 0:
            shortfall = f.amount if p is None else p.shortfall
            flow_feasibility = (flow_status or {}).get(
                f.flow_id, feasibility_status)
            if not flow_feasibility:
                status = "unmet"
                reason = ("当前求解策略未能覆盖该支出；未提供可搜索的支付窗口，"
                          "保持缺口，不重复占用已发起资金")
            elif flow_feasibility == "strategy_not_adopted":
                status = "strategy_not_adopted"
                reason = ("当前支付优先级策略未采用窗口内可足额安排；"
                          "本策略下保留部分支付/缺口，不代表资金真正不足")
            elif flow_feasibility == "search_limit_exceeded" or \
                    flow_feasibility == "search_incomplete":
                status = "search_incomplete"
                reason = ("已达到支付日组合枚举上限，尚未找到可足额安排；"
                          "不能据此断言整个允许窗口内真正不足")
            else:
                status = "true_shortfall"
                reason = ("已完整枚举允许支付窗口：余额、在途资金与后续收入"
                          "均无法覆盖，确认为真正不足")
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
                  plan_result,
                  locked: list[TransferOrder] | None = None
                  ) -> tuple[list[TransferOrder], ReplayResult, dict, dict]:
    """对仍有缺口的 P3 可选支付，尝试推迟到后续有资金的日期；取目标元组最优。"""
    locked = locked or []
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
            trial = solve(shifted, fixed_orders=fixed,
                          locked_orders=locked, id_prefix="R")
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


def _with_outflow_dates(scenario: Scenario, dates: dict[str, str]) -> Scenario:
    from .models import CashFlow
    if not dates:
        return scenario
    return Scenario(
        scenario.scenario_id, scenario.start_date, scenario.end_date,
        list(scenario.accounts),
        [CashFlow(f.flow_id, f.account_id, dates.get(f.flow_id, f.date),
                  f.kind, f.amount, f.priority, f.memo,
                  f.window_start, f.window_end)
         if f.kind == FlowKind.OUTFLOW else f for f in scenario.flows],
        list(scenario.transfer_rules), scenario.description)


def _adjust_residual_payments(
        scenario: Scenario,
        payment_priorities: dict[str, int] | None = None,
        cancelled_payment_ids: set[str] | None = None,
        extend_payment_windows: bool = False) -> Scenario:
    from .models import CashFlow
    payment_priorities = payment_priorities or {}
    cancelled_payment_ids = cancelled_payment_ids or set()
    flows = []
    for flow in scenario.flows:
        if flow.kind == FlowKind.OUTFLOW and flow.flow_id in cancelled_payment_ids:
            continue
        if flow.kind != FlowKind.OUTFLOW:
            flows.append(flow)
            continue
        priority = int(payment_priorities.get(flow.flow_id, flow.priority))
        window_start = flow.window_start
        window_end = flow.window_end
        if extend_payment_windows:
            window_start = max(flow.payment_start, flow.date)
            window_end = scenario.end_date
        flows.append(CashFlow(
            flow.flow_id, flow.account_id, flow.date, flow.kind,
            flow.amount, priority, flow.memo, window_start, window_end))
    return Scenario(
        scenario.scenario_id, scenario.start_date, scenario.end_date,
        list(scenario.accounts), flows, list(scenario.transfer_rules),
        scenario.description)


def replan(scenario: Scenario, original_plan: Plan, execution: Execution,
           cutoff_date: str,
           amendments: list[FeeAmendment] | None = None,
           inflow_updates: dict[str, tuple[str, Decimal]] | None = None,
           outflow_shifts: dict[str, str] | None = None,
           allow_defer_p3: bool = True,
           chain_quote: dict | None = None,
           locked_orders: list[TransferOrder] | None = None,
           confirmed_arrangements: list[dict] | None = None,
           partial_adoption_options: list[dict] | None = None,
           withdrawn_order_ids: list[str] | None = None,
           committed_fee: Decimal | None = None,
           refund_assumptions: dict[str, str | dict] | None = None,
           payment_priorities: dict[str, int] | None = None,
           cancelled_payment_ids: list[str] | None = None,
           extend_payment_windows: bool = False,
           basis_metadata: dict | None = None) -> ReplanResult:
    """根据实际执行与参数变动，生成可执行的剩余（修订）计划。"""
    amendments = amendments or []
    inflow_updates = inflow_updates or {}
    outflow_shifts = outflow_shifts or {}
    chain_quote = chain_quote or {}
    withdrawn_order_ids = list(
        withdrawn_order_ids if withdrawn_order_ids is not None
        else original_plan.withdrawn_order_ids)
    refund_assumptions = dict(refund_assumptions or {})

    prefix = build_prefix(scenario, execution, cutoff_date)
    residual, aux = _residual_scenario(
        scenario, prefix, execution, cutoff_date, amendments,
        inflow_updates, outflow_shifts, refund_assumptions)
    residual = _adjust_residual_payments(
        residual, payment_priorities, set(cancelled_payment_ids or []),
        extend_payment_windows)
    fixed = _fixed_transit_orders(prefix)
    executed_ids = set(execution.initiated_order_ids) | {
        t.order_id for t in execution.actual_transfers
        if t.status != "cancelled"}
    inherited_locked = [
        o for o in original_plan.locked_orders
        if o.initiate_date > cutoff_date
        and o.order_id not in withdrawn_order_ids
        and o.order_id not in executed_ids]
    locked_orders = list(locked_orders if locked_orders is not None
                         else inherited_locked)

    eligible_strategies = None if allow_defer_p3 else ["strict_due"]
    first = solve(residual, fixed_orders=fixed, id_prefix="R",
                  locked_orders=locked_orders,
                  eligible_strategies=eligible_strategies)
    selected_dates = first.plan.schedule_basis.get("scheduled_date", {})
    residual_scenario = _with_outflow_dates(residual, selected_dates)
    orders, result = first.plan.orders, first.replay
    deferred: dict[str, str] = {}
    has_windows = any(f.window_start is not None or f.window_end is not None
                      for f in residual.flows if f.kind == FlowKind.OUTFLOW)
    if allow_defer_p3 and not has_windows:
        orders, result, shifts, reasons = _try_defer_p3(
            residual, fixed, first, locked_orders)
        if shifts:
            residual_scenario = _with_outflow_dates(residual, shifts)
            deferred = reasons
    elif selected_dates:
        for fid, day in selected_dates.items():
            original = first.plan.schedule_basis["original_due_date"][fid]
            if day != original:
                deferred[fid] = (f"在允许支付窗口内从原期限 {original} 改期至 {day}，"
                                 "用后续到账资金足额支付")
    preloaded_ids = {o.order_id for o in fixed}
    final = replay(residual_scenario, fixed + locked_orders + orders,
                   snapshot_daily=True, preloaded_ids=preloaded_ids)

    decisions = _decisions_for(residual_scenario, final,
                               aux["carry"], chain_quote, deferred,
                               first.plan.feasibility_status,
                               first.plan.schedule_basis.get("flow_feasibility"),
                               first.plan.schedule_basis.get(
                                   "original_due_date", {}))

    by_pri = final.gap_total_by_priority()
    new_fee = money(sum((o.fee for o in orders), Decimal("0")))
    committed_fee = money(committed_fee if committed_fee is not None
                          else prefix.ledger.total_fees +
                          sum((o.fee for o in locked_orders), Decimal("0")))
    total_fee = money(new_fee + committed_fee)
    expected = [
        {"flow_id": f.flow_id, "account_id": f.account_id,
         "expected_date": f.date, "amount": str(f.amount), "memo": f.memo}
        for f in residual_scenario.flows if f.kind == FlowKind.INFLOW
        and not f.flow_id.startswith(("REFUND-", "REFUNDFEE-"))
    ]
    # 退回记录与入账依据：重开后继续部分采纳时仍能对上原支付的最终去向
    _, prefix_refund_basis = _refund_flows(
        execution, scenario.start_date, cutoff_date)
    refund_basis = prefix_refund_basis + aux["refund_basis"]
    refund_records = []
    expected_refunds = []
    basis_by_id = {b["refund_id"]: b for b in refund_basis}
    for r in getattr(execution, "refunds", []):
        b = basis_by_id.get(r.refund_id)
        entry = {
            "refund_id": r.refund_id, "kind": r.kind,
            "original_flow_id": r.original_flow_id,
            "payer_account": r.payer_account,
            "return_account": r.return_account,
            "expected_date": r.expected_date,
            "actual_date": r.actual_date,
            "amount": str(r.amount), "fee_refunded": str(r.fee_refunded),
            "status": (b["status"] if b else
                       ("arrived" if r.arrived else "expected_not_usable")),
            "usable_as_funds": b is not None,
        }
        refund_records.append(entry)
        if not r.arrived:
            expected_refunds.append(entry)
    schedule_basis = {
        **first.plan.schedule_basis,
        "refund_basis": refund_basis,
        "refund_assumptions": refund_assumptions,
    }
    if cancelled_payment_ids:
        schedule_basis["cancelled_unissued_payment_ids"] = list(
            cancelled_payment_ids)
    if payment_priorities:
        schedule_basis["payment_priority_overrides"] = {
            key: int(value) for key, value in payment_priorities.items()}
    if basis_metadata:
        schedule_basis.update(basis_metadata)
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
        solver="greedy-replan",
        notes=("延误后修订：已发起资金保持在途不再重扣；按当前在途/收入/费率"),
        revision_of=original_plan.created_at,
        revised_at=datetime.now().isoformat(timespec="seconds"),
        cutoff_date=cutoff_date,
        fixed_transit=sorted(fixed, key=lambda o: o.arrival_date),
        payment_decisions=decisions,
        rule_amendments=[a.to_dict() for a in amendments],
        expected_inflows=expected,
        selected_strategy=first.plan.selected_strategy,
        payment_strategy_options=first.plan.payment_strategy_options,
        feasibility_status=first.plan.feasibility_status,
        schedule_basis=schedule_basis,
        locked_orders=sorted(locked_orders,
                             key=lambda o: (o.initiate_date, o.order_id)),
        confirmed_arrangements=confirmed_arrangements or
        original_plan.confirmed_arrangements,
        partial_adoption_options=partial_adoption_options or
        original_plan.partial_adoption_options,
        withdrawn_order_ids=withdrawn_order_ids,
        committed_fee=committed_fee,
        refund_records=refund_records,
        expected_refunds=expected_refunds,
    )
    return ReplanResult(plan=revised, residual_replay=final, prefix_replay=prefix)


def combined_replay(scenario: Scenario, execution: Execution, revised: Plan,
                    inflow_updates: dict[str, tuple[str, Decimal]] | None = None,
                    refund_assumptions: dict[str, str | dict] | None = None
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
    scheduled_dates = revised.schedule_basis.get("scheduled_date", {})
    revised_inflows = {f["flow_id"]: (f["expected_date"], Decimal(f["amount"]))
                       for f in revised.expected_inflows}

    flows = []
    for f in scenario.flows:
        if f.kind == FlowKind.INFLOW:
            act = actuals.get(f.flow_id)
            if f.date <= cutoff:
                if act is None or act.amount == 0:
                    continue
                flows.append(CashFlow(f.flow_id, f.account_id, act.actual_date,
                                      f.kind, act.amount, f.priority, f.memo,
                                      f.window_start, f.window_end))
            elif f.flow_id in revised_inflows:
                nd, amt = revised_inflows[f.flow_id]
                if amt > 0:
                    flows.append(CashFlow(f.flow_id, f.account_id, nd,
                                          f.kind, amt, f.priority, f.memo,
                                          f.window_start, f.window_end))
            elif f.flow_id in inflow_updates:
                nd, amt = inflow_updates[f.flow_id]
                if amt > 0:
                    flows.append(CashFlow(f.flow_id, f.account_id, nd,
                                          f.kind, amt, f.priority, f.memo,
                                          f.window_start, f.window_end))
            else:
                flows.append(f)
        else:
            nd = scheduled_dates.get(f.flow_id, f.date)
            flows.append(CashFlow(f.flow_id, f.account_id, nd, f.kind, f.amount,
                                  f.priority, f.memo, f.window_start,
                                  f.window_end))
    # 实际到达的退回按实际到账日入账；显式假设的预计退回仅在对照时入账
    refund_flows, _ = _refund_flows(execution, scenario.start_date,
                                    scenario.end_date, refund_assumptions)
    flows.extend(refund_flows)
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
    for o in [*revised.locked_orders, *revised.orders]:
        legs.append(TransferLeg(
            o.order_id, o.source, o.target, o.initiate_date, o.arrival_date,
            o.principal, o.fee))
    return replay(full, legs=legs, snapshot_daily=True,
                  preloaded_ids=fixed_ids)
