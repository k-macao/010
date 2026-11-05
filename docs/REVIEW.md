# 代码审查记录：发现的问题与修复

审查范围：全仓库（`src/qp`、`scripts`、`tests`、配置与 DDL、README/docs）。
基准：`main@4ab0036`（本分支起点）。

## 0. 检查方法

```bash
python -m venv .venv && . .venv/bin/activate && pip install -e '.[dev]'
python -m ruff check src tests scripts     # 通过
python -m pytest -q                        # 174 passed（审查前）
python -m qp demo --as-of 2026-10-27       # 全链路离线跑通
python -m qp check / init-db / ingest / run / factors / history / report / push --dry-run
```

静态审查按「数据是否会被悄悄算错」排序：先看 PIT（可见性/版本/重述/汇率/市值），
再看单位与币种，再看期间口径，最后看 CLI、配置与文档一致性。

审查后：`ruff` 通过、`192 passed / 1 skipped`（跳过项为未安装 PyMuPDF 的 PDF 后端测试）。
下面每一条都补了回归测试。

---

## 1. 已修复的问题

### R1 ［高］跨源币种在 FCF 里被直接相减

- 位置：`src/qp/clean/fcf.py`
- 现象：`standard_fcf_native = ocf_native - |capex_native|`，而 `value_native` 是**每个事实各自的报告币种**。
  OCF/CAPEX 可以来自不同源（配置正是 `hkexnews → akshare → yfinance`，东财一律折成 CNY，港交所/雅虎是原币 HKD），
  相减等于把 CNY 当 HKD 用。
- 影响：`fcf_as_of = standard_fcf_native × FX(as_of)` 与 `fcf_yield` 系统性偏差。
  复现：OCF 240 CNY（期末 1.10）+ CAPEX 35 HKD（期末 1.00）→ 目标币 FCF = **229**，
  旧代码 `fcf_as_of` = (240−35)×1.10 = **225.5**。
- 修复：用各事实自带的期末汇率把 CAPEX 折回 OCF 的报告币种再相减，并打 `mixed_report_currency`
  质量标记（自动变成 `fcf_mixed_report_currency` WARN 事件）。所有边界下
  `standard_fcf_native × 期末汇率 == standard_fcf` 都成立。
- 回归：`tests/test_fcf.py::test_mixed_report_currency_native_is_restated_not_added`

### R2 ［高］期间选择会用中报静默替换年报

- 位置：`src/qp/clean/align.py::choose_period`、`src/qp/pipeline.py::compute_snapshot`
- 现象：`choose_period` 只按 `period_end` 倒序取「字段齐全」的期间，不看 `period_type`。
  一旦某期中报同时有 OCF/CAPEX，就会被选中。
- 影响：`standard_fcf` 是期间**流量**（半年），`fcf_yield` 的分母是时点市值 → 收益率被人为砍半，
  且因子序列出现假台阶。复现：2025 年报齐全 + 2026 中报齐全时，旧逻辑返回 `2026-06-30`。
- 修复：优先取最新**完整 ANNUAL**；只有在没有任何完整年报可见时才退化为其他期间，并由 pipeline
  发出 `non_annual_period` WARN（明示"未年化"）。staleness 检查改为作用在最终选中的期间上。
- 回归：`tests/test_align.py::test_choose_period_prefers_annual_over_newer_interim`、
  `tests/test_pipeline.py::test_interim_only_data_is_flagged_instead_of_silently_annualized`

### R3 ［高］附注文档的期间解析永远返回 12-31

- 位置：`src/qp/clean/notes/pdf_notes.py::infer_period_end`
- 现象：正则抓到了年/月/日却只用了年份，`00700_2025_06_30_interim.txt → 2025-12-31`。
- 影响：中报/季报的非经常性损益被挂到年报期间，`recurring_net_profit` 与 `adjusted_fcf` 都被污染；
  3 月财年公司（09988）的年报也会被挂到错误的 12-31。
- 修复：解析并校验月/日，非法日期返回 `None`（落到 fallback 并 WARN）；同时 `_ingest_notes`
  用真实的 `period_type` 去解析披露时钟，中报按 60 天而不是 90 天估计。
- 回归：`tests/test_pdf_notes.py::test_infer_period_end_keeps_month_and_day`

### R4 ［中］重述标记跨期泄漏，且会把未来才发生的重述算进来

