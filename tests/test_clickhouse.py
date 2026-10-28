from __future__ import annotations

from datetime import date

from qp.domain import FactorValue
from qp.storage.clickhouse import ClickHouseMirror


class FakeClickHouseClient:
    def __init__(self):
        self.calls = []

    def insert(self, table, rows, column_names=None):
        self.calls.append((table, rows, column_names))


def test_mirror_disabled_is_noop(config):
    mirror = ClickHouseMirror(config, client=FakeClickHouseClient())
    assert mirror.push_features([], "run-1") == 0
    assert mirror.client.calls == []


def test_mirror_inserts_versioned_rows(config):
    config.clickhouse.enabled = True
    client = FakeClickHouseClient()
    mirror = ClickHouseMirror(config, client=client)
    factor = FactorValue(
        symbol="00700", as_of=date(2026, 10, 27), period_end=date(2025, 12, 31),
        feature="fcf_yield", value=0.06, currency="HKD", unit="ratio",
        formula_version="fcf-v1",
    )
    assert mirror.push_features([factor], "run-1") == 1
    table, rows, columns = client.calls[0]
    assert table == "factors"
    assert columns[-1] == "version"
    assert len(rows[0]) == len(columns) == 11
    assert rows[0][3] == "fcf_yield"


def test_mirror_failure_never_raises(config):
    config.clickhouse.enabled = True

    class Broken:
        def insert(self, *args, **kwargs):
            raise RuntimeError("clickhouse down")

    mirror = ClickHouseMirror(config, client=Broken())
    factor = FactorValue(
        symbol="00700", as_of=date(2026, 10, 27), period_end=date(2025, 12, 31),
        feature="fcf_yield", value=0.06, currency="HKD", unit="ratio",
        formula_version="fcf-v1",
    )
    assert mirror.push_features([factor], "run-1") == 0
