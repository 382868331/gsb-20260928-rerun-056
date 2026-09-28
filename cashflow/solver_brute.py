"""小规模枚举求解器：在小例子上穷举调拨组合，与贪心结果互验。

枚举空间：对每笔支出独立选择「不调拨」或「一条覆盖其全额的调拨链」。
只适用于账户/收支数量都很小的场景，链数受 max_chains 限制以防爆炸。
"""

from __future__ import annotations

from decimal import Decimal
from itertools import count

from .engine import Chain, _enumerate_chains, _objective, replay
from .models import FlowKind
from .plan import Plan, TransferOrder
from .scenario import Scenario


def brute_force_solve(scenario: Scenario, max_chains: int = 6,
                      max_combos: int = 50000) -> Plan:
    scenario.validate()
    best = {"orders": [], "score": None, "nodes": 0}
    global_ids = (f"B{n:03d}" for n in count(1))

    def unique_chains(need_gap):
        gap = need_gap
        id_gen = global_ids
        chains = _enumerate_chains(scenario, gap.account_id, gap.shortfall,
                                   gap.date, id_gen)
        out, seen = [], set()
        for c in sorted(chains, key=lambda c: (c.fee, len(c.orders))):
            sig = tuple((o.source, o.target, o.initiate_date,
                         o.arrival_date, str(o.principal)) for o in c.orders)
            if sig in seen:
                continue
            seen.add(sig)
            out.append(c.orders)
            if len(out) >= max_chains:
                break
        return out

    def dfs(orders: list[TransferOrder], abandoned: frozenset) -> None:
        best["nodes"] += 1
        if best["nodes"] > max_combos:
            raise ValueError(f"枚举节点超过上限 {max_combos}，请换用贪心求解")
        scored = _objective(scenario, orders)
        if scored is None:
            return
        score, result = scored
        if best["score"] is None or score < best["score"]:
            best["score"] = score
            best["orders"] = list(orders)
        pending = [g for g in result.gaps if g.flow_id not in abandoned]
        if not pending:
            return
        gap = sorted(pending, key=lambda g: (g.date, g.priority, g.flow_id))[0]
        # 分支一：放弃该支出，后续不再为它筹资
        dfs(orders, abandoned | {gap.flow_id})
        # 分支二：为残差缺口加入每条候选调拨链
        for chain_orders in unique_chains(gap):
            dfs(orders + chain_orders, abandoned)

    dfs([], frozenset())

    best_orders = best["orders"]
    total = best["nodes"]

    final = replay(scenario, best_orders, snapshot_daily=True)
    by_pri = final.gap_total_by_priority()
    total_fee = sum((o.fee for o in best_orders), Decimal("0"))
    from datetime import datetime
    return Plan(
        scenario_id=scenario.scenario_id,
        created_at=datetime.now().isoformat(timespec="seconds"),
        orders=sorted(best_orders, key=lambda o: (o.initiate_date, o.order_id)),
        remaining_gaps=[
            {"flow_id": g.flow_id, "account_id": g.account_id, "date": g.date,
             "priority": g.priority, "due": str(g.due), "paid": str(g.paid),
             "shortfall": str(g.shortfall)}
            for g in sorted(final.gaps, key=lambda g: (g.date, g.priority))
        ],
        total_fee=total_fee,
        objective=(int(by_pri[1] * 100), int(by_pri[2] * 100),
                   int(by_pri[3] * 100), int(total_fee * 100)),
        solver="brute-force",
        notes=f"残差 DFS 穷举 {total} 个节点后的字典序最优",
    )
"""小规模枚举求解器：在小例子上穷举调拨组合，与贪心结果互验。

枚举方式（残差 DFS）：重放后取最早、最高优先的缺口，分支为
「放弃该支出」或「加入一条恰好覆盖残差的调拨链」，递归穷举。
候选链会自然利用到账收入抵扣，只对真正的缺口筹资。
仅适用于账户/收支数量都很小的场景。
"""
