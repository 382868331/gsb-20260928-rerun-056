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
from .report import revision_summary, strategy_comparison
from .repo import Repository
from .scenario import Scenario
from .replan import FeeAmendment, replan, combined_replay
from .scheduling import compare_strategies
from .adoption import adopt, diff_versions
from .plan import Adoption
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
    p_rev.add_argument("--window-strategy",
                       choices=["strict_due", "fastest_feasible", "lowest_fee"],
                       help="在支出允许支付窗口内搜索排期，并三分类缺口")
    p_rev.add_argument("--data-dir", default="data")

    p_sch = sub.add_parser("schedule", help="支付窗口内多策略排期对照")
    p_sch.add_argument("scenario")
    p_sch.add_argument("--data-dir", default="data")

    p_adopt = sub.add_parser("adopt", help="登记采纳某计划（旧计划文件不变）")
    p_adopt.add_argument("scenario")
    p_adopt.add_argument("adopted_created_at")
    p_adopt.add_argument("--superseded", default="")
    p_adopt.add_argument("--note", default="")
    p_adopt.add_argument("--data-dir", default="data")

    p_diff = sub.add_parser("diff", help="复演并对出两版计划差异")
    p_diff.add_argument("scenario")
    p_diff.add_argument("old_created_at")
    p_diff.add_argument("new_created_at")
    p_diff.add_argument("--execution", default="")
    p_diff.add_argument("--data-dir", default="data")

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
                    allow_defer_p3=not args.no_defer_p3,
                    window_strategy=args.window_strategy)
        repo = Repository(args.data_dir)
        path = repo.save_plan(rr.plan)
        print(revision_summary(rr.plan))
        print()
        print(daily_ledger(rr.residual_replay))
        full = combined_replay(scenario, execution, rr.plan, inflow_updates)
        print()
        print(reconciliation(full, scenario))
        print(f"\n修订计划与执行记录分开保存：{path}")

    elif args.cmd == "schedule":
        repo = Repository(args.data_dir)
        scenario = _load_scenario(args.scenario)
        repo.save_scenario(scenario)
        outcomes = compare_strategies(scenario)
        for i, o in enumerate(outcomes):
            o.plan.created_at = f"{o.plan.created_at}#{i:02d}-{o.strategy}"
            repo.save_plan(o.plan)
        print(strategy_comparison(outcomes))
        for o in outcomes:
            o.replay.ledger.verify_conservation()
        print("\n各策略计划均通过资金守恒核对，并已分别保存。")

    elif args.cmd == "adopt":
        repo = Repository(args.data_dir)
        adopted = repo.load_plan_by_created_at(args.scenario,
                                               args.adopted_created_at)
        superseded = (repo.load_plan_by_created_at(args.scenario, args.superseded)
                      if args.superseded else None)
        record = adopt(adopted, superseded, note=args.note)
        path = repo.save_adoption(record)
        print(f"已登记采纳：{args.adopted_created_at}"
              + (f"，替代 {args.superseded}" if args.superseded else "")
              + f"；旧计划文件保持不变。记录：{path}")

    elif args.cmd == "diff":
        repo = Repository(args.data_dir)
        old = repo.load_plan_by_created_at(args.scenario, args.old_created_at)
        new = repo.load_plan_by_created_at(args.scenario, args.new_created_at)
        execution = Execution.load(args.execution) if args.execution else None
        scenario = (repo.load_scenario(args.scenario)
                    if not Path(args.scenario).exists()
                    else _load_scenario(args.scenario))
        vd = diff_versions(scenario, old, new, execution)
        print(vd.summary())


if __name__ == "__main__":
    main()
