"""第三轮：部分采纳、确认占款、撤回与重开续作的测试。"""

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from cashflow import (
    Account, CashFlow, FlowKind, Priority, Scenario, TransferRule,
    Execution, money, solve,
)
from cashflow.adopt import (
    Adoption, adoption_combined_replay, compare_partials, confirm_partial,
    confirmed_orders, withdraw_adoption,
)
from cashflow.plan import ActualInflow, ActualTransfer
from cashflow.replan import combined_replay
from cashflow.repo import Repository


def build() -> Scenario:
    """收入迟到 + 共享资金争用 + 部分到账的小场景。"""
    return Scenario(
        "adp", "2026-10-01", "2026-10-12",
        [Account("A", "基本户", money("1000")),
         Account("B", "业务户"), Account("C", "运营户")],
        flows=[
            CashFlow("IN1", "B", "2026-10-06", FlowKind.INFLOW, money("500"),
                     memo="客户回款（实际迟到且部分到账）"),
            CashFlow("W1", "B", "2026-10-03", FlowKind.OUTFLOW, money("500"),
                     Priority.P1_CRITICAL, "工资"),
            CashFlow("R1", "B", "2026-10-05", FlowKind.OUTFLOW, money("400"),
                     Priority.P2_NORMAL, "房租"),
            CashFlow("S1", "C", "2026-10-05", FlowKind.OUTFLOW, money("300"),
                     Priority.P2_NORMAL, "供应商款"),
            CashFlow("X1", "B", "2026-10-07", FlowKind.OUTFLOW, money("200"),
                     Priority.P3_OPTIONAL, "可选采购"),
        ],
        transfer_rules=[
            TransferRule("A", "B", lag_days=0, fee_rate=Decimal("0.01")),
            TransferRule("A", "C", lag_days=0),
            TransferRule("C", "B", lag_days=1),
        ],
    )


def chain(plan, *order_ids):
    return [o for o in plan.orders if o.order_id in order_ids]


def w1_chain(plan):
    """覆盖工资 W1 的完整调拨链（10-03 及之前到账 B）。"""
    return [o.order_id for o in plan.orders
            if o.target == "B" and o.arrival_date <= "2026-10-03"
            or o.target == "C" and o.arrival_date <= "2026-10-02"]


def make_execution(plan, dispatched_ids, inflow_amount="300") -> Execution:
    """cutoff=10-02：部分确认调拨已发出；IN1 迟到到 10-08 且只部分到账。"""
    actuals, initiated = [], []
    for o in plan.orders:
        if o.order_id in dispatched_ids:
            actuals.append(ActualTransfer(
                o.order_id, o.source, o.target, o.initiate_date,
                o.arrival_date, o.principal, o.fee))
            initiated.append(o.order_id)
    return Execution(
        scenario_id="adp", plan_created_at="p",
        recorded_at="2026-10-02T23:00:00",
        actual_transfers=actuals,
        initiated_order_ids=initiated,
        actual_inflows=[
            ActualInflow("IN1", "B", "2026-10-06", "2026-10-08",
                         money(inflow_amount)),
        ],
    )


class TestPartialAdoption(unittest.TestCase):
    def test_confirmed_orders_reserve_funds(self):
        sc = build()
        plan = solve(sc).plan
        scope = w1_chain(plan)          # T003(A->C) + T004(C->B)
        self.assertEqual(len(scope), 2)
        result = confirm_partial(sc, plan, scope, label="甲")
        self.assertIsNotNone(result.residual_plan, result.infeasible_reason)
        # 已确认安排不再出现在剩余计划中，但作为占款参与重放
        residual_ids = [o.order_id for o in result.residual_plan.orders]
        for oid in scope:
            self.assertNotIn(oid, residual_ids)
        result.residual_replay.ledger.verify_conservation()
        # 共享资金争用：A 的 1000 中 500 被确认占款，供应商款 S1 仍无法覆盖
        gaps = {g["flow_id"]: g["shortfall"]
                for g in result.residual_plan.remaining_gaps}
        self.assertEqual(gaps.get("S1"), "300.00")
        self.assertEqual(result.adoption.status, "active")
        self.assertEqual(len(confirmed_orders(result.adoption)), 2)

    def test_broken_chain_scope_reported_infeasible(self):
        sc = build()
        plan = solve(sc).plan
        second_leg = next(o for o in plan.orders
                          if o.source == "C" and o.target == "B")
        result = confirm_partial(sc, plan, [second_leg.order_id])
        # 只确认链的后半段：C 无款可发，明确报不可行而不是静默出错
        self.assertIsNone(result.residual_plan)
        self.assertTrue(result.infeasible_reason)
        self.assertEqual(result.adoption.status, "infeasible")

    def test_impact_analysis_structure(self):
        sc = build()
        plan = solve(sc).plan
        result = confirm_partial(sc, plan, w1_chain(plan))
        self.assertTrue(result.impacts)
        for im in result.impacts:
            self.assertIn("delayed_payments", im)
            self.assertIn("fee_delta", im)
            self.assertIn("lost_feasible", im)

    def test_compare_two_scopes_preserves_constraints(self):
        sc = build()
        plan = solve(sc).plan
        original_dates = {f.flow_id: f.date for f in sc.flows}
        r1_chain = [o.order_id for o in plan.orders
                    if o.initiate_date >= "2026-10-03"]
        scopes = [("甲", w1_chain(plan)), ("乙", r1_chain)]
        results = compare_partials(sc, plan, scopes)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].adoption.label, "甲")
        self.assertEqual(results[1].adoption.label, "乙")
        # 原始期限约束不被改写
        self.assertEqual({f.flow_id: f.date for f in sc.flows}, original_dates)
        for res in results:
            self.assertIsNotNone(res.residual_plan, res.infeasible_reason)
            self.assertIsNotNone(res.residual_plan.remaining_gaps)


