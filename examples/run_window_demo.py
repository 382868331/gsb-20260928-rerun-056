"""第三轮演示：支付窗口内可执行安排、多策略对照、采纳与差异复演。

场景 demo03（2026-10-01..12）：
  A 基本户 800 / B 业务户 0 / C 过桥户 0
  A->B 隔日 2%（贵但及时）；A->C 当日免费、C->B 隔日免费（两跳免费）
  收支：
    10-03 工资 600(P1)；10-04 回款 IN1 500；10-05 房租 700(P2)；
    10-06 采购 X1 500(P3，允许支付窗口 10-06..10-09)；
    10-08 尾款 IN2 500；
    10-06 采购 X2 2000(P3，窗口 10-06..10-09，全盘资金都不够 -> 真正不足)。

演示要点：
  1) strict_due 坚持原期限：X1 在 10-06 当天两跳来不及，被部分支付(0)并记缺口，
     但可行性枚举证明窗口内 10-08 可足额 -> 分类为 strategy_not_used，
     不再谎称“整个期间都付不起”；X2 枚举全部组合仍无法覆盖 -> truly_unmet。
  2) fastest_feasible：X1 改期到 10-08（IN2 当日到 B，过账顺序先收入后支出），
     零费用足额支付；原始期限 10-06 保留不改写。
  3) lowest_fee：与 fastest 相同地等待回款，不为 X1 做任何收费紧急筹资。
  4) 用户采纳 fastest 方案：旧计划文件与其依据快照原样保留，新增 Adoption 记录；
     重开后 diff 对出两版逐日余额、支付日、费用与最终缺口差异，两本账各自守恒。

另含 replan 路径演示：在第二轮“两跳延误+部分到账+费率提高”的残差场景上叠加
支付窗口，收入推迟/费率变化/部分支付同时发生时逐日余额依然守恒。

运行：.venv/bin/python -m examples.run_window_demo
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

from cashflow import (
    Account, CashFlow, FlowKind, Priority, Scenario, TransferRule, money,
)
from cashflow.scheduling import compare_strategies, run_strategy
from cashflow.report import (
    strategy_comparison, daily_ledger, reconciliation,
)
from cashflow.adoption import adopt, diff_versions
from cashflow.repo import Repository


def build_demo03() -> Scenario:
    return Scenario(
        scenario_id="demo03",
        description="支付窗口：当天付不起但窗口内稍后可足额；真不足 vs 策略未采用",
        start_date="2026-10-01",
        end_date="2026-10-12",
        accounts=[
            Account("A", "基本户", money("800")),
            Account("B", "业务户", money("0")),
            Account("C", "过桥户", money("0")),
        ],
        flows=[
            CashFlow("IN1", "B", "2026-10-04", FlowKind.INFLOW, money("500"),
                     memo="客户回款"),
            CashFlow("IN2", "B", "2026-10-08", FlowKind.INFLOW, money("500"),
                     memo="尾款（窗口内到账）"),
            CashFlow("W1", "B", "2026-10-03", FlowKind.OUTFLOW, money("600"),
                     Priority.P1_CRITICAL, "工资"),
            CashFlow("R1", "B", "2026-10-05", FlowKind.OUTFLOW, money("700"),
                     Priority.P2_NORMAL, "房租"),
            CashFlow("X1", "B", "2026-10-06", FlowKind.OUTFLOW, money("500"),
                     Priority.P3_OPTIONAL, "采购（窗口内可改期）",
                     pay_window_end="2026-10-09"),
            CashFlow("X2", "B", "2026-10-06", FlowKind.OUTFLOW, money("2000"),
                     Priority.P3_OPTIONAL, "采购（全盘资金不足）",
                     pay_window_end="2026-10-09"),
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
    scenario = build_demo03()
    repo.save_scenario(scenario)

    section("1) 三种支付优先级方案对照（同一输入，原始期限不改写）")
    outcomes = compare_strategies(scenario)
    strict, fastest, lowest = outcomes
    strict.plan.created_at = "2026-10-01T08:00:00"
    fastest.plan.created_at = "2026-10-01T09:00:00"
    lowest.plan.created_at = "2026-10-01T10:00:00"
    print(strategy_comparison(outcomes))

    section("2) strict_due 的逐日账：X1 10-06 当天确实付不起")
    print(daily_ledger(strict.replay))
    print(reconciliation(strict.replay, scenario))

    section("3) fastest_feasible 的逐日账：X1 改到 10-08 足额、零额外费用")
    print(daily_ledger(fastest.replay))
    print(reconciliation(fastest.replay, scenario))

    by_flow = {d["flow_id"]: d for d in fastest.decisions}
    assert by_flow["X1"]["status"] == "rescheduled"
    assert by_flow["X1"]["scheduled_date"] == "2026-10-08"
    assert by_flow["X1"]["shortfall"] == "0.00"
    assert fastest.total_fee == Decimal("0.00")
    strict_by_flow = {d["flow_id"]: d for d in strict.decisions}
    assert strict_by_flow["X1"]["status"] == "strategy_not_used"
    assert strict_by_flow["X2"]["status"] == "truly_unmet"
    # 输入的原始期限保留
    assert next(f for f in scenario.flows if f.flow_id == "X1").date == "2026-10-06"

    section("4) 采纳 fastest 方案（旧计划与其依据保持不变，另存采纳记录）")
    old_path = repo.save_plan(strict.plan)
    new_path = repo.save_plan(fastest.plan)
    old_bytes = old_path.read_bytes()
    record = adopt(fastest.plan, strict.plan,
                   note="采购 X1 在窗口内改到 10-08，尾款到账即足额支付",
                   adopted_at="2026-10-01T09:10:00")
    adoption_path = repo.save_adoption(record)
    assert old_path.read_bytes() == old_bytes, "采纳不得改写旧计划文件"
    print(f"旧计划：{old_path}（字节未变）")
    print(f"采纳版：{new_path}")
    print(f"采纳记录：{adoption_path}")

    section("5) 重开后复演两版差异（支付日/费用/缺口/逐日余额）")
    vd = diff_versions(scenario, strict.plan, fastest.plan)
    print(vd.summary())

    section("6) 修订重规划叠加支付窗口：延误+部分到账+费率变化同时发生")
    from cashflow import solve
    from cashflow.plan import ActualTransfer
    from cashflow.replan import FeeAmendment, replan, combined_replay
    from tests.test_replan import build as build_r, delayed_execution
    sc2 = build_r()
    from cashflow.models import CashFlow as CF
    sc2.flows = [
        CF(f.flow_id, f.account_id, f.date, f.kind, f.amount, f.priority,
           f.memo, "2026-10-09" if f.flow_id == "X1" else "")
        for f in sc2.flows]
    original = solve(sc2).plan
    execution = delayed_execution(original)
    rr = replan(
        sc2, original, execution, "2026-10-02",
        amendments=[FeeAmendment("C", "B", "2026-10-03",
                                 fee_rate=Decimal("0.02"))],
        inflow_updates={"IN1": ("2026-10-05", money("200"))},  # 500->200 且推迟
        window_strategy="fastest_feasible")
    revised = rr.plan
    repo.save_plan(revised)
    for d in revised.payment_decisions:
        print(f"  [{d['status']}] {d['flow_id']} 原期限 {d['original_due_date']}"
              f" -> {d['scheduled_date']} 付 {d['paid']} 缺口 {d['shortfall']}")
    full = combined_replay(
        sc2, execution, revised,
        inflow_updates={"IN1": ("2026-10-05", money("200"))})
    full.ledger.verify_conservation()
    print(reconciliation(full, sc2))
    print("\n全部断言通过：三分类正确、改期可足额、旧计划不变、逐日资金守恒。")


if __name__ == "__main__":
    main()
