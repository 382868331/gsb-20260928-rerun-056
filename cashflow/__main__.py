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
from .repo import Repository
from .scenario import Scenario
from .replan import FeeAmendment, replan, combined_replay
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


if __name__ == "__main__":
    main()
