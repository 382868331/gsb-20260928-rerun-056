import unittest
from decimal import Decimal

from cashflow import (
    Account, CashFlow, Execution, FlowKind, Priority, Scenario,
    TransferRule, combined_replay, money,
)
from cashflow.adoption import (
    adopt_partial_arrangements, compare_partial_adoptions,
    replay_with_adoptions, withdraw_arrangements,
)
from cashflow.plan import ActualTransfer, Plan
from cashflow.replan import replan
from cashflow.scheduler import solve


def build_scenario():
    return Scenario(
        "partial", "2026-10-01", "2026-10-08",
        [Account("A", "筹资户", money("1000")),
         Account("B", "业务户")],
        [
            CashFlow("P1", "B", "2026-10-02", FlowKind.OUTFLOW,
                     money("400"), Priority.P1_CRITICAL, "工资"),
            CashFlow("P2", "B", "2026-10-04", FlowKind.OUTFLOW,
                     money("400"), Priority.P2_NORMAL, "货款",
                     "2026-10-04", "2026-10-06"),
            CashFlow("IN1", "B", "2026-10-06", FlowKind.INFLOW,
                     money("300"), memo="迟到收入"),
        ],
        [TransferRule("A", "B", lag_days=1, fee_rate=Decimal("0.01"))])


