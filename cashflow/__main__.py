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
from .plan import Execution, Plan, RevisedPlan
from .report import (daily_ledger, event_log, gap_report, plan_summary,
                     reconciliation, revision_summary)
from .replan import replay_full, revise_plan
from .repo import Repository
from .scenario import Scenario


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

    p_rev = sub.add_parser("revise", help="基于实际执行记录重规划剩余计划")
    p_rev.add_argument("scenario")
    p_rev.add_argument("execution")
    p_rev.add_argument("--cutoff", required=True, help="重开日（截断日）YYYY-MM-DD")
    p_rev.add_argument("--revision", type=int, default=1)
    p_rev.add_argument("--plan", default=None, help="原始计划 json，缺省取仓储中最新")
    p_rev.add_argument("--data-dir", default="data")
    p_rev.add_argument("--no-defer", action="store_true",
                       help="不允许推迟可选(P3)支付")

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

    elif args.cmd == "revise":
        scenario = _load_scenario(args.scenario)
        execution = Execution.load(args.execution)
        if args.plan:
            base_plan = Plan.load(args.plan)
        else:
            repo = Repository(args.data_dir)
            base_plan = repo.latest_plan(scenario.scenario_id)
            if base_plan is None:
                raise SystemExit("找不到原始计划，请用 --plan 指定")
        rr = revise_plan(scenario, base_plan, execution, args.cutoff,
                         revision_no=args.revision, allow_defer=not args.no_defer)
        print(revision_summary(rr.revision))
        print()
        full = replay_full(scenario, execution, rr.revision)
        print(daily_ledger(full))
        print()
        print(reconciliation(full, scenario))
        repo = Repository(args.data_dir)
        path = repo.save_revision(rr.revision)
        print(f"\n修订计划已与执行记录分开保存：{path}")


if __name__ == "__main__":
    main()
