"""第三轮演示：部分采纳、确认占款、撤回与重开续作全过程。

场景 demo03（2026-10-01..10-12）：
  A 基本户 1000 / B 业务户 0 / C 运营户 0
  规则：A->B 当日 1%（贵但及时）；A->C 当日免费；C->B 隔日免费
  收支：10-03 工资 500(P1)；10-05 房租 400(P2) 与供应商款 300(P2，C 户，
        与房租争用 A 的同一笔资金)；10-06 回款 IN1 500（实际迟到到 10-08
        且只到 300）；10-07 可选采购 200(P3，窗口 10-07..10-11)

过程：
  1) 求解候选计划；2) 两个部分采纳方案对照（确认工资链 / 确认房租链）；
  3) 采纳方案甲（只确认工资链），查看其余候选安排的影响；
  4) cutoff=10-02：工资链两笔已发出（第二跳仍在途），回款迟到且部分到账；
  5) 撤回采纳：已发出部分保持在途、手续费不退，未执行部分释放重排；
  6) 重新打开采纳记录继续采纳，整段重放逐日对账：不重复占款、费用不抹去。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

from cashflow import (
    Account, CashFlow, FlowKind, Priority, Scenario, TransferRule,
    Execution, money, solve,
)
from cashflow.adopt import (
    Adoption, adoption_combined_replay, compare_partials, confirm_partial,
    withdraw_adoption,
)
from cashflow.plan import ActualInflow, ActualTransfer
from cashflow.replan import combined_replay
from cashflow.report import (
    adoption_compare_report, adoption_summary, daily_ledger, plan_summary,
    reconciliation, withdrawal_summary,
)
from cashflow.repo import Repository


def build_demo03() -> Scenario:
    return Scenario(
        scenario_id="demo03",
        description="部分采纳：收入迟到+共享资金争用+部分到账",
        start_date="2026-10-01",
        end_date="2026-10-12",
        accounts=[
            Account("A", "基本户", money("1000")),
            Account("B", "业务户", money("0")),
            Account("C", "运营户", money("0")),
        ],
        flows=[
            CashFlow("IN1", "B", "2026-10-06", FlowKind.INFLOW, money("500"),
                     memo="客户回款（实际 10-08 才到，且只到 300）"),
            CashFlow("W1", "B", "2026-10-03", FlowKind.OUTFLOW, money("500"),
                     Priority.P1_CRITICAL, "工资"),
            CashFlow("R1", "B", "2026-10-05", FlowKind.OUTFLOW, money("400"),
                     Priority.P2_NORMAL, "房租"),
            CashFlow("S1", "C", "2026-10-05", FlowKind.OUTFLOW, money("300"),
                     Priority.P2_NORMAL, "供应商款（与房租争用 A 的资金）"),
            CashFlow("X1", "B", "2026-10-07", FlowKind.OUTFLOW, money("200"),
                     Priority.P3_OPTIONAL, "可选采购",
                     "2026-10-07", "2026-10-11"),
        ],
        transfer_rules=[
            TransferRule("A", "B", lag_days=0, fee_rate=Decimal("0.01")),
            TransferRule("A", "C", lag_days=0),
            TransferRule("C", "B", lag_days=1),
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

    section("1) 候选计划（共享资金：A 的 1000 要同时养 B、C 两户）")
    plan = solve(scenario).plan
    repo.save_plan(plan)
    print(plan_summary(plan))
    print(f"  缺口：{plan.remaining_gaps or '无'}")

    w1_chain = [o.order_id for o in plan.orders
                if (o.target == "B" and o.arrival_date <= "2026-10-03")
                or (o.target == "C" and o.arrival_date <= "2026-10-02")]
    r1_chain = [o.order_id for o in plan.orders
                if o.initiate_date >= "2026-10-03"]

    section("2) 两个部分采纳方案的对照（保留原约束 / 确认范围 / 剩余缺口）")
    results = compare_partials(scenario, plan,
                               [("甲-只确认工资链", w1_chain),
                                ("乙-只确认房租链", r1_chain)])
    print(adoption_compare_report(results))

    section("3) 采纳方案甲：确认占款后剩余付款重排 + 候选影响")
    t007 = next(o for o in plan.orders
                if o.source == "A" and o.target == "C"
                and o.initiate_date >= "2026-10-03")
    adopted = confirm_partial(scenario, plan, w1_chain + [t007.order_id],
                              label="甲-工资链+房租第一跳")
    print(adoption_summary(adopted))
    assert adopted.residual_plan is not None
    repo.save_adoption(adopted.adoption)
    repo.save_plan(adopted.residual_plan)

    section("4) cutoff=10-02：工资链已发出（第二跳在途），回款迟到且部分到账")
    execution = Execution(
        scenario_id=scenario.scenario_id,
        plan_created_at=plan.created_at,
        recorded_at="2026-10-02T23:00:00",
        actual_transfers=[
            ActualTransfer(o.order_id, o.source, o.target, o.initiate_date,
                           o.arrival_date, o.principal, o.fee)
            for o in plan.orders if o.order_id in w1_chain
        ],
        initiated_order_ids=w1_chain,   # T007 尚未发出，撤回时将被释放
        actual_inflows=[
            ActualInflow("IN1", "B", "2026-10-06", "2026-10-08", money("300")),
        ],
    )
    repo.save_execution(execution)
    print(f"已发出 {w1_chain}（第二跳 10-03 才到 B，cutoff 时在途）；"
          "IN1 实际 10-08 才到且只到 300。")

    section("5) 撤回采纳：只影响未执行部分，在途资金逐笔说明")
    wres = withdraw_adoption(scenario, adopted.adoption, plan, execution,
                             "2026-10-02")
    print(withdrawal_summary(wres))
    repo.save_adoption(wres.adoption)
    repo.save_plan(wres.replan_result.plan)
    print()
    print(daily_ledger(wres.replan_result.residual_replay))

    section("6) 重新打开采纳记录，继续采纳剩余安排")
    loaded = repo.load_adoption(
        repo.list_adoptions(scenario.scenario_id)[-1])
    print(f"重开采纳记录 {loaded.adoption_id}，状态 {loaded.status}，"
          f"历史撤回 {len(loaded.withdrawals)} 次")
    continued = confirm_partial(
        scenario, plan, w1_chain + r1_chain, prior=loaded,
        label="甲+撤回后重新采纳房租链")
    print(f"重复确认被跳过（不重复占款）：{continued.skipped}")
    print(adoption_summary(continued))

    section("7) 整段重放逐日对账：不重复占款、已发生费用不抹去")
    full = adoption_combined_replay(scenario, continued.adoption,
                                    continued.residual_plan, execution)
    print(daily_ledger(full))
    print()
    print(reconciliation(full, scenario))

    initiations = [e["order_id"] for e in full.ledger.log
                   if e["event"] == "transfer_initiate"]
    assert len(initiations) == len(set(initiations)), "同一单号不得发起两次"
    assert full.ledger.total_inflows == money("300"), "收入按实际部分到账入账"
    full.ledger.verify_conservation()
    print("\n断言通过：单号唯一不重复占款；收入按实际 300 入账；"
          "逐日账与资金守恒全部核对通过。")


if __name__ == "__main__":
    main()
