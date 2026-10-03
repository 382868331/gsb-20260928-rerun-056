"""第四轮：退回资金与剩余支付重规划测试。"""

import unittest
from decimal import Decimal

from cashflow import (
    Account, CashFlow, FlowKind, Priority, Scenario, TransferRule,
    Execution, RefundRecord, money, solve,
)
from cashflow.plan import ActualInflow, ActualTransfer
from cashflow.refund import (
    compare_refund_strategies, record_refunds, refund_disposition,
)
from cashflow.replan import build_prefix, combined_replay, replan


def build() -> Scenario:
    return Scenario(
        "rf", "2026-10-01", "2026-10-12",
        [Account("A", "资金户", money("1000")),
         Account("B", "业务户")],
        flows=[
            CashFlow("S1", "B", "2026-10-03", FlowKind.OUTFLOW, money("500"),
                     Priority.P2_NORMAL, "货款（后部分退回）"),
            CashFlow("S2", "B", "2026-10-07", FlowKind.OUTFLOW, money("300"),
                     Priority.P1_CRITICAL, "工资"),
            CashFlow("IN1", "B", "2026-10-10", FlowKind.INFLOW, money("200")),
        ],
        transfer_rules=[
            TransferRule("A", "B", lag_days=1, fee_rate=Decimal("0.01")),
        ],
    )


def executed_execution(plan) -> Execution:
    """T001 已发起（A->B 500，费 5），S1 已付；随后发生退回。"""
    t1 = next(o for o in plan.orders if o.initiate_date <= "2026-10-04")
    execution = Execution(
        scenario_id="rf", plan_created_at=plan.created_at,
        recorded_at="2026-10-04T23:00:00",
        actual_transfers=[
            ActualTransfer(t1.order_id, "A", "B", t1.initiate_date,
                           t1.arrival_date, t1.principal, t1.fee),
        ],
        initiated_order_ids=[t1.order_id],
    )
    record_refunds(execution, [
        RefundRecord("RF1", "S1", "B", "B", "2026-10-04", money("200"),
                     actual_date="2026-10-04", note="部分退回第一笔"),
        RefundRecord("RF2", "S1", "B", "B", "2026-10-08", money("150"),
                     note="预计退回，未到达"),
    ])
    return execution


