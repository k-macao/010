"""Stage 2: standardized Free Cash Flow, computed, never taken from a vendor.

Exposes two interfaces:

* :func:`calculate_standard_fcf` — the DataFrame convenience function from the
  design sketch, hardened (guarded matcher, flexible period column, sign policy).
* :class:`FCFEngine` — the production path operating on aligned, PIT-filtered,
  currency-normalized facts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from ..domain import (
    CAPEX,
    OCF,
    AlignedReport,
    CapexSignPolicy,
    FCFResult,
    NonRecurringItem,
    StatementType,
)
from ..errors import MissingFieldError
from ..logging_utils import get_logger
from ..util import parse_number, to_date
from .field_map import FieldMatcher

logger = get_logger(__name__)

_PERIOD_CANDIDATES = ("报告期", "REPORT_DATE", "report_date", "period_end", "报告日期", "date", "Date")


def _period_series(df: pd.DataFrame) -> tuple[str, pd.Series]:
    for col in _PERIOD_CANDIDATES:
        if col in df.columns:
            return str(col), df[col]
    if df.index.name in _PERIOD_CANDIDATES:
        return str(df.index.name), pd.Series(df.index, index=range(len(df)))
    return "报告期", pd.Series(range(1, len(df) + 1), index=df.index)


def find_columns(df: pd.DataFrame, matcher: FieldMatcher | None = None) -> tuple[str, str]:
    matcher = matcher or FieldMatcher()
    ocf_col = capex_col = None
    for col in df.columns:
        hit = matcher.match(str(col), StatementType.CF)
        if hit is None or hit.ambiguous:
            continue
        if hit.field == OCF and ocf_col is None:
            ocf_col = str(col)
        elif hit.field == CAPEX and capex_col is None:
            capex_col = str(col)
    if not ocf_col or not capex_col:
        missing = []
        if not ocf_col:
            missing.append("OCF (经营活动产生的现金流量净额)")
        if not capex_col:
            missing.append("CAPEX (购建固定资产…支付的现金)")
        raise ValueError(
            f"未能在现金流量表中匹配到关键字段: {', '.join(missing)}；"
            f"实际表头={list(df.columns)}。请检查 field_map.py 的别名字典。"
        )
    return ocf_col, capex_col


def calculate_standard_fcf(
    cash_flow_df: pd.DataFrame,
    capex_sign_policy: str = "auto",
    matcher: FieldMatcher | None = None,
) -> pd.DataFrame:
    """用户草稿中的便捷函数（已加固）。

    ``df['Standard_FCF'] = OCF - |CapEx|``，支持中英文列名、报告期列为索引、
    capex 正负两种口径。请勿用 ``'固定资产' in col`` 找 capex——会命中资产负债表的
    固定资产存量科目（本模块由 FieldMatcher 的 requires/excludes 规则挡住）。
    """
    df = cash_flow_df.copy()
    ocf_col, capex_col = find_columns(df, matcher)
    period_name, periods = _period_series(df)
    ocf = df[ocf_col].map(parse_number).astype(float)
    capex = df[capex_col].map(parse_number).astype(float)
    policy = CapexSignPolicy(capex_sign_policy)
    if policy in {CapexSignPolicy.POSITIVE_OUTFLOW, CapexSignPolicy.NEGATIVE_OUTFLOW}:
        expected_negative = policy is CapexSignPolicy.NEGATIVE_OUTFLOW
        bad = capex[(capex > 0) if expected_negative else (capex < 0)]
        if not bad.empty:
            logger.warning("capex sign mismatch", extra={"context": {
                "policy": policy.value, "rows": len(bad),
            }})
    if policy is CapexSignPolicy.SIGNED:
        fcf = ocf + capex          # capex expected negative (cash outflow)
    else:
        fcf = ocf - capex.abs()    # works for both positive-outflow and negative-outflow vendors
    out = pd.DataFrame({
        period_name: periods.values,
        ocf_col: ocf.values,
        capex_col: capex.values,
        "CapEx_Magnitude": capex.abs().values,
        "Standard_FCF": fcf.values,
    })
    out["CapEx_Sign_Policy"] = policy.value
    return out


@dataclass
class FCFEngine:
    capex_sign_policy: str = "auto"
    formula_version: str = "fcf-v1"

    def compute(
        self,
        aligned: AlignedReport,
        items: list[NonRecurringItem] | None = None,
    ) -> FCFResult:
        ocf_fact = aligned.facts.get(OCF)
        capex_fact = aligned.facts.get(CAPEX)
        if ocf_fact is None or ocf_fact.value is None:
            raise MissingFieldError("OCF missing from aligned facts")
        if capex_fact is None or capex_fact.value is None:
            raise MissingFieldError("CAPEX missing from aligned facts (quarantined?)")
        ocf, capex_raw = float(ocf_fact.value), float(capex_fact.value)
        ocf_native = float(ocf_fact.value_native if ocf_fact.value_native is not None else ocf)
        capex_native = float(capex_fact.value_native if capex_fact.value_native is not None else capex_raw)
        policy = CapexSignPolicy(self.capex_sign_policy)
        quality: list[str] = []

        if policy in {CapexSignPolicy.POSITIVE_OUTFLOW, CapexSignPolicy.NEGATIVE_OUTFLOW}:
            expected_negative = policy is CapexSignPolicy.NEGATIVE_OUTFLOW
            if (expected_negative and capex_raw > 0) or (not expected_negative and capex_raw < 0):
                quality.append("capex_sign_mismatch")
                logger.warning("capex sign mismatch", extra={"context": {
                    "symbol": aligned.symbol, "period_end": aligned.period_end.isoformat(),
                    "policy": policy.value, "capex_raw": capex_raw,
                }})
        capex = abs(capex_raw)
        if policy is CapexSignPolicy.SIGNED:
            standard_fcf = ocf + capex_raw
            if capex_raw > 0:
                quality.append("signed_policy_positive_capex")
                standard_fcf = ocf - capex_raw
        else:
            standard_fcf = ocf - capex
        standard_fcf_native = ocf_native - abs(capex_native)

        if ocf < 0:
            quality.append("negative_ocf")
        if abs(ocf) > 0 and capex > abs(ocf) * 10:
            quality.append("capex_ocf_ratio_implausible")
        ocf_quality = ",".join(ocf_fact.quality or ())
        if "fx_missing" in ocf_quality:
            quality.append("fx_missing_in_normalization")
        elif "fx_fallback" in ocf_quality:
            quality.append("fx_fallback_in_normalization")

        oneoff_cash = sum(
            float(item.profit_impact) for item in (items or []) if item.cash_impact
        )
        return FCFResult(
            symbol=aligned.symbol,
            period_end=to_date(aligned.period_end),
            currency=aligned.currency,
            ocf=ocf, capex_raw=capex_raw, capex=capex,
            standard_fcf=standard_fcf,
            standard_fcf_native=standard_fcf_native,
            fx_rate=ocf_fact.fx_rate,
            fx_as_of=ocf_fact.fx_as_of,
            adjusted_fcf=standard_fcf - oneoff_cash,
            oneoff_cash_impact=oneoff_cash,
            policy=policy.value,
            quality=quality,
        )


def fcf_dataframe(result: FCFResult, extra: dict[str, Any] | None = None) -> pd.DataFrame:
    """Handy for notebooks/ClickHouse export."""
    row = result.as_dict()
    row.pop("quality", None)
    row.update(extra or {})
    return pd.DataFrame([row])
