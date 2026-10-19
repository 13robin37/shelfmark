"""Series, authors and lists whose books go on the wanted list."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from shelfmark.core.logger import setup_logger
from shelfmark.core.wanted_access import WANTED_CONTENT_TYPE

if TYPE_CHECKING:
    from shelfmark.metadata_providers import BookMetadata

logger = setup_logger(__name__)

# Search fields that name one series, author or list by id, so the books behind them
# stay the same set over time (a free-text search would drift).
_WATCH_FIELD_VALUES: dict[str, re.Pattern[str]] = {
    "series": re.compile(r"^id:\d+$"),
    "author": re.compile(r"^id:\d+$"),
    "hardcover_list": re.compile(r"^(id|status):\d+$"),
}
WATCH_PROVIDERS = frozenset({"hardcover"})
# Books a watch keeps track of; past that, books added later may go unnoticed.
MAX_WATCH_BOOKS = 1000
# Books bookmarked at once by "Bookmark all missing".
MAX_BULK_BOOKS = 200
_PAGE_SIZE = 40


class WatchError(ValueError):
    """A series, author or list that cannot be watched."""


class WatchFetchError(RuntimeError):
    """The provider did not return the books completely."""


def normalize_watch(provider: object, fields: object) -> tuple[str, dict[str, str]]:
    """Validate what to watch: exactly one id-based search field of a supported provider."""
    if provider not in WATCH_PROVIDERS:
        msg = "Only Hardcover series, authors and lists can be watched"
        raise WatchError(msg)
    if not isinstance(fields, dict) or len(fields) != 1:
        msg = "Watch exactly one series, author or list"
        raise WatchError(msg)
    key, value = next(iter(fields.items()))
    pattern = _WATCH_FIELD_VALUES.get(key)
    if pattern is None or not isinstance(value, str) or not pattern.match(value.strip()):
        msg = "Pick a series, author or list from the search suggestions"
        raise WatchError(msg)
    return str(provider), {key: value.strip()}


def book_key(book: BookMetadata) -> str:
    """How a watch remembers a book it has seen."""
    return f"{book.provider}:{book.provider_id}"


def fetch_watch_books(
    provider: str, fields: dict[str, str], limit: int = MAX_WATCH_BOOKS
) -> tuple[list[BookMetadata], int]:
    """The books a series, author or list holds now (up to ``limit``), and how many it has.

    Raises:
        WatchFetchError: The provider is off, or stopped answering part way through.

    """
    from shelfmark.metadata_providers import (
        MetadataSearchOptions,
        get_provider,
        get_provider_kwargs,
        is_provider_enabled,
    )

    if not is_provider_enabled(provider):
        msg = f"{provider.title()} is not set up"
        raise WatchFetchError(msg)
    source = get_provider(provider, **get_provider_kwargs(provider))
    books: list[BookMetadata] = []
    total = 0
    page = 1
    while len(books) < limit:
        result = source.search_paginated(
            MetadataSearchOptions(query="", fields=dict(fields), page=page, limit=_PAGE_SIZE)
        )
        total = max(total, result.total_found)
        if not result.books:
            # Providers report failures as empty pages; an empty page short of the
            # announced total is one.
            if total > len(books):
                msg = f"{provider.title()} returned {len(books)} of {total} books"
                raise WatchFetchError(msg)
            break
        books.extend(result.books)
        if not result.has_more:
            break
        page += 1
    return books[:limit], max(total, len(books))


def book_data_from_metadata(book: BookMetadata) -> dict[str, Any]:
    """What the wanted list stores for a book, as the frontend would send it."""
    data: dict[str, Any] = {
        "title": book.title,
        "author": ", ".join(book.authors) or None,
        "authors": list(book.authors) or None,
        "provider": book.provider,
        "provider_id": book.provider_id,
        "isbn_13": book.isbn_13,
        "isbn_10": book.isbn_10,
        "year": str(book.publish_year) if book.publish_year else None,
        "release_date": book.release_date,
        "preview": book.cover_url,
        "series_name": book.series_name,
        "series_position": book.series_position,
        "series_count": book.series_count,
        "subtitle": book.subtitle,
        "language": book.language,
        "source_url": book.source_url,
        "search_title": book.search_title,
        "search_author": book.search_author,
        "titles_by_language": book.titles_by_language or None,
        "content_type": WANTED_CONTENT_TYPE,
    }
    return {key: value for key, value in data.items() if value is not None}
