"""Vendor adapters. All of them emit :class:`qp.domain.RawReport`."""

from __future__ import annotations

from .base import DisclosureRecord, SourceAdapter, iter_reports  # noqa: F401
from .shapes import to_long  # noqa: F401

__all__ = ["DisclosureRecord", "SourceAdapter", "iter_reports", "to_long"]
