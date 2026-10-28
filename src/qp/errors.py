"""Explicit exception types: fail loudly at the ingestion boundary, never silently mid-factor."""

from __future__ import annotations


class QPError(Exception):
    """Base class for all pipeline errors."""


class ConfigError(QPError):
    pass


class SourceError(QPError):
    """A vendor adapter failed (network, schema drift, auth, ...)."""


class SchemaDriftError(SourceError):
    """Vendor changed column names / payload shape and we refuse to guess."""


class MissingFieldError(QPError):
    pass


class MissingFxRate(QPError):
    pass


class QualityGateError(QPError):
    """Strict quality gate rejected a report before factor write."""


class NotesExtractionError(QPError):
    pass


class LlmError(QPError):
    pass


class StorageError(QPError):
    pass
