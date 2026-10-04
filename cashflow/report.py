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
        elif e["event"] == "reject_notice":
            lines.append(f"  {e['date']} 拒绝通知  {e['rejection_id']:<6} "
                         f"调拨 {e['order_id']:<6} 拒绝 {e['amount']} "
                         f"退回 {e['return_account']}（费用 {e['return_fee']}，"
                         f"剩余在途 {e['remaining_principal']}，通知日不可用）")
        elif e["event"] == "reject_return_arrive":
            lines.append(f"  {e['date']} 退回到账  {e['rejection_id']:<6} "
                         f"入账户 {e['account']:<6} +{e['amount']}")
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
    if plan.partial_adoption_options:
        lines.append("  部分采纳方案对照:")
        for o in plan.partial_adoption_options:
            scope = o.get("confirmed_scope", {})
            delayed = ", ".join(f"{k}->{v}" for k, v in
                                 o.get("delayed_remaining_payments", {}).items())
            lines.append(
                f"    {o.get('name')}: 确认订单 {scope.get('order_ids', [])} "
                f"确认费 {scope.get('fee', '0.00')}；剩余费用 {o.get('remaining_fee')} "
                f"总费用 {o.get('total_expected_fee')} 溢价 {o.get('fee_premium', '0.00')} "
                f"剩余缺口 {o.get('remaining_gap')} 推迟 {delayed or '无'}")
    if plan.locked_orders:
        lines.append("  已确认未发起（继续占用账户余额/调拨能力）:")
        for o in plan.locked_orders:
            lines.append(f"    {o.order_id}: {o.initiate_date} {o.source}->"
                         f"{o.target} 本金 {o.principal} 费 {o.fee}")
    withdrawal = plan.schedule_basis.get("withdrawal_notes")
    if withdrawal:
        lines.append("  撤回处理:")
        for note in withdrawal:
            lines.append(f"    {note['order_id']} [{note['status']}] {note['handling']}")
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


def refund_report(plan: Plan) -> str:
    """退回资金核对：区分原付款、已退回可用、预计退回（不可用）与手续费。"""
    if not plan.refund_records:
        return "退回资金核对：无退回/拒付记录"
    lines = ["退回资金核对（预计退回不作为可用资金）:"]
    for r in plan.refund_records:
        kind = "拒付" if r["kind"] == "chargeback" else "退回"
        if r["status"] == "expected_not_usable":
            lines.append(
                f"    {r['refund_id']} [{kind}] 原付款 {r['original_flow_id']} "
                f"预计 {r['expected_date']} 退回 {r['amount']} 至 "
                f"{r['return_account']}（未到达，不可用）")
        else:
            tag = "假设到账" if r["status"] == "assumed" else "已到账可用"
            fee = (f"，手续费退回 {r['fee_refunded']}"
                   if r["fee_refunded"] != "0.00" else "，手续费不退")
            lines.append(
                f"    {r['refund_id']} [{kind}] 原付款 {r['original_flow_id']} "
                f"{r['actual_date']} 退回 {r['amount']} 至 "
                f"{r['return_account']}（{tag}{fee}）")
    basis = plan.schedule_basis.get("refund_basis")
    if basis:
        lines.append("  本次重规划采用的实际到账依据:")
        for b in basis:
            lines.append(f"    {b['refund_id']} {b['date']} "
                         f"+{b['amount']} -> {b['return_account']} [{b['status']}]")
    return "\n".join(lines)


def rejection_report(disposition: dict) -> str:
    """在途部分拒绝核对：区分通知、退回在途、已退回可用与收款到账。"""
    if not disposition["orders"]:
        return "在途拒绝核对：无拒绝记录"
    lines = [f"在途拒绝核对（截至 {disposition['cutoff_date']}，"
             "通知与预计日期本身不产生可用资金）:"]
    for o in disposition["orders"]:
        lines.append(
            f"  调拨 {o['order_id']} 本金 {o['principal']} -> "
            f"收款方将收到 {o['payee_receives']}（已退回部分不会二次入账）")
        for e in o["rejections"]:
            tag = {"cancelled": "已撤销（留在原划拨）",
                   "arrived": "已退回可用",
                   "return_in_transit": "退回在途（已确认实际日期），不可用",
                   "expected_unconfirmed": "预计退回（预测，未确认），不可用",
                   }[e["status"]]
            fee = (f"，退回费用 {e['return_fee']} 不退"
                   if e["return_fee"] != "0.00" else "")
            actual = e["actual_return_date"] or "未确认"
            lines.append(
                f"    {e['rejection_id']} 拒绝 {e['amount']} -> "
                f"{e['return_account']} 预计 {e['expected_return_date']} "
                f"实际 {actual} [{tag}]{fee}")
        lines.append(
            f"    小计：已退回可用 {o['returned_arrived_usable']}，"
            f"已确认退回在途 {o['return_in_transit_not_usable']}，"
            f"预计退回（预测）{o['expected_return_forecast_not_usable']}，"
            f"退回费用 {o['return_fees']}")
    forecast = disposition.get("expected_returns_forecast") or []
    if forecast:
        lines.append("  预计退回一览（仅为预测，未确认实际到账，"
                     "预计日期经过也不增加可用余额）:")
        for f in forecast:
            lines.append(
                f"    {f['rejection_id']} 预计 {f['expected_return_date']} "
                f"退回 {f['amount']} -> {f['return_account']} [预测，不可用]")
    return "\n".join(lines)


def refund_strategy_report(options: list[dict]) -> str:
    """继续等待退回 vs 替代账户安排 的对照报表。"""
    lines = ["剩余支付重规划对照（等待退回 vs 替代账户安排）:"]
    for o in options:
        assumed = ", ".join(f"{a['refund_id']}@{a['assumed_date']}"
                            for a in o["assumed_refunds"]) or "无"
        delayed = ", ".join(f"{k}->{v}" for k, v in
                            o["delayed_payments"].items()) or "无"
        lines.append(
            f"  {o['name']}: 剩余缺口 {o['remaining_gap']} "
            f"总费用 {o['total_fee']} 费用溢价 {o['fee_premium']} "
            f"新增调拨 {len(o['new_orders'])} 笔 推迟 {delayed} "
            f"假设退回 {assumed}")
    return "\n".join(lines)
