"""第五轮：在途划拨部分拒绝、退回与收款到账测试。"""

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from cashflow import (
    Account, CashFlow, FlowKind, Priority, Scenario, TransferRule,
    Execution, RejectionEvent, LedgerError, money,
)
from cashflow.plan import ActualTransfer
from cashflow.rejection import (
    record_rejections, revise_rejection, replay_with_rejections,
    rejection_disposition,
)
from cashflow.repo import Repository


def build() -> Scenario:
    return Scenario(
        "rj", "2026-10-01", "2026-10-12",
        [Account("A", "资金户", money("2000")),
         Account("B", "业务户"),
         Account("C", "备用金户")],
        flows=[
            CashFlow("OUT1", "B", "2026-10-08", FlowKind.OUTFLOW,
                     money("500"), Priority.P1_CRITICAL, "工资"),
            CashFlow("IN1", "B", "2026-10-10", FlowKind.INFLOW,
                     money("300")),
        ],
        transfer_rules=[
            TransferRule("A", "B", lag_days=3, fee_rate=Decimal("0.01")),
            TransferRule("A", "C", lag_days=1),
        ],
    )


def base_execution() -> Execution:
    """T1：A->B 本金 1000、费 10，10-01 发起、10-04 到账（在途 3 天）。"""
    return Execution(
        scenario_id="rj", plan_created_at="p",
        recorded_at="2026-10-02T09:00:00",
        actual_transfers=[
            ActualTransfer("T1", "A", "B", "2026-10-01", "2026-10-04",
                           money("1000"), money("10")),
        ],
        initiated_order_ids=["T1"],
    )


def snap(result, day):
    return next(s for s in result.ledger.snapshots if s.date == day)


class TestRejectionNoticeVsArrival(unittest.TestCase):
    def test_notice_adds_no_funds_only_arrival_does(self):
        sc = build()
        execution = base_execution()
        record_rejections(execution, [
            RejectionEvent("RJ1", "T1", "2026-10-02", money("400"), "C",
                           "2026-10-06", return_fee=money("6")),
        ])
        result = replay_with_rejections(sc, execution)
        # 通知日：可用余额不变，拒绝部分只是换了一段在途
        s = snap(result, "2026-10-02")
        self.assertEqual(s.balances["A"], money("990"))   # 2000-1000-10
        self.assertEqual(s.balances["C"], money("0"))     # 通知不入账
        self.assertEqual(s.in_transit["T1"], money("600"))
        self.assertEqual(s.in_transit["RET-RJ1"], money("394"))  # 400-6
        # 原划拨 10-04 到账：收款方只收到剩余 600，不是全额 1000
        s = snap(result, "2026-10-04")
        self.assertEqual(s.balances["B"], money("600"))
        self.assertNotIn("T1", s.in_transit)
        # 预计退回日 10-06 经过但没有实际确认：退回段留在在途，
        # 可用余额不增加，直到期末也不入账
        for day in ("2026-10-05", "2026-10-06", "2026-10-07",
                    "2026-10-12"):
            s = snap(result, day)
            self.assertEqual(s.balances["C"], money("0"))
            self.assertEqual(s.in_transit["RET-RJ1"], money("394"))
        result.ledger.verify_conservation()

    def test_payee_not_credited_returned_part_again(self):
        sc = build()
        execution = base_execution()
        record_rejections(execution, [
            RejectionEvent("RJ1", "T1", "2026-10-02", money("400"), "C",
                           "2026-10-06", return_fee=money("6")),
        ])
        result = replay_with_rejections(sc, execution)
        arrivals = [e for e in result.ledger.log
                    if e["event"] == "transfer_arrive"
                    and e["order_id"] == "T1"]
        self.assertEqual(len(arrivals), 1)
        self.assertEqual(arrivals[0]["principal"], "600.00")
        # 原手续费 10 不退，新退回费用 6 计入累计费用
        self.assertEqual(result.ledger.total_fees, money("16"))

    def test_cross_account_return_and_fees(self):
        sc = build()
        execution = base_execution()
        record_rejections(execution, [
            RejectionEvent("RJ1", "T1", "2026-10-02", money("400"), "C",
                           "2026-10-06", return_fee=money("6"),
                           actual_return_date="2026-10-06"),
            RejectionEvent("RJ2", "T1", "2026-10-03", money("200"), "A",
                           "2026-10-05", actual_return_date="2026-10-05"),
        ])
        result = replay_with_rejections(sc, execution)
        # RJ2 退回原付款账户 A，无退回费用
        self.assertEqual(snap(result, "2026-10-05").balances["A"],
                         money("990") + money("200"))
        # RJ1 跨账户退到 C，扣退回费用 6
        self.assertEqual(snap(result, "2026-10-06").balances["C"],
                         money("394"))
        # 收款方最终只收到 1000-400-200=400
        self.assertEqual(snap(result, "2026-10-04").balances["B"],
                         money("400"))
        result.ledger.verify_conservation()