- 位置：`src/qp/pipeline.py::compute_snapshot`
- 现象：`restated_fields = {r.field for r in detect_restatements(all_facts)}` 不带期间、不带 as_of。
- 影响：FY2024 的一次重述会把 FY2025 的同名字段标成 restated（demo 里多出 4 条 INFO，27→23）；
  更严重的是 `as_of` 早于重述日时，回放会用「当时还不知道」的重述标记。
- 修复：重述事件分两套——`known_restatements`（`available_at <= as_of`）用于事件；
  只有 `period_end == 选中期间` 的字段才进入 `restated_fields` 参与对齐标记。
- 回归：`tests/test_pipeline.py::test_restatement_flag_is_scoped_to_its_own_period`

### R5 ［中］过期汇率被静默使用

- 位置：`src/qp/ingest/fx.py`、`src/qp/clean/quality.py`
- 现象：`fx.max_age_days` 只写进 `fx_stale` 标记，而 `QualityChecker.check_alignment`
  只处理 `fx_fallback` / `fx_missing`；`rate()` 本身不校验年龄。
- 影响：2019 年的汇率可以被用来归一化 2025 年财报，报告里一条事件都没有。
- 修复：`check_alignment` 增加 `fx_stale` WARN（带 `fx_as_of`），与 `fx_missing`(ERROR) 分级。
- 回归：`tests/test_quality.py::test_stale_fx_is_reported_as_a_warning`

### R6 ［中］过期市值被静默当成当日市值

- 位置：`src/qp/config.py`（新增 `pit.market_cap_max_age_days`）、`src/qp/clean/quality.py`
- 现象：`fcf_yield` 的分母取「`as_of <= 评估日` 的最新一条」，对年龄没有任何约束。
  demo 用的市值停在 2026-06-30，评估日 2026-10-27，差 119 天，旧版本完全无提示。
- 修复：新增 `pit.market_cap_max_age_days`（默认 10 天），超龄发 `stale_market_cap` WARN。
- 回归：`tests/test_quality.py::test_stale_market_cap_is_reported`

### R7 ［中］README 里的命令直接报错

- 位置：`src/qp/cli.py`、`README.md`
- 现象：`python -m qp ingest --symbols ... --offline` → `error: unrecognized arguments: --offline`。
  argparse 只接受把全局开关放在子命令**之前**，README 却在之后（`docs/PUSHPLUS.md` 反而写对了）。
- 修复：全局开关（`--config/--offline/--db-url/--strict/--log-level`）同时挂到每个子命令上，
  用 `default=argparse.SUPPRESS` 保证「放在前面」的用法不被覆盖。两种写法都可用。
- 回归：`tests/test_cli.py::test_global_flags_are_accepted_after_the_subcommand`

### R8 ［中］`--config` 指向不存在的文件时静默使用默认配置

- 位置：`src/qp/config.py::load_config`
- 现象：候选列表是 `(path, QP_CONFIG, config/pipeline.toml)`，任一存在即采用；显式传入的路径不存在
  时会悄悄回落到默认 `pipeline.toml`。
- 影响：以为切到了生产配置，实际跑的是 demo 默认值（SQLite + fixtures）。
- 修复：显式路径必须存在，否则 `ConfigError`；CLI 捕获后打印「配置错误：…」并返回 2（不再抛栈）。
- 回归：`tests/test_config.py::test_missing_explicit_config_raises_instead_of_silently_using_default`、
  `tests/test_cli.py::test_missing_config_path_fails_loudly`

### R9 ［低］期间类型推断把 Q1/Q3 当作中报

- 位置：`src/qp/ingest/shapes.py::_infer_period_type`
- 现象：`period_end.month in (6, 9, 12, 3) → INTERIM`，与财年无关；12-31 财年的 3-31/9-30 其实是季报，
  3-31 财年的 12-31 也被叫成中报。
- 影响：期间标签错误会被下游用来选披露 lag（60 天 vs 45 天）和展示口径。
- 修复：按财年末计算月份距离：0 → ANNUAL，6 → INTERIM，3/9 → QUARTERLY，其他 → INTERIM；
  月末日按当年实际天数比较（兼容 2-29）。
- 回归：`tests/test_shapes.py::test_period_type_is_relative_to_fiscal_year_end`

### R10 ［低］XBRL 期间一律标成 ANNUAL，且未标记估算披露日

- 位置：`src/qp/ingest/hkexnews.py`
- 现象：`period_type=PeriodType.ANNUAL` 写死；`metadata` 里也少了 `disclosure_estimated`，
  于是 `estimated_disclosure_date` 质量事件永远不会对 XBRL 源触发。
