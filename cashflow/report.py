"""文字报表：时序重放清单、每日各账户资金账、缺口位置。"""

from __future__ import annotations

from decimal import Decimal

from .engine import ReplayResult
from .models import money
from .plan import Plan
from .scenario import Scenario


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


_STATUS_CN = {"on_time": "准时支付", "deferred": "窗口内改期支付",
              "late_paid": "延误后补付", "true_shortfall": "真正不足",
              "unmet": "未满足",
              "strategy_not_adopted": "当前策略未采用可行改期",
              "search_incomplete": "尚未找到方案"}


def payment_strategy_report(plan: Plan) -> str:
    if not plan.payment_strategy_options:
        return "支付优先级方案对照：无支付窗口"
    lines = ["支付优先级方案对照:"]
    for o in plan.payment_strategy_options:
        moved = ", ".join(f"{fid}:{day}" for fid, day in o["rescheduled_date"].items())
        lines.append(
            f"  {o['strategy']} #{o['rank']} [{_STATUS_CN.get(o['status'], o['status'])}] "
            f"改期 {moved or '无'} 费用 {o['total_fee']} 最终缺口 {o['final_gap']}")
    if plan.feasibility_status:
        lines.append(f"  结论：{_STATUS_CN.get(plan.feasibility_status, plan.feasibility_status)}")
    return "\n".join(lines)


def revision_summary(plan: Plan) -> str:
    """修订计划摘要：修订关系、保持在途、费率调整、逐笔支付决策与理由。"""
    lines = [
        f"修订计划（修订自 {plan.revision_of}，cutoff={plan.cutoff_date}）:",
        f"  新增/保留调拨 {len(plan.orders)} 笔，总手续费 {plan.total_fee}，"
        f"目标元组 {tuple(plan.objective)}",
    ]
    if plan.payment_strategy_options:
        lines.append(payment_strategy_report(plan))
    if plan.fixed_transit:
        lines.append("  已发起保持在途（不再从原账户重扣）:")
        for o in plan.fixed_transit:
            lines.append(f"    {o.order_id}: {o.source}->{o.target} "
                         f"本金 {o.principal}，{o.arrival_date} 到账")
    if plan.rule_amendments:
        lines.append("  费率调整（对 cutoff 后新发起调拨生效）:")
        for a in plan.rule_amendments:
            rate = a["fee_rate"] if a["fee_rate"] is not None else "不变"
            fixed = a["fee_fixed"] if a["fee_fixed"] is not None else "不变"
            lines.append(f"    {a['source']}->{a['target']} 自 {a['effective_date']} "
                         f"比例费率 {rate}，固定费 {fixed}")
    lines.append("  后续确认收入:")
    for f in plan.expected_inflows:
        lines.append(f"    {f['expected_date']} {f['flow_id']} "
                     f"账户 {f['account_id']} +{f['amount']}")
    lines.append("  逐笔支付决策:")
    for d in plan.payment_decisions:
        lines.append(
            f"    [{_STATUS_CN.get(d['status'], d['status'])}] "
            f"{d['scheduled_date']} {d['flow_id']} P{d['priority']} "
            f"应付 {d['due']} 实付 {d['paid']} 缺口 {d['shortfall']}")
        lines.append(f"      理由：{d['reason']}")
    return "\n".join(lines)


