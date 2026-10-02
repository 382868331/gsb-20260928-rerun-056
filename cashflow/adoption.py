"""候选支付安排的部分采纳、对照与撤回。"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from decimal import Decimal

from .engine import SolveResult, replay, solve
from .models import CashFlow, FlowKind, money
from .plan import Execution, Plan, TransferOrder
from .scenario import Scenario


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def scheduled_scenario(scenario: Scenario, dates: dict[str, str]) -> Scenario:
    flows = []
    for flow in scenario.flows:
        day = dates.get(flow.flow_id)
        if flow.kind == FlowKind.OUTFLOW and day is not None:
            flows.append(CashFlow(flow.flow_id, flow.account_id, day,
                                  flow.kind, flow.amount, flow.priority,
                                  flow.memo, flow.window_start,
                                  flow.window_end))
        else:
            flows.append(flow)
    return Scenario(scenario.scenario_id, scenario.start_date,
                    scenario.end_date, list(scenario.accounts), flows,
                    list(scenario.transfer_rules), scenario.description)


def candidate_option(plan: Plan, strategy: str | None = None,
                     rank: int | None = None) -> dict | None:
    for option in plan.payment_strategy_options:
        if strategy is not None and option.get("strategy") != strategy:
            continue
        if rank is not None and option.get("rank") != rank:
            continue
        return option
    return None


def option_dates(option: dict) -> dict[str, str]:
    return dict(option.get("rescheduled_date", {}))


def solve_option(scenario: Scenario, plan: Plan, option: dict,
                 fixed_orders: list[TransferOrder] | None = None,
                 locked_orders: list[TransferOrder] | None = None,
                 id_prefix: str = "A") -> SolveResult:
    candidate = scheduled_scenario(scenario, option_dates(option))
    return solve(candidate, fixed_orders=fixed_orders or [],
                 locked_orders=locked_orders or [], id_prefix=id_prefix)


def _order_map(plan: Plan) -> dict[str, TransferOrder]:
    return {o.order_id: o for o in plan.orders}


def _infer_flow_ids(plan: Plan, selected_order_ids: list[str],
                    selected_flow_ids: list[str] | None) -> list[str]:
    if selected_flow_ids is not None:
        return list(selected_flow_ids)
    changed = list(plan.schedule_basis.get("scheduled_date", {}))
    return changed if len(changed) == 1 and not selected_order_ids else []


def adopt_partial_arrangements(scenario: Scenario, plan: Plan,
                               selected_order_ids: list[str],
                               selected_flow_ids: list[str] | None = None,
                               option: dict | None = None,
                               name: str = "partial_adoption",
                               fixed_orders: list[TransferOrder] | None = None,
                               id_prefix: str = "A") -> Plan:
    available = _order_map(plan)
    missing = [oid for oid in selected_order_ids if oid not in available]
    if missing:
        raise ValueError(f"候选方案不存在这些订单: {missing}")
    option = option or (candidate_option(plan, plan.selected_strategy)
                        if plan.payment_strategy_options else
                        {"rescheduled_date": plan.schedule_basis.get(
                            "scheduled_date", {})})
    locked = [available[oid] for oid in selected_order_ids]
    flow_ids = _infer_flow_ids(plan, selected_order_ids, selected_flow_ids)
    result = solve_option(
        scenario, plan, option or {},
        fixed_orders=fixed_orders or plan.fixed_transit,
        locked_orders=plan.locked_orders + locked,
        id_prefix=id_prefix)
    committed_new = sum((o.fee for o in locked), Decimal("0"))
    confirmed = list(plan.confirmed_arrangements)
    for order in locked:
        confirmed.append({
            "order_id": order.order_id, "source": order.source,
            "target": order.target,
            "initiate_date": order.initiate_date,
            "arrival_date": order.arrival_date,
            "principal": str(order.principal), "fee": str(order.fee),
            "flow_ids": flow_ids,
            "status": "confirmed_uninitiated",
            "withdrawable": True,
        })
    if not locked and flow_ids:
        confirmed.append({
            "order_id": None, "flow_ids": flow_ids,
            "status": "confirmed_schedule", "withdrawable": True,
            "scheduled_date": {fid: option_dates(option or {}).get(fid)
                                for fid in flow_ids},
        })
    schedule_basis = {
        **plan.schedule_basis, "partial_adoption": name,
        "adopted_at": _now(),
        "selected_order_ids": list(selected_order_ids),
        "selected_flow_ids": flow_ids,
    }
    committed_fee = money(plan.committed_fee + committed_new)
    adopted = replace(
        plan,
        orders=result.plan.orders,
        remaining_gaps=result.plan.remaining_gaps,
        objective=result.plan.objective,
        total_fee=money(result.plan.total_fee + committed_fee),
        locked_orders=list(plan.locked_orders) + locked,
        confirmed_arrangements=confirmed,
        partial_adoption_options=list(plan.partial_adoption_options),
        withdrawn_order_ids=list(plan.withdrawn_order_ids),
        schedule_basis=schedule_basis,
        committed_fee=committed_fee,
        notes=(f"部分采纳 {name}：确认 {len(locked)} 笔调拨；"
               "未发起确认调拨照常占用余额，其余支付已重新规划"))
    return adopted


def compare_partial_adoptions(scenario: Scenario, plan: Plan,
                              selections: list[dict],
                              fixed_orders: list[TransferOrder] | None = None
                              ) -> list[dict]:
    due_dates = {f.flow_id: f.date for f in scenario.flows
                 if f.kind == FlowKind.OUTFLOW}
    comparisons = []
    for index, selection in enumerate(selections):
        option = selection["option"]
        dates = option_dates(option)
        order_ids = list(selection.get("order_ids", []))
        locked = [_order_map(plan)[oid] for oid in order_ids]
        result = solve_option(
            scenario, plan, option,
            fixed_orders=fixed_orders or plan.fixed_transit,
            locked_orders=plan.locked_orders + locked,
            id_prefix=f"C{index + 1}")
        selected_flows = list(selection.get("flow_ids", []))
        delayed = {fid: dates.get(fid, due_dates[fid])
                   for fid in due_dates
                   if fid not in selected_flows and
                   dates.get(fid, due_dates[fid]) > due_dates[fid]}
        gaps = {g["flow_id"]: g["shortfall"]
                for g in result.plan.remaining_gaps}
        confirmed_fee = money(sum((o.fee for o in locked), Decimal("0")))
        total_expected_fee = money(result.plan.total_fee + confirmed_fee)
        comparisons.append({
            "name": selection.get("name", f"partial_{index + 1}"),
            "source_strategy": option.get("strategy"),
            "source_rank": option.get("rank"),
            "constraints_preserved": {
                "payment_window": True, "transfer_rules": True,
                "fee_rules": True,
                "fixed_transit_order_ids": [
                    o.order_id for o in (fixed_orders or plan.fixed_transit)]},
            "confirmed_scope": {
                "order_ids": order_ids, "flow_ids": selected_flows,
                "principal": str(money(sum((o.principal for o in locked),
                                            Decimal("0")))),
                "fee": str(money(sum((o.fee for o in locked),
                                      Decimal("0"))))},
            "remaining_orders": [o.to_dict() for o in result.plan.orders],
            "remaining_fee": str(result.plan.total_fee),
            "total_expected_fee": str(total_expected_fee),
            "remaining_gap": str(money(sum((Decimal(v) for v in gaps.values()),
                                          Decimal("0")))),
            "gap_by_flow": gaps,
            "delayed_remaining_payments": delayed,
            "increases_fee": False,
            "loses_feasible_arrangement": bool(result.plan.remaining_gaps),
        })
    min_fee = min((Decimal(o["total_expected_fee"]) for o in comparisons),
                  default=Decimal("0"))
    for comparison in comparisons:
        comparison["fee_premium"] = str(money(
            Decimal(comparison["total_expected_fee"]) - min_fee))
        comparison["increases_fee"] = Decimal(comparison["fee_premium"]) > 0
    return comparisons


def withdraw_arrangements(plan: Plan, order_ids: list[str],
                          execution: Execution | None = None,
                          cutoff_date: str = "") -> Plan:
    plan = replace(
        plan,
        locked_orders=list(plan.locked_orders),
        confirmed_arrangements=[dict(item)
                                for item in plan.confirmed_arrangements],
        withdrawn_order_ids=list(plan.withdrawn_order_ids),
        schedule_basis=dict(plan.schedule_basis))
    initiated = set()
    if execution is not None:
        initiated.update(execution.initiated_order_ids)
        initiated.update(t.order_id for t in execution.actual_transfers
                         if t.status != "cancelled" and
                         (not cutoff_date or
                          t.actual_initiate_date <= cutoff_date))
    locked_map = {o.order_id: o for o in plan.locked_orders}
    transit_map = {o.order_id: o for o in plan.fixed_transit}
    cancel_ids, retained_ids, notes = set(), set(), []
    for oid in dict.fromkeys(order_ids):
        if oid in transit_map:
            retained_ids.add(oid)
            notes.append({"order_id": oid, "status": "fixed_in_transit",
                          "action": "retained",
                          "handling": "调拨已发出且处于在途，撤回不生效；仅后续未执行部分可作废，在途本金按原到账日入账，到账后才可重新使用，已发生费用保留"})
            continue
        if oid not in locked_map:
            raise ValueError(f"订单未处于可撤回的已确认未发起状态: {oid}")
        if oid in initiated:
            retained_ids.add(oid)
            notes.append({"order_id": oid, "status": "in_transit_or_executed",
                          "action": "retained",
                          "handling": "调拨已发出，只撤回尚未执行部分；在途本金按原到账日入账，到账后才可重新使用，已扣手续费保留"})
        else:
            cancel_ids.add(oid)
            notes.append({"order_id": oid, "status": "withdrawn",
                          "action": "release_future_capacity",
                          "handling": "尚未发起，释放未来发起日占用的本金和调拨能力"})
    plan.locked_orders = [o for o in plan.locked_orders
                          if o.order_id not in cancel_ids]
    plan.withdrawn_order_ids = list(dict.fromkeys(
        list(plan.withdrawn_order_ids) + list(cancel_ids)))
    for item in plan.confirmed_arrangements:
        oid = item.get("order_id")
        if oid in cancel_ids:
            item.update(status="withdrawn", withdrawable=False)
        elif oid in retained_ids:
            item.update(status="in_transit_or_executed", withdrawable=False)
    plan.schedule_basis = {**plan.schedule_basis, "withdrawal_notes": notes}
    plan.notes = (f"撤回 {len(cancel_ids)} 笔未发起安排；"
                  f"{len(retained_ids)} 笔已发起保持在途")
    return plan


def replay_with_adoptions(scenario: Scenario, plan: Plan,
                          scheduled_dates: dict[str, str] | None = None):
    dates = scheduled_dates or plan.schedule_basis.get("scheduled_date", {})
    candidate = scheduled_scenario(scenario, dates)
    preloaded = {o.order_id for o in plan.fixed_transit}
    return replay(candidate, plan.fixed_transit + plan.locked_orders +
                  plan.orders, snapshot_daily=True,
                  preloaded_ids=preloaded)
