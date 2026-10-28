# 架构与设计决策

## 1. 分层

```
CLI (qp.cli)
   ↓
Pipeline (pipeline.py)  ─── 编排 / 事务边界 / 运行清单
   ↓
 ┌──────────────┬────────────────┬───────────────┬────────────────┐
 │ SourceHub    │ Normalizer     │ NonRecurring  │ FactorEngine   │
 │ (ingest/)    │ + Aligner      │ Engine        │ (PIT FX/市值)  │
 │              │ + FCFEngine    │ (rules+LLM)   │                │
 └──────────────┴────────────────┴───────────────┴────────────────┘
   ↓                        ↓                         ↓
 Repository (SQLite / PostgreSQL)  ←→  ClickHouse mirror
```

每一层只通过 `qp.domain` 的数据类或固定列名的 DataFrame 通信。任何一层都不允许把 vendor 原始 DataFrame 直接传给下游。

## 2. 关键设计决策与取舍

| 决策 | 原因 | 代价 |
|---|---|---|
| 事实表存「每个字段 × 每个版本」一行 | 重述必须可回放；UPDATE 会毁掉历史 | 行数 ×版本数；需要窗口去重 |
| `published_at` 与 `available_at` 分离 | 真实披露时间 vs 我们看到这个版本的时间 | 需要维护快照哈希 |
| 归一化用报告期末 FX，估值用 as_of FX | 跨源可比 + 无未来汇率，同时估值口径正确 | 同一个 FCF 有两个币种口径字段 |
| 保留 `value_native` | 允许未来切换目标币种重算，不必重新拉取 | 多一列 |
| 规则层为基线，LLM 只做增量 | 模型不可用时因子仍可算；规则项可审计 | LLM 发现的同类项需要去重 |
| LLM 金额必须能在原文定位 | 港股附注数字密集，幻觉代价极高 | 会丢弃合理但未逐字引用的项 |
| `formula_version` 进主键 | 改公式不覆盖旧序列，回测可复现 | 需要显式清理旧版本 |
| SQLite 为默认库 | 零依赖跑通 demo/CI | 生产需切 PG（`QP_DATABASE_URL`） |
| ClickHouse 只做镜像 | 因子宽表查询快，但 PIT 真源必须单一 | 镜像失败不影响主流程 |

## 3. 扩展点

- **新增数据源**：实现 `SourceAdapter.fetch(symbol, statements) -> list[RawReport]`，在 `config.sources.priority` 注册，在 `SourceHub.adapter()` 里构造。字段映射放在 `clean/field_map.py`（或以 `extra_aliases` 注入）。
- **新增因子**：在 `FactorEngine.compute()` 里 `add(name, value, unit, **inputs)`；改 `config.fcf.formula_version` 让新序列与旧序列共存。
- **新增质量规则**：在 `QualityChecker` 增加一个返回 `QualityEvent` 的方法，在 `Pipeline.compute_snapshot()` 中挂接。
- **替换 LLM**：实现 `.parse(chunks) -> ParsedNotes` 的对象，传给 `Pipeline(config, note_parser=...)`；内置 `LlmWithFallbackParser` 可直接包一层。
- **替换 PDF 后端**：`page_texts()` 已按 PyMuPDF → pdfplumber → 纯文本降级，扩展只需在此函数注册。

## 4. 失败隔离

- 单个源失败：`SourceHub` 捕获异常、重试、记录 `FetchEvent`，继续下一个源；全部失败才报错。
- 单个 symbol 失败：`compute_snapshot` 返回 `QUARANTINED`，不写因子，不影响其它 symbol。
- LLM 失败：回退正则解析，记录 `llm_fallback` WARN。
- ClickHouse 失败：记录 WARN，主库写入照常完成。
- 缺汇率：事实保留但打 `fx_missing` ERROR；严格模式下该 symbol 不产因子。

## 5. 幂等性

| 操作 | 保证 |
|---|---|
| `raw_reports` / `normalized_facts` upsert | 复合主键 + `ON CONFLICT DO UPDATE`；相同 `content_hash` 复用历史 `available_at` |
| `nonrecurring_items` upsert | 同主键覆盖（证据/置信度以最新一次解析为准） |
| `derived_features` upsert | `(symbol, as_of, feature, formula_version)` 覆盖 |
| `quality_events` | 按 `run_id` 先删后插，重跑不重复 |
| LLM 缓存 | 文档文本 + model + prompt_version 哈希 |

`tests/test_pipeline.py::test_rerun_is_idempotent` 验证重复运行不增加事实行数。