def adoption_summary(result) -> str:
    """部分采纳摘要：确认范围、占款、剩余付款重规划与候选影响。"""
    from .adopt import confirmed_orders
    adoption = result.adoption
    orders = confirmed_orders(adoption)
    principal = sum((o.principal for o in orders), Decimal("0"))
    fees = sum((o.fee for o in orders), Decimal("0"))
    lines = [
        f"部分采纳（{adoption.label or adoption.adoption_id}，"
        f"来源计划 {adoption.plan_created_at}）:",
        f"  确认范围：{len(orders)} 笔调拨，占用本金 {money(principal)} "
        f"+ 手续费 {money(fees)}（其他方案不能再花费这部分余额）",
    ]
    for o in orders:
        lines.append(f"    {o.order_id}: {o.initiate_date} {o.source}->{o.target} "
                     f"本金 {o.principal} 费 {o.fee}，{o.arrival_date} 到账")
    if result.skipped:
        lines.append(f"  已确认过、未重复占款：{', '.join(result.skipped)}")
    if result.residual_plan is None:
        lines.append(f"  确认范围不可行：{result.infeasible_reason}")
        lines.append("  （如只确认了调拨链的后半段；请调整确认范围后重试）")
        return "\n".join(lines)
    plan = result.residual_plan
    lines.append(f"  剩余付款重规划：{len(plan.orders)} 笔新调拨，"
                 f"新增手续费 {plan.total_fee}")
    for o in plan.orders:
        lines.append(f"    {o.order_id}: {o.initiate_date} {o.source}->{o.target} "
                     f"本金 {o.principal} 费 {o.fee}，{o.arrival_date} 到账")
    if plan.remaining_gaps:
        lines.append("  剩余缺口:")
        for g in plan.remaining_gaps:
            lines.append(f"    {g['date']} {g['flow_id']} P{g['priority']} "
                         f"缺口 {g['shortfall']}")
    else:
        lines.append("  剩余缺口：无")
    if result.impacts:
        lines.append("  尚未确认安排的影响（若采纳）:")
        for im in result.impacts:
            if im.get("error"):
                lines.append(f"    {im['order_id']} ({im['route']} "
                             f"本金 {im['principal']}): {im['error']}")
                continue
            parts = []
            if im["delayed_payments"]:
                parts.append("使付款推迟 " + ", ".join(
                    f"{d['flow_id']} {d['from']}->{d['to']}"
                    for d in im["delayed_payments"]))
            if Decimal(im["fee_delta"]) != 0:
                parts.append(f"费用变化 {im['fee_delta']}")
            if im["lost_feasible"]:
                parts.append("失去可行安排 " + ", ".join(im["lost_feasible"]))
            if not parts:
                parts.append("不影响其余付款的期限、费用与可行性")
            lines.append(f"    {im['order_id']} ({im['route']} "
                         f"本金 {im['principal']}): " + "；".join(parts))
    return "\n".join(lines)


def adoption_compare_report(results) -> str:
    """两个部分采纳方案的对照：保留原约束、确认范围、剩余缺口。"""
    lines = ["部分采纳方案对照（原始期限约束均保留，互不影响）:"]
    for res in results:
        plan = res.residual_plan
        originals = plan.schedule_basis.get("original_due_date", {})
        kept = ", ".join(f"{k}:{v}" for k, v in sorted(originals.items())) or "按原计划期限"
        gap_total = sum((Decimal(g["shortfall"]) for g in plan.remaining_gaps),
                        Decimal("0"))
        scope = ", ".join(e["order"]["order_id"] for e in res.adoption.confirmed)
        lines.append(
            f"  方案[{res.adoption.label}] 确认范围: {scope or '无'} | "
            f"保留原约束: {kept} | 新增费用 {plan.total_fee} | "
            f"剩余缺口合计 {money(gap_total)}")
        for g in plan.remaining_gaps:
            lines.append(f"    缺口 {g['date']} {g['flow_id']} "
                         f"P{g['priority']} {g['shortfall']}")
    return "\n".join(lines)


def withdrawal_summary(result) -> str:
    """撤回摘要：在途保持、释放占款、已发生费用。"""
    lines = [
        f"撤回部分采纳（{result.adoption.adoption_id}，状态 "
        f"{result.adoption.status}）:",
        f"  已发生手续费 {result.fees_incurred} 保留在账上，不退回",
    ]
    if result.kept_in_transit:
        lines.append("  已发出、保持在途（撤回不影响）:")
        for k in result.kept_in_transit:
            lines.append(f"    {k['order_id']} ({k['route']} "
                         f"本金 {k['principal']}): {k['handling']}")
    if result.released:
        lines.append("  尚未执行、释放占款:")
        for r in result.released:
            lines.append(f"    {r['order_id']} ({r['route']} "
                         f"本金 {r['principal']}): {r['handling']}")
    plan = result.replan_result.plan
    lines.append(f"  撤回后剩余付款重规划：{len(plan.orders)} 笔新调拨，"
                 f"新增手续费 {plan.total_fee}，缺口 "
                 f"{len(plan.remaining_gaps)} 处")
    return "\n".join(lines)