class TestWithdraw(unittest.TestCase):
    def _adopted_and_execution(self):
        sc = build()
        plan = solve(sc).plan
        scope = w1_chain(plan)          # T003 + T004
        adopted = confirm_partial(sc, plan, scope).adoption
        # 第一跳已到账、第二跳 cutoff 时仍在途；执行时手续费实际发生 2.00
        execution = make_execution(plan, scope)
        t = execution.actual_transfers[0]
        execution.actual_transfers[0] = ActualTransfer(
            t.order_id, t.source, t.target, t.actual_initiate_date,
            t.actual_arrival_date, t.actual_principal, money("2.00"))
        return sc, plan, adopted, execution

    def test_withdraw_keeps_dispatched_and_fees(self):
        sc, plan, adopted, execution = self._adopted_and_execution()
        wres = withdraw_adoption(sc, adopted, plan, execution, "2026-10-02")
        # 已发出的两笔保持在途/已到账，释放列表为空（本范围全部已发出）
        self.assertEqual(sorted(k["order_id"] for k in wres.kept_in_transit),
                         sorted(w1_chain(plan)))
        self.assertEqual(wres.released, [])
        self.assertIn("不再从原账户重扣",
                      wres.kept_in_transit[0]["handling"])
        # 已发生手续费 2.00 不退回
        self.assertEqual(wres.fees_incurred, money("2.00"))
        self.assertEqual(wres.adoption.status, "partially_withdrawn")
        self.assertTrue(all(e["status"] == "dispatched_kept"
                            for e in wres.adoption.confirmed))
        wres.replan_result.residual_replay.ledger.verify_conservation()
        full = combined_replay(sc, execution, wres.replan_result.plan)
        full.ledger.verify_conservation()
        # 已发生费用仍在账上，未被抹去
        self.assertGreaterEqual(full.ledger.total_fees, money("2.00"))

    def test_withdraw_releases_undispatched(self):
        sc = build()
        plan = solve(sc).plan
        scope = w1_chain(plan)
        extra = next(o for o in plan.orders if o.order_id not in scope)
        adopted = confirm_partial(sc, plan, scope + [extra.order_id]).adoption
        execution = make_execution(plan, scope)   # extra 未发出
        wres = withdraw_adoption(sc, adopted, plan, execution, "2026-10-02")
        self.assertEqual([r["order_id"] for r in wres.released],
                         [extra.order_id])
        self.assertIn("释放", wres.released[0]["handling"])
        self.assertEqual(len(confirmed_orders(wres.adoption)), len(scope))

    def test_partial_inflow_day_by_day(self):
        sc, plan, adopted, execution = self._adopted_and_execution()
        wres = withdraw_adoption(sc, adopted, plan, execution, "2026-10-02")
        full = combined_replay(sc, execution, wres.replan_result.plan)
        # 收入按实际（迟到到 10-08 + 部分到账 300）入账，逐日快照齐全
        self.assertEqual(full.ledger.total_inflows, money("300"))
        days = [s.date for s in full.ledger.snapshots]
        self.assertEqual(days[0], "2026-10-01")
        self.assertEqual(days[-1], "2026-10-12")
        self.assertEqual(len(days), 12)
        full.ledger.verify_conservation()


class TestReopen(unittest.TestCase):
    def test_reopen_continue_no_double_reservation(self):
        sc = build()
        plan = solve(sc).plan
        scope = w1_chain(plan)
        extra = next(o for o in plan.orders if o.order_id not in scope)
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repository(Path(tmp))
            first = confirm_partial(sc, plan, scope)
            path = repo.save_adoption(first.adoption)
            # 重新打开后继续采纳：重复单号被跳过，不重复占款
            loaded = repo.load_adoption(path)
            again = confirm_partial(sc, plan, scope + [extra.order_id],
                                    prior=loaded)
            self.assertEqual(sorted(again.skipped), sorted(scope))
            self.assertEqual(len(confirmed_orders(again.adoption)),
                             len(scope) + 1)
            # 整段重放：同一单号只发起一次，资金守恒
            full = adoption_combined_replay(sc, again.adoption,
                                            again.residual_plan)
            initiations = [e for e in full.ledger.log
                           if e["event"] == "transfer_initiate"]
            ids = [e["order_id"] for e in initiations]
            self.assertEqual(len(ids), len(set(ids)))
            full.ledger.verify_conservation()

    def test_adoption_roundtrip_json(self):
        sc = build()
        plan = solve(sc).plan
        adoption = confirm_partial(sc, plan, w1_chain(plan)).adoption
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.json"
            adoption.save(path)
            loaded = Adoption.load(path)
        self.assertEqual(loaded.confirmed, adoption.confirmed)
        self.assertEqual(confirmed_orders(loaded)[0].order_id,
                         w1_chain(plan)[0])


if __name__ == "__main__":
    unittest.main()
