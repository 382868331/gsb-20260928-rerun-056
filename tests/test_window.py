"""第三轮测试：支付窗口排期、三分类、策略对照、采纳与差异复演。"""

import unittest
from decimal import Decimal

from cashflow import (
    Account, CashFlow, FlowKind, Priority, Scenario, TransferRule,
    Execution, money, solve,
)
from cashflow.models import CashFlow as CF
from cashflow.scheduling import (
    compare_strategies, run_strategy,
    STRICT, FASTEST, LOWEST_FEE,
    TRULY_UNMET, STRATEGY_NOT_USED,
)
from cashflow.replan import FeeAmendment, replan, combined_replay
from cashflow.adoption import adopt, diff_versions, replay_version


def windowed_scenario(a_balance="800", x2_amount="2000") -> Scenario:
    """A 仅够工资600+房租自筹200；X1 窗口内等 10-08 回款可足额；X2 真不足。"""
    return Scenario(
        "w", "2026-10-01", "2026-10-12",
        [Account("A", "基本户", money(a_balance)),
         Account("B", "业务户"), Account("C", "过桥户")],
        flows=[
            CashFlow("IN1", "B", "2026-10-04", FlowKind.INFLOW, money("500")),
            CashFlow("IN2", "B", "2026-10-08", FlowKind.INFLOW, money("500")),
            CashFlow("W1", "B", "2026-10-03", FlowKind.OUTFLOW, money("600"),
                     Priority.P1_CRITICAL, "工资"),
            CashFlow("R1", "B", "2026-10-05", FlowKind.OUTFLOW, money("700"),
                     Priority.P2_NORMAL, "房租"),
            CashFlow("X1", "B", "2026-10-06", FlowKind.OUTFLOW, money("500"),
                     Priority.P3_OPTIONAL, "窗口内可改期的采购",
                     pay_window_end="2026-10-09"),
            CashFlow("X2", "B", "2026-10-06", FlowKind.OUTFLOW,
                     money(x2_amount), Priority.P3_OPTIONAL, "真正不足的采购",
                     pay_window_end="2026-10-09"),
        ],
        transfer_rules=[
            TransferRule("A", "B", lag_days=1, fee_rate=Decimal("0.02")),
            TransferRule("A", "C", lag_days=0),
            TransferRule("C", "B", lag_days=1),
        ],
    )


class TestWindowScheduling(unittest.TestCase):
    def test_strict_marks_strategy_not_used(self):
        out = run_strategy(windowed_scenario(), STRICT)
        by_flow = {d["flow_id"]: d for d in out.decisions}
        # 10-06 当天付不起、但窗口内 10-08 可足额：strict 下必须判“策略未采用”
        self.assertEqual(by_flow["X1"]["status"], STRATEGY_NOT_USED)
        self.assertEqual(by_flow["X1"]["scheduled_date"], "2026-10-06")
        self.assertGreater(Decimal(by_flow["X1"]["shortfall"]), 0)
        # 原始期限保留
        self.assertEqual(by_flow["X1"]["original_due_date"], "2026-10-06")

    def test_fastest_reschedules_within_window_and_pays_full(self):
        out = run_strategy(windowed_scenario(), FASTEST)
        by_flow = {d["flow_id"]: d for d in out.decisions}
        self.assertEqual(by_flow["X1"]["status"], "rescheduled")
        self.assertEqual(by_flow["X1"]["scheduled_date"], "2026-10-08")
        self.assertEqual(by_flow["X1"]["shortfall"], "0.00")
        self.assertEqual(by_flow["X1"]["paid"], "500.00")
        out.replay.ledger.verify_conservation()

    def test_truly_unmet_even_with_rescheduling(self):
        for name in (STRICT, FASTEST, LOWEST_FEE):
            out = run_strategy(windowed_scenario(), name)
            by_flow = {d["flow_id"]: d for d in out.decisions}
            self.assertEqual(by_flow["X2"]["status"], TRULY_UNMET,
                             msg=f"{name} 应判真正不足")
            self.assertGreater(Decimal(by_flow["X2"]["shortfall"]), 0)

    def test_strategy_comparison_final_gaps_and_fees(self):
        outs = {o.strategy: o for o in compare_strategies(windowed_scenario())}
        strict_gap = outs[STRICT].replay.gap_total_by_priority()[3]
        fast_gap = outs[FASTEST].replay.gap_total_by_priority()[3]
        # strict 把 X1 也留成缺口；fastest/lowest 只余 X2 的真不足
        self.assertEqual(strict_gap, money("2500"))
        self.assertEqual(fast_gap, money("2000"))
        # 三策略 P1/P2 均为 0，且每日资金守恒
        for o in outs.values():
            totals = o.replay.gap_total_by_priority()
            self.assertEqual(totals[1] + totals[2], 0)
            o.replay.ledger.verify_conservation()

    def test_lowest_fee_waits_for_inflow_instead_of_costly_funding(self):
        # 有免费慢路径与 2% 直达：等到 10-08 回款直接付，费用应为 0
        out = run_strategy(windowed_scenario(), LOWEST_FEE)
        by_flow = {d["flow_id"]: d for d in out.decisions}
        self.assertEqual(by_flow["X1"]["status"], "rescheduled")
        x1_orders_fee = sum(
            (o.fee for o in out.plan.orders
             if o.purpose == "" and o.target == "B"), Decimal("0"))
        # X1 未产生任何为它紧急筹资的收费单（总费用仅来自其他支出）
        self.assertEqual(out.total_fee, Decimal("0.00"))
        self.assertEqual(x1_orders_fee, Decimal("0"))

    def test_original_due_date_never_mutated(self):
        sc = windowed_scenario()
        original_date = next(f for f in sc.flows if f.flow_id == "X1").date
        run_strategy(sc, FASTEST)
        after = next(f for f in sc.flows if f.flow_id == "X1")
        self.assertEqual(after.date, original_date)
        self.assertEqual(after.pay_window_end, "2026-10-09")


