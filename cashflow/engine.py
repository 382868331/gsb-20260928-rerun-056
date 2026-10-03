"""推演引擎：无调拨基线、按订单重放、贪心求解调拨计划。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from itertools import count

from .ledger import Ledger, LedgerError, PaymentResult, TransferLeg
from .models import FlowKind, Priority, money
from .plan import Plan, TransferOrder
from .scenario import Scenario


def daterange(start: str, end: str):
    d = date.fromisoformat(start)
    last = date.fromisoformat(end)
    while d <= last:
        yield d.isoformat()
        d += timedelta(days=1)


def add_days(day: str, days: int) -> str:
    return (date.fromisoformat(day) + timedelta(days=days)).isoformat()


@dataclass
class ReplayResult:
    ledger: Ledger
    gaps: list[PaymentResult] = field(default_factory=list)

    def gap_total_by_priority(self) -> dict[int, Decimal]:
        out = {int(Priority.P1_CRITICAL): Decimal("0"),
               int(Priority.P2_NORMAL): Decimal("0"),
               int(Priority.P3_OPTIONAL): Decimal("0")}
        for g in self.gaps:
            out[g.priority] += g.shortfall
        return out


def replay(scenario: Scenario, orders: list[TransferOrder] | None = None,
           legs: list[TransferLeg] | None = None,
           snapshot_daily: bool = False,
            preloaded_ids: set[str] | None = None,
            refunds: list | None = None) -> ReplayResult:
    """按日期重放。orders 为计划时与 legs 二选一；legs 可用于执行记录。"""
    scenario.validate()
    if legs is None:
        legs = Ledger.legs_from_orders(orders or [])
    preloaded_ids = preloaded_ids or set()
    leg_ids = [leg.order_id for leg in legs]
    if len(set(leg_ids)) != len(leg_ids):
        raise LedgerError("调拨单号重复")

    inflows: dict[str, list] = {}
    outflows: dict[str, list] = {}
    for f in scenario.flows:
        (inflows if f.kind == FlowKind.INFLOW else outflows).setdefault(
            f.date, []).append(f)
    refunds_by_day: dict[str, list] = {}
    for r in refunds or []:
        refunds_by_day.setdefault(r.arrival_date, []).append(r)
    initiations: dict[str, list[TransferLeg]] = {}
    arrivals: dict[str, list[TransferLeg]] = {}
    for leg in legs:
        initiations.setdefault(leg.initiate_date, []).append(leg)
        arrivals.setdefault(leg.arrival_date, []).append(leg)

    ledger = Ledger.open({a.account_id: a.opening_balance
                          for a in scenario.accounts})
    # 已在途本金早已从付款方扣付：残差重放只补到账，本金计入期初在途
    gaps: list[PaymentResult] = []
    for day in daterange(scenario.start_date, scenario.end_date):
        for f in sorted(inflows.get(day, []), key=lambda x: x.flow_id):
            ledger.post_inflow(day, f.account_id, f.amount, f.flow_id)
        for r in sorted(refunds_by_day.get(day, []), key=lambda x: x.refund_id):
            ledger.post_refund(day, r)
        for leg in sorted(initiations.get(day, []), key=lambda x: x.order_id):
            ledger.initiate(day, leg)   # 余额不足直接抛错
        for leg in sorted(arrivals.get(day, []), key=lambda x: x.order_id):
            if leg.order_id in preloaded_ids:
                # 重规划时已在途的调拨：本金早在 cutoff 前已扣付，此处只补到账
                ledger.balances[leg.target] = (
                    ledger.balances.get(leg.target, Decimal("0")) + leg.principal)
                # 本金作为「携带在途转入」入账，与残差期初余额互不相干
                ledger.total_inflows += leg.principal
                ledger._record(day, "transfer_arrive", order_id=leg.order_id,
                               target=leg.target, principal=str(leg.principal),
                               fixed_in_transit=True)
            elif leg.order_id not in ledger.in_transit:
                raise LedgerError(
                    f"{day} 调拨 {leg.order_id} 到账失败：该笔调拨此前未能成功发起"
                    f"（资金未在 {leg.initiate_date} 离开 {leg.source}）")
            else:
                ledger.arrive(day, leg.order_id)
        day_outs = sorted(outflows.get(day, []),
                          key=lambda x: (x.priority, x.flow_id))
        for f in day_outs:
            res = ledger.pay_outflow(day, f.flow_id, f.account_id,
                                     f.amount, int(f.priority))
            if res.shortfall > 0:
                gaps.append(res)
        if snapshot_daily:
            ledger.snapshot(day)
        ledger.verify_conservation()
    ledger.verify_conservation()
    return ReplayResult(ledger=ledger, gaps=gaps)


def simulate_baseline(scenario: Scenario) -> ReplayResult:
    """不做任何调拨时的资金缺口基线。"""
    return replay(scenario, orders=[])


# ---------------- 贪心求解器 ----------------

MAX_HOPS = 3


@dataclass
class Chain:
    orders: list[TransferOrder]
    fee: Decimal


def _split_principal(amount: Decimal, rule) -> list[Decimal] | None:
    """按规则单笔上下限把总额拆成若干笔本金。"""
    amount = money(amount)
    if amount <= 0:
        return None
    cap = rule.max_amount
    if cap is None:
        if amount < rule.min_amount:
            return [rule.min_amount]
        return [amount]
    if amount < rule.min_amount:
        amount = rule.min_amount
    n = int((amount + cap - 1) // cap)
    base = money(amount / n)
    pieces = [base] * n
    rest = money(amount - base * n)
    pieces[0] += rest
    if pieces[-1] < rule.min_amount or pieces[0] > cap:
        return None
    return pieces


def _enumerate_chains(scenario: Scenario, target: str, need: Decimal,
                      deadline: str, id_gen) -> list[Chain]:
    """反向 DFS 枚举资金路径，生成调拨链（含手续费逐级回补）。"""
    chains: list[Chain] = []

    # rev: target_account -> [(rule, source_account)]
    rev: dict[str, list] = {}
    for r in scenario.transfer_rules:
        rev.setdefault(r.target, []).append(r)

    def walk(cur_account: str, deliver: Decimal, cur_deadline: str,
             hops: list, visited: frozenset):
        # hops: 反向累积的 (rule, pieces, initiate, arrive)
        for rule in rev.get(cur_account, []):
            src = rule.source
            if src in visited:
                continue
            arrive = cur_deadline                       # 到账不晚于用款日
            initiate = add_days(arrive, -rule.lag_days)
            if initiate < scenario.start_date:
                continue                                # 期初之前无法发起
            pieces = _split_principal(deliver, rule)
            if pieces is None:
                continue
            fee = sum((rule.fee_for(p) for p in pieces), Decimal("0"))
            hop = (rule, pieces, initiate, arrive)
            new_hops = hops + [hop]
            orders = []
            for h_rule, h_pieces, h_init, h_arr in reversed(new_hops):
                for p in h_pieces:
                    orders.append(TransferOrder(
                        order_id=next(id_gen), source=h_rule.source,
                        target=h_rule.target, initiate_date=h_init,
                        arrival_date=h_arr, principal=p,
                        fee=h_rule.fee_for(p), purpose=""))
            chains.append(Chain(orders, fee))
            if len(new_hops) < MAX_HOPS:
                # 上游须在用款方发起日的前一天到账（同日到账不可用于发起）
                upstream_need = money(sum(pieces) + fee)
                walk(src, upstream_need, add_days(initiate, -1),
                     new_hops, visited | {src})

    walk(target, need, deadline, [], frozenset({target}))
    return chains


def _objective(scenario: Scenario, orders: list[TransferOrder],
               preloaded_ids: set[str] | None = None):
    try:
        result = replay(scenario, orders, preloaded_ids=preloaded_ids)
    except LedgerError:
        return None
    by_pri = result.gap_total_by_priority()
    fee = sum((o.fee for o in orders), Decimal("0"))
    score = (by_pri[int(Priority.P1_CRITICAL)],
             by_pri[int(Priority.P2_NORMAL)],
             by_pri[int(Priority.P3_OPTIONAL)],
             fee)
    return score, result


@dataclass
class SolveResult:
    plan: Plan
    replay: ReplayResult


def solve(scenario: Scenario, solver_name: str = "greedy",
          fixed_orders: list[TransferOrder] | None = None,
          locked_orders: list[TransferOrder] | None = None,
          id_prefix: str = "T",
          schedule_windows: bool = True,
          eligible_strategies: list[str] | None = None) -> SolveResult:
    """逐缺口贪心：始终处理最早、最高优先的缺口，选费用最低的可行链。

    fixed_orders 为 cutoff 前已扣款、当前仍在途的资金，只补到账。
    locked_orders 为已确认但尚未发起的调拨，会正常扣款并占用资金。
    """
    scenario.validate()
    if schedule_windows and any(
            f.kind == FlowKind.OUTFLOW and (f.window_start is not None or
                                     f.window_end is not None)
            for f in scenario.flows):
        from .scheduler import solve_with_windows
        return solve_with_windows(scenario, fixed_orders=fixed_orders,
                                  locked_orders=locked_orders,
                                  id_prefix=id_prefix,
                                  eligible_strategies=eligible_strategies)[0]
    fixed_orders = list(fixed_orders or [])
    locked_orders = list(locked_orders or [])
    orders: list[TransferOrder] = fixed_orders + locked_orders
    abandoned: set[str] = set()
    preloaded_ids = {o.order_id for o in fixed_orders}
    locked_ids = {o.order_id for o in locked_orders}
    id_gen = (f"{id_prefix}{i:03d}" for i in count(1))

    for _ in range(200):
        result = replay(scenario, orders, preloaded_ids=preloaded_ids)
        pending = [g for g in result.gaps if g.flow_id not in abandoned]
        if not pending:
            break
        gap = sorted(pending, key=lambda g: (g.date, g.priority, g.flow_id))[0]
        need = money(gap.shortfall)

        chains = _enumerate_chains(scenario, gap.account_id, need,
                                   gap.date, id_gen)
        best = None
        best_score = None
        base_score, _ = _objective(scenario, orders,
                                   preloaded_ids=preloaded_ids)
        for chain in sorted(chains, key=lambda c: (c.fee, len(c.orders))):
            trial = orders + chain.orders
            scored = _objective(scenario, trial,
                                preloaded_ids=preloaded_ids)
            if scored is None:
                continue
            score, _ = scored
            if score[:3] < base_score[:3] and (
                    best_score is None or score < best_score):
                best = chain
                best_score = score
        if best is None:
            abandoned.add(gap.flow_id)
            continue
        orders.extend(best.orders)

    new_orders = [o for o in orders
                  if o.order_id not in preloaded_ids
                  and o.order_id not in locked_ids]
    final = replay(scenario, orders, snapshot_daily=True,
                   preloaded_ids=preloaded_ids)
    by_pri = final.gap_total_by_priority()
    new_fee = money(sum((o.fee for o in new_orders), Decimal("0")))
    remaining = [
        {"flow_id": g.flow_id, "account_id": g.account_id, "date": g.date,
         "priority": g.priority, "due": str(g.due), "paid": str(g.paid),
         "shortfall": str(money(g.shortfall))}
        for g in sorted(final.gaps, key=lambda g: (g.date, g.priority))
    ]
    plan = Plan(
        scenario_id=scenario.scenario_id,
        created_at=datetime.now().isoformat(timespec="seconds"),
        orders=sorted(new_orders, key=lambda o: (o.initiate_date, o.order_id)),
        remaining_gaps=remaining,
        total_fee=new_fee,
        objective=(int(by_pri[int(Priority.P1_CRITICAL)] * 100),
                   int(by_pri[int(Priority.P2_NORMAL)] * 100),
                   int(by_pri[int(Priority.P3_OPTIONAL)] * 100),
                   int(new_fee * 100)),
        solver=solver_name,
        notes="目标序：(P1缺口,P2缺口,P3缺口,总手续费)，字典序最小",
    )
    return SolveResult(plan=plan, replay=final)
