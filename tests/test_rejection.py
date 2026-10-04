"""第五轮：在途划拨的部分拒绝与退回测试。"""

import unittest
from decimal import Decimal

from cashflow import (
    Account, CashFlow, FlowKind, Priority, Scenario, TransferRule,
    Execution, TransferReturn, money,
)
from cashflow.plan import ActualTransfer
from cashflow.rejection import (
    cancel_return_arrangement, record_return_arrival, record_return_notice,
    replay_with_returns, revise_return_schedule, transfer_disposition,
)
from cashflow.replan import build_prefix


def build() -> Scenario:
    return Scenario(
        "rj", "2026-10-01", "2026-10-10",
        [Account("A", "资金户", money("2000")),
         Account("B", "业务户"),
         Account("C", "备用户")],
        flows=[
            CashFlow("S1", "B", "2026-10-06", FlowKind.OUTFLOW, money("500"),
                     Priority.P2_NORMAL, "货款"),
        ],
        transfer_rules=[
            TransferRule("A", "B", lag_days=3, fee_rate=Decimal("0.01")),
        ],
    )


def executed() -> Execution:
    """T1：A->B 1000，费 10，10-01 发起，10-04 到账。"""
    return Execution(
        scenario_id="rj", plan_created_at="p", recorded_at="2026-10-01T09:00:00",
        actual_transfers=[
            ActualTransfer("T1", "A", "B", "2026-10-01", "2026-10-04",
                           money("1000"), money("10")),
        ],
        initiated_order_ids=["T1"],
    )


def notice_r1() -> TransferReturn:
    return TransferReturn("R1", "T1", "C", "2026-10-03", money("400"),
                          return_fee=money("5"), note="跨账户退回到备用户")


class TestNoticeVsActual(unittest.TestCase):
    def test_notice_does_not_move_funds(self):
        sc, execution = build(), executed()
        record_return_notice(execution, notice_r1())
        prefix = build_prefix(sc, execution, "2026-10-02")
        # 只有通知：A 扣本金+费，1000 全部仍在途，C 一分未增
        self.assertEqual(prefix.ledger.available("A"), money("990"))
        self.assertEqual(prefix.ledger.available("C"), money("0"))
        self.assertEqual(prefix.ledger.in_transit["T1"].principal,
                         money("1000"))
        prefix.ledger.verify_conservation()

    def test_actual_return_cross_account_and_payee_not_double_credited(self):
        sc, execution = build(), executed()
        record_return_notice(execution, notice_r1())
        record_return_arrival(execution, "R1", "2026-10-03")
        result = replay_with_returns(sc, execution)
        led = result.ledger
        # 实际退回：C 收到 400-5=395；T1 到账时 B 只记 600，不是 1000
        self.assertEqual(led.available("A"), money("990"))
        self.assertEqual(led.available("C"), money("395"))
        self.assertEqual(led.available("B"), money("100"))  # 600-500 货款
        self.assertEqual(led.total_fees, money("15"))       # 原费 10 + 退回费 5
        arrive = [e for e in led.log if e["event"] == "transfer_arrive"][0]
        self.assertEqual(arrive["principal"], "600.00")
        led.verify_conservation()


class TestMultipleReturnsCapAndDedup(unittest.TestCase):
    def test_over_cap_rejected(self):
        execution = executed()
        record_return_notice(execution, notice_r1())
        with self.assertRaises(ValueError):
            record_return_notice(execution, TransferReturn(
                "R2", "T1", "A", "2026-10-03", money("700")))
        # 未登记的退回不能乱序登记实际退回
        with self.assertRaises(ValueError):
            record_return_arrival(execution, "R9", "2026-10-03")

    def test_duplicate_correction_and_out_of_order(self):
        sc = build()
        execution = executed()
        # 乱序：先登记 R2 再登记 R1
        record_return_notice(execution, TransferReturn(
            "R2", "T1", "A", "2026-10-03", money("200"),
            return_fee=money("2")))
        notes = record_return_notice(execution, notice_r1())
        self.assertEqual(notes["action"], "recorded")
        # 重复通知被忽略；更正在实际到达前覆盖
        self.assertEqual(record_return_notice(execution, notice_r1())["action"],
                         "duplicate_ignored")
        record_return_notice(execution, TransferReturn(
            "R2", "T1", "A", "2026-10-03", money("150"),
            return_fee=money("2")))
        self.assertEqual(len(execution.transfer_returns), 2)
        record_return_arrival(execution, "R2", "2026-10-03")
        record_return_arrival(execution, "R1", "2026-10-03")
        # 重复的实际退回通知不制造第二份现金
        self.assertEqual(
            record_return_arrival(execution, "R1", "2026-10-03")["action"],
            "duplicate_ignored")
        result = replay_with_returns(sc, execution)
        led = result.ledger
        # C: 400-5=395；A: 2000-1000-10+150-2=1138；B: 450 付货款后 0... 
        self.assertEqual(led.available("C"), money("395"))
        self.assertEqual(led.available("A"), money("1138"))
        # B 到账 1000-400-150=450，付 S1 500 只能付 450
        self.assertEqual(led.available("B"), money("0"))
        self.assertEqual(led.total_fees, money("17"))
        led.verify_conservation()
        # 已实际到账的退回不能撤销或覆盖
        with self.assertRaises(ValueError):
            cancel_return_arrangement(execution, "R1", "2026-10-04")
        with self.assertRaises(ValueError):
            record_return_notice(execution, notice_r1().__class__(
                "R1", "T1", "C", "2026-10-03", money("300")))


