"""第六轮：依赖退回资金的后续付款方案研究测试。

验证：一笔迟到退回、一次部分退回、两项争用同一笔现金的付款，
以及资金守恒与保存/恢复。
"""

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from cashflow import (
    Account, CashFlow, FlowKind, Priority, Scenario, TransferRule,
    Execution, RefundRecord, money, solve,
)
from cashflow.followup import (
    RefundStudyCase, adjust_followup_payments, compare_refund_studies,
    payment_execution_schedule, recompute_affected,
)
from cashflow.plan import ActualTransfer, Plan
from cashflow.refund import record_refunds
from cashflow.replan import FeeAmendment, combined_replay


def build() -> Scenario:
    return Scenario(
        "fu", "2026-10-01", "2026-10-12",
        [Account("A", "资金户", money("700")),
         Account("B", "业务户"),
         Account("C", "备用户")],
        flows=[
            CashFlow("S1", "B", "2026-10-03", FlowKind.OUTFLOW, money("500"),
                     Priority.P2_NORMAL, "货款（后部分退回）"),
            CashFlow("S2", "B", "2026-10-08", FlowKind.OUTFLOW, money("180"),
                     Priority.P1_CRITICAL, "工资"),
            CashFlow("S3", "B", "2026-10-09", FlowKind.OUTFLOW, money("250"),
                     Priority.P2_NORMAL, "货款",
                     "2026-10-09", "2026-10-12"),
            CashFlow("S4", "B", "2026-10-10", FlowKind.OUTFLOW, money("100"),
                     Priority.P3_OPTIONAL, "可选采购"),
            CashFlow("IN1", "B", "2026-10-12", FlowKind.INFLOW, money("200")),
        ],
        transfer_rules=[
            TransferRule("A", "B", lag_days=1, fee_rate=Decimal("0.01")),
            TransferRule("C", "B", lag_days=1, fee_rate=Decimal("0.005")),
        ],
    )


def make_execution(plan) -> Execution:
    """S1 已付（T 系列 A->B 已发起）；随后 RF1 部分退回到 B，
    RF2 预计退回（尚未到达，将迟到且部分到 C）。"""
    executed = Execution(
        scenario_id="fu", plan_created_at=plan.created_at,
        recorded_at="2026-10-05T23:00:00")
    for o in plan.orders:
        if o.initiate_date <= "2026-10-05":
            executed.actual_transfers.append(ActualTransfer(
                o.order_id, o.source, o.target, o.initiate_date,
                o.arrival_date, o.principal, o.fee))
            executed.initiated_order_ids.append(o.order_id)
    record_refunds(executed, [
        RefundRecord("RF1", "S1", "B", "B", "2026-10-06", money("200"),
                     actual_date="2026-10-06", note="部分退回，实际到 B"),
        RefundRecord("RF2", "S1", "B", "C", "2026-10-07", money("250"),
                     note="预计退回到 C，未到达"),
    ])
    return executed


