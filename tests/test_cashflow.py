from decimal import Decimal

from cashflow.engine import parse_scenario
from cashflow.planner import enumerate_plans
from cashflow.replay import replay_actual
from cashflow.serialization import load_json


SCENARIO_PATH = "data/scenario_example.json"
ACTUAL_PATH = "data/actual_example.json"


def _scenario():
    return parse_scenario(load_json(SCENARIO_PATH))


def test_small_example_enumeration_prioritizes_payment_then_fees():
    accounts, flows, rules, times, planning = _scenario()
    result = enumerate_plans(accounts, flows, rules, times, planning)

    assert all(gap["shortfall"] == Decimal("0.00") for gap in result.gaps)
    assert result.state.fees == Decimal("2.50")
    assert len(result.state.transfers) == 1
    transfer = result.state.transfers[0]
    assert transfer["rule_id"] == "A-TO-B-1"
    assert transfer["initiate_time"] == "T0"
    assert transfer["arrival_time"] == "T1"
    assert transfer["principal"] == Decimal("250.00")
    assert transfer["source_debit"] == Decimal("252.50")
    assert result.plans_considered > 1


def test_planned_money_cannot_be_spent_before_arrival():
    accounts, flows, rules, times, planning = _scenario()
    result = enumerate_plans(accounts, flows, rules, times, planning)

    tax_gap = next(gap for gap in result.gaps if gap["flow_id"] == "EXP-TAX")
    assert tax_gap["paid"] == Decimal("300.00")
    arrival = next(
        entry
        for entry in result.state.ledger
        if entry.event_id == "ARRIVAL" and entry.account == "B"
    )
    assert arrival.time == "T1"
    assert arrival.credit == Decimal("250.00")


def test_planned_ledger_trial_balance_is_zero():
    accounts, flows, rules, times, planning = _scenario()
    result = enumerate_plans(accounts, flows, rules, times, planning)

    from cashflow.engine import trial_balance

    balance = trial_balance(accounts, flows, result.state)
    assert balance["check"] == Decimal("0.00")
    assert result.state.balances == {
        "A": Decimal("97.50"),
        "B": Decimal("250.00"),
    }


def test_actual_replay_reports_gap_when_transfer_is_late():
    accounts, flows, rules, _times, _planning = _scenario()
    actual = load_json(ACTUAL_PATH)
    result = replay_actual(accounts, flows, rules, actual)

    tax_period = next(period for period in result["periods"] if period["time"] == "T1")
    tax = next(item for item in tax_period["expenses"] if item["flow_id"] == "EXP-TAX")
    assert tax["paid"] == Decimal("50.00")
    assert tax["shortfall"] == Decimal("250.00")
    assert result["trial_balance"]["check"] == Decimal("0.00")
