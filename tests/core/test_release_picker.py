"""Tests for automatic release selection."""

from __future__ import annotations

import pytest

from shelfmark.core.release_picker import (
    PickerPreferences,
    language_matches,
    names_the_book,
    pick_releases,
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


@pytest.mark.parametrize(
    ("title", "candidate", "expected"),
    [
        ("Dune", "dune", 10000),
        ("Dune Messiah", "dune", 6000 + 2500 - 8),
        ("The Dune Book", "dune", 6000 + 2500 - 9),
        ("Messiah of Dune", "dune", 3000 + 2500 - 11),
        ("Something Else", "dune", -10),
        ("", "dune", 0),
    ],
)
def test_title_match_score_matches_the_frontend(title, candidate, expected):
    assert title_match_score(title, candidate) == expected


@pytest.mark.parametrize(
    "title",
    [
        "Dune",
        "Frank Herbert - Dune (1965) [epub] retail",
        "Dune: Deluxe Edition",
        "Dune (Dune Chronicles #1)",
        "Frank Herbert - Dune [ENG] (1965)",
        "Dune.1965.eBook-AlkPub",
        "Dune 9780441172719",
    ],
)
def test_names_the_book_accepts_the_same_work(title):
    assert names_the_book(_release(title), BOOK)


@pytest.mark.parametrize(
    "title",
    ["Dune Messiah", "Children of Dune", "Dune Trilogy Box Set", "The Road to Dune", "Arrakis"],
)
def test_names_the_book_rejects_other_works_and_bundles(title):
    assert not names_the_book(_release(title), BOOK)


def test_names_the_book_rejects_a_contradicting_author():
    assert not names_the_book(_release(extra={"author": "Brian Herbert"}), BOOK)
    assert names_the_book(_release(extra={"author": "Herbert, Frank"}), BOOK)
    assert names_the_book(_release(extra={"author": "Herbert"}), BOOK)


def test_names_the_book_allows_the_subtitle():
    book = {
        **BOOK,
        "title": "Sapiens",
        "subtitle": "A Brief History of Humankind",
        "author": "Yuval Noah Harari",
    }
    assert names_the_book(_release("Sapiens: A Brief History of Humankind"), book)


@pytest.mark.parametrize(
    ("release_language", "languages", "expected"),
    [
        (None, ["en"], True),
        ("en", None, True),
        ("en", ["all"], True),
        ("en", ["en", "de"], True),
        ("english", ["en"], True),
        ("de", ["en"], False),
        ("en, de", ["en"], False),
    ],
)
def test_language_matches(release_language, languages, expected):
    assert language_matches(release_language, languages) is expected


def test_pick_releases_filters_unusable_releases():
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


def test_pick_releases_skips_excluded_and_disabled_formats():
    releases = [_release(source_id="a"), _release(source_id="b", format="pdf")]
    excluded = release_attempt_keys("direct_download", "a", None)
    prefs = PickerPreferences(
        supported_formats=PREFS.supported_formats, excluded_formats=frozenset({"pdf"})
    )

    assert pick_releases(releases, BOOK, prefs, excluded) == []


def test_pick_releases_excludes_by_match_key():
    release = _release(source="prowlarr", source_id="unstable-1", indexer="idx", size="1 MB")
    excluded = {f"match:{release_match_key(release)}"}

    assert pick_releases([release], BOOK, PREFS, excluded) == []


def test_pick_releases_prefers_seeded_torrents():
    releases = [
        _release(source="prowlarr", source_id="few", protocol=ReleaseProtocol.TORRENT, seeders=1),
        _release(source="prowlarr", source_id="many", protocol=ReleaseProtocol.TORRENT, seeders=50),
    ]

    assert [release.source_id for release in pick_releases(releases, BOOK, PREFS)] == [
        "many",
        "few",
    ]


@pytest.mark.parametrize(
    ("book", "title"),
    [
        ({"title": "Saga, Vol. 1"}, "Saga, Vol. 4"),
        ({"title": "Heartstopper: Volume 1"}, "Heartstopper Volume 2"),
        ({"title": "Persepolis"}, "Persepolis 2"),
        ({"title": "Diary of a Wimpy Kid", "series_position": 1}, "Diary of a Wimpy Kid 05"),
        ({"title": "Dune"}, "Dune - Book 2"),
    ],
)
def test_names_the_book_rejects_another_volume(book, title):
    assert not names_the_book(_release(title), {**book, "provider": "x", "provider_id": "1"})


@pytest.mark.parametrize(
    ("book", "title"),
    [
        ({"title": "Dune", "series_position": 1.0}, "Dune - Book 1"),
        ({"title": "Catch-22", "author": "Joseph Heller"}, "Catch-22"),
        ({"title": "1984", "author": "George Orwell"}, "George Orwell - 1984"),
        ({"title": "Bird Box", "author": "Josh Malerman"}, "Bird Box"),
        ({"title": "The Complete Maus"}, "The Complete Maus"),
        (
            {"title": "Senlin Ascends", "series_name": "The Books of Babel"},
            "Senlin Ascends (The Books of Babel #1)",
        ),
        ({"title": "Мастер и Маргарита"}, "Мастер и Маргарита"),
        ({"title": "ノルウェイの森"}, "ノルウェイの森"),
        (
            {
                "title": "Harry Potter and the Philosopher's Stone",
                "titles_by_language": {"de": "Harry Potter und der Stein der Weisen"},
            },
            "Harry Potter und der Stein der Weisen",
        ),
    ],
)
def test_names_the_book_accepts_numbers_markers_and_scripts_the_book_carries(book, title):
    assert names_the_book(_release(title), {**book, "provider": "x", "provider_id": "1"})


def test_format_preference_beats_title_variants():
    releases = [
        _release("Dune.", source_id="mobi", format="mobi"),
        _release(source_id="pdf", format="pdf"),
        _release("Dune (Deluxe Edition)", source_id="epub"),
    ]
    prefs = PickerPreferences(
        supported_formats=PREFS.supported_formats, preferred_formats=("epub", "mobi", "pdf")
    )

    assert [release.source_id for release in pick_releases(releases, BOOK, prefs)] == [
        "epub",
        "mobi",
        "pdf",
    ]


def test_audiobook_releases_are_rejected():
    release = _release("Frank Herbert - Dune (Unabridged) [M4B]", format=None)
    release.content_type = "audiobook"

    assert pick_releases([release], BOOK, PREFS) == []


def test_unknown_language_placeholder_passes():
    assert language_matches("unknown", ["de"]) is True
    assert language_matches("en, unknown", ["en"]) is True


def test_match_key_only_for_sources_with_unstable_ids():
    assert release_match_key(_release(source="direct_download")) is None
    assert release_match_key(_release(source="prowlarr", format="epub")) is not None