class TestRefundRecords(unittest.TestCase):
    def test_duplicate_and_correction_do_not_double_balance(self):
        execution = Execution("rf", "p", "2026-10-04T23:00:00")
        r1 = RefundRecord("RF1", "S1", "B", "B", "2026-10-04",
                          money("200"), actual_date="2026-10-04")
        notes = record_refunds(execution, [r1, r1])  # 重复通知
        self.assertEqual([n["action"] for n in notes],
                         ["recorded", "duplicate_ignored"])
        self.assertEqual(len(execution.refunds), 1)
        # 同一笔款的更正：金额改为 180，覆盖而不是新增
        notes = record_refunds(execution, [
            RefundRecord("RF1", "S1", "B", "B", "2026-10-04",
                         money("180"), actual_date="2026-10-04")])
        self.assertEqual(notes[0]["action"], "corrected")
        self.assertEqual(len(execution.refunds), 1)
        self.assertEqual(execution.refunds[0].amount, money("180"))
        # 重放只入账一次
        sc = build()
        plan = solve(sc).plan
        execution.actual_transfers = [
            ActualTransfer(plan.orders[0].order_id, "A", "B",
                           plan.orders[0].initiate_date,
                           plan.orders[0].arrival_date,
                           plan.orders[0].principal, plan.orders[0].fee)]
        execution.initiated_order_ids = [plan.orders[0].order_id]
        prefix = build_prefix(sc, execution, "2026-10-04")
        refund_events = [e for e in prefix.ledger.log
                         if e["event"] == "inflow" and
                         e["flow_id"] == "REFUND-RF1"]
        self.assertEqual(len(refund_events), 1)
        self.assertEqual(prefix.ledger.available("B"), money("180"))

    def test_expected_refund_not_counted_as_available(self):
        sc = build()
        plan = solve(sc).plan
        execution = executed_execution(plan)
        rr = replan(sc, plan, execution, "2026-10-04")
        # RF1 已到达：进入残差期初余额；RF2 未到达：不产生任何入账
        self.assertEqual(rr.prefix_replay.ledger.available("B"), money("200"))
        refund_entries = {r["refund_id"]: r for r in rr.plan.refund_records}
        self.assertTrue(refund_entries["RF1"]["usable_as_funds"])
        self.assertFalse(refund_entries["RF2"]["usable_as_funds"])
        self.assertEqual(refund_entries["RF2"]["status"],
                         "expected_not_usable")
        self.assertEqual([r["refund_id"] for r in rr.plan.expected_refunds],
                         ["RF2"])
        # 残差重放中不存在 RF2 的入账事件
        inflow_ids = [e["flow_id"] for e in rr.residual_replay.ledger.log
                      if e["event"] == "inflow"]
        self.assertNotIn("REFUND-RF2", inflow_ids)
        rr.residual_replay.ledger.verify_conservation()

    def test_cross_account_return_and_fee_not_refunded(self):
        sc = build()
        plan = solve(sc).plan
        execution = executed_execution(plan)
        # 退回到 A 户（跨账户返回），手续费 5 不退
        record_refunds(execution, [
            RefundRecord("RF3", "S1", "B", "A", "2026-10-05",
                         money("50"), actual_date="2026-10-05")])
        prefix = build_prefix(sc, execution, "2026-10-05")
        self.assertEqual(prefix.ledger.available("A"),
                         money("1000") - money("500") - money("5") +
                         money("50"))
        self.assertEqual(prefix.ledger.total_fees, money("5"))  # 手续费不退
        prefix.ledger.verify_conservation()

    def test_disposition_distinguishes_paid_transit_received_usable(self):
        sc = build()
        plan = solve(sc).plan
        execution = executed_execution(plan)
        disp = refund_disposition(sc, execution, "2026-10-04")
        entry = next(p for p in disp["payments"] if p["flow_id"] == "S1")
        self.assertEqual(entry["paid"], "500.00")
        self.assertEqual(entry["payee_kept"], "300.00")           # 收款方净留存
        self.assertEqual(entry["refund_arrived_usable"], "200.00")  # 可再次使用
        self.assertEqual(entry["refund_expected_not_usable"], "150.00")
        self.assertEqual(entry["fee_refunded"], "0.00")
        self.assertEqual(disp["balances"]["B"], "200.00")


