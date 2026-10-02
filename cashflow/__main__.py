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
from .report import adoption_summary, adoption_compare_report, withdrawal_summary
from .repo import Repository
from .scenario import Scenario
from .replan import FeeAmendment, replan, combined_replay
from .adopt import (Adoption, confirm_partial, compare_partials,
                    withdraw_adoption, adoption_combined_replay)
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
    p_rev.add_argument("--data-dir", default="data")


    p_adopt = sub.add_parser("adopt", help="部分采纳候选计划中的调拨安排")
    p_adopt.add_argument("scenario")
    p_adopt.add_argument("plan")
    p_adopt.add_argument("--confirm", action="append", default=[],
                         metavar="ORDER_ID", help="确认的调拨单号，可多次")
    p_adopt.add_argument("--prior", default=None,
                         help="重开的历史采纳记录 JSON（继续采纳）")
    p_adopt.add_argument("--label", default="")
    p_adopt.add_argument("--execution", default=None, help="实际执行记录 JSON")
    p_adopt.add_argument("--data-dir", default="data")

    p_cmp = sub.add_parser("adopt-compare", help="两个部分采纳方案的对照")
    p_cmp.add_argument("scenario")
    p_cmp.add_argument("plan")
    p_cmp.add_argument("--scope", action="append", required=True,
                       metavar="LABEL:ID1,ID2",
                       help="一个部分采纳范围，可多次（至少两个）")

    p_wd = sub.add_parser("withdraw", help="撤回部分采纳（只影响未执行部分）")
    p_wd.add_argument("scenario")
    p_wd.add_argument("adoption")
    p_wd.add_argument("plan")
    p_wd.add_argument("execution")
    p_wd.add_argument("cutoff")
    p_wd.add_argument("--data-dir", default="data")

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
        rr = replan(scenario, original, execution, args.cutoff,
                    amendments=amendments, inflow_updates=inflow_updates,
                    allow_defer_p3=not args.no_defer_p3)
        repo = Repository(args.data_dir)
        path = repo.save_plan(rr.plan)
        print(revision_summary(rr.plan))
        print()
        print(payment_strategy_report(rr.plan))
        print()
        print(daily_ledger(rr.residual_replay))
        full = combined_replay(scenario, execution, rr.plan, inflow_updates)
        print()
        print(reconciliation(full, scenario))
        print(f"\n修订计划与执行记录分开保存：{path}")



    elif args.cmd == "adopt":
        scenario = _load_scenario(args.scenario)
        plan = Plan.load(args.plan)
        prior = Adoption.load(args.prior) if args.prior else None
        result = confirm_partial(scenario, plan, args.confirm, prior=prior,
                                 label=args.label)
        repo = Repository(args.data_dir)
        repo.save_adoption(result.adoption)
        path = repo.save_plan(result.residual_plan)
        print(adoption_summary(result))
        execution = Execution.load(args.execution) if args.execution else None
        full = adoption_combined_replay(scenario, result.adoption,
                                        result.residual_plan, execution)
        print()
        print(daily_ledger(full))
        print()
        print(reconciliation(full, scenario))
        print(f"\n采纳记录与剩余计划已保存：{path}")

    elif args.cmd == "adopt-compare":
        scenario = _load_scenario(args.scenario)
        plan = Plan.load(args.plan)
        scopes = []
        for item in args.scope:
            label, _, ids = item.partition(":")
            scopes.append((label, [x for x in ids.split(",") if x]))
        results = compare_partials(scenario, plan, scopes)
        print(adoption_compare_report(results))
        print()
        for res in results:
            print(adoption_summary(res))
            print()

    elif args.cmd == "withdraw":
        scenario = _load_scenario(args.scenario)
        adoption = Adoption.load(args.adoption)
        plan = Plan.load(args.plan)
        execution = Execution.load(args.execution)
        result = withdraw_adoption(scenario, adoption, plan, execution,
                                   args.cutoff)
        repo = Repository(args.data_dir)
        repo.save_adoption(result.adoption)
        path = repo.save_plan(result.replan_result.plan)
        print(withdrawal_summary(result))
        print()
        print(daily_ledger(result.replan_result.residual_replay))
        print(f"\n撤回后的修订计划已保存：{path}")

if __name__ == "__main__":
    main()
