from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .serialization import money


TWO_PLACES = Decimal("0.01")


@dataclass(frozen=True)
class Account:
    id: str
    name: str
    opening_balance: Decimal


@dataclass(frozen=True)
class Flow:
    id: str
    account: str
    time: str
    direction: str
    amount: Decimal
    priority: int | None = None
    order: int = 0
    description: str = ""


@dataclass(frozen=True)
class TransferRule:
    id: str
    source: str
    destination: str
    delay: int
    fee_rate: Decimal = Decimal("0")
    fixed_fee: Decimal = Decimal("0")


@dataclass
class LedgerEntry:
    time: str
    event_id: str
    account: str
    debit: Decimal
    credit: Decimal
    balance: Decimal
    note: str


@dataclass
class SimulationState:
    time_index: int
    balances: dict[str, Decimal]
    in_transit: dict[str, dict[str, Decimal]] = field(default_factory=dict)
    paid: dict[str, Decimal] = field(default_factory=dict)
    transfers: list[dict[str, Any]] = field(default_factory=list)
    ledger: list[LedgerEntry] = field(default_factory=list)
    fees: Decimal = Decimal("0.00")
    next_transfer_seq: int = 1
    settled_arrivals: set[str] = field(default_factory=set)
    settled_income: set[str] = field(default_factory=set)
    arrived_transfer_ids: set[str] = field(default_factory=set)

    def snapshot_key(self) -> tuple[Any, ...]:
        arrivals = tuple(
            (time, account, amount)
            for time, bucket in sorted(self.in_transit.items())
            for account, amount in sorted(bucket.items())
            if amount > 0
        )
        return (
            self.time_index,
            tuple(sorted(self.balances.items())),
            arrivals,
            tuple(sorted(self.paid.items())),
            tuple(sorted(self.settled_arrivals)),
            tuple(sorted(self.settled_income)),
            tuple(sorted(self.arrived_transfer_ids)),
        )


def parse_scenario(raw: dict[str, Any]) -> tuple[list[Account], list[Flow], list[TransferRule], list[str], dict[str, Any]]:
    accounts = [
        Account(item["id"], item.get("name", item["id"]), money(item["opening_balance"]))
        for item in raw["accounts"]
    ]
    flows = [
        Flow(
            id=item["id"],
            account=item["account"],
            time=item["time"],
            direction=item["direction"],
            amount=money(item["amount"]),
            priority=item.get("priority"),
            order=item.get("order", 0),
            description=item.get("description", ""),
        )
        for item in raw["flows"]
    ]
    rules = [
        TransferRule(
            id=item["id"],
            source=item["source"],
            destination=item["destination"],
            delay=int(item["delay"]),
            fee_rate=Decimal(str(item.get("fee_rate", "0"))),
            fixed_fee=money(item.get("fixed_fee", "0")),
        )
        for item in raw["transfer_rules"]
    ]
    return accounts, flows, rules, list(raw["time_points"]), raw.get("planning", {})


def initial_state(accounts: list[Account]) -> SimulationState:
    balances = {account.id: account.opening_balance for account in accounts}
    return SimulationState(0, balances)


def _post(state: SimulationState, time: str, event_id: str, account: str, amount: Decimal, note: str) -> None:
    state.ledger.append(
        LedgerEntry(
            time=time,
            event_id=event_id,
            account=account,
            debit=amount,
            credit=Decimal("0.00"),
            balance=state.balances[account],
            note=note,
        )
    )


def post_debit(state: SimulationState, time: str, event_id: str, account: str, amount: Decimal, note: str) -> None:
    state.balances[account] -= amount
    _post(state, time, event_id, account, amount, note)


def post_credit(state: SimulationState, time: str, event_id: str, account: str, amount: Decimal, note: str) -> None:
    state.balances[account] += amount
    entry = LedgerEntry(time, event_id, account, Decimal("0.00"), amount, state.balances[account], note)
    state.ledger.append(entry)


def transfer_fee(rule: TransferRule, principal: Decimal) -> Decimal:
    return (principal * rule.fee_rate + rule.fixed_fee).quantize(TWO_PLACES)


