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

## 第四轮：退回资金与剩余支付重规划

已发出的支付安排可能只退回一部分资金，原手续费也未必退回。系统把退回/拒付
作为独立实际记录建模，与剩余支付重规划打通：

- **退回记录按 `refund_id` 去重**：`RefundRecord` 记在执行记录
  `Execution.refunds` 中；重复通知被忽略，同一笔款的更正覆盖原记录，
  重放时绝不会增加第二份余额。退回可分多次到达（不同 `refund_id`）。
- **四种资金状态逐笔区分**（`refund_disposition()`）：原付款额、
  在途金额（任何一方不可用）、收款方已经收到的净留存、已退回且可再次
  使用的余额；`actual_date` 为空的**预计退回不作为可用资金**，
  只登记在 `Plan.expected_refunds`。
- **退回是新的入账事件，不是回滚**：原安排已经发生的调拨保持不动，
  已扣手续费照常保留（`fee_refunded` 通常为 0）；退回可入**不同账户**
  （跨账户返回 `return_account`），可迟到（`actual_date` 晚于预计日）。
- **退回到达后的剩余支付重规划**：`compare_refund_strategies()` 对照
  「继续等待退回」（预计退回按预计日显式假设入账）与「替代账户安排」
  （不等退回，立即用其他账户调拨）两种选择的剩余缺口与费用；
  重规划可用退回的一部分覆盖尚未确认的付款，撤回候选仍只释放未执行的占用。
- **重开继续对账**：每次重规划把采用的实际到账依据写入
  `schedule_basis["refund_basis"]`；`combined_replay()` 把退回入账、
  在途、已确认未发起订单与新增补差额订单放入同一本逐日账，
  账户余额、在途金额与原支付的最终去向仍能对上。

新增模块/接口：

- `cashflow/refund.py`：`record_refunds()`、`refund_disposition()`、
  `compare_refund_strategies()`。
- `Plan` 扩展 `refund_records/expected_refunds`；`Execution` 扩展
  `refunds` 与 `record_refund()`；`replan()` 新增
  `refund_assumptions` 参数（仅对照假设时预计退回才入账）。
- `python -m cashflow replan ... --refund RID,FLOW,RET_ACCT,预计,实际,金额[,费]
  [--compare-refund]`
- `examples/run_refund_demo.py`：部分退回 + 退回迟到 + 跨账户返回 +
  手续费不退 + 重复通知/更正的全过程演示；新增测试见
  `tests/test_refund.py`。

## 第五轮：在途划拨的部分拒绝与退回

原划拨尚未到达收款方时，可能一部分先退回付款方（或指定账户），另一部分
仍在途，之后才分别到账或再次退回。系统把三个阶段严格分开建模：

- **拒绝通知不等于可用资金**：`RejectionEvent`（存在 `Execution.rejections`）
  在通知日只拆分在途本金——原划拨在途减少、退回段 `RET-<id>` 进入在途，
  任何一方的可用余额都不变；只有退回段实际到达才入 `return_account`。
- **收款到账只记未退回部分**：原划拨到账时，收款方只收到
  `本金 - 有效拒绝部分`，已退回部分不会被第二次记给收款方。
- **跨账户退回与费用**：退回可入另一账户；原手续费不退，新的退回费用
  `return_fee` 从退回款中扣（计入累计手续费，不退还）。
- **多笔退回逐笔核对**：一个划拨可对应多笔拒绝，按 `rejection_id` 去重，
  重复通知被忽略、更正覆盖原记录，乱序登记结果一致；重放时逐笔核对
  实际来源（必须是在途的实际调拨）且有效拒绝总额不得超过本金上限，
  重复或乱序记录不能制造现金。
- **修改与撤销**：`revise_rejection()` 修改预计到账日/补记实际到账日；
  `cancel_rejection()` 只撤销尚未执行的退回安排（该部分留在原划拨上，
  随原到账日记给收款方），已实际到账的退回保留不变。
- **中途存档重放对账**：执行记录随时落盘，重开后继续追加事件；
  `replay_with_rejections()` 从期初重放全部事件（收入->发起->拒绝拆分
  ->到账->支出），每日验证资金守恒。

新增模块/命令：

- `cashflow/rejection.py`：`record_rejections()`、`revise_rejection()`、
  `replay_with_rejections()`、`rejection_disposition()`；
  `Ledger.reject_in_transit()` 负责在途拆账。
- `python -m cashflow reject <场景> <执行记录> --transfer ...
  --reject RID,ORDER,NOTICE,AMOUNT,RET_ACCT,EXPECTED[,ACTUAL,FEE]
  [--revise RID,EXPECTED[,ACTUAL]] [--cancel RID]`
- `examples/run_rejection_demo.py`：部分拒绝 + 跨账户退回 + 退回费用 +
  改期 + 撤销 + 中途存档重放的全过程；新增测试见 `tests/test_rejection.py`。

## 第五轮修订：预计退回与实际可用资金严格分开

部分拒绝通知附带的预计到账日期只是预测，不是可用资金：

- **预计日期经过不入账**：退回段 `RET-<id>` 在只有预计日期、没有实际
  退回确认时，一直以未确认状态挂在在途（`arrival_date=None`），
  预计日期经过也不会增加任何账户的可用余额；预测在
  `rejection_disposition()` 的 `expected_returns_forecast` 中单独列出，
  并清晰标注「预计退回（预测，未确认，不得当作可用资金）」。
- **确认实际日期后才入账**：`revise_rejection()` 补记
  `actual_return_date` 后，退回段才按实际日期入账；实际日期晚于
  预计日期时按实际日期入账，不得改写成提前可用。
- **覆盖既有全部情形**：跨账户退回、重复通知/更正去重、退回费用
  （不退）、预计日期修改、撤销未执行退回安排、中途保存重放，
  本金与在途资金全程守恒（退回段未确认时在途金额保持不变）。

## 第六轮：依赖退回资金的后续付款方案研究

研究层允许并列保存多组预计退回日期、金额、费用和退回账户假设；预测只在
对应研究方案内作为 `forecast` 依据，不直接进入实际可用余额：

- `research_refund_payment_options()` 比较保守口径（不用预计退回）与多组
  预测，逐笔报告准时可执行、迟到可执行、等待/仍不足和取消状态，以及最早
  可执行日期；也允许调整尚未发出付款的优先级或取消其安排。
- 分批退回进入不同账户时，调度继续遵守实际到账日、调拨时延、当日到账不能
  再发起下一跳、以及不能跨账户透支；两笔付款争用同一笔现金时，高优先级
  先占用，低优先级等待后续资金并给出最早日期。
- `replan_after_refund_actuals()` 只重算受影响的未执行付款；未受影响的
  未发出订单原样锁定，已发起、在途和已到账事实不可撤销。
- 研究方案和实际修订方案独立保存；`refund_revision` 保留原研究、预测
  快照、影响范围与保留订单，保存恢复后仍可完整重放并核对资金守恒。

新增模块：`cashflow/refund_followup.py`；测试：
`tests/test_refund_followup.py`。

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
- `cashflow/refund.py`：退回/拒付记录、退回去向核对、等待 vs 替代账户对照
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
.venv/bin/python -m examples.run_refund_demo         # 第四轮：退回资金与剩余支付重规划
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
