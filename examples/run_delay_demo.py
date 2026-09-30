"""第二轮演示：两跳调拨中途延误 + 部分收入未到 + 手续费提高后的剩余计划。

运行：.venv/bin/python -m examples.run_delay_demo

时间线（2026-10-01 至 10-10）：
  - 原计划用免费两跳 A->C->B 为 10-03 工资 600（P1）筹资；
  - 第二跳 C->B 在途延误一天（10-04 才到账），10-03 工资失去覆盖；
  - 客户回款 IN1 原定 10-05 到 500，实际 10-06 只到 300（部分未到）；
  - 10-03 起 A->B 加急通道当日到账，费率 2% -> 5%（更贵但及时）；
  - 10-08 可选采购 X1（P3）800，允许推迟到资金更宽松的日期。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

from cashflow import (
    Account, ActualInflow, ActualTransfer, CashFlow, Execution, FlowKind,
    Priority, RuleChange, Scenario, TransferRule, money, replay_full,
    revise_plan, solve,
)
from cashflow.report import (
    daily_ledger, event_log, gap_report, reconciliation, revision_summary,
)
from cashflow.repo import Repository


def build_scenario() -> Scenario:
    return Scenario(
        scenario_id="demo02",
        description="两跳延误+部分收入+费率提高后的重规划",
        start_date="2026-10-01",
        end_date="2026-10-10",
        accounts=[
            Account("A", "基本户", money("1300")),
            Account("B", "业务户", money("0")),
            Account("C", "过桥户", money("0")),
        ],
        flows=[
            CashFlow("IN1", "B", "2026-10-05", FlowKind.INFLOW, money("500"),
                     memo="客户回款（实际部分延误）"),
            CashFlow("IN2", "B", "2026-10-09", FlowKind.INFLOW, money("400"),
                     memo="另一笔回款（晚于采购原定日）"),
            CashFlow("W1", "B", "2026-10-03", FlowKind.OUTFLOW, money("600"),
                     Priority.P1_CRITICAL, "工资"),
            CashFlow("R1", "B", "2026-10-06", FlowKind.OUTFLOW, money("700"),
                     Priority.P2_NORMAL, "房租"),
            CashFlow("X1", "B", "2026-10-08", FlowKind.OUTFLOW, money("800"),
                     Priority.P3_OPTIONAL, "可选采购"),
        ],
        transfer_rules=[
            TransferRule("A", "B", lag_days=1, fee_rate=Decimal("0.02")),
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
    scenario = build_scenario()
    repo.save_scenario(scenario)

    section("1) 原始计划（不知延误）：免费两跳 A->C->B")
    base = solve(scenario).plan
    for o in base.orders:
        print(f"  {o.order_id}: {o.initiate_date} {o.source}->{o.target} "
              f"{o.principal}，{o.arrival_date} 到账，费 {o.fee}")
    print(gap_report(base))
    repo.save_plan(base)

    # ---- 10-03 早晨重开：只录入此前真实发生的事实 ----
    actual = [
        # T001 第一跳按时：10-01 A->C 600 当日到账（C 已无此款）
        ActualTransfer("T001", "A", "C", "2026-10-01", "2026-10-01",
                       money("600"), money("0"), "executed"),
        # T002 第二跳 10-02 按时发起，但在途延误，10-04 才到账
        ActualTransfer("T002", "C", "B", "2026-10-02", "2026-10-04",
                       money("600"), money("0"), "executed"),
        # T003/T004 原排期在 10-04/10-05，尚未发起，不录入 -> 重规划重新安排
    ]
    actual_inflows = [
        ActualInflow("IN1", "B", "2026-10-05", "2026-10-06",
                     money("500"), money("300")),
    ]
    rule_changes = [
        RuleChange("A", "B", "2026-10-03", lag_days=0,
                   fee_rate=Decimal("0.05"),
                   note="加急通道当日到账，费率 2% -> 5%"),
    ]
    execution = Execution(
        scenario_id=scenario.scenario_id,
        plan_created_at=base.created_at,
        recorded_at=datetime.now().isoformat(timespec="seconds"),
        actual_transfers=actual, actual_inflows=actual_inflows,
        rule_changes=rule_changes,
    )
    repo.save_execution(execution)  # 实际执行记录独立落盘

    section("2) 10-03 早晨重开：基于实际事实重规划（执行记录与计划分开）")
    rr = revise_plan(scenario, base, execution, cutoff="2026-10-03")
    print(revision_summary(rr.revision))
    path = repo.save_revision(rr.revision)

    section("3) 实际前缀 + 修订后缀 全程重放：每日余额核对")
    full = replay_full(scenario, execution, rr.revision)
    print(event_log(full))
    print()
    print(daily_ledger(full))
    print()
    print(reconciliation(full, scenario))
    print(f"\n修订计划已独立保存：{path}")
    print("重开后可直接读取该修订计划与执行记录再次对账，资金去向可对清。")


if __name__ == "__main__":
    main()
