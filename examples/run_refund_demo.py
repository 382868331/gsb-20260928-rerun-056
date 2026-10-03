"""第四轮示例：退回与拒付后的剩余支付重规划。

场景：A 基本户 500、B 业务户 0；A->B 隔日到账、费率 2%。
10-03 发工资 400（P1），10-06 可选采购 300（P3）。

实际执行：
- 调拨 T 如期发起（400 + 手续费 8），10-03 到账 B 并发出工资；
- 收款方随后只退回一部分：RF1 退 150（10-04 到 A，跨账户返回，手续费不退），
  另有一笔 RF2 重复通知两次并更正一次金额（10-05 到 B）；
- 另有一笔预计退回 RF3（10-09 才到 A），cutoff 时不能当作可用资金。

演示：预计退回 vs 已到账、部分使用已到账退回、等待 vs 替代账户对照、
重开后资金守恒与原支付去向核对。
"""

from decimal import Decimal

from cashflow import (
    Account, CashFlow, FlowKind, Priority, Scenario, TransferRule,
    Execution, money, solve,
)
from cashflow.plan import ActualTransfer, RefundRecord
from cashflow.refunds import (
    allocate_arrived_refund, compare_refund_usage, refund_view,
)
from cashflow.replan import combined_replay, replan
from cashflow.report import daily_ledger, event_log, reconciliation


def main() -> None:
    sc = Scenario(
        "refund-demo", "2026-10-01", "2026-10-12",
        [Account("A", "基本户", money("500")), Account("B", "业务户")],
        flows=[
            CashFlow("W1", "B", "2026-10-03", FlowKind.OUTFLOW,
                     money("400"), Priority.P1_CRITICAL, "工资"),
            CashFlow("X1", "B", "2026-10-06", FlowKind.OUTFLOW,
                     money("300"), Priority.P3_OPTIONAL, "可选采购"),
        ],
        transfer_rules=[
            TransferRule("A", "B", lag_days=1, fee_rate=Decimal("0.02"))],
        description="退回与拒付后的剩余支付重规划")
    plan = solve(sc).plan
    order = plan.orders[0]
    execution = Execution(
        "refund-demo", plan.created_at, "2026-10-04T09:00:00",
        actual_transfers=[ActualTransfer(
            order.order_id, "A", "B", order.initiate_date,
            order.arrival_date, order.principal, order.fee)],
        initiated_order_ids=[order.order_id],
        refunds=[
            RefundRecord("RF1", "payment", "W1", "refund",
                         money("150"), "2026-10-04", "A",
                         memo="收款方部分退回，手续费 8 不退"),
            RefundRecord("RF2", "payment", "W1", "refund",
                         money("40"), "2026-10-05", "B"),
            RefundRecord("RF2", "payment", "W1", "refund",
                         money("40"), "2026-10-05", "B", notice="duplicate"),
            RefundRecord("RF2", "payment", "W1", "refund",
                         money("60"), "2026-10-05", "B", notice="correction"),
            RefundRecord("RF3", "payment", "W1", "refund",
                         money("200"), "2026-10-09", "A",
                         memo="预计退回，到账前不可用"),
        ])

    cutoff = "2026-10-04"
    rr = replan(sc, plan, execution, cutoff)
    print("== 修订计划（cutoff", cutoff, "）==")
    print("已到账退回依据:", [b["refund_id"] for b in rr.plan.refund_basis])
    print("预计退回（不可用）:",
          [(r["refund_id"], r["arrival_date"]) for r in rr.plan.expected_refunds])
    print("剩余缺口:", [(g["flow_id"], g["shortfall"])
                       for g in rr.plan.remaining_gaps])

    # 已到账退回只使用一部分，用于尚未确认的可选采购
    adopted = allocate_arrived_refund(
        rr.plan, execution, cutoff, "RF1", [("X1", money("120"))])
    print("\n== 退回余额部分使用 ==")
    for a in adopted.refund_allocations:
        print(f"  {a['refund_id']} -> {a['flow_id']}: {a['amount']}（{a['note']}）")

    print("\n== 继续等待退回 vs 替代账户安排 ==")
    for opt in compare_refund_usage(sc, execution, cutoff):
        print(f"  [{opt['name']}] 剩余缺口 {opt['remaining_gap']} "
              f"费用 {opt['total_fee']} 顺延 {opt.get('delayed_payments')}")

    print("\n== 退回视图（按原付款汇总）==")
    view = refund_view(execution, cutoff)
    for ref_id, slot in view["by_payment"].items():
        print(f"  原付款 {ref_id}: 已退 {slot['refunded_arrived']} "
              f"预计 {slot['expected']} 入账账户 {slot['credit_accounts']}")
    for note in view["normalization_notes"]:
        print(f"  去重: {note['refund_id']} {note['reason']}")

    print("\n== 重放（期初 -> 期末，单本账）==")
    full = combined_replay(sc, execution, rr.plan)
    print(event_log(full))
    print()
    print(daily_ledger(full))
    print()
    print(reconciliation(full, sc))


if __name__ == "__main__":
    main()
