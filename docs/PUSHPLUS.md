# PushPlus 微信早午晚报告

如果 token 放在 GitHub Actions secrets，请使用 [GitHub Actions 推送说明](GITHUB_PUSHPLUS.md)；仅在 GitHub 保存 token 不会自动启动下面的服务器 cron。

通过常驻 Linux 服务器的 cron，每天北京时间/澳门时间 **08:00、12:00、18:00** 运行一次流水线并发送 PushPlus 消息（含状态、FCF、FCF 收益率和质量告警）。时间可自行调整；周末和节假日也会发送。cron 只会在服务器运行且 cron 服务启动时执行，关机期间不会补发。

## 配置

1. 在 PushPlus 官网完成微信绑定并获取个人 token；不需要把 token 发给本项目开发者。
2. 在服务器上安装项目和线上源依赖（`pip install -e '.[sources]'`），并把真实公告、汇率、市值数据和持久数据库配置好。**默认配置中的汇率、市值和公告数据是合成示例，即使线上抓取也不可用于投资。**
3. `cp .env.example .env`，仅在本机 `.env` 填 `QP_PUSHPLUS_TOKEN`，按需设置 `QP_NOTIFY_SYMBOLS`、`QP_DATABASE_URL`。`.env` 是可信 shell 配置文件，权限建议 `chmod 600 .env`，不要提交、公开或使用不可信文件。若 Python 不在 `.venv/bin/python`，设置 `QP_PYTHON` 为绝对路径。`QP_OFFLINE=1` 为合成数据演示，不能用于生产。
4. 可手工验证（在已激活虚拟环境中，导入 `.env` 后运行）：

```sh
set -a; . ./.env; set +a
python -m qp --offline push --symbols 00700 --as-of 2026-10-27 --slot 早报 --dry-run  # 无需 token、不发消息
python -m qp push --slot 早报  # 实际运行并发送，PushPlus 接受不等于微信一定送达
```

全局参数 `--offline`、`--db-url`、`--strict` 必须放在 `push` 前。真实发送前请确认微信绑定及数据源配置；非 OK 状态、推送失败或流水线异常会返回非零退出码，流水线异常时尽力发送失败通知。

## 启用定时任务

本项目不会自行安装或启动 cron。修改 `scripts/pushplus.crontab.example` 中两处仓库绝对路径，然后用 `crontab -e` **追加**该例子中的两行，不要覆盖已有任务。例子使用 `CRON_TZ=UTC`，适用于支持 `CRON_TZ` 的 cron（如 cronie）；若系统不支持，请按服务器系统时区手工换算。确保 cron 服务运行、`flock` 已安装，仓库路径及环境文件有读取权限。定时脚本使用 `data/pushplus.lock` 防止上一次未完成时并发执行；跳过会记录非零状态。stdout/stderr 记录到被 `.gitignore` 排除的 `pushplus.log`；注意日志仍可能包含第三方服务响应，请保护文件权限。

如果应用在容器中部署，必须在持久运行的宿主机或独立调度器执行脚本并共享持久数据库；不要依赖 GitHub Actions PR 分支定时任务（定时 workflow 通常只在默认分支运行，且临时 runner 不保留本地 SQLite PIT 历史）。PushPlus token 使用 HTTPS JSON 请求体提交，不放 URL；API 响应非 200 或网络异常时退出失败，不自动重试以避免重复通知。
