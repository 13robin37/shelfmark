"""Pick which releases of a wanted book to download, best first.

The scoring mirrors the release modal's default order
(src/frontend/src/utils/releaseScoring.ts). Acceptance is stricter than that order:
nobody reviews an automatic pick, and a wrong download ends the search, so a release
must name the book and must not name another work, volume or bundle.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from shelfmark.core.author_match import AUTHOR_MISMATCH, author_affinity
from shelfmark.core.languages import LANGUAGE_PLACEHOLDERS, normalize_language
from shelfmark.core.text_match import COLLECTION_MARKERS, PACKAGING_MARKERS, STOPWORDS
from shelfmark.core.wanted_service import release_attempt_keys

if TYPE_CHECKING:
    from collections.abc import Iterable

    from shelfmark.release_sources import Release

_EXACT_TITLE_SCORE = 10000
_PREFIX_TITLE_SCORE = 6000
_SUBSTRING_TITLE_SCORE = 3000
_TOKEN_OVERLAP_SCORE = 2500
_AUTHOR_MATCH_BONUS = 1500
_MIN_TOKEN_LENGTH = 3
_MAX_LENGTH_PENALTY = 100
_TITLE_MATCH_THRESHOLD = 0.85
_YEAR_RANGE = range(1800, 2101)
# Larger numbers are identifiers (ISBNs, catalogue ids), not volume or sequel numbers.
_MAX_VOLUME_NUMBER = 999

_STOP_WORDS = frozenset(
    {"a", "an", "the", "and", "or", "of", "in", "to", "for", "on", "at", "by", "is"}
)

# Words release names add around a title that say nothing about which work it is.
_RELEASE_NOISE_TOKENS = frozenset(
    {
        "epub",
        "mobi",
        "azw",
        "azw3",
        "pdf",
        "fb2",
        "djvu",
        "cbz",
        "cbr",
        "txt",
        "rtf",
        "doc",
        "docx",
        "zip",
        "rar",
        "ebook",
        "ebooks",
        "retail",
        "kindle",
        "scan",
        "ocr",
        "english",
        "german",
        "deutsch",
        "french",
        "spanish",
        "italian",
        "eng",
        "ger",
        "deu",
        "fre",
        "fra",
        "spa",
        "ita",
        "multi",
        "novel",
        "fiction",
        "nonfiction",
    }
)

# A series marker a release name adds, as in "Dune (Dune Chronicles #1)".
_SERIES_MARKER_PATTERN = re.compile(r"\([^()]*#\s*\d+(?:\.\d+)?[^()]*\)")
# A scene release group suffix, as in "Dune.1965.eBook-AlkPub".
_RELEASE_GROUP_PATTERN = re.compile(r"-[A-Za-z0-9]*[A-Za-z][A-Za-z0-9]*$")
_WORD_PATTERN = re.compile(r"\w+", re.UNICODE)
_APOSTROPHES = re.compile(r"['\u2019\u02bc]")
# Volume numbers written as Roman numerals ("Vol. II"); I, V and X alone are too often
# words or initials to count.
_ROMAN_NUMERALS = {
    "ii": 2,
    "iii": 3,
    "iv": 4,
    "vi": 6,
    "vii": 7,
    "viii": 8,
    "ix": 9,
    "xi": 11,
    "xii": 12,
    "xiii": 13,
    "xiv": 14,
    "xv": 15,
    "xvi": 16,
    "xvii": 17,
    "xviii": 18,
    "xix": 19,
    "xx": 20,
}

MAX_EBOOK_SIZE_BYTES = 200 * 1024 * 1024
LANGUAGE_OPTION_ALL = "all"
_UNSTABLE_ID_SOURCES = frozenset({"prowlarr", "newznab"})
_NON_EBOOK_CONTENT_TYPES = frozenset({"audiobook", "other"})


def _normalize_match_text(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", value.lower())).strip()


def _remove_stop_words(text: str) -> str:
    return " ".join(word for word in text.split(" ") if word not in _STOP_WORDS)


def title_match_score(release_title: str, title_candidate: str) -> int:
    """Port of ``getTitleMatchScore`` from releaseScoring.ts."""
    normalized_title = _normalize_match_text(release_title)
    if not normalized_title or not title_candidate:
        return 0
    if normalized_title == title_candidate:
        return _EXACT_TITLE_SCORE

    stripped_title = _remove_stop_words(normalized_title)
    stripped_candidate = _remove_stop_words(title_candidate)

    score = 0
    if normalized_title.startswith(title_candidate) or stripped_title.startswith(
        stripped_candidate
    ):
        score += _PREFIX_TITLE_SCORE
    elif title_candidate in normalized_title or stripped_candidate in stripped_title:
        score += _SUBSTRING_TITLE_SCORE

    candidate_tokens = [
        token for token in stripped_candidate.split(" ") if len(token) >= _MIN_TOKEN_LENGTH
    ]
    if candidate_tokens:
        title_tokens = set(stripped_title.split(" "))
        matched = sum(1 for token in candidate_tokens if token in title_tokens)
        # JS Math.round rounds halves up; Python's round() would round them to even.
        score += int(matched / len(candidate_tokens) * _TOKEN_OVERLAP_SCORE + 0.5)

    score -= min(abs(len(normalized_title) - len(title_candidate)), _MAX_LENGTH_PENALTY)
    return score


def _collect_normalized(values: Iterable[object]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        normalized = _normalize_match_text(value)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def _raw_titles(book: dict[str, Any]) -> list[str]:
    titles = [book.get("search_title"), book.get("title")]
    localized = book.get("titles_by_language")
    if isinstance(localized, dict):
        titles.extend(localized.values())
    return [title for title in titles if isinstance(title, str) and title.strip()]


def book_title_candidates(book: dict[str, Any]) -> list[str]:
    """Normalized titles a release may carry for this book (for scoring)."""
    return _collect_normalized(_raw_titles(book))


def book_author_candidates(book: dict[str, Any]) -> list[str]:
    """Normalized author names of this book."""
    raw_authors = book.get("authors")
    authors: list[object] = raw_authors if isinstance(raw_authors, list) else []
    raw_author = book.get("author")
    author_text = raw_author if isinstance(raw_author, str) else ""
    parts = [part.strip() for part in author_text.split(",") if part.strip()]
    return _collect_normalized([book.get("search_author"), *authors, *parts])


def _release_author(release: Release) -> str | None:
    author = release.extra.get("author") if isinstance(release.extra, dict) else None
    return author if isinstance(author, str) and author.strip() else None


def _has_author_match(release: Release, author_candidates: list[str]) -> bool:
    author = _release_author(release)
    if not author or not author_candidates:
        return False
    release_tokens = set(_normalize_match_text(author).split(" "))
    return any(
        candidate.split(" ") and all(token in release_tokens for token in candidate.split(" "))
        for candidate in author_candidates
    )


def _fold(text: str) -> str:
    """Casefold and drop accents and apostrophes, so "Schätzing" matches "Schatzing"."""
    decomposed = unicodedata.normalize("NFKD", _APOSTROPHES.sub("", text.casefold()))
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def _words(text: object) -> list[str]:
    """Unicode word tokens, so non-Latin titles compare as well as Latin ones."""
    if not isinstance(text, str):
        return []
    return [word for word in _WORD_PATTERN.findall(_fold(text)) if word != "_"]


def _number_of(word: str) -> int | None:
    if word.isdigit():
        return int(word)
    return _ROMAN_NUMERALS.get(word)


def _significant(words: Iterable[str]) -> list[str]:
    return [word for word in words if len(word) >= 2 and word not in STOPWORDS]


def _numbers(words: Iterable[str]) -> set[int]:
    """Standalone numbers other than years: volume and sequel numbers."""
    numbers = {_number_of(word) for word in words}
    return {
        number
        for number in numbers
        if number is not None and number <= _MAX_VOLUME_NUMBER and number not in _YEAR_RANGE
    }


def _release_words(title: str) -> list[str]:
    title = _SERIES_MARKER_PATTERN.sub(" ", title).strip()
    if " " not in title:
        # Only scene-style names ("Dune.1965.eBook-AlkPub") carry a group suffix; in a
        # spaced title the hyphen belongs to a word ("Spider-Man").
        title = _RELEASE_GROUP_PATTERN.sub(" ", title)
    return _words(title)


def _book_words(book: dict[str, Any], author_candidates: list[str]) -> set[str]:
    """Every word the book's own metadata accounts for."""
    words: set[str] = set()
    for title in _raw_titles(book):
        words |= set(_words(title))
    for key in ("series_name", "subtitle"):
        words |= set(_words(book.get(key)))
    for author in author_candidates:
        words |= set(author.split(" "))
    raw_authors = book.get("authors")
    for author in [book.get("author"), *(raw_authors if isinstance(raw_authors, list) else [])]:
        words |= set(_words(author))
    return words


