"""第五轮+：在途划拨的部分拒绝、退回与收款到账。

原划拨尚未到达收款方时，可能一部分先退回付款方（或指定账户），
另一部分仍在途，之后才分别到账或再次退回。本模块把三个阶段严格分开：

- 拒绝通知（RejectionEvent.notice_date）：只登记事件，不增加可用资金；
- 预计退回日期（expected_return_date）：仅为预测，另行展示并清晰标注；
  日期经过但没有实际退回确认时，不得增加账户可用余额；
- 实际退回（actual_return_date）：确认实际日期后才入账，退回本金扣除
  退回费用后入 return_account（可跨账户），原手续费与新退回费用均不退还；
  实际日期晚于预计日期时按实际日期入账，不得改写成提前可用；
- 收款到账：原划拨到账时，收款方只收到 本金 - 有效拒绝部分，
  已退回部分不会被第二次记给收款方。

核对规则：
- 一个划拨可对应多笔退回，按 rejection_id 去重，重复通知被忽略、
  更正覆盖原记录，乱序登记不会制造现金；
- 同一划拨的有效拒绝总额不得超过其本金（重放时逐笔核对实际来源）；
- 研究者可修改预计到账日（更正记录），或撤销尚未执行的退回安排
  （cancel_rejection），已实际到账的退回保留不变；
- 通过保存中途执行记录后重放全部事件，验证部分拒绝、跨账户退回
  与费用调整下的资金守恒。
"""

from __future__ import annotations

from decimal import Decimal

from .engine import ReplayResult, daterange
from .ledger import Ledger, LedgerError, TransferLeg
from .models import FlowKind, money
from .plan import Execution, RejectionEvent
from .scenario import Scenario


def record_rejections(execution: Execution,
                      events: list[RejectionEvent]) -> list[dict]:
    """批量登记在途拒绝事件；返回每笔的处理说明（去重/更正/新增）。"""
    return [execution.record_rejection(e) for e in events]


def revise_rejection(execution: Execution, rejection_id: str,
                     expected_return_date: str | None = None,
                     actual_return_date: str | None = None,
                     note: str | None = None) -> dict:
    """修改预计到账日 / 补记实际到账日；按 rejection_id 覆盖原记录。"""
    for existing in execution.rejections:
        if existing.rejection_id != rejection_id:
            continue
        revised = RejectionEvent(
            existing.rejection_id, existing.order_id, existing.notice_date,
            existing.amount, existing.return_account,
            expected_return_date or existing.expected_return_date,
            actual_return_date if actual_return_date is not None
            else existing.actual_return_date,
            existing.return_fee, existing.cancelled,
            existing.note if note is None else note)
        return execution.record_rejection(revised)
    return {"rejection_id": rejection_id, "action": "rejected",
            "handling": "不存在的 rejection_id"}


def _active_rejections(execution: Execution) -> list[RejectionEvent]:
    return [r for r in execution.rejections if not r.cancelled]


def _validate_against_legs(legs: list[TransferLeg],
                           events: list[RejectionEvent]) -> None:
    """逐笔核对实际来源与总额上限：重复/乱序记录不能制造现金。"""
    remaining = {leg.order_id: money(leg.principal) for leg in legs}
    for e in events:
        if e.order_id not in remaining:
            raise LedgerError(
                f"拒绝 {e.rejection_id} 来源不明：调拨 {e.order_id} "
                "不在实际执行的在途划拨中")
        if e.amount <= 0:
            raise LedgerError(f"拒绝 {e.rejection_id} 金额必须为正")
        remaining[e.order_id] = money(remaining[e.order_id] - e.amount)
        if remaining[e.order_id] < 0:
            raise LedgerError(
                f"调拨 {e.order_id} 的有效拒绝总额超出本金上限："
                f"多出的部分来自 {e.rejection_id}（{e.amount}）")


