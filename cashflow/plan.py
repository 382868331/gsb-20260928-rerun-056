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
    # ---- 第三轮：候选方案的部分采纳 ----
    # 已确认但尚未发起的调拨；重放/重规划时与普通订单一样扣款，因此会实际占用余额。
    locked_orders: list[TransferOrder] = field(default_factory=list)
    # 逐笔确认范围：订单、对应支付、确认金额、费用、状态及是否可撤回。
    confirmed_arrangements: list[dict] = field(default_factory=list)
    # 两个及以上部分采纳选择的对照（原约束、确认范围、剩余缺口、延迟/费用影响）。
    partial_adoption_options: list[dict] = field(default_factory=list)
    withdrawn_order_ids: list[str] = field(default_factory=list)
    committed_fee: Decimal = Decimal("0")
    # ---- 第四轮：退回资金与剩余支付重规划 ----
    # 全部退回/拒付记录（含状态：已到可用 / 预计退回不可用 / 假设到账）
    refund_records: list[dict] = field(default_factory=list)
    # 预计退回但尚未到达的退回（不作为可用资金，仅登记）
    expected_refunds: list[dict] = field(default_factory=list)

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
            "locked_orders": [o.to_dict() for o in self.locked_orders],
            "confirmed_arrangements": self.confirmed_arrangements,
            "partial_adoption_options": self.partial_adoption_options,
            "withdrawn_order_ids": self.withdrawn_order_ids,
            "committed_fee": str(self.committed_fee),
            "refund_records": self.refund_records,
            "expected_refunds": self.expected_refunds,
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
            [TransferOrder.from_dict(o) for o in d.get("locked_orders", [])],
            d.get("confirmed_arrangements", []),
            d.get("partial_adoption_options", []),
            d.get("withdrawn_order_ids", []),
            money(d.get("committed_fee", "0")),
            d.get("refund_records", []),
            d.get("expected_refunds", []),
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


@dataclass(frozen=True)
class RefundRecord:
    """一笔退回/拒付的实际记录（第四轮）。

    已发出的支付安排可能只退回一部分资金，原手续费未必退回。
    - refund_id 唯一：重复通知或同一笔款的更正按同一 refund_id 覆盖，
      不会产生第二份余额；
    - actual_date 为 None 表示退回仍在路上（预计退回），不能当作可用资金；
    - return_account 可与原付款账户不同（跨账户返回）；
    - fee_refunded 为原手续费中实际退回的部分（通常为 0，即手续费不退）。
    """

    refund_id: str
    original_flow_id: str          # 被退回的原支出 flow_id
    payer_account: str             # 原付款账户
    return_account: str            # 退回入账账户（可跨账户返回）
    expected_date: str             # 预计退回日
    amount: Decimal                # 退回本金（部分退回时小于原付款）
    actual_date: str | None = None          # 实际到账日；None 表示尚未到达
    fee_refunded: Decimal = Decimal("0")    # 原手续费退回部分
    kind: str = "refund"           # refund（退回）/ chargeback（拒付）
    note: str = ""

    @property
    def arrived(self) -> bool:
        return self.actual_date is not None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["amount"] = str(self.amount)
        d["fee_refunded"] = str(self.fee_refunded)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "RefundRecord":
        return cls(
            d["refund_id"], d["original_flow_id"], d["payer_account"],
            d["return_account"], d["expected_date"], money(d["amount"]),
            d.get("actual_date"), money(d.get("fee_refunded", "0")),
            d.get("kind", "refund"), d.get("note", ""),
        )


