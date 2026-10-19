from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from shelfmark.core import wanted_watches
from shelfmark.metadata_providers import BookMetadata


def _book(provider_id, **fields):
    return BookMetadata(provider="hardcover", provider_id=provider_id, title="Dune", **fields)


@pytest.mark.parametrize(
    ("provider", "fields"),
    [
        ("openlibrary", {"series": "id:1"}),
        ("hardcover", {"series": "Dune"}),
        ("hardcover", {"title": "id:1"}),
        ("hardcover", {"series": "id:1", "author": "id:2"}),
        ("hardcover", "series"),
        ("hardcover", {"author": "status:1"}),
    ],
)
def test_only_one_id_based_series_author_or_list_can_be_watched(provider, fields):
    with pytest.raises(wanted_watches.WatchError):
        wanted_watches.normalize_watch(provider, fields)


def test_a_valid_watch_is_normalized():
    assert wanted_watches.normalize_watch("hardcover", {"hardcover_list": " status:1 "}) == (
        "hardcover",
        {"hardcover_list": "status:1"},
    )


def test_books_are_read_page_by_page_up_to_the_cap(monkeypatch):
    monkeypatch.setattr(wanted_watches, "MAX_WATCH_BOOKS", 3)
    pages = {
        1: SimpleNamespace(books=[_book("1"), _book("2")], has_more=True),
        2: SimpleNamespace(books=[_book("3"), _book("4")], has_more=True),
    }
    calls = []

    class _Provider:
        def search_paginated(self, options):
            calls.append((options.page, options.fields))
            return pages[options.page]

    with (
        patch("shelfmark.metadata_providers.get_provider", return_value=_Provider()),
        patch("shelfmark.metadata_providers.get_provider_kwargs", return_value={}),
    ):
        books = wanted_watches.fetch_watch_books("hardcover", {"series": "id:9"})

    assert [book.provider_id for book in books] == ["1", "2", "3"]
    assert calls == [(1, {"series": "id:9"}), (2, {"series": "id:9"})]


def test_a_book_becomes_bookmark_data():
    data = wanted_watches.book_data_from_metadata(
        _book("7", authors=["Frank Herbert"], publish_year=1965, release_date="1965-08-01")
    )
    assert data == {
        "title": "Dune",
        "author": "Frank Herbert",
        "authors": ["Frank Herbert"],
        "provider": "hardcover",
        "provider_id": "7",
        "year": "1965",
        "release_date": "1965-08-01",
        "content_type": "ebook",
    }
