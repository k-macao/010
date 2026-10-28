# 数据契约与口径

## 1. 阶段间对象

| 对象 | 出处 | 关键字段 |
|---|---|---|
| `RawReport` | 适配器 | `symbol, source, statement, period_end, period_type, currency, unit_scale, unit_label, published_at, available_at, data, metadata` |
| `NormalizedFact` | Normalizer | `value`(目标币种、期末汇率), `value_native`(原币、元), `report_currency, fx_rate, fx_as_of, sign, quality, content_hash` |
| `AlignedFact` | MultiSourceAligner | `value, value_native, sources, conflict, restated, published_at, available_at, quality` |
| `NonRecurringItem` | 规则/LLM | `category, amount, profit_impact, direction, source_kind, evidence, page, confidence, cash_impact` |
| `FCFResult` | FCFEngine | `ocf, capex_raw, capex, standard_fcf, standard_fcf_native, adjusted_fcf, oneoff_cash_impact, policy, quality` |
| `FactorValue` | FactorEngine | `symbol, as_of, period_end, feature, value, currency, unit, formula_version, inputs` |

`statement ∈ {BS, IS, CF}`；`period_type ∈ {ANNUAL, INTERIM, QUARTERLY}`。所有时间戳为 **naive UTC**（SQLite 无时区类型；PG 用 `TIMESTAMPTZ` 存 UTC）。

## 2. 符号与聚合约定

| `sign` | 含义 | `profit_impact` |
|---|---|---|
| `flow` | 现金流/收入/存量 | 不参与非经常性调整 |
| `as_gain` | 报告值正=收益、负=损失 | `+value` |
| `as_loss` | 损失金额（统一存正数幅度） | `−value` |
| `stock` | 资产负债表存量 | 不参与 |

`FieldSpec.aggregation = sum` 的字段（公允价值变动、减值、补助、汇兑、处置、诉讼、其他收益/亏损）允许多个原始行合并；其余字段同标签多行时取第一行并打 `duplicate_label` WARN。

**CapEx**：`raw_value` 保留源侧符号（XBRL/yfinance 常为负），跨源比对用绝对值，`FCFEngine` 的 `auto` 策略取 `abs()`。`signed` 策略用 `OCF + capex_raw`。

## 3. 汇率口径

```
rate(base=B, quote=Q, as_of=T) = 1 单位 Q 可换多少单位 B
value_B = value_Q × rate
```

- `fx_rates.csv`：`base, quote, as_of, rate, source`；演示值 `HKD/CNY = 1.08~1.11` 表示 1 CNY = 1.08~1.11 HKD；
- 归一化用 `rate(quote=report_currency, as_of=period_end)`；
- 估值用 `rate(quote=report_currency, as_of=评估日)` 和 `rate(quote=market_currency, as_of=评估日)`；
- 只取 `as_of <= T` 的最新一条；`fallback_rates` 兜底会标记 `is_fallback=True` 并触发 WARN；
- 中行牌价 `100 HKD = N CNY` → `1 CNY = 100/N HKD`（`FxRateProvider.from_boc_sina`）。

## 4. 关系表

| 表 | 主键 | 用途 |
|---|---|---|
| `raw_reports` | `(symbol, source, statement, period_end, published_at, available_at)` | 原始快照 + `content_hash` |
| `normalized_facts` | 上述 + `field` | PIT 事实库 |
| `nonrecurring_items` | `(symbol, period_end, published_at, available_at, category, label, source_kind)` | 明细与证据 |
| `fx_rates` | `(base, quote, as_of, source)` | 汇率 |
| `market_caps` | `(symbol, as_of, source)` | 市值 |
| `derived_features` | `(symbol, as_of, feature, formula_version)` | 因子 |
| `quality_events` | 自增 id | 监控事件 |
| `pipeline_runs` | `run_id` | 运行清单 + summary JSON |

PostgreSQL `pit_facts` 视图：

```sql
SELECT DISTINCT ON (symbol, source, statement, period_end, field) ...
FROM normalized_facts
ORDER BY ..., available_at DESC, published_at DESC;
```

> 注意：视图展示的是「最新可见版本」，**不带 as_of 过滤**。回测必须先在 SQL 里按 `available_at <= :as_of` 过滤，或用 `Repository.get_facts(symbol, as_of)`。

## 5. 派生因子清单（`fcf-v1`）

| 因子 | 定义 | 单位 |
|---|---|---|
| `standard_fcf` | OCF − \|CapEx\|，期末汇率 | 目标币种 |
| `fcf_as_of` | `standard_fcf_native × FX(as_of)` | 目标币种 |
| `fcf_adjusted` | `standard_fcf − Σ 一次性现金损益` | 目标币种 |
| `fcf_yield` | `fcf_as_of / (market_cap × FX(as_of))` | ratio |
| `fcf_yield_adjusted` | 同上但用 adjusted FCF 缩放 | ratio |
| `fcf_margin` | `standard_fcf / REVENUE` | ratio |
| `fcf_to_net_profit` | `standard_fcf / NET_PROFIT` | ratio |
| `recurring_net_profit` | `NET_PROFIT − Σ profit_impact` | 目标币种 |
| `fcf_to_recurring_earnings` | `standard_fcf / recurring_net_profit` | ratio |
| `nonrecurring_ratio` | `\|Σ profit_impact\| / \|NET_PROFIT\|` | ratio |
| `capex_to_ocf` | `\|CapEx\| / \|OCF\|` | ratio |
| `net_debt_to_fcf` | `(TOTAL_DEBT − CASH) / standard_fcf` | ratio |

所有分母为 0 时因子不产出或写 NaN，并在 `inputs` 里保留原始输入以便复算。