- 影响：中报 XBRL 会被当成年度数据（叠加 R2 后更危险）；用 lag 估计的披露日没有任何告警。
- 修复：按 context 的 `startDate→endDate` 跨度判定 ANNUAL/INTERIM/QUARTERLY（瞬时上下文回落到财年末推断），
  并按真实期间类型解析披露时钟；`metadata` 补 `disclosure_estimated`。
- 回归：`tests/test_xbrl.py::test_xbrl_period_type_uses_the_context_span`

### R11 ［低］`bool(NaN)` 把 NULL 读成 True

- 位置：`src/qp/pipeline.py::rows_to_items`
- 现象：`cash_impact=bool(row.get("cash_impact"))`；从库里读回 NULL 时 pandas 给 `NaN`，
  而 `bool(float('nan'))` 是 `True`。
- 影响：`cash_impact` 被误判为真会把该笔非经常项从 `adjusted_fcf` 里扣掉；`corroborated` 同理。
- 修复：新增 `_is_true()`（None/NaN/空串 → False，字符串按 `1/true/yes` 解析）。
- 说明：现有写入路径总会写 0/1，所以是潜在风险而非当前 demo 的活跃 bug。

### R12 ［低］浮点噪声被识别成重述

- 位置：`src/qp/pit.py::detect_restatements`
- 现象：`float(row["value"]) != float(previous["value"])` 精确比较；不同源/不同折算顺序产生的
  1e-12 级差异会产出假的 `restatement_detected` 事件。
- 修复：`math.isclose(rel_tol=1e-9, abs_tol=1e-6)`。
- 回归：`tests/test_pit.py::test_float_noise_is_not_a_restatement`

### R13 ［低］DDL 用 `TIMESTAMPTZ`，应用写的是 naive UTC

- 位置：`src/qp/storage/sql/postgres.sql`
- 现象：SQLAlchemy 模型全部是 `DateTime`（无时区），`util.utcnow()` 也返回 naive UTC；但 DBA 侧 DDL
  用 `TIMESTAMPTZ`，文件头注释还写着「由应用以 TIMESTAMPTZ 存储」。
- 影响：在非 UTC 会话时区，psycopg2 会把 naive UTC 当成会话本地时间写入，`published_at/available_at`
  整体平移，`available_at <= cutoff` 的 PIT 过滤随之出错；而 `create_all` 建库与 DDL 建库得到两种语义。
- 修复：DDL 改为 `TIMESTAMP`（WITHOUT TIME ZONE），文件头写清「全库 naive UTC，服务器保持 TZ=UTC」；
  `pit_facts` 视图注释改为「最新归档版本，查询必须自己加 `available_at <= :as_of`」。
- 说明：已用 `TIMESTAMPTZ` 建过库的环境需要一次迁移，否则两种 DDL 语义仍会并存。

### 附带：文档与示例输出对齐

- `README.md` 的 `quality` 计数、`counts.events_warn`、`PIT replay` 表格改为实际输出
  （INFO 27→23 是 R4 的预期结果；WARN 1→2 来自 R6 的 `stale_market_cap`）。

---

## 2. 未修复的风险与建议（按优先级）

