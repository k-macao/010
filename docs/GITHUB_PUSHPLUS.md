# GitHub Actions 实际推送

仓库级 Actions secret `PUSHPLUS_TOKEN` 会注入到进程变量 `QP_PUSHPLUS_TOKEN`，不能写入源码、日志或聊天。还需要在 GitHub 仓库 Settings → Secrets and variables → Actions 添加仓库级 secret `QP_DATABASE_URL`，内容为可访问的持久 PostgreSQL SQLAlchemy URL（如 `postgresql+psycopg2://…`，请仅在 GitHub UI 中填写）。不要在聊天里发送数据库地址或密码。数据库需允许 GitHub 托管 runner 访问，并提供项目 `fx_rates`、`market_caps`、`normalized_facts` 等表及相应的权限；已有 PostgreSQL 如果表结构不同，需要先适配或迁移，不能假设已经兼容。代码库可通过生产配置执行 `python -m qp --config config/github-actions.toml init-db` 建表，注意会创建其他项目表。

Actions 可选变量 `QP_NOTIFY_SYMBOLS`：逗号分隔、五位港股代码，默认 `00700,09988`。`config/github-actions.toml` 禁止默认静态汇率、指向空的生产参考目录，不加载 `examples/fixtures/`。执行前的只读预检拒绝 SQLite、库内带 fixture/demo/synthetic 来源的汇率或市值、缺失/过期市值及汇率；请在生产库导入真实历史数据。若此前曾把演示数据写入生产库，建议改用隔离的干净数据库并核验所有表：仅检查来源标签不能证明数据真实性。预检失败时不计算因子，会尝试通过 PushPlus 发出失败告警。

**公告日历限制：**当前流水线不会读取数据库里的公告日历，生产参考目录默认也为空；缺失的披露日会使用估计值并告警，不能将其视为已经具备准确 PIT。`SourceHub.refresh_calendar` 尚未接入该调度任务。先对接并核验真实公告时间、汇率、市值及源数据，再用于研究；默认配置及预检不是投资安全保证。在线数据源的首见时间也会影响 `available_at`，刚启用时因 PIT 延迟可能暂时没有可用数据。

工作流 `.github/workflows/pushplus.yml`：

- 每天 UTC 00:00 / 04:00 / 10:00（北京时间/澳门时间 08:00 / 12:00 / 18:00），包括周末；GitHub 定时任务可能延迟或跳过。
- GitHub 定时任务只从**默认分支**执行：合并此工作流到默认分支后才会自动开始。本分支上的文件不会自动生效。
- 合并后到 GitHub Actions → “PushPlus 财务早午晚报告” → Run workflow 手动触发一次；查看运行日志及微信，PushPlus API 返回成功不代表已实际送达微信。
- 不会使用临时 SQLite 保存 PIT 历史；运行前需要在可访问的 PostgreSQL 中准备真实数据。在线源、数据库、Actions 和 PushPlus 网络访问/配额均可能导致失败。
- 生产库不要并发运行其他同股票池的写入任务；Actions concurrency 只约束本工作流。为避免重复通知没有自动重试，也没有持久化的幂等送达保证。
