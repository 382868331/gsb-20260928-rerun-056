from __future__ import annotations

import copy
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .engine import (
    Flow,
    SimulationState,
    TransferRule,
    initial_state,
    initiate_transfer,
    settle_arrivals,
    settle_flow,
    transfer_fee,
)
from .serialization import money


TWO_PLACES = Decimal("0.01")


@dataclass
class PlanResult:
    state: SimulationState
    gaps: list[dict[str, Any]]
    score: tuple[Any, ...]
    plans_considered: int


def _ordered_expenses(flows: list[Flow]) -> list[Flow]:
    expenses = [flow for flow in flows if flow.direction == "expense"]
    return sorted(expenses, key=lambda item: (item.priority if item.priority is not None else 999, item.time, item.order, item.id))


def _settle_pre_expenses(state: SimulationState, flows: list[Flow], time: str) -> None:
    settle_arrivals(state, time)
    period_flows = [flow for flow in flows if flow.time == time]
    for flow in sorted(period_flows, key=lambda item: (0 if item.direction == "income" else 1, item.priority or 999, item.order, item.id)):
        if flow.direction == "income":
            settle_flow(state, flow)


def _settle_expenses(state: SimulationState, flows: list[Flow], time: str) -> list[dict[str, Any]]:
    expenses = sorted(
        [flow for flow in flows if flow.time == time and flow.direction == "expense"],
        key=lambda item: (item.priority if item.priority is not None else 999, item.order, item.id),
    )
    return [settle_flow(state, flow) for flow in expenses]


def _score(flows: list[Flow], state: SimulationState, fees_penalty: bool) -> tuple[Any, ...]:
    paid_vector = tuple(state.paid.get(flow.id, Decimal("0.00")) for flow in _ordered_expenses(flows))
    total_shortfall = sum(
        (flow.amount - state.paid.get(flow.id, Decimal("0.00")) for flow in flows if flow.direction == "expense"),
        Decimal("0.00"),
    )
    fee_component = -state.fees if fees_penalty else state.fees
    return (*paid_vector, -total_shortfall, fee_component)


def _max_principal(available: Decimal, rule: TransferRule, cap: Decimal, step: Decimal) -> Decimal:
    if rule.fee_rate >= 1:
        return Decimal("0.00")
    cash_cap = (available - rule.fixed_fee) / (1 + rule.fee_rate)
    maximum = min(cash_cap, cap)
    units = (maximum / step).to_integral_value(rounding="ROUND_FLOOR")
    return (units * step).quantize(TWO_PLACES)


def _candidate_transfers(
    state: SimulationState,
    rules: list[TransferRule],
    flows: list[Flow],
    time_index: int,
    times: list[str],
    step: Decimal,
    include_current: bool,
) -> list[tuple[TransferRule, Decimal]]:
    candidates: list[tuple[TransferRule, Decimal]] = []
    for rule in rules:
        arrival_index = time_index + rule.delay
        if arrival_index >= len(times):
            continue
        destination_need = sum(
            (
                flow.amount
                for flow in flows
                if flow.direction == "expense"
                and flow.account == rule.destination
                and times.index(flow.time) >= arrival_index
            ),
            Decimal("0.00"),
        )
        if destination_need <= 0:
            continue
        maximum = _max_principal(state.balances[rule.source], rule, destination_need, step)
        principal = step
        while principal <= maximum:
            total = principal + transfer_fee(rule, principal)
            if total <= state.balances[rule.source]:
                candidates.append((rule, principal))
            principal += step
        candidates.sort(key=lambda item: (item[0].delay, transfer_fee(item[0], item[1]), item[0].id, item[1]))
    return candidates


def enumerate_plans(
    accounts: list[Any],
    flows: list[Flow],
    rules: list[TransferRule],
    times: list[str],
    planning: dict[str, Any] | None = None,
) -> PlanResult:
    planning = planning or {}
    step = money(planning.get("transfer_step", "50"))
    max_actions = int(planning.get("max_transfers_per_period", max(1, len(rules))))
    max_nodes = int(planning.get("max_search_nodes", 200000))

    root = initial_state(accounts)
    best_state: SimulationState | None = None
    best_gaps: list[dict[str, Any]] = []
    best_score: tuple[Any, ...] | None = None
    considered = 0
    seen: set[tuple[Any, ...]] = set()

    def finish(state: SimulationState, gaps: list[dict[str, Any]]) -> None:
        nonlocal best_state, best_gaps, best_score, considered
        considered += 1
        if considered > max_nodes:
            raise RuntimeError(f"search node limit exceeded: {max_nodes}")
        score = _score(flows, state, fees_penalty=True)
        if best_score is None or score > best_score:
            best_score = score
            best_state = copy.deepcopy(state)
            best_gaps = copy.deepcopy(gaps)

    def search(
        state: SimulationState,
        time_index: int,
        gaps: list[dict[str, Any]],
        actions_this_period: int,
        phase: str,
        rule_usage: dict[str, int] | None = None,
    ) -> None:
        rule_usage = {} if rule_usage is None else rule_usage
        if time_index >= len(times):
            finish(state, gaps)
            return

        time = times[time_index]
        if phase == "pre":
            current = copy.deepcopy(state)
            _settle_pre_expenses(current, flows, time)
        else:
            current = state

        key = (time_index, phase, actions_this_period, current.snapshot_key())
        if key in seen:
            return
        seen.add(key)

        if phase == "pre":
            search(copy.deepcopy(current), time_index, gaps, actions_this_period, "expense")
            if actions_this_period < max_actions:
                candidates = _candidate_transfers(current, rules, flows, time_index, times, step, True)
                for rule, principal in candidates:
                    if rule_usage.get(rule.id, 0) >= 1:
                        continue
                    action_state = copy.deepcopy(current)
                    initiate_transfer(action_state, rule, principal, times)
                    next_usage = dict(rule_usage)
                    next_usage[rule.id] = next_usage.get(rule.id, 0) + 1
                    search(action_state, time_index, gaps, actions_this_period + 1, "pre", next_usage)
            return

        settled_state = copy.deepcopy(current)
        period_gaps = _settle_expenses(settled_state, flows, time)
        search(settled_state, time_index + 1, gaps + period_gaps, 0, "pre")

    search(root, 0, [], 0, "pre")
    assert best_state is not None and best_score is not None
    return PlanResult(best_state, best_gaps, best_score, considered)
