"""计划与实际执行记录：分别保存，均为按日期可重放的事件。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from decimal import Decimal
from pathlib import Path

from .models import money


@dataclass(frozen=True)
class TransferOrder:
    """一笔调拨指令（计划层）。

    发起日扣 source：principal + fee；到账日加 target：principal。
    """

    order_id: str
    source: str
    target: str
    initiate_date: str
    arrival_date: str
    principal: Decimal
    fee: Decimal
    purpose: str = ""          # 对应要覆盖的支出 flow_id，可空

    def to_dict(self) -> dict:
        d = asdict(self)
        d["principal"] = str(self.principal)
        d["fee"] = str(self.fee)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "TransferOrder":
        return cls(
            d["order_id"], d["source"], d["target"],
            d["initiate_date"], d["arrival_date"],
            money(d["principal"]), money(d["fee"]), d.get("purpose", ""),
        )


@dataclass
class Plan:
    """推演输出的调拨计划。

    remaining_gaps 为求解器仍无法覆盖的缺口（按支出逐条记录）。
    目标排序：先满足优先支出，再减少调拨费用。
    """

    scenario_id: str
    created_at: str
    orders: list[TransferOrder] = field(default_factory=list)
    remaining_gaps: list[dict] = field(default_factory=list)
    total_fee: Decimal = Decimal("0")
    objective: tuple[int, ...] = (0, 0, 0, 0)  # (P1缺口,P2缺口,P3缺口,总费用*100)
    solver: str = "greedy"
    notes: str = ""
    # ---- 第二轮：修订重规划元数据 ----
    revision_of: str = ""                # 被修订原计划的 created_at；空表示首轮计划
    revised_at: str = ""                 # 重规划时刻（cutoff 次日）
    cutoff_date: str = ""                # 已过账截止日（重放前缀末天）
    fixed_transit: list[TransferOrder] = field(default_factory=list)   # cutoff 时仍在途，保持不动
    payment_decisions: list[dict] = field(default_factory=list)        # 支付决策：准时/推迟/未满足
    rule_amendments: list[dict] = field(default_factory=list)          # 生效的费率调整
    expected_inflows: list[dict] = field(default_factory=list)         # 修订时刻确认的后续收入
    selected_strategy: str = "strict_due"
    payment_strategy_options: list[dict] = field(default_factory=list)
    feasibility_status: str = ""
    schedule_basis: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "scenario_id": self.scenario_id,
            "created_at": self.created_at,
            "solver": self.solver,
            "objective": list(self.objective),
            "total_fee": str(self.total_fee),
            "notes": self.notes,
            "orders": [o.to_dict() for o in self.orders],
            "remaining_gaps": self.remaining_gaps,
            "revision_of": self.revision_of,
            "revised_at": self.revised_at,
            "cutoff_date": self.cutoff_date,
            "fixed_transit": [o.to_dict() for o in self.fixed_transit],
            "payment_decisions": self.payment_decisions,
            "rule_amendments": self.rule_amendments,
            "expected_inflows": self.expected_inflows,
            "selected_strategy": self.selected_strategy,
            "payment_strategy_options": self.payment_strategy_options,
            "feasibility_status": self.feasibility_status,
            "schedule_basis": self.schedule_basis,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Plan":
        return cls(
            d["scenario_id"], d["created_at"],
            [TransferOrder.from_dict(o) for o in d.get("orders", [])],
            d.get("remaining_gaps", []),
            money(d.get("total_fee", "0")),
            tuple(d.get("objective", [0, 0, 0, 0])),
            d.get("solver", "greedy"), d.get("notes", ""),
            d.get("revision_of", ""), d.get("revised_at", ""),
            d.get("cutoff_date", ""),
            [TransferOrder.from_dict(o) for o in d.get("fixed_transit", [])],
            d.get("payment_decisions", []), d.get("rule_amendments", []),
            d.get("expected_inflows", []),
            d.get("selected_strategy", "strict_due"),
            d.get("payment_strategy_options", []),
            d.get("feasibility_status", ""),
            d.get("schedule_basis", {}),
        )

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path

    @classmethod
    def load(cls, path: str | Path) -> "Plan":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


@dataclass(frozen=True)
class ActualTransfer:
    """一笔调拨的实际执行记录；可与计划不同（延后、部分、取消）。"""

    order_id: str
    source: str
    target: str
    actual_initiate_date: str
    actual_arrival_date: str
    actual_principal: Decimal
    actual_fee: Decimal
    status: str = "executed"   # executed / cancelled

    def to_dict(self) -> dict:
        d = asdict(self)
        d["actual_principal"] = str(self.actual_principal)
        d["actual_fee"] = str(self.actual_fee)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ActualTransfer":
        return cls(
            d["order_id"], d["source"], d["target"],
            d["actual_initiate_date"], d["actual_arrival_date"],
            money(d["actual_principal"]), money(d["actual_fee"]),
        d.get("status", "executed"),
        )


@dataclass(frozen=True)
class ActualInflow:
    """收入的实际到账记录；金额可少于应收（部分到账）。"""

    flow_id: str
    account_id: str
    expected_date: str   # 场景中原定到账日
    actual_date: str     # 实际到账日（可晚于原定日）
    amount: Decimal      # 实际到账金额；0 表示确认未到

    def to_dict(self) -> dict:
        d = asdict(self)
        d["amount"] = str(self.amount)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ActualInflow":
        return cls(
            d["flow_id"], d["account_id"], d["expected_date"],
            d["actual_date"], money(d["amount"]),
        )


@dataclass
class Execution:
    """实际执行记录，与计划分开保存，供下一轮做计划 vs 实际差异分析。"""

    scenario_id: str
    plan_created_at: str
    recorded_at: str
    actual_transfers: list[ActualTransfer] = field(default_factory=list)
    # 已发起（资金已离开原账户）的调拨单号；未列入者视为尚未发起，可重新规划
    initiated_order_ids: list[str] = field(default_factory=list)
    # 收入实际到账记录：金额可少于计划（部分到账），未列入的往期收入视为未到账
    actual_inflows: list["ActualInflow"] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "scenario_id": self.scenario_id,
            "plan_created_at": self.plan_created_at,
            "recorded_at": self.recorded_at,
            "actual_transfers": [t.to_dict() for t in self.actual_transfers],
            "initiated_order_ids": self.initiated_order_ids,
            "actual_inflows": [f.to_dict() for f in self.actual_inflows],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Execution":
        return cls(
            d["scenario_id"], d["plan_created_at"], d["recorded_at"],
            [ActualTransfer.from_dict(t) for t in d.get("actual_transfers", [])],
            d.get("initiated_order_ids", []),
            [ActualInflow.from_dict(f) for f in d.get("actual_inflows", [])],
        )

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path

    @classmethod
    def load(cls, str_or_path: str | Path) -> "Execution":
        return cls.from_dict(
            json.loads(Path(str_or_path).read_text(encoding="utf-8"))
        )
