# 资金流动推演系统（首轮）
# 资金流动推演系统

根据虚构账户、已知收支和账户间调拨限制，按日推演未来各时段的资金缺口，
产出可按时间重放的调拨计划；场景、计划、实际执行记录分别持久化。
后续轮次在此基础上加入融资选择。

## 第二轮：调拨延误后的修订重规划

实际执行与计划出现偏差（两跳调拨中途延误、收入部分未到、手续费提高）后，
系统以 cutoff 日终真实状态为起点，给出**可执行的剩余计划**，而不是重跑整段：

- **已发出的资金保持在途**：cutoff 时仍在途的调拨记为 `fixed_transit`，
  残差重放只在到账日补入账，本金/手续费**不会再从原账户扣第二次**；
  未发起的计划调拨自动作废，只对真正缺的钱重新筹资（新单号 `R***`）。
- **期限与费用逐笔重估**：残差场景以真实余额/在途为期初，收入可按实际
  到账日与金额改写（部分到账、延迟、未到）；`FeeAmendment` 让手续费提高
  仅对 cutoff 之后**新发起**的调拨生效，延误单仍按原费用。
- **两类允许的选择，理由逐条记录**（`payment_decisions`）：
  - `deferred`：P3 可选支付推迟到后续资金到账日，并写明缺口与等待的资金；
  - `on_time`：采用更贵但及时的路径时，写明准时路径与最便宜路径的费用差；
  - `late_paid`：延误导致错过原期限后的补付；`unmet`：仍无法覆盖的缺口。
- **执行与修订分开存档、重开可对账**：实际执行 `Execution`（含实际调拨、
  已发起单号、实际收入）与修订计划 `Plan(revision_of=…)` 分别落盘；
  `combined_replay` 从期初一路串起重放，单本账满足资金守恒并逐日可核。

新增模块/命令：

- `cashflow/replan.py`：`replan()`、`build_prefix()`、`combined_replay()`、
  `FeeAmendment`。
- `Plan` 扩展 `revision_of/cutoff_date/fixed_transit/payment_decisions/
  rule_amendments/expected_inflows`；`Execution` 扩展 `initiated_order_ids`
  与 `actual_inflows`（`ActualInflow`）。
- `python -m cashflow replan <场景> <原计划> <执行记录> <cutoff>
  --amend S,T,日期,费率,固定费 --inflow 收入号,日期,金额 [--no-defer-p3]`
- `examples/run_revision_demo.py`：两跳中途延误 + 回款仅到 300 + C→B
  费率升至 2% 的全过程：工资/房租改走贵但及时的直达路径，可选采购
  推迟到尾款日，每日余额与重开后资金守恒全部核对通过。

## 第三轮：候选方案的部分采纳与撤回

候选支付安排不必整包采纳。系统支持只确认其中部分调拨或支付日期，再为尚未确认
的付款重新求解：

- **确认即占用，未发起也不能复用**：部分采纳的调拨保存在
  `Plan.locked_orders`，重放时仍在其发起日扣本金和手续费；其他方案不能重复
  使用同一账户余额或调拨能力。`confirmed_arrangements` 记录确认订单、支付范围、
  金额、费用和可撤回状态。
- **两个部分采纳选择可对照**：`compare_partial_adoptions()` 分别保留原支付窗口、
  调拨/费率约束和固定在途，报告确认范围、剩余补差额订单、剩余缺口、费用增加，
  以及剩余付款是否推迟或失去可行安排。
- **撤回只影响未执行部分**：`withdraw_arrangements()` 仅释放尚未发起订单；已发起、
  在途或已到账订单保留，并在 `withdrawal_notes` 说明在途本金按原到账日入账、
  到账后才可重新使用，已扣手续费不得抹除。
- **重开继续对账**：`replan()` 自动继承 cutoff 后仍未发起的确认订单；cutoff 前
  已发起的订单进入 `fixed_transit`，只补到账不重复扣款。`combined_replay()` 会把
  实际前缀、在途、已确认未发起订单和新增补差额订单放入同一本逐日账。

新增接口位于 `cashflow/adoption.py`：`adopt_partial_arrangements()`、
`compare_partial_adoptions()`、`withdraw_arrangements()`、
`replay_with_adoptions()`；新增测试见 `tests/test_partial_adoption.py`。

## 第四轮：退回与拒付后的剩余支付重规划

已发出的支付安排可能只退回一部分资金，原手续费也未必退回。系统把退回/拒付
作为**实际记录**（`RefundRecord`，挂在 `Execution.refunds`）参与重放与重规划：

- **四类金额分得清**：原付款（已扣本金+手续费）、在途金额（仍在途中、
  双方不可用）、收款方已收到的部分、退回后再次可用的余额，逐日在账簿与
  报表中分开列示；`refund_view()` 按原付款汇总已退/预计/入账账户。
- **预计退回不是可用资金**：cutoff 之后才能到账的退回只列入
  `Plan.expected_refunds`（`usable=False`），不参与残差求解；到账后进入
  `Plan.refund_basis`，成为重规划的实际到账依据。
- **退回到达后可只使用一部分**：`allocate_arrived_refund()` 把已到账退回的
  部分金额分配给尚未确认的付款（`Plan.refund_allocations`），累计分配
  不得超过实际到账；预计退回提前使用直接报错。
  `compare_refund_usage()` 对照「继续等待退回（缺口付款顺延）」与
  「替代账户安排」的剩余缺口与费用。
