"""文字报表：时序重放清单、每日各账户资金账、缺口位置。"""

from __future__ import annotations

from decimal import Decimal

from .engine import ReplayResult
from .plan import Plan
from .scenario import Scenario


def revision_summary(revision) -> str:
    lines = [
        f"修订计划 rev{revision.revision_no}（截断日 {revision.cutoff_date} 早晨）:",
        f"  已发起且不再重复占用 {len(revision.committed_orders)} 笔"
        f"（其中在途 {len(revision.in_transit_orders)} 笔）；"
        f"新增调拨 {len(revision.revised_orders)} 笔；总手续费 {revision.total_fee}",
        f"  目标元组 (P1缺口分,P2缺口分,P3缺口分,新增费用分) = "
        f"{tuple(revision.objective)}",
    ]
    for t in revision.committed_orders:
        mark = "在途" if t in revision.in_transit_orders else "已到账"
        lines.append(
            f"  [实际-{mark}] {t.order_id}: {t.actual_initiate_date} 发起 "
            f"{t.source}->{t.target} 本金 {t.actual_principal} 费 {t.actual_fee}"
            f"，{t.actual_arrival_date} 到账")
    for o in revision.revised_orders:
        lines.append(
            f"  [新增] {o.order_id}: {o.initiate_date} 发起 {o.source}->{o.target} "
            f"本金 {o.principal} 费 {o.fee}，{o.arrival_date} 到账")
    for d in revision.deferrals:
        lines.append(
            f"  [推迟] {d['flow_id']}(P3) {d['original_date']} -> "
            f"{d['deferred_to']}：{d['reason']}")
    for c in revision.choices:
        lines.append(f"  [选择理由] {c['order_id']} {c['source']}->{c['target']} "
                     f"本金 {c['principal']} 费 {c['fee']}：{c['reason']}")
    if not revision.remaining_gaps:
        lines.append("  未满足部分：无")
    else:
        lines.append("  未满足部分:")
        for g in revision.remaining_gaps:
            lines.append(
                f"    {g['date']} {g['flow_id']} 账户 {g['account_id']} "
                f"P{g['priority']} 应付 {g['due']} 已付 {g['paid']} "
                f"缺口 {g['shortfall']}")
    return "\n".join(lines)


def event_log(result: ReplayResult) -> str:
    lines = ["按时间可重放的事件清单（过账顺序：收入 -> 调拨发起 -> 调拨到账 -> 支出）:"]
    for e in result.ledger.log:
        if e["event"] == "inflow":
            lines.append(f"  {e['date']} 收入到账  {e['flow_id']:<8} "
                         f"账户 {e['account']:<6} +{e['amount']}")
        elif e["event"] == "transfer_initiate":
            lines.append(f"  {e['date']} 调拨发起  {e['order_id']:<6} "
                         f"{e['source']}->{e['target']} 本金 {e['principal']} "
                         f"手续费 {e['fee']}（资金进入在途）")
        elif e["event"] == "transfer_arrive":
            lines.append(f"  {e['date']} 调拨到账  {e['order_id']:<6} "
                         f"入账户 {e['target']:<6} +{e['principal']}")
        else:
            mark = "" if e["shortfall"] == "0.00" else f" 缺口 {e['shortfall']}"
            lines.append(f"  {e['date']} 支出支付  {e['flow_id']:<8} "
                         f"账户 {e['account']:<6} P{e['priority']} "
                         f"应付 {e['due']} 实付 {e['paid']}{mark}")
    return "\n".join(lines)


def daily_ledger(result: ReplayResult) -> str:
    lines = ["每日各账户资金账（可用余额 / 在途）:"]
    for snap in result.ledger.snapshots:
        bal = "  ".join(f"{k}:{v}" for k, v in snap.balances.items())
        transit = "  ".join(f"{k}@{v}" for k, v in snap.in_transit.items()) or "无"
        lines.append(f"  {snap.date} | {bal} | 在途: {transit}")
    return "\n".join(lines)


def gap_report(plan: Plan) -> str:
    if not plan.remaining_gaps:
        return "仍有缺口的位置：无（所有支出均已覆盖）"
    lines = ["仍有缺口的位置:"]
    for g in plan.remaining_gaps:
        lines.append(
            f"  {g['date']} 支出 {g['flow_id']:<8} 账户 {g['account_id']:<6} "
            f"P{g['priority']} 应付 {g['due']} 已付 {g['paid']} "
            f"缺口 {g['shortfall']}")
    return "\n".join(lines)


def plan_summary(plan: Plan) -> str:
    lines = [
        f"计划（求解器 {plan.solver}）: {len(plan.orders)} 笔调拨, "
        f"总手续费 {plan.total_fee}",
        f"  目标元组 (P1缺口分, P2缺口分, P3缺口分, 费用分) = {tuple(plan.objective)}",
    ]
    for o in plan.orders:
        lines.append(
            f"  {o.order_id}: {o.initiate_date} 发起 {o.source}->{o.target} "
            f"本金 {o.principal} 费 {o.fee}，{o.arrival_date} 到账"
            + (f"（用于 {o.purpose}）" if o.purpose else ""))
    return "\n".join(lines)


def reconciliation(result: ReplayResult, scenario: Scenario) -> str:
    led = result.ledger
    opening = sum((a.opening_balance for a in scenario.accounts), Decimal("0"))
    balance = sum(led.balances.values(), Decimal("0"))
    transit = sum((leg.principal for leg in led.in_transit.values()), Decimal("0"))
    lines = [
        "各账户资金账核对:",
        f"  期初合计 {opening} + 收入 {led.total_inflows} - 已付支出 {led.total_paid} "
        f"= {opening + led.total_inflows - led.total_paid}",
        f"  期末余额 {balance} + 在途 {transit} + 手续费 {led.total_fees} "
        f"= {balance + transit + led.total_fees}",
    ]
    for a in scenario.accounts:
        lines.append(f"  账户 {a.account_id}({a.name}): 期初 {a.opening_balance} "
                     f"-> 期末 {led.available(a.account_id)}")
    led.verify_conservation()
    lines.append("  核对结果：资金守恒，全部账户余额非负。")
    return "\n".join(lines)
