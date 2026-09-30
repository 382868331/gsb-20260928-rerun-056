"""第二轮：调拨延误后的剩余计划重规划测试。"""

import tempfile
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from cashflow import (
    Account, ActualInflow, ActualTransfer, CashFlow, Execution, FlowKind,
    LedgerError, Priority, RevisedPlan, RuleChange, Scenario, TransferRule,
    money, replay_full, revise_plan, solve,
)
from cashflow.repo import Repository
from examples.run_delay_demo import build_scenario


def _base_orders(scenario):
    return solve(scenario).plan


class TestInTransitNotReused(unittest.TestCase):
    def test_delayed_two_hop_keeps_money_in_transit(self):
        scenario = build_scenario()
        base = _base_orders(scenario)
        execution = Execution(
            scenario_id=scenario.scenario_id, plan_created_at=base.created_at,
            recorded_at=datetime.now().isoformat(timespec="seconds"),
            actual_transfers=[
                ActualTransfer("T001", "A", "C", "2026-10-01", "2026-10-01",
                               money("600"), money("0")),
                ActualTransfer("T002", "C", "B", "2026-10-02", "2026-10-04",
                               money("600"), money("0")),
            ],
            actual_inflows=[
                ActualInflow("IN1", "B", "2026-10-05", "2026-10-06",
                             money("500"), money("300")),
            ],
            rule_changes=[
                RuleChange("A", "B", "2026-10-03", lag_days=0,
                           fee_rate=Decimal("0.05")),
            ],
        )
        rr = revise_plan(scenario, base, execution, "2026-10-03")
        rev = rr.revision

        # 在途的 T002 本金 600 不得再次从 C（或 A）动用：新增单不得动用该笔
        new_sources_c = [o for o in rev.revised_orders if o.source == "C"]
        self.assertEqual(new_sources_c, [])

        # 工资 P1 必须通过更贵但当日到账的 A->B 加急路径按期覆盖
        urgent = [o for o in rev.revised_orders if (o.source, o.target) == ("A", "B")]
        self.assertEqual(len(urgent), 1)
        self.assertEqual(urgent[0].arrival_date, "2026-10-03")
        self.assertEqual(urgent[0].fee, money("30"))  # 600 * 5%
        gaps = {g["flow_id"]: g["shortfall"] for g in rev.remaining_gaps}
        self.assertNotIn("W1", gaps)
        self.assertNotIn("R1", gaps)

        # 选择理由明确写出“更贵但及时”
        self.assertTrue(any("更贵但及时" in c["reason"] for c in rev.choices))

        # P3 被推迟且仍有明确未满足部分
        self.assertEqual([d["flow_id"] for d in rev.deferrals], ["X1"])
        self.assertEqual(gaps["X1"], "200.00")

        # 全程（实际前缀 + 修订后缀）重放资金守恒、余额非负
        full = replay_full(scenario, execution, rev)
        full.ledger.verify_conservation()

    def test_in_transit_seed_conservation(self):
        # 窗口开始时在途本金计入期初资产：余额+在途+费 = 期初+期初在途+收入-支出
        scenario = build_scenario()
        base = _base_orders(scenario)
        execution = Execution(
            scenario_id=scenario.scenario_id, plan_created_at=base.created_at,
            recorded_at=datetime.now().isoformat(timespec="seconds"),
            actual_transfers=[
                ActualTransfer("T001", "A", "C", "2026-10-01", "2026-10-01",
                               money("600"), money("0")),
                ActualTransfer("T002", "C", "B", "2026-10-02", "2026-10-04",
                               money("600"), money("0")),
            ],
            actual_inflows=[
                ActualInflow("IN1", "B", "2026-10-05", "2026-10-06",
                             money("500"), money("300")),
            ],
            rule_changes=[
                RuleChange("A", "B", "2026-10-03", lag_days=0,
                           fee_rate=Decimal("0.05")),
            ],
        )
        rr = revise_plan(scenario, base, execution, "2026-10-03")
        # 截断窗口期初在途 600 必须守恒，不抛账不平
        rr.replay.replay.ledger.verify_conservation()