class TestRejectionGuards(unittest.TestCase):
    def test_total_cap_and_unknown_source(self):
        sc = build()
        execution = base_execution()
        record_rejections(execution, [
            RejectionEvent("RJ1", "T1", "2026-10-02", money("700"), "A",
                           "2026-10-05"),
            RejectionEvent("RJ2", "T1", "2026-10-03", money("400"), "A",
                           "2026-10-06"),
        ])
        with self.assertRaises(LedgerError):
            replay_with_rejections(sc, execution)  # 700+400 > 1000
        execution2 = base_execution()
        record_rejections(execution2, [
            RejectionEvent("RJ9", "T-UNKNOWN", "2026-10-02", money("1"),
                           "A", "2026-10-05"),
        ])
        with self.assertRaises(LedgerError):
            replay_with_rejections(sc, execution2)

    def test_duplicate_and_out_of_order_records_make_no_cash(self):
        sc = build()
        execution = base_execution()
        e1 = RejectionEvent("RJ1", "T1", "2026-10-02", money("400"), "C",
                            "2026-10-06", return_fee=money("6"),
                            actual_return_date="2026-10-06")
        e2 = RejectionEvent("RJ2", "T1", "2026-10-03", money("200"), "A",
                            "2026-10-05", actual_return_date="2026-10-05")
        # 乱序登记 + 重复通知
        notes = record_rejections(execution, [e2, e1, e1, e2])
        self.assertEqual([n["action"] for n in notes],
                         ["recorded", "recorded",
                          "duplicate_ignored", "duplicate_ignored"])
        self.assertEqual(len(execution.rejections), 2)
        result = replay_with_rejections(sc, execution)
        # 与按顺序登记完全一致：退回只入账一次
        ordered = base_execution()
        record_rejections(ordered, [e1, e2])
        result2 = replay_with_rejections(sc, ordered)
        self.assertEqual(result.ledger.balances, result2.ledger.balances)
        returns = [e for e in result.ledger.log
                   if e["event"] == "reject_return_arrive"]
        self.assertEqual(len(returns), 2)
        self.assertEqual(sum((Decimal(e["amount"]) for e in returns),
                             Decimal("0")), money("594"))

    def test_correction_replaces_not_adds(self):
        execution = base_execution()
        record_rejections(execution, [
            RejectionEvent("RJ1", "T1", "2026-10-02", money("400"), "C",
                           "2026-10-06")])
        # 更正金额 400->300：覆盖而不是新增
        note = record_rejections(execution, [
            RejectionEvent("RJ1", "T1", "2026-10-02", money("300"), "C",
                           "2026-10-06")])[0]
        self.assertEqual(note["action"], "corrected")
        self.assertEqual(len(execution.rejections), 1)
        result = replay_with_rejections(build(), execution)
        self.assertEqual(snap(result, "2026-10-04").balances["B"],
                         money("700"))


