"""Tests for automatic release selection."""

from __future__ import annotations

import time

import pytest

from shelfmark.core.release_picker import (
    PickerPreferences,
    book_mismatch,
    language_matches,
    pick_releases,
    rejection_reason,
    release_match_key,
    title_match_score,
)
from shelfmark.core.wanted_service import release_attempt_keys
from shelfmark.release_sources import Release, ReleaseProtocol

BOOK = {"title": "Dune", "author": "Frank Herbert", "provider": "hardcover", "provider_id": "1"}
PREFS = PickerPreferences(
    supported_formats=frozenset({"epub", "mobi", "azw3", "pdf"}),
    preferred_formats=("epub", "azw3", "mobi"),
    source_order=("direct_download", "libgen", "prowlarr"),
)


def _release(title="Dune", **kwargs):
    defaults = {"source": "direct_download", "source_id": title, "format": "epub"}
    defaults.update(kwargs)
    return Release(title=title, **defaults)


def _book(**fields):
    return {"provider": "x", "provider_id": "1", **fields}


@pytest.mark.parametrize(
    ("title", "candidate", "expected"),
    [
        ("Dune", "dune", 10000),
        ("The Dune Book", "dune", 6000 + 2500 - 9),
        ("Messiah of Dune", "dune", 3000 + 2500 - 11),
        ("Something Else", "dune", -10),
        ("", "dune", 0),
    ],
)
def test_title_match_score_matches_the_frontend(title, candidate, expected):
    assert title_match_score(title, candidate) == expected


@pytest.mark.parametrize(
    ("book", "title"),
    [
        (BOOK, "Frank Herbert - Dune (1965) [epub] retail"),
        (BOOK, "Dune: Deluxe Edition"),
        (BOOK, "Dune (Dune Chronicles #1)"),
        # A scene group suffix is dropped, but a spaced title keeps its hyphenated word.
        (BOOK, "Dune.1965.eBook-AlkPub"),
        (_book(title="The Amazing Spider-Man"), "The Amazing Spider-Man"),
        (BOOK, "Dune 9780441172719"),
        (_book(title="Dune", series_position=1.0), "Dune - Book 1"),
        (_book(title="Catch-22", author="Joseph Heller"), "Catch-22"),
        (_book(title="Bird Box", author="Josh Malerman"), "Bird Box"),
        (_book(title="Мастер и Маргарита"), "Мастер и Маргарита"),
        (
            _book(
                title="Harry Potter and the Philosopher's Stone",
                titles_by_language={"de": "Harry Potter und der Stein der Weisen"},
            ),
            "Harry Potter und der Stein der Weisen",
        ),
        (
            _book(title="Sapiens", subtitle="A Brief History of Humankind"),
            "Sapiens: A Brief History of Humankind",
        ),
        (_book(title="Der Schwarm", author="Frank Schatzing"), "Frank Schätzing - Der Schwarm"),
        (_book(title="Solaris", author="Stanisław Lem"), "Stanisław Lem - Solaris"),
        (
            _book(title="Harry Potter and the Philosopher's Stone"),
            "Harry.Potter.and.the.Philosophers.Stone",
        ),
        (
            _book(title="Ender's Game", author="Orson Scott Card"),
            "Orson.Scott.Card.-.Ender.s.Game.EPUB",
        ),
        (_book(title="Dune", series_position=1, series_count=6), "Dune (Book I of VI)"),
        # "Xi" in an author's name is no Roman numeral.
        (
            _book(title="The Governance of China", author="Xi Jinping"),
            "Xi Jinping - The Governance of China",
        ),
    ],
)
def test_book_mismatch_accepts_the_same_work(book, title):
    assert book_mismatch(_release(title), book) is None


@pytest.mark.parametrize(
    ("book", "title", "reason"),
    [
        (BOOK, "Dune Trilogy Box Set", "A collection or bundle"),
        (BOOK, "Arrakis", "Title does not match"),
        (BOOK, "Children of Dune", "Names another work"),
        (_book(title="Saga, Vol. 1"), "Saga, Vol. 4", "Another volume or part"),
        (_book(title="Saga, Vol. I", series_position=1), "Saga, Vol. II", "Another volume or part"),
        (
            _book(title="Diary of a Wimpy Kid", series_position=1),
            "Diary of a Wimpy Kid 05",
            "Another volume or part",
        ),
    ],
)
def test_book_mismatch_rejects_other_works_volumes_and_bundles(book, title, reason):
    assert book_mismatch(_release(title), book).startswith(reason)


