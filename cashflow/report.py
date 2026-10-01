"""文字报表：时序重放清单、每日各账户资金账、缺口位置。"""

from __future__ import annotations

from decimal import Decimal

from .engine import ReplayResult
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


_STATUS_CN = {"on_time": "准时支付", "deferred": "推迟可选支付",
              "late_paid": "延误后补付", "unmet": "未满足",
              "rescheduled": "窗口内改期足额支付",
              "truly_unmet": "真正不足（窗口内无法足额）",
              "strategy_not_used": "当前策略未采用可行安排",
              "no_solution_found": "尚未找到方案（搜索截断）"}


def strategy_comparison(outcomes) -> str:
    """不同支付优先级方案对照表：改期、费用、最终缺口、三分类。"""
    lines = ["支付优先级方案对照（同一输入；原始期限保留不改写）:",
             "  策略                总费用   P1缺口 P2缺口 P3缺口 改期笔数 分类(逐笔缺口)"]
    for o in outcomes:
        by_pri = o.replay.gap_total_by_priority()
        moved = sum(1 for d in o.decisions
                    if d["status"] == "rescheduled")
        verdict = ",".join(f"{fid}:{v}"
                            for fid, v in o.feasibility.items())
        lines.append(
            f"  {o.strategy:<18} {o.total_fee!s:<8} "
            f"{by_pri[1]!s:<6} {by_pri[2]!s:<6} {by_pri[3]!s:<7} "
            f"{moved:<8} {verdict or '无缺口'}")
    lines.append("")
    for o in outcomes:
        lines.append(f"  [{o.strategy}] 逐笔支付安排:")
        for d in o.decisions:
            cn = _STATUS_CN.get(d["status"], d["status"])
            lines.append(
                f"    {d['flow_id']} P{d['priority']} 原期限 {d['original_due_date']}"
                f" 窗口至 {d['window_end']} -> {d['scheduled_date']} 付 {d['paid']}"
                f" 缺口 {d['shortfall']} [{cn}]")
    return "\n".join(lines)


def revision_summary(plan: Plan) -> str:
    """修订计划摘要：修订关系、保持在途、费率调整、逐笔支付决策与理由。"""
    lines = [
        f"修订计划（修订自 {plan.revision_of}，cutoff={plan.cutoff_date}）:",
        f"  新增/保留调拨 {len(plan.orders)} 笔，总手续费 {plan.total_fee}，"
        f"目标元组 {tuple(plan.objective)}",
    ]
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
