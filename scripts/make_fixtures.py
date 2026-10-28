"""Regenerate the deterministic offline demo fixtures.

All numbers are SYNTHETIC (vaguely Tencent/Alibaba-like) and exist only to exercise
currency/unit normalization, restatement PIT, cross-source conflicts and LLM/regex
note parsing. Never use them for research.

Usage: python scripts/make_fixtures.py [output_dir]
"""

from __future__ import annotations

import csv
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("examples/fixtures")

COLUMNS = [
    "source", "statement", "period_end", "period_type", "published_at", "available_at",
    "currency", "unit_scale", "unit_label", "raw_label", "value",
]

PUB = {
    "2023-12-31": "2024-03-20T08:00:00",
    "2024-12-31": "2025-03-19T08:00:00",
    "2024-12-31-R": "2025-10-15T08:00:00",
    "2025-12-31": "2026-03-25T08:00:00",
}

# CNY thousands (unit_scale=1000): Eastmoney/AkShare-shaped synthetic statements.
AK_00700 = {
    "CF": {
        "2023-12-31": {
            "经营活动产生的现金流量净额": 175_000_000,
            "购建固定资产、无形资产和其他长期资产支付的现金": 25_000_000,
            "固定资产": 120_000_000,   # balance-sheet stock, must NEVER be used as capex
        },
        "2024-12-31": {
            "经营活动产生的现金流量净额": 200_000_000,
            "购建固定资产、无形资产和其他长期资产支付的现金": 30_000_000,
            "固定资产": 135_000_000,
        },
        "2024-12-31-R": {
            "经营活动产生的现金流量净额": 198_000_000,
            "购建固定资产、无形资产和其他长期资产支付的现金": 30_500_000,
            "固定资产": 135_000_000,
        },
        "2025-12-31": {
            "经营活动产生的现金流量净额": 240_000_000,
            "购建固定资产、无形资产和其他长期资产支付的现金": 35_000_000,
            "固定资产": 150_000_000,
        },
    },
    "IS": {
        "2023-12-31": {
            "营业收入": 609_000_000, "除税后溢利": 115_000_000, "股东应占溢利": 112_000_000,
            "投资物业公允价值变动收益": 6_000_000, "资产减值损失": 2_500_000,
            "政府补助": 1_000_000, "汇兑损益": -300_000, "处置子公司收益": 1_200_000,
        },
        "2024-12-31": {
            "营业收入": 620_000_000, "除税后溢利": 120_000_000, "股东应占溢利": 117_000_000,
            "投资物业公允价值变动收益": 7_000_000, "资产减值损失": 3_000_000,
            "政府补助": 1_200_000, "汇兑损益": -400_000, "处置子公司收益": 1_500_000,
        },
        "2024-12-31-R": {
            "营业收入": 620_000_000, "除税后溢利": 118_000_000, "股东应占溢利": 115_000_000,
            "投资物业公允价值变动收益": 7_000_000, "资产减值损失": 3_000_000,
            "政府补助": 1_200_000, "汇兑损益": -400_000, "处置子公司收益": 1_500_000,
        },
        "2025-12-31": {
            "营业收入": 660_000_000, "除税后溢利": 130_000_000, "股东应占溢利": 125_000_000,
            "投资物业公允价值变动收益": 8_000_000, "资产减值损失": 3_000_000,
            "政府补助": 1_500_000, "汇兑损益": -500_000, "处置子公司收益": 2_000_000,
        },
    },
    "BS": {
        "2023-12-31": {
            "现金及现金等价物": 170_000_000, "总负债": 280_000_000, "总资产": 1_600_000_000,
            "总权益": 900_000_000, "投资物业": 4_000_000, "物业厂房及设备": 120_000_000,
        },
        "2024-12-31": {
            "现金及现金等价物": 185_000_000, "总负债": 290_000_000, "总资产": 1_700_000_000,
            "总权益": 950_000_000, "投资物业": 4_500_000, "物业厂房及设备": 135_000_000,
        },
        "2025-12-31": {
            "现金及现金等价物": 200_000_000, "总负债": 300_000_000, "总资产": 1_800_000_000,
            "总权益": 1_000_000_000, "投资物业": 5_000_000, "物业厂房及设备": 150_000_000,
        },
    },
}

