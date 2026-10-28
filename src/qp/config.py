"""Configuration: dataclass defaults -> TOML file -> environment -> CLI overrides.

No pydantic/settings dependency: TOML is stdlib on Python 3.11 and every option has a
safe default, so `Config()` works in a unit test with no files at all.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

from .errors import ConfigError

DEFAULT_CONFIG_PATH = Path("config/pipeline.toml")


@dataclass
class DatabaseConfig:
    url: str = "sqlite:///data/quant.db"
    echo: bool = False


@dataclass
class ClickHouseConfig:
    enabled: bool = False
    url: str = "http://localhost:8123"
    database: str = "quant"
    user: str = "quant"
    password: str = ""
    table: str = "factors"


@dataclass
class SourceConfig:
    priority: tuple[str, ...] = ("hkexnews", "akshare", "yfinance")
    fixtures_dir: str = "examples/fixtures"
    securities_file: str = "config/securities.csv"
    retry: int = 2
    retry_backoff_seconds: float = 1.0
    request_timeout_seconds: int = 30
    hkexnews_base: str = "https://www1.hkexnews.hk"
    hkexnews_stock_list_path: str = "/ncms/script/eds/activestock_sehk_e.json"
    hkexnews_search_path: str = "/search/titleSearchServlet.do"
    hkexnews_financial_category: str = "40000"
    max_document_bytes: int = 25_000_000
    raw_dir: str = "data/raw/hkexnews"
    offline: bool = False


@dataclass
class FxConfig:
    target_currency: str = "HKD"
    rates_file: str = "examples/fixtures/fx_rates.csv"
    fallback_rates: dict[str, float] = field(
        default_factory=lambda: {"CNY": 1.09, "USD": 7.80, "HKD": 1.0}
    )
    max_age_days: int = 45


@dataclass
class PitConfig:
    lag_days: int = 1
    backfill_mode: bool = False
    max_staleness_days: int = 550


@dataclass
class AlignConfig:
    conflict_rel_tolerance: float = 0.01
    quarantine_on_conflict: bool = False


@dataclass
class FcfConfig:
    capex_sign_policy: str = "auto"
    formula_version: str = "fcf-v1"


@dataclass
class NotesConfig:
    keywords: tuple[str, ...] = (
        "非经常性损益", "其他收益", "其他收入", "公允价值变动", "政府补助",
        "汇兑", "诉讼", "赔偿", "non-recurring", "other gains", "fair value",
    )
    context_chars: int = 700
    max_chunks: int = 20
    max_note_chars: int = 12000


@dataclass
class LlmConfig:
    enabled: bool = False
    base_url: str = "http://localhost:11434/v1"
    model: str = "qwen2.5:7b"
    api_key_env: str = "QP_LLM_API_KEY"
    timeout_seconds: int = 60
    retries: int = 2
    temperature: float = 0.0
    strict_grounding: bool = True
    cache_dir: str = "data/cache/llm"
    prompt_version: str = "notes-zh-v1"


@dataclass
class QualityConfig:
    strict: bool = False
    fail_on_error: bool = False
    alert_webhook_url_env: str = "QP_ALERT_WEBHOOK_URL"


@dataclass
class ReportConfig:
    output_dir: str = "reports"
    log_level: str = "INFO"


@dataclass
class Config:
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    clickhouse: ClickHouseConfig = field(default_factory=ClickHouseConfig)
    sources: SourceConfig = field(default_factory=SourceConfig)
    fx: FxConfig = field(default_factory=FxConfig)
    pit: PitConfig = field(default_factory=PitConfig)
    align: AlignConfig = field(default_factory=AlignConfig)
    fcf: FcfConfig = field(default_factory=FcfConfig)
    notes: NotesConfig = field(default_factory=NotesConfig)
    llm: LlmConfig = field(default_factory=LlmConfig)
    quality: QualityConfig = field(default_factory=QualityConfig)
    reports: ReportConfig = field(default_factory=ReportConfig)

    # where relative file paths are resolved from
    base_dir: Path = field(default_factory=Path.cwd)
    path: Path | None = None

    _SECTIONS = {
        "database": DatabaseConfig, "clickhouse": ClickHouseConfig,
        "sources": SourceConfig, "fx": FxConfig, "pit": PitConfig,
        "align": AlignConfig, "fcf": FcfConfig, "notes": NotesConfig,
        "llm": LlmConfig, "quality": QualityConfig, "reports": ReportConfig,
    }

    def resolve(self, path: str | Path) -> Path:
        p = Path(path)
        return p if p.is_absolute() else (self.base_dir / p)

    def validate(self) -> None:
        if not self.sources.priority:
            raise ConfigError("sources.priority must not be empty")
        if self.fcf.capex_sign_policy not in {"auto", "signed", "positive_outflow", "negative_outflow"}:
            raise ConfigError(f"bad capex_sign_policy: {self.fcf.capex_sign_policy}")
        if self.pit.lag_days < 0:
            raise ConfigError("pit.lag_days must be >= 0")
        if not 0 <= self.align.conflict_rel_tolerance < 1:
            raise ConfigError("align.conflict_rel_tolerance must be in [0, 1)")


def _apply_section(obj: Any, values: dict[str, Any]) -> Any:
    known = {f.name: f for f in fields(obj)}
    kwargs: dict[str, Any] = {}
    for key, value in values.items():
        key = key.replace("-", "_")
        if key not in known:
            raise ConfigError(f"unknown config key {key!r} in {type(obj).__name__}")
        current = getattr(obj, key)
        if isinstance(value, list) and isinstance(current, tuple):
            value = tuple(value)
        kwargs[key] = value
    return replace(obj, **kwargs)


def _apply_env(cfg: "Config") -> "Config":
    env_map: tuple[tuple[str, str, str], ...] = (
        ("QP_DATABASE_URL", "database", "url"),
        ("QP_OFFLINE", "sources", "offline"),
        ("QP_CLICKHOUSE_ENABLED", "clickhouse", "enabled"),
        ("QP_CLICKHOUSE_URL", "clickhouse", "url"),
        ("QP_TARGET_CURRENCY", "fx", "target_currency"),
        ("QP_LLM_ENABLED", "llm", "enabled"),
        ("QP_LLM_BASE_URL", "llm", "base_url"),
        ("QP_LLM_MODEL", "llm", "model"),
    )
    bool_vars = {"QP_OFFLINE", "QP_CLICKHOUSE_ENABLED", "QP_LLM_ENABLED"}
    for env_name, section, key in env_map:
        if env_name not in os.environ:
            continue
        raw = os.environ[env_name]
        value: Any = _as_bool(raw) if env_name in bool_vars else raw
        setattr(cfg, section, _apply_section(getattr(cfg, section), {key: value}))
    if "QP_STRICT" in os.environ:
        cfg.quality = replace(cfg.quality, strict=_as_bool(os.environ["QP_STRICT"]))
    return cfg


def _as_bool(raw: str) -> bool:
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


def load_config(
    path: str | Path | None = None,
    overrides: dict[str, dict[str, Any]] | None = None,
    apply_env: bool = True,
) -> Config:
    env_path = os.environ.get("QP_CONFIG")
    chosen: Path | None = None
    for candidate in (path, env_path, DEFAULT_CONFIG_PATH):
        if candidate and Path(candidate).exists():
            chosen = Path(candidate)
            break
    if chosen is None and path is not None:
        raise ConfigError(f"config file not found: {path}")

    cfg = Config()
    if chosen is not None:
        with chosen.open("rb") as fh:
            raw = tomllib.load(fh)
        if chosen.name == DEFAULT_CONFIG_PATH.name and chosen.parent.name == "config":
            cfg.base_dir = chosen.resolve().parent.parent
        else:
            cfg.base_dir = chosen.resolve().parent
        cfg.path = chosen
        for section, values in raw.items():
            key = section.replace("-", "_")
            if key not in cfg._SECTIONS or not isinstance(values, dict):
                continue
            setattr(cfg, key, _apply_section(getattr(cfg, key), values))
    if apply_env:
        cfg = _apply_env(cfg)
    if overrides:
        for section, values in overrides.items():
            key = section.replace("-", "_")
            if key not in cfg._SECTIONS:
                raise ConfigError(f"unknown config section: {section}")
            setattr(cfg, key, _apply_section(getattr(cfg, key), values))
    cfg.validate()
    return cfg
