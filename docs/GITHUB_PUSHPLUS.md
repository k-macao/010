# GitHub Actions 实际推送

仓库级 Actions secret `PUSHPLUS_TOKEN` 会注入到进程变量 `QP_PUSHPLUS_TOKEN`，不能写入源码、日志或聊天。还需要在 GitHub 仓库 Settings → Secrets and variables → Actions 添加仓库级 secret `QP_DATABASE_URL`，内容为可访问的持久 PostgreSQL SQLAlchemy URL（如 `postgresql+psycopg2://…`，请仅在 GitHub UI 中填写）。不要在聊天里发送数据库地址或密码。数据库需允许 GitHub 托管 runner 访问，并提供项目 `fx_rates`、`market_caps`、`normalized_facts` 等表及相应的权限；已有 PostgreSQL 如果表结构不同，需要先适配或迁移，不能假设已经兼容。代码库可通过生产配置执行 `python -m qp --config config/github-actions.toml init-db` 建表，注意会创建其他项目表。

Actions 可选变量 `QP_NOTIFY_SYMBOLS`：逗号分隔、五位港股代码，默认 `00700,09988`。`config/github-actions.toml` 禁止默认静态汇率、指向空的生产参考目录，不加载 `examples/fixtures/`。运行时先尝试采集免费行情，再做预检；预检拒绝 SQLite、库内带 fixture/demo/synthetic 来源的汇率或市值、缺失/过期市值及汇率。若此前曾把演示数据写入生产库，建议改用隔离的干净数据库并核验所有表：仅检查来源标签不能证明数据真实性。采集或预检失败时不计算因子，会尝试通过 PushPlus 发出失败告警。

## 免费行情自动采集及限制

每次 GitHub 工作流预检前先运行 `scripts/refresh_market_reference.py`：使用 yfinance 获取近期汇率及港股市值；市值读取 `marketCap`，必要时回退 `fast_info` 并以近期日线校验行情日期。之后运行 `scripts/refresh_historical_fx.py`，从香港金融管理局（HKMA）官方 `er-eeri-daily` API 同步最近 550 天每日 CNY/HKD 记录；HKMA 字段是每单位外币对应的 HKD，直接符合本项目 `base=HKD, quote=CNY` 口径。历史行按 `(base, quote, as_of, source)` 幂等写入，不覆盖已保存的观察。该历史序列供财报期末币种归一化使用；供应商公布/修订时间仍不在 schema 中，必须保留发布日及财报可用日的 PIT 限制。任一采集源失败会以固定提示失败关闭，不会伪造汇率。HKMA 汇率是参考价，不等同于交易成交价；免费行情可能延迟、失效或受供应商限制，市值的股份数更新时间也无法保证。请核验来源、口径和使用许可，不能直接用于投资。

为了避免把今天抓到的行情伪装成更早可用的值，近期 yfinance 快照仍将今天首次观察记到明天的 `as_of`，同一来源、同一可用日第一次写入后不再覆盖。历史 CNY/HKD 是独立路径：由 HKMA 发布的 `end_of_day` 历史参考汇率写入其真实报价日，且仅覆盖最近 550 天；它用于报告期末币种归一化，不会将当前汇率回填为历史数据。HKMA 对历史记录的修订/实际公开时间在本项目 date-only schema 中不可审计，研究历史 PIT 时应另外核验。其他币种或更早历史期间仍需自行提供有可靠来源与日期的汇率。首次运行当天的最新行情预检仍可能因快照次日才可用而失败。`as_of` 是日期而非完整时间戳，表中不保存供应商报价时间或抓取时间，无法审计盘中 PIT 或保证供应商从未修订；预检的 7 天市值/45 天汇率限制基于可用日，不等于供应商原始报价的新鲜度。三次推送可能使用同一份较早快照，不是盘中实时行情。

采集源依赖网络和供应商可用性，GitHub 工作流需要 PostgreSQL 连接账号对两张表有 SELECT 和 INSERT 权限。供应商可能无数据或限流：此时不会写入伪造值，但若已有未过期数据，预检可能仍会通过，须检查工作流中的 `参考数据警告`。空表只表示准备好了结构，不表示已有真实数据。**未实际验证行情、历史披露和汇率口径前，结果仅供工程参考。**

