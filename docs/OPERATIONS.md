# 部署与运维手册

## 1. 安装矩阵

```bash
pip install -e .                       # 核心：pandas + SQLAlchemy
pip install -e '.[sources]'            # AkShare / yfinance / PyMuPDF / pdfplumber
pip install -e '.[postgres]'           # psycopg2
pip install -e '.[clickhouse]'         # clickhouse-connect
pip install -e '.[all]'                # 全部（含 dev）
```

Python ≥ 3.11（使用 `tomllib`）。测试只依赖核心 + pytest。

## 2. 配置优先级

```
代码默认值 < config/pipeline.toml < 环境变量 (QP_*) < CLI 参数
```

常用变量见 `.env.example`。敏感信息（API Key、DB 口令）只从环境变量读取，**不要写进 TOML 并提交**。

## 3. 数据库

### PostgreSQL

```bash
docker compose up -d postgres
export QP_DATABASE_URL='postgresql+psycopg2://quant:quant@localhost:5432/quant'
python -m qp init-db
```

`docker-compose.yml` 会挂载 `src/qp/storage/sql/postgres.sql` 作为初始化脚本；应用侧的 `Repository.init_schema()` 用 SQLAlchemy 创建同样的表（可重复执行）。生产建议：

- 对 `normalized_facts` 按 `period_end` 做年度分区；
- `available_at` / `content_hash` 建索引（DDL 已含）；
- 定期 VACUUM/ANALYZE，长时间线的事实表用 BRIN 索引。

### ClickHouse（可选镜像）

```bash
export QP_CLICKHOUSE_ENABLED=1 QP_CLICKHOUSE_URL='http://localhost:8123'
```

DDL 在 `src/qp/storage/sql/clickhouse.sql`；`ReplacingMergeTree(version)` + `pit_factors` 视图处理重复写入。镜像失败只记 WARN，不影响主库。

## 4. 建议调度

| 频率 | 任务 | 命令 |
|---|---|---|
| 每日 07:00 HKT | 同步 HKEXnews 公告日历（PIT 时钟） | `SourceHub.refresh_calendar(symbol)` 遍历股票池 |
| 每日 07:30 HKT | 汇率快照 | 自有汇率源 / `FxRateProvider.from_boc_sina` |
| 每日收盘后 | 市值快照 | 行情系统 → `market_caps` |
| 财报季增量 | 拉取 + 清洗 + 入库 | `qp ingest --symbols ... --as-of ...` |
| 每日 | 因子计算与质量门禁 | `qp run --symbols ... --as-of ... --strict` |
| 每周 | PIT 回放自检 | `qp history --start ... --end ... --freq QE` |
| 每次改公式 | 递增 `config.fcf.formula_version` | 新序列与旧序列并存 |

`qp run` 是幂等的：同一 `as_of` 可以安全重跑。

## 5. 监控指标

从 `pipeline_runs.summary` / `quality_events` 采集：

- `events_error`、`events_warn` 的绝对值与环比；
- `schema_drift` / `fx_missing` / `estimated_disclosure_date` / `llm_fallback` 的出现次数；
- `cross_source_conflict` 按字段与源分布（持续上升说明某个源改了口径）；
- `restatement` 事件数量与影响字段；
- `raw_reports` / `normalized_facts` 每日新增行数（骤降说明抓取失败）；
- 因子缺失率（某 symbol 无 `fcf_yield` 的天数）。

告警：`QP_ALERT_WEBHOOK_URL` 指向 Slack/企微/告警网关；出现 ERROR 或非 OK 状态时推送。

## 6. 故障处理

| 现象 | 排查 |
|---|---|
| 全部 `NO_DATA` | 检查 `available_at` 是否都晚于 `as_of`；`pit.lag_days` 是否过大；`announcements.csv` 是否缺该 symbol |
| `schema_drift` ERROR | 厂商改了列名；用 `qp ingest` 看 `raw_reports.payload`，把新标签加到 `field_map.py` 的 aliases |
| `cross_source_conflict` 暴增 | 先确认是否是符号/单位差异（CapEx 已按绝对值比较）；再确认 AkShare 是否又换了一套 `STD_ITEM_CODE` |
| `fx_missing` | 汇率表未按日更新或 `as_of` 早于最早汇率；补齐历史汇率，不要用今日汇率回填历史 |
| LLM 抽取为空 | `note_documents()` 是否找到 PDF；PyMuPDF 是否安装；关键词是否覆盖繁体；检查 `llm_fallback` WARN |
| 因子值在不同 run 间漂移 | 检查 `formula_version`、`fx_rates` 历史是否被覆盖、`market_caps` 是否只有当前值 |
| ClickHouse 与 PG 不一致 | 镜像是最终一致；以 PG 为准，重跑 `qp run` 触发重写 |

## 7. 安全与合规

- 所有外部调用通过 `urllib` 统一设置超时；PDF 下载有 `max_document_bytes` 上限。
- API Key 从环境变量读取，日志与异常信息不打印密钥。
- `raw_reports` 保存厂商原始响应，注意许可证与数据保留期限；HKEXnews 站点条款不是开放数据许可。
- `examples/fixtures/` 为合成数据，禁止用于研究/交易；生产数据目录 `data/` 与 `reports/` 已在 `.gitignore` 中排除。

## 8. CI

`.github/workflows/ci.yml` 在 Python 3.11/3.12 下执行 `ruff check` + `pytest -q` + `qp demo`，全部离线、无需密钥。
