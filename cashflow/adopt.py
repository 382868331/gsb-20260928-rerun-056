"""第三轮：候选方案的部分采纳、确认占款与撤回。

- 部分采纳（confirm_partial）：只确认候选计划中的部分调拨安排；已确认安排
  按其「本金 + 手续费」实际占用账户资金与调拨能力（作为 fixed 订单参与
  重放，其他方案不能同时花费这部分余额），剩余付款重新求解。
- 影响分析：对每个尚未确认的候选安排，给出采纳它会让哪些剩余付款推迟、
  增加多少手续费、让哪些付款失去可行安排。
- 撤回（withdraw_adoption）：已发出的调拨保持在途（本金不再重扣、手续费
  已发生不退回），撤回只影响尚未执行部分，并逐笔说明在途资金如何处理。
- 对照（compare_partials）：两个部分采纳方案并排比较，均保留原始期限
  约束，各自记录确认范围与剩余缺口。
- Adoption 独立持久化（data/adoptions/），重开后可继续采纳或撤回；
  确认集合按单号去重，不会重复占款，已发生费用保留在账上。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from .engine import ReplayResult, replay, solve
from .ledger import LedgerError
from .ledger import TransferLeg
from .models import CashFlow, FlowKind, money
from .plan import Execution, Plan, TransferOrder
from .replan import FeeAmendment, ReplanResult, replan
from .scenario import Scenario


# ---------------- 采纳记录（独立持久化） ----------------

@dataclass
class Adoption:
    """一次部分采纳的持久化记录。

    confirmed 中每条为 {"order": TransferOrder.to_dict(), "status": ...,
    "confirmed_at": ...}；status 取值：
      confirmed        已确认、尚未执行（占款中，可撤回）
      dispatched_kept  撤回时该笔已发出，保持在途（不可撤）
    """

    adoption_id: str
    scenario_id: str
    plan_created_at: str
    created_at: str
    label: str = ""
    confirmed: list[dict] = field(default_factory=list)
    withdrawals: list[dict] = field(default_factory=list)
    status: str = "active"          # active / partially_withdrawn / withdrawn

    # ---- 序列化 ----
    def to_dict(self) -> dict:
        return {
            "adoption_id": self.adoption_id,
            "scenario_id": self.scenario_id,
            "plan_created_at": self.plan_created_at,
            "created_at": self.created_at,
            "label": self.label,
            "confirmed": self.confirmed,
            "withdrawals": self.withdrawals,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Adoption":
        return cls(
            d["adoption_id"], d["scenario_id"], d["plan_created_at"],
            d["created_at"], d.get("label", ""),
            d.get("confirmed", []), d.get("withdrawals", []),
            d.get("status", "active"),
        )

    def save(self, path: "str | Path") -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: "str | Path") -> "Adoption":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def confirmed_orders(adoption: "Adoption | None") -> list[TransferOrder]:
    """已确认安排的订单快照（按单号去重，同一笔钱不会占两次）。"""
    if adoption is None:
        return []
    orders: list[TransferOrder] = []
    seen: set[str] = set()
    for entry in adoption.confirmed:
        order = TransferOrder.from_dict(entry["order"])
        if order.order_id in seen:
            continue
        seen.add(order.order_id)
        orders.append(order)
    return orders


# ---------------- 部分采纳 ----------------

@dataclass
class AdoptionResult:
    adoption: Adoption
    residual_plan: Plan          # 确认占款后，剩余付款的重规划结果（不可行时为 None）
    residual_replay: ReplayResult
    impacts: list[dict]          # 每个尚未确认候选安排的影响分析
    skipped: list[str] = field(default_factory=list)  # 已确认过、未重复占款的单号
    infeasible_reason: str = ""  # 确认集合自身不可行（如打断调拨链）的原因


def _payment_map(result: ReplayResult) -> dict:
    return {p.flow_id: p for p in result.ledger.payments}


def _residual_plan_view(plan: Plan, confirmed: list[TransferOrder]) -> Plan:
    """剩余付款计划视图：费用/目标只计新产生的调拨，确认占款单独列示。"""
    fixed_ids = {o.order_id for o in confirmed}
    new_orders = [o for o in plan.orders if o.order_id not in fixed_ids]
    new_fee = money(sum((o.fee for o in new_orders), Decimal("0")))
    objective = tuple(list(plan.objective[:3]) + [int(new_fee * 100)])
    return replace(plan, orders=new_orders, total_fee=new_fee,
                   objective=objective, solver="adoption-residual",
                   notes="部分采纳后的剩余付款重规划；已确认安排占款不参与候选")


def _impact_of(scenario: Scenario, confirmed: list[TransferOrder],
               candidate: TransferOrder, base: ReplayResult,
               base_fee: Decimal) -> dict:
    """采纳 candidate（在已确认集合之上）对剩余付款的影响。"""
    try:
        trial = solve(scenario, fixed_orders=confirmed + [candidate],
                      id_prefix="A", schedule_windows=True)
    except LedgerError as exc:
        return {
            "order_id": candidate.order_id,
            "route": candidate.source + "->" + candidate.target,
            "principal": str(candidate.principal),
            "fee": str(candidate.fee),
            "delayed_payments": [], "fee_delta": "0.00",
            "new_shortfalls": [], "lost_feasible": [],
            "error": f"采纳后计划不可行：{exc}",
        }
    base_pay = _payment_map(base)
    trial_pay = _payment_map(trial.replay)
    delayed, new_shortfalls, lost = [], [], []
    for fid, tp in sorted(trial_pay.items()):
        bp = base_pay.get(fid)
        if bp is None:
            continue
        if bp.shortfall == 0 and tp.shortfall == 0 and tp.date > bp.date:
            delayed.append({"flow_id": fid, "from": bp.date, "to": tp.date})
        if tp.shortfall > bp.shortfall:
            new_shortfalls.append({"flow_id": fid,
                                   "shortfall": str(money(tp.shortfall))})
            if bp.shortfall == 0:
                lost.append(fid)
    fee_delta = money(trial.replay.ledger.total_fees - base_fee)
    return {
        "order_id": candidate.order_id,
        "route": candidate.source + "->" + candidate.target,
        "principal": str(candidate.principal),
        "fee": str(candidate.fee),
        "delayed_payments": delayed,
        "fee_delta": str(fee_delta),
        "new_shortfalls": new_shortfalls,
        "lost_feasible": lost,
    }


def confirm_partial(scenario: Scenario, plan: Plan, order_ids: list[str],
                    prior: "Adoption | None" = None, label: str = "",
                    id_prefix: str = "A") -> AdoptionResult:
    """只采纳候选计划中的部分调拨安排，再为剩余付款重新规划。

    prior 为重开后载入的历史采纳记录：其确认安排继续占款；
    重复确认同一单号会被跳过（不重复占款）。
    """
    plan_orders = {o.order_id: o for o in plan.orders}
    unknown = [oid for oid in order_ids if oid not in plan_orders]
    if unknown:
        raise ValueError("候选计划中不存在这些调拨单号: " + str(unknown))
    prior_orders = confirmed_orders(prior)
    already = {o.order_id for o in prior_orders}
    skipped = [oid for oid in dict.fromkeys(order_ids) if oid in already]
    new_orders = [plan_orders[oid] for oid in dict.fromkeys(order_ids)
                  if oid not in already]
    confirmed = prior_orders + new_orders

    now = datetime.now().isoformat(timespec="seconds")
    try:
        solved = solve(scenario, fixed_orders=confirmed or None,
                       id_prefix=id_prefix)
    except LedgerError as exc:
        # 确认集合自身不可行（如只确认了调拨链的后半段）：明确告知，
        # 不产生剩余计划，由用户调整确认范围
        confirmed_entries = list(prior.confirmed) if prior else []
        confirmed_entries += [
            {"order": o.to_dict(), "status": "confirmed", "confirmed_at": now}
            for o in new_orders
        ]
        adoption = Adoption(
            adoption_id=(prior.adoption_id if prior else "ADP-" + now),
            scenario_id=scenario.scenario_id,
            plan_created_at=plan.created_at, created_at=now,
            label=label or (prior.label if prior else ""),
            confirmed=confirmed_entries,
            withdrawals=list(prior.withdrawals) if prior else [],
            status="infeasible")
        return AdoptionResult(adoption, None, None, [], skipped,
                              infeasible_reason=str(exc))
    residual_plan = _residual_plan_view(solved.plan, confirmed)

    impacts = []
    base_fee = solved.replay.ledger.total_fees
    confirmed_ids = {o.order_id for o in confirmed}
    for o in plan.orders:
        if o.order_id in confirmed_ids:
            continue
        impacts.append(_impact_of(scenario, confirmed, o, solved.replay,
                                  base_fee))

    confirmed_entries = list(prior.confirmed) if prior else []
    confirmed_entries += [
        {"order": o.to_dict(), "status": "confirmed", "confirmed_at": now}
        for o in new_orders
    ]
    adoption = Adoption(
        adoption_id=(prior.adoption_id if prior else "ADP-" + now),
        scenario_id=scenario.scenario_id,
        plan_created_at=plan.created_at,
        created_at=now,
        label=label or (prior.label if prior else ""),
        confirmed=confirmed_entries,
        withdrawals=list(prior.withdrawals) if prior else [],
        status="active",
    )
    return AdoptionResult(adoption, residual_plan, solved.replay, impacts,
                          skipped)


def compare_partials(scenario: Scenario, plan: Plan,
                     scopes: "list[tuple[str, list[str]]]"
                     ) -> list[AdoptionResult]:
    """两个（或多个）部分采纳方案的对照，各自独立从同一候选计划出发。"""
    return [confirm_partial(scenario, plan, ids, label=label)
            for label, ids in scopes]


# ---------------- 撤回 ----------------

@dataclass
class WithdrawalResult:
    adoption: Adoption           # 更新后的采纳记录
    replan_result: ReplanResult  # 撤回后剩余付款的重规划
    kept_in_transit: list[dict]  # 已发出、保持在途的安排及处理说明
    released: list[dict]         # 尚未执行、释放占款的安排
    fees_incurred: Decimal       # 已发生、不退回的手续费


def withdraw_adoption(scenario: Scenario, adoption: Adoption, plan: Plan,
                      execution: Execution, cutoff_date: str,
                      amendments: "list[FeeAmendment] | None" = None,
                      inflow_updates: "dict[str, tuple[str, Decimal]] | None" = None
                      ) -> WithdrawalResult:
    """撤回部分采纳：只影响尚未执行部分。

    已发出（initiated）的调拨保持在途：本金早已离开原账户，不再重扣，
    到账日照常入收款方；已发生手续费不退回。未发出的确认安排释放占款，
    剩余付款以 cutoff 日终真实状态为起点重新规划。
    """
    confirmed = confirmed_orders(adoption)
    dispatched = set(execution.initiated_order_ids)
    kept, released = [], []
    for o in confirmed:
        if o.order_id in dispatched:
            kept.append({
                "order_id": o.order_id,
                "route": o.source + "->" + o.target,
                "principal": str(o.principal),
                "handling": (f"本金 {o.principal} 已于 {o.initiate_date} 离开 "
                             f"{o.source}，在途期间双方均不可用；{o.arrival_date} "
                             f"到账照常入 {o.target}，不再从原账户重扣；"
                             f"手续费 {o.fee} 已发生，不退回"),
            })
        else:
            released.append({
                "order_id": o.order_id,
                "route": o.source + "->" + o.target,
                "principal": str(o.principal),
                "handling": "尚未发出，确认占款释放，该部分余额可重新安排",
            })
    fees_incurred = money(sum(
        (t.actual_fee for t in execution.actual_transfers
         if t.order_id in dispatched and t.status != "cancelled"
         and t.actual_initiate_date <= cutoff_date),
        Decimal("0")))

    rr = replan(scenario, plan, execution, cutoff_date,
                amendments=amendments or [],
                inflow_updates=inflow_updates or {})

    now = datetime.now().isoformat(timespec="seconds")
    kept_ids = {k["order_id"] for k in kept}
    new_confirmed = []
    for entry in adoption.confirmed:
        oid = entry["order"]["order_id"]
        if oid in kept_ids:
            new_confirmed.append({**entry, "status": "dispatched_kept"})
    withdrawal_record = {
        "withdrawn_at": now,
        "cutoff_date": cutoff_date,
        "kept_in_transit": [k["order_id"] for k in kept],
        "released": [r["order_id"] for r in released],
        "fees_incurred": str(fees_incurred),
        "note": "撤回只影响尚未执行部分；在途资金保持到账入账、不再重扣，"
                "已发生手续费保留在账上",
    }
    updated = Adoption(
        adoption_id=adoption.adoption_id,
        scenario_id=adoption.scenario_id,
        plan_created_at=adoption.plan_created_at,
        created_at=now,
        label=adoption.label,
        confirmed=new_confirmed,
        withdrawals=adoption.withdrawals + [withdrawal_record],
        status="partially_withdrawn" if kept else "withdrawn",
    )
    return WithdrawalResult(updated, rr, kept, released, fees_incurred)


# ---------------- 采纳后的整段重放（重开对账） ----------------

def adoption_combined_replay(scenario: Scenario, adoption: Adoption,
                             residual_plan: Plan,
                             execution: "Execution | None" = None
                             ) -> ReplayResult:
    """从期初一路重放：已确认安排 + 剩余付款计划 + 实际执行记录。

    - 实际收入（迟到/部分到账）覆盖场景中的原定收入；
    - 已发出的确认安排用实际执行腿（实际日期/费用），未发出的用计划腿；
    - 同一单号只入账一次：不重复占款，已发生费用不抹去。
    """
    actual_inflows = ({f.flow_id: f for f in execution.actual_inflows}
                      if execution else {})
    flows = []
    for f in scenario.flows:
        act = actual_inflows.get(f.flow_id)
        if f.kind == FlowKind.INFLOW and act is not None:
            if act.amount > 0:
                flows.append(CashFlow(f.flow_id, f.account_id, act.actual_date,
                                      f.kind, act.amount, f.priority, f.memo,
                                      f.window_start, f.window_end))
            continue
        flows.append(f)
    full = Scenario(
        scenario.scenario_id + "@adoption", scenario.start_date,
        scenario.end_date, list(scenario.accounts), flows,
        list(scenario.transfer_rules), scenario.description)

    actual_legs = ({t.order_id: t for t in execution.actual_transfers
                    if t.status != "cancelled"} if execution else {})
    legs: list[TransferLeg] = []
    seen: set[str] = set()
    for o in confirmed_orders(adoption):
        t = actual_legs.pop(o.order_id, None)
        if t is not None:
            legs.append(TransferLeg(o.order_id, t.source, t.target,
                                    t.actual_initiate_date,
                                    t.actual_arrival_date,
                                    money(t.actual_principal),
                                    money(t.actual_fee)))
        else:
            legs.append(TransferLeg(o.order_id, o.source, o.target,
                                    o.initiate_date, o.arrival_date,
                                    money(o.principal), money(o.fee)))
        seen.add(o.order_id)
    for oid, t in sorted(actual_legs.items()):   # 执行记录中额外的实际调拨
        legs.append(TransferLeg(oid, t.source, t.target,
                                t.actual_initiate_date, t.actual_arrival_date,
                                money(t.actual_principal), money(t.actual_fee)))
        seen.add(oid)
    for o in (residual_plan.orders if residual_plan else []):
        if o.order_id in seen:
            continue
        legs.append(TransferLeg(o.order_id, o.source, o.target,
                                o.initiate_date, o.arrival_date,
                                money(o.principal), money(o.fee)))
    return replay(full, legs=legs, snapshot_daily=True)
