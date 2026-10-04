"""第五轮：在途划拨的部分拒绝与退回。

原划拨尚未到达收款方时，一部分本金可能先被退回，另一部分仍在途，
之后才分别到账或再次退回。本模块把三类事件严格区分：

- 拒绝通知（TransferReturn 登记，actual_date 为空）：只是退回安排，
  不产生任何可用资金；
- 实际退回（actual_date 非空）：本金从在途转入 return_account（可跨账户），
  同日扣收新的退回费用 return_fee（不退还）；原划拨手续费照常不退；
- 收款到账：原划拨到账时，收款方只记 本金 - 未执行退回 的差额，
  已退回部分不会再次全额记给收款方。

核对规则：
- 同一划拨可对应多笔退回，逐笔核对实际来源（在途本金或拆分在途），
  活跃退回总额不得超过划拨本金；重复通知 / 乱序记录不制造现金；
- 预计退回日可修改（revise_return_schedule）；尚未实际到达的退回安排
  可撤销（cancel_return_arrangement），已实际到账的退回继续保留；
- 执行记录可随时保存为 JSON，重开后 replay_with_returns / combined_replay
  从同一本账重放，资金守恒。
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from .engine import ReplayResult, replay
from .ledger import TransferLeg
from .models import money
from .plan import Execution, Plan, TransferReturn
from .replan import build_prefix
from .scenario import Scenario


def _order_map(execution: Execution,
               plan: Plan | None = None) -> dict[str, dict]:
    """划拨单号 -> (source, target, principal)，来自实际执行（含计划兜底）。"""
    orders: dict[str, dict] = {}
    if plan is not None:
        for o in plan.orders:
            orders[o.order_id] = {"source": o.source, "target": o.target,
                                  "principal": o.principal}
    for t in execution.actual_transfers:
        if t.status == "cancelled":
            continue
        orders[t.order_id] = {"source": t.source, "target": t.target,
                              "principal": t.actual_principal}
    return orders


def _active_total(execution: Execution, order_id: str,
                  exclude_id: str | None = None) -> Decimal:
    return sum(
        (r.amount for r in execution.transfer_returns
         if r.order_id == order_id and r.active and r.return_id != exclude_id),
        Decimal("0"))


def _check_return(execution: Execution, record: TransferReturn,
                  orders: dict[str, dict]) -> None:
    if record.order_id not in orders:
        raise ValueError(
            f"退回 {record.return_id} 来源不明：划拨 {record.order_id} "
            "不在已发起划拨中")
    if record.amount <= 0:
        raise ValueError(f"退回 {record.return_id} 金额必须为正")
    principal = orders[record.order_id]["principal"]
    total = _active_total(execution, record.order_id,
                          exclude_id=record.return_id) + record.amount
    if record.active and total > principal:
        raise ValueError(
            f"划拨 {record.order_id} 活跃退回合计 {money(total)} 超过本金 "
            f"{money(principal)}：退回总额不能超过原划拨本金")
    if record.arrived and record.cancelled_date is not None:
        raise ValueError("已实际到账的退回不能再撤销")


def record_return_notice(execution: Execution, record: TransferReturn,
                         plan: Plan | None = None) -> dict:
    """登记拒绝通知 / 退回安排；通知本身不产生可用资金。"""
    _check_return(execution, record, _order_map(execution, plan))
    return execution.record_transfer_return(record)


def record_return_arrival(execution: Execution, return_id: str,
                          actual_date: str,
                          return_fee: Decimal | None = None) -> dict:
    """登记实际退回：退回安排变为实际记录，本金于 actual_date 入退回账户。"""
    existing = next((r for r in execution.transfer_returns
                     if r.return_id == return_id), None)
    if existing is None:
        raise ValueError(f"退回 {return_id} 不存在：先登记拒绝通知再登记实际退回")
    if existing.cancelled_date is not None:
        raise ValueError(f"退回 {return_id} 已撤销，不能再登记实际退回")
    updated = replace(existing, actual_date=actual_date,
                      return_fee=(existing.return_fee if return_fee is None
                                  else money(return_fee)))
    return execution.record_transfer_return(updated)


def revise_return_schedule(execution: Execution, return_id: str,
                           new_expected_date: str) -> dict:
    """修改预计退回日；仅尚未实际到达、未撤销的退回安排可修改。"""
    existing = next((r for r in execution.transfer_returns
                     if r.return_id == return_id), None)
    if existing is None:
        raise ValueError(f"退回 {return_id} 不存在")
    if existing.arrived:
        raise ValueError(f"退回 {return_id} 已实际到账，预计日不可再修改")
    if existing.cancelled_date is not None:
        raise ValueError(f"退回 {return_id} 已撤销")
    return execution.record_transfer_return(
        replace(existing, expected_date=new_expected_date))


def cancel_return_arrangement(execution: Execution, return_id: str,
                              cancelled_date: str) -> dict:
    """撤销尚未执行的退回安排；已实际到账的退回继续保留（拒绝撤销）。"""
    existing = next((r for r in execution.transfer_returns
                     if r.return_id == return_id), None)
    if existing is None:
        raise ValueError(f"退回 {return_id} 不存在")
    if existing.arrived:
        raise ValueError(
            f"退回 {return_id} 已实际到账，属于既有实际记录，继续保留，不能撤销")
    return execution.record_transfer_return(
        replace(existing, cancelled_date=cancelled_date))


def replay_with_returns(scenario: Scenario, execution: Execution,
                        snapshot_daily: bool = True) -> ReplayResult:
    """按实际执行 + 部分退回记录从期初重放全区间，单本账资金守恒。"""
    legs = [TransferLeg(t.order_id, t.source, t.target,
                        t.actual_initiate_date, t.actual_arrival_date,
                        money(t.actual_principal), money(t.actual_fee))
            for t in execution.actual_transfers if t.status != "cancelled"]
    return replay(scenario, legs=legs, snapshot_daily=snapshot_daily,
                  transfer_returns=execution.transfer_returns)


def transfer_disposition(scenario: Scenario, execution: Execution,
                         cutoff_date: str,
                         plan: Plan | None = None) -> dict:
    """按原划拨逐笔核对部分拒绝后的资金去向（截至 cutoff 日终）。

    对每笔被退回的划拨给出：本金、已实际退回（可用）、退回在途/预计退回
    （不可用）、已撤销（仍归收款方）、收款方已到账、原手续费（不退）与
    退回新收费用。
    """
    prefix = build_prefix(scenario, execution, cutoff_date)
    orders = _order_map(execution, plan)
    by_order: dict[str, list[TransferReturn]] = {}
    for r in execution.transfer_returns:
        by_order.setdefault(r.order_id, []).append(r)

    arrived_events = {e["return_id"]: e for e in prefix.ledger.log
                      if e["event"] == "transfer_return"}
    transfers = []
    for order_id, records in sorted(by_order.items()):
        info = orders.get(order_id, {"source": "?", "target": "?",
                                     "principal": Decimal("0")})
        arrived = [r for r in records if r.arrived]
        pending = [r for r in records if r.active and not r.arrived]
        cancelled = [r for r in records if not r.active]
        arrived_amount = sum((r.amount for r in arrived), Decimal("0"))
        pending_amount = sum((r.amount for r in pending), Decimal("0"))
        cancelled_amount = sum((r.amount for r in cancelled), Decimal("0"))
        return_fees = sum((Decimal(arrived_events[r.return_id]["return_fee"])
                           for r in arrived if r.return_id in arrived_events),
                          Decimal("0"))
        transfers.append({
            "order_id": order_id,
            "source": info["source"], "target": info["target"],
            "principal": str(money(info["principal"])),
            "returned_arrived_usable": str(money(arrived_amount)),
            "return_pending_not_usable": str(money(pending_amount)),
            "return_cancelled_back_to_payee": str(money(cancelled_amount)),
            "delivered_to_payee": str(money(
                info["principal"] - arrived_amount - pending_amount)),
            "original_fee_kept": "原划拨手续费不退",
            "return_fees_charged": str(money(return_fees)),
            "returns": [
                {"return_id": r.return_id, "amount": str(r.amount),
                 "return_account": r.return_account,
                 "expected_date": r.expected_date,
                 "actual_date": r.actual_date,
                 "return_fee": str(r.return_fee),
                 "status": ("cancelled" if not r.active else
                            "arrived" if r.arrived else "notice_only")}
                for r in records],
        })

    return {
        "cutoff_date": cutoff_date,
        "transfers": transfers,
        "in_transit": [
            {"order_id": oid, "source": leg.source, "target": leg.target,
             "remaining_principal": str(money(leg.principal)),
             "arrival_date": leg.arrival_date}
            for oid, leg in sorted(prefix.ledger.in_transit.items())],
        "return_in_transit": [
            {"return_id": rid, "order_id": e["order_id"],
             "return_account": e["return_account"],
             "amount": str(money(e["amount"]))}
            for rid, e in sorted(prefix.ledger.return_in_transit.items())],
        "balances": {a.account_id: str(prefix.ledger.available(a.account_id))
                     for a in scenario.accounts},
    }

