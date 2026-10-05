"""依赖退回资金的后续付款研究测试。"""

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from cashflow import (
    Account, CashFlow, Execution, FlowKind, Plan, Priority,
    RefundRecord, Scenario, TransferRule, money, solve,
)
from cashflow.plan import ActualTransfer
from cashflow.refund_followup import (
    replan_after_refund_actuals, research_refund_payment_options,
)
from cashflow.replan import combined_replay


def build_scenario() -> Scenario:
    return Scenario(
        "rf-followup", "2026-10-01", "2026-10-08",
        [Account("A", "资金户", money("100")),
         Account("B", "业务户"),
         Account("C", "代收户")],
        [
            CashFlow("P0", "B", "2026-10-02", FlowKind.OUTFLOW,
                     money("100"), Priority.P1_CRITICAL, "已付款"),
            CashFlow("X1", "B", "2026-10-05", FlowKind.OUTFLOW,
                     money("60"), Priority.P2_NORMAL, "争用现金付款一",
                     window_start="2026-10-05",
                     window_end="2026-10-08"),
            CashFlow("X2", "B", "2026-10-05", FlowKind.OUTFLOW,
                     money("40"), Priority.P1_CRITICAL, "争用现金付款二",
                     window_start="2026-10-05",
                     window_end="2026-10-08"),
            CashFlow("X3", "A", "2026-10-08", FlowKind.OUTFLOW,
                     money("10"), Priority.P3_OPTIONAL, "不受影响付款",
                     window_start="2026-10-05",
                     window_end="2026-10-08"),
            CashFlow("X4", "C", "2026-10-08", FlowKind.OUTFLOW,
                     money("10"), Priority.P3_OPTIONAL, "分批退回直接付款",
                     window_start="2026-10-05",
                     window_end="2026-10-08"),
        ],
        [
            TransferRule("A", "B", lag_days=1),
            TransferRule("C", "B", lag_days=1),
            TransferRule("C", "A", lag_days=1),
        ],
    )


def cut_execution(plan: Plan) -> Execution:
    initial = plan.orders[0]
    execution = Execution(
        "rf-followup", plan.created_at, "2026-10-02T23:00:00",
        [ActualTransfer(initial.order_id, "A", "B",
                        "2026-10-01", "2026-10-02",
                        money("100"), money("0"))],
        [initial.order_id],
    )
    execution.record_refund(RefundRecord(
        "RF1", "P0", "B", "B", "2026-10-03", money("100"),
        note="预计全额退回"))
    execution.record_refund(RefundRecord(
        "RF2", "P0", "B", "C", "2026-10-05", money("20"),
        note="预计退到代收户"))
    execution.record_refund(RefundRecord(
        "RF3", "P0", "B", "C", "2026-10-06", money("10"),
        note="预计第三笔退回"))
    execution.record_refund(RefundRecord(
        "RF4", "P0", "B", "C", "2026-10-06", money("10"),
        note="预计第四笔退回"))
    execution.record_refund(RefundRecord(
        "RF5", "P0", "B", "C", "2026-10-06", money("10"),
        note="预计供 X3 调拨的退回"))
    return execution


