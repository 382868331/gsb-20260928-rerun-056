"""资金流动推演系统（首轮：账户、收支、调拨限制与资金缺口）。"""

from .models import Account, CashFlow, FlowKind, Priority, TransferRule, money
from .scenario import Scenario
from .plan import Plan, Execution, TransferOrder
from .engine import simulate_baseline, solve, SolveResult
from .solver_brute import brute_force_solve
from .ledger import Ledger, LedgerError
from .replan import FeeAmendment, replan, combined_replay, build_prefix

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
]
from .adopt import (
    Adoption, AdoptionResult, WithdrawalResult,
    confirm_partial, compare_partials, withdraw_adoption,
    adoption_combined_replay, confirmed_orders,
)

__all__ += [
    "Adoption", "AdoptionResult", "WithdrawalResult",
    "confirm_partial", "compare_partials", "withdraw_adoption",
    "adoption_combined_replay", "confirmed_orders",
]