class TestReviseAndCancel(unittest.TestCase):
    def test_revise_then_cancel_keeps_arrived(self):
        sc, execution = build(), executed()
        record_return_notice(execution, notice_r1())
        record_return_arrival(execution, "R1", "2026-10-03")
        record_return_notice(execution, TransferReturn(
            "R2", "T1", "A", "2026-10-05", money("200")))
        # 修改预计到账时间
        revise_return_schedule(execution, "R2", "2026-10-06")
        r2 = next(r for r in execution.transfer_returns
                  if r.return_id == "R2")
        self.assertEqual(r2.expected_date, "2026-10-06")
        # 撤销尚未执行的退回：200 仍归收款方 B
        cancel_return_arrangement(execution, "R2", "2026-10-03")
        result = replay_with_returns(sc, execution)
        led = result.ledger
        # B 到账 1000-400=600（R2 已撤销），付 500 后余 100
        self.assertEqual(led.available("B"), money("100"))
        self.assertEqual(led.available("C"), money("395"))  # R1 实际到账保留
        led.verify_conservation()

    def test_cancel_after_split_delivers_to_payee(self):
        """划拨到账时退回仍在途（拆分），之后撤销则本金转交原收款方。"""
        sc, execution = build(), executed()
        record_return_notice(execution, TransferReturn(
            "R3", "T1", "C", "2026-10-08", money("300"),
            return_fee=money("3")))
        # T1 于 10-04 到账：B 先收 700，300 拆分退回在途
        prefix = build_prefix(sc, execution, "2026-10-04")
        self.assertEqual(prefix.ledger.available("B"), money("700"))
        self.assertEqual(prefix.ledger.return_in_transit["R3"]["amount"],
                         money("300"))
        prefix.ledger.verify_conservation()
        # 撤销：300 转交 B
        cancel_return_arrangement(execution, "R3", "2026-10-05")
        result = replay_with_returns(sc, execution)
        self.assertEqual(result.ledger.available("B"), money("500"))
        result.ledger.verify_conservation()


class TestSaveReloadReplay(unittest.TestCase):
    def test_mid_state_save_reload_conservation(self, tmp_path=None):
        import tempfile, os
        sc, execution = build(), executed()
        record_return_notice(execution, notice_r1())
        record_return_arrival(execution, "R1", "2026-10-03")
        record_return_notice(execution, TransferReturn(
            "R2", "T1", "A", "2026-10-06", money("200"),
            return_fee=money("2")))
        # 保存中途状态后重开
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "execution.json")
            execution.save(path)
            loaded = Execution.load(path)
        self.assertEqual(len(loaded.transfer_returns), 2)
        # 重开后继续：R2 实际退回（迟于预计日）
        record_return_arrival(loaded, "R2", "2026-10-07")
        result = replay_with_returns(sc, loaded)
        led = result.ledger
        led.verify_conservation()
        self.assertEqual(led.available("C"), money("395"))
        self.assertEqual(led.available("A"),
                         money("2000") - money("1010") + money("198"))
        self.assertEqual(led.available("B"), money("0"))  # 400 到账全付货款
        self.assertEqual(led.total_fees, money("17"))

    def test_disposition_report(self):
        sc, execution = build(), executed()
        record_return_notice(execution, notice_r1())
        record_return_arrival(execution, "R1", "2026-10-03")
        record_return_notice(execution, TransferReturn(
            "R2", "T1", "A", "2026-10-06", money("200")))
        disp = transfer_disposition(sc, execution, "2026-10-03")
        entry = disp["transfers"][0]
        self.assertEqual(entry["principal"], "1000.00")
        self.assertEqual(entry["returned_arrived_usable"], "400.00")
        self.assertEqual(entry["return_pending_not_usable"], "200.00")
        self.assertEqual(entry["delivered_to_payee"], "400.00")
        self.assertEqual(entry["return_fees_charged"], "5.00")
        self.assertEqual(disp["balances"]["C"], "395.00")
        self.assertEqual(disp["in_transit"][0]["remaining_principal"],
                         "600.00")


if __name__ == "__main__":
    unittest.main()