@dataclass(frozen=True)
class RejectionEvent:
    """在途划拨被部分拒绝的事件记录（第五轮）。

    严格区分三个阶段：
    - 拒绝通知（notice_date）：只登记，不增加任何可用资金；
    - 实际退回（actual_return_date 非空）：退回本金扣除退回费用后
      入 return_account（可跨账户），退回费用不退；
    - 收款到账：原划拨到账时，收款方只收到 本金 - 有效拒绝部分，
      已退回部分不会被第二次记给收款方。

    - rejection_id 唯一：重复通知被忽略，同一笔的更正（改预计到账日、
      补实际到账日）按 rejection_id 覆盖，乱序登记不产生第二份现金；
    - 同一 order_id 的有效拒绝总额不得超过该划拨本金（重放时核对）；
    - cancelled=True 表示尚未执行的退回安排被撤销：该部分留在原划拨上，
      随原到账日记给收款方；已实际到账的退回不得撤销。
    """

    rejection_id: str
    order_id: str                # 被拒绝的在途调拨单号
    notice_date: str             # 拒绝通知日（不产生资金变动）
    amount: Decimal              # 被拒绝部分本金
    return_account: str          # 退回入账账户（可与原付款账户不同）
    expected_return_date: str    # 预计退回日（可修改）
    actual_return_date: str | None = None   # 实际退回日；None 表示尚未到达
    return_fee: Decimal = Decimal("0")      # 新退回费用（从退回款中扣，不退）
    cancelled: bool = False      # 尚未执行的退回安排被撤销
    note: str = ""

    @property
    def arrived(self) -> bool:
        return self.actual_return_date is not None

    @property
    def return_date(self) -> str:
        return self.actual_return_date or self.expected_return_date

    def to_dict(self) -> dict:
        d = asdict(self)
        d["amount"] = str(self.amount)
        d["return_fee"] = str(self.return_fee)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "RejectionEvent":
        return cls(
            d["rejection_id"], d["order_id"], d["notice_date"],
            money(d["amount"]), d["return_account"],
            d["expected_return_date"], d.get("actual_return_date"),
            money(d.get("return_fee", "0")), d.get("cancelled", False),
            d.get("note", ""),
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
    # 退回/拒付实际记录：按 refund_id 去重，重复通知/更正不增加第二份余额
    refunds: list["RefundRecord"] = field(default_factory=list)
    # 在途划拨部分拒绝事件：按 rejection_id 去重，通知本身不产生可用资金
    rejections: list["RejectionEvent"] = field(default_factory=list)

    def record_refund(self, record: "RefundRecord") -> dict:
        """登记一笔退回。同一 refund_id 重复通知被忽略，更正则覆盖原记录。"""
        for i, existing in enumerate(self.refunds):
            if existing.refund_id != record.refund_id:
                continue
            if existing == record:
                return {"refund_id": record.refund_id, "action": "duplicate_ignored",
                        "handling": "重复通知：同一 refund_id 已存在，不增加第二份余额"}
            self.refunds[i] = record
            return {"refund_id": record.refund_id, "action": "corrected",
                    "handling": "同一笔退回的更正：按 refund_id 覆盖原记录，余额只算一次"}
        self.refunds.append(record)
        return {"refund_id": record.refund_id, "action": "recorded",
                "handling": "新退回记录"}

    def record_rejection(self, event: "RejectionEvent") -> dict:
        """登记一笔在途部分拒绝。同一 rejection_id 重复通知被忽略，
        更正（改预计日/补实际到账日）覆盖原记录，不会制造第二份现金。"""
        for i, existing in enumerate(self.rejections):
            if existing.rejection_id != event.rejection_id:
                continue
            if existing == event:
                return {"rejection_id": event.rejection_id,
                        "action": "duplicate_ignored",
                        "handling": "重复通知：同一 rejection_id 已存在，不增加第二份余额"}
            if existing.arrived and not event.arrived:
                return {"rejection_id": event.rejection_id,
                        "action": "rejected",
                        "handling": "已实际到账的退回不得改回未到账"}
            self.rejections[i] = event
            return {"rejection_id": event.rejection_id,
                    "action": "corrected",
                    "handling": "同一笔拒绝的更正：按 rejection_id 覆盖原记录，余额只算一次"}
        self.rejections.append(event)
        return {"rejection_id": event.rejection_id, "action": "recorded",
                "handling": "新拒绝通知（仅登记，不产生可用资金）"}

    def cancel_rejection(self, rejection_id: str) -> dict:
        """撤销尚未执行的退回安排；已实际到账的退回保留不变。"""
        for i, existing in enumerate(self.rejections):
            if existing.rejection_id != rejection_id:
                continue
            if existing.cancelled:
                return {"rejection_id": rejection_id,
                        "action": "duplicate_ignored",
                        "handling": "该退回安排此前已撤销"}
            if existing.arrived:
                return {"rejection_id": rejection_id, "action": "rejected",
                        "handling": "已实际到账的退回不得撤销，既有到账保留"}
            self.rejections[i] = RejectionEvent(
                existing.rejection_id, existing.order_id,
                existing.notice_date, existing.amount,
                existing.return_account, existing.expected_return_date,
                existing.actual_return_date, existing.return_fee,
                True, existing.note)
            return {"rejection_id": rejection_id, "action": "cancelled",
                    "handling": "退回安排撤销：该部分留在原划拨上，随原到账日记给收款方"}
        return {"rejection_id": rejection_id, "action": "rejected",
                "handling": "不存在的 rejection_id"}

    def to_dict(self) -> dict:
        return {
            "scenario_id": self.scenario_id,
            "plan_created_at": self.plan_created_at,
            "recorded_at": self.recorded_at,
            "actual_transfers": [t.to_dict() for t in self.actual_transfers],
            "initiated_order_ids": self.initiated_order_ids,
            "actual_inflows": [f.to_dict() for f in self.actual_inflows],
            "refunds": [r.to_dict() for r in self.refunds],
            "rejections": [r.to_dict() for r in self.rejections],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Execution":
        return cls(
            d["scenario_id"], d["plan_created_at"], d["recorded_at"],
            [ActualTransfer.from_dict(t) for t in d.get("actual_transfers", [])],
            d.get("initiated_order_ids", []),
            [ActualInflow.from_dict(f) for f in d.get("actual_inflows", [])],
            [RefundRecord.from_dict(r) for r in d.get("refunds", [])],
            [RejectionEvent.from_dict(r) for r in d.get("rejections", [])],
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
