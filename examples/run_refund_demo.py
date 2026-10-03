"""第四轮演示：退回资金与剩余支付重规划全过程。

场景 demo03（2026-10-01..12）：
  A 资金户 700 / B 业务户 0
  规则：A->B 隔日到账，费率 1%
  收支：10-03 货款 S1 500(P2)（供应商后来部分退回）；
        10-09 工资 S2 300(P1)；10-10 回款 IN1 200

cutoff=2026-10-04 日终，实际：
  T001（A->B 500，费 5）已发起并到账，S1 已全额付给供应商；
  供应商分两次退回：RF1 200 已于 10-04 退回 B 户（可用），
  RF2 150 预计 10-08 退回（未到达，不能提前当作可用资金），手续费 5 不退；
  RF1 的重复通知与 RF2 的金额更正都不会产生第二份余额。

演示要点：
  1. 退回去向核对：原付款 500 = 收款方留存 300 + 已退回可用 200
     （另有预计退回 150 不可用）；
  2. 对照「等待退回」与「替代账户安排」两种剩余支付重规划的缺口与费用；
  3. 采用等待方案后，重开 combined_replay 单本账资金守恒，
     原调拨只发起一次、退回只入账一次。
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from cashflow import (
    Account, CashFlow, FlowKind, Priority, Scenario, TransferRule,
    Execution, RefundRecord, money, solve,
)
from cashflow.plan import ActualTransfer
from cashflow.refund import (
    compare_refund_strategies, record_refunds, refund_disposition,
)
from cashflow.replan import replan, combined_replay
from cashflow.report import (
    daily_ledger, reconciliation, refund_report, refund_strategy_report,
    revision_summary,
)
from cashflow.repo import Repository


def build_demo03() -> Scenario:
    return Scenario(
        scenario_id="demo03",
        description="部分退回+退回迟到+手续费不退后的剩余支付重规划",
        start_date="2026-10-01",
        end_date="2026-10-12",
        accounts=[
            Account("A", "资金户", money("700")),
            Account("B", "业务户", money("0")),
        ],
        flows=[
            CashFlow("S1", "B", "2026-10-03", FlowKind.OUTFLOW, money("500"),
                     Priority.P2_NORMAL, "货款（供应商部分退回）"),
            CashFlow("S2", "B", "2026-10-09", FlowKind.OUTFLOW, money("300"),
                     Priority.P1_CRITICAL, "工资"),
            CashFlow("IN1", "B", "2026-10-10", FlowKind.INFLOW, money("200"),
                     memo="客户回款"),
        ],
        transfer_rules=[
            TransferRule("A", "B", lag_days=1, fee_rate=Decimal("0.01")),
        ],
    )


def main() -> None:
    scenario = build_demo03()
    repo = Repository(Path("data"))
    repo.save_scenario(scenario)

    plan = solve(scenario).plan
    repo.save_plan(plan)

    t1 = plan.orders[0]
    execution = Execution(
        scenario_id=scenario.scenario_id,
        plan_created_at=plan.created_at,
        recorded_at="2026-10-04T23:00:00",
        actual_transfers=[
            ActualTransfer(t1.order_id, "A", "B", t1.initiate_date,
                           t1.arrival_date, t1.principal, t1.fee)],
        initiated_order_ids=[t1.order_id],
    )
    # 退回实际记录：RF1 到达；RF2 预计；RF1 重复通知；RF2 更正为 140
    notes = record_refunds(execution, [
        RefundRecord("RF1", "S1", "B", "B", "2026-10-04", money("200"),
                     actual_date="2026-10-04", note="部分退回第一笔"),
        RefundRecord("RF2", "S1", "B", "B", "2026-10-08", money("150"),
                     note="预计退回"),
        RefundRecord("RF1", "S1", "B", "B", "2026-10-04", money("200"),
                     actual_date="2026-10-04", note="部分退回第一笔"),
        RefundRecord("RF2", "S1", "B", "B", "2026-10-08", money("140"),
                     note="更正：退回金额改为 140"),
    ])
    print("退回登记（重复通知被忽略，更正覆盖原记录）:")
    for n in notes:
        print(f"  {n['refund_id']} [{n['action']}] {n['handling']}")
    assert len(execution.refunds) == 2
    assert execution.refunds[1].amount == money("140")

    cutoff = "2026-10-04"
    disp = refund_disposition(scenario, execution, cutoff)
    print("\n退回去向核对（原付款 / 收款方留存 / 已退回可用 / 预计退回）:")
    for p in disp["payments"]:
        print(f"  {p['flow_id']}: 原付款 {p['paid']} 收款方留存 {p['payee_kept']} "
              f"已退回可用 {p['refund_arrived_usable']} "
              f"预计退回不可用 {p['refund_expected_not_usable']} "
              f"手续费退回 {p['fee_refunded']}")
    print(f"  各账户可用余额: {disp['balances']}  在途: {disp['in_transit']}")

    print("\n剩余支付重规划对照:")
    options = compare_refund_strategies(scenario, plan, execution, cutoff)
    print(refund_strategy_report(options))

    # 采用「等待退回」方案：RF2 按 10-08 假设到账参与规划
    rr = replan(scenario, plan, execution, cutoff,
                refund_assumptions={"RF2": "2026-10-08"})
    repo.save_plan(rr.plan)
    repo.save_execution(execution)
    print()
    print(revision_summary(rr.plan))
    print()
    print(refund_report(rr.plan))
    print()
    print(daily_ledger(rr.residual_replay))

    full = combined_replay(scenario, execution, rr.plan,
                           refund_assumptions={"RF2": "2026-10-08"})
    print()
    print(reconciliation(full, scenario))

    # 关键断言
    full.ledger.verify_conservation()
    refund_inflows = [e for e in full.ledger.log
                      if e["event"] == "inflow" and
                      e["flow_id"].startswith("REFUND-")]
    assert len(refund_inflows) == 2, refund_inflows   # RF1/RF2 各入账一次
    initiations = [e["order_id"] for e in full.ledger.log
                   if e["event"] == "transfer_initiate"]
    assert len(initiations) == len(set(initiations))  # 原调拨不重复发起
    rf2 = next(r for r in rr.plan.refund_records if r["refund_id"] == "RF2")
    assert rf2["status"] == "assumed"                 # 预计退回仅作假设入账
    print("\n全部断言通过：退回只入账一次，原调拨不回滚，预计退回不计入可用资金。")


if __name__ == "__main__":
    main()