- **在途拒付只冲回未到达部分**：原安排已发生的调拨不回滚；在途调拨被
  部分拒付时，退回部分从在途冲回（不再发往收款方），剩余本金仍按原到账日
  入账，已扣手续费保留（`fee_refunded` 默认 0）。
- **分次到账、重复通知与更正**：同一 `refund_id` 的重复通知忽略、更正以
  最新记录为准（`normalize_refunds()`），不会形成第二份余额；不同
  `refund_id` 的分次退回各自入账。
- **按日期组合核对**：部分到账后再退回、退回迟到、跨账户返回
  （`credit_account` 可与原付款账户不同）、费用不退等组合逐日重放；
  `combined_replay` 重开后余额、在途与原支付最终去向仍满足
  资金守恒（外部退回计入恒等式右侧，在途冲回与退手续费只是账内搬家）。

新增模块 `cashflow/refunds.py`；`Plan` 扩展 `expected_refunds/refund_basis/
refund_allocations`，`Execution` 扩展 `refunds`；示例见
`examples/run_refund_demo.py`，测试见 `tests/test_refunds.py`。

## 核心约束

- **支付窗口与原始期限分离**：支出可提供 `window_start/window_end`，`date`
  始终保留输入的原始期限；求解器只在允许窗口内枚举改期，不会回写输入。
- **方案状态三分类**：窗口枚举完且无安排为 `true_shortfall`（真正不足）；
  存在可行方案但当前优先级策略不选为 `strategy_not_adopted`；
  枚举受组合上限截断为 `search_limit_exceeded`（尚未找到，不能断言不足）。
- **支付优先级方案对照**：同时输出 `strict_due`、`defer_optional`、
  `window_priority` 的改期日期、手续费、最终缺口；采纳一种修订后，旧计划与
  其依据仍独立保存，修订计划通过 `revision_of/schedule_basis` 复演差异。

- **到账时间参与计算**：调拨有 `lag_days`，发起日付款方立即扣本金+手续费，
  到账日收款方才可用；在途资金任何一方都不能使用。
- **手续费参与计算**：按比例+固定额，发起日实时占用资金；余额不足以覆盖
  “本金+手续费”时调拨不允许发起。
- **不能用没到账的钱支付**：每日过账顺序固定为
  收入到账 → 调拨发起 → 调拨到账 → 支出支付；当日到账资金不能当日再发起
  下一跳（同日多跳链被拒绝）。
- **同一笔钱不安排两次**：过账时账户余额严禁为负，不足即报错；单号唯一。
- **目标排序**：先把 P1→P2→P3 支出缺口降到最小，再最小化总调拨手续费
  （目标元组 `(P1缺口, P2缺口, P3缺口, 总费用)` 字典序）。

## 目录

- `cashflow/models.py`：账户、收支（含优先级）、调拨规则（到账天数/手续费/限额）
- `cashflow/scenario.py`：场景，校验并独立存取 JSON
- `cashflow/ledger.py`：时序账簿与资金守恒核对（重放内核）
- `cashflow/engine.py`：基线推演、按订单重放、贪心求解器（反向 DFS 选最便宜调拨链）
- `cashflow/solver_brute.py`：小规模残差 DFS 枚举求解器，用于小例子对拍
- `cashflow/plan.py`：计划 `Plan` 与实际执行 `Execution`，分文件保存
- `cashflow/repo.py`：`data/scenarios|plans|executions/` 三类数据分目录
- `cashflow/report.py`：事件清单、每日各账户资金账、缺口位置、对账报表
- `examples/run_demo.py`：三账户小例子，枚举与贪心对拍
- `tests/`：11 个单元测试

## 运行

```bash
.venv/bin/python -m examples.run_demo          # 小例子全流程演示
.venv/bin/python -m unittest discover -s tests # 测试
.venv/bin/python -m cashflow baseline data/scenarios/demo01.json
.venv/bin/python -m cashflow solve data/scenarios/demo01.json --brute
.venv/bin/python -m cashflow replay data/scenarios/demo01.json <plan.json>
.venv/bin/python -m examples.run_revision_demo       # 第二轮：延误后修订重规划
.venv/bin/python -m examples.run_refund_demo         # 第四轮：退回与拒付重规划
```

## 小例子结论（2026-10-01 至 10-08）

- B 业务户 10-03 发工资 600（P1），但回款 500 在 10-04 才到账，不能提前用。
- A→B 直达隔日到账但收 2%；A→C 当日免费、C→B 隔日免费。
- 求解器选择免费的 A→C→B 两跳：10-01/10-02 筹资覆盖工资，
  10-03/10-04 再筹 200，与 10-04 回款 500 一起覆盖 10-05 房租 700（P2），
  总手续费 0。
- A 户仅剩 200，10-06 的可选采购 800（P3）无法覆盖，缺口 800 按位置输出。
- 穷举枚举与贪心目标元组一致，对账满足：
  期末余额 + 在途 + 手续费 = 期初 + 收入 - 已付支出。
- 执行记录可与计划不同（延误、部分、取消），重放时会暴露链式资金断裂；
  例如 T003 延误一天，C 户 10-02 无款发起 T004，直接报错。

## 下一轮扩展点

- 融资选择（授信、票据、拆借）建模为带额度、利率、起息日的“外部资金来源”，
  接入同一时序账簿与目标元组。
- 计划 vs 实际差异分析：`Execution` 已独立落盘，可直接按执行腿重放对比。