AK_09988 = {
    "CF": {
        "2025-03-31": {
            "经营活动产生的现金流量净额": 160_000_000,
            "购建固定资产、无形资产和其他长期资产支付的现金": 28_000_000,
        },
        "2026-03-31": {
            "经营活动产生的现金流量净额": 175_000_000,
            "购建固定资产、无形资产和其他长期资产支付的现金": 32_000_000,
        },
    },
    "IS": {
        "2025-03-31": {"营业收入": 940_000_000, "除税后溢利": 80_000_000, "股东应占溢利": 78_000_000,
                       "投资物业公允价值变动收益": 2_000_000},
        "2026-03-31": {"营业收入": 990_000_000, "除税后溢利": 90_000_000, "股东应占溢利": 88_000_000,
                       "投资物业公允价值变动收益": 2_500_000},
    },
    "BS": {
        "2025-03-31": {"现金及现金等价物": 180_000_000, "总负债": 400_000_000, "总资产": 1_900_000_000},
        "2026-03-31": {"现金及现金等价物": 190_000_000, "总负债": 420_000_000, "总资产": 2_000_000_000},
    },
}

# yfinance: raw CNY yuan, English labels, negative capex, slightly different OCF
# (3.3% off → exercises the cross-source conflict gate).
YF_00700 = {
    "CF": {
        "2024-12-31": {
            "Operating Cash Flow": 199_000_000_000,
            "Capital Expenditure": -30_000_000_000,
        },
        "2025-12-31": {
            "Operating Cash Flow": 232_000_000_000,
            "Capital Expenditure": -35_000_000_000,
        },
    },
    "IS": {
        "2025-12-31": {
            "Total Revenue": 660_000_000_000,
            "Net Income": 130_000_000_000,
        },
    },
}

PUB_09988 = {
    "2025-03-31": "2025-05-15T08:00:00",
    "2026-03-31": "2026-05-20T08:00:00",
}


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in COLUMNS})


def vendor_rows(symbol: str, source: str, statements: dict, publisher: dict[str, str],
                scale: float, unit_label: str, currency: str = "CNY") -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for statement, periods in statements.items():
        rows: list[dict] = []
        for period, items in periods.items():
            base = period.replace("-R", "")
            published = publisher.get(period, publisher.get(base, ""))
            period_end = base
            for label, value in items.items():
                rows.append({
                    "source": source, "statement": statement,
                    "period_end": period_end, "period_type": "ANNUAL",
                    "published_at": published, "available_at": published,
                    "currency": currency, "unit_scale": scale, "unit_label": unit_label,
                    "raw_label": label, "value": value,
                })
        out[statement] = rows
    return out


