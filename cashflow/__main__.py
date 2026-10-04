"""命令行入口。

  python -m cashflow baseline <scenario.json>
  python -m cashflow solve <scenario.json> [--brute] [--data-dir data]
  python -m cashflow replay <scenario.json> <plan.json>
"""

from __future__ import annotations

import argparse
from pathlib import Path

from . import brute_force_solve, simulate_baseline, solve
from .engine import replay
from .plan import Plan
from .plan import Execution
from .report import daily_ledger, event_log, gap_report, plan_summary, reconciliation
from .report import payment_strategy_report, revision_summary
from .report import refund_report, refund_strategy_report
from .report import rejection_report
from .repo import Repository
from .scenario import Scenario
from .replan import FeeAmendment, replan, combined_replay
from .refund import compare_refund_strategies
from .plan import RefundRecord
from .plan import RejectionEvent, ActualTransfer
from .rejection import (record_rejections, revise_rejection,
                        replay_with_rejections, rejection_disposition)
from decimal import Decimal


def _print_gaps(gaps) -> None:
    if not gaps:
        print("无缺口")
        return
    print("缺口位置:")
    for g in gaps:
        print(f"  {g.date} {g.flow_id} 账户 {g.account_id} P{g.priority} "
              f"应付 {g.due} 已付 {g.paid} 缺口 {g.shortfall}")


def _load_scenario(value: str) -> Scenario:
    path = Path(value)
    if not path.exists() and path.suffix == "":
        path = path.with_suffix(".json")
    return Scenario.load(path)


