"""Small, dependency-free helpers shared by every stage."""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from datetime import date, datetime, time, timezone
from typing import Any

import pandas as pd

# ----------------------------- text -----------------------------------------

# A deliberately small traditional→simplified table covering the vocabulary used in
# HK financial statements. Full OpenCC is optional; this is enough for field matching
# and avoids adding a heavy dependency to the ingestion hot path.
_TRAD_PAIRS: tuple[tuple[str, str], ...] = (
    ("經營業務", "经营业务"), ("經營活動", "经营活动"), ("經營", "经营"), ("業務", "业务"),
    ("現金及現金等價物", "现金及现金等价物"), ("現金", "现金"), ("淨額", "净额"),
    ("淨", "净"), ("額", "额"), ("購建", "购建"), ("購置", "购置"), ("購買", "购买"),
    ("購", "购"), ("設備", "设备"), ("廠房", "厂房"), ("廠", "厂"), ("物業", "物业"),
    ("投資", "投资"), ("公允價值", "公允价值"), ("價值", "价值"), ("價", "价"),
    ("變動", "变动"), ("變", "变"), ("動", "动"), ("匯兌", "汇兑"), ("兌", "兑"),
    ("匯", "汇"), ("虧損", "亏损"), ("虧", "亏"), ("損", "损"), ("補貼", "补贴"),
    ("補助", "补助"), ("補", "补"), ("貼", "贴"), ("差額", "差额"), ("資產", "资产"),
    ("資", "资"), ("產", "产"), ("減值", "减值"), ("減", "减"), ("處置", "处置"),
    ("處", "处"), ("當期", "当期"), ("當", "当"), ("確認", "确认"), ("確", "确"),
    ("認", "认"), ("賠償", "赔偿"), ("賠", "赔"), ("償", "偿"), ("訴訟", "诉讼"),
    ("訴", "诉"), ("訟", "讼"), ("附註", "附注"), ("註", "注"), ("賬", "账"),
    ("帳", "账"), ("萬", "万"), ("億", "亿"), ("幣", "币"), ("港幣", "港币"),
    ("人民幣", "人民币"), ("總", "总"), ("產生", "产生"), ("無形", "无形"),
    ("長期", "长期"), ("應佔", "应占"), ("應", "应"), ("佔", "占"),
    ("除稅後", "除税后"), ("稅", "税"), ("後", "后"), ("股東", "股东"), ("東", "东"),
    ("營運", "营运"), ("營業額", "营业额"), ("融資", "融资"), ("貸", "贷"),
    ("銀", "银"), ("於", "于"), ("與", "与"), ("內", "内"), ("數", "数"),
    ("項目", "项目"), ("項", "项"), ("附屬公司", "附属公司"), ("屬", "属"),
    ("其他收入", "其他收入"), ("為", "为"), ("計", "计"), ("億", "亿"),
    ("撥回", "拨回"), ("撥", "拨"), ("攤銷", "摊销"), ("攤", "摊"),
)

_PUNCT_RE = re.compile(r"[\s\-_/:：,，.。、()（）\[\]【】'\"|]+")


def fold_text(text: Any) -> str:
    """Length-preserving NFKC + traditional→simplified + lowercase.

    Keeps character offsets aligned with the original so keyword windows can be
    sliced out of the raw PDF text. ``normalize_label`` builds on this.
    """
    s = unicodedata.normalize("NFKC", str(text)).lower()
    for trad, simp in _TRAD_PAIRS:
        if trad in s:
            s = s.replace(trad, simp)
    return s


def normalize_label(label: Any) -> str:
    """NFKC + traditional→simplified + lowercase + strip punctuation.

    Used as the join key between vendor labels and our canonical field dictionary.
    """
    return _PUNCT_RE.sub("", fold_text(label).strip())


def number_tokens(text: str) -> set[str]:
    """Numeric literals present in a text, in a canonical form (for LLM grounding)."""
    out: set[str] = set()
    for raw in re.findall(r"-?\d[\d,\s]*(?:\.\d+)?", text.replace("\u00a0", " ")):
        cleaned = raw.replace(",", "").replace(" ", "")
        try:
            value = float(cleaned)
        except ValueError:
            continue
        out.add(f"{value:.6g}")
        out.add(f"{abs(value):.6g}")
    return out


# ----------------------------- dates ----------------------------------------


def to_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, pd.Timestamp):
        return value.date()
    return pd.to_datetime(str(value), errors="raise").date()


def to_datetime(value: Any) -> datetime:
    """All internal timestamps are naive UTC (SQLite has no TZ type)."""
    if isinstance(value, pd.Timestamp):
        dt = value.to_pydatetime()
    elif isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime.combine(value, time.min)
    else:
        dt = pd.to_datetime(str(value), errors="raise", utc=True).to_pydatetime()
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.replace(microsecond=0)


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def as_of_cutoff(as_of: date | datetime, lag_days: int = 0) -> datetime:
    """End of the as-of day (UTC) minus the disclosure lag."""
    day = to_date(as_of)
    from datetime import timedelta

    return datetime.combine(day, time(23, 59, 59)) - timedelta(days=lag_days)


# ----------------------------- misc -----------------------------------------


def parse_number(value: Any) -> float | None:
    """Vendor numbers arrive as '(1,234)', '—', '1 234', '1.2亿', etc."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if math.isfinite(float(value)) else None
    s = str(value).strip()
    if s in {"", "-", "—", "–", "N/A", "NA", "null", "None", "nan"}:
        return None
    negative = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace(",", "").replace(" ", "").replace("\u00a0", "")
    multiplier = 1.0
    for suffix, factor in (("万亿", 1e12), ("亿", 1e8), ("万", 1e4), ("千", 1e3), ("百", 1e2)):
        if s.endswith(suffix):
            multiplier, s = factor, s[: -len(suffix)]
            break
    try:
        out = float(s) * multiplier
    except ValueError:
        return None
    return -out if negative else out


def stable_hash(payload: Any) -> str:
    if isinstance(payload, pd.DataFrame):
        text = payload.to_json(orient="records", force_ascii=False, date_format="iso")
    elif isinstance(payload, (dict, list, tuple)):
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    else:
        text = str(payload)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def rel_diff(a: float, b: float) -> float:
    denom = max(abs(a), abs(b), 1.0)
    return abs(a - b) / denom