class TestWindowedReplan(unittest.TestCase):
    def _prep(self):
        from tests.test_replan import build as build_r, delayed_execution
        sc = build_r()
        sc.flows = [
            CF(f.flow_id, f.account_id, f.date, f.kind, f.amount, f.priority,
               f.memo, "2026-10-09" if f.flow_id == "X1" else "")
            for f in sc.flows]
        plan = solve(sc).plan
        return sc, plan, delayed_execution(plan)

    def test_replan_window_strategy_reschedules_and_freezes_basis(self):
        sc, plan, execution = self._prep()
        rr = replan(sc, plan, execution, "2026-10-02",
                    amendments=[FeeAmendment("C", "B", "2026-10-03",
                                             fee_rate=Decimal("0.02"))],
                    inflow_updates={"IN1": ("2026-10-04", money("300"))},
                    window_strategy=FASTEST)
        revised = rr.plan
        by_flow = {d["flow_id"]: d for d in revised.payment_decisions}
        self.assertEqual(by_flow["X1"]["status"], "rescheduled")
        self.assertEqual(by_flow["X1"]["scheduled_date"], "2026-10-08")
        self.assertEqual(by_flow["X1"]["original_due_date"], "2026-10-06")
        self.assertEqual(revised.remaining_gaps, [])
        # 依据快照冻结：含原场景、cutoff、费率与收入修订
        self.assertEqual(revised.basis["window_strategy"], FASTEST)
        self.assertEqual(revised.basis["cutoff_date"], "2026-10-02")
        self.assertEqual(revised.basis["inflow_updates"]["IN1"][1], "300.00")
        self.assertEqual(revised.basis["scenario"]["scenario_id"], "r")
        # 资金守恒（残差 + 全期合并），在途单不重扣
        rr.residual_replay.ledger.verify_conservation()
        full = combined_replay(
            sc, execution, revised,
            inflow_updates={"IN1": ("2026-10-04", money("300"))})
        full.ledger.verify_conservation()

    def test_combined_stress_partial_inflow_rate_change_conservation(self):
        """收入推迟 + 费率变化 + 部分支付同时发生：逐日余额守恒。"""
        sc, plan, execution = self._prep()
        rr = replan(
            sc, plan, execution, "2026-10-02",
            amendments=[FeeAmendment("C", "B", "2026-10-03",
                                     fee_rate=Decimal("0.03")),
                        FeeAmendment("A", "B", "2026-10-03",
                                     fee_rate=Decimal("0.05"))],
            inflow_updates={"IN1": ("2026-10-05", money("200"))},  # 500->200 且推迟
            window_strategy=LOWEST_FEE)
        full = combined_replay(
            sc, execution, rr.plan,
            inflow_updates={"IN1": ("2026-10-05", money("200"))})
        # 逐日恒等校验在 replay 内每天执行；这里再整体核对
        full.ledger.verify_conservation()
        for snap in full.ledger.snapshots:
            for acc, bal in snap.balances.items():
                self.assertGreaterEqual(bal, 0)
        # 部分到账：IN1 只有 200 入账
        inflow_events = [e for e in full.ledger.log
                         if e["event"] == "inflow" and e["flow_id"] == "IN1"]
        self.assertEqual(len(inflow_events), 1)
        self.assertEqual(inflow_events[0]["amount"], "200.00")


class TestAdoptionAndDiff(unittest.TestCase):
    def test_adoption_keeps_old_plan_and_replays_both(self):
        import tempfile
        from pathlib import Path
        from cashflow.repo import Repository
        sc = windowed_scenario()
        old = run_strategy(sc, STRICT).plan
        new = run_strategy(sc, FASTEST).plan
        old.created_at = "2026-10-01T08:00:00"
        new.created_at = "2026-10-01T09:00:00"

        with tempfile.TemporaryDirectory() as d:
            repo = Repository(Path(d))
            p_old = repo.save_plan(old)
            repo.save_plan(new)
            record = adopt(new, old, note="采纳改期方案",
                           adopted_at="2026-10-01T09:05:00")
            repo.save_adoption(record)

            old_text_before = p_old.read_text(encoding="utf-8")
            # 重开：旧文件原样、可加载
            reloaded_old = Plan.load(p_old) if False else None
            from cashflow.plan import Plan
            reloaded_old = Plan.load(p_old)
            self.assertEqual(reloaded_old.created_at, old.created_at)
            self.assertEqual(p_old.read_text(encoding="utf-8"), old_text_before)

        # 差异复演：X1 由 10-06 部分/缺口 变为 10-08 足额
        vd = diff_versions(sc, old, new)
        x1 = next(c for c in vd.payment_changes if c["flow_id"] == "X1")
        self.assertEqual(x1["old_date"], "2026-10-06")
        self.assertEqual(x1["new_date"], "2026-10-08")
        self.assertEqual(x1["new_shortfall"], "0.00")
        self.assertEqual(vd.gap_old[3], money("2500"))
        self.assertEqual(vd.gap_new[3], money("2000"))
        # 两版各自逐日守恒
        replay_version(sc, None, old).ledger.verify_conservation()
        replay_version(sc, None, new).ledger.verify_conservation()


if __name__ == "__main__":
    unittest.main()

