# 资金流动推演系统

系统把三类信息分开保存：

- `data/scenario_example.json`：虚构账户、已知收支、账户间调拨规则和搜索参数。
- 计划输出：由规划器生成，包含逐时段调拨、缺口、余额和完整流水账。
- `data/actual_example.json`：实际执行记录；可与同一场景独立回放。

## 计算规则

- 每个时段先结算已到账调拨和已知收入，再枚举该时段调拨决策，最后按支出优先级扣款。
- 发起调拨时立即从源账户扣除本金和手续费；延迟到账的本金处于在途状态，不能用于任何当期支付。
- 到账时才增加目标账户余额，因此同一笔钱不会被源、目标账户重复使用。
- 规划器枚举小额可行调拨组合，先最大化优先支出满足额，再最小化总缺口，最后最小化手续费。
- 输出完整流水账，并用恒等式核对：期初资金 + 已实现收入 = 账户余额 + 在途资金 + 已付支出 + 手续费。

## 使用

```bash
python -m cashflow.cli plan data/scenario_example.json --output tmp/plan.json
python -m cashflow.cli replay data/scenario_example.json data/actual_example.json --output tmp/actual_replay.json
```

小例子的最优计划在 `T0` 从 A 向 B 调拨 250.00。该调拨 `T1` 到账，费用为 1%，即 2.50，可在不使用未到账资金的情况下覆盖全部优先支出。实际执行记录故意把调拨推迟到 `T1` 发起，因此 `T1` 的 300.00 税款只能用 B 账户已有余额支付 50.00，留下 250.00 缺口。

测试：

```bash
python -m pytest -q
```
