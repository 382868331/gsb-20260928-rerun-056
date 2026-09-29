from __future__ import annotations

import argparse
from decimal import Decimal
from typing import Any

from .engine import balance_timeline, ledger_to_dict, parse_scenario, trial_balance
from .planner import enumerate_plans
from .replay import replay_actual
from .serialization import dump_json, load_json


def _decimalable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {key: _decimalable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decimalable(item) for item in value]
    return value


def plan_command(scenario_path: str, output_path: str) -> None:
    accounts, flows, rules, times, planning = parse_scenario(load_json(scenario_path))
    result = enumerate_plans(accounts, flows, rules, times, planning)
    gaps_by_time = {flow.time: [] for flow in flows}
    for gap in result.gaps:
        flow = next(item for item in flows if item.id == gap["flow_id"])
        gaps_by_time[flow.time].append(
            {
                "flow_id": flow.id,
                "account": flow.account,
                "priority": flow.priority,
                "due": gap["due"],
                "paid": gap["paid"],
                "shortfall": gap["shortfall"],
                "description": flow.description,
            }
        )
    balances_by_time = {item["time"]: item["balances"] for item in balance_timeline(accounts, result.state.ledger, times)}
    transfers_by_start: dict[str, list[dict[str, Any]]] = {}
    for transfer in result.state.transfers:
        transfers_by_start.setdefault(transfer["initiate_time"], []).append(transfer)
    payload = {
        "scenario_path": scenario_path,
        "planning": planning,
        "plans_considered": result.plans_considered,
        "transfers": result.state.transfers,
        "periods": [
            {
                "time": time,
                "gaps": gaps_by_time[time],
                "transfers_initiated": transfers_by_start.get(time, []),
                "ending_balances": balances_by_time[time],
            }
            for time in times
        ],
        "final_balances": result.state.balances,
        "total_fees": result.state.fees,
        "trial_balance": trial_balance(accounts, flows, result.state),
        "ledger": ledger_to_dict(result.state.ledger),
    }
    dump_json(_decimalable(payload), output_path)


def replay_command(scenario_path: str, actual_path: str, output_path: str) -> None:
    accounts, flows, rules, _times, _planning = parse_scenario(load_json(scenario_path))
    payload = replay_actual(accounts, flows, rules, load_json(actual_path))
    dump_json(_decimalable(payload), output_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fund flow projection and replay system")
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan_parser = subparsers.add_parser("plan")
    plan_parser.add_argument("--output", required=True)
    plan_parser.add_argument("scenario")

    replay_parser = subparsers.add_parser("replay")
    replay_parser.add_argument("--output", required=True)
    replay_parser.add_argument("scenario")
    replay_parser.add_argument("actual")

    args = parser.parse_args()
    if args.command == "plan":
        plan_command(args.scenario, args.output)
    else:
        replay_command(args.scenario, args.actual, args.output)


if __name__ == "__main__":
    main()
