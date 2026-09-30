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
    """一条收入的实际到账记录；金额可为部分到账，日期可推迟。"""

    flow_id: str
    account_id: str
    scheduled_date: str       # 场景中的原定到账日
    actual_date: str          # 实际到账日（晚于原定即延误）
    scheduled_amount: Decimal
    actual_amount: Decimal    # 小于原定即部分到账

    def to_dict(self) -> dict:
        d = asdict(self)
        d["scheduled_amount"] = str(self.scheduled_amount)
        d["actual_amount"] = str(self.actual_amount)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ActualInflow":
        return cls(
            d["flow_id"], d["account_id"], d["scheduled_date"],
            d["actual_date"], money(d["scheduled_amount"]),
            money(d["actual_amount"]),
        )


@dataclass(frozen=True)
class RuleChange:
    """调拨规则在某日生效的变化（如手续费上调、到账变慢）。

    仅对生效日及之后新发起的调拨有效；在途调拨沿用发起时的条件。
    """

    source: str
    target: str
    effective_date: str
    lag_days: int | None = None
    fee_rate: Decimal | None = None
    fee_fixed: Decimal | None = None
    max_amount: Decimal | None = None
    note: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        if self.fee_rate is not None:
            d["fee_rate"] = str(self.fee_rate)
        if self.fee_fixed is not None:
            d["fee_fixed"] = str(self.fee_fixed)
        if self.max_amount is not None:
            d["max_amount"] = str(self.max_amount)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "RuleChange":
        return cls(
            d["source"], d["target"], d["effective_date"],
            d.get("lag_days"),
            None if d.get("fee_rate") is None else Decimal(d["fee_rate"]),
            None if d.get("fee_fixed") is None else Decimal(d["fee_fixed"]),
            None if d.get("max_amount") is None else Decimal(d["max_amount"]),
            d.get("note", ""),
        )


@dataclass
class Execution:
    """实际执行记录，与计划分开保存，供下一轮做计划 vs 实际差异分析。"""

    scenario_id: str
    plan_created_at: str
    recorded_at: str
    actual_transfers: list[ActualTransfer] = field(default_factory=list)
    actual_inflows: list[ActualInflow] = field(default_factory=list)
    rule_changes: list[RuleChange] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "scenario_id": self.scenario_id,
            "plan_created_at": self.plan_created_at,
            "recorded_at": self.recorded_at,
            "actual_transfers": [t.to_dict() for t in self.actual_transfers],
            "actual_inflows": [f.to_dict() for f in self.actual_inflows],
            "rule_changes": [c.to_dict() for c in self.rule_changes],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Execution":
        return cls(
            d["scenario_id"], d["plan_created_at"], d["recorded_at"],
            [ActualTransfer.from_dict(t) for t in d.get("actual_transfers", [])],
            [ActualInflow.from_dict(f) for f in d.get("actual_inflows", [])],
            [RuleChange.from_dict(c) for c in d.get("rule_changes", [])],
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


@dataclass
class RevisedPlan:
    """在某个截断日（重开日）早晨，基于已发生事实给出的剩余可执行计划。

    - committed_orders：截断日之前已发起的调拨（含仍在途），保持原样，
      重规划不得再次动用其已离开原账户的本金；在途笔在 revised_orders 之外
      单独保存，用于全程对账。
    - revised_orders：截断日起新安排（或原计划保留、或改走更贵路径）的调拨。
    - deferrals：被推迟的可选（P3）支出及推迟到的日期、理由。
    - choices：路径/取舍选择的逐条理由（如更贵但及时）。
    - remaining_gaps：修订后仍无法满足的支出。
    """

    scenario_id: str
    base_plan_created_at: str
    revision_no: int
    created_at: str
    cutoff_date: str
    committed_orders: list[ActualTransfer] = field(default_factory=list)
    revised_orders: list[TransferOrder] = field(default_factory=list)
    deferrals: list[dict] = field(default_factory=list)
    choices: list[dict] = field(default_factory=list)
    remaining_gaps: list[dict] = field(default_factory=list)
    total_fee: Decimal = Decimal("0")
    objective: tuple[int, ...] = (0, 0, 0, 0)
    notes: str = ""

    @property
    def in_transit_orders(self) -> list[ActualTransfer]:
        """截断日早晨仍在途（实际到账日 >= 截断日）的已发起调拨。"""
        return [t for t in self.committed_orders
                if t.status != "cancelled" and t.actual_arrival_date >= self.cutoff_date]

    def to_dict(self) -> dict:
        return {
            "scenario_id": self.scenario_id,
            "base_plan_created_at": self.base_plan_created_at,
            "revision_no": self.revision_no,
            "created_at": self.created_at,
            "cutoff_date": self.cutoff_date,
            "committed_orders": [t.to_dict() for t in self.committed_orders],
            "revised_orders": [o.to_dict() for o in self.revised_orders],
            "deferrals": self.deferrals,
            "choices": self.choices,
            "remaining_gaps": self.remaining_gaps,
            "total_fee": str(self.total_fee),
            "objective": list(self.objective),
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RevisedPlan":
        return cls(
            d["scenario_id"], d["base_plan_created_at"],
            int(d.get("revision_no", 1)), d["created_at"], d["cutoff_date"],
            [ActualTransfer.from_dict(t) for t in d.get("committed_orders", [])],
            [TransferOrder.from_dict(o) for o in d.get("revised_orders", [])],
            d.get("deferrals", []), d.get("choices", []),
            d.get("remaining_gaps", []), money(d.get("total_fee", "0")),
            tuple(d.get("objective", [0, 0, 0, 0])), d.get("notes", ""),
        )

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path

    @classmethod
    def load(cls, path: str | Path) -> "RevisedPlan":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
