#!/bin/sh
# Called by cron from any directory. Configure .env locally; never commit it.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
ENV_FILE=${QP_ENV_FILE:-"$ROOT/.env"}
if [ ! -f "$ENV_FILE" ]; then
    echo "缺少环境文件，请先配置 $ENV_FILE" >&2
    exit 1
fi
set -a
# .env is a trusted shell file, not arbitrary untrusted input.
. "$ENV_FILE"
set +a
PYTHON=${QP_PYTHON:-"$ROOT/.venv/bin/python"}
mkdir -p data
# Fail rather than overlap runs or send duplicate reports if a prior run is still active.
exec 9>data/pushplus.lock
if ! flock -n 9; then
    echo "上一轮推送仍在运行，本次跳过" >&2
    exit 1
fi
exec "$PYTHON" -m qp push
