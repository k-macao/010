# 010 · 港股量化财务数据流水线（PIT 安全）

把用户草稿里的「自动化拉取 → 清洗 Pipeline → 规则引擎/LLM 辅助解析 → 因子入库」真正落成一套**可运行、可测试、可灰度上线**的工程闭环。核心对象是港股三大报表、`Standard_FCF`、非经常性损益，以及回测最容易被污染的三个地方：**币种/单位、披露时间、重述**。

> 仓库默认配置即可离线跑通全链路：`python -m qp demo` 使用 `examples/fixtures/` 里的**合成演示数据**（不是真实财报），无需任何 API Key、无需外网、无需数据库安装。

---

## 微信早午晚推送

支持 PushPlus 推送流水线摘要到微信。可以使用 [GitHub Actions](docs/GITHUB_PUSHPLUS.md) 或 [服务器 cron](docs/PUSHPLUS.md) 在北京时间 08:00、12:00、18:00 推送。GitHub 工作流尝试用 AkShare/yfinance 采集免费行情；日期级快照保守地从次日开始可用，不会回填历史。首次运行和缺历史汇率时仍可能失败；行情可能延迟或错误，公告日历也尚未接入，**核验真实数据前不能用于投资**。

## 1. 五分钟跑通

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'          # 核心只需 pandas + SQLAlchemy
cp .env.example .env             # 可选
python -m qp demo --as-of 2026-10-27
```

`demo` 会依次：初始化 SQLite → 拉取 3 个源的快照 → 字段/币种/单位归一化 → 解析附注 → PIT 对齐 → 复算 FCF → 计算 12 个因子 → 落库 → 生成 `reports/*.json` 质量报告 → 打印 PIT 回放（检验未来函数）。

输出（节选）：

```
run_id   : run-20261027T...  status: OK   as_of=2026-10-27
counts   : {"raw_reports": 17, "normalized_facts": 71, "note_items": 6, "features": 12, ...}
quality  : 0 ERROR / 1 WARN / 27 INFO
  - [WARN] cross_source_conflict: field=OCF: yfinance=2.529e+11 vs hkexnews=2.616e+11 (Δ=3.33%)

00700  status=OK  period_end=2025-12-31
  OCF=261,600,000,000  CapEx=38,150,000,000  Standard_FCF=223,450,000,000 HKD
  fcf_yield                0.059882 ratio
  recurring_net_profit   128,838,000,000
  net_debt_to_fcf           0.487805 ratio

PIT replay (look-ahead check):
     as_of status period_end  standard_fcf  fcf_yield  recurring_net_profit
2025-06-30     OK 2024-12-31  1.836000e+11   0.054500      1.195560e+11
2025-12-31     OK 2024-12-31  1.809000e+11   0.050715      1.173960e+11   ← 重述生效
2026-06-30     OK 2025-12-31  2.234500e+11   0.059342      1.288380e+11   ← 新财报生效
```

其余命令：

```bash
python -m qp check                             # 配置/连通性自检
python -m qp init-db                           # 建表 + 灌入汇率/市值参考数据
python -m qp ingest --symbols 00700,09988 --offline
python -m qp run --symbols 00700,09988 --as-of 2026-10-27 --offline
python -m qp factors --symbol 00700 --as-of 2026-10-27
python -m qp history --symbol 00700 --start 2025-01-01 --end 2026-09-30 --freq QE --run
python -m qp report --run-id <run_id>
pytest -q                                      # 全离线测试（PDF 后端缺失时跳过相关测试）
```

生产切换（无需改代码）：

```bash
export QP_DATABASE_URL='postgresql+psycopg2://quant:quant@localhost:5432/quant'
export QP_CLICKHOUSE_ENABLED=1
export QP_LLM_ENABLED=1 QP_LLM_BASE_URL='http://vllm.internal:8000/v1' QP_LLM_MODEL='qwen2.5:14b'
python -m qp run --symbols 00700,09988 --as-of 2026-10-27
```

`docker compose up -d postgres clickhouse` 会带起两份生产 DDL（`src/qp/storage/sql/*.sql`）。

---

## 2. 总体架构

```
[ 原始数据源 ]
  HKEXnews XBRL / PDF  ·  AkShare(东财)  ·  yfinance  ·  汇率/市值/公告日历
        │                                                     │
        ▼                                                     ▼
┌──────────────────────────── Stage 1 ───────────────────────────────┐
│ SourceHub：优先级/重试/单源隔离；RawReport（原始快照 + 披露时钟）      │
│ Normalizer：字段别名（中/英/繁）→ 单位(千元/百万)→ 币种(CNY→HKD)     │
│ MultiSourceAligner：逐字段取值、容差冲突检测、缺失字段由后备源补齐     │
└────────────────────────────────────────────────────────────────────┘
        │
        ├──────────────────────── Stage 2 ────────────────────────────┐
        │  FCFEngine：Standard_FCF = OCF − |CapEx|（硬编码复算）        │
        └──────────────────────────────────────────────────────────────┘
        │
        ├──────────────────────── Stage 3 ────────────────────────────┐
        │  硬规则：利润表 8 类非经常性字段直接扣除                       │
        │  LLM 层：PyMuPDF 抽附注 → OpenAI 兼容结构化 Prompt            │
        │           → 金额必须能在原文定位（反幻觉）→ 正则兜底 → 去重   │
        └──────────────────────────────────────────────────────────────┘
        │
        ▼
┌──────────────────────────── Stage 4 ───────────────────────────────┐
│ QualityChecker：ERROR/WARN/INFO 门禁 + 隔离区                        │
│ Repository：raw_reports / normalized_facts / derived_features …       │
│   PostgreSQL(或 SQLite) 为 PIT 真源；ClickHouse ReplacingMergeTree 镜像│
│ RunSummary → reports/{run_id}.json + 可选 webhook 告警                │
└────────────────────────────────────────────────────────────────────┘
```

代码地图：

| 路径 | 职责 |
|---|---|
| `src/qp/domain.py` | 跨阶段数据契约（`RawReport` / `NormalizedFact` / `AlignedFact` / `FactorValue`） |
| `src/qp/ingest/` | 适配器、形状归一化、证券主数据、披露日历、PIT 汇率、SourceHub |
| `src/qp/clean/` | 字段字典、单位/币种归一化、多源对齐、FCF、质量门禁、附注解析 |
| `src/qp/pit.py` | 可见性过滤 / 版本选择 / 重述检测 |
| `src/qp/factors.py` | PIT 汇率 + PIT 市值下的因子计算 |
| `src/qp/storage/` | SQLAlchemy 仓储（SQLite/PG）、ClickHouse 镜像、DDL |
| `src/qp/pipeline.py` | 四阶段编排：`ingest` / `compute_snapshot` / `run` / `history` |
| `examples/fixtures/` | 合成演示数据（含重述版本、XBRL、附注文本、汇率、市值、公告日历） |
| `tests/` | 离线单测与集成测试 |

---

## 3. 步骤 1：多源拉取与对齐 —— 三个必须先修的坑

### 3.1 AkShare 的真实签名与返回结构

草稿里的调用是 **错的**：

```python
ak.stock_financial_hk_report_em(symbol='00700', indicator='利润表')   # ❌
```

实际接口是 [AkShare 官方文档](https://akshare.akfamily.xyz/data/stock/stock_finance.html)：

```python
ak.stock_financial_hk_report_em(stock="00700", symbol="现金流量表", indicator="年度")
```

且返回的是**长表**（`STD_ITEM_NAME / STD_ITEM_CODE / AMOUNT / REPORT_DATE / DATE_TYPE_CODE / START_DATE`），不是「一行一个报告期、一列一个科目」的宽表。本仓库 `call_akshare()` 先按真实签名调用，遇到 `TypeError` 再回退到旧签名；`to_long()` 同时兼容 AkShare 长表、yfinance 转置宽表、草稿里的 `报告期` 宽表，统一输出：

```
period_end | period_type | raw_label | raw_code | value | unit_label
```

`DATE_TYPE_CODE`：001 年报 / 002 中报 / 003,004 季报（相对各自财年，09988 财年止于 3-31）。

### 3.2 币种红线：`AMOUNT` 不是公司记账原币

社区实测（[Mercurius probe-hk](https://github.com/MMortise/Mercurius/blob/main/docs/probe-hk.md)，akshare 1.18.73 / 2026-07-22）：东财 `stock_financial_hk_report_em` 的 `AMOUNT` 对**所有港股**已按报告期汇率折算为**人民币元**：汇丰(USD) 比例 7.1884、长和(HKD) 0.90622、腾讯/阿里(CNY) 1.000000；返回值里既没有币种字段也没有公告日字段；`analysis_indicator_em` 里的 `CURRENCY` 字段一律写死 `HKD`，不可用于判定。

因此本仓库的做法：

- `config/securities.csv` 同时保存 `report_currency`（真实记账币）与 `akshare_currency`（源侧交付币，默认 `CNY`）；
- `yfinance` 用 `ticker.info['financialCurrency']`；HKEXnews XBRL 用 `unitRef` 的 `iso4217:*`；
- 每个 `NormalizedFact` 同时存 `value_native`（原币、元）与 `value`（目标币种、按期确定性汇率折算），以及 `fx_rate / fx_as_of / fx_source`；
- 估值因子用**评估日 PIT 汇率**重新定价（见 §6），绝不拿今天汇率折算历史财报。

单位同理：`unit_scale`（千元=1e3、百万元=1e6、亿=1e8）显式存在 raw 报告上，`value_native = raw_value × unit_scale`。汇率方向写死在代码注释里：`rate(HKD, CNY) = 1.09` 表示 1 CNY = 1.09 HKD。AkShare 的中行牌价适配器 `from_boc_sina()` 还处理了「100 港币 = N 人民币」→ `1 CNY = 100/N HKD` 的方向转换。

### 3.3 字段对齐不是字符串 `in`

草稿的 `next(col for col in df.columns if '固定资产' in col)` 会命中**资产负债表的固定资产存量**，把 FCF 算成 `OCF − 固定资产总额`。`FieldMatcher` 的规则是：

1. 按报表类型隔离（CF 字段不可能由 BS 行产生）；
2. 精确别名优先，其次按优先级正则（支持简/繁/英文，含 yfinance `Capital Expenditure`、IFRS XBRL tag）；
3. `requires` 动作词（购建/购置/添置/支付/capital expenditure…）与 `excludes` 排除词（折旧/处置/期末/净值…）双重护栏；
4. 歧义并列时拒绝猜测并打 WARN；
5. 稳定的 `STD_ITEM_CODE`（如 `004012999` 除税后溢利）优先于标签。

`tests/test_field_map.py::test_bare_fixed_assets_is_not_capex` 就是这个坑的回归测试。

### 3.4 多源比对

`MultiSourceAligner` 按 `sources.priority = [hkexnews, akshare, yfinance]` 逐字段取值：

- 相对差异 > `align.conflict_rel_tolerance`（默认 1%）→ 记录 `cross_source_conflict` 事件，保留主源值；严格模式把该字段放入隔离区（值为 NULL）；
- **CapEx 只比绝对值**：`hkexnews=-35e9` 与 `akshare=+35e9` 是符号口径差异，不是数据冲突；
- 主源缺字段时用后备源补齐，并记入 `fallback_fields`；
- 每个字段保留 `sources` 血缘、生效 `published_at / available_at`、是否重述。

HKEXnews 侧还实现了 `titleSearchServlet.do` 的公告发现（`stockId` 由 `activestock_sehk_e.json` 解析，`DATE_TIME` 为 HKT 转 UTC）、PDF 下载缓存、以及独立的 XBRL instance 解析器（`context/unitRef/scale/sign`）。端点参数来自 [社区实测记录](https://github.com/carrotly-ai/disclosures/blob/main/HKSG-FEASIBILITY.md)，属于未公开接口，全部放进了配置项以便站点改版时热修。

---

## 4. 步骤 2：FCF 标准化复算

\[
\text{Standard\_FCF} = \text{OCF} - |\text{CapEx}|
\]

- 保留草稿的便捷函数 `calculate_standard_fcf(cash_flow_df)`（返回 `['报告期', ocf列, capex列, 'Standard_FCF', ...]`），但换成 `FieldMatcher` 找列、支持报告期在索引、支持 `(1,234)` / `1.2亿` 之类的数字清洗；
- 生产路径 `FCFEngine.compute()` 作用在 PIT 对齐后的事实上，输出 `ocf / capex_raw / capex / standard_fcf / standard_fcf_native / adjusted_fcf / quality`；
- `fcf.capex_sign_policy` 支持 `auto`（取绝对值，默认）、`positive_outflow`、`negative_outflow`（校验符号并 WARN）、`signed`（`OCF + capex`，适用于 XBRL 负值流出）；
- 质量标记：`negative_ocf`、`capex_sign_mismatch`、`capex_ocf_ratio_implausible`、`fx_*`；
- `adjusted_fcf = standard_fcf − 一次性现金损益`（仅当 LLM/附注把该项标为 `cash_impact`），非现金的公允价值变动**只调整盈利类因子，不动 FCF**。

---

## 5. 步骤 3：非经常性损益 = 硬规则 + LLM 附注解析

**基础层（永远执行）**：`FAIR_VALUE_GAIN / IMPAIRMENT_LOSS / GOVERNMENT_GRANT / FX_GAIN_LOSS / DISPOSAL_GAIN / LITIGATION_SETTLEMENT / OTHER_NONRECURRING_GAIN|LOSS` 八个标准字段从利润表直接抽取，按 `as_gain`（正收益）/ `as_loss`（损失金额）统一成 `profit_impact`：

```
recurring_net_profit = net_profit − Σ profit_impact
```

**进阶层（可关闭）**：

1. `extract_note_chunks()` 用 PyMuPDF（缺则 pdfplumber，再缺则纯文本）在年报 PDF 中按关键词抽**带页码的上下文窗口**，而不是整页塞给模型；
2. `LlmNoteParser` 向任意 OpenAI 兼容 `/chat/completions` 发送严格 JSON schema（[`SYSTEM_PROMPT`](src/qp/clean/notes/llm.py)），要求 `label/amount/currency/unit_scale/direction/category/evidence/cash_impact/confidence/page`；
3. **反幻觉三层校验**：JSON 容错解析、枚举/置信度钳制、以及 `strict_grounding`——金额必须能在输入附注的数字 token 里定位、`evidence` 必须是原文子串、页码必须真实存在，否则丢弃并 WARN；
4. 磁盘缓存按 `(附注文本, model, prompt_version)` 哈希，重跑不重复付费；
5. LLM 关闭/超时/返回非法 JSON → 自动回退到确定性 `RegexNoteParser`（关键词行 + 金额 + 千元/百万元单位），流水线永不因模型阻塞；
6. 规则项与附注项按 `(category, amount)` 容差去重，避免同一笔收益被扣两次；金额不一致时保留两条并 WARN 供人工复核。

演示数据里附注新增了「诉讼赔偿支出 1,200,000 千元」，因此 `recurring_net_profit` 从 `136.25e9` 降到 `128.838e9 HKD`。

---

## 6. 步骤 4：入库、PIT 与监控

### 6.1 时间轴（最容易产生未来函数的地方）

| 字段 | 含义 |
|---|---|
| `period_end` | 财报归属期，**绝不能当可用时间** |
| `published_at` | 交易所披露时间（HKEXnews `DATE_TIME`，HKT→UTC）；无公告时按年报+90/中报+60/季报+45 天**保守估计**并打 WARN |
| `available_at` | 这个**版本**何时对我们可见。AkShare/yfinance 会静默覆盖历史且不给重述日期，因此「第一次看到这个版本」才是可用时间；重复摄取同一 `content_hash` 会复用旧时钟，保证幂等 |

`pit.py` 的规则：只取 `available_at <= as_of(23:59:59 UTC) − lag_days` 的行；同一 `(source, statement, period_end, field)` 取最新版本；重述是**新增行**而不是 UPDATE。`qp history` 用一组历史日期回放因子，是上线前必做的未来函数自检。

### 6.2 汇率与市值同样 PIT

- 归一化（跨源可比）用**报告期末**汇率：确定性、无未来信息；
- `fcf_yield = (standard_fcf_native × FX(as_of)) / (market_cap(as_of) × FX(market_currency→target, as_of))`；
- `market_caps` / `fx_rates` 都只取 `as_of <= 评估日` 的最新一条；缺失/兜底汇率必须打 WARN 或 ERROR，不允许静默用 1.0。

### 6.3 存储

- `raw_reports`：原始长表快照 + 内容哈希，供审计与回放；
- `normalized_facts`：**每个字段每个版本一行**，含币种、单位、汇率、血缘、质量标记；
- `nonrecurring_items`：规则/LLM/正则抽取的明细与证据、页码、置信度；
- `derived_features`：主键 `(symbol, as_of, feature, formula_version)`，改公式生成新序列而非覆盖旧序列；
- `quality_events / pipeline_runs`：质量事件与运行清单；
- PostgreSQL 视图 `pit_facts` 用 `DISTINCT ON` 展示每字段最新可见版本；ClickHouse `ReplacingMergeTree` + `argMax` 视图 `pit_factors` 做宽表服务。

### 6.4 质量门禁与监控

`QualityChecker` 输出结构化事件（而非 `print`）：`estimated_disclosure_date`、`schema_drift`、`cross_source_conflict`、`restatement`、`fx_missing`、`missing_required_field`、`stale_fundamentals`、`llm_fallback`、`quarantined_field` 等。

- ERROR 在 `--strict` 下把该 symbol 放入隔离区（不写因子）；
- 每次 run 生成 `reports/{run_id}.json` + `reports/latest.md`（因子表 + 事件表）；
- 配置 `QP_ALERT_WEBHOOK_URL` 后，出现 ERROR/非 OK 状态时推送 webhook；
- 幂等：同一批数据重复 `run` 不会增加事实行数或改写历史 `available_at`（有集成测试覆盖）。

---

## 7. 工程化清单与已知边界

**已经做到**：全链路离线可跑、自动化测试、类型化数据契约、字段/单位/币种映射表、PIT 版本库、重述回放、LLM 反幻觉与缓存、结构化质量事件、幂等 upsert、PG/ClickHouse DDL、CLI、CI。

**上线前仍需按你的环境补齐**（`docs/OPERATIONS.md` 有详细 checklist）：

1. **公告日历**：把 `refresh_calendar()` 的 HKEXnews 结果持久化到 `announcements.csv`/表；不要用 lag 估计做实盘 PIT。东财/雅虎接口不提供公告日。
2. **汇率源**：GitHub 工作流试用 `from_boc_sina()` 与 yfinance 的免费日度观察，历史期末汇率仍需可靠来源；演示月频 CSV 只是占位。
3. **市值源**：GitHub 工作流试用 yfinance 的当前市值并保守延后至次日可用，历史 PIT、行情准确性及停牌/`8xxxx` 人民币柜台口径仍需核验；服务器 cron 不自动采集。
4. **科目模板**：东财 `STD_ITEM_CODE` 随 `ORG_TYPE`（一般企业/银行/保险/证券）分四套模板；`field_map.py` 的别名需按你们的股票池持续补齐，并监控 `schema_drift` 事件。
5. **LLM**：生产建议本地 vLLM/Ollama + 严格 grounding + 抽样人工复核；`confidence < 0.6` 的项可配置为不参与因子。
6. **规模**：`Repository.get_facts()` 目前把单票事实读进 pandas 做窗口去重；全市场回放应下推为 SQL 窗口函数 / ClickHouse `argMax`（DDL 已给出视图）。
7. **许可与合规**：HKEXnews 不是开放数据许可，PDF 缓存请遵守站点条款；AkShare/yfinance 为第三方接口，生产需要限速与降级。

详细文档：

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — 模块图、数据流、扩展点
- [`docs/DATA_CONTRACT.md`](docs/DATA_CONTRACT.md) — 表结构、字段口径、汇率/符号约定
- [`docs/PIT.md`](docs/PIT.md) — 未来函数的五种来源与验证方法
- [`docs/OPERATIONS.md`](docs/OPERATIONS.md) — 部署、调度、监控、故障处理
- [`docs/SOURCES.md`](docs/SOURCES.md) — 数据源实测结论与参考链接

**演示数据声明**：`examples/fixtures/` 全部是合成数字，只用于验证工程逻辑，不可用于任何研究或交易决策。`scripts/make_fixtures.py` 可一键重建。
