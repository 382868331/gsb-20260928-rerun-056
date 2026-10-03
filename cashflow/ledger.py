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
    total_fees: Decimal = Decimal("0")
    payments: list[PaymentResult] = field(default_factory=list)
    snapshots: list[DaySnapshot] = field(default_factory=list)
    log: list[dict] = field(default_factory=list)
    total_inflows: Decimal = Decimal("0")
    total_paid: Decimal = Decimal("0")
    total_refunds: Decimal = Decimal("0")          # 退回本金合计（含在途冲回）
    refund_from_transit: Decimal = Decimal("0")    # 其中从在途冲回的部分
    total_fee_refunds: Decimal = Decimal("0")      # 退回的手续费
    refund_ids: set = field(default_factory=set)

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

    def post_refund(self, date: str, record) -> None:
        """退回/拒付到账：可只退一部分，原手续费未必退回。

        - 若原调拨仍在途，退回部分直接从在途冲回（不再发往收款方）；
          剩余在途本金仍按原到账日入账，已扣手续费不动。
        - 其余退回（收款方已收后退回、跨账户返回）记为外部回流，
          贷入 credit_account，到账后才成为可用余额。
        - fee_refunded 冲减累计手续费；默认 0（手续费不退）。
        """
        from .plan import RefundRecord
        assert isinstance(record, RefundRecord)
        if record.refund_id in self.refund_ids:
            raise LedgerError(f"退回记录重复入账: {record.refund_id}")
        self.refund_ids.add(record.refund_id)
        amount = money(record.amount)
        fee_back = money(record.fee_refunded)
        from_transit = Decimal("0")
        if record.target == "order" and record.ref_id in self.in_transit:
            leg = self.in_transit[record.ref_id]
            from_transit = min(amount, leg.principal)
            leg.principal = money(leg.principal - from_transit)
            if leg.principal == 0:
                del self.in_transit[record.ref_id]
        self.refund_from_transit += from_transit
        self.total_refunds += amount
        self.total_fee_refunds += fee_back
        self.total_fees -= fee_back
        credit = amount + fee_back
        self.balances[record.credit_account] = (
            self.balances.get(record.credit_account, Decimal("0")) + credit)
        self._record(date, "refund", refund_id=record.refund_id,
                     refund_kind=record.kind, target=record.target,
                     ref_id=record.ref_id, account=record.credit_account,
                     amount=str(amount), fee_refunded=str(fee_back),
                     from_transit=str(money(from_transit)))

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
        self.snapshots.append(DaySnapshot(
            date,
            {k: money(v) for k, v in sorted(self.balances.items())},
            {oid: money(leg.principal) for oid, leg in sorted(self.in_transit.items())},
        ))

    # ---- 核对 ----
    def verify_conservation(self) -> None:
        """资产恒等式：期末余额 + 在途 + 累计手续费
        = 期初余额 + 累计收入 + 外部退回 - 累计已付支出。

        在途冲回的退回与手续费退回只是账内搬家，不改变恒等式两侧；
        只有收款方已收后又退回的部分（外部回流）计入右侧。
        """
        opening_total = sum(self.opening.values(), Decimal("0"))
        balance_total = sum(self.balances.values(), Decimal("0"))
        transit_total = sum((leg.principal for leg in self.in_transit.values()),
                            Decimal("0"))
        lhs = balance_total + transit_total + self.total_fees
        external_refunds = self.total_refunds - self.refund_from_transit
        rhs = opening_total + self.total_inflows + external_refunds - self.total_paid
        if money(lhs) != money(rhs):
            raise LedgerError(
                f"资金账不平：余额{balance_total}+在途{transit_total}+费{self.total_fees}"
                f"={lhs} != 期初{opening_total}+收入{self.total_inflows}"
                f"+外部退回{external_refunds}-支出{self.total_paid}={rhs}"
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