@pytest.mark.parametrize(
    ("book_author", "release_author", "matches"),
    [
        ("Frank Herbert", "Brian Herbert", False),
        ("Frank Herbert", "Herbert, Frank", True),
        ("Frank Herbert", "Herbert", True),
        ("Frank Schatzing", "Frank Schätzing", True),
    ],
)
def test_book_mismatch_compares_the_release_author(book_author, release_author, matches):
    book = {**BOOK, "author": book_author}
    reason = book_mismatch(_release(extra={"author": release_author}), book)
    assert reason == (None if matches else "Different author")


@pytest.mark.parametrize(
    ("release_language", "languages", "expected"),
    [
        (None, ["en"], True),
        ("en", ["all"], True),
        ("english", ["en"], True),
        ("en, unknown", ["en"], True),
        ("de", ["en"], False),
        ("en, de", ["en"], False),
    ],
)
def test_language_matches(release_language, languages, expected):
    assert language_matches(release_language, languages) is expected


def test_pick_releases_filters_unusable_releases():
    audiobook = _release("Frank Herbert - Dune (Unabridged) [M4B]", source_id="audio", format=None)
    audiobook.content_type = "audiobook"
    releases = [
        _release(source_id="unsupported", format="lit"),
        _release(source_id="german", language="de"),
        _release(source_id="huge", size_bytes=500 * 1024 * 1024),
        _release(
            source_id="dead-torrent",
            source="prowlarr",
            protocol=ReleaseProtocol.TORRENT,
            seeders=0,
        ),
        _release("Dune Messiah", source_id="other-work"),
        audiobook,
        _release(source_id="good"),
    ]
    prefs = PickerPreferences(supported_formats=PREFS.supported_formats, languages=("en",))

    picked = pick_releases(releases, BOOK, prefs)

    assert [release.source_id for release in picked] == ["good"]


def test_pick_releases_orders_by_match_then_format_then_source():
    releases = [
        _release("Dune Deluxe Edition", source_id="deluxe-epub"),
        _release(source_id="mobi", format="mobi"),
        _release(source_id="no-format", format=None),
        _release(source_id="libgen-epub", source="libgen"),
        _release(source_id="aa-epub"),
        _release(source_id="pdf", format="pdf"),
    ]

    picked = pick_releases(releases, BOOK, PREFS)

    # Every accepted release is the book, so format and source decide; an exact
    # title only breaks ties within the same format and source.
    assert [release.source_id for release in picked] == [
        "aa-epub",
        "deluxe-epub",
        "libgen-epub",
        "mobi",
        "pdf",
        "no-format",
    ]


def test_pick_releases_skips_excluded_releases_and_disabled_formats():
    by_id = _release(source_id="a")
    by_match = _release(source="prowlarr", source_id="unstable-1", indexer="idx", size="1 MB")
    excluded = release_attempt_keys("direct_download", "a", None) | {
        f"match:{release_match_key(by_match)}"
    }
    prefs = PickerPreferences(
        supported_formats=PREFS.supported_formats, excluded_formats=frozenset({"pdf"})
    )

    # Only sources with unstable ids get a match key.
    assert release_match_key(by_id) is None
    assert pick_releases([by_id, by_match, _release(format="pdf")], BOOK, prefs, excluded) == []


def test_pick_releases_prefers_seeded_torrents():
    releases = [
        _release(source="prowlarr", source_id="few", protocol=ReleaseProtocol.TORRENT, seeders=1),
        _release(source="prowlarr", source_id="many", protocol=ReleaseProtocol.TORRENT, seeders=50),
    ]

    assert [release.source_id for release in pick_releases(releases, BOOK, PREFS)] == [
        "many",
        "few",
    ]


def test_rejection_reasons_say_why_a_release_is_skipped():
    prefs = PickerPreferences(supported_formats=frozenset({"epub"}))
    assert rejection_reason(_release("Dune"), BOOK, prefs) is None
    assert rejection_reason(_release("Dune", format="pdf"), BOOK, prefs) == (
        "PDF is not a wanted format"
    )
    assert rejection_reason(_release("Dune Messiah"), BOOK, prefs).startswith("Names another work")
    blocked = {"id:direct_download:Dune"}
    assert rejection_reason(_release("Dune"), BOOK, prefs, blocked) == (
        "Failed or was cancelled before"
    )


def test_odd_release_titles_are_matched_quickly_and_without_errors():
    started = time.monotonic()
    for title in ("-" + "a" * 200_000 + "!", "(#" + "1" * 200_000, "(" + "# " * 100_000, "Dune ²"):
        assert book_mismatch(_release(title), BOOK) is not None
    assert time.monotonic() - started < 2
