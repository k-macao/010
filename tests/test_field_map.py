from __future__ import annotations

import pytest

from qp.clean.field_map import ITEM_CODE_MAP, FieldMatcher
from qp.domain import CAPEX, NET_PROFIT, OCF, PPE, REVENUE, TOTAL_DEBT, TOTAL_LIABILITIES, StatementType


@pytest.fixture(scope="module")
def matcher() -> FieldMatcher:
    return FieldMatcher()


@pytest.mark.parametrize("label", [
    "经营活动产生的现金流量净额",
    "經營業務所得現金淨額",           # traditional Chinese HK wording
    "Net Cash From Operating Activities",
    "OperatingCashFlow",
])
def test_ocf_aliases(matcher, label):
    hit = matcher.match(label, StatementType.CF)
    assert hit is not None and hit.field == OCF


@pytest.mark.parametrize("label", [
    "购建固定资产、无形资产和其他长期资产支付的现金",
    "購置物業、廠房及設備",           # traditional
    "Capital Expenditure",
    "Payments to Acquire Property Plant and Equipment",
])
def test_capex_aliases(matcher, label):
    hit = matcher.match(label, StatementType.CF)
    assert hit is not None and hit.field == CAPEX


def test_bare_fixed_assets_is_not_capex(matcher):
    """The classic bug: '固定资产' is a balance-sheet STOCK, not a cash flow."""
    assert matcher.match("固定资产", StatementType.CF) is None
    bs_hit = matcher.match("固定资产", StatementType.BS)
    assert bs_hit is not None and bs_hit.field == PPE


def test_capex_negative_guards(matcher):
    assert matcher.match("固定资产折旧", StatementType.CF) is None
    assert matcher.match("处置固定资产收回的现金", StatementType.CF) is None
    assert matcher.match("固定资产期末余额", StatementType.CF) is None


def test_statement_scope(matcher):
    assert matcher.match("经营活动产生的现金流量净额", StatementType.IS) is None
    assert matcher.match("营业收入", StatementType.IS).field == REVENUE
    assert matcher.match("营业收入", StatementType.CF) is None


def test_total_liabilities_are_not_mapped_as_total_debt(matcher):
    assert matcher.match("总负债", StatementType.BS).field == TOTAL_LIABILITIES
    assert matcher.match("Total Liabilities Net Minority Interest", StatementType.BS).field == TOTAL_LIABILITIES
    assert matcher.match("Total Debt", StatementType.BS).field == TOTAL_DEBT
    assert matcher.match("有息负债", StatementType.BS).field == TOTAL_DEBT


def test_loss_sign_and_aggregation(matcher):
    hit = matcher.match("资产减值损失", StatementType.IS)
    assert hit.spec.sign == "as_loss"
    assert hit.spec.aggregation == "sum"


def test_item_code_wins(matcher):
    hit = matcher.match_by_code("004012999", StatementType.IS)
    assert hit is not None and hit.field == NET_PROFIT
    assert ITEM_CODE_MAP["004025002"] == "NET_PROFIT_ATTRIBUTABLE"
    assert matcher.match_by_code("unknown", StatementType.IS) is None


def test_matcher_is_case_and_punctuation_insensitive(matcher):
    assert matcher.match("  Capital   Expenditure  ", StatementType.CF).field == CAPEX