| # | 问题 | 影响 | 建议 |
|---|---|---|---|
| 1 | 全库使用 naive UTC | 跨系统/BI 读取、非 UTC 会话时区下容易错判 | 中长期统一 `DateTime(timezone=True)` + aware datetime；短期保持 `TZ=UTC` 并在运维手册写明 |
| 2 | `Repository.get_market_cap` 用 `(as_of DESC, source DESC)` 取值 | 同日多源并存时按字母序取源（`yfinance` 会压过 `akshare`），绕过 `sources.priority` | 传入 priority 列表，或在 `market_caps` 增加 `priority` 列 |
| 3 | 只有中报时仍产出因子 | 现在是 WARN，`--strict` 下也可入库 | 若策略要求年化，把 `non_annual_period` 在 strict 模式升级为 ERROR |
| 4 | 「其他收入/其他收益」被当作一次性收益剔除 | 港股很多公司的其他收入是经常性的（利息、租金），会高估 `nonrecurring_ratio`、低估经常性盈利 | 按科目细分（利息收入/租金收入单独处理），或用 `confidence < 0.6` 门槛不参与因子 |
| 5 | `IMPAIRMENT_AND_PROVISIONS` 只映射不使用 | 东财「减值及拨备」这类合并科目既不进规则项，也不参与 `recurring_net_profit` → 减值被漏掉 | 明确策略：或折算进 `IMPAIRMENT_LOSS`，或作为独立科目参与剔除并标注口径 |
| 6 | `Repository.get_facts` 把单票全量读进 pandas 做窗口过滤 | 全市场回放不可行 | 下推为 SQL 窗口函数 / 走 ClickHouse `argmax` 视图（DDL 已给出） |
| 7 | ClickHouse `version` 列复用 `computed_at`（秒级） | 同一秒内多批写入时 ReplacingMergeTree 的去重结果不确定 | 改用 `now64(9)`/单调版本号 |
| 8 | `refresh_calendar()` 只在内存生效 | 实盘 PIT 依赖 lag 估计，`estimated_disclosure_date` 会长期偏高 | 落库/落 `announcements.csv`（README 已列为待办），并做成每日作业 |
| 9 | `schema_drift` 会同时产生 `normalization` ERROR 与 `schema_drift` ERROR | 监控里同一故障被计两次 | 归一化层只发 WARN，`schema_drift` 保留唯一 ERROR |
| 10 | `qp demo` 的回放窗口截断在 2026-09-30 | `--as-of` 更晚时回放静默变短 | 改为 `as_of` 与「最后可用报告期」取小，或直接打印截断提示 |
| 11 | 演示/生产共用 `examples/fixtures/fx_rates.csv` 且含 `HKD→HKD` 恒等行 | 无害但冗余；`fixture` 源标识会被 `require_no_demo_reference_rows` 拒绝写入，属预期 | 保持现状，或在 `make_fixtures.py` 里去掉恒等行 |
| 12 | 测试缺少 PDF 后端（CI 只装 `.[dev]`） | `test_pdf_backend_if_available` 长期 skipped，PDF 抽取无 CI 覆盖 | CI 增加一档 job 装 `.[sources]` |

---

## 3. 关键结论的复现方法

```bash
# R1 跨源币种
python - <<'PY'
from datetime import date, datetime
from qp.domain import AlignedReport, AlignedFact, OCF, CAPEX
from qp.clean.fcf import FCFEngine
def f(field, value, native, ccy, fx):
    return AlignedFact("00700","CF",date(2025,12,31),"ANNUAL",field,value,native,
                       "HKD",ccy,fx,date(2025,12,31),("x",),datetime(2026,3,25),datetime(2026,3,25))
a = AlignedReport("00700", date(2025,12,31), "ANNUAL", date(2026,10,27),
                  facts={OCF: f(OCF,240*1.1,240,"CNY",1.1), CAPEX: f(CAPEX,35,35,"HKD",1.0)})
r = FCFEngine("auto").compute(a)
print(r.standard_fcf, r.standard_fcf_native, r.quality)   # 229 208.18 ['mixed_report_currency']
PY

# R2 期间选择
python -m qp history --symbol 00700 --start 2025-01-01 --end 2026-09-30 --freq QE --run

# R3/R4/R6 组合效果（demo 的 PIT 回放 + 质量事件）
python -m qp demo --as-of 2026-10-27
```

---

## 4. 变更清单

| 文件 | 变更 |
|---|---|
| `src/qp/clean/fcf.py` | R1 跨源币种折算、`mixed_report_currency` 标记 |
| `src/qp/clean/align.py` | R2 `choose_period` 优先年报 |
| `src/qp/clean/notes/pdf_notes.py` | R3 期间解析保留月/日 |
| `src/qp/clean/quality.py` | R5 `fx_stale`、R6 `stale_market_cap` |
| `src/qp/pipeline.py` | R4 重述作用域、R2 非年度 WARN、R3 附注披露时钟、R11 `_is_true` |
| `src/qp/config.py` | R6 新配置项、R8 显式配置必须存在 |
| `src/qp/cli.py` | R7 子命令后置全局开关、R8 配置错误友好退出 |
| `src/qp/ingest/shapes.py` | R9 期间类型推断 |
| `src/qp/ingest/hkexnews.py` | R10 XBRL 期间类型 + `disclosure_estimated` |
| `src/qp/pit.py` | R12 重述浮点容差 |
| `src/qp/storage/sql/postgres.sql` | R13 `TIMESTAMP` + 注释 |
| `config/pipeline.toml` | R6 `pit.market_cap_max_age_days` |
| `README.md` | 示例输出与文档一致 |
| `tests/*` | 每个修复对应的回归测试（18 个新用例，参数化后 19 项） |
