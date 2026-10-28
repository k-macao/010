"""Minimal, dependency-free XBRL instance parser for HKEXnews inline/standalone XBRL.

It extracts contexts (period start/end or instant), units (currency), numeric facts
with their ``scale`` and ``sign`` attributes, and maps common IFRS tags onto our
canonical Chinese labels so the normalizer can consume them unchanged.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from ..util import to_date

TAG_MAP: dict[str, str] = {
    "netcashflowsfromusedinoperatingactivities": "经营活动产生的现金流量净额",
    "netcashflowsfromusedinoperatingactivitiescontinuingoperations": "经营活动产生的现金流量净额",
    "netcashflowfromoperatingactivities": "经营活动产生的现金流量净额",
    "paymentstoacquirepropertyplantandequipment": "购建固定资产、无形资产和其他长期资产支付的现金",
    "paymentsforcapitalexpenditures": "购建固定资产、无形资产和其他长期资产支付的现金",
    "capitalexpenditure": "购建固定资产、无形资产和其他长期资产支付的现金",
    "revenue": "营业收入",
    "revenuefromcontractswithcustomersexcludingassessedtax": "营业收入",
    "profitloss": "净利润",
    "profitlossattributabletoownersofparent": "股东应占溢利",
    "gainlossonfairvalueadjustmentinvestmentproperty": "投资物业公允价值变动收益",
    "impairmentloss": "资产减值损失",
    "governmentgrants": "政府补助",
    "foreignexchangegainslosses": "汇兑损益",
    "gainlossondisposalofsubsidiaries": "处置子公司收益",
    "cashandcashequivalents": "现金及现金等价物",
    "assets": "总资产",
    "liabilities": "总负债",
    "equity": "总权益",
}


@dataclass(frozen=True)
class XbrlFact:
    concept: str
    context_ref: str
    value: float
    currency: str
    period_end: date
    period_start: date | None = None
    decimals: str | None = None
    scale: int = 0

    @property
    def canonical_label(self) -> str:
        local = self.concept.split(":")[-1].lower().replace("_", "").replace("-", "")
        return TAG_MAP.get(local, self.concept)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].split(":")[-1]


def parse_xbrl_instance(source: str | Path | bytes) -> list[XbrlFact]:
    if isinstance(source, bytes):
        root = ET.fromstring(source)
    else:
        root = ET.parse(Path(source)).getroot()

    contexts: dict[str, tuple[date | None, date]] = {}
    units: dict[str, str] = {}
    for node in root.iter():
        name = _local(str(node.tag)).lower()
        if name == "context" and node.get("id"):
            start = end = None
            for child in node.iter():
                child_name = _local(str(child.tag)).lower()
                if child_name == "startdate" and child.text:
                    start = to_date(child.text.strip())
                elif child_name == "enddate" and child.text:
                    end = to_date(child.text.strip())
                elif child_name == "instant" and child.text:
                    end = to_date(child.text.strip())
            if end is not None:
                contexts[node.get("id", "")] = (start, end)
        elif name == "unit" and node.get("id"):
            for child in node.iter():
                if _local(str(child.tag)).lower() == "measure" and child.text:
                    measure = child.text.strip()
                    units[node.get("id", "")] = measure.split(":")[-1].upper()
                    break

    facts: list[XbrlFact] = []
    for node in root.iter():
        context_ref = next((v for k, v in node.attrib.items() if _local(k).lower() == "contextref"), None)
        unit_ref = next((v for k, v in node.attrib.items() if _local(k).lower() == "unitref"), None)
        if not context_ref or context_ref not in contexts or not node.text:
            continue
        text = node.text.strip().replace(",", "").replace(" ", "")
        if not re.fullmatch(r"-?\d+(\.\d+)?", text):
            continue
        scale = next((int(v) for k, v in node.attrib.items() if _local(k).lower() == "scale"), 0)
        sign_attr = next((v for k, v in node.attrib.items() if _local(k).lower() == "sign"), "")
        decimals = next((v for k, v in node.attrib.items() if _local(k).lower() == "decimals"), None)
        value = float(text) * (10 ** scale)
        if sign_attr == "-":
            value = -value
        start, end = contexts[context_ref]
        facts.append(XbrlFact(
            # ElementTree expands prefixes into {namespace}local; keep the LOCAL
            # name so TAG_MAP / the field matcher can see the IFRS concept.
            concept=_local(str(node.tag)), context_ref=context_ref, value=value,
            currency=units.get(unit_ref or "", ""), period_end=end,
            period_start=start, decimals=decimals, scale=scale,
        ))
    return facts


def xbrl_facts_to_frame(facts: list[XbrlFact]) -> Any:
    import pandas as pd

    return pd.DataFrame([{
        "raw_label": f.canonical_label,
        "value": f.value,
        "raw_code": f.concept,
    } for f in facts])