def replay_with_rejections(scenario: Scenario, execution: Execution,
                           snapshot_daily: bool = True) -> ReplayResult:
    """从期初重放全部事件：实际调拨 + 在途部分拒绝 + 退回/到账。

    每日过账顺序（确定性）：
      1) 收入到账（有实际记录按实际，否则按场景）
      2) 调拨发起
      3) 拒绝通知：拆分在途本金，退回段进入在途（通知日不可用）
      4) 调拨/退回段到账
      5) 支出支付
    """
    scenario.validate()
    legs = Ledger.legs_from_orders(execution.actual_transfers)
    events = sorted(_active_rejections(execution),
                    key=lambda e: (e.notice_date, e.rejection_id))
    _validate_against_legs(legs, events)

    actual_inflows = {f.flow_id: f for f in execution.actual_inflows}
    inflows: dict[str, list] = {}
    outflows: dict[str, list] = {}
    for f in scenario.flows:
        if f.kind == FlowKind.INFLOW:
            act = actual_inflows.get(f.flow_id)
            if act is not None:
                if act.amount == 0:
                    continue
                f = type(f)(f.flow_id, f.account_id, act.actual_date,
                            f.kind, act.amount, f.priority, f.memo,
                            f.window_start, f.window_end)
            inflows.setdefault(f.date, []).append(f)
        else:
            outflows.setdefault(f.date, []).append(f)

    initiations: dict[str, list[TransferLeg]] = {}
    arrivals: dict[str, list[TransferLeg]] = {}
    for leg in legs:
        initiations.setdefault(leg.initiate_date, []).append(leg)
        arrivals.setdefault(leg.arrival_date, []).append(leg)
    notices: dict[str, list[RejectionEvent]] = {}
    for e in events:
        notices.setdefault(e.notice_date, []).append(e)

    ledger = Ledger.open({a.account_id: a.opening_balance
                          for a in scenario.accounts})
    gaps = []
    for day in daterange(scenario.start_date, scenario.end_date):
        for f in sorted(inflows.get(day, []), key=lambda x: x.flow_id):
            ledger.post_inflow(day, f.account_id, f.amount, f.flow_id)
        for leg in sorted(initiations.get(day, []), key=lambda x: x.order_id):
            ledger.initiate(day, leg)
        for e in notices.get(day, []):
            ledger.reject_in_transit(
                day, e.order_id, e.rejection_id, e.amount,
                e.return_account, e.return_fee,
                e.expected_return_date, e.actual_return_date)
        for leg in sorted(arrivals.get(day, []), key=lambda x: x.order_id):
            if leg.order_id not in ledger.in_transit:
                raise LedgerError(
                    f"{day} 调拨 {leg.order_id} 到账失败：该笔调拨此前未能成功发起")
            ledger.arrive(day, leg.order_id)
        # 退回段到账（在途 RET-* 中已确认实际到账日且今日到期者；
        # 仅有预计日期的退回段 arrival_date 为 None，不会在此入账）
        for oid in sorted(o for o, leg in ledger.in_transit.items()
                          if o.startswith("RET-") and leg.arrival_date == day):
            ledger.arrive(day, oid)
        day_outs = sorted(outflows.get(day, []),
                          key=lambda x: (x.priority, x.flow_id))
        for f in day_outs:
            res = ledger.pay_outflow(day, f.flow_id, f.account_id,
                                     f.amount, int(f.priority))
            if res.shortfall > 0:
                gaps.append(res)
        if snapshot_daily:
            ledger.snapshot(day)
        ledger.verify_conservation()
    ledger.verify_conservation()
    return ReplayResult(ledger=ledger, gaps=gaps)


def rejection_disposition(scenario: Scenario, execution: Execution,
                          cutoff_date: str | None = None) -> dict:
    """按原划拨逐笔核对拒绝/退回/到账去向。

    对每笔被拒绝的划拨报告：原划拨本金、收款方将收到（本金-有效拒绝）、
    已退回可用、已确认退回在途（不可用）、预计退回（预测，未确认，
    不可用，单独列出并清晰标注）、已撤销部分、退回费用。
    """
    cutoff_date = cutoff_date or scenario.end_date
    legs = {leg.order_id: leg
            for leg in Ledger.legs_from_orders(execution.actual_transfers)}
    by_order: dict[str, list[RejectionEvent]] = {}
    for e in execution.rejections:
        by_order.setdefault(e.order_id, []).append(e)

    orders = []
    forecast: list[dict] = []
    for order_id, events in sorted(by_order.items()):
        leg = legs.get(order_id)
        principal = leg.principal if leg else Decimal("0")
        active = [e for e in events if not e.cancelled]
        cancelled = [e for e in events if e.cancelled]
        arrived = [e for e in active
                   if e.arrived and e.actual_return_date <= cutoff_date]
        confirmed_transit = [e for e in active
                             if e.arrived and e.actual_return_date > cutoff_date]
        unconfirmed = [e for e in active if not e.arrived]
        arrived_amount = sum((e.amount - e.return_fee for e in arrived),
                             Decimal("0"))
        transit_amount = sum((e.amount - e.return_fee
                              for e in confirmed_transit), Decimal("0"))
        forecast_amount = sum((e.amount - e.return_fee
                               for e in unconfirmed), Decimal("0"))
        rejected_total = sum((e.amount for e in active), Decimal("0"))
        fees = sum((e.return_fee for e in active), Decimal("0"))

        def _status(e: RejectionEvent) -> str:
            if e.cancelled:
                return "cancelled"
            if not e.arrived:
                return "expected_unconfirmed"
            if e.actual_return_date <= cutoff_date:
                return "arrived"
            return "return_in_transit"

        orders.append({
            "order_id": order_id,
            "principal": str(money(principal)),
            "payee_receives": str(money(principal - rejected_total)),
            "rejected_total": str(money(rejected_total)),
            "returned_arrived_usable": str(money(arrived_amount)),
            "return_in_transit_not_usable": str(money(transit_amount)),
            "expected_return_forecast_not_usable": str(money(forecast_amount)),
            "return_fees": str(money(fees)),
            "rejections": [
                {"rejection_id": e.rejection_id,
                 "amount": str(e.amount),
                 "return_account": e.return_account,
                 "expected_return_date": e.expected_return_date,
                 "actual_return_date": e.actual_return_date,
                 "return_fee": str(e.return_fee),
                 "status": _status(e)}
                for e in events],
        })
        for e in unconfirmed:
            forecast.append({
                "rejection_id": e.rejection_id,
                "order_id": order_id,
                "amount": str(money(e.amount - e.return_fee)),
                "return_account": e.return_account,
                "expected_return_date": e.expected_return_date,
                "label": "预计退回（预测，未确认实际到账，不得当作可用资金）",
            })
    return {"cutoff_date": cutoff_date, "orders": orders,
            "expected_returns_forecast": forecast}
