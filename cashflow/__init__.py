"""资金流动推演系统（首轮：账户、收支、调拨限制与资金缺口）。"""

from .models import Account, CashFlow, FlowKind, Priority, TransferRule, money
from .scenario import Scenario
from .plan import Plan, Execution, TransferOrder
from .engine import simulate_baseline, solve, SolveResult
from .solver_brute import brute_force_solve
from .ledger import Ledger, LedgerError
from .replan import FeeAmendment, replan, combined_replay, build_prefix
from .adoption import (
    adopt_partial_arrangements, compare_partial_adoptions,
    withdraw_arrangements, replay_with_adoptions,
)

__all__ = [
    "Account",
    "CashFlow",
    "FlowKind",
    "Priority",
    "TransferRule",
    "Scenario",
    "Plan",
    "Execution",
    "TransferOrder",
    "simulate_baseline",
    "solve",
    "SolveResult",
    "brute_force_solve",
    "Ledger",
    "LedgerError", "money",
    "FeeAmendment", "replan", "combined_replay", "build_prefix",
    "adopt_partial_arrangements", "compare_partial_adoptions",
    "withdraw_arrangements", "replay_with_adoptions",
]
