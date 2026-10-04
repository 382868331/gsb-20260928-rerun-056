"""第五轮演示：在途划拨被部分拒绝的完整生命周期。

场景：资金户 A 向业务户 B 调拨 1000（在途 3 天，手续费 10 不退）。
在途期间收款方分两次拒绝：400 跨账户退回备用金户 C（退回费用 6 不退），
200 退回 A；随后研究者修改预计到账日、撤销尚未执行的第二笔退回，
中途保存执行记录并重开，最后重放全部事件验证资金守恒。
"""

from decimal import Decimal
from pathlib import Path
import tempfile

from cashflow import (
    Account, CashFlow, FlowKind, Priority, Scenario, TransferRule,
    Execution, RejectionEvent, money,
)
from cashflow.plan import ActualTransfer
from cashflow.rejection import (
    record_rejections, revise_rejection, replay_with_rejections,
    rejection_disposition,
)
from cashflow.repo import Repository
from cashflow.report import (
    daily_ledger, event_log, reconciliation, rejection_report,
)


def main() -> None:
    scenario = Scenario(
        "rj-demo", "2026-10-01", "2026-10-12",
        [Account("A", "资金户", money("2000")),
         Account("B", "业务户"),
         Account("C", "备用金户")],
        flows=[
            CashFlow("OUT1", "B", "2026-10-08", FlowKind.OUTFLOW,
                     money("500"), Priority.P1_CRITICAL, "工资"),
            CashFlow("IN1", "B", "2026-10-10", FlowKind.INFLOW,
                     money("300"), memo="回款"),
        ],
        transfer_rules=[
            TransferRule("A", "B", lag_days=3, fee_rate=Decimal("0.01")),
            TransferRule("A", "C", lag_days=1),
        ],
        description="在途划拨部分拒绝演示",
    )

    execution = Execution(
        scenario_id=scenario.scenario_id, plan_created_at="demo",
        recorded_at="2026-10-02T09:00:00",
        actual_transfers=[
            ActualTransfer("T1", "A", "B", "2026-10-01", "2026-10-04",
                           money("1000"), money("10")),
        ],
        initiated_order_ids=["T1"],
    )

    print("== 10-02/10-03 在途期间收到两笔拒绝通知（乱序登记 + 重复通知）==")
    for note in record_rejections(execution, [
        RejectionEvent("RJ2", "T1", "2026-10-03", money("200"), "A",
                       "2026-10-07", note="第二笔，先登记"),
        RejectionEvent("RJ1", "T1", "2026-10-02", money("400"), "C",
                       "2026-10-06", return_fee=money("6"),
                       note="跨账户退回备用金户"),
        RejectionEvent("RJ1", "T1", "2026-10-02", money("400"), "C",
                       "2026-10-06", return_fee=money("6"),
                       note="跨账户退回备用金户"),  # 重复通知
    ]):
        print(f"  {note['rejection_id']}: {note['handling']}")

    with tempfile.TemporaryDirectory() as d:
        repo = Repository(Path(d))
        mid = repo.save_execution(execution)
        print(f"\n== 中途状态已保存：{mid.name}，重开后继续 ==")
        execution = Execution.load(mid)

        print("\n== 研究者修改 RJ1 预计到账日 10-06 -> 10-08（退回迟到）==")
        print("  ", revise_rejection(
            execution, "RJ1", expected_return_date="2026-10-08")["handling"])
        print("\n== 撤销尚未执行的 RJ2（200 留在原划拨随原到账日记给 B）==")
        print("  ", execution.cancel_rejection("RJ2")["handling"])
        print("\n== RJ1 实际于 10-08 到达 ==")
        print("  ", revise_rejection(
            execution, "RJ1", actual_return_date="2026-10-08")["handling"])
        repo.save_execution(execution)

    print("\n" + rejection_report(rejection_disposition(scenario, execution)))
    result = replay_with_rejections(scenario, execution)
    print("\n" + event_log(result))
    print("\n" + daily_ledger(result))
    print("\n" + reconciliation(result, scenario))
    result.ledger.verify_conservation()
    print("\n资金守恒核对通过：通知不产生现金、退回只入账一次、"
          "收款方只收到未退回部分、原手续费与退回费用均不退。")


if __name__ == "__main__":
    main()