class TestRefundReplan(unittest.TestCase):
    def test_replan_keeps_refund_basis_and_combined_reconciles(self):
        import tempfile
        from pathlib import Path
        from cashflow.repo import Repository

        sc = build()
        plan = solve(sc).plan
        execution = executed_execution(plan)
        # RF2 实际于 10-09 迟到到达（预计 10-08），更正记录
        record_refunds(execution, [
            RefundRecord("RF2", "S1", "B", "B", "2026-10-08",
                         money("150"), actual_date="2026-10-09",
                         note="退回迟到一天")])
        rr = replan(sc, plan, execution, "2026-10-04")
        basis = {b["refund_id"]: b for b in
                 rr.plan.schedule_basis["refund_basis"]}
        self.assertEqual(basis["RF1"]["date"], "2026-10-04")
        self.assertEqual(basis["RF2"]["date"], "2026-10-09")
        self.assertEqual(basis["RF2"]["status"], "arrived")
        full = combined_replay(sc, execution, rr.plan)
        full.ledger.verify_conservation()
        # 原调拨只发起一次，退回只入账一次
        logs = full.ledger.log
        self.assertEqual(len([e for e in logs
                              if e["event"] == "transfer_initiate"]),
                         len({e["order_id"] for e in logs
                              if e["event"] == "transfer_initiate"}))
        self.assertEqual(len([e for e in logs if e["event"] == "inflow" and
                              e["flow_id"] == "REFUND-RF2"]), 1)
        with tempfile.TemporaryDirectory() as d:
            repo = Repository(Path(d))
            repo.save_plan(rr.plan)
            reloaded = repo.latest_plan("rf")
            self.assertEqual(reloaded.refund_records, rr.plan.refund_records)
            self.assertEqual(reloaded.schedule_basis["refund_basis"],
                             rr.plan.schedule_basis["refund_basis"])

    def test_compare_wait_vs_alternative_account(self):
        sc = build()
        # A 资金收紧：工资必须等退回或跨户筹资
        for a in sc.accounts:
            if a.account_id == "A":
                object.__setattr__(a, "opening_balance", money("600"))
        plan = solve(sc).plan
        execution = executed_execution(plan)
        options = compare_refund_strategies(
            sc, plan, execution, "2026-10-04")
        self.assertEqual([o["name"] for o in options],
                         ["等待退回", "替代账户安排"])
        wait, alt = options
        self.assertEqual(wait["assumed_refunds"],
                         [{"refund_id": "RF2",
                           "assumed_date": "2026-10-08"}])
        self.assertEqual(alt["assumed_refunds"], [])
        # 两个方案都报告剩余缺口与费用，且保留原约束
        for o in options:
            self.assertIn("remaining_gap", o)
            self.assertIn("total_fee", o)
            self.assertTrue(o["constraints_preserved"]["transfer_rules"])
        self.assertEqual(Decimal(wait["fee_premium"]), Decimal("0"))
        self.assertGreaterEqual(Decimal(alt["total_fee"]),
                                Decimal(wait["total_fee"]))

    def test_partial_receipt_then_refund_combo(self):
        """部分到账后再退回 + 退回迟到 + 跨账户返回 + 费用不退的组合。"""
        sc = build()
        plan = solve(sc).plan
        t1 = plan.orders[0]
        execution = Execution(
            "rf", plan.created_at, "2026-10-05T23:00:00",
            [ActualTransfer(t1.order_id, "A", "B", t1.initiate_date,
                            t1.arrival_date, t1.principal, t1.fee)],
            [t1.order_id],
            # IN1 原定 10-10 到 200，实际只到 120
            actual_inflows=[ActualInflow("IN1", "B", "2026-10-10",
                                         "2026-10-10", money("120"))],
        )
        record_refunds(execution, [
            # 部分退回，迟到两天，跨账户返回到 A，手续费不退
            RefundRecord("RF1", "S1", "B", "A", "2026-10-04",
                         money("200"), actual_date="2026-10-06"),
            # 第二笔仍预计，不可用
            RefundRecord("RF2", "S1", "B", "B", "2026-10-11", money("100")),
        ])
        rr = replan(sc, plan, execution, "2026-10-05",
                    inflow_updates={"IN1": ("2026-10-10", money("120"))})
        # RF1 于 10-06 入 A 户；RF2 不可用
        residual_inflows = {e["flow_id"]: e for e in
                            rr.residual_replay.ledger.log
                            if e["event"] == "inflow"}
        self.assertEqual(residual_inflows["REFUND-RF1"]["account"], "A")
        self.assertEqual(residual_inflows["REFUND-RF1"]["date"], "2026-10-06")
        self.assertNotIn("REFUND-RF2", residual_inflows)
        full = combined_replay(
            sc, execution, rr.plan,
            inflow_updates={"IN1": ("2026-10-10", money("120"))})
        full.ledger.verify_conservation()
        self.assertEqual(full.ledger.total_fees,
                         rr.plan.committed_fee +
                         sum((o.fee for o in rr.plan.orders), Decimal("0")))

    def test_withdraw_releases_only_unexecuted_then_refund_replan(self):
        from cashflow.adoption import (adopt_partial_arrangements,
                                       withdraw_arrangements)
        sc = build()
        plan = solve(sc).plan
        adopted = adopt_partial_arrangements(
            sc, plan, [plan.orders[-1].order_id], ["S2"])
        withdrawn = withdraw_arrangements(adopted, [plan.orders[-1].order_id])
        self.assertEqual(withdrawn.locked_orders, [])
        execution = executed_execution(plan)
        rr = replan(sc, withdrawn, execution, "2026-10-04")
        # 撤回的候选不再占用；退回已到达部分可用，预计部分不可用
        self.assertEqual(rr.plan.locked_orders, [])
        self.assertIn(plan.orders[-1].order_id, rr.plan.withdrawn_order_ids)
        full = combined_replay(sc, execution, rr.plan)
        full.ledger.verify_conservation()


if __name__ == "__main__":
    unittest.main()
