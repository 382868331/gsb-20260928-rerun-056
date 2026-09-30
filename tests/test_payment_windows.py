import unittest
from decimal import Decimal

from cashflow import (
    Account, CashFlow, Execution, FlowKind, Priority, Scenario,
    TransferRule, money, solve,
)
from cashflow.plan import ActualTransfer, Plan
from cashflow.replan import FeeAmendment, combined_replay, replan


def scenario(window_end="2026-10-08", inflow_date="2026-10-08"):
    return Scenario(
        "window", "2026-10-01", "2026-10-10",
        [Account("A", "筹资户", money(0)), Account("B", "业务户")],
        [
            CashFlow("IN1", "B", inflow_date, FlowKind.INFLOW, money("800")),
            CashFlow("P1", "B", "2026-10-06", FlowKind.OUTFLOW, money("800"),
                     Priority.P3_OPTIONAL, "采购", "2026-10-06", window_end),
        ],
        [TransferRule("A", "B", lag_days=1)],
    )


class TestPaymentWindows(unittest.TestCase):
    def test_window_schedule_pays_full_and_preserves_original_due_date(self):
        result = solve(scenario())
        plan = result.plan
        self.assertEqual(plan.remaining_gaps, [])
        self.assertEqual(plan.feasibility_status, "feasible")
        self.assertEqual(plan.schedule_basis["scheduled_date"]["P1"],
                         "2026-10-08")
        self.assertEqual(plan.schedule_basis["original_due_date"]["P1"],
                         "2026-10-06")
        statuses = {(o["strategy"], o["rank"]): o["status"]
                    for o in plan.payment_strategy_options}
        self.assertIn(("strict_due", 0), statuses)
        self.assertTrue(any(o["status"] == "feasible" and
                            o["rescheduled_date"] == {"P1": "2026-10-08"}
                            for o in plan.payment_strategy_options))

    def test_strict_policy_keeps_gap_but_marks_strategy_not_adopted(self):
        result = solve(scenario(), eligible_strategies=["strict_due"])
        plan = result.plan
        self.assertEqual(plan.selected_strategy, "strict_due")
        self.assertEqual(plan.feasibility_status, "strategy_not_adopted")
        self.assertEqual(plan.remaining_gaps[0]["shortfall"], "800.00")
        self.assertTrue(any(o["status"] == "feasible"
                            for o in plan.payment_strategy_options))

    def test_exhausted_window_is_true_shortfall(self):
        plan = solve(scenario(window_end="2026-10-07",
                              inflow_date="2026-10-08")).plan
        self.assertEqual(plan.feasibility_status, "true_shortfall")
        self.assertTrue(all(o["status"] == "true_shortfall"
                            for o in plan.payment_strategy_options))

    def test_search_limit_does_not_claim_true_shortfall(self):
        from cashflow.scheduler import solve_with_windows
        result, _, status, _ = solve_with_windows(
            scenario(), max_combinations=2)
        self.assertEqual(status, "search_limit_exceeded")
        self.assertEqual(result.plan.feasibility_status, "search_limit_exceeded")

    def test_replan_window_inflow_delay_fee_and_combined_conservation(self):
        sc = Scenario(
            "rw", "2026-10-01", "2026-10-10",
            [Account("A", "基本户", money("1700")),
             Account("B", "业务户"), Account("C", "过桥户")],
            flows=[
                CashFlow("IN1", "B", "2026-10-04", FlowKind.INFLOW,
                         money("500")),
                CashFlow("IN2", "B", "2026-10-08", FlowKind.INFLOW,
                         money("400")),
                CashFlow("W1", "B", "2026-10-03", FlowKind.OUTFLOW,
                         money("600"), Priority.P1_CRITICAL, "工资"),
                CashFlow("X1", "B", "2026-10-06", FlowKind.OUTFLOW,
                         money("800"), Priority.P3_OPTIONAL, "采购",
                         "2026-10-06", "2026-10-09"),
            ],
            transfer_rules=[
                TransferRule("A", "B", lag_days=0, fee_rate=Decimal("0.02")),
                TransferRule("A", "C", lag_days=0),
                TransferRule("C", "B", lag_days=1),
            ],
        )
        original = solve(sc, schedule_windows=False).plan
        fixed = next(o for o in original.orders
                     if o.source == "A" and o.target == "C")
        execution = Execution(
            "rw", original.created_at, "2026-10-02T23:00:00",
            actual_transfers=[ActualTransfer(
                fixed.order_id, "A", "C", fixed.initiate_date,
                "2026-10-04", fixed.principal, fixed.fee)],
            initiated_order_ids=[fixed.order_id])
        rr = replan(
            sc, original, execution, "2026-10-02",
            amendments=[FeeAmendment("C", "B", "2026-10-03",
                                    fee_rate=Decimal("0.02"))],
            inflow_updates={"IN1": ("2026-10-05", money("300"))})
        self.assertEqual(rr.plan.remaining_gaps, [])
        self.assertTrue(any(
            o["rescheduled_date"].get("X1") == "2026-10-08" and
            o["status"] == "feasible"
            for o in rr.plan.payment_strategy_options))
        decision = next(d for d in rr.plan.payment_decisions
                        if d["flow_id"] == "X1")
        self.assertEqual(decision["shortfall"], "0.00")
        full = combined_replay(sc, execution, rr.plan)
        full.ledger.verify_conservation()
        self.assertEqual(full.ledger.available("A"),
                         rr.residual_replay.ledger.available("A"))


if __name__ == "__main__":
    unittest.main()