class TestReviseAndCancel(unittest.TestCase):
    def test_revise_expected_then_actual_arrival(self):
        sc = build()
        execution = base_execution()
        record_rejections(execution, [
            RejectionEvent("RJ1", "T1", "2026-10-02", money("400"), "C",
                           "2026-10-06", return_fee=money("6"))])
        # 研究者修改预计到账日 10-06 -> 10-08
        note = revise_rejection(execution, "RJ1",
                                expected_return_date="2026-10-08")
        self.assertEqual(note["action"], "corrected")
        result = replay_with_rejections(sc, execution)
        # 只有预计日期、没有实际确认：两个预计日经过都不入账
        self.assertEqual(snap(result, "2026-10-06").balances["C"], money("0"))
        self.assertEqual(snap(result, "2026-10-08").balances["C"], money("0"))
        self.assertEqual(snap(result, "2026-10-08").in_transit["RET-RJ1"],
                         money("394"))
        # 之后补记实际退回日（迟到一天 10-09）
        revise_rejection(execution, "RJ1", actual_return_date="2026-10-09")
        result = replay_with_rejections(sc, execution)
        # 实际晚于预计：不得改写成提前可用，10-08 仍不可用
        self.assertEqual(snap(result, "2026-10-08").balances["C"], money("0"))
        self.assertEqual(snap(result, "2026-10-09").balances["C"],
                         money("394"))
        result.ledger.verify_conservation()

    def test_cancel_unexecuted_keeps_arrived(self):
        sc = build()
        execution = base_execution()
        record_rejections(execution, [
            RejectionEvent("RJ1", "T1", "2026-10-02", money("400"), "C",
                           "2026-10-05",
                           actual_return_date="2026-10-05"),
            RejectionEvent("RJ2", "T1", "2026-10-03", money("200"), "A",
                           "2026-10-06"),
        ])
        # 撤销尚未执行的 RJ2：200 留在原划拨上，随原到账日记给 B
        note = execution.cancel_rejection("RJ2")
        self.assertEqual(note["action"], "cancelled")
        # 已实际到账的 RJ1 不得撤销
        note = execution.cancel_rejection("RJ1")
        self.assertEqual(note["action"], "rejected")
        result = replay_with_rejections(sc, execution)
        self.assertEqual(snap(result, "2026-10-04").balances["B"],
                         money("600"))   # 1000-400，撤销的 200 回到原划拨
        self.assertEqual(snap(result, "2026-10-05").balances["C"],
                         money("400"))   # 既有实际到账保留
        result.ledger.verify_conservation()


