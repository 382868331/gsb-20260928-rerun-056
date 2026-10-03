"""第四轮：退回与拒付的实际记录测试。"""

import unittest
from decimal import Decimal

from cashflow import (
    Account, CashFlow, FlowKind, Priority, Scenario, TransferRule,
    Execution, money, solve,
)
from cashflow.plan import ActualTransfer, RefundRecord
from cashflow.replan import build_prefix, combined_replay, replan
from cashflow.refunds import (
    allocate_arrived_refund, compare_refund_usage, normalize_refunds,
    refund_view, split_refunds,
)


def build() -> Scenario:
    return Scenario(
        "rf", "2026-10-01", "2026-10-12",
        [Account("A", "基本户", money("500")), Account("B", "业务户")],
        flows=[
            CashFlow("W1", "B", "2026-10-03", FlowKind.OUTFLOW,
                     money("400"), Priority.P1_CRITICAL, "工资"),
            CashFlow("X1", "B", "2026-10-06", FlowKind.OUTFLOW,
                     money("300"), Priority.P3_OPTIONAL, "可选采购"),
        ],
        transfer_rules=[
            TransferRule("A", "B", lag_days=1, fee_rate=Decimal("0.02")),
        ],
    )


def executed_transfer(plan) -> ActualTransfer:
    o = plan.orders[0]
    return ActualTransfer(o.order_id, o.source, o.target,
                          o.initiate_date, o.arrival_date,
                          o.principal, o.fee)