def main() -> None:
    root = ROOT
    root.mkdir(parents=True, exist_ok=True)

    for symbol, data, publisher in (("00700", AK_00700, PUB), ("09988", AK_09988, PUB_09988)):
        for statement, rows in vendor_rows(
            symbol, "akshare", data, publisher, 1000, "人民币千元"
        ).items():
            write_csv(root / "akshare" / f"{symbol}_{statement.lower()}.csv", rows)

    for statement, rows in vendor_rows(
        "00700", "yfinance", YF_00700, PUB, 1, "元(原币)"
    ).items():
        write_csv(root / "yfinance" / f"00700_{statement.lower()}.csv", rows)

    # HKEXnews XBRL instance document (primary source, 2025 annual report).
    xbrl = """<?xml version="1.0" encoding="UTF-8"?>
<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
            xmlns:ifrs-full="http://xbrl.ifrs.org/taxonomy/2023/ifrs-full"
            xmlns:iso4217="http://www.xbrl.org/2003/iso4217">
  <xbrli:context id="FY2025">
    <xbrli:entity><xbrli:identifier scheme="http://www.hkexnews.hk">00700</xbrli:identifier></xbrli:entity>
    <xbrli:period><xbrli:startDate>2025-01-01</xbrli:startDate><xbrli:endDate>2025-12-31</xbrli:endDate></xbrli:period>
  </xbrli:context>
  <xbrli:context id="FY2025I">
    <xbrli:entity><xbrli:identifier scheme="http://www.hkexnews.hk">00700</xbrli:identifier></xbrli:entity>
    <xbrli:period><xbrli:instant>2025-12-31</xbrli:instant></xbrli:period>
  </xbrli:context>
  <xbrli:unit id="CNY"><xbrli:measure>iso4217:CNY</xbrli:measure></xbrli:unit>
  <ifrs-full:NetCashFlowsFromUsedInOperatingActivities contextRef="FY2025" unitRef="CNY" scale="3" decimals="-3">240000000</ifrs-full:NetCashFlowsFromUsedInOperatingActivities>
  <ifrs-full:PaymentsToAcquirePropertyPlantAndEquipment contextRef="FY2025" unitRef="CNY" scale="3" decimals="-3">-35000000</ifrs-full:PaymentsToAcquirePropertyPlantAndEquipment>
  <ifrs-full:Revenue contextRef="FY2025" unitRef="CNY" scale="3" decimals="-3">660000000</ifrs-full:Revenue>
  <ifrs-full:ProfitLoss contextRef="FY2025" unitRef="CNY" scale="3" decimals="-3">130000000</ifrs-full:ProfitLoss>
  <ifrs-full:GainLossOnFairValueAdjustmentInvestmentProperty contextRef="FY2025" unitRef="CNY" scale="3" decimals="-3">8000000</ifrs-full:GainLossOnFairValueAdjustmentInvestmentProperty>
  <ifrs-full:CashAndCashEquivalents contextRef="FY2025I" unitRef="CNY" scale="3" decimals="-3">200000000</ifrs-full:CashAndCashEquivalents>
  <ifrs-full:Liabilities contextRef="FY2025I" unitRef="CNY" scale="3" decimals="-3">300000000</ifrs-full:Liabilities>
</xbrli:xbrl>
"""
    (root / "hkexnews" / "00700").mkdir(parents=True, exist_ok=True)
    (root / "hkexnews" / "00700" / "00700_2025_annual.xml").write_text(xbrl, encoding="utf-8")

    notes = """腾讯控股有限公司 2025 年度报告 — 财务报表附注（节选，演示数据）
附注 12 非经常性损益明细（单位：人民币千元）
投资物业公允价值变动收益 8,000,000 千元
资产减值损失 3,000,000 千元
政府补助及补贴 1,500,000 千元
汇兑损失净额 500,000 千元
出售附属公司收益 2,000,000 千元
诉讼赔偿支出 1,200,000 千元
上述项目均为一次性损益，计算经常性盈利时应予剔除。
"""
    (root / "hkexnews" / "00700" / "00700_2025_annual_notes.txt").write_text(notes, encoding="utf-8")

    announcements = [
        ("00700", "2023-12-31", "ANNUAL", "2024-03-20T08:00:00", "hkexnews", "2023 Annual Report", "", 0),
        ("00700", "2024-12-31", "ANNUAL", "2025-03-19T08:00:00", "hkexnews", "2024 Annual Report", "", 0),
        ("00700", "2024-12-31", "ANNUAL", "2025-10-15T08:00:00", "hkexnews", "2024 Annual Report (restated)", "", 1),
        ("00700", "2025-12-31", "ANNUAL", "2026-03-25T08:00:00", "hkexnews", "2025 Annual Report", "", 0),
        ("09988", "2025-03-31", "ANNUAL", "2025-05-15T08:00:00", "hkexnews", "2025 Annual Report", "", 0),
        ("09988", "2026-03-31", "ANNUAL", "2026-05-20T08:00:00", "hkexnews", "2026 Annual Report", "", 0),
    ]
    with (root / "announcements.csv").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["symbol", "period_end", "period_type", "published_at", "source", "title", "url", "is_restatement"])
        writer.writerows(announcements)

    # Monthly PIT FX: 1 unit quote = rate units HKD. Rates change over time so the
    # demo shows why factor FX must be PIT (as_of) rather than today's rate.
    with (root / "fx_rates.csv").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["base", "quote", "as_of", "rate", "source"])
        day = date(2023, 1, 1)
        while day <= date(2026, 10, 1):
            if day.year <= 2024:
                cny = 1.08
            elif day.year == 2025:
                cny = 1.09
            elif day.month <= 6:
                cny = 1.10
            else:
                cny = 1.11
            writer.writerow(["HKD", "CNY", day.isoformat(), cny, "fixture"])
            writer.writerow(["HKD", "USD", day.isoformat(), 7.80, "fixture"])
            writer.writerow(["HKD", "HKD", day.isoformat(), 1.0, "fixture"])
            day = (day.replace(day=28) + timedelta(days=7)).replace(day=1)

    with (root / "market_caps.csv").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["symbol", "as_of", "market_cap", "currency", "source"])
        for row in [
            ("00700", "2024-03-28", 3_100_000_000_000),
            ("00700", "2024-09-30", 3_200_000_000_000),
            ("00700", "2025-03-31", 3_400_000_000_000),
            ("00700", "2025-09-30", 3_600_000_000_000),
            ("00700", "2026-03-31", 3_700_000_000_000),
            ("00700", "2026-06-30", 3_800_000_000_000),
            ("09988", "2025-06-30", 2_000_000_000_000),
            ("09988", "2026-06-30", 2_300_000_000_000),
        ]:
            writer.writerow([row[0], row[1], row[2], "HKD", "fixture"])

    print(f"fixtures written to {root.resolve()}")


if __name__ == "__main__":
    main()