def _book_numbers(book: dict[str, Any]) -> set[int]:
    numbers: set[int] = set()
    for text in [*_raw_titles(book), book.get("subtitle")]:
        numbers |= {number for word in _words(text) if (number := _number_of(word)) is not None}
    position = book.get("series_position")
    if (
        isinstance(position, (int, float))
        and not isinstance(position, bool)
        and float(position).is_integer()
    ):
        numbers.add(int(position))
    return numbers


def names_the_book(release: Release, book: dict[str, Any]) -> bool:
    """Whether a release is this book rather than another work, volume or bundle."""
    titles = _raw_titles(book)
    if not titles:
        return False
    author_candidates = book_author_candidates(book)
    release_words = _release_words(release.title)
    release_word_set = set(release_words)
    book_words = _book_words(book, author_candidates)

    if (release_word_set & COLLECTION_MARKERS) - book_words:
        return False
    # A word the book's own metadata uses ("Xi" in an author's name) is no numeral.
    release_numbers = _numbers(
        word for word in release_words if word.isdigit() or word not in book_words
    )
    if release_numbers - _book_numbers(book):
        return False

    def matches(title: str) -> bool:
        title_words = _significant(_words(title))
        if not title_words:
            return False
        present = sum(1 for word in title_words if word in release_word_set)
        return present / len(title_words) >= _TITLE_MATCH_THRESHOLD

    if not any(matches(title) for title in titles):
        return False
    extra = {
        word
        for word in release_word_set
        if word.isalpha()
        and len(word) >= _MIN_TOKEN_LENGTH
        and word not in STOPWORDS
        and word not in PACKAGING_MARKERS
        and word not in _RELEASE_NOISE_TOKENS
        and word not in book_words
    }
    if extra:
        return False
    release_author = _release_author(release)
    if release_author and author_candidates:
        return any(
            author_affinity(candidate, release_author) != AUTHOR_MISMATCH
            for candidate in author_candidates
        )
    return True


