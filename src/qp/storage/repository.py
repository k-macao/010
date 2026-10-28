"""Repository: idempotent upserts + PIT reads.

SQLite and PostgreSQL share this code path via SQLAlchemy Core. Dialect-specific
``ON CONFLICT DO UPDATE`` is used when available; otherwise a transaction-scoped
DELETE+INSERT fallback keeps the same idempotency guarantee.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
from sqlalchemy import Date, DateTime, Engine, Integer, and_, create_engine, delete, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ..errors import StorageError
from ..ingest.fx import FxRateRow
from ..util import as_of_cutoff, json_dumps, to_date, to_datetime, utcnow
from .schema import TABLES, metadata

EMPTY_FACTS_COLUMNS = [
    "symbol", "source", "statement", "period_end", "period_type", "published_at",
    "available_at", "field", "value", "value_native", "currency", "report_currency",
    "raw_value", "raw_label", "unit_scale", "fx_rate", "fx_as_of", "fx_source",
    "sign", "quality", "content_hash", "ingested_at", "run_id",
]


def build_upsert(table: Any, prepared: list[dict[str, Any]], pk: list[str], dialect: str) -> Any | None:
    """Dialect-specific INSERT ... ON CONFLICT DO UPDATE (or None for fallback)."""
    if dialect == "sqlite":
        stmt = sqlite_insert(table).values(prepared)
    elif dialect == "postgresql":
        stmt = pg_insert(table).values(prepared)
    else:
        return None
    updates = {c.name: getattr(stmt.excluded, c.name) for c in table.columns if c.name not in pk}
    return stmt.on_conflict_do_update(index_elements=pk, set_=updates)


class Repository:
    def __init__(self, url_or_engine: str | Engine, echo: bool = False) -> None:
        if isinstance(url_or_engine, Engine):
            self.engine = url_or_engine
        else:
            url = str(url_or_engine)
            if url.startswith("sqlite") and ":///" in url and ":memory:" not in url:
                path = Path(url.split("///", 1)[1])
                path.parent.mkdir(parents=True, exist_ok=True)
            self.engine = create_engine(url, echo=echo, future=True)
        self.dialect = self.engine.dialect.name

    # ------------------------------------------------------------------ setup
    def init_schema(self) -> None:
        metadata.create_all(self.engine)

    def close(self) -> None:
        self.engine.dispose()

    # ------------------------------------------------------------------ write
    @staticmethod
    def _coerce(table_name: str, row: dict[str, Any]) -> dict[str, Any]:
        table = TABLES[table_name]
        out: dict[str, Any] = {}
        for column in table.columns:
            if column.name not in row:
                continue
            value = row[column.name]
            if value is None:
                out[column.name] = None
                continue
            if isinstance(column.type, DateTime):
                value = to_datetime(value) if not isinstance(value, datetime) else value.replace(microsecond=0)
            elif isinstance(column.type, Date):
                value = to_date(value)
            elif isinstance(column.type, Integer):
                value = int(value) if value not in {"", None} else None
            out[column.name] = value
        return out

    def upsert(self, table_name: str, rows: Iterable[dict[str, Any]]) -> int:
        if table_name not in TABLES:
            raise StorageError(f"unknown table {table_name}")
        table = TABLES[table_name]
        prepared = [self._coerce(table_name, row) for row in rows]
        if not prepared:
            return 0
        # SQLAlchemy multi-values INSERT requires identical key sets on every row.
        keys = sorted({key for row in prepared for key in row})
        prepared = [{key: row.get(key) for key in keys} for row in prepared]
        pk = [c.name for c in table.primary_key.columns]
        stmt = build_upsert(table, prepared, pk, self.dialect)
        with self.engine.begin() as conn:
            if stmt is not None:
                conn.execute(stmt)
            else:
                for row in prepared:
                    conditions = and_(*[table.c[name] == row.get(name) for name in pk])
                    conn.execute(delete(table).where(conditions))
                    conn.execute(table.insert().values(**row))
        return len(prepared)

    def insert_quality_events(self, run_id: str, events: Iterable[dict[str, Any]]) -> int:
        rows = []
        for event in events:
            row = dict(event)
            row["check_name"] = row.pop("check", "")
            row["created_at"] = row.get("created_at") or utcnow()
            rows.append(self._coerce("quality_events", row))
        if not rows:
            return 0
        with self.engine.begin() as conn:
            conn.execute(delete(TABLES["quality_events"]).where(
                TABLES["quality_events"].c.run_id == run_id
            ))
            conn.execute(TABLES["quality_events"].insert(), rows)
        return len(rows)

    def save_run(self, summary: Any) -> None:
        self.upsert("pipeline_runs", [{
            "run_id": summary.run_id,
            "started_at": summary.started_at,
            "finished_at": summary.finished_at or utcnow(),
            "as_of": summary.as_of,
            "status": summary.status,
            "config_hash": "",
            "summary": json_dumps(summary.as_dict()),
        }])

    # ------------------------------------------------------------------ reads
    def known_available_at(
        self, symbol: str, source: str, statement: str, period_end: date, content_hash: str
    ) -> datetime | None:
        table = TABLES["raw_reports"]
        query = (
            select(table.c.available_at)
            .where(and_(
                table.c.symbol == symbol, table.c.source == source,
                table.c.statement == statement, table.c.period_end == to_date(period_end),
                table.c.content_hash == content_hash,
            ))
            .order_by(table.c.available_at.desc())
            .limit(1)
        )
        with self.engine.connect() as conn:
            row = conn.execute(query).first()
        return to_datetime(row[0]) if row else None

    def get_facts(self, symbol: str, as_of: date, lag_days: int = 0, only_visible: bool = True) -> pd.DataFrame:
        cutoff = as_of_cutoff(as_of, lag_days)
        sql = "SELECT * FROM normalized_facts WHERE symbol = :symbol"
        if only_visible:
            sql += " AND available_at <= :cutoff"
        with self.engine.connect() as conn:
            return pd.read_sql(text(sql), conn, params={"symbol": symbol, "cutoff": cutoff})

    def get_all_facts(self, symbol: str) -> pd.DataFrame:
        with self.engine.connect() as conn:
            return pd.read_sql(text("SELECT * FROM normalized_facts WHERE symbol = :symbol"),
                               conn, params={"symbol": symbol})

    def get_nonrecurring(self, symbol: str, as_of: date, period_end: date | None = None,
                         lag_days: int = 0) -> pd.DataFrame:
        cutoff = as_of_cutoff(as_of, lag_days)
        sql = "SELECT * FROM nonrecurring_items WHERE symbol = :symbol AND available_at <= :cutoff"
        params: dict[str, Any] = {"symbol": symbol, "cutoff": cutoff}
        if period_end is not None:
            sql += " AND period_end = :period_end"
            params["period_end"] = to_date(period_end)
        with self.engine.connect() as conn:
            return pd.read_sql(text(sql), conn, params=params)

    def get_market_cap(self, symbol: str, as_of: date) -> tuple[float, str, date] | None:
        sql = ("SELECT market_cap, currency, as_of FROM market_caps "
               "WHERE symbol = :symbol AND as_of <= :as_of "
               "ORDER BY as_of DESC, source DESC LIMIT 1")
        with self.engine.connect() as conn:
            row = conn.execute(text(sql), {"symbol": symbol, "as_of": to_date(as_of)}).first()
        if row is None:
            return None
        return float(row[0]), str(row[1]), to_date(row[2])

    def get_features(self, symbol: str, as_of: date, formula_version: str | None = None) -> pd.DataFrame:
        sql = "SELECT * FROM derived_features WHERE symbol = :symbol AND as_of = :as_of"
        params: dict[str, Any] = {"symbol": symbol, "as_of": to_date(as_of)}
        if formula_version:
            sql += " AND formula_version = :version"
            params["version"] = formula_version
        with self.engine.connect() as conn:
            return pd.read_sql(text(sql), conn, params=params)

    def get_quality_events(self, run_id: str) -> pd.DataFrame:
        with self.engine.connect() as conn:
            return pd.read_sql(
                text("SELECT * FROM quality_events WHERE run_id = :run_id"),
                conn, params={"run_id": run_id},
            )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = conn.execute(
                text("SELECT summary FROM pipeline_runs WHERE run_id = :run_id"),
                {"run_id": run_id},
            ).first()
        return json.loads(row[0]) if row and row[0] else None

    def all_fx_rows(self) -> list[FxRateRow]:
        with self.engine.connect() as conn:
            df = pd.read_sql(text("SELECT * FROM fx_rates"), conn)
        return [
            FxRateRow(base=str(r.base), quote=str(r.quote), as_of=to_date(r.as_of),
                      rate=float(r.rate), source=str(r.source))
            for r in df.itertuples()
        ]

    # ------------------------------------------------------------- convenience
    def load_reference_data(self, fixtures_dir: Path) -> dict[str, int]:
        """Seed FX rates and market caps from CSV fixtures (idempotent)."""
        counts = {"fx_rates": 0, "market_caps": 0}
        fx_path = fixtures_dir / "fx_rates.csv"
        if fx_path.exists():
            df = pd.read_csv(fx_path, dtype=str)
            rows = [{
                "base": str(r.get("base", "HKD")).upper(), "quote": str(r["quote"]).upper(),
                "as_of": r["as_of"], "rate": float(r["rate"]), "source": str(r.get("source", "csv")),
            } for r in df.to_dict(orient="records")]
            counts["fx_rates"] = self.upsert("fx_rates", rows)
        mc_path = fixtures_dir / "market_caps.csv"
        if mc_path.exists():
            df = pd.read_csv(mc_path, dtype=str)
            rows = [{
                "symbol": str(r["symbol"]).zfill(5), "as_of": r["as_of"],
                "source": str(r.get("source", "fixture")), "market_cap": float(r["market_cap"]),
                "currency": str(r.get("currency", "HKD")).upper(),
            } for r in df.to_dict(orient="records")]
            counts["market_caps"] = self.upsert("market_caps", rows)
        return counts
