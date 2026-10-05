"""依赖退回资金的后续付款方案研究。

研究中的预计退回、金额和费用假设只在对应研究方案内生效；它们不会写回
Execution，也不会改变其他方案的期初余额。实际退回到账后，再以实际记录
生成修订方案，并用 refund_revision 保留原研究/原安排用于核对。
"""

from __future__ import annotations

import copy
from dataclasses import replace
from decimal import Decimal

from .models import FlowKind, Priority, money
from .plan import Execution, Plan, TransferOrder
from .replan import FeeAmendment, replan
from .scenario import Scenario


def _forecast_execution(execution: Execution,
                        forecasts: dict[str, dict]) -> Execution:
    """复制执行记录并替换预计退回；已实际到账的事实不会被预测覆盖。"""
    cloned = copy.copy(execution)
    cloned.refunds = []
    for record in execution.refunds:
        if record.arrived or record.refund_id not in forecasts:
            cloned.refunds.append(record)
            continue
        assumption = forecasts[record.refund_id]
        cloned.refunds.append(replace(
            record,
            expected_date=assumption.get("date", record.expected_date),
            amount=money(assumption.get("amount", record.amount)),
            return_account=assumption.get("return_account",
                                           record.return_account),
            fee_refunded=money(assumption.get("fee_refunded",
                                              record.fee_refunded)),
        ))
    return cloned


def _actions(plan: Plan, cancelled: list[str]) -> dict[str, dict]:
    actions: dict[str, dict] = {}
    for decision in plan.payment_decisions:
        flow_id = decision["flow_id"]
        due = decision.get("original_due_date",
                          decision["scheduled_date"])
        scheduled = decision["scheduled_date"]
        shortfall = money(decision.get("shortfall", "0"))
        executable = shortfall == 0
        if executable and scheduled <= due:
            status = "executable_on_time"
        elif executable:
            status = "executable_late"
        else:
            status = "waiting_or_unmet_in_forecast"
        actions[flow_id] = {
            "status": status,
            "scheduled_date": scheduled if executable else None,
            "earliest_executable_date": scheduled if executable else None,
            "shortfall": str(shortfall),
            "account_id": decision["account_id"],
            "priority": decision["priority"],
            "reason": decision.get("reason", ""),
        }
    for flow_id in cancelled:
        actions[flow_id] = {
            "status": "cancelled_unissued",
            "scheduled_date": None,
            "earliest_executable_date": None,
            "shortfall": None,
            "reason": "尚未发出的付款安排已取消；已发出/已到账事实不撤销",
        }
    return dict(sorted(actions.items()))


def research_refund_payment_options(
        scenario: Scenario,
        original_plan: Plan,
        execution: Execution,
        cutoff_date: str,
        scenarios: list[dict],
        payment_priorities: dict[str, int] | None = None,
        cancelled_payment_ids: list[str] | None = None,
        amendments: list[FeeAmendment] | None = None) -> list[dict]:
    """比较多组预计退回日期/金额/费用/账户假设下的付款可行性。

    scenarios: [{"name": str, "refunds": {refund_id: {date, amount,
      return_account, fee_refunded}}, "amendments": [...可选]}]

    空 refunds 是保守基准：只用实际到账退回，预计退回不形成余额。
    """
    cancelled_payment_ids = list(cancelled_payment_ids or [])
    options = []

    for index, item in enumerate(scenarios):
        forecasts = item.get("refunds", {})
        scenario_execution = _forecast_execution(execution, forecasts)
        scenario_amendments = item.get("amendments", amendments)
        result = replan(
            scenario,
            original_plan,
            scenario_execution,
            cutoff_date,
            amendments=scenario_amendments or [],
            refund_assumptions=forecasts,
            payment_priorities=item.get("payment_priorities",
                                       payment_priorities),
            cancelled_payment_ids=item.get("cancelled_payment_ids",
                                          cancelled_payment_ids),
            extend_payment_windows=True,
            allow_defer_p3=True,
            basis_metadata={
                "refund_research": {
                    "name": item.get("name", f"forecast_{index + 1}"),
                    "forecast_refunds": forecasts,
                    "actual_execution_unchanged": True,
                    "forecast_not_available_balance": True,
                }
            })
        plan = result.plan
        actions = _actions(plan, item.get("cancelled_payment_ids",
                                         cancelled_payment_ids))
        forecast = {
            "name": item.get("name", f"forecast_{index + 1}"),
            "forecast_refunds": plan.schedule_basis
            .get("refund_basis", []),
            "payment_actions": actions,
            "executable_now_or_later": [
                flow_id for flow_id, action in actions.items()
                if action["status"].startswith("executable")],
            "waiting_payments": [
                flow_id for flow_id, action in actions.items()
                if action["status"] == "waiting_or_unmet_in_forecast"],
            "cancelled_payments": [
                flow_id for flow_id, action in actions.items()
                if action["status"] == "cancelled_unissued"],
            "remaining_gap": str(money(sum(
                (money(g["shortfall"]) for g in plan.remaining_gaps),
                Decimal("0")))),
            "total_fee": str(plan.total_fee),
        }
        plan.refund_forecasts = [forecast]
        options.append({"plan": plan, **forecast})
    return options


def replan_after_refund_actuals(
        scenario: Scenario,
        prior_plan: Plan,
        execution: Execution,
        cutoff_date: str,
        affected_payment_ids: list[str],
        forecast_name: str = "",
        forecast_snapshot: dict | None = None,
        **replan_kwargs) -> tuple[Plan, list[TransferOrder]]:
    """实际退回与预测不一致后，只重算受影响且尚未执行的付款。

    不受影响的未发出订单作为 locked_orders 原样保留；已发起和已到账事实
    继续来自 Execution，不能撤销。返回 (修订计划, 被保留的未发出订单)。
    """
    affected = set(affected_payment_ids)
    initiated = set(execution.initiated_order_ids) | {
        transfer.order_id for transfer in execution.actual_transfers}
    withdrawn = set(replan_kwargs.get("withdrawn_order_ids",
                                     prior_plan.withdrawn_order_ids))
    unaffected = [
        order for order in prior_plan.orders
        if order.initiate_date > cutoff_date
        and order.order_id not in initiated
        and order.order_id not in withdrawn
        and (order.purpose not in affected if order.purpose else True)
    ]
    preserved_locked = [*prior_plan.locked_orders, *unaffected]
    existing_locked = replan_kwargs.get("locked_orders")
    replan_kwargs["locked_orders"] = (
        existing_locked if existing_locked is not None else preserved_locked)
    replan_kwargs.setdefault("extend_payment_windows", True)
    replan_kwargs.setdefault("allow_defer_p3", True)
    result = replan(scenario, prior_plan, execution, cutoff_date,
                    **replan_kwargs)
    revised = result.plan
    revised.refund_revision = {
        "source_plan_created_at": prior_plan.created_at,
        "cutoff_date": cutoff_date,
        "forecast_name": forecast_name,
        "forecast_snapshot": forecast_snapshot or {},
        "affected_payment_ids": sorted(affected),
        "recalculated_scope": "affected_unexecuted_payments_only",
        "preserved_payment_ids": sorted(
            flow.flow_id for flow in scenario.flows
            if flow.kind == FlowKind.OUTFLOW and
            flow.date > cutoff_date and
            flow.flow_id not in affected),
        "preserved_unissued_order_ids": [order.order_id
                                         for order in unaffected],
        "issued_or_arrived_facts_irreversible": True,
    }
    revised.schedule_basis["refund_revision"] = revised.refund_revision
    return revised, unaffected
