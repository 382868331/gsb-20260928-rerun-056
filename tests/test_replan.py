"""第二轮：调拨延误后的修订重规划测试。"""

import unittest
from decimal import Decimal

from cashflow import (
    Account, CashFlow, FlowKind, Priority, Scenario, TransferRule,
    Execution, money, solve,
)
from cashflow.plan import ActualInflow, ActualTransfer, Plan
from cashflow.replan import FeeAmendment, build_prefix, replan, combined_replay


def build() -> Scenario:
    return Scenario(
        "r", "2026-10-01", "2026-10-10",
        [Account("A", "基本户", money("1700")),
         Account("B", "业务户"), Account("C", "过桥户")],
        flows=[
            CashFlow("IN1", "B", "2026-10-04", FlowKind.INFLOW, money("500")),
            CashFlow("IN2", "B", "2026-10-08", FlowKind.INFLOW, money("400")),
            CashFlow("W1", "B", "2026-10-03", FlowKind.OUTFLOW, money("600"),
                     Priority.P1_CRITICAL, "工资"),
            CashFlow("R1", "B", "2026-10-05", FlowKind.OUTFLOW, money("700"),
                     Priority.P2_NORMAL, "房租"),
            CashFlow("X1", "B", "2026-10-06", FlowKind.OUTFLOW, money("800"),
                     Priority.P3_OPTIONAL, "可选采购"),
        ],
        transfer_rules=[
            TransferRule("A", "B", lag_days=0, fee_rate=Decimal("0.02")),
            TransferRule("A", "C", lag_days=0),
            TransferRule("C", "B", lag_days=1),
        ],
    )


def delayed_execution(plan: Plan) -> Execution:
    first = next(o for o in plan.orders if o.source == "A" and o.target == "C")
    return Execution(
        scenario_id="r", plan_created_at=plan.created_at,
        recorded_at="2026-10-02T23:00:00",
        actual_transfers=[
            ActualTransfer(first.order_id, "A", "C",
                           first.initiate_date, "2026-10-04",
                           first.principal, first.fee),
        ],
        initiated_order_ids=[first.order_id],
        actual_inflows=[],
    )


