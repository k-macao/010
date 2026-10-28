"""Stage 3: hard rules (income-statement fields) + LLM/PDF note items.

Hard rules are the base layer and always win on duplicates. Note-derived items
(LLM or regex) add detail that never appears as a standalone income-statement
line. Dedupe is by (category, amount) within a tolerance, so a note that merely
restates an existing rule item cannot double-count the adjustment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterable

from ...domain import (
    IMPAIRMENT_LOSS,
    LITIGATION_SETTLEMENT,
    NET_PROFIT,
    NET_PROFIT_ATTRIBUTABLE,
    NONRECURRING_FIELDS,
    AlignedReport,
    NonRecurringItem,
)
from ...ingest.fx import FxRateProvider
from ...util import rel_diff, to_date, to_datetime
from .models import NoteItemDraft, ParsedNotes

_FIELD_LABELS = {
    "FAIR_VALUE_GAIN": "投资物业/金融资产公允价值变动收益",
    "IMPAIRMENT_LOSS": "资产减值损失",
    "GOVERNMENT_GRANT": "政府补助",
    "FX_GAIN_LOSS": "汇兑损益",
    "DISPOSAL_GAIN": "处置/出售资产或子公司收益",
    "LITIGATION_SETTLEMENT": "诉讼赔偿/法律拨备",
    "OTHER_NONRECURRING_GAIN": "其他一次性收益",
    "OTHER_NONRECURRING_LOSS": "其他一次性亏损",
}


def rules_from_aligned(
    aligned: AlignedReport,
    published_at: datetime | None = None,
    available_at: datetime | None = None,
) -> list[NonRecurringItem]:
    """Deterministic extraction of non-recurring P&L fields from the income statement."""
    items: list[NonRecurringItem] = []
    for field_name in sorted(NONRECURRING_FIELDS):
        fact = aligned.facts.get(field_name)
        if fact is None or fact.value is None:
            continue
        value = float(fact.value)
        # as_loss fields are stored as magnitudes; as_gain fields keep their sign.
        if field_name in {IMPAIRMENT_LOSS, LITIGATION_SETTLEMENT}:
            profit_impact = -abs(value)
        else:
            profit_impact = value
        amount = abs(profit_impact)
        if amount < 1e-9:
            continue
        items.append(NonRecurringItem(
            symbol=aligned.symbol,
            period_end=to_date(aligned.period_end),
            category=field_name,
            label=_FIELD_LABELS.get(field_name, field_name),
            amount=amount,
            profit_impact=profit_impact,
            direction="gain" if profit_impact >= 0 else "loss",
            source_kind="rule",
            evidence=f"利润表硬规则字段 {field_name}（source={','.join(fact.sources)}）",
            confidence=0.95,
            currency=fact.currency,
            published_at=to_datetime(published_at or fact.published_at),
            available_at=to_datetime(available_at or fact.available_at),
        ))
    return items


def drafts_to_items(
    drafts: Iterable[NoteItemDraft],
    symbol: str,
    period_end: date,
    fx: FxRateProvider,
    target_currency: str,
    published_at: datetime,
    available_at: datetime,
) -> tuple[list[NonRecurringItem], list[str]]:
    warnings: list[str] = []
    items: list[NonRecurringItem] = []
    for draft in drafts:
        try:
            quote = fx.rate(draft.currency, period_end, base=target_currency)
        except Exception as exc:
            warnings.append(f"note item {draft.label!r} dropped: FX {draft.currency}->"
                            f"{target_currency} unavailable ({exc})")
            continue
        amount = draft.amount * draft.unit_scale * quote.rate
        profit_impact = amount if draft.direction == "gain" else -amount
        items.append(NonRecurringItem(
            symbol=symbol,
            period_end=to_date(period_end),
            category=draft.category,
            label=draft.label,
            amount=amount,
            profit_impact=profit_impact,
            direction=draft.direction,
            source_kind=draft.source_kind,
            evidence=draft.evidence,
            confidence=draft.confidence,
            currency=target_currency,
            published_at=to_datetime(published_at),
            available_at=to_datetime(available_at),
            cash_impact=draft.cash_impact,
            page=draft.page,
            raw_amount=draft.amount,
            raw_currency=draft.currency,
        ))
        if quote.is_fallback:
            warnings.append(f"note item {draft.label!r} used fallback FX rate")
    return items, warnings


@dataclass
class NonRecurringResult:
    items: list[NonRecurringItem]
    recurring_net_profit: float | None
    oneoff_profit_impact: float
    nonrecurring_ratio: float | None
    warnings: list[str] = field(default_factory=list)

    @property
    def cash_items(self) -> list[NonRecurringItem]:
        return [i for i in self.items if i.cash_impact]


class NonRecurringEngine:
    def __init__(self, fx: FxRateProvider, target_currency: str = "HKD", dedupe_tolerance: float = 0.02) -> None:
        self.fx = fx
        self.target_currency = target_currency.upper()
        self.dedupe_tolerance = dedupe_tolerance

    def merge(
        self,
        rule_items: list[NonRecurringItem],
        note_items: list[NonRecurringItem],
    ) -> tuple[list[NonRecurringItem], list[str]]:
        merged = list(rule_items)
        warnings: list[str] = []
        for note in note_items:
            match = next((
                rule for rule in merged
                if rule.category == note.category
                and rule.amount > 0 and note.amount > 0
                and rel_diff(rule.amount, note.amount) <= self.dedupe_tolerance
            ), None)
            if match is not None:
                match.corroborated = True
                continue
            same_category = any(rule.category == note.category for rule in merged)
            if same_category:
                warnings.append(
                    f"note item {note.label!r} ({note.category}) differs from rule items; kept both — review"
                )
            merged.append(note)
        return merged, warnings

    def extract(
        self,
        aligned: AlignedReport,
        parsed: ParsedNotes,
        published_at: datetime | None = None,
        available_at: datetime | None = None,
        prebuilt_note_items: list[NonRecurringItem] | None = None,
    ) -> NonRecurringResult:
        rule_items = rules_from_aligned(aligned, published_at, available_at)
        note_items, fx_warnings = (
            (list(prebuilt_note_items), []) if prebuilt_note_items is not None else
            drafts_to_items(
                parsed.items, aligned.symbol, to_date(aligned.period_end), self.fx,
                self.target_currency,
                to_datetime(published_at or min((f.published_at for f in aligned.facts.values()), default=aligned.as_of)),
                to_datetime(available_at or min((f.available_at for f in aligned.facts.values()), default=aligned.as_of)),
            )
        )
        items, merge_warnings = self.merge(rule_items, note_items)
        oneoff = sum(i.profit_impact for i in items)
        net_profit = aligned.get(NET_PROFIT_ATTRIBUTABLE) or aligned.get(NET_PROFIT)
        recurring = None if net_profit is None else float(net_profit) - oneoff
        ratio = None if not net_profit else abs(oneoff) / abs(float(net_profit))
        return NonRecurringResult(
            items=items, recurring_net_profit=recurring, oneoff_profit_impact=oneoff,
            nonrecurring_ratio=ratio, warnings=fx_warnings + merge_warnings + parsed.warnings,
        )
