"""Pick which releases of a wanted book to download, best first.

The scoring mirrors the release modal's default order
(src/frontend/src/utils/releaseScoring.ts) so automatic picks agree with what a user
would see on top. Acceptance is stricter than that order: nobody reviews an automatic
pick, so a release must name the book and must not name another work.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from shelfmark.core.author_match import AUTHOR_MISMATCH, author_affinity
from shelfmark.core.languages import normalize_language
from shelfmark.core.text_match import (
    COLLECTION_MARKERS,
    extra_work_tokens,
    title_tokens_match,
    tokens,
)
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
        "novel",
        "fiction",
        "nonfiction",
    }
)

MAX_EBOOK_SIZE_BYTES = 200 * 1024 * 1024
LANGUAGE_OPTION_ALL = "all"


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


def book_title_candidates(book: dict[str, Any]) -> list[str]:
    """Normalized titles a release may carry for this book."""
    return _collect_normalized([book.get("search_title"), book.get("title")])


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


def _context_tokens(book: dict[str, Any], author_candidates: list[str]) -> set[str]:
    context: set[str] = set()
    for key in ("series_name", "subtitle"):
        value = book.get(key)
        context |= set(tokens(value if isinstance(value, str) else None))
    for author in author_candidates:
        context |= set(author.split(" "))
    return context | _RELEASE_NOISE_TOKENS


def names_the_book(release: Release, book: dict[str, Any]) -> bool:
    """Whether a release is this book rather than another work or a bundle."""
    title_candidates = book_title_candidates(book)
    if not title_candidates:
        return False
    author_candidates = book_author_candidates(book)
    release_tokens = set(tokens(release.title))
    context = _context_tokens(book, author_candidates)

    if release_tokens & COLLECTION_MARKERS:
        return False
    if not any(
        title_tokens_match(candidate, release_tokens)
        and not extra_work_tokens(release_tokens, candidate, context)
        for candidate in title_candidates
    ):
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
    return all((normalize_language(part) or part.lower()) in wanted for part in parts)


def release_match_key(release: Release) -> str:
    """Identity of a release that survives an unstable source id (e.g. Prowlarr without a GUID)."""
    title = _normalize_match_text(release.title)
    return f"{release.source}|{release.indexer or ''}|{title}|{release.size or ''}"


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
    """Releases worth trying for ``book``, best first."""
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
        ) + (_AUTHOR_MATCH_BONUS if _has_author_match(release, author_candidates) else 0)
        sort_key = (
            fmt is None,
            -score,
            format_rank.get(fmt or "", len(format_rank)),
            source_rank.get(release.source, len(source_rank)),
            -(release.seeders or 0),
            index,
        )
        ranked.append((sort_key, release))
    ranked.sort(key=lambda entry: entry[0])
    return [release for _, release in ranked]
