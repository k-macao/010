# 数据源实测结论与参考

> 本节记录实现时依赖的第三方接口事实。接口可能改版；`schema_drift` 质量事件就是为此准备的。

## 1. AkShare（东方财富港股财报）

- 文档：<https://akshare.akfamily.xyz/data/stock/stock_finance.html>
- 真实调用：`stock_financial_hk_report_em(stock="00700", symbol="现金流量表", indicator="年度")`；股票代码必须 **5 位零填充**（`00700`），否则报错。
- 返回**长表**：`SECUCODE, SECURITY_CODE, REPORT_DATE, DATE_TYPE_CODE, FISCAL_YEAR, STD_ITEM_CODE, STD_ITEM_NAME, AMOUNT, START_DATE, STD_REPORT_DATE`。利润表/现金流量表为**年初至今累计**口径，不是单季。
- `AMOUNT` 单位：**人民币元**，对所有港股一律如此（东财按报告期汇率折算）。原币需另取东财 `RPT_HKF10_FN_BALANCE` / `RPT_CUSTOM_HKSK_APPFN_CASHFLOW_SUMMARY`；`stock_financial_hk_analysis_indicator_em` 的 `CURRENCY` 字段一律 `HKD`，不可信。
- **没有公告日字段**。公告时间必须从 HKEXnews 拿，或用保守 lag 估计。
- 社区实测报告：<https://github.com/MMortise/Mercurius/blob/main/docs/probe-hk.md>（akshare 1.18.73，2026-07-22；包含币种折算比例、代码模板、汇率方向的详细证据）。
- 同一文档还指出：`stock_hk_spot_em` / `stock_hk_hist` 的东财行情路径可能被服务端拒绝，列表/日线建议用新浪源；`8xxxx` 人民币柜台与主柜台是同一公司的两只证券，做估值可比集时需去重。

## 2. yfinance（备用源）

- `Ticker("0700.HK").cashflow / income_stmt / balance_sheet`：index 为英文科目，columns 为报告期 Timestamp，值以 `ticker.info['financialCurrency']` 计。
- `Capital Expenditure` 通常为**负数**（现金流出），与 AkShare 的正数支出口径相反；本仓库跨源比对时按绝对值。
- 只提供最近约 4 个年度，**没有公告日、没有重述历史**，因此不能作为 PIT 时钟，只能补字段。

## 3. HKEXnews（披露易）

- 搜索页：<https://www1.hkexnews.hk/search/titlesearch.xhtml?lang=en>
- 搜索 servlet：`https://www1.hkexnews.hk/search/titleSearchServlet.do`，关键参数（[社区实测](https://github.com/carrotly-ai/disclosures/blob/main/HKSG-FEASIBILITY.md)，2026-08-21）：
  `market=SEHK&stockId=<内部ID>&searchType=0&fromDate=YYYYMMDD&toDate=YYYYMMDD&sortDir=0&sortByOptions=DateTime&rowRange=100&lang=E&t1code=40000...`；`result` 是 **JSON 字符串**，行内 `DATE_TIME` 为 `DD/MM/YYYY HH:MM`（HKT）、`FILE_LINK` 为可直接下载的 PDF 路径。
- `stockId` 不是 5 位股票代码：从 `/ncms/script/eds/activestock_sehk_e.json` 的 `{"i":7609,"c":"00700","n":"TENCENT"}` 解析，需缓存。腾讯 00700 → 7609。
- `t1code=40000` 为 Financial Statements/ESG Information 大类。
- 官方检索指南：<https://www2.hkexnews.hk/-/media/HKEXnews/Homepage/Listed-Company-Publications/Search-Guide/TitleSearchGuide_e.pdf>
- 财务数字主要在 **PDF/XBRL 文档内部**，没有全市场结构化财务 feed。本仓库用 `xbrl.py` 解析 XBRL instance（`context` 周期、`unitRef` 币种、`scale`、`sign`），用 `pdf_notes.py` 抽附注文本。
- 端点未公开文档、可能变更：所有 URL/参数在 `config/pipeline.toml` 的 `[sources]` 中可配置；建议本地缓存 `activestock_sehk_e.json` 与已下载 PDF。

## 4. 汇率

- `ak.currency_boc_sina(symbol="港币")` 的报价口径是「100 港币 = N 人民币」（[实测](https://github.com/MMortise/Mercurius/blob/main/docs/probe-hk.md)），因此 `1 CNY = 100/N HKD`。
- 本仓库 `rate(base, quote)` = 1 单位 `quote` 兑换多少 `base`；`HKD/CNY = 1.09` 即 1 CNY = 1.09 HKD。
- 生产请用自有/官方日频汇率并保留历史快照；演示用 `examples/fixtures/fx_rates.csv`（月频合成）。

## 5. 关于「非经常性损益」口径

港股财报没有 A 股式的标准「非经常性损益明细表」，相关项目散落在：

- 利润表：投资物业/金融资产公允价值变动、减值、汇兑损益、出售收益；
- 附注：其他收益/其他收入净额、政府补助、诉讼拨备、一次性赔偿；
- 管理层讨论：Non-GAAP / adjusted profit 调整项（注意这是公司自定义口径，不能与会计口径混用）。

因此本仓库采用「硬规则字段 + 附注抽取」双层，并对每条结果保留 `evidence` 与 `page`，便于人工复核。规则表在 `clean/field_map.py`，可按你们的股票池持续维护。