class TestRefundRecords(unittest.TestCase):
    def test_partial_rejection_in_transit_fee_not_refunded(self):
        """在途调拨被部分拒付：退回部分从在途冲回，手续费不退。"""
        sc = Scenario(
            "rf2", "2026-10-01", "2026-10-10",
            [Account("A", "基本户", money("500")), Account("B", "业务户")],
            flows=[CashFlow("W1", "B", "2026-10-04", FlowKind.OUTFLOW,
                            money("400"), Priority.P1_CRITICAL, "工资")],
            transfer_rules=[
                TransferRule("A", "B", lag_days=2, fee_rate=Decimal("0.02"))])
        plan = solve(sc).plan
        order = plan.orders[0]
        execution = Execution(
            "rf2", plan.created_at, "2026-10-03T10:00:00",
            actual_transfers=[ActualTransfer(
                order.order_id, "A", "B", order.initiate_date,
                order.arrival_date, order.principal, order.fee)],
            initiated_order_ids=[order.order_id],
            refunds=[RefundRecord(
                "RF1", "order", order.order_id, "rejection",
                money("150"), "2026-10-03", "A")])
        prefix = build_prefix(sc, execution, "2026-10-03")
        led = prefix.ledger
        # 原付款 400 + 手续费 8 已扣；退回 150 回到 A，在途剩 250
        self.assertEqual(led.available("A"), money("500") - 408 + 150)
        self.assertEqual(led.in_transit[order.order_id].principal, money("250"))
        self.assertEqual(led.total_fees, money("8"))       # 手续费不退
        self.assertEqual(led.refund_from_transit, money("150"))
        led.verify_conservation()
        # 重规划：固定在途只剩 250，原安排不回滚
        rr = replan(sc, plan, execution, "2026-10-03")
        fixed = {o.order_id: o for o in rr.plan.fixed_transit}
        self.assertEqual(fixed[f"FIXED-{order.order_id}"].principal, money("250"))
        self.assertEqual(rr.plan.refund_basis[0]["refund_id"], "RF1")
        full = combined_replay(sc, execution, rr.plan)
        full.ledger.verify_conservation()
        # 原支付最终去向：B 只收到 250，其余 150 退回 A
        self.assertEqual(full.ledger.total_refunds, money("150"))

    def test_expected_refund_not_usable_before_arrival(self):
        """预计退回不能提前当作可用资金；到账后才进入可用余额。"""
        sc = build()
        plan = solve(sc).plan
        t = executed_transfer(plan)
        execution = Execution(
            "rf", plan.created_at, "2026-10-03T10:00:00",
            actual_transfers=[t], initiated_order_ids=[t.order_id],
            refunds=[RefundRecord(
                "RF9", "payment", "W1", "refund",
                money("250"), "2026-10-04", "A")])
        rr = replan(sc, plan, execution, "2026-10-03")
        # 退回预计 10-09 到账：列示为预计，不作为可用资金
        self.assertEqual([r["refund_id"] for r in rr.plan.expected_refunds],
                         ["RF9"])
        self.assertFalse(rr.plan.expected_refunds[0]["usable"])
        self.assertEqual(rr.plan.refund_basis, [])
        # A 只有 92，无法为 X1 筹资 300，缺口保留
        gaps = {g["flow_id"]: g["shortfall"] for g in rr.plan.remaining_gaps}
        self.assertIn("X1", gaps)
        # 退回到达后再重规划：成为实际到账依据，缺口消失
        rr2 = replan(sc, plan, execution, "2026-10-04")
        self.assertEqual([b["refund_id"] for b in rr2.plan.refund_basis], ["RF9"])
        self.assertEqual(rr2.plan.expected_refunds, [])
        gaps2 = {g["flow_id"] for g in rr2.plan.remaining_gaps}
        self.assertNotIn("X1", gaps2)

    def test_duplicate_and_correction_do_not_double_count(self):
        """重复通知与同一笔款的更正不形成第二份余额；分次到账累加。"""
        sc = build()
        plan = solve(sc).plan
        t = executed_transfer(plan)
        refunds = [
            RefundRecord("RF1", "payment", "W1", "refund",
                         money("100"), "2026-10-05", "A"),
            RefundRecord("RF1", "payment", "W1", "refund",
                         money("100"), "2026-10-05", "A",
                         notice="duplicate"),
            RefundRecord("RF2", "payment", "W1", "refund",
                         money("50"), "2026-10-06", "B"),
            RefundRecord("RF2", "payment", "W1", "refund",
                         money("60"), "2026-10-06", "B",
                         notice="correction"),
        ]
        effective, notes = normalize_refunds(refunds)
        self.assertEqual(len(effective), 2)
        amounts = {r.refund_id: r.amount for r in effective}
        self.assertEqual(amounts, {"RF1": money("100"), "RF2": money("60")})
        self.assertEqual(len(notes), 2)
        execution = Execution(
            "rf", plan.created_at, "2026-10-06T10:00:00",
            actual_transfers=[t], initiated_order_ids=[t.order_id],
            refunds=refunds)
        rr = replan(sc, plan, execution, "2026-10-06")
        full = combined_replay(sc, execution, rr.plan)
        # 分两次到账合计 160，重复/更正没有多记
        self.assertEqual(full.ledger.total_refunds, money("160"))
        full.ledger.verify_conservation()

    def test_partial_allocation_of_arrived_refund(self):
        """退回到达后可只把一部分用于尚未确认的付款。"""
        sc = build()
        plan = solve(sc).plan
        t = executed_transfer(plan)
        execution = Execution(
            "rf", plan.created_at, "2026-10-05T10:00:00",
            actual_transfers=[t], initiated_order_ids=[t.order_id],
            refunds=[
                RefundRecord("RF1", "payment", "W1", "refund",
                             money("150"), "2026-10-05", "A"),
                RefundRecord("RF2", "payment", "W1", "refund",
                             money("80"), "2026-10-08", "A"),
            ])
        rr = replan(sc, plan, execution, "2026-10-05")
        # 已到账退回可以部分使用
        updated = allocate_arrived_refund(
            rr.plan, execution, "2026-10-05", "RF1", [("X1", money("100"))])
        self.assertEqual(updated.refund_allocations[0]["amount"], "100.00")
        # 同一笔退回累计分配不能超过实际到账
        with self.assertRaises(ValueError):
            allocate_arrived_refund(
                updated, execution, "2026-10-05", "RF1", [("X1", money("60"))])
        # 预计退回（10-08 才到账）不能提前使用
        with self.assertRaises(ValueError):
            allocate_arrived_refund(
                rr.plan, execution, "2026-10-05", "RF2", [("X1", money("1"))])

    def test_compare_wait_vs_alternative_account(self):
        """继续等待退回 vs 替代账户安排：对照剩余缺口与费用。"""
        sc = build()
        plan = solve(sc).plan
        t = executed_transfer(plan)
        execution = Execution(
            "rf", plan.created_at, "2026-10-03T10:00:00",
            actual_transfers=[t], initiated_order_ids=[t.order_id],
            refunds=[RefundRecord(
                "RF9", "payment", "W1", "refund",
                money("250"), "2026-10-09", "A")])
        options = compare_refund_usage(sc, execution, "2026-10-03")
        self.assertEqual([o["name"] for o in options],
                         ["alternative_account", "wait_for_refund"])
        alt, wait = options
        # 替代安排：A 只剩 92，X1 缺口保留
        self.assertGreater(Decimal(alt["remaining_gap"]), Decimal("0"))
        # 等待退回：10-09 到账后 X1 顺延足额支付
        self.assertEqual(Decimal(wait["remaining_gap"]), Decimal("0"))
        self.assertEqual(wait["delayed_payments"], {"X1": "2026-10-09"})
        self.assertEqual(wait["expected_refund_ids"], ["RF9"])

    def test_cross_account_return_and_reopen_reconciliation(self):
        """跨账户返回 + 迟到退回：重开后余额、在途与原支付去向仍可对上。"""
        sc = build()
        plan = solve(sc).plan
        t = executed_transfer(plan)
        execution = Execution(
            "rf", plan.created_at, "2026-10-04T10:00:00",
            actual_transfers=[t], initiated_order_ids=[t.order_id],
            refunds=[RefundRecord(
                "RF3", "payment", "W1", "refund",
                money("120"), "2026-10-05", "A")])  # 从 B 付出，退回 A
        rr = replan(sc, plan, execution, "2026-10-04")
        self.assertEqual(rr.plan.expected_refunds[0]["credit_account"], "A")
        full = combined_replay(sc, execution, rr.plan)
        led = full.ledger
        led.verify_conservation()
        # 原支付最终去向：W1 从 B 支付 400；120 跨账户退回 A（外部回流）
        w1 = next(p for p in led.payments if p.flow_id == "W1")
        self.assertEqual(w1.paid, money("400"))
        self.assertEqual(led.total_refunds, money("120"))
        self.assertEqual(led.refund_from_transit, money("0"))
        self.assertEqual(led.available("A"), money("500") - 408 + 120)
        # 重开继续部分采纳：以实际到账退回为依据，账目仍守恒
        view = refund_view(execution, "2026-10-05")
        self.assertEqual(view["by_payment"]["W1"]["refunded_arrived"], "120.00")
        self.assertEqual(view["by_payment"]["W1"]["credit_accounts"], ["A"])

    def test_plan_roundtrip_keeps_refund_fields(self):
        import tempfile, pathlib
        sc = build()
        plan = solve(sc).plan
        t = executed_transfer(plan)
        execution = Execution(
            "rf", plan.created_at, "2026-10-05T10:00:00",
            actual_transfers=[t], initiated_order_ids=[t.order_id],
            refunds=[RefundRecord(
                "RF1", "payment", "W1", "refund",
                money("150"), "2026-10-05", "A")])
        rr = replan(sc, plan, execution, "2026-10-05")
        updated = allocate_arrived_refund(
            rr.plan, execution, "2026-10-05", "RF1", [("X1", money("100"))])
        from cashflow.plan import Plan
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "plan.json"
            updated.save(path)
            loaded = Plan.load(path)
            ex_path = pathlib.Path(d) / "ex.json"
            execution.save(ex_path)
            self.assertEqual(Execution.load(ex_path).refunds[0].refund_id, "RF1")
        self.assertEqual(loaded.refund_basis[0]["refund_id"], "RF1")
        self.assertEqual(loaded.refund_allocations[0]["flow_id"], "X1")


if __name__ == "__main__":
    unittest.main()
