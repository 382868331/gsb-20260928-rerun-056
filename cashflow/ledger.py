"""时序账簿：计划/执行的可重放内核。

每日过账顺序（确定性，保证语义无歧义）：
  1) 已知收入到账
  2) 调拨发起：发起方立即扣本金+手续费，钱进入在途
  3) 调拨到账：收款方方可使用（lag=0 也排在发起之后）
  4) 已知支出：按优先级 P1->P2->P3 顺序支付；
     余额不足则部分支付，未付部分记为缺口

关键约束：
  - 发起时若可用余额不足，直接报错（不能把同一笔钱安排两次）；
  - 在途资金既不在付款方也不在收款方可用余额中；
  - 手续费在发起日实时扣付，参与资金占用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable

from .models import FlowKind, Priority, money
from .plan import ActualTransfer, TransferOrder


class LedgerError(RuntimeError):
    pass


@dataclass
class TransferLeg:
    order_id: str
    source: str
    target: str
    initiate_date: str
    arrival_date: str
    principal: Decimal
    fee: Decimal


@dataclass
class PaymentResult:
    flow_id: str
    account_id: str
    date: str
    priority: int
    due: Decimal
    paid: Decimal

    @property
    def shortfall(self) -> Decimal:
        return self.due - self.paid


@dataclass
class DaySnapshot:
    date: str
    balances: dict[str, Decimal]
    in_transit: dict[str, Decimal]  # 按在途单号


@dataclass
class Ledger:
    opening: dict[str, Decimal]
    balances: dict[str, Decimal] = field(default_factory=dict)
    in_transit: dict[str, TransferLeg] = field(default_factory=dict)
    # 已与原划拨拆分、正在退回途中的本金（return_id -> 明细）；
    # 任何一方都不可用，计入在途总额
    return_in_transit: dict[str, dict] = field(default_factory=dict)
    total_fees: Decimal = Decimal("0")
    payments: list[PaymentResult] = field(default_factory=list)
    snapshots: list[DaySnapshot] = field(default_factory=list)
    log: list[dict] = field(default_factory=list)
    total_inflows: Decimal = Decimal("0")
    total_paid: Decimal = Decimal("0")

    @classmethod
    def open(cls, opening: dict[str, Decimal]) -> "Ledger":
        return cls(opening=dict(opening), balances=dict(opening))

    def available(self, account_id: str) -> Decimal:
        return self.balances.get(account_id, Decimal("0"))

    def _record(self, date: str, kind: str, **detail) -> None:
        self.log.append({"date": date, "event": kind, **detail})

    def post_inflow(self, date: str, account_id: str, amount: Decimal,
                    flow_id: str) -> None:
        self.balances[account_id] = self.balances.get(account_id, Decimal("0")) + amount
        self.total_inflows += amount
        self._record(date, "inflow", flow_id=flow_id, account=account_id,
                     amount=str(money(amount)))

    def initiate(self, date: str, leg: TransferLeg) -> None:
        """发起调拨：扣本金+手续费，本金转入在途。"""
        need = leg.principal + leg.fee
        if self.balances.get(leg.source, Decimal("0")) < need:
            raise LedgerError(
                f"{date} 调拨 {leg.order_id} 发起失败：账户 {leg.source} "
                f"可用 {self.available(leg.source)} < 需付 {need}（含手续费 {leg.fee}）"
            )
        self.balances[leg.source] -= need
        self.in_transit[leg.order_id] = leg
        self.total_fees += leg.fee
        self._record(date, "transfer_initiate", order_id=leg.order_id,
                     source=leg.source, target=leg.target,
                     principal=str(leg.principal), fee=str(leg.fee))

    def arrive(self, date: str, order_id: str) -> None:
        leg = self.in_transit.pop(order_id)
        self.balances[leg.target] = self.balances.get(leg.target, Decimal("0")) + leg.principal
        self._record(date, "transfer_arrive", order_id=order_id,
                     target=leg.target, principal=str(leg.principal))

    def arrive_split(self, date: str, order_id: str,
                     pending_returns: list[dict]) -> None:
        """部分拒绝后的划拨到账：收款方只记本金扣除未执行退回后的余额。

        pending_returns 各项含 return_id/return_account/amount/return_fee；
        未执行退回的本金转入 return_in_transit，实际退回时才入退回账户。
        """
        leg = self.in_transit.pop(order_id)
        held = sum((money(r["amount"]) for r in pending_returns), Decimal("0"))
        delivered = leg.principal - held
        if delivered < 0:
            raise LedgerError(
                f"{date} 调拨 {order_id} 到账失败：未执行退回合计 {held} "
                f"超过剩余本金 {leg.principal}")
        self.balances[leg.target] = (
            self.balances.get(leg.target, Decimal("0")) + delivered)
        self._record(date, "transfer_arrive", order_id=order_id,
                     target=leg.target, principal=str(money(delivered)),
                     held_for_return=str(money(held)))
        for r in pending_returns:
            self.return_in_transit[r["return_id"]] = {
                "order_id": order_id, "target": leg.target,
                "return_account": r["return_account"],
                "amount": money(r["amount"]),
                "return_fee": money(r["return_fee"]),
            }
            self._record(date, "transfer_return_in_transit",
                         return_id=r["return_id"], order_id=order_id,
                         amount=str(money(r["amount"])))

    def post_transfer_return(self, date: str, return_id: str, order_id: str,
                             account: str, amount: Decimal,
                             fee: Decimal) -> None:
        """实际退回入账：本金从在途（原划拨或退回在途）转入退回账户。

        退回费用 fee 同日从退回账户扣收（不退还）；退回本金不是新收入，
        只是在途资金的重新归类，因此不计入 total_inflows。
        """
        amount, fee = money(amount), money(fee)
        if order_id in self.in_transit:
            leg = self.in_transit[order_id]
            if leg.principal < amount:
                raise LedgerError(
                    f"{date} 退回 {return_id} 失败：划拨 {order_id} 剩余在途本金 "
                    f"{leg.principal} < 退回 {amount}（退回总额不能超过划拨本金）")
            leg.principal = money(leg.principal - amount)
        elif return_id in self.return_in_transit:
            entry = self.return_in_transit.pop(return_id)
            if entry["amount"] != amount:
                raise LedgerError(
                    f"{date} 退回 {return_id} 金额 {amount} 与拆分在途 "
                    f"{entry['amount']} 不一致")
        else:
            raise LedgerError(
                f"{date} 退回 {return_id} 失败：划拨 {order_id} 不在途，"
                "且该退回无拆分在途记录（重复或来源不明，不能制造现金）")
        self.balances[account] = (
            self.balances.get(account, Decimal("0")) + amount - fee)
        self.total_fees += fee
        self._record(date, "transfer_return", return_id=return_id,
                     order_id=order_id, account=account,
                     amount=str(amount), return_fee=str(fee))

    def cancel_transfer_return(self, date: str, return_id: str,
                               order_id: str) -> None:
        """撤销尚未执行的退回安排。

        本金已拆分在途的，撤销日转交原收款方；本金仍随原划拨在途的，
        无需移动（到账时自然全额付给收款方）。
        """
        entry = self.return_in_transit.pop(return_id, None)
        if entry is not None:
            target = entry["target"]
            self.balances[target] = (
                self.balances.get(target, Decimal("0")) + entry["amount"])
            self._record(date, "transfer_return_cancelled",
                         return_id=return_id, order_id=order_id,
                         delivered_to=target, amount=str(entry["amount"]))
        elif order_id in self.in_transit:
            self._record(date, "transfer_return_cancelled",
                         return_id=return_id, order_id=order_id,
                         note="本金仍随原划拨在途，到账时全额付给收款方")
        else:
            raise LedgerError(
                f"{date} 撤销退回 {return_id} 失败：既无拆分在途记录，"
                f"划拨 {order_id} 也不在途")

    def pay_outflow(self, date: str, flow_id: str, account_id: str,
                    due: Decimal, priority: int) -> PaymentResult:
        """支付支出；余额不足时部分支付，缺口留在 PaymentResult。"""
        avail = self.balances.get(account_id, Decimal("0"))
        paid = min(due, max(avail, Decimal("0")))
        self.balances[account_id] = avail - paid
        self.total_paid += paid
        result = PaymentResult(flow_id, account_id, date, priority, due, paid)
        self.payments.append(result)
        self._record(date, "outflow", flow_id=flow_id, account=account_id,
                     due=str(due), paid=str(paid), shortfall=str(result.shortfall),
                     priority=priority)
        return result

    def snapshot(self, date: str) -> None:
        """抓取日终状态：可用余额取在途之外的部分。"""
        transit = {oid: money(leg.principal)
                   for oid, leg in sorted(self.in_transit.items())}
        for rid, entry in sorted(self.return_in_transit.items()):
            transit[f"RET-{rid}"] = money(entry["amount"])
        self.snapshots.append(DaySnapshot(
            date,
            {k: money(v) for k, v in sorted(self.balances.items())},
            transit,
        ))

    # ---- 核对 ----
    def verify_conservation(self) -> None:
        """资产恒等式：期末余额 + 在途 + 累计手续费
        = 期初余额 + 累计收入 - 累计已付支出。"""
        opening_total = sum(self.opening.values(), Decimal("0"))
        balance_total = sum(self.balances.values(), Decimal("0"))
        transit_total = sum((leg.principal for leg in self.in_transit.values()),
                            Decimal("0"))
        transit_total += sum((e["amount"] for e in self.return_in_transit.values()),
                             Decimal("0"))
        lhs = balance_total + transit_total + self.total_fees
        rhs = opening_total + self.total_inflows - self.total_paid
        if money(lhs) != money(rhs):
            raise LedgerError(
                f"资金账不平：余额{balance_total}+在途{transit_total}+费{self.total_fees}"
                f"={lhs} != 期初{opening_total}+收入{self.total_inflows}-支出{self.total_paid}={rhs}"
            )
        for acc, bal in self.balances.items():
            if bal < 0:
                raise LedgerError(f"账户 {acc} 余额为负: {bal}")

    @staticmethod
    def legs_from_orders(orders: Iterable[TransferOrder | ActualTransfer]) -> list[TransferLeg]:
        legs = []
        for o in orders:
            if isinstance(o, ActualTransfer):
                if o.status == "cancelled":
                    continue
                idate, adate = o.actual_initiate_date, o.actual_arrival_date
                principal, fee = o.actual_principal, o.actual_fee
            else:
                idate, adate = o.initiate_date, o.arrival_date
                principal, fee = o.principal, o.fee
            legs.append(TransferLeg(
                o.order_id, o.source, o.target, idate, adate,
                money(principal), money(fee),
            ))
        return legs
