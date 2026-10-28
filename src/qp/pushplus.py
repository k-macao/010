"""PushPlus delivery of pipeline summaries; token is never put in URLs or logs."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

from .domain import RunSummary, Severity

PUSHPLUS_URL = "https://www.pushplus.plus/send"
CHINA_TZ = ZoneInfo("Asia/Shanghai")


class PushPlusError(Exception):
    """Delivery failed (messages intentionally exclude response body and credentials)."""


def current_slot(now: datetime) -> str:
    local = now.astimezone(CHINA_TZ)
    return "早报" if local.hour < 11 else "午报" if local.hour < 15 else "晚报"


def format_summary(summary: RunSummary, *, offline: bool = False) -> str:
    lines = [f"日期：{summary.as_of}  运行状态：{summary.status}",
             f"运行编号：{summary.run_id}"]
    if offline:
        lines.append("【离线演示：全部数据为合成数据，不可用于投资】")
    else:
        lines.append("注意：请核验数据来源、汇率、市值及公告日；默认参考数据为合成示例。")
    for snap in summary.snapshots:
        lines.append(f"\n{snap.symbol}  状态：{snap.status}  财报期：{snap.period_end or '无'}")
        if snap.fcf:
            lines.append(f"标准FCF：{snap.fcf.standard_fcf:,.2f} {snap.fcf.currency}")
        for name, label in (("fcf_yield", "FCF收益率"), ("recurring_net_profit", "经常性净利润")):
            value = snap.factor(name)
            if value is not None:
                lines.append(f"{label}：{value:.2%}" if name == "fcf_yield" else f"{label}：{value:,.2f}")
    alerts = [e for e in summary.events if e.severity in (Severity.ERROR, Severity.WARN)]
    lines.append(f"\n质量告警：{len(alerts)} 条（详情见运行报告）")
    # Only include structured names; vendor exception details may contain credentials.
    for event in alerts[:5]:
        lines.append(f"{event.severity.value} {event.symbol} {event.check}")
    if len(alerts) > 5:
        lines.append(f"另有 {len(alerts) - 5} 条告警")
    return "\n".join(lines)


def send_pushplus(token: str, title: str, content: str, *, timeout: int = 15) -> None:
    token = token.strip()
    if not token:
        raise PushPlusError("未配置 QP_PUSHPLUS_TOKEN")
    payload = json.dumps({"token": token, "title": title, "content": content, "template": "txt"},
                         ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(PUSHPLUS_URL, data=payload,
                                     headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
        if not isinstance(body, dict) or body.get("code") != 200:
            raise PushPlusError("PushPlus 未接受消息，请检查服务状态和配置")
    except (urllib.error.URLError, OSError, ValueError, UnicodeError) as exc:
        raise PushPlusError("PushPlus 请求失败，请检查网络或服务状态") from exc