def language_matches(release_language: str | None, languages: list[str] | None) -> bool:
    """Port of ``releaseLanguageMatchesFilter``: unknown or unfiltered languages pass."""
    if not release_language or not languages or LANGUAGE_OPTION_ALL in languages:
        return True
    wanted = {normalize_language(code) or code.lower() for code in languages}
    parts = [part.strip() for part in re.split(r"[,/+&]", release_language) if part.strip()]
    known = [part for part in parts if part.lower() not in LANGUAGE_PLACEHOLDERS]
    return all((normalize_language(part) or part.lower()) in wanted for part in known)


def release_match_key(release: Release) -> str | None:
    """Identity for sources whose ids are not stable (Prowlarr without a GUID)."""
    if release.source not in _UNSTABLE_ID_SOURCES:
        return None
    title = _normalize_match_text(release.title)
    size = release.size_bytes if release.size_bytes is not None else release.size or ""
    return f"{release.source}|{release.indexer or ''}|{title}|{release.format or ''}|{size}"


@dataclass(frozen=True)
class PickerPreferences:
    """Settings that decide which releases qualify and in what order."""

    supported_formats: frozenset[str]
    preferred_formats: tuple[str, ...] = ()
    excluded_formats: frozenset[str] = frozenset()
    source_order: tuple[str, ...] = ()
    languages: tuple[str, ...] = ()


def _format_of(release: Release) -> str | None:
    fmt = release.format.strip().lower() if isinstance(release.format, str) else ""
    return fmt or None


def _qualifies(release: Release, book: dict[str, Any], prefs: PickerPreferences) -> bool:
    if release.content_type in _NON_EBOOK_CONTENT_TYPES:
        return False
    fmt = _format_of(release)
    if fmt is not None and (fmt not in prefs.supported_formats or fmt in prefs.excluded_formats):
        return False
    if not language_matches(release.language, list(prefs.languages) or None):
        return False
    if release.size_bytes is not None and release.size_bytes > MAX_EBOOK_SIZE_BYTES:
        return False
    if release.seeders is not None and release.protocol == "torrent" and release.seeders <= 0:
        return False
    return names_the_book(release, book)


def pick_releases(
    releases: list[Release],
    book: dict[str, Any],
    prefs: PickerPreferences,
    excluded_keys: set[str] | frozenset[str] = frozenset(),
) -> list[Release]:
    """Releases worth trying for ``book``, best first.

    Every accepted release names the book, so the user's preferences decide the order:
    author agreement first, then format, then source; title score only breaks ties.
    """
    title_candidates = book_title_candidates(book)
    author_candidates = book_author_candidates(book)
    format_rank = {fmt: index for index, fmt in enumerate(prefs.preferred_formats)}
    source_rank = {source: index for index, source in enumerate(prefs.source_order)}

    ranked: list[tuple[tuple[Any, ...], Release]] = []
    for index, release in enumerate(releases):
        keys = release_attempt_keys(release.source, release.source_id, release_match_key(release))
        if keys & excluded_keys or not _qualifies(release, book, prefs):
            continue
        fmt = _format_of(release)
        score = max(
            (title_match_score(release.title, candidate) for candidate in title_candidates),
            default=0,
        )
        sort_key = (
            fmt is None,
            not _has_author_match(release, author_candidates),
            format_rank.get(fmt or "", len(format_rank)),
            source_rank.get(release.source, len(source_rank)),
            score < _EXACT_TITLE_SCORE,
            -(release.seeders or 0),
            -score,
            index,
        )
        ranked.append((sort_key, release))
    ranked.sort(key=lambda entry: entry[0])
    return [release for _, release in ranked]
