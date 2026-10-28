"""Optional ClickHouse mirror for wide factor serving (never the system of record).

PostgreSQL/SQLite remain the PIT source of truth; ClickHouse gets an idempotent
``ReplacingMergeTree`` feed keyed by (symbol, as_of, feature, formula_version).
The SQL view ``pit_factors`` in ``sql/clickhouse.sql`` resolves the latest version.
"""

from __future__ import annotations

from typing import Any, Iterable
from urllib.parse import urlparse

from ..config import Config
from ..domain import FactorValue
from ..logging_utils import get_logger
from ..util import utcnow

logger = get_logger(__name__)


class ClickHouseMirror:
    def __init__(self, config: Config, client: Any = None) -> None:
        self.config = config
        self._client = client
        self.enabled = bool(config.clickhouse.enabled)

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                import clickhouse_connect  # type: ignore
            except ImportError as exc:  # pragma: no cover - optional extra
                raise RuntimeError("clickhouse-connect not installed: pip install '.[clickhouse]'") from exc
            parsed = urlparse(self.config.clickhouse.url)
            self._client = clickhouse_connect.get_client(
                host=parsed.hostname or "localhost",
                port=parsed.port or 8123,
                username=self.config.clickhouse.user,
                password=self.config.clickhouse.password,
                database=self.config.clickhouse.database,
            )
        return self._client

    def push_features(self, factors: Iterable[FactorValue], run_id: str) -> int:
        if not self.enabled:
            return 0
        rows = []
        computed = utcnow()
        for factor in factors:
            rows.append([
                factor.symbol, factor.as_of, factor.period_end, factor.feature,
                factor.value, factor.currency, factor.unit, factor.formula_version,
                run_id, computed, computed,
            ])
        if not rows:
            return 0
        try:
            self.client.insert(
                self.config.clickhouse.table,
                rows,
                column_names=[
                    "symbol", "as_of", "period_end", "feature", "value", "currency",
                    "unit", "formula_version", "run_id", "computed_at", "version",
                ],
            )
        except Exception as exc:
            # A broken mirror must never fail the core pipeline.
            logger.warning("ClickHouse mirror failed", extra={"context": {"error": str(exc)}})
            return 0
        return len(rows)
