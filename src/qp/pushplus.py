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

# Fixed diagnostics only: never log the response text, which can contain secrets.
BUSINESS_ERRORS = {
    302: "未登录",
    401: "请求未授权，请检查开放接口是否启用",
    403: "请求 IP 未授权，请检查 IP 白名单",
    500: "PushPlus 服务异常",
    600: "数据异常",
    805: "无权查看",
    888: "积分不足",
    900: "账号受限或请求次数过多，请勿继续重试",
    903: "用户令牌无效，请核对 PUSHPLUS_TOKEN",
    905: "账号未实名认证",
    999: "服务端验证错误，请在 PushPlus 后台检查账号、渠道和消息配置",
}


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
        lines.append("注意：免费行情可能延迟，次日参考快照不保证历史 PIT；请核验汇率、市值及公告日。")
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
    except urllib.error.HTTPError as exc:
        # HTTP status is separate from the PushPlus JSON business code.
        status = exc.code if type(exc.code) is int and 100 <= exc.code <= 599 else None
        detail = f"（HTTP {status}）" if status is not None else ""
        raise PushPlusError(f"PushPlus HTTP 请求失败{detail}") from None
    except (urllib.error.URLError, OSError, ValueError, UnicodeError):
        # Do not chain the raw error: driver/network errors may contain credentials.
        raise PushPlusError("PushPlus 请求失败或响应无效，请检查网络和服务状态") from None

    code = body.get("code") if isinstance(body, dict) else None
    if type(code) is int and code == 200:
        return
    if type(code) is int and code in BUSINESS_ERRORS:
        detail = BUSINESS_ERRORS[code]
        # A known validation message can help diagnose the WeChat subscription.
        # Equality against a constant avoids printing arbitrary server responses.
        if code == 999 and isinstance(body, dict) and any(
            body.get(key) == "接收用户未关注公众号" for key in ("msg", "data")
        ):
            detail = "接收用户未关注公众号，请关注并绑定 PushPlus 公众号"
        raise PushPlusError(f"PushPlus 未接受消息（业务码 {code}）：{detail}")
    raise PushPlusError("PushPlus 未接受消息（未知业务码或响应格式；未输出响应内容）")
