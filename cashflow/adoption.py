"""计划采纳与新旧版本差异复演（第三轮）。

用户采纳一种修订后：
  - 旧计划文件及其 basis 依据快照原样保留，不做任何改写；
  - 仅新增一条 Adoption 记录，登记“采纳了谁、替代了谁”；
  - 重开后用 replay_version 分别复演旧版与采纳版，再用 diff_versions
    对出逐日余额、逐笔支付日/实付、费用与最终缺口的差异。

两份计划在同一套真实执行前缀（Execution）与在途资金上重放：
FIXED 单只在到账日入账，已发起款项不会被扣第二次。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .engine import add_days, daterange, replay
from .ledger import TransferLeg
from .models import CashFlow, FlowKind, money
from .plan import Adoption, Execution, Plan
from .replan import combined_replay, _actual_map  # noqa: PLC2701
from .scenario import Scenario
from .scheduling import shifted_scenario


def adopt(adopted: Plan, superseded: Plan | None = None,
          note: str = "", adopted_at: str | None = None) -> Adoption:
    """生成采纳记录。该函数不修改任何旧计划文件。"""
    from datetime import datetime
    return Adoption(
        scenario_id=adopted.scenario_id,
        adopted_created_at=adopted.created_at,
        superseded_created_at="" if superseded is None else superseded.created_at,
        adopted_at=adopted_at or datetime.now().isoformat(timespec="seconds"),
        note=note,
    )


def _inflow_updates_from_plan(plan: Plan) -> dict[str, tuple[str, Decimal]]:
    basis = plan.basis or {}
    return {k: (v[0], money(v[1]))
            for k, v in (basis.get("inflow_updates") or {}).items()}


def replay_version(scenario: Scenario, execution: Execution | None,
                   plan: Plan):
    """复演某一版计划的全期资金账（期初..期末），返回 ReplayResult。

    首版计划（revision_of 为空、无执行记录）直接按订单重放；
    修订计划走 实际前缀 + 残差新单 的合并重放，并套用其支付排期。
    """
    if not plan.revision_of:
        if plan.payment_schedule:
            sched_sc = shifted_scenario(
                scenario, dict(plan.payment_schedule),
                f"@version-{plan.created_at}")
            return replay(sched_sc, plan.orders, snapshot_daily=True)
        return replay(scenario, plan.orders, snapshot_daily=True)

    assert execution is not None, "修订计划必须提供执行记录才能合并重放"
    inflow_updates = _inflow_updates_from_plan(plan)
    if not plan.payment_schedule:
        return combined_replay(scenario, execution, plan,
                               inflow_updates=inflow_updates)

    # 合并重放里支出仍用原期限；这里按采纳版排期把支出改到安排日再重放。
    sched = dict(plan.payment_schedule)
    base_flows = _combined_flows(scenario, execution, plan, inflow_updates)
    full = Scenario(
        scenario.scenario_id + f"@version-{plan.created_at}",
        scenario.start_date, scenario.end_date,
        list(scenario.accounts), base_flows,
        list(scenario.transfer_rules), scenario.description)
    full = shifted_scenario(full, sched, "@scheduled")
    legs = _combined_legs(scenario, execution, plan)
    fixed_ids = {f"FIXED-{o.order_id.split('-', 1)[1]}"
                 for o in plan.fixed_transit}
    return replay(full, legs=legs, snapshot_daily=True,
                  preloaded_ids=fixed_ids)


def _combined_flows(scenario: Scenario, execution: Execution, plan: Plan,
                    inflow_updates: dict[str, tuple[str, Decimal]]):
    actuals = _actual_map(execution)
    cutoff = plan.cutoff_date
    flows = []
    for f in scenario.flows:
        if f.kind == FlowKind.INFLOW:
            act = actuals.get(f.flow_id)
            if f.date <= cutoff:
                if act is None or act.amount == 0:
                    continue
                flows.append(CashFlow(f.flow_id, f.account_id, act.actual_date,
                                      f.kind, act.amount, f.priority, f.memo))
            elif f.flow_id in inflow_updates:
                nd, amt = inflow_updates[f.flow_id]
                if amt > 0:
                    flows.append(CashFlow(f.flow_id, f.account_id, nd,
                                          f.kind, amt, f.priority, f.memo))
            else:
                flows.append(f)
        else:
            flows.append(f)
    return flows


def _combined_legs(scenario: Scenario, execution: Execution, plan: Plan):
    legs: list[TransferLeg] = []
    for t in execution.actual_transfers:
        if t.status == "cancelled":
            continue
        legs.append(TransferLeg(
            t.order_id, t.source, t.target,
            t.actual_initiate_date, t.actual_arrival_date,
            money(t.actual_principal), money(t.actual_fee)))
    for o in plan.orders:
        legs.append(TransferLeg(
            o.order_id, o.source, o.target, o.initiate_date, o.arrival_date,
            o.principal, o.fee))
    return legs


@dataclass
class VersionDiff:
    old_created_at: str
    new_created_at: str
    payment_changes: list[dict] = field(default_factory=list)
    daily_balance_deltas: list[dict] = field(default_factory=list)
    fee_old: Decimal = Decimal("0")
    fee_new: Decimal = Decimal("0")
    gap_old: dict[int, Decimal] = field(default_factory=dict)
    gap_new: dict[int, Decimal] = field(default_factory=dict)

    def summary(self) -> str:
        lines = [
            f"计划差异：{self.old_created_at} -> {self.new_created_at}",
            f"  新增手续费：{self.old_created_at} 为 {self.fee_old}，"
            f"{self.new_created_at} 为 {self.fee_new}，差额 "
            f"{money(self.fee_new - self.fee_old)}",
            f"  最终缺口：旧 { {k: str(v) for k, v in sorted(self.gap_old.items())} }"
            f" -> 新 { {k: str(v) for k, v in sorted(self.gap_new.items())} }",
            "  逐笔支付变化:",
        ]
        if not self.payment_changes:
            lines.append("    （无）")
        for ch in self.payment_changes:
            lines.append(
                f"    {ch['flow_id']}: {ch['old_date']} 付 {ch['old_paid']} "
                f"缺口 {ch['old_shortfall']} -> {ch['new_date']} 付 "
                f"{ch['new_paid']} 缺口 {ch['new_shortfall']}"
                + (f"（{ch['note']}）" if ch["note"] else ""))
        lines.append("  逐日余额合计有变化的日期:")
        changes = [d for d in self.daily_balance_deltas if d["delta_total"] != 0]
        if not changes:
            lines.append("    （逐日余额合计完全一致）")
        for d in changes:
            lines.append(
                f"    {d['date']}: 旧合计 {d['old_total']} -> "
                f"新合计 {d['new_total']}（差 {d['delta_total']}）")
        return "\n".join(lines)


def diff_versions(scenario: Scenario, old_plan: Plan, new_plan: Plan,
                  execution: Execution | None = None) -> VersionDiff:
    """分别复演两版计划，对出支付安排、逐日余额、费用与缺口差异。"""
    old_r = replay_version(scenario, execution, old_plan)
    new_r = replay_version(scenario, execution, new_plan)

    old_pay = {p.flow_id: p for p in old_r.ledger.payments}
    new_pay = {p.flow_id: p for p in new_r.ledger.payments}
    changes = []
    for fid in sorted(set(old_pay) | set(new_pay)):
        o, n = old_pay.get(fid), new_pay.get(fid)
        od = (o.date, o.paid, o.shortfall) if o else ("-", Decimal("0"), None)
        nd = (n.date, n.paid, n.shortfall) if n else ("-", Decimal("0"), None)
        note = ""
        if o and n and n.date != o.date:
            note = f"在支付窗口内由 {o.date} 改期至 {n.date}"
        if od != nd:
            changes.append({
                "flow_id": fid,
                "old_date": od[0], "old_paid": str(money(od[1])),
                "old_shortfall": "-" if od[2] is None else str(money(od[2])),
                "new_date": nd[0], "new_paid": str(money(nd[1])),
                "new_shortfall": "-" if nd[2] is None else str(money(nd[2])),
                "note": note,
            })

    old_snaps = {s.date: s for s in old_r.ledger.snapshots}
    new_snaps = {s.date: s for s in new_r.ledger.snapshots}
    daily = []
    for day in daterange(scenario.start_date, scenario.end_date):
        os_ = old_snaps.get(day)
        ns = new_snaps.get(day)
        ot = sum(os_.balances.values(), Decimal("0")) if os_ else Decimal("0")
        nt = sum(ns.balances.values(), Decimal("0")) if ns else Decimal("0")
        daily.append({
            "date": day, "old_total": money(ot), "new_total": money(nt),
            "delta_total": money(nt - ot),
            "old_balances": os_.balances if os_ else {},
            "new_balances": ns.balances if ns else {},
        })

    def gap_totals(result):
        return result.gap_total_by_priority()

    return VersionDiff(
        old_created_at=old_plan.created_at,
        new_created_at=new_plan.created_at,
        payment_changes=changes,
        daily_balance_deltas=daily,
        fee_old=money(sum((o.fee for o in old_plan.orders), Decimal("0"))),
        fee_new=money(sum((o.fee for o in new_plan.orders), Decimal("0"))),
        gap_old=gap_totals(old_r), gap_new=gap_totals(new_r))