def initiate_transfer(
    state: SimulationState,
    rule: TransferRule,
    principal: Decimal,
    times: list[str],
    planned_id: str | None = None,
) -> dict[str, Any]:
    principal = principal.quantize(TWO_PLACES)
    fee = transfer_fee(rule, principal)
    total = principal + fee
    if principal <= 0:
        raise ValueError("transfer principal must be positive")
    if state.balances[rule.source] < total:
        raise ValueError(f"insufficient balance in {rule.source}")

    start_time = times[state.time_index]
    arrival_index = state.time_index + rule.delay
    arrival_time = times[arrival_index]
    transfer_id = planned_id or f"TR-{state.next_transfer_seq:04d}"
    state.next_transfer_seq += 1

    post_debit(state, start_time, transfer_id, rule.source, total, f"transfer out via {rule.id}: principal {principal} + fee {fee}")
    state.fees += fee
    if rule.delay == 0:
        post_credit(state, start_time, transfer_id, rule.destination, principal, f"transfer arrival via {rule.id}")
    else:
        state.in_transit.setdefault(arrival_time, {}).setdefault(rule.destination, Decimal("0.00"))
        state.in_transit[arrival_time][rule.destination] += principal

    record = {
        "transfer_id": transfer_id,
        "rule_id": rule.id,
        "source": rule.source,
        "destination": rule.destination,
        "initiate_time": start_time,
        "arrival_time": arrival_time,
        "principal": principal,
        "fee": fee,
        "source_debit": total,
        "status": "settled" if rule.delay == 0 else "in_transit",
    }
    state.transfers.append(record)
    return record


def settle_arrivals(state: SimulationState, time: str) -> None:
    for account, amount in sorted(state.in_transit.pop(time, {}).items()):
        arrival_key = f"{time}:{account}:{amount}"
        if arrival_key not in state.settled_arrivals:
            post_credit(state, time, "ARRIVAL", account, amount, "scheduled transfer arrival")
            state.settled_arrivals.add(arrival_key)
    for record in state.transfers:
        if record["arrival_time"] == time and record["status"] == "in_transit":
            record["status"] = "settled"
            state.arrived_transfer_ids.add(record["transfer_id"])


def settle_flow(state: SimulationState, flow: Flow) -> dict[str, Any]:
    if flow.direction == "income":
        if flow.id not in state.settled_income:
            post_credit(state, flow.time, flow.id, flow.account, flow.amount, flow.description)
            state.settled_income.add(flow.id)
        return {"flow_id": flow.id, "due": flow.amount, "paid": flow.amount, "shortfall": Decimal("0.00")}

    available = state.balances[flow.account]
    paid = min(flow.amount, max(available, Decimal("0.00"))).quantize(TWO_PLACES)
    if paid > 0:
        post_debit(state, flow.time, flow.id, flow.account, paid, flow.description)
    shortfall = flow.amount - paid
    state.paid[flow.id] = paid
    return {"flow_id": flow.id, "due": flow.amount, "paid": paid, "shortfall": shortfall}


def expense_gaps(state: SimulationState, flows: list[Flow], time: str) -> list[dict[str, Any]]:
    due = [
        flow
        for flow in flows
        if flow.time == time and flow.direction == "expense"
    ]
    due.sort(key=lambda item: (item.priority if item.priority is not None else 999, item.order, item.id))
    return [settle_flow(state, flow) for flow in due]


def income_due(flows: list[Flow], time: str) -> list[Flow]:
    return [flow for flow in flows if flow.time == time and flow.direction == "income"]


def ledger_to_dict(entries: list[LedgerEntry]) -> list[dict[str, Any]]:
    return [
        {
            "time": entry.time,
            "event_id": entry.event_id,
            "account": entry.account,
            "debit": entry.debit,
            "credit": entry.credit,
            "balance": entry.balance,
            "note": entry.note,
        }
        for entry in entries
    ]


def balance_timeline(accounts: list[Account], entries: list[LedgerEntry], times: list[str]) -> list[dict[str, Any]]:
    balances = {account.id: account.opening_balance for account in accounts}
    entries_by_time: dict[str, list[LedgerEntry]] = {time: [] for time in times}
    for entry in entries:
        entries_by_time.setdefault(entry.time, []).append(entry)
    timeline: list[dict[str, Any]] = []
    for time in times:
        for entry in entries_by_time.get(time, []):
            balances[entry.account] = entry.balance
        timeline.append({"time": time, "balances": dict(balances)})
    return timeline


def trial_balance(
    accounts: list[Account],
    flows: list[Flow],
    state: SimulationState,
) -> dict[str, Any]:
    opening = sum((account.opening_balance for account in accounts), Decimal("0.00"))
    paid_expenses = sum(state.paid.values(), Decimal("0.00"))
    settled_income = sum((flow.amount for flow in flows if flow.direction == "income"), Decimal("0.00"))
    in_transit = sum(
        (amount for bucket in state.in_transit.values() for amount in bucket.values()),
        Decimal("0.00"),
    )
    account_total = sum(state.balances.values(), Decimal("0.00"))
    return {
        "opening": opening,
        "settled_income": settled_income,
        "paid_expenses": paid_expenses,
        "fees": state.fees,
        "in_transit": in_transit,
        "account_total": account_total,
        "check": account_total + in_transit + paid_expenses + state.fees - opening - settled_income,
    }
