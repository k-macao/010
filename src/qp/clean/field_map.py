"""Canonical field dictionary + a defensive matcher.

Why not ``next(col for col in df.columns if '固定资产' in col)``?
On a real HK cash-flow statement that matches the balance-sheet *stock* item
("固定资产 / 物业厂房及设备") as easily as the capex *flow* item, and silently
produces FCF = OCF - total fixed assets. Matching is therefore:

1. statement-scoped (CF fields can never be produced by a BS row),
2. exact-alias first, then prioritized regex,
3. guarded by negative keywords (折旧/处置/期末/账面...),
4. ambiguity-aware (ties are rejected, not guessed),
5. code-aware (AkShare STD_ITEM_CODE, yfinance English labels).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from ..domain import (
    CAPEX,
    CASH_AND_EQUIVALENTS,
    DISPOSAL_GAIN,
    FAIR_VALUE_GAIN,
    FX_GAIN_LOSS,
    GOVERNMENT_GRANT,
    IMPAIRMENT_LOSS,
    INVESTMENT_PROPERTY,
    LITIGATION_SETTLEMENT,
    NET_PROFIT,
    NET_PROFIT_ATTRIBUTABLE,
    OCF,
    OTHER_NONRECURRING_GAIN,
    OTHER_NONRECURRING_LOSS,
    PPE,
    REVENUE,
    TOTAL_ASSETS,
    TOTAL_DEBT,
    TOTAL_EQUITY,
    StatementType,
)
from ..util import normalize_label


@dataclass(frozen=True)
class FieldSpec:
    field: str
    statement: StatementType
    aliases: tuple[str, ...] = ()
    patterns: tuple[str, ...] = ()
    excludes: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()   # at least one must be present (action words for flows)
    sign: str = "flow"               # flow | as_gain | as_loss | stock
    aggregation: str = "first"       # first | sum
    priority: int = 0                # higher wins on multiple matches


def _specs() -> tuple[FieldSpec, ...]:
    CF, IS, BS = StatementType.CF, StatementType.IS, StatementType.BS
    return (
        # ------------------------------- cash flow -------------------------------
        FieldSpec(OCF, CF,
            aliases=(
                "经营活动产生的现金流量净额", "经营活动现金流量净额", "经营业务所得现金净额",
                "经营业务所得之现金净额", "经营业务现金净额", "营运活动产生的现金流量净额",
                "netcashfromoperatingactivities", "operatingcashflow", "netcashprovidedbyusedinoperatingactivities",
                "cashgeneratedfromoperations", "cashflowsfromoperatingactivities",
            ),
            patterns=(r"经营活动.*现金流量.*净额", r"经营业务.*现金.*净额", r"经营.*现金.*净额",
                      r"operating.*cash.*(net|flow)",),
            excludes=("每股", "增长率", "同比", "pershare", "期末余额"),
            priority=100),
        FieldSpec(CAPEX, CF,
            aliases=(
                "购建固定资产无形资产和其他长期资产支付的现金",
                "购建固定资产无形资产及其他长期资产所支付的现金",
                "购建物业厂房及设备所支付的现金", "购买物业厂房及设备",
                "购置物业厂房及设备", "添置物业厂房及设备", "资本开支", "资本性支出",
                "capitalexpenditure", "capitalexpenditures", "paymentstoacquirepropertyplantandequipment",
                "paymentsforcapitalexpenditures", "purchaseofpropertyplantandequipment",
                "additionspropertyplantandequipment", "purchasesofpropertyplantandequipment",
            ),
            patterns=(
                r"(购建|购置|购买|添置).*(固定资产|物业|厂房|设备|无形资产|长期资产).*(支付|现金|开支)",
                r"(购建|购买|购置|添置).*(固定资产|物业厂房)",
                r"capitalexpenditure",
                r"paymentstoacquirepropertyplantandequipment",
            ),
            excludes=("折旧", "摊销", "处置", "出售", "收回", "余额", "期末", "净值", "账面",
                      "原值", "减值准备", "depreciation", "proceeds", "disposal", "saleof"),
            requires=("购建", "购置", "购买", "添置", "支付", "资本开支", "资本性支出",
                      "capitalexpend", "paymentstoacquire", "additions", "purchasesof"),
            priority=95),
        # ------------------------------- income ----------------------------------
        FieldSpec(REVENUE, IS,
            aliases=("营业收入", "营业额", "营运收入", "收入", "收益总额", "totalrevenue",
                     "revenue", "sales", "turnover"),
            patterns=(r"^(营业收入|营业额|营运收入|收入)$", r"^(total)?revenue$", r"^turnover$"),
            excludes=("每股", "增长", "同比", "其他", "成本", "other", "cost"),
            priority=60),
        FieldSpec(NET_PROFIT_ATTRIBUTABLE, IS,
            aliases=("股东应占溢利", "归属于母公司股东的净利润", "归属于本公司股东的净利润",
                     "本公司股东应占溢利", "归属于母公司所有者的净利润", "归母净利润",
                     "netincomeattributable", "netprofitattributabletoowners",
                     "profitattributabletoshareholders"),
            patterns=(r"股东应占.*(溢利|利润)", r"归属.*母公司.*净利润", r"归母净利润",
                      r"attributableto(owners|shareholders|parent)"),
            priority=90),
        FieldSpec(NET_PROFIT, IS,
            aliases=("除税后溢利", "净利润", "年度溢利", "年内溢利", "本期溢利", "纯利",
                     "netincome", "netprofit", "profitfortheyear", "profitloss",
                     "profitfortheperiod"),
            patterns=(r"除税后.*(溢利|利润)", r"^(净利润|纯利|年度溢利|年内溢利)$",
                      r"^net(income|profit)$", r"^profit(for|loss)"),
            excludes=("每股", "股东应占", "归属", "增长率", "同比", "pershare", "attributable"),
            priority=80),
        FieldSpec(FAIR_VALUE_GAIN, IS,
            aliases=("投资物业公允价值变动收益", "投资物业公平值变动", "公允价值变动收益",
                     "公平值变动收益", "金融资产公允价值变动", "fairvaluegain",
                     "changesinfairvalue", "fairvaluechangeoninvestmentproperties"),
            patterns=(r"(公允价值|公平值).*(变动|收益|损益)", r"投资物业.*(公允|公平).*变",
                      r"fairvalue.*(change|gain)",),
            excludes=("负债", "亏损", "loss"),
            sign="as_gain", aggregation="sum", priority=70),
        FieldSpec(IMPAIRMENT_LOSS, IS,
            aliases=("资产减值损失", "资产减值亏损", "减值亏损", "商誉减值", "金融资产减值损失",
                     "impairmentloss", "impairmentofassets", "impairmentcharges"),
            patterns=(r".*减值.*(损失|亏损|拨备|费用)", r"impairment"),
            excludes=("拨回", "转回", "reversal"),
            sign="as_loss", aggregation="sum", priority=70),
        FieldSpec(GOVERNMENT_GRANT, IS,
            aliases=("政府补助", "政府补贴", "政府资助", "governmentgrants", "governmentsubsidies"),
            patterns=(r"政府.*(补助|补贴|资助)", r"government(grant|subsid)"),
            sign="as_gain", aggregation="sum", priority=70),
        FieldSpec(FX_GAIN_LOSS, IS,
            aliases=("汇兑损益", "汇兑收益", "汇兑差额", "外汇收益", "foreignexchangegain",
                     "exchangegains", "netforeignexchangegain"),
            patterns=(r"(汇兑|外汇).*(损益|收益|亏损|差额)", r"(foreignexchange|exchangegain)"),
            sign="as_gain", aggregation="sum", priority=65),
        FieldSpec(DISPOSAL_GAIN, IS,
            aliases=("处置子公司收益", "出售附属公司收益", "出售资产收益", "处置长期资产收益",
                     "gainondisposal", "gainonsaleofassets", "gainonsaleofsubsidiaries"),
            patterns=(r"(处置|出售).*(子公司|附属公司|资产).*(收益|损益)", r"gainon(disposal|sale)"),
            sign="as_gain", aggregation="sum", priority=65),
        FieldSpec(LITIGATION_SETTLEMENT, IS,
            aliases=("诉讼赔偿", "诉讼拨备", "法律诉讼赔偿", "litigationsettlement", "legalprovision"),
            patterns=(r"诉讼.*(赔偿|拨备|损失)", r"litigation(settlement|provision)"),
            sign="as_loss", aggregation="sum", priority=65),
        FieldSpec(OTHER_NONRECURRING_GAIN, IS,
            aliases=("其他收益净额", "其他收入", "其他收益", "othergainsnet", "otherincome",
                     "otheroperatingincome"),
            patterns=(r"^(其他收益|其他收入|其他收益净额)$", r"^other(gains|income)"),
            sign="as_gain", aggregation="sum", priority=40),
        FieldSpec(OTHER_NONRECURRING_LOSS, IS,
            aliases=("其他亏损", "其他损失", "otherexpenses", "otherlosses"),
            patterns=(r"^其他(亏损|损失)", r"^other(losses|expenses)"),
            sign="as_loss", aggregation="sum", priority=40),
        # ------------------------------- balance sheet --------------------------
        FieldSpec(CASH_AND_EQUIVALENTS, BS,
            aliases=("现金及现金等价物", "现金及现金等值项目", "期末现金及现金等价物余额",
                     "cashandcashequivalents", "cashcashequivalentsandshortterminvestments"),
            patterns=(r"现金及现金(等价物|等值)", r"cashandcashequivalent"),
            sign="stock", priority=60),
        FieldSpec(TOTAL_DEBT, BS,
            aliases=("总负债", "负债总额", "totalliabilities", "totaldebt"),
            patterns=(r"^(总负债|负债总额)$", r"^total(liabilities|debt)$"),
            sign="stock", priority=60),
        FieldSpec(TOTAL_ASSETS, BS,
            aliases=("总资产", "资产总额", "totalassets"),
            patterns=(r"^(总资产|资产总额)$", r"^totalassets$"),
            sign="stock", priority=60),
        FieldSpec(TOTAL_EQUITY, BS,
            aliases=("总权益", "权益总额", "股东权益合计", "totalequity", "totalshareholdersequity"),
            patterns=(r"^(总权益|权益总额|股东权益合计)$", r"^total(equity|shareholdersequity)$"),
            sign="stock", priority=60),
        FieldSpec(INVESTMENT_PROPERTY, BS,
            aliases=("投资物业", "投资性房地产", "investmentproperties", "investmentproperty"),
            patterns=(r"^投资(物业|性房地产)$", r"^investmentpropert"),
            excludes=("变动", "公平值", "公允"),
            sign="stock", priority=70),
        FieldSpec(PPE, BS,
            aliases=("物业厂房及设备", "固定资产", "物业厂房设备", "propertyplantandequipment",
                     "fixedassets", "ppe"),
            patterns=(r"^(物业厂房及设备|固定资产|物业厂房设备)$", r"^(propertyplantandequipment|fixedassets|ppe)$"),
            excludes=("购建", "购置", "折旧", "处置", "支付", "资本"),
            sign="stock", priority=50),
    )


FIELD_SPECS: tuple[FieldSpec, ...] = _specs()

# A few AkShare STD_ITEM_CODEs are stable across ORG_TYPE templates; codes win over labels.
ITEM_CODE_MAP: dict[str, str] = {
    "004012999": NET_PROFIT,          # 除税后溢利
    "004025002": NET_PROFIT_ATTRIBUTABLE,  # 股东应占溢利
    "004001999": REVENUE,             # 营运收入
    "004001001": REVENUE,             # 营业额
    "004010999": "OPERATING_PROFIT",
    "004001002": PPE,                 # 物业厂房及设备
    "004001003": INVESTMENT_PROPERTY,
}


@dataclass(frozen=True)
class MatchResult:
    spec: FieldSpec
    matched_alias: str
    ambiguous: bool = False

    @property
    def field(self) -> str:
        return self.spec.field


class FieldMatcher:
    """Statement-scoped, alias-first, guarded matcher."""

    def __init__(self, specs: Iterable[FieldSpec] = FIELD_SPECS, extra_aliases: dict[str, tuple[str, ...]] | None = None):
        self.specs: tuple[FieldSpec, ...] = tuple(specs)
        self._exact: dict[tuple[StatementType, str], list[tuple[FieldSpec, str]]] = {}
        self._compiled: list[tuple[FieldSpec, re.Pattern[str], str]] = []
        for spec in self.specs:
            aliases = list(spec.aliases)
            if extra_aliases and spec.field in extra_aliases:
                aliases.extend(extra_aliases[spec.field])
            for alias in aliases:
                self._exact.setdefault((spec.statement, normalize_label(alias)), []).append((spec, alias))
            for pattern in spec.patterns:
                self._compiled.append((spec, re.compile(pattern), pattern))
        self._compiled.sort(key=lambda item: -item[0].priority)

    def match_by_code(self, code: str | None, statement: StatementType) -> MatchResult | None:
        if not code:
            return None
        field = ITEM_CODE_MAP.get(str(code).strip())
        if not field:
            return None
        for spec in self.specs:
            if spec.field == field and spec.statement == statement:
                return MatchResult(spec, f"code:{code}")
        return None

    def match(self, label: str, statement: StatementType) -> MatchResult | None:
        key = normalize_label(label)
        if not key:
            return None
        candidates: list[tuple[int, FieldSpec, str]] = []
        for spec, alias in self._exact.get((statement, key), []):
            candidates.append((1000 + spec.priority, spec, alias))
        for spec, pattern, source in self._compiled:
            if spec.statement != statement or not pattern.search(key):
                continue
            if any(normalize_label(x) in key for x in spec.excludes):
                continue
            if spec.requires and not any(normalize_label(x) in key for x in spec.requires):
                continue
            candidates.append((500 + spec.priority, spec, source))
        if not candidates:
            return None
        candidates.sort(key=lambda item: -item[0])
        best_score, best, alias = candidates[0]
        fields = {item[1].field for item in candidates if item[0] == best_score}
        return MatchResult(best, alias, ambiguous=len(fields) > 1)

    def match_any(self, label: str) -> MatchResult | None:
        for statement in StatementType:
            hit = self.match(label, statement)
            if hit:
                return hit
        return None
