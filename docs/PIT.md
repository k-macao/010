# Point-In-Time：五种未来函数与本仓库的防线

回测里最贵的 bug 不是算错一个数，而是**用了当时还不知道的数据**。港股尤其容易踩，因为开源数据源普遍不给公告日、不保留重述、还偷偷改币种。

## 1. 五种典型污染

| # | 污染方式 | 后果 | 本仓库的防线 |
|---|---|---|---|
| 1 | 用 `period_end`（如 2025-12-31）当可用时间 | 2026 年 3 月才披露的数据在 2026 年 1 月就被使用 | `DisclosureCalendar` 提供 HKEXnews 公告日；无公告时用 lag 估计并打 WARN；`pit.lag_days` 再延后 1 天 |
| 2 | 静默重述：厂商把历史 OCF 从 2000 亿改成 1980 亿，不告诉你何时改的 | 回测用了「未来修正值」 | 快照内容哈希 + `available_at = 首次看到该版本的时间`；重述作为**新行**入库；`detect_restatements()` 记录变更 |
| 3 | 用今天的汇率折算 3 年前的财报 | 汇率序列本身带未来信息 | 归一化用 `period_end` 汇率，估值用 `as_of` 汇率，两者都只取 `<= as_of` 的最新值 |
| 4 | 用今天/未来的市值去除历史 FCF | 分母含未来价格 | `market_caps` 按 `as_of <= 评估日` 取最新一条 |
| 5 | 重跑流水线时用最新解析结果覆盖历史事件/因子 | 同一 as_of 的因子值会变 | 事实/因子按版本主键 upsert，公式版本进主键，`quality_events` 按 run_id 替换 |

## 2. 双时钟模型

```
period_end ─── published_at ─── available_at ─── as_of
  归属期        真实披露日       该版本首次可见     回测评估日
   (不可用)      (公告日历)       (快照哈希)        (只取左边的行)
```

- `published_at` 来自 HKEXnews `DATE_TIME`（`DD/MM/YYYY HH:MM` HKT → UTC−8h）。
- `available_at` 对于**回填/离线演示**等于 `published_at`；对于**在线摄取**，若内容哈希未见过，则取 `max(published_at, now)`——因为厂商不告诉你它什么时候偷偷改了历史。
- 若同一内容哈希再次出现，复用最早那次的 `available_at`，保证重复运行幂等。
- 查询条件恒为：`available_at <= as_of_cutoff(as_of, lag_days)`。

## 3. 演示中的重述回放

`examples/fixtures/akshare/00700_cf.csv` 对 2024-12-31 存了两个版本：

| 版本 | published_at / available_at | OCF(千元 CNY) | CapEx(千元 CNY) |
|---|---|---|---|
| 原始年报 | 2025-03-19 | 200,000,000 | 30,000,000 |
| 重述 | 2025-10-15 | 198,000,000 | 30,500,000 |

`qp demo` 的 PIT 回放结果（HKD，期末汇率 1.08）：

| as_of | 使用版本 | Standard_FCF | fcf_yield |
|---|---|---:|---:|
| 2025-06-30 | 原始 | 183.6e9 | 0.0545 |
| 2025-12-31 | 重述 | 180.9e9 | 0.0507 |
| 2026-06-30 | 2025 新财报 | 223.45e9 | 0.0593 |

`fcf_yield` 在同一 FCF 下也会变，因为 PIT 汇率与 PIT 市值变了——这是正确行为。

## 4. 自检清单

- [ ] `python -m qp history --symbol 00700 --start 2024-01-01 --end 2026-09-30 --freq QE --run`：早期日期必须是 `NO_DATA`，不能出现 2025 年报。
- [ ] 检查 `quality_events` 里 `estimated_disclosure_date` 的数量；实盘应接近 0。
- [ ] 任意 `as_of` 下，把 `normalized_facts` 的 `available_at` 与该日比较：`tests/test_pit.py::is_lookahead_free` 已覆盖基本条件。
- [ ] 对同一 `as_of` 重复跑 `qp run`，因子值必须完全一致（`test_rerun_is_idempotent`）。
- [ ] 汇率/市值表也要有历史快照；只存「当前值」的表无法做 PIT。
- [ ] 因子上线前用 `--strict` 跑一遍：有 ERROR 的 symbol 应被隔离，不应悄悄产出因子。

## 5. 已知妥协

- AkShare/yfinance 只返回**当前**快照，无法还原「历史上第一次披露的原始数据」。因此本仓库只能保证「不会使用晚于我们首次抓取时间的修正值」，不能保证拿到真正的首发原稿。若需要首发原稿，必须每天归档原始响应（本仓库的 `raw_reports` 快照表正是为此设计）或购买含公告日的商业数据。
- 无公告日时使用 lag 估计是保守的（推迟可用时间），不会引入未来函数，但会降低因子时效性。`estimated_disclosure_date` 事件用于衡量这个比例。