class TestFollowupSchedule(unittest.TestCase):
    def test_expected_refund_never_becomes_balance(self):
        sc = build()
        plan = solve(sc).plan
        execution = make_execution(plan)
        # 仅实际到账：RF2 预计 10-07 到 C，但执行表不得把它当作可用资金
        schedule = payment_execution_schedule(sc, plan, execution,
                                              "2026-10-05")
        self.assertFalse(schedule["assumptions_used"])
        batch_ids = [b["refund_id"] for b in schedule["refund_batches"]]
        self.assertEqual(batch_ids, ["RF1"])  # RF2 未实际到达，不是批次
        # 对照假设下 RF2 才按假设日入账，且只影响该对照方案
        options = compare_refund_studies(sc, plan, execution, "2026-10-05", [
            RefundStudyCase("退回按时", {"RF2": "2026-10-07"}),
            RefundStudyCase("退回迟到+费率升", {"RF2": "2026-10-11"},
                            (FeeAmendment("C", "B", "2026-10-06",
                                          fee_rate=Decimal("0.02")),)),
        ])
        self.assertEqual(len(options), 3)
        self.assertTrue(all(o["assumption_only"] for o in options))
        base, on_time_case, late_case = options
        self.assertEqual(base["assumed_refunds"], [])
        # 假设退回按时时，S3 比「退回迟到」假设下更早可付
        s3 = {o["name"]: next(p for p in o["payments"] if p["flow_id"] == "S3")
              for o in options}
        self.assertLessEqual(
            s3["退回按时"]["scheduled_date"],
            s3["退回迟到+费率升"]["scheduled_date"])
        # 假设不改动实际执行记录：预计退回仍然没有 actual_date
        rf2 = next(r for r in execution.refunds if r.refund_id == "RF2")
        self.assertFalse(rf2.arrived)

    def test_batched_refunds_and_contention(self):
        sc = build()
        plan = solve(sc).plan
        execution = make_execution(plan)
        schedule = payment_execution_schedule(sc, plan, execution,
                                              "2026-10-05")
        payments = {p["flow_id"]: p for p in schedule["payments"]}
        # S2（P1 工资 180）与 S3（P2 货款 250）争用 RF1 的 200：
        # 只有一笔现金，S2 先执行，S3 仍需等待后续资金
        self.assertTrue(payments["S2"]["executable"])
        self.assertEqual(payments["S2"]["earliest_executable_date"],
                         "2026-10-08")
        contention = schedule["contention"]
        self.assertEqual(len(contention), 1)
        self.assertEqual(contention[0]["refund_id"], "RF1")
        self.assertEqual(contention[0]["covered_first"], ["S2"])
        self.assertIn("S3", contention[0]["must_wait"])
        # S3 可用资金不足，最早可执行日期晚于原期限（等待后续资金）
        self.assertGreater(payments["S3"]["earliest_executable_date"],
                           "2026-10-09")
        # 资金守恒：combined_replay 每日核对（负余额直接报错）
        full = combined_replay(sc, execution, schedule["plan"])
        self.assertFalse(full.gaps and any(
            g.flow_id == "S2" for g in full.gaps))

    def test_late_partial_refund_recompute_only_affected(self):
        sc = build()
        plan = solve(sc).plan
        execution = make_execution(plan)
        # 研究方案：假设 RF2 于 10-07 按时到 C
        studied = compare_refund_studies(sc, plan, execution, "2026-10-05", [
            RefundStudyCase("退回按时", {"RF2": "2026-10-07"}),
        ])[1]["plan"]
        before_snapshot = studied.to_dict()
        # 实际：RF2 迟到（10-09）且部分（150 < 250），入 C 户
        out = recompute_affected(sc, studied, execution, "2026-10-05", [
            RefundRecord("RF2", "S1", "B", "C", "2026-10-07", money("150"),
                         actual_date="2026-10-09", note="迟到且部分"),
        ])
        # 预测核对：假设 10-07，实际 10-09，不匹配
        check = out["forecast_vs_actual"][0]
        self.assertEqual(check["assumed_date"], "2026-10-07")
        self.assertEqual(check["actual_date"], "2026-10-09")
        self.assertEqual(check["actual_amount"], "150.00")
        self.assertFalse(check["matches_forecast"])
        # 只重算受影响部分：S2 不受影响，S3 受影响改期
        self.assertIn("S2", out["unaffected_payments"])
        affected_ids = [c["flow_id"] for c in out["affected_payments"]]
        self.assertIn("S3", affected_ids)
        # 原方案保持不动，可供核对；新方案 revision_of 指向它
        self.assertEqual(studied.to_dict(), before_snapshot)
        self.assertEqual(out["plan"].revision_of, studied.created_at)
        self.assertEqual(out["original_plan_kept"], studied.created_at)
        # 已发出事实不撤销：原已发起调拨仍在执行记录中，且不在新订单里重扣
        new_order_ids = {o.order_id for o in out["plan"].orders}
        for oid in execution.initiated_order_ids:
            self.assertNotIn(oid, new_order_ids)
        # 部分退回只入账 150，不会按预计 250 制造余额
        full = combined_replay(sc, execution, out["plan"])
        c_events = [e for e in full.ledger.log
                    if e["event"] == "inflow" and e.get("flow_id") == "REFUND-RF2"]
        self.assertEqual(len(c_events), 1)
        self.assertEqual(c_events[0]["amount"], "150.00")

    def test_adjust_priority_and_cancel_unissued_only(self):
        sc = build()
        plan = solve(sc).plan
        execution = make_execution(plan)
        adj = adjust_followup_payments(
            sc, plan, execution, "2026-10-05",
            priority_overrides={"S3": int(Priority.P1_CRITICAL)},
            cancelled_flow_ids=["S4"])
        decisions = {d["flow_id"]: d for d in adj["plan"].payment_decisions}
        self.assertEqual(decisions["S4"]["status"], "cancelled")
        self.assertEqual(adj["priority_overrides"],
                         {"S3": int(Priority.P1_CRITICAL)})
        # 取消后 S4 不在调整场景里，重放守恒
        full = combined_replay(adj["adjusted_scenario"], execution,
                               adj["plan"])
        self.assertNotIn("S4", {p.flow_id for p in full.ledger.payments})
        # 已发出/已到账的事实不能调整：S1 已付，S2 期限未到但 S1 已付
        with self.assertRaises(ValueError):
            adjust_followup_payments(sc, plan, execution, "2026-10-05",
                                     cancelled_flow_ids=["S1"])

    def test_save_restore_conservation(self):
        sc = build()
        plan = solve(sc).plan
        execution = make_execution(plan)
        schedule = payment_execution_schedule(sc, plan, execution,
                                              "2026-10-05")
        studied = schedule["plan"]
        out = recompute_affected(sc, studied, execution, "2026-10-05", [
            RefundRecord("RF2", "S1", "B", "C", "2026-10-07", money("150"),
                         actual_date="2026-10-09"),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            p_old = Path(tmp) / "studied.json"
            p_new = Path(tmp) / "recomputed.json"
            p_exec = Path(tmp) / "execution.json"
            studied.save(p_old)
            out["plan"].save(p_new)
            execution.save(p_exec)
            # 重开恢复：原方案与新方案各自完整，执行记录一致
            old_loaded = Plan.load(p_old)
            new_loaded = Plan.load(p_new)
            exec_loaded = Execution.load(p_exec)
            self.assertEqual(old_loaded.to_dict(), studied.to_dict())
            self.assertEqual(new_loaded.to_dict(), out["plan"].to_dict())
            self.assertEqual(new_loaded.revision_of, old_loaded.created_at)
            self.assertEqual(len(exec_loaded.refunds), 2)
            # 恢复后重放仍满足资金守恒（replay 每日 verify_conservation）
            full = combined_replay(sc, exec_loaded, new_loaded)
            led = full.ledger
            total_balance = sum(led.balances.values(), Decimal("0"))
            in_transit = sum(l.principal for l in led.in_transit.values())
            paid = sum(p.paid for p in led.payments)
            self.assertEqual(
                money(total_balance + in_transit + led.total_fees + paid),
                money(sum((a.opening_balance for a in sc.accounts),
                          Decimal("0")) + led.total_inflows))


if __name__ == "__main__":
    unittest.main()
