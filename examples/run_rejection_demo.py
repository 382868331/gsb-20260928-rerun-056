"""第五轮演示：在途划拨的部分拒绝、跨账户退回与费用调整。

场景 demo04（2026-10-01..10）：
  A 资金户 2000 / B 业务户 0 / C 备用户 0
  规则：A->B 3 日到账，费率 1%（原手续费不退）
  收支：10-06 货款 S1 500(P2)

实际经过：
  T1（A->B 1000，费 10）10-01 发起，10-04 才到账。在途期间：
  - 10-02 收到拒绝通知 R1：400 将跨账户退回 C（预计 10-03，退回费 5）。
    通知本身不动资金；
  - 10-03 R1 实际退回：C 收到 395，在途本金剩 600；
  - 10-02 又收到 R2：200 将退回 A（预计 10-05，退回费 2）；
    研究者先把预计日改为 10-06，又在 10-03 撤销该安排（未执行），
    这 200 仍随原划拨归收款方；
  - 10-04 T1 到账：B 只记 600（已退回的 400 不会再次记给收款方）。

演示要点：
  1. 拒绝通知 / 实际退回 / 收款到账三类事件严格区分；
  2. 重复通知与乱序登记不制造现金，退回总额不得超过本金；
  3. 执行记录保存中途状态后重开，重放事件仍满足资金守恒。
"""

from __future__ import annotations

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
from cashflow.report import daily_ledger, event_log, reconciliation
from cashflow.repo import Repository


def build_demo04() -> Scenario:
    return Scenario(
        scenario_id="demo04",
        description="在途划拨部分拒绝：跨账户退回、退回费用与退回撤销",
        start_date="2026-10-01",
        end_date="2026-10-10",
        accounts=[
            Account("A", "资金户", money("2000")),
            Account("B", "业务户", money("0")),
            Account("C", "备用户", money("0")),
        ],
        flows=[
            CashFlow("S1", "B", "2026-10-06", FlowKind.OUTFLOW, money("500"),
                     Priority.P2_NORMAL, "货款"),
        ],
        transfer_rules=[
            TransferRule("A", "B", lag_days=3, fee_rate=Decimal("0.01")),
        ],
    )


def main() -> None:
    scenario = build_demo04()
    execution = Execution(
        scenario_id="demo04", plan_created_at="manual",
        recorded_at="2026-10-01T09:00:00",
        actual_transfers=[
            ActualTransfer("T1", "A", "B", "2026-10-01", "2026-10-04",
                           money("1000"), money("10")),
        ],
        initiated_order_ids=["T1"],
    )

    print("== 10-02 拒绝通知（不动资金）==")
    note = record_return_notice(execution, TransferReturn(
        "R1", "T1", "C", "2026-10-03", money("400"),
        return_fee=money("5"), note="部分拒绝，跨账户退回备用户"))
    print(f"  R1: {note['handling']}")
    note = record_return_notice(execution, TransferReturn(
        "R2", "T1", "A", "2026-10-05", money("200"),
        return_fee=money("2"), note="另一部分退回付款方"))
    print(f"  R2: {note['handling']}")
    dup = record_return_notice(execution, TransferReturn(
        "R1", "T1", "C", "2026-10-03", money("400"), return_fee=money("5"),
        note="部分拒绝，跨账户退回备用户"))
    print(f"  R1 重复通知: {dup['handling']}")

    print("== 10-03 R1 实际退回；R2 改预计日后撤销 ==")
    print(f"  R1 实际退回: {record_return_arrival(execution, 'R1', '2026-10-03')['handling']}")
    revise_return_schedule(execution, "R2", "2026-10-06")
    print("  R2 预计退回日改为 2026-10-06（尚未执行）")
    print(f"  R2 撤销: {cancel_return_arrangement(execution, 'R2', '2026-10-03')['handling']}")

    print("== 10-04 日终去向核对 ==")
    disp = transfer_disposition(scenario, execution, "2026-10-04")
    for t in disp["transfers"]:
        print(f"  划拨 {t['order_id']} 本金 {t['principal']}: "
              f"已退回可用 {t['returned_arrived_usable']}（至 C），"
              f"撤销归还收款方 {t['return_cancelled_back_to_payee']}，"
              f"收款方到账 {t['delivered_to_payee']}，"
              f"退回费 {t['return_fees_charged']}（原手续费不退）")
    print(f"  余额: {disp['balances']}")

    print()
    print("== 保存中途状态后重开，重放全部事件 ==")
    repo = Repository("data")
    repo.save_scenario(scenario)
    path = repo.save_execution(execution)
    loaded = Execution.load(path)
    result = replay_with_returns(scenario, loaded)
    print(event_log(result))
    print()
    print(daily_ledger(result))
    print()
    print(reconciliation(result, scenario))
    print(f"\n执行记录已保存并重开：{path}")


if __name__ == "__main__":
    main()
