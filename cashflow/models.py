"""基础模型：账户、已知收支、账户间调拨限制。

金额一律使用 Decimal，避免浮点误差。时间使用 ISO 日期字符串（YYYY-MM-DD）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import IntEnum


class FlowKind:
    INFLOW = "inflow"      # 已知收入
    OUTFLOW = "outflow"    # 已知支出


class Priority(IntEnum):
    """支出优先级：数字越小越优先，必须先被满足。"""

    P1_CRITICAL = 1   # 优先支出（如工资、税款、必付货款）
    P2_NORMAL = 2     # 一般支出
    P3_OPTIONAL = 3   # 可延后支出


@dataclass(frozen=True)
class Account:
    account_id: str
    name: str
    opening_balance: Decimal = Decimal("0")  # 推演起始日期初余额


@dataclass(frozen=True)
class CashFlow:
    """一条已知收支，发生在指定账户、指定日期。"""

    flow_id: str
    account_id: str
    date: str                      # 发生日（收入到账日 / 支出应付日）
    kind: str                      # FlowKind
    amount: Decimal                # 恒为正数
    priority: int = Priority.P2_NORMAL
    memo: str = ""
    # 支出允许支付窗口。date 始终保留输入的原始期限，不改写历史输入。
    window_start: str | None = None
    window_end: str | None = None

    @property
    def signed_amount(self) -> Decimal:
        return self.amount if self.kind == FlowKind.INFLOW else -self.amount

    @property
    def payment_start(self) -> str:
        return self.window_start or self.date

    @property
    def payment_end(self) -> str:
        return self.window_end or self.date


@dataclass(frozen=True)
class TransferRule:
    """账户间调拨限制。

    - 只能从 source 调往 target（有向）。
    - lag_days：发起日到到账日的自然日天数；到账前收款方不能使用。
    - fee_rate：按调拨本金比例收取的手续费（发起方在发起日另付）。
    - fee_fixed：每笔固定手续费（发起方在发起日另付）。
    - min_amount / max_amount：单笔本金上下限。
    """

    source: str
    target: str
    lag_days: int = 0
    fee_rate: Decimal = Decimal("0")
    fee_fixed: Decimal = Decimal("0")
    min_amount: Decimal = Decimal("0")
    max_amount: Decimal | None = None

    def fee_for(self, principal: Decimal) -> Decimal:
        return (principal * self.fee_rate + self.fee_fixed).quantize(CENT)

    @property
    def rule_id(self) -> str:
        return f"{self.source}->{self.target}"


CENT = Decimal("0.01")


def money(value: str | int | Decimal) -> Decimal:
    """统一金额构造，保留两位小数。"""
    return Decimal(str(value)).quantize(CENT)
