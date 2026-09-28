"""小例子：枚举 vs 贪心对拍，输出可重放计划、缺口位置与资金账核对。

运行：python -m examples.run_demo
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

from cashflow import (
    Account, CashFlow, FlowKind, Priority, Scenario, TransferRule,
    Execution, Ledger, money, simulate_baseline, solve, brute_force_solve,
)
from cashflow.report import (
    daily_ledger, event_log, gap_report, plan_summary, reconciliation,
)
from cashflow.repo import Repository
from cashflow.engine import replay
from cashflow.plan import ActualTransfer


def build_demo() -> Scenario:
    """账户：A 基本户(1000) / B 业务户(0) / C 过桥户(0)。

    调拨规则：
      A->B 隔日到账(lag=1)，手续费 2%（贵但一跳）
      A->C 当日到账，免费
      C->B 隔日到账，免费（两跳免费路径）
    """
    return Scenario(
        scenario_id="demo01",
        description="三账户小例子：到账时延+手续费+支出优先级",
        start_date="2026-10-01",
        end_date="2026-10-08",
        accounts=[
            Account("A", "基本户", money("1000")),
            Account("B", "业务户", money("0")),
            Account("C", "过桥户", money("0")),
        ],
        flows=[
            CashFlow("IN1", "B", "2026-10-04", FlowKind.INFLOW, money("500"),
                     memo="客户回款（晚于工资日，不能提前用）"),
            CashFlow("W1", "B", "2026-10-03", FlowKind.OUTFLOW, money("600"),
                     Priority.P1_CRITICAL, "工资"),
            CashFlow("R1", "B", "2026-10-05", FlowKind.OUTFLOW, money("700"),
                     Priority.P2_NORMAL, "房租"),
            CashFlow("X1", "B", "2026-10-06", FlowKind.OUTFLOW, money("800"),
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
    scenario = build_demo()
    repo.save_scenario(scenario)

    section("1) 无调拨基线")
    base = simulate_baseline(scenario)
    gaps = base.gap_total_by_priority()
    print(f"缺口：P1={gaps[1]}  P2={gaps[2]}  P3={gaps[3]}")
    print("（IN1 在 10-04 才到账，无法支付 10-03 的工资；B 户自身无余额）")

    section("2) 贪心求解：先满足优先支出，再最小化手续费")
    result = solve(scenario)
    plan, final = result.plan, result.replay
    print(plan_summary(plan))
    print()
    print(gap_report(plan))
    print("\n说明：A->B 直达需 2% 手续费（600 元要 12 元、200 元要 4 元）；")
    print("      A->C->B 两跳免费，故优先选免费路径；P3 受 A 户余额约束仍有缺口。")

    section("3) 小例子穷举枚举，与贪心对拍")
    brute = brute_force_solve(scenario)
    print(f"枚举最优目标元组 {tuple(brute.objective)} vs 贪心 {tuple(plan.objective)}")
    assert tuple(brute.objective) == tuple(plan.objective), "贪心与枚举结果不一致！"
    assert money(brute.total_fee) == money(plan.total_fee)
    print("对拍通过：贪心在穷举空间内达到字典序最优。")

    section("4) 按时间重放计划")
    print(event_log(final))

    section("5) 每日各账户资金账")
    print(daily_ledger(final))

    section("6) 账户资金核对")
    print(reconciliation(final, scenario))

    path = repo.save_plan(plan)
    print(f"\n场景与计划已分别保存：{path}")

    delayed = plan.orders[0]
    section(f"7) 实际执行记录（与计划分开）：{delayed.order_id} 延误一天")
    actuals = []
    for o in plan.orders:
        if o.order_id == delayed.order_id:
            actuals.append(ActualTransfer(
                o.order_id, o.source, o.target,
                "2026-10-02", "2026-10-02",     # 发起、到账各晚一天
                o.principal, o.fee))
        else:
            actuals.append(ActualTransfer(
                o.order_id, o.source, o.target,
                o.initiate_date, o.arrival_date, o.principal, o.fee))
    execution = Execution(
        scenario_id=scenario.scenario_id,
        plan_created_at=plan.created_at,
        recorded_at=datetime.now().isoformat(timespec="seconds"),
        actual_transfers=actuals,
    )
    repo.save_execution(execution)
    legs = Ledger.legs_from_orders(actuals)
    try:
        actual_result = replay(scenario, legs=legs)
        print(reconciliation(actual_result, scenario))
        for g in actual_result.gaps:
            print(f"  实际执行新增缺口：{g.date} {g.flow_id} 缺口 {g.shortfall}")
    except Exception as exc:  # noqa: BLE001
        print(f"  实际执行无法按序过账：{exc}")
    print("（执行记录独立存档，下一轮可做计划 vs 实际差异，并在此基础上加入融资选择）")


if __name__ == "__main__":
    main()
