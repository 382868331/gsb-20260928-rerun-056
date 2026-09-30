import unittest
from decimal import Decimal

from cashflow import (
    Account, CashFlow, FlowKind, Priority, Scenario, TransferRule, Execution,
    money, solve,
)
from cashflow.plan import ActualTransfer
from cashflow.replan import replan


class TestLatePaid(unittest.TestCase):
    def test_past_due_outflow_carried_to_residual_and_paid(self):
        # 工资 10-03 到期；cutoff=10-03 日终仍未付（A->B lag=1 且未提前发起），
        # 残差首日补付，决策记为 late_paid 并保留原期限。
        sc = Scenario(
            "late", "2026-10-01", "2026-10-06",
            [Account("A", "a", money("1000")), Account("B", "b")],
            flows=[CashFlow("W1", "B", "2026-10-03", FlowKind.OUTFLOW,
                            money("500"), Priority.P1_CRITICAL, "工资")],
            transfer_rules=[TransferRule("A", "B", lag_days=0)],
        )
        plan = solve(sc).plan
        execution = Execution(scenario_id="late", plan_created_at=plan.created_at,
                              recorded_at="2026-10-03T23:00:00")
        rr = replan(sc, plan, execution, "2026-10-03")
        d = next(x for x in rr.plan.payment_decisions if x["flow_id"] == "W1")
        self.assertEqual(d["status"], "late_paid")
        self.assertEqual(d["original_due_date"], "2026-10-03")
        self.assertEqual(d["scheduled_date"], "2026-10-04")
        self.assertEqual(d["paid"], "500.00")
        self.assertIn("原期限 2026-10-03 已错过", d["reason"])


if __name__ == "__main__":
    unittest.main()