class TestRefundFollowupResearch(unittest.TestCase):
    def test_multiple_forecasts_and_actual_contention_preserve_reload(self):
        scenario = build_scenario()
        original = solve(scenario).plan
        execution = cut_execution(original)

        options = research_refund_payment_options(
            scenario, original, execution, "2026-10-02",
            [
                {"name": "保守实际口径", "refunds": {}},
                {"name": "乐观准时全额", "refunds": {
                    "RF1": {"date": "2026-10-03", "amount": "100",
                            "return_account": "B", "fee_refunded": "0"},
                    "RF2": {"date": "2026-10-04", "amount": "20",
                            "return_account": "C", "fee_refunded": "0"},
                    "RF3": {"date": "2026-10-05", "amount": "10",
                            "return_account": "C", "fee_refunded": "0"},
                    "RF4": {"date": "2026-10-05", "amount": "10",
                            "return_account": "C", "fee_refunded": "0"},
                    "RF5": {"date": "2026-10-05", "amount": "10",
                            "return_account": "C", "fee_refunded": "0"}}},
                {"name": "迟到部分退回", "refunds": {
                    "RF1": {"date": "2026-10-04", "amount": "80",
                            "return_account": "B", "fee_refunded": "0"},
                    "RF2": {"date": "2026-10-06", "amount": "20",
                            "return_account": "C", "fee_refunded": "0"},
                    "RF3": {"date": "2026-10-06", "amount": "10",
                            "return_account": "C", "fee_refunded": "0"},
                    "RF4": {"date": "2026-10-06", "amount": "10",
                            "return_account": "C", "fee_refunded": "0"},
                    "RF5": {"date": "2026-10-06", "amount": "10",
                            "return_account": "C", "fee_refunded": "0"}}},
            ])

        by_name = {option["name"]: option for option in options}
        conservative = by_name["保守实际口径"]
        optimistic = by_name["乐观准时全额"]
        late_partial = by_name["迟到部分退回"]

        self.assertEqual(conservative["waiting_payments"],
                         ["X1", "X2", "X3", "X4"])
        self.assertIsNone(
            conservative["payment_actions"]["X1"]["earliest_executable_date"])
        self.assertEqual(
            optimistic["payment_actions"]["X1"]["earliest_executable_date"],
            "2026-10-05")
        self.assertEqual(
            optimistic["payment_actions"]["X2"]["earliest_executable_date"],
            "2026-10-05")
        self.assertEqual(
            late_partial["payment_actions"]["X2"]["earliest_executable_date"],
            "2026-10-05")
        self.assertEqual(
            late_partial["payment_actions"]["X1"]["earliest_executable_date"],
            "2026-10-07")
        self.assertEqual(late_partial["payment_actions"]["X1"]["status"],
                         "executable_late")

        forecast_plan = late_partial["plan"]
        self.assertTrue(
            forecast_plan.schedule_basis["refund_research"]
            ["forecast_not_available_balance"])

        execution.record_refund(RefundRecord(
            "RF1", "P0", "B", "B", "2026-10-03", money("90"),
            actual_date="2026-10-04", note="迟到且仅部分退回"))
        execution.record_refund(RefundRecord(
            "RF2", "P0", "B", "C", "2026-10-05", money("20"),
            actual_date="2026-10-06", note="分批退到 C 户"))
        execution.record_refund(RefundRecord(
            "RF3", "P0", "B", "C", "2026-10-06", money("10"),
            actual_date="2026-10-06", note="第三笔退回"))
        execution.record_refund(RefundRecord(
            "RF4", "P0", "B", "C", "2026-10-06", money("10"),
            actual_date="2026-10-06", note="第四笔退回"))
        execution.record_refund(RefundRecord(
            "RF5", "P0", "B", "C", "2026-10-06", money("10"),
            actual_date="2026-10-06", note="供 X3 调拨的退回"))

        revised, preserved = replan_after_refund_actuals(
            scenario, forecast_plan, execution, "2026-10-02",
            affected_payment_ids=["X1", "X2"],
            forecast_name="迟到部分退回",
            forecast_snapshot={
                "RF1": {"date": "2026-10-04", "amount": "80"},
                "RF2": {"date": "2026-10-06", "amount": "20"},
                "RF3": {"date": "2026-10-06", "amount": "10"},
                "RF4": {"date": "2026-10-06", "amount": "10"},
                "RF5": {"date": "2026-10-06", "amount": "10"}},
        )

        self.assertEqual([order.purpose for order in preserved], ["X3"])
        self.assertEqual(revised.revision_of, forecast_plan.created_at)
        self.assertEqual(
            revised.refund_revision["recalculated_scope"],
            "affected_unexecuted_payments_only")
        self.assertTrue(
            revised.refund_revision["issued_or_arrived_facts_irreversible"])
        actions = {
            item["flow_id"]: item
            for item in revised.payment_decisions}
        self.assertEqual(actions["X2"]["scheduled_date"], "2026-10-05")
        self.assertEqual(actions["X1"]["scheduled_date"], "2026-10-07")
        self.assertEqual(actions["X2"]["status"], "on_time")
        self.assertEqual(actions["X1"]["status"], "deferred")

        full = combined_replay(scenario, execution, revised)
        full.ledger.verify_conservation()
        self.assertEqual(full.ledger.available("A"), money("0"))
        self.assertEqual(full.ledger.available("B"), money("0"))
        self.assertEqual(full.ledger.available("C"), money("20"))
        self.assertEqual(full.ledger.total_paid, money("220"))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_path = root / "forecast.json"
            new_path = root / "actual.json"
            execution_path = root / "execution.json"
            forecast_plan.save(old_path)
            revised.save(new_path)
            execution.save(execution_path)

            restored_old = Plan.load(old_path)
            restored_new = Plan.load(new_path)
            restored_execution = Execution.load(execution_path)

            self.assertEqual(restored_new.revision_of,
                             restored_old.created_at)
            self.assertEqual(
                restored_old.refund_forecasts[0]["payment_actions"]["X1"]
                ["earliest_executable_date"], "2026-10-07")
            self.assertEqual(
                restored_new.refund_revision["affected_payment_ids"],
                ["X1", "X2"])
            self.assertEqual(len(restored_execution.refunds), 5)
            self.assertEqual(
                {refund.refund_id: refund.actual_date
                 for refund in restored_execution.refunds},
                {f"RF{i}": "2026-10-06" for i in range(2, 6)} |
                {"RF1": "2026-10-04"})


if __name__ == "__main__":
    unittest.main()