class TestReplan(unittest.TestCase):
    def test_fixed_in_transit_not_debited_twice(self):
        sc = build()
        plan = solve(sc).plan
        execution = delayed_execution(plan)
        prefix = build_prefix(sc, execution, "2026-10-02")
        # A 已为在途 600 扣过款，余额 1100，不会再扣一次
        self.assertEqual(prefix.ledger.available("A"), money("1100"))
        self.assertIn(execution.actual_transfers[0].order_id,
                      prefix.ledger.in_transit)

        rr = replan(sc, plan, execution, "2026-10-02",
                    amendments=[FeeAmendment("C", "B", "2026-10-03",
                                             fee_rate=Decimal("0.02"))],
                    inflow_updates={"IN1": ("2026-10-04", money("300"))})
        revised = rr.plan
        # 修订新增调拨全部是新单号；FIXED 单不收费（费用已在 cutoff 前扣付）
        self.assertTrue(all(o.order_id.startswith("R") for o in revised.orders))
        self.assertTrue(all(o.fee == 0 for o in revised.fixed_transit))
        self.assertEqual(revised.revision_of, plan.created_at)
        # P1 工资必须准时覆盖，且走了收费路径（费用 > 0）
        self.assertEqual(revised.objective[0], 0)
        self.assertGreater(revised.total_fee, 0)
        # 残差账资金守恒
        rr.residual_replay.ledger.verify_conservation()

    def test_payment_decisions_record_defer_and_timely_premium(self):
        sc = build()
        plan = solve(sc).plan
        rr = replan(sc, plan, delayed_execution(plan), "2026-10-02",
                    amendments=[FeeAmendment("C", "B", "2026-10-03",
                                             fee_rate=Decimal("0.02"))],
                    inflow_updates={"IN1": ("2026-10-04", money("300"))},
                    chain_quote={
                        "W1": {"timely_fee": money("12"), "cheap_fee": 0,
                               "extra_fee": money("12")},
                        "R1": {"timely_fee": money("8"), "cheap_fee": 0,
                               "extra_fee": money("8")}})
        by_flow = {d["flow_id"]: d for d in rr.plan.payment_decisions}
        self.assertEqual(by_flow["W1"]["status"], "on_time")
        self.assertIn("更贵但及时", by_flow["W1"]["reason"])
        self.assertEqual(by_flow["X1"]["status"], "deferred")
        self.assertEqual(by_flow["X1"]["scheduled_date"], "2026-10-08")
        self.assertIn("可选支付", by_flow["X1"]["reason"])
        self.assertEqual(rr.plan.remaining_gaps, [])

    def test_no_defer_p3_leaves_unmet_with_reason(self):
        sc = build()
        # 资金更紧：A 只有 1300，房租覆盖后无法支撑 P3
        for a in sc.accounts:
            if a.account_id == "A":
                object.__setattr__(a, "opening_balance", money("1300"))
        plan = solve(sc).plan
        rr = replan(sc, plan, delayed_execution(plan), "2026-10-02",
                    amendments=[FeeAmendment("C", "B", "2026-10-03",
                                             fee_rate=Decimal("0.02"))],
                    inflow_updates={"IN1": ("2026-10-04", money("300"))},
                    allow_defer_p3=False)
        statuses = {d["flow_id"]: d["status"] for d in rr.plan.payment_decisions}
        self.assertIn(statuses["X1"], ("unmet", "deferred"))
        if statuses["X1"] == "unmet":
            self.assertTrue(any(g["flow_id"] == "X1"
                                for g in rr.plan.remaining_gaps))

    def test_fee_amendment_only_applies_after_effective_date(self):
        sc = build()
        plan = solve(sc).plan
        execution = delayed_execution(plan)
        rr = replan(sc, plan, execution, "2026-10-02",
                    amendments=[FeeAmendment("C", "B", "2026-10-03",
                                             fee_rate=Decimal("0.02"))],
                    inflow_updates={"IN1": ("2026-10-04", money("300"))})
        cb = next(o for o in rr.plan.orders if o.source == "C" and o.target == "B")
        self.assertEqual(cb.fee, money(cb.principal * Decimal("0.02")))
        amd = rr.plan.rule_amendments[0]
        self.assertEqual((amd["source"], amd["target"]), ("C", "B"))

    def test_partial_inflow_missing_is_not_counted_early(self):
        sc = build()
        plan = solve(sc).plan
        execution = delayed_execution(plan)
        # 原定 500 回款只到 300，且推迟到 10-05
        rr = replan(sc, plan, execution, "2026-10-02",
                    inflow_updates={"IN1": ("2026-10-05", money("300"))},
                    allow_defer_p3=False)
        expected = {f["flow_id"]: f["amount"]
                    for f in rr.plan.expected_inflows}
        self.assertEqual(expected["IN1"], "300.00")
        self.assertEqual(expected["IN2"], "400.00")

    def test_combined_replay_conservation_and_persistence(self):
        import tempfile
        from pathlib import Path
        from cashflow.repo import Repository
        sc = build()
        plan = solve(sc).plan
        execution = delayed_execution(plan)
        rr = replan(sc, plan, execution, "2026-10-02",
                    inflow_updates={"IN1": ("2026-10-04", money("300"))})
        full = combined_replay(sc, execution, rr.plan,
                               inflow_updates={"IN1": ("2026-10-04",
                                                      money("300"))})
        full.ledger.verify_conservation()
        # 10-01..02 在途，10-04 到账后不再在途；A 未被二次扣款
        self.assertEqual(full.ledger.available("A"),
                         rr.residual_replay.ledger.available("A"))
        with tempfile.TemporaryDirectory() as d:
            repo = Repository(Path(d))
            repo.save_execution(execution)
            repo.save_plan(rr.plan)
            reloaded = repo.latest_plan("r")
            self.assertEqual(reloaded.cutoff_date, "2026-10-02")
            self.assertEqual(len(reloaded.fixed_transit), 1)
            self.assertEqual(reloaded.payment_decisions,
                             rr.plan.payment_decisions)


if __name__ == "__main__":
    unittest.main()