def main() -> None:
    parser = argparse.ArgumentParser(description="资金流动推演系统")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_base = sub.add_parser("baseline", help="无调拨基线缺口")
    p_base.add_argument("scenario")

    p_solve = sub.add_parser("solve", help="生成调拨计划")
    p_solve.add_argument("scenario")
    p_solve.add_argument("--brute", action="store_true", help="小例子穷举求解")
    p_solve.add_argument("--data-dir", default="data")

    p_rep = sub.add_parser("replay", help="按计划重放并出账")
    p_rep.add_argument("scenario")
    p_rep.add_argument("plan")

    p_rev = sub.add_parser("replan", help="按实际执行与变动生成修订剩余计划")
    p_rev.add_argument("scenario")
    p_rev.add_argument("original_plan")
    p_rev.add_argument("execution")
    p_rev.add_argument("cutoff")
    p_rev.add_argument("--amend", action="append", default=[],
                       metavar="S,T,YYYY-MM-DD,rate,fixed",
                       help="费率调整，可多次；rate/fixed 填空表示不变")
    p_rev.add_argument("--inflow", action="append", default=[],
                       metavar="FLOW_ID,YYYY-MM-DD,amount",
                       help="修订未来收入到账日/金额，可多次")
    p_rev.add_argument("--no-defer-p3", action="store_true",
                       help="不允许推迟可选支付")
    p_rev.add_argument("--refund", action="append", default=[],
                       metavar="RID,FLOW,RET_ACCT,EXPECTED,ACTUAL,AMOUNT[,FEE]",
                       help="退回/拒付记录；ACTUAL 留空表示预计退回（不可用），可多次")
    p_rev.add_argument("--compare-refund", action="store_true",
                       help="对照 等待退回 vs 替代账户安排")
    p_rev.add_argument("--data-dir", default="data")

    p_rej = sub.add_parser("reject",
                           help="在途划拨部分拒绝：登记/修改/撤销并重放核对")
    p_rej.add_argument("scenario")
    p_rej.add_argument("execution",
                       help="执行记录路径；不存在则新建，存在则在其上追加事件")
    p_rej.add_argument("--transfer", action="append", default=[],
                       metavar="ORDER,S,T,INIT,ARRIVE,PRINCIPAL,FEE",
                       help="登记实际调拨（在途划拨），可多次")
    p_rej.add_argument("--reject", action="append", default=[],
                       metavar="RID,ORDER,NOTICE,AMOUNT,RET_ACCT,EXPECTED[,ACTUAL,FEE]",
                       help="拒绝通知；ACTUAL 留空表示退回未到达，可多次")
    p_rej.add_argument("--revise", action="append", default=[],
                       metavar="RID,EXPECTED[,ACTUAL]",
                       help="修改预计退回日/补记实际退回日，可多次")
    p_rej.add_argument("--cancel", action="append", default=[],
                       metavar="RID", help="撤销尚未执行的退回安排，可多次")

    args = parser.parse_args()

    if args.cmd == "baseline":
        scenario = _load_scenario(args.scenario)
        result = simulate_baseline(scenario)
        totals = result.gap_total_by_priority()
        print(f"基线缺口  P1={totals[1]}  P2={totals[2]}  P3={totals[3]}")
        _print_gaps(result.gaps)

    elif args.cmd == "solve":
        scenario = _load_scenario(args.scenario)
        if args.brute:
            plan = brute_force_solve(scenario)
        else:
            plan = solve(scenario).plan
        repo = Repository(args.data_dir)
        path = repo.save_plan(plan)
        print(plan_summary(plan))
        print()
        print(payment_strategy_report(plan))
        print()
        print(gap_report(plan))
        print(f"\n计划已保存：{path}")

    elif args.cmd == "replay":
        plan = Plan.load(args.plan)
        scenario = _load_scenario(args.scenario)
        result = replay(scenario, plan.orders, snapshot_daily=True)
        print(event_log(result))
        print()
        print(daily_ledger(result))
        print()
        print(reconciliation(result, scenario))

    elif args.cmd == "replan":
        scenario = _load_scenario(args.scenario)
        original = Plan.load(args.original_plan)
        execution = Execution.load(args.execution)
        amendments = []
        for item in args.amend:
            src, tgt, eff, rate, fixed = item.split(",")
            amendments.append(FeeAmendment(
                src, tgt, eff,
                None if rate == "" else Decimal(rate),
                None if fixed == "" else Decimal(fixed)))
        inflow_updates = {}
        for item in args.inflow:
            fid, day, amount = item.split(",")
            inflow_updates[fid] = (day, Decimal(amount))
        for item in args.refund:
            parts = item.split(",")
            rid, fid, ret, expected, actual, amount = parts[:6]
            fee = parts[6] if len(parts) > 6 else "0"
            payer = next((f.account_id for f in scenario.flows
                          if f.flow_id == fid), ret)
            execution.record_refund(RefundRecord(
                rid, fid, payer, ret, expected, Decimal(amount),
                actual or None, Decimal(fee)))
        rr = replan(scenario, original, execution, args.cutoff,
                    amendments=amendments, inflow_updates=inflow_updates,
                    allow_defer_p3=not args.no_defer_p3)
        repo = Repository(args.data_dir)
        path = repo.save_plan(rr.plan)
        repo.save_execution(execution)
        print(revision_summary(rr.plan))
        print()
        print(refund_report(rr.plan))
        if args.compare_refund:
            print()
            print(refund_strategy_report(compare_refund_strategies(
                scenario, original, execution, args.cutoff,
                amendments=amendments, inflow_updates=inflow_updates)))
        print()
        print(payment_strategy_report(rr.plan))
        print()
        print(daily_ledger(rr.residual_replay))
        full = combined_replay(scenario, execution, rr.plan, inflow_updates)
        print()
        print(reconciliation(full, scenario))
        print(f"\n修订计划与执行记录分开保存：{path}")


    elif args.cmd == "reject":
        scenario = _load_scenario(args.scenario)
        epath = Path(args.execution)
        if epath.exists():
            execution = Execution.load(epath)
        else:
            from datetime import datetime as _dt
            execution = Execution(scenario.scenario_id, "",
                                  _dt.now().isoformat(timespec="seconds"))
        for item in args.transfer:
            oid, src, tgt, init, arr, principal, fee = item.split(",")
            execution.actual_transfers.append(ActualTransfer(
                oid, src, tgt, init, arr, Decimal(principal), Decimal(fee)))
            if oid not in execution.initiated_order_ids:
                execution.initiated_order_ids.append(oid)
        for item in args.reject:
            parts = item.split(",")
            rid, oid, notice, amount, ret, expected = parts[:6]
            actual = parts[6] if len(parts) > 6 and parts[6] else None
            fee = parts[7] if len(parts) > 7 else "0"
            note = execution.record_rejection(RejectionEvent(
                rid, oid, notice, Decimal(amount), ret, expected,
                actual, Decimal(fee)))
            print(f"  登记 {rid}: {note['handling']}")
        for item in args.revise:
            parts = item.split(",")
            rid, expected = parts[0], parts[1]
            actual = parts[2] if len(parts) > 2 and parts[2] else None
            note = revise_rejection(execution, rid, expected, actual)
            print(f"  修改 {rid}: {note['handling']}")
        for rid in args.cancel:
            note = execution.cancel_rejection(rid)
            print(f"  撤销 {rid}: {note['handling']}")
        execution.save(epath)
        result = replay_with_rejections(scenario, execution)
        print()
        print(rejection_report(rejection_disposition(scenario, execution)))
        print()
        print(event_log(result))
        print()
        print(daily_ledger(result))
        print()
        print(reconciliation(result, scenario))
        print(f"\n执行记录已保存：{epath}（重开后继续追加事件仍可对账）")

if __name__ == "__main__":
    main()