class TestExpectedVsActualSeparation(unittest.TestCase):
    """预计退回与实际可用资金严格分开。"""

    def test_expected_date_passes_without_confirmation_no_credit(self):
        sc = build()
        execution = base_execution()
        record_rejections(execution, [
            RejectionEvent("RJ1", "T1", "2026-10-02", money("400"), "C",
                           "2026-10-06", return_fee=money("6")),
        ])
        result = replay_with_rejections(sc, execution)
        # 预计日 10-06 经过、无实际确认：C 的可用余额始终不增加，
        # 退回净额一直挂在在途（本金与在途守恒）
        final = snap(result, "2026-10-12")
        self.assertEqual(final.balances["C"], money("0"))
        self.assertEqual(final.in_transit["RET-RJ1"], money("394"))
        result.ledger.verify_conservation()
        # 预测另行展示并清晰标注为不可用
        disp = rejection_disposition(sc, execution)
        order = disp["orders"][0]
        self.assertEqual(order["returned_arrived_usable"], "0.00")
        self.assertEqual(order["return_in_transit_not_usable"], "394.00")
        entry = order["rejections"][0]
        self.assertEqual(entry["status"], "return_in_transit")
        self.assertEqual(entry["expected_return_date"], "2026-10-06")
        self.assertIsNone(entry["actual_return_date"])

    def test_actual_confirmed_late_never_available_early(self):
        sc = build()
        execution = base_execution()
        record_rejections(execution, [
            RejectionEvent("RJ1", "T1", "2026-10-02", money("400"), "A",
                           "2026-10-05",
                           actual_return_date="2026-10-09"),
        ])
        result = replay_with_rejections(sc, execution)
        # 预计 10-05、实际 10-09：10-05 不得提前可用，10-09 才入账
        self.assertEqual(snap(result, "2026-10-05").balances["A"],
                         money("990"))
        self.assertEqual(snap(result, "2026-10-08").in_transit["RET-RJ1"],
                         money("400"))
        self.assertEqual(snap(result, "2026-10-09").balances["A"],
                         money("1390"))
        result.ledger.verify_conservation()

    def test_duplicate_actual_confirmation_no_double_credit(self):
        sc = build()
        execution = base_execution()
        event = RejectionEvent("RJ1", "T1", "2026-10-02", money("400"), "C",
                               "2026-10-06", return_fee=money("6"),
                               actual_return_date="2026-10-06")
        notes = record_rejections(execution, [event, event])
        self.assertEqual([n["action"] for n in notes],
                         ["recorded", "duplicate_ignored"])
        # 重复补记同一实际到账日：覆盖而非新增
        revise_rejection(execution, "RJ1", actual_return_date="2026-10-06")
        self.assertEqual(len(execution.rejections), 1)
        result = replay_with_rejections(sc, execution)
        returns = [e for e in result.ledger.log
                   if e["event"] == "reject_return_arrive"]
        self.assertEqual(len(returns), 1)
        self.assertEqual(snap(result, "2026-10-06").balances["C"],
                         money("394"))
        result.ledger.verify_conservation()

    def test_save_reload_replay_with_unconfirmed_expected(self):
        sc = build()
        execution = base_execution()
        record_rejections(execution, [
            RejectionEvent("RJ1", "T1", "2026-10-02", money("400"), "C",
                           "2026-10-06", return_fee=money("6")),
        ])
        with tempfile.TemporaryDirectory() as d:
            repo = Repository(Path(d))
            reloaded = Execution.load(repo.save_execution(execution))
        result = replay_with_rejections(sc, reloaded)
        final = snap(result, "2026-10-12")
        self.assertEqual(final.balances["C"], money("0"))
        self.assertEqual(final.in_transit["RET-RJ1"], money("394"))
        result.ledger.verify_conservation()


class TestMidStateReplay(unittest.TestCase):
    def test_save_mid_state_reload_and_replay_conserves(self):
        sc = build()
        execution = base_execution()
        record_rejections(execution, [
            RejectionEvent("RJ1", "T1", "2026-10-02", money("400"), "C",
                           "2026-10-06", return_fee=money("6")),
        ])
        with tempfile.TemporaryDirectory() as d:
            repo = Repository(Path(d))
            # 中途存档：只有拒绝通知，退回尚未到达
            path = repo.save_execution(execution)
            reloaded = Execution.load(path)
            self.assertEqual(reloaded.rejections, execution.rejections)
            # 重开后继续追加事件：第二笔拒绝、修改预计日、撤销
            record_rejections(reloaded, [
                RejectionEvent("RJ2", "T1", "2026-10-03", money("200"),
                               "A", "2026-10-07")])
            revise_rejection(reloaded, "RJ1",
                             expected_return_date="2026-10-08",
                             actual_return_date="2026-10-08")
            reloaded.cancel_rejection("RJ2")
            path2 = repo.save_execution(reloaded)
            final = Execution.load(path2)
        result = replay_with_rejections(sc, final)
        result.ledger.verify_conservation()
        # RJ2 已撤销：B 收到 600；RJ1 改期后 10-08 到 C
        self.assertEqual(snap(result, "2026-10-04").balances["B"],
                         money("600"))
        self.assertEqual(snap(result, "2026-10-08").balances["C"],
                         money("394"))
        # 资金守恒：期末余额+在途+费用 = 期初+收入-支出
        disp = rejection_disposition(sc, final)
        order = disp["orders"][0]
        self.assertEqual(order["payee_receives"], "600.00")
        self.assertEqual(order["returned_arrived_usable"], "394.00")
        self.assertEqual(order["return_fees"], "6.00")
        statuses = {e["rejection_id"]: e["status"]
                    for e in order["rejections"]}
        self.assertEqual(statuses, {"RJ1": "arrived", "RJ2": "cancelled"})


if __name__ == "__main__":
    unittest.main()
