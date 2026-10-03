"""第四轮：退回资金与剩余支付重规划。

已发出的支付安排可能只退回一部分资金，原手续费也未必退回。本模块提供：

- 退回/拒付实际记录（RefundRecord，见 plan.py）按 refund_id 去重：
  重复通知被忽略，同一笔款的更正覆盖原记录，不会增加第二份余额；
- refund_disposition()：按原付款逐笔区分 原付款额 / 收款方已收到的部分 /
  已退回且可再次使用的余额 / 预计退回（不能提前当作可用资金）/
  手续费是否退回，并列出 cutoff 时仍在途的调拨；
- compare_refund_strategies()：退回到达（或预计到达）后，可以只把其中一部分
  用于尚未确认的付款；对照「继续等待退回」与「立即采用替代账户安排」
  两种选择的剩余缺口与费用。

原安排已经发生的调拨不回滚：退回是新的入账事件，而不是撤销原调拨；
撤回一个候选只释放它尚未执行的占用（见 adoption.withdraw_arrangements）。
"""

from __future__ import annotations

from decimal import Decimal

from .models import FlowKind, money
from .plan import Execution, Plan, RefundRecord
from .replan import build_prefix, replan
from .scenario import Scenario


def record_refunds(execution: Execution,
                   records: list[RefundRecord]) -> list[dict]:
    """批量登记退回记录；返回每笔的处理说明（去重/更正/新增）。"""
    return [execution.record_refund(r) for r in records]


def refund_disposition(scenario: Scenario, execution: Execution,
                       cutoff_date: str) -> dict:
    """按原付款逐笔核对退回去向，并列出在途金额与账户可用余额。

    返回：
      payments: 每笔被退回的原付款 -> 原付款额、收款方已收到（净留存）、
                已退回可用、预计退回（不可用）、手续费退回；
      in_transit: cutoff 时仍在途的调拨（任何一方都不可用）；
      balances: cutoff 日终各账户可用余额。
    """
    prefix = build_prefix(scenario, execution, cutoff_date)
    paid_map = {p.flow_id: p.paid for p in prefix.ledger.payments}

    by_flow: dict[str, list[RefundRecord]] = {}
    for r in execution.refunds:
        by_flow.setdefault(r.original_flow_id, []).append(r)

    payments = []
    for flow_id, records in sorted(by_flow.items()):
        arrived = [r for r in records if r.arrived]
        expected = [r for r in records if not r.arrived]
        arrived_amount = sum((r.amount for r in arrived), Decimal("0"))
        expected_amount = sum((r.amount for r in expected), Decimal("0"))
        fee_back = sum((r.fee_refunded for r in arrived), Decimal("0"))
        paid = paid_map.get(flow_id, Decimal("0"))
        payments.append({
            "flow_id": flow_id,
            "paid": str(money(paid)),
            "payee_kept": str(money(paid - arrived_amount)),
            "refund_arrived_usable": str(money(arrived_amount)),
            "refund_expected_not_usable": str(money(expected_amount)),
            "fee_refunded": str(money(fee_back)),
            "refunds": [
                {"refund_id": r.refund_id, "kind": r.kind,
                 "amount": str(r.amount),
                 "expected_date": r.expected_date,
                 "actual_date": r.actual_date,
                 "return_account": r.return_account,
                 "status": "arrived" if r.arrived else "expected"}
                for r in records],
        })

    return {
        "cutoff_date": cutoff_date,
        "payments": payments,
        "in_transit": [
            {"order_id": oid, "source": leg.source, "target": leg.target,
             "principal": str(money(leg.principal)),
             "arrival_date": leg.arrival_date}
            for oid, leg in sorted(prefix.ledger.in_transit.items())],
        "balances": {a.account_id: str(prefix.ledger.available(a.account_id))
                     for a in scenario.accounts},
    }


def _summarize(plan: Plan) -> dict:
    gap = sum((Decimal(g["shortfall"]) for g in plan.remaining_gaps),
              Decimal("0"))
    delayed = {d["flow_id"]: d["scheduled_date"]
               for d in plan.payment_decisions
               if d["status"] in ("deferred", "late_paid")}
    return {
        "remaining_gap": str(money(gap)),
        "gap_by_flow": {g["flow_id"]: g["shortfall"]
                        for g in plan.remaining_gaps},
        "total_fee": str(plan.total_fee),
        "new_orders": [o.to_dict() for o in plan.orders],
        "delayed_payments": delayed,
        "decisions": {d["flow_id"]: d["status"]
                      for d in plan.payment_decisions},
    }


def compare_refund_strategies(scenario: Scenario, original_plan: Plan,
                              execution: Execution, cutoff_date: str,
                              expected_refund_ids: list[str] | None = None,
                              **replan_kwargs) -> list[dict]:
    """对照两种剩余支付重规划：

    - 继续等待退回：预计退回按预计日期入账后再安排剩余付款
      （仅作为对照假设显式启用；默认重规划不会把预计退回当作可用资金）；
    - 替代账户安排：不等退回，立即用其他账户调拨覆盖尚未确认的付款。

    两者都保留原支付窗口、调拨/费率约束与固定在途；报告剩余缺口与费用。
    """
    expected = {r.refund_id: r.expected_date
                for r in execution.refunds if not r.arrived}
    if expected_refund_ids is not None:
        expected = {rid: day for rid, day in expected.items()
                    if rid in expected_refund_ids}

    wait = replan(scenario, original_plan, execution, cutoff_date,
                  refund_assumptions=dict(expected),
                  **replan_kwargs).plan
    alternative = replan(scenario, original_plan, execution, cutoff_date,
                         **replan_kwargs).plan

    options = [
        {"name": "等待退回",
         "assumed_refunds": [
             {"refund_id": rid, "assumed_date": day}
             for rid, day in sorted(expected.items())],
         **_summarize(wait)},
        {"name": "替代账户安排",
         "assumed_refunds": [],
         **_summarize(alternative)},
    ]
    min_fee = min(Decimal(o["total_fee"]) for o in options)
    for o in options:
        o["fee_premium"] = str(money(Decimal(o["total_fee"]) - min_fee))
        o["constraints_preserved"] = {
            "payment_window": True, "transfer_rules": True,
            "fee_rules": True,
            "fixed_transit_order_ids": [
                t.order_id for t in wait.fixed_transit]}
    return options
