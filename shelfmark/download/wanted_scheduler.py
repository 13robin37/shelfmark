"""Background job that searches for wanted books and downloads them until obtained.

One pass ("cycle") for an item searches every allowed source, ranks all releases with
the release picker and queues them one at a time until one completes. When nothing is
left to try the item waits with a growing interval, and becomes due at once when the
source configuration changes. See docs/dev/wanted-list-concept.md.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from shelfmark.core.config import config
from shelfmark.core.logger import setup_logger
from shelfmark.core.models import QueueStatus
from shelfmark.core.release_picker import PickerPreferences, pick_releases, release_match_key
from shelfmark.core.request_helpers import now_utc_iso
from shelfmark.core.wanted_access import WANTED_CONTENT_TYPE, user_may_download
from shelfmark.core.wanted_config import (
    backoff_hours,
    max_searches_per_hour,
    preferred_formats,
    supported_formats,
    wanted_enabled,
    wanted_sources,
)
from shelfmark.core.wanted_service import (
    ATTEMPT_CANCELLED,
    ATTEMPT_COMPLETE,
    ATTEMPT_ERROR,
    STATE_DOWNLOADING,
    STATE_OBTAINED,
    STATE_PAUSED,
    STATE_SEARCHING,
    STATE_WAITING,
    WantedService,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from shelfmark.core.user_db import UserDB
    from shelfmark.metadata_providers import BookMetadata
    from shelfmark.release_sources import Release

logger = setup_logger(__name__)

TICK_SECONDS = 60.0
# Background searches wait this long after a user's own search; the bypasser solves one
# challenge at a time, and the user's search must not queue behind ours.
USER_SEARCH_GRACE_SECONDS = 180.0
# A candidate list older than this is searched afresh instead of continued: Prowlarr and
# Newznab download links expire after an hour.
CANDIDATES_MAX_AGE_SECONDS = 30 * 60
_RATE_WINDOW_SECONDS = 3600.0
_STALLED_MESSAGE_PREFIX = "Download stalled"
_LOOP_ERROR_MAX_DELAY = 300.0
# Tabs whose saved values decide which sources, mirrors and indexers exist.
_SOURCE_SETTINGS_TABS = (
    "download_sources",
    "mirrors",
    "libgen_config",
    "prowlarr_config",
    "newznab_config",
    "irc",
    "wanted",
)


@dataclass
class _Cycle:
    """Releases still to try in the current pass for one item."""

    number: int
    started: float
    releases: list[Release] = field(default_factory=list)


def _iso_after(hours: float) -> str:
    return (datetime.now(UTC) + timedelta(hours=hours)).isoformat(timespec="seconds")


def _positive_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def release_to_queue_payload(
    release: Release, book: dict[str, Any], wanted_id: int
) -> dict[str, Any]:
    """The /api/releases/download body the release modal would send for this pick."""
    extra = release.extra if isinstance(release.extra, dict) else {}
    downloads = extra.get("downloads")
    payload: dict[str, Any] = {
        "source": release.source,
        "source_id": release.source_id,
        "title": book.get("title"),
        "author": book.get("author"),
        "year": book.get("year"),
        "format": release.format,
        "size": release.size,
        "size_bytes": release.size_bytes,
        "downloads": downloads if isinstance(downloads, int) else None,
        "download_url": release.download_url,
        "protocol": str(release.protocol) if release.protocol else None,
        "indexer": release.indexer,
        "seeders": release.seeders,
        "extra": extra,
        "preview": book.get("preview"),
        "content_type": WANTED_CONTENT_TYPE,
        "series_name": book.get("series_name"),
        "series_position": book.get("series_position"),
        "subtitle": book.get("subtitle"),
        "language": release.language,
        "_wanted_id": wanted_id,
    }
    return {key: value for key, value in payload.items() if value is not None}


class WantedScheduler:
    """Single background thread driving the wanted list."""

    def __init__(
        self,
        service: WantedService,
        user_db: UserDB | None,
        *,
        resolve_auth_mode: Callable[[], str],
        queue_release: Callable[..., tuple[bool, str | None]],
        on_change: Callable[[int | None], None] | None = None,
    ) -> None:
        """Wire the scheduler to its store, the download queue and change listeners."""
        self._service = service
        self._user_db = user_db
        self._resolve_auth_mode = resolve_auth_mode
        self._queue_release = queue_release
        self._on_change = on_change
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._cycles: dict[int, _Cycle] = {}
        self._searches: deque[float] = deque()
        self._source_fingerprint: str | None = None

    # -- lifecycle -------------------------------------------------------------------

    def start(self) -> None:
        """Start the background thread once."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="WantedScheduler")
        self._thread.start()

    def wake(self) -> None:
        """Run the next tick now instead of after the tick interval."""
        self._wake.set()

    def _run(self) -> None:
        consecutive_errors = 0
        while True:
            self._wake.wait(TICK_SECONDS)
            self._wake.clear()
            try:
                self.tick()
                consecutive_errors = 0
            # The only thread driving the wanted list; it must outlive any one bad tick.
            except Exception as exc:  # noqa: BLE001
                consecutive_errors += 1
                logger.error_trace("Wanted scheduler tick failed: %s", exc)
                time.sleep(
                    min(TICK_SECONDS * 2 ** min(consecutive_errors - 1, 5), _LOOP_ERROR_MAX_DELAY)
                )

    def _changed(self, wanted_id: int | None) -> None:
        if self._on_change is not None:
            self._on_change(wanted_id)

    # -- tick ------------------------------------------------------------------------

    def tick(self) -> None:
        """One scheduling step: continue running cycles, then start at most one search."""
        if not wanted_enabled():
            return
        self._detect_source_changes()
        self._continue_cycles()
        if self._should_yield():
            return
        due = self._service.due_items(now_utc_iso(), 1)
        if due:
            self.run_cycle(due[0]["id"])

    def _should_yield(self) -> bool:
        from shelfmark.download import warmup

        since_user_search = warmup.seconds_since_user_search()
        if since_user_search is not None and since_user_search < USER_SEARCH_GRACE_SECONDS:
            return True
        now = time.monotonic()
        while self._searches and now - self._searches[0] > _RATE_WINDOW_SECONDS:
            self._searches.popleft()
        return len(self._searches) >= max_searches_per_hour()

    def _detect_source_changes(self) -> None:
        from shelfmark.core.settings_registry import load_config_file

        snapshot = {
            "sources": sorted(self._available_sources()),
            "tabs": {tab: load_config_file(tab) for tab in _SOURCE_SETTINGS_TABS},
        }
        fingerprint = hashlib.sha256(
            json.dumps(snapshot, sort_keys=True, default=str).encode()
        ).hexdigest()
        previous = self._source_fingerprint
        self._source_fingerprint = fingerprint
        if previous is not None and previous != fingerprint:
            made_due = self._service.mark_all_due()
            logger.info("Wanted: source settings changed, %d item(s) due now", made_due)
            if made_due:
                self._changed(None)

    def _continue_cycles(self) -> None:
        """Queue the next candidate for items whose last attempt has ended."""
        for item in self._service.items_in_state(STATE_SEARCHING):
            cycle = self._cycles.get(item["id"])
            if cycle is None:
                # No cycle in memory (a restart or a crash mid-search): start over later.
                self._end_cycle(item, found_nothing=True, source_errors=True)
            elif time.monotonic() - cycle.started > CANDIDATES_MAX_AGE_SECONDS:
                self._cycles.pop(item["id"], None)
                self._service.update_item(
                    item["id"],
                    expected_state=STATE_SEARCHING,
                    state=STATE_WAITING,
                    next_search_at=now_utc_iso(),
                )
                self._changed(item["id"])
            else:
                self._try_next(item["id"])

    # -- sources, users, book --------------------------------------------------------

    def _available_sources(self) -> list[str]:
        from shelfmark.release_sources import list_available_sources

        available = {
            source["name"]
            for source in list_available_sources()
            if source["enabled"] and WANTED_CONTENT_TYPE in source["supported_content_types"]
        }
        return [source for source in wanted_sources() if source in available]

    def _pending_subscribers(self, wanted_id: int) -> list[dict[str, Any]]:
        return [
            sub
            for sub in self._service.list_subscribers(wanted_id)
            if sub.get("delivered_at") is None
        ]

    def _users_allowed(
        self, subscribers: list[dict[str, Any]], source: str
    ) -> list[dict[str, Any]]:
        auth_mode = self._resolve_auth_mode()
        return [
            sub
            for sub in subscribers
            if user_may_download(self._user_db, sub.get("user_id"), source, auth_mode=auth_mode)
        ]

    @staticmethod
    def _search_languages(subscribers: list[dict[str, Any]]) -> list[str] | None:
        languages: set[str] = set()
        for sub in subscribers:
            chosen = sub.get("languages")
            if not chosen:
                default = config.get("BOOK_LANGUAGE", None, user_id=sub.get("user_id"))
                chosen = [default] if isinstance(default, str) else default
            if not isinstance(chosen, list) or not chosen or "all" in chosen:
                return None
            languages |= {str(code) for code in chosen}
        return sorted(languages) or None

    @staticmethod
    def _resolve_book(book_data: dict[str, Any]) -> BookMetadata:
        from shelfmark.metadata_providers import (
            BookMetadata,
            get_provider,
            get_provider_kwargs,
            is_provider_registered,
        )

        provider = str(book_data.get("provider") or "")
        provider_id = str(book_data.get("provider_id") or "")
        if is_provider_registered(provider):
            try:
                resolved = get_provider(provider, **get_provider_kwargs(provider)).get_book(
                    provider_id
                )
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                logger.warning(
                    "Wanted: metadata lookup failed for %s/%s: %s", provider, provider_id, exc
                )
                resolved = None
            if resolved is not None:
                return resolved
        raw_authors = book_data.get("authors")
        authors = [str(a) for a in raw_authors] if isinstance(raw_authors, list) else []
        if not authors and isinstance(book_data.get("author"), str):
            authors = [a.strip() for a in book_data["author"].split(",") if a.strip()]
        return BookMetadata(
            provider=provider,
            provider_id=provider_id,
            title=str(book_data.get("title") or ""),
            authors=authors,
            isbn_10=book_data.get("isbn_10"),
            isbn_13=book_data.get("isbn_13"),
            subtitle=book_data.get("subtitle"),
            search_title=book_data.get("search_title"),
            search_author=book_data.get("search_author"),
            series_name=book_data.get("series_name"),
        )

    @staticmethod
    def _picker_book(book_data: dict[str, Any], book: BookMetadata) -> dict[str, Any]:
        enriched = dict(book_data)
        for key in ("search_title", "search_author", "subtitle", "series_name"):
            value = getattr(book, key, None)
            if value and not enriched.get(key):
                enriched[key] = value
        if book.authors and not enriched.get("authors"):
            enriched["authors"] = list(book.authors)
        return enriched

    # -- cycle -----------------------------------------------------------------------

    def run_cycle(self, wanted_id: int) -> None:
        """Search all allowed sources for an item and start trying its releases."""
        item = self._service.get_item(wanted_id)
        if item is None or item["state"] != STATE_WAITING:
            return
        cycle_number = int(item["search_count"]) + 1
        item = self._service.update_item(
            wanted_id,
            expected_state=STATE_WAITING,
            state=STATE_SEARCHING,
            search_count=cycle_number,
            last_search_at=now_utc_iso(),
        )
        if item is None:
            return
        self._searches.append(time.monotonic())
        self._changed(wanted_id)

        pending = self._pending_subscribers(wanted_id)
        if not pending:
            self._mark_obtained(item, reason=item.get("obtained_reason") or "downloaded")
            return

        book = self._resolve_book(item["book_data"])
        if self._in_library(book):
            logger.info("Wanted: %s is already in the library", item["title"])
            self._service.mark_delivered(wanted_id, [sub.get("user_id") for sub in pending], None)
            self._mark_obtained(item, reason="in_library")
            return

        sources = [
            source for source in self._available_sources() if self._users_allowed(pending, source)
        ]
        if not sources:
            self._end_cycle(
                item,
                found_nothing=True,
                source_errors=False,
                result="No allowed source is available",
            )
            return

        releases, answered, failed = self._search(sources, book, pending)
        prefs = PickerPreferences(
            supported_formats=supported_formats(),
            preferred_formats=preferred_formats()[0],
            excluded_formats=preferred_formats()[1],
            source_order=tuple(sources),
            languages=tuple(self._search_languages(pending) or ()),
        )
        picked = pick_releases(
            releases,
            self._picker_book(item["book_data"], book),
            prefs,
            self._service.excluded_release_keys(wanted_id, cycle_number),
        )
        logger.info(
            "Wanted: %s - %d release(s) from %s, %d worth trying%s",
            item["title"],
            len(releases),
            ", ".join(answered) or "no source",
            len(picked),
            f" ({', '.join(failed)} failed)" if failed else "",
        )
        self._service.update_item(wanted_id, searched_sources=answered)
        if not picked:
            result = f"Searched {len(answered)} source(s), no matching release"
            if failed:
                result += f"; {', '.join(failed)} failed"
            self._end_cycle(item, found_nothing=True, source_errors=bool(failed), result=result)
            return
        self._cycles[wanted_id] = _Cycle(
            number=cycle_number, started=time.monotonic(), releases=picked
        )
        self._try_next(wanted_id)

    def _in_library(self, book: BookMetadata) -> bool:
        from shelfmark.core import library_index

        try:
            return library_index.is_in_library(book, WANTED_CONTENT_TYPE)
        except (OSError, RuntimeError, ValueError) as exc:
            logger.warning("Wanted: library check failed: %s", exc)
            return False

    def _search(
        self, sources: list[str], book: BookMetadata, pending: list[dict[str, Any]]
    ) -> tuple[list[Release], list[str], list[str]]:
        from shelfmark.core import search_deadline
        from shelfmark.core.release_search import search_source_releases

        languages = self._search_languages(pending)
        user_id = pending[0].get("user_id")
        releases: list[Release] = []
        answered: list[str] = []
        failed: list[str] = []
        with search_deadline.search_deadline():
            for source in sources:
                if search_deadline.expired():
                    failed.append(source)
                    continue
                _source, found, error = search_source_releases(
                    source,
                    book,
                    languages=languages,
                    content_type=WANTED_CONTENT_TYPE,
                    user_id=user_id,
                )
                if error is not None:
                    failed.append(source)
                    continue
                answered.append(source)
                releases.extend(found)
        return releases, answered, failed

    def _try_next(self, wanted_id: int) -> None:
        """Queue the best remaining release of the running cycle, or end the cycle."""
        cycle = self._cycles.get(wanted_id)
        item = self._service.get_item(wanted_id)
        if item is None or item["state"] != STATE_SEARCHING:
            self._cycles.pop(wanted_id, None)
            return
        if cycle is None:
            self._end_cycle(item, found_nothing=True, source_errors=True)
            return
        pending = self._pending_subscribers(wanted_id)
        while cycle.releases:
            release = cycle.releases.pop(0)
            allowed = self._users_allowed(pending, release.source)
            if not allowed:
                continue
            owner = allowed[0]
            if self._queue_attempt(item, release, owner, cycle.number):
                return
        self._cycles.pop(wanted_id, None)
        self._end_cycle(
            item, found_nothing=True, source_errors=False, result="Every matching release failed"
        )

    def _queue_attempt(
        self, item: dict[str, Any], release: Release, owner: dict[str, Any], cycle_number: int
    ) -> bool:
        wanted_id = item["id"]
        # Claim the task before queueing it: a download can fail before queue_release returns.
        claimed = self._service.update_item(
            wanted_id,
            expected_state=STATE_SEARCHING,
            state=STATE_DOWNLOADING,
            current_task_id=release.source_id,
        )
        if claimed is None:
            return True
        self._service.record_attempt(
            wanted_id,
            cycle=cycle_number,
            source=release.source,
            source_id=release.source_id,
            match_key=release_match_key(release),
            release_title=release.title,
            file_format=release.format,
            size=release.size,
            indexer=release.indexer,
        )
        payload = release_to_queue_payload(release, item["book_data"], wanted_id)
        username = owner.get("username")
        success, error = self._queue_release(
            payload, 0, user_id=_positive_int(owner.get("user_id")), username=username
        )
        if success:
            logger.info(
                "Wanted: queued %s from %s for %s", release.title, release.source, item["title"]
            )
            self._changed(wanted_id)
            return True
        logger.info("Wanted: could not queue %s from %s: %s", release.title, release.source, error)
        self._service.finish_attempt(wanted_id, release.source_id, ATTEMPT_CANCELLED, error)
        self._service.update_item(
            wanted_id, expected_state=STATE_DOWNLOADING, state=STATE_SEARCHING, current_task_id=None
        )
        return False

    def _end_cycle(
        self,
        item: dict[str, Any],
        *,
        found_nothing: bool,
        source_errors: bool,
        result: str | None = None,
    ) -> None:
        """Send an item back to waiting with its next search scheduled."""
        self._cycles.pop(item["id"], None)
        hours = backoff_hours()
        level = int(item.get("backoff_level") or 0)
        delay = hours[min(level, len(hours) - 1)]
        if source_errors:
            delay = min(delay, hours[0])
        elif found_nothing:
            level += 1
        updated = self._service.update_item(
            item["id"],
            expected_state=item["state"]
            if item["state"] in (STATE_SEARCHING, STATE_DOWNLOADING)
            else None,
            state=STATE_WAITING,
            current_task_id=None,
            backoff_level=level,
            next_search_at=_iso_after(delay),
            **({"last_result": result} if result is not None else {}),
        )
        if updated is not None:
            self._changed(item["id"])

    def _mark_obtained(self, item: dict[str, Any], *, reason: str) -> None:
        self._cycles.pop(item["id"], None)
        self._service.update_item(
            item["id"],
            state=STATE_OBTAINED,
            current_task_id=None,
            next_search_at=None,
            obtained_at=now_utc_iso(),
            obtained_reason=reason,
            last_result="Obtained" if reason == "downloaded" else "Already in the library",
        )
        self._changed(item["id"])

    # -- download outcomes ---------------------------------------------------------

    def on_task_terminal(self, task_id: str, status: QueueStatus, task: Any) -> None:
        """React to the end of a download queued for a wanted item."""
        wanted_id = _positive_int(getattr(task, "wanted_id", None))
        if wanted_id is None:
            return
        item = self._service.get_item(wanted_id)
        if item is None or item.get("current_task_id") != task_id:
            return
        message = getattr(task, "status_message", None)
        message = message.strip() if isinstance(message, str) and message.strip() else None

        if status == QueueStatus.COMPLETE:
            self._service.finish_attempt(wanted_id, task_id, ATTEMPT_COMPLETE)
            self._deliver(item, task)
        elif status == QueueStatus.CANCELLED and not (message or "").startswith(
            _STALLED_MESSAGE_PREFIX
        ):
            self._service.finish_attempt(wanted_id, task_id, ATTEMPT_CANCELLED, message)
            self._cycles.pop(wanted_id, None)
            self._service.update_item(
                wanted_id,
                expected_state=STATE_DOWNLOADING,
                state=STATE_PAUSED,
                current_task_id=None,
                last_result="Paused: the download was cancelled",
            )
            self._changed(wanted_id)
        else:
            self._service.finish_attempt(
                wanted_id, task_id, ATTEMPT_ERROR, message or "Download failed"
            )
            self._service.update_item(
                wanted_id,
                expected_state=STATE_DOWNLOADING,
                state=STATE_SEARCHING,
                current_task_id=None,
                last_result=f"Last attempt failed: {message or 'Download failed'}",
            )
            self._changed(wanted_id)
            self.wake()

    def _deliver(self, item: dict[str, Any], task: Any) -> None:
        wanted_id = item["id"]
        source = str(getattr(task, "source", "") or "")
        pending = self._pending_subscribers(wanted_id)
        receivers = self._users_allowed(pending, source)
        self._service.mark_delivered(
            wanted_id, [sub.get("user_id") for sub in receivers], getattr(task, "task_id", None)
        )
        self._cycles.pop(wanted_id, None)
        if len(receivers) < len(pending):
            # Someone may not download from this source; keep looking for them.
            self._service.update_item(
                wanted_id,
                state=STATE_WAITING,
                current_task_id=None,
                backoff_level=0,
                next_search_at=now_utc_iso(),
                last_result="Obtained for some subscribers",
            )
            self._changed(wanted_id)
            return
        self._mark_obtained(item, reason="downloaded")
