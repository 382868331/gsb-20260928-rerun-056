"""推演场景：虚构账户、已知收支、调拨限制，独立保存为 JSON。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from .models import Account, CashFlow, FlowKind, Priority, TransferRule, money


class ScenarioError(ValueError):
    pass


@dataclass
class Scenario:
    scenario_id: str
    start_date: str
    end_date: str
    accounts: list[Account]
    flows: list[CashFlow] = field(default_factory=list)
    transfer_rules: list[TransferRule] = field(default_factory=list)
    description: str = ""

    # ---- 便捷查询 ----
    @property
    def account_ids(self) -> set[str]:
        return {a.account_id for a in self.accounts}

    def account(self, account_id: str) -> Account:
        for acc in self.accounts:
            if acc.account_id == account_id:
                return acc
        raise ScenarioError(f"未知账户: {account_id}")

    def rules_from(self, account_id: str) -> list[TransferRule]:
        return [r for r in self.transfer_rules if r.source == account_id]

    def rule_between(self, source: str, target: str) -> TransferRule | None:
        for r in self.transfer_rules:
            if r.source == source and r.target == target:
                return r
        return None

    # ---- 校验 ----
    def validate(self, check_dates: bool = True) -> None:
        if not (self.start_date <= self.end_date):
            raise ScenarioError("start_date 必须不晚于 end_date")
        ids = self.account_ids
        if len(ids) != len(self.accounts):
            raise ScenarioError("账户 ID 重复")
        for acc in self.accounts:
            if acc.opening_balance < 0:
                raise ScenarioError(f"账户 {acc.account_id} 期初余额不能为负")
        seen_flow: set[str] = set()
        for f in self.flows:
            if f.flow_id in seen_flow:
                raise ScenarioError(f"收支 ID 重复: {f.flow_id}")
            seen_flow.add(f.flow_id)
            if f.account_id not in ids:
                raise ScenarioError(f"收支 {f.flow_id} 引用了未知账户 {f.account_id}")
            if check_dates and not (self.start_date <= f.date <= self.end_date):
                raise ScenarioError(f"收支 {f.flow_id} 日期 {f.date} 超出推演区间")
            if f.kind not in (FlowKind.INFLOW, FlowKind.OUTFLOW):
                raise ScenarioError(f"收支 {f.flow_id} 类型非法: {f.kind}")
            if f.amount <= 0:
                raise ScenarioError(f"收支 {f.flow_id} 金额必须为正")
            if f.kind == FlowKind.INFLOW and f.priority != Priority.P2_NORMAL:
                raise ScenarioError(f"收入 {f.flow_id} 不应设置优先级")
            if f.kind == FlowKind.OUTFLOW and f.priority not in tuple(Priority):
                raise ScenarioError(f"支出 {f.flow_id} 优先级非法")
        pairs: set[tuple[str, str]] = set()
        for r in self.transfer_rules:
            if r.source not in ids or r.target not in ids:
                raise ScenarioError(f"调拨规则 {r.rule_id} 引用了未知账户")
            if r.source == r.target:
                raise ScenarioError(f"调拨规则 {r.rule_id} 不能自转")
            if (r.source, r.target) in pairs:
                raise ScenarioError(f"调拨规则重复: {r.rule_id}")
            pairs.add((r.source, r.target))
            if r.lag_days < 0:
                raise ScenarioError(f"调拨规则 {r.rule_id} 到账天数不能为负")
            if r.fee_rate < 0 or r.fee_fixed < 0:
                raise ScenarioError(f"调拨规则 {r.rule_id} 手续费不能为负")
            if r.max_amount is not None and r.min_amount > r.max_amount:
                raise ScenarioError(f"调拨规则 {r.rule_id} 金额上下限矛盾")

    # ---- JSON 持久化 ----
    def to_dict(self) -> dict:
        return {
            "scenario_id": self.scenario_id,
            "description": self.description,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "accounts": [
                {"account_id": a.account_id, "name": a.name,
                 "opening_balance": str(a.opening_balance)}
                for a in self.accounts
            ],
            "flows": [
                {"flow_id": f.flow_id, "account_id": f.account_id, "date": f.date,
                 "kind": f.kind, "amount": str(f.amount),
                 "priority": int(f.priority), "memo": f.memo}
                for f in self.flows
            ],
            "transfer_rules": [
                {"source": r.source, "target": r.target,
                 "lag_days": r.lag_days, "fee_rate": str(r.fee_rate),
                 "fee_fixed": str(r.fee_fixed),
                 "min_amount": str(r.min_amount),
                "max_amount": None if r.max_amount is None else str(r.max_amount)}
                for r in self.transfer_rules
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Scenario":
        accounts = [
            Account(a["account_id"], a["name"], money(a["opening_balance"]))
            for a in data["accounts"]
        ]
        flows = [
            CashFlow(
                f["flow_id"], f["account_id"], f["date"], f["kind"],
                money(f["amount"]), int(f.get("priority", Priority.P2_NORMAL)),
                f.get("memo", ""),
            )
            for f in data.get("flows", [])
        ]
        rules = [
            TransferRule(
                r["source"], r["target"], int(r["lag_days"]),
                Decimal(r["fee_rate"]), Decimal(r["fee_fixed"]),
                Decimal(r["min_amount"]),
                None if r["max_amount"] is None else Decimal(r["max_amount"]),
            )
            for r in data.get("transfer_rules", [])
        ]
        sc = cls(data["scenario_id"], data["start_date"], data["end_date"],
                 accounts, flows, rules, data.get("description", ""))
        sc.validate()
        return sc

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path

    @classmethod
    def load(cls, path: str | Path) -> "Scenario":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(data)
