"""Readers for the wanted list settings."""

from __future__ import annotations

from typing import Any

from shelfmark.core.config import config
from shelfmark.core.request_helpers import coerce_bool

DEFAULT_BACKOFF_HOURS: tuple[float, ...] = (1.0, 6.0, 24.0)
DEFAULT_MAX_SEARCHES_PER_HOUR = 6
_DEFAULT_SOURCES: tuple[tuple[str, bool], ...] = (
    ("direct_download", True),
    ("libgen", True),
    ("prowlarr", True),
    ("newznab", True),
    ("irc", False),
)
# Fixed-layout and comic formats are off by default: rarely what an ebook reader wants.
_DEFAULT_FORMATS: tuple[tuple[str, bool], ...] = (
    ("epub", True),
    ("azw3", True),
    ("mobi", True),
    ("fb2", True),
    ("pdf", False),
    ("djvu", False),
    ("cbz", False),
    ("cbr", False),
)


def default_source_order() -> list[dict[str, Any]]:
    """Default value of WANTED_SOURCES."""
    return [{"id": source, "enabled": enabled} for source, enabled in _DEFAULT_SOURCES]


def default_format_order() -> list[dict[str, Any]]:
    """Default value of WANTED_PREFERRED_FORMATS."""
    return [{"id": fmt, "enabled": enabled} for fmt, enabled in _DEFAULT_FORMATS]


def _ordered_entries(value: object, default: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entries = value if isinstance(value, list) else default
    # A plain list of ids (e.g. ["epub", "mobi"] from an env var) enables those ids.
    entries = [{"id": entry} if isinstance(entry, str) else entry for entry in entries]
    return [
        entry for entry in entries if isinstance(entry, dict) and isinstance(entry.get("id"), str)
    ]


def wanted_enabled() -> bool:
    """Whether the wanted list is switched on."""
    return coerce_bool(config.get("WANTED_ENABLED", False), default=False)


def wanted_sources() -> list[str]:
    """Enabled wanted-list sources in order of preference."""
    entries = _ordered_entries(config.get("WANTED_SOURCES"), default_source_order())
    return [entry["id"] for entry in entries if entry.get("enabled", True)]


def preferred_formats() -> tuple[tuple[str, ...], frozenset[str]]:
    """(formats to download automatically, best first; formats switched off)."""
    entries = _ordered_entries(config.get("WANTED_PREFERRED_FORMATS"), default_format_order())
    order = tuple(entry["id"].lower() for entry in entries if entry.get("enabled", True))
    excluded = frozenset(entry["id"].lower() for entry in entries if not entry.get("enabled", True))
    return order, excluded


def format_order() -> tuple[str, ...]:
    """Every format of WANTED_PREFERRED_FORMATS in order, switched off or not."""
    entries = _ordered_entries(config.get("WANTED_PREFERRED_FORMATS"), default_format_order())
    return tuple(entry["id"].lower() for entry in entries)


def format_wait() -> tuple[str | None, float]:
    """(format a release must reach while waiting, days to wait for it); 0 days is off.

    No format means the first preferred one.
    """
    cutoff = str(config.get("WANTED_FORMAT_CUTOFF", "") or "").strip().lower() or None
    try:
        days = float(str(config.get("WANTED_FORMAT_WAIT_DAYS", 0) or 0))
    except ValueError:
        days = 0.0
    return cutoff, max(days, 0.0)


def supported_formats() -> frozenset[str]:
    """Formats allowed in search results (SUPPORTED_FORMATS)."""
    value = config.get("SUPPORTED_FORMATS", None)
    if isinstance(value, str):
        value = [part.strip() for part in value.split(",")]
    if not isinstance(value, list):
        return frozenset(fmt for fmt, _enabled in _DEFAULT_FORMATS)
    return frozenset(str(fmt).strip().lower() for fmt in value if str(fmt).strip())


def backoff_hours() -> tuple[float, ...]:
    """Hours between searches for a book not yet found; the last value repeats."""
    value = config.get("WANTED_SEARCH_BACKOFF_HOURS", None)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        raw: list[object] = [value]
    elif isinstance(value, str):
        raw = [part for part in value.replace(";", ",").split(",") if part.strip()]
    else:
        raw = []
    hours: list[float] = []
    for part in raw:
        try:
            parsed = float(str(part).strip())
        except ValueError:
            continue
        if parsed > 0:
            hours.append(parsed)
    return tuple(hours) or DEFAULT_BACKOFF_HOURS


def max_searches_per_hour() -> int:
    """Upper limit for background searches per hour."""
    value = config.get("WANTED_MAX_SEARCHES_PER_HOUR", DEFAULT_MAX_SEARCHES_PER_HOUR)
    try:
        parsed = int(float(str(value)))
    except ValueError:
        return DEFAULT_MAX_SEARCHES_PER_HOUR
    return max(parsed, 1)
