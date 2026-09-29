from __future__ import annotations

from decimal import Decimal
from typing import Any

from .engine import (
    Flow,
    TransferRule,
    expense_gaps,
    initial_state,
    initiate_transfer,
    ledger_to_dict,
    post_credit,
    post_debit,
    settle_arrivals,
    trial_balance,
)
from .serialization import money


def _index(items: list[Any], attr: str) -> dict[str, Any]:
    return {getattr(item, attr): item for item in items}


def replay_actual(accounts: list[Any], flows: list[Flow], rules: list[TransferRule], actual: dict[str, Any]) -> dict[str, Any]:
    state = initial_state(accounts)
    rule_by_id = _index(rules, "id")
    times = sorted({flow.time for flow in flows} | {item["initiate_time"] for item in actual.get("actual_transfers", [])} | {item.get("arrival_time") for item in actual.get("actual_transfers", []) if item.get("arrival_time")})

    transfers_by_start: dict[str, list[dict[str, Any]]] = {}
    income_records = {item["flow_id"]: item for item in actual.get("executed_income", [])}
    expense_records = {item["flow_id"]: item for item in actual.get("executed_expenses", [])}
    explicit_expense_ids = set(expense_records)

    for item in actual.get("actual_transfers", []):
        transfers_by_start.setdefault(item["initiate_time"], []).append(item)

    period_results: list[dict[str, Any]] = []
    for time in times:
        settle_arrivals(state, time)

        for transfer in transfers_by_start.get(time, []):
            rule = rule_by_id[transfer["rule_id"]]
            principal = money(transfer["principal"])
            initiate_transfer(state, rule, principal, times, planned_id=transfer.get("transfer_id"))

        gaps_this_period = []
        for flow in flows:
            if flow.time != time or flow.direction != "income":
                continue
            record = income_records.get(flow.id)
            amount = flow.amount if record is None else money(record.get("actual_amount", flow.amount))
            post_credit(state, time, flow.id, flow.account, amount, f"actual income: {flow.description}")

        for flow in sorted(
            [flow for flow in flows if flow.time == time and flow.direction == "expense"],
            key=lambda item: (item.priority if item.priority is not None else 999, item.order, item.id),
        ):
            record = expense_records.get(flow.id, {})
            default_amount = "0" if flow.id in explicit_expense_ids else flow.amount
            requested = money(record.get("actual_amount", default_amount))
            due = min(flow.amount, requested)
            if due <= 0:
                state.paid[flow.id] = Decimal("0.00")
                gaps_this_period.append({"flow_id": flow.id, "due": flow.amount, "paid": Decimal("0.00"), "shortfall": flow.amount})
                continue
            paid = min(due, state.balances[flow.account]).quantize(Decimal("0.01"))
            if paid > 0:
                post_debit(state, time, flow.id, flow.account, paid, f"actual expense: {flow.description}")
            state.paid[flow.id] = paid
            gaps_this_period.append({"flow_id": flow.id, "due": flow.amount, "paid": paid, "shortfall": flow.amount - paid})

        period_results.append({"time": time, "expenses": gaps_this_period, "balances": dict(state.balances)})

    return {
        "periods": period_results,
        "transfers": state.transfers,
        "ledger": ledger_to_dict(state.ledger),
        "trial_balance": trial_balance(accounts, flows, state),
    }