class TestPartialInflowAndFeeHike(unittest.TestCase):
    def test_partial_inflow_reduces_coverage(self):
        # 收入 IN1 只到 300（而非 500），房租 700 仍须由筹资/在途覆盖
        scenario = build_scenario()
        base = _base_orders(scenario)
        execution = Execution(
            scenario_id=scenario.scenario_id, plan_created_at=base.created_at,
            recorded_at=datetime.now().isoformat(timespec="seconds"),
            actual_transfers=[
                ActualTransfer("T001", "A", "C", "2026-10-01", "2026-10-01",
                               money("600"), money("0")),
                ActualTransfer("T002", "C", "B", "2026-10-02", "2026-10-04",
                               money("600"), money("0")),
            ],
            actual_inflows=[
                ActualInflow("IN1", "B", "2026-10-05", "2026-10-06",
                             money("500"), money("300")),
            ],
        )
        rev = revise_plan(scenario, base, execution, "2026-10-03").revision
        paid_r1 = {}
        for p in rev.remaining_gaps:
            paid_r1[p["flow_id"]] = p["shortfall"]
        self.assertNotIn("R1", paid_r1)  # 在途600 + 部分到账300 = 900 覆盖700

    def test_fee_hike_only_applies_after_effective_date(self):
        scenario = build_scenario()
        base = _base_orders(scenario)
        execution = Execution(
            scenario_id=scenario.scenario_id, plan_created_at=base.created_at,
            recorded_at=datetime.now().isoformat(timespec="seconds"),
            actual_transfers=[
                ActualTransfer("T001", "A", "C", "2026-10-01", "2026-10-01",
                               money("600"), money("0")),
                ActualTransfer("T002", "C", "B", "2026-10-02", "2026-10-04",
                               money("600"), money("0")),
            ],
            rule_changes=[
                RuleChange("A", "B", "2026-10-03", lag_days=0,
                           fee_rate=Decimal("0.05")),
            ],
        )
        rev = revise_plan(scenario, base, execution, "2026-10-03").revision
        # 在途 T002 手续费仍为 0（发起时条件），新增加急单按 5%
        self.assertEqual(
            [t.actual_fee for t in rev.committed_orders
             if t.order_id == "T002"], [money("0")])


class TestNoDeferOption(unittest.TestCase):
    def test_disallow_defer_keeps_p3_on_original_date(self):
        scenario = build_scenario()
        base = _base_orders(scenario)
        execution = Execution(
            scenario_id=scenario.scenario_id, plan_created_at=base.created_at,
            recorded_at=datetime.now().isoformat(timespec="seconds"),
            actual_transfers=[
                ActualTransfer("T001", "A", "C", "2026-10-01", "2026-10-01",
                               money("600"), money("0")),
                ActualTransfer("T002", "C", "B", "2026-10-02", "2026-10-04",
                               money("600"), money("0")),
            ],
            actual_inflows=[
                ActualInflow("IN1", "B", "2026-10-05", "2026-10-06",
                             money("500"), money("300")),
            ],
            rule_changes=[
                RuleChange("A", "B", "2026-10-03", lag_days=0,
                           fee_rate=Decimal("0.05")),
            ],
        )
        rev = revise_plan(scenario, base, execution, "2026-10-03",
                          allow_defer=False).revision
        self.assertEqual(rev.deferrals, [])
        # 不推迟时 X1 在原定日 10-08 即无法覆盖（被求解器放弃，不在新计划中覆盖）
        # 全程重放时 10-08 B 无款，缺口在原日期
        from cashflow.replan import replay_full
        full = replay_full(scenario, execution, rev)
        x1 = [g for g in rev.remaining_gaps if g["flow_id"] == "X1"]
        actual_gap = [p for p in full.gaps if p.flow_id == "X1"]
        self.assertTrue(actual_gap)
        self.assertEqual(actual_gap[0].date, "2026-10-08")


class TestSeparatePersistence(unittest.TestCase):
    def test_execution_and_revision_saved_separately(self):
        scenario = build_scenario()
        base = _base_orders(scenario)
        execution = Execution(
            scenario_id=scenario.scenario_id, plan_created_at=base.created_at,
            recorded_at=datetime.now().isoformat(timespec="seconds"),
            actual_transfers=[
                ActualTransfer("T001", "A", "C", "2026-10-01", "2026-10-01",
                               money("600"), money("0")),
                ActualTransfer("T002", "C", "B", "2026-10-02", "2026-10-04",
                               money("600"), money("0")),
            ],
        )
        rev = revise_plan(scenario, base, execution, "2026-10-03").revision
        with tempfile.TemporaryDirectory() as d:
            repo = Repository(Path(d))
            repo.save_scenario(scenario)
            repo.save_plan(base)
            ep = repo.save_execution(execution)
            rp = repo.save_revision(rev)
            self.assertIn("executions", str(ep))
            self.assertIn("revisions", str(rp))
            # 重开：分别读回，仍可全程对账
            loaded = repo.latest_revision("demo02")
            self.assertIsInstance(loaded, RevisedPlan)
            self.assertEqual(len(loaded.committed_orders), 2)
            self.assertEqual(loaded.cutoff_date, "2026-10-03")
            full = replay_full(repo.load_scenario("demo02"),
                               repo.latest_execution("demo02"), loaded)
            full.ledger.verify_conservation()


if __name__ == "__main__":
    unittest.main()