**公告日历限制：**当前流水线不会读取数据库里的公告日历，生产参考目录默认也为空；缺失的披露日会使用估计值并告警，不能将其视为已经具备准确 PIT。`SourceHub.refresh_calendar` 尚未接入该调度任务。先对接并核验真实公告时间、汇率、市值及源数据，再用于研究；默认配置及预检不是投资安全保证。在线数据源的首见时间也会影响 `available_at`，刚启用时因 PIT 延迟可能暂时没有可用数据。

工作流 `.github/workflows/pushplus.yml`：

- 每天 UTC 00:00 / 04:00 / 10:00（北京时间/澳门时间 08:00 / 12:00 / 18:00），包括周末；GitHub 定时任务可能延迟或跳过。
- GitHub 定时任务只从**默认分支**执行：合并此工作流到默认分支后才会自动开始。本分支上的文件不会自动生效。
- 合并后到 GitHub Actions → “PushPlus 财务早午晚报告” → Run workflow 手动触发一次；查看运行日志及微信，PushPlus API 返回成功不代表已实际送达微信。
- 不会使用临时 SQLite 保存 PIT 历史；运行前需要在可访问的 PostgreSQL 中准备真实数据。在线源、数据库、Actions 和 PushPlus 网络访问/配额均可能导致失败。
- 生产库不要并发运行其他同股票池的写入任务；Actions concurrency 只约束本工作流。为避免重复通知没有自动重试，也没有持久化的幂等送达保证。

## 预检排查

`数据库不可访问或表结构不兼容` 是旧版的通用安全提示，单凭该提示无法确定故障；新版将输出 DNS、连接、认证、TLS、库名、表结构、权限或未知原因等固定类别，不会打印数据库 URL、密码、SQL 或驱动原始错误。新版合并到默认分支后重新运行才能看到分类，分类仍可能无法覆盖所有服务商的错误。

- DNS / 连接：`QP_DATABASE_URL` 必须是完整的 PostgreSQL SQLAlchemy URL，不只是数据库名。GitHub 托管 runner 无法直接访问 `localhost` 或一般私网地址；检查数据库服务状态、端口、网络和允许的来源。优先采用私网 self-hosted runner 或受控网络，不要为了测试而无条件公开数据库。
- 认证 / TLS：核对数据库账号、密码、服务端访问策略与 SSL 参数；URL 中密码包含 `@`、`:`、`/` 等特殊字符时要进行 URL 编码。请在 GitHub 界面修改 secret，不要把密码或 URL 放到聊天和日志里。
- 表结构 / 权限：确认连接到预期的数据库与 schema，且账号可以查询 `fx_rates` 和 `market_caps`，结构见 `src/qp/storage/schema.py`。`init-db` 会创建项目表但不会提供真实市场数据，运行前确认权限和对现有数据库的影响。
- 如仍显示未知原因，可在自己的可信机器上独立检查数据库、网络和数据库服务端日志；分享诊断时请先移除 URL、用户名、密码、IP、SQL 参数及其他敏感信息。

## PushPlus 返回错误

如果 **Check production reference data** 先失败，而 **Push data-quality failure notification** 又失败，表示数据库预检和失败告警是两个独立问题；请先查看前者的诊断，正常财务推送步骤不会执行。PushPlus 告警失败不说明数据库预检已通过。

新版仅输出 PushPlus 的已知业务码及固定提示，不记录 token 和服务端原文。按 [PushPlus 官方返回码说明](https://dev.pushplus.plus/doc/guide/code.html)：903 表示用户令牌无效，905 表示账号未实名认证，900 表示账号受限/请求过多（不要反复重试），401/403 与授权或 IP 白名单有关；999 需要在 PushPlus 后台核对账号、公众号关注/绑定和消息配置。HTTP 状态码与 JSON 业务码不是同一个字段。如果 API 返回 200，仅代表 PushPlus 接收请求，并不保证微信送达。避免在日志、聊天或 issue 中粘贴 token、完整响应或数据库连接地址。若密钥曾被公开，请在服务端立即更换。

如果诊断仍是“未知原因”，新版预检还会显示固定的阶段和类别，不输出异常原文：`url-parse` 是 URL 格式检查，`engine-init` 是驱动初始化，`connection` 是建立数据库连接，`reference-query` 是查询参考表；类别如 `value`、`driver`、`db-operational`、`db-programming` 可以帮助缩小范围。Supabase 的 Session pooler 地址需从项目 Connect 面板完整复制，不能只修改直连地址的端口；GitHub 托管 runner 通常需要 IPv4 可访问的 shared pooler。通过连接后仍须准备真实汇率和市值数据并核验公告日历。
