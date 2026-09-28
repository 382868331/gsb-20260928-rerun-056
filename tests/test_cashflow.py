import unittest
from decimal import Decimal

from cashflow import (
    Account, CashFlow, FlowKind, Ledger, LedgerError, Priority, Scenario,
    TransferRule, brute_force_solve, money, simulate_baseline, solve,
)
from cashflow.engine import replay
from cashflow.plan import TransferOrder
from cashflow.ledger import TransferLeg


def sc_two_accounts(**rule_kw):
    kw = dict(lag_days=0, fee_rate=Decimal("0"), fee_fixed=Decimal("0"))
    kw.update(rule_kw)
    return Scenario(
        "t", "2026-01-01", "2026-01-05",
        [Account("A", "a", money("100")), Account("B", "b")],
        flows=[CashFlow("O1", "B", "2026-01-03", FlowKind.OUTFLOW, money("100"),
                        Priority.P1_CRITICAL)],
        transfer_rules=[TransferRule("A", "B", **kw)],
    )


class TestLedgerRules(unittest.TestCase):
    def test_fee_blocks_overdraw(self):
        # 本金100 + 手续费2，A 只有100：手续费必须占用资金，发起失败
        scenario = sc_two_accounts(fee_rate=Decimal("0.02"))
        orders = [TransferOrder("X1", "A", "B", "2026-01-03", "2026-01-03",
                                money("100"), money("2"))]
        with self.assertRaises(LedgerError):
            replay(scenario, orders)

    def test_lag_means_not_yet_available(self):
        # lag=1：01-03 才到账，无法支付 01-03 的支出 -> 缺口100
        scenario = sc_two_accounts(lag_days=1)
        orders = [TransferOrder("X1", "A", "B", "2026-01-03", "2026-01-04",
                                money("100"), money("0"))]
        result = replay(scenario, orders)
        self.assertEqual(result.gaps[0].shortfall, money("100"))

    def test_same_money_not_arranged_twice(self):
        # 两笔调拨都想在同一天用 A 的同一笔钱，第二笔必须失败
        scenario = Scenario(
            "t2", "2026-01-01", "2026-01-05",
            [Account("A", "a", money("100")), Account("B", "b")],
            transfer_rules=[TransferRule("A", "B")],
        )
        orders = [
            TransferOrder("X1", "A", "B", "2026-01-02", "2026-01-02",
                          money("60"), money("0")),
            TransferOrder("X2", "A", "B", "2026-01-02", "2026-01-02",
                          money("60"), money("0")),
        ]
        with self.assertRaises(LedgerError):
            replay(scenario, orders)

    def test_same_day_arrival_cannot_relay(self):
        # C 当日从 A 收到的钱，当日不能再作为 C->B 的发起资金
        scenario = Scenario(
            "t3", "2026-01-01", "2026-01-05",
            [Account("A", "a", money("100"))] +
            [Account(x, x) for x in ("B", "C")],
            transfer_rules=[TransferRule("A", "C"), TransferRule("C", "B")],
        )
        orders = [
            TransferOrder("X1", "A", "C", "2026-01-02", "2026-01-02",
                          money("100"), money("0")),
            TransferOrder("X2", "C", "B", "2026-01-02", "2026-01-02",
                          money("100"), money("0")),
        ]
        with self.assertRaises(LedgerError):
            replay(scenario, orders)

    def test_duplicate_order_id_rejected(self):
        scenario = sc_two_accounts()
        orders = [
            TransferOrder("X1", "A", "B", "2026-01-02", "2026-01-02",
                          money("10"), money("0")),
            TransferOrder("X1", "A", "B", "2026-01-03", "2026-01-03",
                          money("10"), money("0")),
        ]
        with self.assertRaises(LedgerError):
            replay(scenario, orders)

    def test_conservation_with_fee_and_transit(self):
        scenario = sc_two_accounts(lag_days=1, fee_fixed=Decimal("3"))
        orders = [TransferOrder("X1", "A", "B", "2026-01-01", "2026-01-02",
                                money("50"), money("3"))]
        result = replay(scenario, orders)
        # 期初100 = 期末A47 + 在途50 + 费3
        self.assertEqual(result.ledger.available("A"), money("47"))
        result.ledger.verify_conservation()


class TestSolver(unittest.TestCase):
    def test_priority_first_then_fee(self):
        # 直达收费、两跳免费；资金只够覆盖 P1+部分 P2 时，P3 留缺口
        from examples.run_demo import build_demo
        scenario = build_demo()
        base = simulate_baseline(scenario).gap_total_by_priority()
        self.assertEqual(base[1], money("600"))
        result = solve(scenario)
        plan = result.plan
        self.assertEqual(plan.total_fee, money("0"))  # 选中免费两跳路径
        gaps = {g["flow_id"]: g["shortfall"] for g in plan.remaining_gaps}
        self.assertNotIn("W1", gaps)   # P1 工资覆盖
        self.assertNotIn("R1", gaps)   # P2 房租覆盖（借助10-04回款500）
        self.assertEqual(gaps["X1"], "800.00")  # P3 资金不足留缺口

    def test_greedy_matches_brute_force(self):
        from examples.run_demo import build_demo
        scenario = build_demo()
        plan = solve(scenario).plan
        brute = brute_force_solve(scenario)
        self.assertEqual(tuple(plan.objective), tuple(brute.objective))
        self.assertEqual(plan.total_fee, brute.total_fee)

    def test_brute_prefers_cheaper_direct_when_tight_deadline(self):
        # lag 导致两跳来不及：10-03 用款，两跳必须 10-02 前完成，
        # 只能选收费直达 -> 计划含手续费
        from examples.run_demo import build_demo
        scenario = build_demo()
        # 把免费两跳也改成需要两个完整在途日，工资日仍为10-03
        for r in scenario.transfer_rules:
            if (r.source, r.target) in (("A", "C"), ("C", "B")):
                object.__setattr__(r, "lag_days", 2)
        plan = solve(scenario).plan
        w1_gap = [g for g in plan.remaining_gaps if g["flow_id"] == "W1"]
        # 直达 A->B lag=1：工资10-02发起10-03到账(费12)，房租缺口200同样直达(费4)
        self.assertEqual(w1_gap, [])
        self.assertEqual(plan.total_fee, money("16"))


class TestPersistence(unittest.TestCase):
    def test_scenario_roundtrip(self, tmp_path=None):
        import tempfile
        from pathlib import Path
        from cashflow.repo import Repository
        from examples.run_demo import build_demo
        with tempfile.TemporaryDirectory() as d:
            repo = Repository(Path(d))
            repo.save_scenario(build_demo())
            loaded = repo.load_scenario("demo01")
            self.assertEqual(loaded.account_ids, {"A", "B", "C"})
            self.assertEqual(len(loaded.transfer_rules), 3)

    def test_invalid_scenario(self):
        bad = Scenario(
            "bad", "2026-01-10", "2026-01-01",
            [Account("A", "a")],
        )
        with self.assertRaises(ValueError):
            bad.validate()


if __name__ == "__main__":
    unittest.main()
