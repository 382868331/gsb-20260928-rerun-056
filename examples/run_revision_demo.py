"""第二轮演示：调拨延误后的修订重规划全过程。

场景 demo02（2026-10-01..10）：
  A 基本户 1700 / B 业务户 0 / C 过桥户 0
  原规则：A->B 隔日 2%（贵但及时）；A->C 当日免费；C->B 隔日免费（两跳免费）
  收支：10-03 工资 600(P1)；10-04 回款 IN1 原定 500（实际仅部分到账 300）；
        10-05 房租 700(P2)；10-06 可选采购 800(P3)；10-08 尾款 IN2 400

cutoff=2026-10-02 日终，实际：
  原计划第一跳 A->C 600 已发起（钱已离开 A），但中途延误，10-04 才到 C；
  第二跳 C->B 因 C 无款未发起；无收入到账。
  自 10-03 起手续费提高：C->B 按 2% 收，A->B 仍 2%。

修订要点：在途 600 保持不动；工资只能走更贵但及时的 A->B 直达；
  房租借助 300 部分回款 + 到账后的在途资金，C->B(2%) 补差额；
  可选采购推迟到 10-08 尾款到账后支付；每日余额可核对，重开后单本账对清。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

from cashflow import (
    Account, CashFlow, FlowKind, Priority, Scenario, TransferRule,
    Execution, Plan, money, solve,
)
from cashflow.plan import ActualInflow, ActualTransfer
from cashflow.engine import replay
from cashflow.replan import FeeAmendment, replan, combined_replay
from cashflow.report import (
    daily_ledger, event_log, gap_report, reconciliation, revision_summary,
)
from cashflow.repo import Repository


def build_demo02() -> Scenario:
    return Scenario(
        scenario_id="demo02",
        description="延误修订：两跳中途延误+部分收入未到+手续费提高",
        start_date="2026-10-01",
        end_date="2026-10-10",
        accounts=[
            Account("A", "基本户", money("1700")),
            Account("B", "业务户", money("0")),
            Account("C", "过桥户", money("0")),
        ],
        flows=[
            CashFlow("IN1", "B", "2026-10-04", FlowKind.INFLOW, money("500"),
                     memo="客户回款（实际仅到账300）"),
            CashFlow("IN2", "B", "2026-10-08", FlowKind.INFLOW, money("400"),
                     memo="尾款"),
            CashFlow("W1", "B", "2026-10-03", FlowKind.OUTFLOW, money("600"),
                     Priority.P1_CRITICAL, "工资"),
            CashFlow("R1", "B", "2026-10-05", FlowKind.OUTFLOW, money("700"),
                     Priority.P2_NORMAL, "房租"),
            CashFlow("X1", "B", "2026-10-06", FlowKind.OUTFLOW, money("800"),
                     Priority.P3_OPTIONAL, "可选采购"),
        ],
        transfer_rules=[
            TransferRule("A", "B", lag_days=0, fee_rate=Decimal("0.02")),
            TransferRule("A", "C", lag_days=0, fee_rate=Decimal("0")),
            TransferRule("C", "B", lag_days=1, fee_rate=Decimal("0")),
        ],
    )


def section(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def main() -> None:
    repo = Repository(Path(__file__).resolve().parent.parent / "data")
    scenario = build_demo02()
    repo.save_scenario(scenario)

    section("1) 原计划（延误发生前）")
    original = solve(scenario).plan
    print(f"原计划 {len(original.orders)} 笔，总费用 {original.total_fee}")
    repo.save_plan(original)

    # 实际执行：只执行了第一跳 A->C（T001 类），且延误到 10-04
    first_leg = next(o for o in original.orders
                     if o.source == "A" and o.target == "C")
    execution = Execution(
        scenario_id=scenario.scenario_id,
        plan_created_at=original.created_at,
        recorded_at=datetime.now().isoformat(timespec="seconds"),
        actual_transfers=[
            ActualTransfer(first_leg.order_id, "A", "C",
                           first_leg.initiate_date, "2026-10-04",  # 中途延误
                           first_leg.principal, first_leg.fee,
                           status="executed"),
        ],
        initiated_order_ids=[first_leg.order_id],
        actual_inflows=[],   # cutoff=10-02 前无任何收入到账
    )
    repo.save_execution(execution)
    print(f"实际只发起 {first_leg.order_id}（A->C 600），10-04 才到 C；"
          f"第二跳未发起；执行记录独立存档。")

    section("2) cutoff=10-02 日终：按实际执行核对每日余额")
    from cashflow.replan import build_prefix
    prefix = build_prefix(scenario, execution, "2026-10-02")
    print(daily_ledger(prefix))
    print(f"在途：{ {k: str(v.principal)+f'（{v.arrival_date}到）' for k, v in prefix.ledger.in_transit.items()} }")

    section("3) 手续费提高 + 回款部分到账，修订剩余计划")
    amendments = [FeeAmendment("C", "B", "2026-10-03",
                               fee_rate=Decimal("0.02"))]
    rr = replan(
        scenario, original, execution, "2026-10-02",
        amendments=amendments,
        inflow_updates={"IN1": ("2026-10-04", money("300"))},
        chain_quote={"W1": {"timely_fee": money("12"),
                             "cheap_fee": money("0"),
                             "extra_fee": money("12")},
                     "R1": {"timely_fee": money("8"),
                            "cheap_fee": money("0"),
                            "extra_fee": money("8"),
                            "note": "两跳到账日晚于房租日（同日到账不可再发起）"}},
    )
    revised = rr.plan
    path = repo.save_plan(revised)
    print(revision_summary(revised))
    print(f"\n修订计划独立保存（revision_of={revised.revision_of}）：{path}")

    section("4) 残差期间每日余额（cutoff 次日起）")
    print(daily_ledger(rr.residual_replay))

    section("5) 重开后从期初一路对清资金去向")
    full = combined_replay(scenario, execution, revised,
                           inflow_updates={"IN1": ("2026-10-04", money("300"))})
    print(daily_ledger(full))
    print()
    print(reconciliation(full, scenario))
    assert revised.remaining_gaps == [], "本场景所有支付均可满足或推迟"
    assert not revised.fixed_transit[0].order_id.startswith("R")
    print("\n断言通过：无未覆盖缺口；在途单未生成新扣费；执行记录与修订计划分文件存档。")


if __name__ == "__main__":
    main()
