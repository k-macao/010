# 演示数据（全部为合成数据）

此目录下的 CSV / XML / TXT 均由 `scripts/make_fixtures.py` 生成，数字只是为了使下列工程逻辑可验证，**不是任何公司的真实财报，禁止用于研究或交易决策**。

| 文件 | 用途 |
|---|---|
| `akshare/00700_*.csv` | 长表、人民币千元（`unit_scale=1000`）、含 2024 年报**原始版与重述版**两个 `available_at` |
| `akshare/09988_*.csv` | 3-31 财年结束的第二个 symbol |
| `yfinance/00700_*.csv` | 英文科目、原始币种元、CapEx 负数、OCF 与主源相差 3.33%（触发跨源冲突告警） |
| `hkexnews/00700/00700_2025_annual.xml` | XBRL instance：`context / unitRef / scale / sign` |
| `hkexnews/00700/00700_2025_annual_notes.txt` | 财报附注节选，供 PDF/正则/LLM 解析（LLM 启用时可直接换成 PDF） |
| `announcements.csv` | HKEXnews 披露时间（含重述公告），PIT 时钟 |
| `fx_rates.csv` | 月频 PIT 汇率，1 单位 quote = rate 单位 HKD |
| `market_caps.csv` | 市值快照（HKD），按 as_of 取最新 |

重建：`python scripts/make_fixtures.py`（可选传入输出目录）。