class TestPartialAdoption(unittest.TestCase):
    def test_partial_order_locks_shared_funds_and_replans_remainder(self):
        scenario = build_scenario()
        candidate = solve(scenario).plan
        original_fee = candidate.total_fee
        adopted = adopt_partial_arrangements(
            scenario, candidate, ["T001"], ["P1"],
            option=next(o for o in candidate.payment_strategy_options
                        if o["strategy"] == "window_priority" and
                        o["rank"] == 2))

        self.assertEqual([o.order_id for o in adopted.locked_orders], ["T001"])
        self.assertEqual(len(adopted.orders), 1)
        new_order = adopted.orders[0]
        self.assertTrue(new_order.order_id.startswith("A"))
        self.assertEqual(new_order.principal, money("100"))
        self.assertEqual(adopted.remaining_gaps, [])

        result = replay_with_adoptions(scenario, adopted, {"P2": "2026-10-06"})
        result.ledger.verify_conservation()
        self.assertEqual(result.ledger.available("A"), money("495"))
        self.assertEqual(result.ledger.total_fees, money("5"))
        self.assertEqual(adopted.total_fee, money("5"))
        self.assertLess(adopted.total_fee, original_fee)

    def test_two_partial_adoption_options_preserve_scope_constraints_and_gap(self):
        scenario = build_scenario()
        candidate = solve(scenario).plan
        strict = next(o for o in candidate.payment_strategy_options
                      if o["strategy"] == "strict_due" and o["rank"] == 0)
        deferred = next(o for o in candidate.payment_strategy_options
                        if o["strategy"] == "window_priority" and
                        o["rank"] == 2)
        comparisons = compare_partial_adoptions(scenario, candidate, [
            {"name": "保工资并准时付货款", "option": strict,
             "order_ids": ["T001"], "flow_ids": ["P1"]},
            {"name": "保工资并等迟到收入", "option": deferred,
             "order_ids": ["T001"], "flow_ids": ["P1"]},
        ])

        self.assertEqual([o["name"] for o in comparisons],
                         ["保工资并准时付货款", "保工资并等迟到收入"])
        self.assertEqual(comparisons[0]["confirmed_scope"]["order_ids"],
                         ["T001"])
        self.assertTrue(comparisons[0]["constraints_preserved"]["payment_window"])
        self.assertEqual(comparisons[0]["remaining_fee"], "4.00")
        self.assertEqual(comparisons[0]["total_expected_fee"], "8.00")
        self.assertEqual(comparisons[0]["fee_premium"], "3.00")
        self.assertTrue(comparisons[0]["increases_fee"])
        self.assertEqual(comparisons[0]["delayed_remaining_payments"], {})
        self.assertEqual(comparisons[1]["remaining_fee"], "1.00")
        self.assertEqual(comparisons[1]["total_expected_fee"], "5.00")
        self.assertEqual(comparisons[1]["fee_premium"], "0.00")
        self.assertFalse(comparisons[1]["increases_fee"])
        self.assertEqual(comparisons[1]["delayed_remaining_payments"],
                         {"P2": "2026-10-06"})
        self.assertFalse(any(o["loses_feasible_arrangement"]
                             for o in comparisons))

    def test_withdraw_only_uninitiated_order_and_explains_transit(self):
        scenario = build_scenario()
        candidate = solve(scenario).plan
        adopted = adopt_partial_arrangements(
            scenario, candidate, ["T002"], ["P2"])
        withdrawn = withdraw_arrangements(adopted, ["T002"])
        self.assertEqual(withdrawn.locked_orders, [])
        self.assertIn("T002", withdrawn.withdrawn_order_ids)
        self.assertEqual(withdrawn.confirmed_arrangements[0]["status"],
                         "withdrawn")

    def test_initiated_transfer_survives_withdraw_and_replan_keeps_fee(self):
        scenario = build_scenario()
        candidate = solve(scenario).plan
        adopted = adopt_partial_arrangements(
            scenario, candidate, ["T001"], ["P1"])
        execution = Execution(
            "partial", candidate.created_at, "2026-10-01T23:00:00",
            [ActualTransfer("T001", "A", "B", "2026-10-01",
                            "2026-10-02", money("400"), money("4"))],
            ["T001"])
        withdrawn = withdraw_arrangements(adopted, ["T001"], execution,
                                          "2026-10-01")
        self.assertEqual([o.order_id for o in withdrawn.locked_orders],
                         ["T001"])
        note = withdrawn.schedule_basis["withdrawal_notes"][0]
        self.assertEqual(note["status"], "in_transit_or_executed")
        self.assertIn("在途", note["handling"])

        revised = replan(scenario, withdrawn, execution, "2026-10-01",
                         inflow_updates={"IN1": ("2026-10-06", money("180"))},
                         outflow_shifts={"P2": "2026-10-06"}).plan
        self.assertEqual(revised.fixed_transit[0].order_id, "FIXED-T001")
        self.assertEqual([o.order_id for o in revised.locked_orders], [])
        self.assertEqual(revised.committed_fee, money("4"))
        self.assertGreaterEqual(revised.total_fee, money("4"))
        full = combined_replay(
            scenario, execution, revised,
            inflow_updates={"IN1": ("2026-10-06", money("180"))})
        full.ledger.verify_conservation()
        self.assertEqual(full.ledger.total_fees, revised.total_fee)
        initiation_logs = [e for e in full.ledger.log
                           if e["event"] == "transfer_initiate" and
                           e["order_id"] == "T001"]
        self.assertEqual(len(initiation_logs), 1)

    def test_reload_keeps_confirmed_scope_without_duplicate_occupation(self):
        import tempfile
        from pathlib import Path
        from cashflow.repo import Repository

        scenario = build_scenario()
        candidate = solve(scenario).plan
        adopted = adopt_partial_arrangements(
            scenario, candidate, ["T001"], ["P1"],
            option=next(o for o in candidate.payment_strategy_options
                        if o["strategy"] == "window_priority" and
                        o["rank"] == 2))
        with tempfile.TemporaryDirectory() as directory:
            Repository(Path(directory)).save_plan(adopted)
            reloaded = Repository(Path(directory)).latest_plan("partial")
            result = replay_with_adoptions(scenario, reloaded,
                                           {"P2": "2026-10-06"})
            result.ledger.verify_conservation()
            ids = [e["order_id"] for e in result.ledger.log
                   if e["event"] == "transfer_initiate"]
            self.assertEqual(ids.count("T001"), 1)


if __name__ == "__main__":
    unittest.main()
