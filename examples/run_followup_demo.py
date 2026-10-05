"""第六轮示例：依赖退回资金的后续付款方案研究。

场景：S1 货款 500 已付（A->B 调拨已发起），之后分两笔退回——
RF1 实际 10-06 到 B（200），RF2 预计 10-07 到 C（250，尚未到达）。
后续付款 S2（P1 工资 180，10-08）、S3（P2 货款 250，窗口 10-09..10-12）、
S4（P3 可选采购 100，10-10）都盯着退回资金。

演示：
1. 仅实际到账的付款执行表：S2 可执行，S3/S4 争用同一笔退回现金需等待；
2. 多组预计退回日期/费用假设对照（假设不进入可用余额）；
3. 调整未发出付款：S3 提为 P1、取消 S4；
4. RF2 实际迟到（10-09）且部分（150 < 250）：只重算受影响的 S3，
   原方案保留供核对；
5. 全部结果落盘重开，combined_replay 资金守恒。
"""

from decimal import Decimal
from pathlib import Path
import tempfile

from cashflow import (
    Account, CashFlow, FlowKind, Priority, Scenario, TransferRule,
    Execution, RefundRecord, money, solve,
)
from cashflow.followup import (
    RefundStudyCase, adjust_followup_payments, compare_refund_studies,
    payment_execution_schedule, recompute_affected,
)
from cashflow.plan import ActualTransfer, Plan
from cashflow.refund import record_refunds
from cashflow.replan import FeeAmendment, combined_replay
from cashflow.report import (
    daily_ledger, followup_schedule_report, followup_study_report,
    reconciliation,
)


def build() -> Scenario:
    return Scenario(
        "fu-demo", "2026-10-01", "2026-10-12",
        [Account("A", "资金户", money("700")),
         Account("B", "业务户"),
         Account("C", "备用户")],
        flows=[
            CashFlow("S1", "B", "2026-10-03", FlowKind.OUTFLOW, money("500"),
                     Priority.P2_NORMAL, "货款（后部分退回）"),
            CashFlow("S2", "B", "2026-10-08", FlowKind.OUTFLOW, money("180"),
                     Priority.P1_CRITICAL, "工资"),
            CashFlow("S3", "B", "2026-10-09", FlowKind.OUTFLOW, money("250"),
                     Priority.P2_NORMAL, "货款", "2026-10-09", "2026-10-12"),
            CashFlow("S4", "B", "2026-10-10", FlowKind.OUTFLOW, money("100"),
                     Priority.P3_OPTIONAL, "可选采购"),
            CashFlow("IN1", "B", "2026-10-12", FlowKind.INFLOW, money("200")),
        ],
        transfer_rules=[
            TransferRule("A", "B", lag_days=1, fee_rate=Decimal("0.01")),
            TransferRule("C", "B", lag_days=1, fee_rate=Decimal("0.005")),
        ],
        description="依赖退回资金的后续付款方案研究",
    )


def main() -> None:
    sc = build()
    plan = solve(sc).plan

    execution = Execution("fu-demo", plan.created_at, "2026-10-05T23:00:00")
    for o in plan.orders:
        if o.initiate_date <= "2026-10-05":
            execution.actual_transfers.append(ActualTransfer(
                o.order_id, o.source, o.target, o.initiate_date,
                o.arrival_date, o.principal, o.fee))
            execution.initiated_order_ids.append(o.order_id)
    record_refunds(execution, [
        RefundRecord("RF1", "S1", "B", "B", "2026-10-06", money("200"),
                     actual_date="2026-10-06", note="部分退回，实际到 B"),
        RefundRecord("RF2", "S1", "B", "C", "2026-10-07", money("250"),
                     note="预计退回到 C，未到达（预测，不可用）"),
    ])
    cutoff = "2026-10-05"

    print("== 1. 仅实际到账的后续付款执行表 ==")
    schedule = payment_execution_schedule(sc, plan, execution, cutoff)
    print(followup_schedule_report(schedule))

    print()
    print("== 2. 多组预计退回日期/费用假设对照（假设不进入可用余额）==")
    options = compare_refund_studies(sc, plan, execution, cutoff, [
        RefundStudyCase("退回按时", {"RF2": "2026-10-07"}),
        RefundStudyCase("退回迟到+费率升", {"RF2": "2026-10-11"},
                        (FeeAmendment("C", "B", "2026-10-06",
                                      fee_rate=Decimal("0.02")),)),
    ])
    print(followup_study_report(options))
    studied = options[1]["plan"]   # 「退回按时」假设下的方案，留待核对

    print()
    print("== 3. 调整尚未发出的付款：S3 提为 P1、取消 S4 ==")
    adj = adjust_followup_payments(
        sc, plan, execution, cutoff,
        priority_overrides={"S3": int(Priority.P1_CRITICAL)},
        cancelled_flow_ids=["S4"])
    for d in adj["plan"].payment_decisions:
        print(f"  {d['flow_id']} -> {d['status']} @ {d['scheduled_date']}"
              f"（{d['reason']}）")

    print()
    print("== 4. RF2 实际迟到且部分：只重算受影响的未执行部分 ==")
    out = recompute_affected(sc, studied, execution, cutoff, [
        RefundRecord("RF2", "S1", "B", "C", "2026-10-07", money("150"),
                     actual_date="2026-10-09", note="迟到且部分"),
    ])
    for c in out["forecast_vs_actual"]:
        print(f"  预测核对 {c['refund_id']}: 假设 {c['assumed_date']} "
              f"实际 {c['actual_date']} 金额 {c['actual_amount']} "
              f"一致={c['matches_forecast']}")
    for c in out["affected_payments"]:
        print(f"  受影响 {c['flow_id']}: {c['was']} -> {c['now']}")
    print(f"  未受影响: {', '.join(out['unaffected_payments'])}")
    print(f"  原方案保留供核对: {out['original_plan_kept']}")

    print()
    print("== 5. 落盘重开 + 资金守恒 ==")
    with tempfile.TemporaryDirectory() as tmp:
        p_old = Path(tmp) / "studied.json"
        p_new = Path(tmp) / "recomputed.json"
        p_exec = Path(tmp) / "execution.json"
        studied.save(p_old)
        out["plan"].save(p_new)
        execution.save(p_exec)
        loaded_new = Plan.load(p_new)
        loaded_exec = Execution.load(p_exec)
        full = combined_replay(sc, loaded_exec, loaded_new)
        print(f"  重开后续算方案 revision_of = {loaded_new.revision_of}")
        print(f"  原方案仍在: {Plan.load(p_old).created_at}")
        print()
        print(daily_ledger(full))
        print()
        print(reconciliation(full, sc))


if __name__ == "__main__":
    main()
