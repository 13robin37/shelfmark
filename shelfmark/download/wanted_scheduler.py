"""Background job that searches for wanted books and downloads them until obtained.

One pass ("cycle") for an item searches every allowed source, ranks all releases with
the release picker and queues them one at a time until one completes. When nothing is
left to try the item waits with a growing interval, and becomes due at once when the
source configuration changes. See docs/dev/wanted-list-concept.md.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from shelfmark.config.env import TMP_DIR
from shelfmark.core.config import config
from shelfmark.core.logger import setup_logger
from shelfmark.core.models import TERMINAL_QUEUE_STATUSES, QueueStatus
from shelfmark.core.release_picker import (
    PickerPreferences,
    may_name_the_book,
    pick_releases,
    rejection_reason,
    release_match_key,
)
from shelfmark.core.request_helpers import now_utc_iso
from shelfmark.core.utils import ARCHIVE_FORMATS
from shelfmark.core.wanted_access import WANTED_CONTENT_TYPE, user_may_download
from shelfmark.core.wanted_config import (
    backoff_hours,
    feed_interval_minutes,
    format_order,
    format_wait,
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
    ATTEMPT_REJECTED,
    STATE_DOWNLOADING,
    STATE_OBTAINED,
    STATE_PAUSED,
    STATE_SEARCHING,
    STATE_WAITING,
    WantedService,
)
from shelfmark.download.fs import run_blocking_io

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

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
    "users",
)
# Failure kinds that may be the release's fault. Anything else (a worker exception, a
# disabled source) happened on our side and says nothing about the release.
_RELEASE_FAILURE_TYPES = frozenset({None, "StatusCallbackError", "UnknownFailure"})
# Messages of failures that come from our own destination, output, download client or an
# outage - nothing that says the release itself is bad.
_LOCAL_FAILURE_PATTERN = re.compile(
    r"destination|not writable|permission|custom script|smtp|email|booklore|grimmory|"
    r"no space|disk full|cannot create|could not connect|connection|timed out|timeout|"
    r"authentication|unauthorized|qbittorrent|transmission|deluge|rtorrent|sabnzbd|"
    r"nzbget|download client",
    re.IGNORECASE,
)
# A user's own search makes background searches wait, but never longer than this.
_MAX_YIELD_SECONDS = 30 * 60
_DUE_BATCH = 5
_WATCH_INTERVAL = timedelta(hours=6)
# A release date is checked against the metadata again at least this often.
_RELEASE_RECHECK = timedelta(days=7)
# Sources with feeds of their newest releases.
_FEED_SOURCES = ("prowlarr", "newznab")
# Feed releases already checked, remembered so each is matched against bookmarks once.
_FEED_SEEN_LIMIT = 20_000


@dataclass
class _Cycle:
    """Releases still to try in the current pass for one item."""

    number: int
    started: float
    releases: list[Release] = field(default_factory=list)
    queue_error: str | None = None
    announced: bool = False
    # "manual" (a user's pick) or "feed": when its releases fail, no backoff follows.
    origin: str = "search"


@dataclass(frozen=True)
class _FormatHold:
    until: datetime
    message: str


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    # SQLite's CURRENT_TIMESTAMP is UTC without an offset.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _search_starts(book_data: dict[str, Any], book: BookMetadata | None = None) -> datetime | None:
    """When searching may start for a book not released yet (a day early); None if it may now.

    Only a full release date counts: a bare year is often an edition's, not the book's.
    """
    date = (book.release_date if book is not None else None) or book_data.get("release_date")
    if not isinstance(date, str):
        return None
    try:
        start = datetime.fromisoformat(date).replace(tzinfo=UTC) - timedelta(days=1)
    except ValueError, OverflowError:
        return None
    return start if start > datetime.now(UTC) else None


def release_from_dict(data: object) -> Release | None:
    """A release as the frontend holds it (an /api/releases entry), or None if malformed."""
    from dataclasses import fields

    from shelfmark.release_sources import Release

    if not isinstance(data, dict):
        return None
    if not all(isinstance(data.get(key), str) and data[key] for key in ("source", "source_id")):
        return None
    if not isinstance(data.get("title"), str):
        return None
    known = {item.name for item in fields(Release)}
    values = {key: value for key, value in data.items() if key in known}
    for key in ("size_bytes", "seeders"):
        value = values.get(key)
        values[key] = value if isinstance(value, int) and not isinstance(value, bool) else None
    for key in ("format", "language", "size", "download_url", "info_url", "indexer", "peers"):
        if not isinstance(values.get(key), str):
            values[key] = None
    if not isinstance(values.get("extra"), dict):
        values["extra"] = {}
    content_type = values.get("content_type")
    values["content_type"] = content_type if isinstance(content_type, str) else None
    protocol = values.get("protocol")
    values["protocol"] = protocol if isinstance(protocol, str) and protocol else None
    return Release(**values)


def _release_format(release: Release) -> str:
    return release.format.strip().lower() if isinstance(release.format, str) else ""


def _iso_after(hours: float) -> str:
    return (datetime.now(UTC) + timedelta(hours=hours)).isoformat(timespec="seconds")


def _positive_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


_USER_TEMPLATE_KEYS = ("TEMPLATE_RENAME", "TEMPLATE_ORGANIZE")


def _held_copies_dir() -> Path:
    return TMP_DIR / "wanted"


def delivery_target(user_id: int | None, username: str | None) -> str:
    """Where a download for this user ends up; subscribers sharing it share one delivery."""
    from shelfmark.core.utils import get_destination

    mode = str(config.get("BOOKS_OUTPUT_MODE", "folder", user_id=user_id) or "folder")
    mode = mode.strip().lower()
    if mode == "email":
        recipient = str(config.get("EMAIL_RECIPIENT", "", user_id=user_id) or "").strip().lower()
        return f"email:{recipient}"
    if mode == "booklore":
        library = config.get("BOOKLORE_LIBRARY_ID", "", user_id=user_id)
        path = config.get("BOOKLORE_PATH_ID", "", user_id=user_id)
        return f"booklore:{library}:{path}"
    folder = f"folder:{get_destination(is_audiobook=False, user_id=user_id, username=username)}"
    templates = (config.get(key, "", user_id=user_id) for key in _USER_TEMPLATE_KEYS)
    if any(isinstance(template, str) and "{user}" in template.lower() for template in templates):
        # File names carry the user, so each user is a target of their own.
        return f"{folder}|user:{username or user_id}"
    return folder


def _copy_path(source: Path, target_dir: Path) -> Path:
    run_blocking_io(target_dir.mkdir, parents=True, exist_ok=True)
    target = target_dir / source.name
    if run_blocking_io(source.is_dir):
        run_blocking_io(shutil.copytree, source, target)
    else:
        run_blocking_io(shutil.copy2, source, target)
    return target


def _remove_path(path: Path) -> None:
    if run_blocking_io(path.is_dir):
        run_blocking_io(shutil.rmtree, path, ignore_errors=True)
    else:
        run_blocking_io(path.unlink, missing_ok=True)


def release_to_queue_payload(release: Release, book: dict[str, Any]) -> dict[str, Any]:
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
        task_lookup: Callable[[str], tuple[QueueStatus | None, Any]] | None = None,
        cancel_task: Callable[[str], bool] | None = None,
        on_change: Callable[[int | None], None] | None = None,
    ) -> None:
        """Wire the scheduler to its store, the download queue and change listeners."""
        self._service = service
        self._user_db = user_db
        self._resolve_auth_mode = resolve_auth_mode
        self._queue_release = queue_release
        self._task_lookup = task_lookup
        self._cancel_task = cancel_task
        self._on_change = on_change
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._cycles: dict[int, _Cycle] = {}
        self._searches: deque[float] = deque()
        self._source_fingerprint: str | None = None
        self._feed_checked_at: float | None = None
        # Items a user asked to search now, even before their release date.
        self._forced: set[int] = set()
        self._feed_seen: dict[str, None] = {}
        # Download worker threads and the terminal hook touch these, the scheduler too.
        self._delivery_lock = threading.Lock()
        self._payloads: dict[str, dict[str, Any]] = {}
        self._held: dict[str, list[tuple[list[dict[str, Any]], Path]]] = {}
        # Extra delivery task id -> (wanted id, subscriber ids it delivers to, its copy).
        self._extras: dict[str, tuple[int, list[int | None], Path]] = {}
        # The terminal hook (worker / request threads) and reconciliation (this thread)
        # may see the same ending; only one may act on it.
        self._terminal_lock = threading.RLock()

    # -- lifecycle -------------------------------------------------------------------

    def start(self) -> None:
        """Start the background thread once."""
        if self._thread is not None and self._thread.is_alive():
            return
        # Copies held for deliveries do not survive a restart; neither does their task.
        _remove_path(_held_copies_dir())
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
        self._service.prune_orphans()
        self._cancel_unwanted()
        self._reconcile_downloads()
        if not wanted_enabled():
            # Downloads already running finish and are recorded; nothing new starts.
            self._park_cycles()
            return
        self._detect_source_changes()
        self._continue_cycles()
        self._check_feeds()
        self._sync_watch()
        # Books not out yet only get their date checked; keep going to one that searches.
        for item in self._service.due_items(now_utc_iso(), _DUE_BATCH):
            if self._should_yield(item) or self.run_cycle(item["id"]):
                break

    def _check_feeds(self) -> None:
        """Match the newest releases of indexer feeds against every waiting bookmark."""
        interval = feed_interval_minutes()
        now = time.monotonic()
        if interval <= 0 or (
            self._feed_checked_at is not None and now - self._feed_checked_at < interval * 60
        ):
            return
        if self._user_searched_recently():
            return
        self._feed_checked_at = now
        sources = [source for source in self._available_sources() if source in _FEED_SOURCES]
        # Never-searched items get their first search soon, with the metadata checks.
        items = [
            item
            for item in self._service.items_in_state(STATE_WAITING)
            if int(item.get("search_count") or 0) > 0 and _search_starts(item["book_data"]) is None
        ]
        if not sources or not items:
            return
        fresh = self._fresh_feed_releases(sources)
        if not fresh:
            return
        logger.debug("Wanted: %d new release(s) in indexer feeds", len(fresh))
        for item in items:
            # Each match runs the picker; let requests in between on the gevent loop.
            time.sleep(0)
            try:
                self._match_feed(item, fresh, sources)
            except Exception as exc:  # noqa: BLE001 - one item must not cost the others the feed
                logger.warning("Wanted: feed match failed for %s: %s", item["title"], exc)

    def _sync_watch(self) -> None:
        """Bookmark what is new in the watched series, author or list synced longest ago."""
        from shelfmark.core.wanted_watches import (
            book_data_from_metadata,
            book_key,
            fetch_watch_books,
        )

        synced_before = (datetime.now(UTC) - _WATCH_INTERVAL).isoformat(timespec="seconds")
        watch = self._service.due_watch(synced_before)
        if watch is None:
            return
        owner = {"user_id": watch.get("user_id"), "approved_by": watch.get("approved_by")}
        allowed = any(self._users_allowed([owner], source) for source in self._available_sources())
        try:
            books = fetch_watch_books(watch["provider"], watch["fields"]) if allowed else []
        except Exception as exc:  # noqa: BLE001 - a provider outage must not stop the tick
            logger.warning("Wanted: could not read %s: %s", watch["label"], exc)
            self._service.update_watch(
                watch["id"],
                seen=watch["seen"],
                last_result=f"Could not read it: {exc}",
                synced_at=now_utc_iso(),
            )
            return
        seen = set(watch["seen"])
        added = 0
        for book in books:
            if book_key(book) in seen:
                continue
            try:
                self._service.add(
                    user_id=watch.get("user_id"),
                    book_data=book_data_from_metadata(book),
                    approved_by=watch.get("approved_by"),
                )
                added += 1
            except (ValueError, RuntimeError) as exc:
                logger.warning("Wanted: could not bookmark %s: %s", book.title, exc)
        if not allowed:
            result = "Not allowed to download from any wanted-list source"
        else:
            result = f"Bookmarked {added} new book(s)" if added else "Nothing new"
        self._service.update_watch(
            watch["id"],
            seen=sorted(seen | {book_key(book) for book in books}),
            last_result=result,
            synced_at=now_utc_iso(),
        )
        if added:
            logger.info("Wanted: %s - bookmarked %d new book(s)", watch["label"], added)
            self._changed(None)

    def _fresh_feed_releases(self, sources: list[str]) -> list[Release]:
        from shelfmark.release_sources import get_source

        fresh: list[Release] = []
        for name in sources:
            try:
                releases = get_source(name).recent_releases(WANTED_CONTENT_TYPE) or []
            except Exception as exc:  # noqa: BLE001 - a broken feed must not stop the others
                logger.warning("Wanted: could not read the %s feeds: %s", name, exc)
                continue
            for release in releases:
                key = f"{release.source}:{release.source_id}"
                if key in self._feed_seen:
                    continue
                self._feed_seen[key] = None
                fresh.append(release)
        while len(self._feed_seen) > _FEED_SEEN_LIMIT:
            self._feed_seen.pop(next(iter(self._feed_seen)))
        return fresh

    def _match_feed(
        self, item: dict[str, Any], fresh: list[Release], feed_sources: list[str]
    ) -> None:
        candidates = [release for release in fresh if may_name_the_book(release, item["book_data"])]
        if not candidates:
            return
        wanted_id = item["id"]
        pending = self._pending_subscribers(wanted_id)
        sources = [source for source in feed_sources if self._users_allowed(pending, source)]
        candidates = [release for release in candidates if release.source in sources]
        if not pending or not candidates:
            return
        prefs = self._preferences(pending, sources)
        picked = pick_releases(
            candidates,
            item["book_data"],
            prefs,
            self._service.blocked_release_keys(wanted_id),
        )
        picked, _hold = self._hold_for_format(picked, pending, prefs.preferred_formats)
        if not picked:
            return
        if self._in_library(self._book_from_data(item["book_data"])):
            # The regular search sorts out who already has it.
            self._service.update_item(
                wanted_id, expected_state=STATE_WAITING, next_search_at=now_utc_iso()
            )
            return
        from shelfmark.release_sources import get_source_display_name

        cycle_number = int(item["search_count"]) + 1
        claimed = self._service.update_item(
            wanted_id,
            expected_state=STATE_WAITING,
            state=STATE_SEARCHING,
            search_count=cycle_number,
            last_result=f"Found in the {get_source_display_name(picked[0].source)} feed",
        )
        if claimed is None:
            return
        logger.info("Wanted: %s turned up in an indexer feed", item["title"])
        self._cycles[wanted_id] = _Cycle(
            number=cycle_number, started=time.monotonic(), releases=picked, origin="feed"
        )
        self._changed(wanted_id)
        self._try_next(wanted_id)

    @staticmethod
    def _user_searched_recently() -> bool:
        from shelfmark.download import warmup

        since_user_search = warmup.seconds_since_user_search()
        return since_user_search is not None and since_user_search < USER_SEARCH_GRACE_SECONDS

    def _should_yield(self, item: dict[str, Any]) -> bool:
        from shelfmark.download import warmup

        since_user_search = warmup.seconds_since_user_search()
        overdue = (
            datetime.now(UTC) - datetime.fromisoformat(item["next_search_at"])
        ).total_seconds()
        if (
            since_user_search is not None
            and since_user_search < USER_SEARCH_GRACE_SECONDS
            and overdue < _MAX_YIELD_SECONDS
        ):
            return True
        now = time.monotonic()
        while self._searches and now - self._searches[0] > _RATE_WINDOW_SECONDS:
            self._searches.popleft()
        return len(self._searches) >= max_searches_per_hour()

    def _detect_source_changes(self) -> None:
        from shelfmark.core.settings_registry import load_config_file

        snapshot = {
            "sources": self._available_sources(),
            "tabs": {tab: load_config_file(tab) for tab in _SOURCE_SETTINGS_TABS},
            "formats": preferred_formats()[0],
            "auth": self._resolve_auth_mode(),
            "users": self._user_policies(),
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

    def _user_policies(self) -> list[tuple[Any, ...]]:
        from shelfmark.core.request_policy import filter_request_policy_settings

        if self._user_db is None:
            return []
        # Users without bookmarks cannot change what any item may download.
        policies: list[tuple[Any, ...]] = []
        for user_id in sorted(self._service.all_subscriber_ids()):
            user = self._user_db.get_user(user_id=user_id)
            settings = self._user_db.get_user_settings(user_id)
            policies.append(
                (user_id, user and user.get("role"), filter_request_policy_settings(settings))
            )
        return policies

    def _cancel_unwanted(self) -> None:
        """Cancel downloads of items nobody wants any more."""
        if self._cancel_task is None or self._task_lookup is None:
            return
        with self._delivery_lock:
            task_ids = [*self._payloads, *self._extras]
        for task_id in task_ids:
            status, task = self._task_lookup(task_id)
            wanted_id = _positive_int(getattr(task, "wanted_id", None))
            if wanted_id is None or self._service.get_item(wanted_id) is not None:
                continue
            if status is not None and status not in TERMINAL_QUEUE_STATUSES:
                logger.info("Wanted: nobody wants task %s any more; cancelling it", task_id)
                self._cancel_task(task_id)
            else:
                # Its end went unhandled; nothing else will clean up after it now.
                self._forget_task(task_id)

    def _park_cycles(self) -> None:
        for item in self._service.items_in_state(STATE_SEARCHING):
            self._park(item["id"])

    def _park(self, wanted_id: int) -> None:
        """Stop a cycle while the wanted list is off; it searches again once back on."""
        self._cycles.pop(wanted_id, None)
        parked = self._service.update_item(
            wanted_id,
            expected_state=STATE_SEARCHING,
            state=STATE_WAITING,
            next_search_at=now_utc_iso(),
        )
        if parked is not None:
            self._changed(wanted_id)

    def _reconcile_downloads(self) -> None:
        """Finish attempts whose end the terminal hook missed (a crash in it, a lost task)."""
        if self._task_lookup is None:
            return
        # Download endings arrive on other threads; reconcile must not interleave with one.
        with self._terminal_lock:
            self._reconcile_downloads_locked(self._task_lookup)

    def _reconcile_downloads_locked(
        self, task_lookup: Callable[[str], tuple[QueueStatus | None, Any]]
    ) -> None:
        for item in self._service.items_in_state(STATE_DOWNLOADING):
            task_id = item.get("current_task_id")
            if not task_id:
                self._reconcile_extras(item["id"])
                if not self._deliveries_in_flight(item["id"]):
                    self._settle_deliveries(item)
                continue
            status, task = task_lookup(task_id)
            if status is None or getattr(task, "wanted_id", None) != item["id"]:
                logger.warning("Wanted: task %s of %s vanished; moving on", task_id, item["title"])
                self._service.finish_attempt(item["id"], task_id, ATTEMPT_CANCELLED, "Task lost")
                self._service.update_item(
                    item["id"],
                    expected_state=STATE_DOWNLOADING,
                    state=STATE_SEARCHING,
                    current_task_id=None,
                )
                self._forget_task(task_id)
            elif status in TERMINAL_QUEUE_STATUSES:
                self.on_task_terminal(task_id, status, task)

    def _reconcile_extras(self, wanted_id: int) -> None:
        if self._task_lookup is None:
            return
        with self._delivery_lock:
            extra_ids = [tid for tid, extra in self._extras.items() if extra[0] == wanted_id]
        for extra_id in extra_ids:
            status, task = self._task_lookup(extra_id)
            if status is None:
                with self._delivery_lock:
                    extra = self._extras.pop(extra_id, None)
                if extra is not None:
                    _remove_path(extra[2].parent)
            elif status in TERMINAL_QUEUE_STATUSES:
                self.on_task_terminal(extra_id, status, task)

    def _continue_cycles(self) -> None:
        """Queue the next candidate for items whose last attempt has ended."""
        for item in self._service.items_in_state(STATE_SEARCHING):
            # A user's pick (grab) claims and queues under this lock; do not step in between.
            with self._terminal_lock:
                self._continue_cycle(item["id"])

    def _continue_cycle(self, wanted_id: int) -> None:
        item = self._service.get_item(wanted_id)
        if item is None or item["state"] != STATE_SEARCHING:
            return
        cycle = self._cycles.get(wanted_id)
        if cycle is None:
            # No cycle in memory (a restart or a crash mid-search): start over later.
            self._end_cycle(item, found_nothing=True, source_errors=True)
        elif time.monotonic() - cycle.started > CANDIDATES_MAX_AGE_SECONDS:
            self._cycles.pop(wanted_id, None)
            self._service.update_item(
                wanted_id,
                expected_state=STATE_SEARCHING,
                state=STATE_WAITING,
                next_search_at=now_utc_iso(),
            )
            self._changed(wanted_id)
        else:
            self._try_next(wanted_id)

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
        # An admin approved the bookmark (an approved request): it may download.
        return [
            sub
            for sub in subscribers
            if sub.get("approved_by")
            or user_may_download(self._user_db, sub.get("user_id"), source, auth_mode=auth_mode)
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
        return WantedScheduler._book_from_data(book_data)

    @staticmethod
    def _book_from_data(book_data: dict[str, Any]) -> BookMetadata:
        """The bookmark's own copy of the book, without asking the provider."""
        from shelfmark.metadata_providers import BookMetadata

        provider = str(book_data.get("provider") or "")
        provider_id = str(book_data.get("provider_id") or "")
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
            release_date=book_data.get("release_date"),
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

    def search_now(self, wanted_id: int) -> None:
        """Search an item at the next tick, even before its release date."""
        self._forced.add(wanted_id)
        self.wake()

    def run_cycle(self, wanted_id: int) -> bool:
        """Search all allowed sources for an item and start trying its releases.

        Returns whether a search ran (a book not out yet only gets its date checked).
        """
        item = self._service.get_item(wanted_id)
        if item is None or item["state"] != STATE_WAITING:
            return False
        forced = wanted_id in self._forced
        self._forced.discard(wanted_id)
        if not forced and _search_starts(item["book_data"]) is not None:
            # The date may have moved; a metadata lookup is no release search.
            book = self._resolve_book(item["book_data"])
            book_data = self._remember_book(wanted_id, item["book_data"], book)
            starts = _search_starts(book_data, book)
            if starts is not None:
                self._wait_for_release(wanted_id, starts)
                return False
        before = item
        cycle_number = int(item["search_count"]) + 1
        item = self._service.update_item(
            wanted_id,
            expected_state=STATE_WAITING,
            state=STATE_SEARCHING,
            search_count=cycle_number,
            last_search_at=now_utc_iso(),
        )
        if item is None:
            return False
        self._changed(wanted_id)

        pending = self._pending_subscribers(wanted_id)
        if not pending:
            self._mark_obtained(item, reason=item.get("obtained_reason") or "downloaded")
            return True

        book = self._resolve_book(item["book_data"])
        item["book_data"] = self._remember_book(wanted_id, item["book_data"], book)
        starts = None if forced else _search_starts(item["book_data"], book)
        if starts is not None:
            # Only now learned the book is not out: undo the claim, no search ran.
            self._service.update_item(
                wanted_id,
                expected_state=STATE_SEARCHING,
                state=STATE_WAITING,
                search_count=before["search_count"],
                last_search_at=before.get("last_search_at"),
            )
            self._wait_for_release(wanted_id, starts)
            return False
        if self._in_library(book):
            # Only those whose downloads go to a folder have it; email and BookLore
            # subscribers still want it delivered.
            in_library = [
                sub
                for sub in pending
                if delivery_target(
                    _positive_int(sub.get("user_id")), sub.get("username")
                ).startswith("folder:")
            ]
            if in_library:
                logger.info("Wanted: %s is already in the library", item["title"])
                library_ids = [sub.get("user_id") for sub in in_library]
                self._service.mark_delivered(wanted_id, library_ids, None)
                self._notify_obtained(item, library_ids, reason="in_library")
                pending = [sub for sub in pending if sub not in in_library]
            if not pending:
                self._mark_obtained(item, reason="in_library")
                return True

        sources = [
            source for source in self._available_sources() if self._users_allowed(pending, source)
        ]
        if not sources:
            # Rests until sources, rights or subscribers change; searching cannot help.
            self._cycles.pop(wanted_id, None)
            self._service.update_item(
                wanted_id,
                expected_state=STATE_SEARCHING,
                state=STATE_WAITING,
                next_search_at=None,
                last_result="No source the subscribers may download from is available",
            )
            searched_for = {sub["id"] for sub in pending}
            if {sub["id"] for sub in self._pending_subscribers(wanted_id)} - searched_for:
                # Someone joined while this pass ran; their wake-up did not reach it.
                self._service.update_item(
                    wanted_id, expected_state=STATE_WAITING, next_search_at=now_utc_iso()
                )
            self._changed(wanted_id)
            return True

        self._searches.append(time.monotonic())
        releases, answered, failed = self._search(sources, book, pending)
        prefs = self._preferences(pending, sources)
        formats = prefs.preferred_formats
        picked = pick_releases(
            releases,
            item["book_data"],
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
        searched = item.get("searched_sources")
        previously_failed = set(searched.get("failed", [])) if isinstance(searched, dict) else set()
        self._service.update_item(
            wanted_id, searched_sources={"answered": answered, "failed": failed}
        )
        picked, hold = self._hold_for_format(picked, pending, formats)
        if not picked:
            result = f"Searched {len(answered)} source(s), no matching release"
            if hold is not None:
                result = hold.message
            if failed:
                result += f"; {', '.join(failed)} failed"
            # A source failing again is down, not blipping: back off as usual.
            self._end_cycle(
                item,
                found_nothing=True,
                source_errors=bool(set(failed) - previously_failed),
                result=result,
                search_by=hold.until if hold is not None else None,
            )
            return True
        self._cycles[wanted_id] = _Cycle(
            number=cycle_number, started=time.monotonic(), releases=picked
        )
        self._try_next(wanted_id)
        return True

    def _remember_book(
        self, wanted_id: int, book_data: dict[str, Any], book: BookMetadata
    ) -> dict[str, Any]:
        """Keep what the metadata adds (release date, series, authors) for later matching."""
        enriched = self._picker_book(book_data, book)
        if book.release_date:
            enriched["release_date"] = book.release_date
        if enriched != book_data:
            self._service.update_item(wanted_id, book_data=enriched)
        return enriched

    def _wait_for_release(self, wanted_id: int, starts: datetime) -> None:
        # Check the date again now and then: releases move, and placeholders get fixed.
        recheck = datetime.now(UTC) + _RELEASE_RECHECK
        waiting = self._service.update_item(
            wanted_id,
            expected_state=STATE_WAITING,
            next_search_at=min(starts, recheck).isoformat(timespec="seconds"),
            last_result="Not released yet",
        )
        if waiting is not None:
            self._changed(wanted_id)

    @staticmethod
    def _format_hold(
        pending: list[dict[str, Any]], formats: tuple[str, ...]
    ) -> tuple[frozenset[str], str, datetime] | None:
        """(good-enough formats, what is waited for, until when) while subscribers wait."""
        cutoff, days = format_wait()
        if days <= 0 or not formats:
            return None
        joined = [_parse_time(sub.get("created_at")) for sub in pending]
        started = min((time for time in joined if time is not None), default=None)
        if started is None:
            return None
        until = started + timedelta(days=days)
        if datetime.now(UTC) >= until:
            return None
        order = format_order()
        if cutoff is None or cutoff not in order:
            return frozenset(formats[:1]), formats[0].upper(), until
        # The cutoff may be a switched-off format; it still marks the line in the order.
        good = frozenset(order[: order.index(cutoff) + 1]) & frozenset(formats)
        if not good:
            return None
        label = cutoff.upper() if cutoff == formats[0] else f"{cutoff.upper()} or better"
        return good, label, until

    def _hold_for_format(
        self, picked: list[Release], pending: list[dict[str, Any]], formats: tuple[str, ...]
    ) -> tuple[list[Release], _FormatHold | None]:
        """Drop releases below the good-enough format while subscribers still wait for it."""
        hold = self._format_hold(pending, formats) if picked else None
        if hold is None:
            return picked, None
        good_formats, label, until = hold
        good = [release for release in picked if _release_format(release) in good_formats]
        if good:
            return good, None
        found = sorted({_release_format(release).upper() or "unknown format" for release in picked})
        message = (
            f"Only {', '.join(found)} found; waiting for {label} until {until.date().isoformat()}"
        )
        return [], _FormatHold(until=until, message=message)

    def _preferences(self, pending: list[dict[str, Any]], sources: list[str]) -> PickerPreferences:
        formats = preferred_formats()[0]
        return PickerPreferences(
            # Archives are unpacked to their books, so they are no format choice.
            supported_formats=supported_formats() & {*formats, *ARCHIVE_FORMATS},
            preferred_formats=formats,
            excluded_formats=preferred_formats()[1],
            source_order=tuple(sources),
            languages=tuple(self._search_languages(pending) or ()),
        )

    def evaluate(self, wanted_id: int, releases: list[Release]) -> dict[str, str | None]:
        """Why the wanted list would skip each release ("source:id" -> reason or None)."""
        item = self._service.get_item(wanted_id)
        if item is None:
            return {}
        pending = self._pending_subscribers(wanted_id)
        sources = [
            source for source in self._available_sources() if self._users_allowed(pending, source)
        ]
        prefs = self._preferences(pending, sources)
        blocked = self._service.blocked_release_keys(wanted_id)
        hold = self._format_hold(pending, prefs.preferred_formats)
        reasons: dict[str, str | None] = {}
        for release in releases:
            if release.source not in sources:
                reason: str | None = "Source not used by the wanted list"
            else:
                reason = rejection_reason(release, item["book_data"], prefs, blocked)
            if reason is None and hold is not None:
                good_formats, label, until = hold
                if _release_format(release) not in good_formats:
                    reason = f"Waiting for {label} until {until.date().isoformat()}"
            reasons[f"{release.source}:{release.source_id}"] = reason
        return reasons

    def grab(
        self, wanted_id: int, release: Release, owner: dict[str, Any]
    ) -> tuple[bool, str | None]:
        """Download a release a user picked for an item, as if the wanted list had found it."""
        with self._terminal_lock:
            item = self._service.get_item(wanted_id)
            if item is None:
                return False, "Wanted item not found"
            if item["state"] != STATE_WAITING:
                return False, f"The wanted list is {item['state']} for this book"
            with self._delivery_lock:
                in_flight = release.source_id in self._payloads
            if in_flight:
                return False, "This release is already downloading"
            cycle_number = int(item["search_count"]) + 1
            claimed = self._service.update_item(
                wanted_id,
                expected_state=STATE_WAITING,
                state=STATE_SEARCHING,
                search_count=cycle_number,
            )
            if claimed is None:
                return False, "The wanted list is busy with this book"
            cycle = _Cycle(number=cycle_number, started=time.monotonic(), origin="manual")
            self._cycles[wanted_id] = cycle
            if self._queue_attempt(claimed, release, owner, cycle_number):
                return True, None
            # Back to where it was; the next scheduled search goes on as planned.
            self._cycles.pop(wanted_id, None)
            self._service.update_item(
                wanted_id, expected_state=STATE_SEARCHING, state=STATE_WAITING
            )
            self._changed(wanted_id)
            return False, cycle.queue_error or "Could not queue the release"

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

        # None means "every language"; said explicitly, or the search would fall back to
        # the first subscriber's default languages.
        languages = self._search_languages(pending) or ["all"]
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
        if not wanted_enabled():
            # Switched off while this pass searched.
            self._park(wanted_id)
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
        if cycle.origin != "search":
            # A user's pick keeps the planned search; a feed find makes a full search due.
            feed = cycle.origin == "feed"
            self._service.update_item(
                wanted_id,
                expected_state=STATE_SEARCHING,
                state=STATE_WAITING,
                current_task_id=None,
                last_result="The release from the feed failed"
                if feed
                else "The picked release failed",
                **({"next_search_at": now_utc_iso()} if feed else {}),
            )
            self._changed(wanted_id)
            return
        result = "Every matching release failed"
        if cycle.queue_error:
            result = f"{result}; could not queue: {cycle.queue_error}"
        self._end_cycle(item, found_nothing=True, source_errors=False, result=result)

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
        payload = release_to_queue_payload(release, item["book_data"])
        username = owner.get("username")
        with self._delivery_lock:
            self._payloads[release.source_id] = payload
        success, error = self._queue_release(
            payload,
            0,
            user_id=_positive_int(owner.get("user_id")),
            username=username,
            wanted_id=wanted_id,
        )
        if not success and self._task_lookup is not None:
            status, queued_task = self._task_lookup(release.source_id)
            # Queued after all (e.g. only the status broadcast failed) - if it is ours, not
            # the same release queued by a user or another item.
            success = (
                status is not None
                and status not in TERMINAL_QUEUE_STATUSES
                and getattr(queued_task, "wanted_id", None) == wanted_id
            )
        cycle = self._cycles.get(wanted_id)
        if cycle is not None:
            cycle.queue_error = None if success else error
        if success:
            logger.info(
                "Wanted: queued %s from %s for %s", release.title, release.source, item["title"]
            )
            if cycle is not None and not cycle.announced:
                cycle.announced = True
                self._notify_grabbed(item, release)
            self._changed(wanted_id)
            return True
        logger.info("Wanted: could not queue %s from %s: %s", release.title, release.source, error)
        self._forget_task(release.source_id)
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
        search_by: datetime | None = None,
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
        if search_by is not None:
            delay = max(min(delay, (search_by - datetime.now(UTC)).total_seconds() / 3600), 0)
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

    def _notify_grabbed(self, item: dict[str, Any], release: Release) -> None:
        from shelfmark.core.notifications import (
            NotificationContext,
            NotificationEvent,
            notify_admin,
            notify_user,
        )
        from shelfmark.release_sources import get_source_display_name

        context = NotificationContext(
            event=NotificationEvent.WANTED_GRABBED,
            title=item["title"],
            author=item.get("author") or "Unknown author",
            content_type=WANTED_CONTENT_TYPE,
            detail=get_source_display_name(release.source),
        )
        # Only those this source may serve get this download.
        receivers = self._users_allowed(self._pending_subscribers(item["id"]), release.source)
        user_ids = {_positive_int(sub.get("user_id")) for sub in receivers}
        try:
            notify_admin(NotificationEvent.WANTED_GRABBED, context)
            for user_id in user_ids - {None}:
                notify_user(user_id, NotificationEvent.WANTED_GRABBED, context)
        except (RuntimeError, TypeError, ValueError) as exc:
            logger.warning("Wanted: could not send notifications for %s: %s", item["title"], exc)

    def _notify_obtained(
        self, item: dict[str, Any], user_ids: list[int | None], *, reason: str
    ) -> None:
        from shelfmark.core.notifications import (
            NotificationContext,
            NotificationEvent,
            notify_admin,
            notify_user,
        )

        context = NotificationContext(
            event=NotificationEvent.WANTED_OBTAINED,
            title=item["title"],
            author=item.get("author") or "Unknown author",
            content_type=WANTED_CONTENT_TYPE,
            detail="is already in your library" if reason == "in_library" else "was downloaded",
        )
        try:
            notify_admin(NotificationEvent.WANTED_OBTAINED, context)
            for user_id in {_positive_int(user_id) for user_id in user_ids} - {None}:
                notify_user(user_id, NotificationEvent.WANTED_OBTAINED, context)
        except (RuntimeError, TypeError, ValueError) as exc:
            logger.warning("Wanted: could not send notifications for %s: %s", item["title"], exc)

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
        with self._terminal_lock:
            self._on_task_terminal(wanted_id, task_id, status, task)

    def _on_task_terminal(
        self, wanted_id: int, task_id: str, status: QueueStatus, task: Any
    ) -> None:
        with self._delivery_lock:
            extra = self._extras.pop(task_id, None)
        if extra is not None:
            self._extra_delivery_ended(extra, task_id, status, task)
            return
        item = self._service.get_item(wanted_id)
        if item is None:
            self._cycles.pop(wanted_id, None)
            self._forget_task(task_id)
            return
        if item.get("current_task_id") != task_id:
            # An earlier attempt the user retried by hand: its success still counts.
            if status == QueueStatus.COMPLETE and item["state"] in (
                STATE_WAITING,
                STATE_SEARCHING,
                STATE_PAUSED,
            ):
                self._deliver(item, task)
            self._forget_task(task_id)
            return
        message = getattr(task, "status_message", None)
        message = message.strip() if isinstance(message, str) and message.strip() else None
        stalled = (message or "").startswith(_STALLED_MESSAGE_PREFIX)

        if status == QueueStatus.COMPLETE:
            self._service.finish_attempt(wanted_id, task_id, ATTEMPT_COMPLETE)
            self._deliver(item, task)
        elif status == QueueStatus.CANCELLED and not stalled:
            # Cancelling a wanted download rejects that release, e.g. the wrong book.
            self._service.finish_attempt(wanted_id, task_id, ATTEMPT_REJECTED, message)
            self._next_attempt(wanted_id, "Last release was cancelled")
        elif self._failure_is_local(task, message):
            self._service.finish_attempt(wanted_id, task_id, ATTEMPT_CANCELLED, message)
            self._notify_local_failure(item, message)
            # Waits like a book that was not found, so a lasting fault backs off.
            self._end_cycle(
                {**item, "state": STATE_DOWNLOADING},
                found_nothing=True,
                source_errors=False,
                result=f"Download could not be delivered: {message or 'unknown error'}",
            )
        else:
            self._service.finish_attempt(
                wanted_id, task_id, ATTEMPT_ERROR, message or "Download failed"
            )
            self._next_attempt(wanted_id, f"Last attempt failed: {message or 'Download failed'}")
        self._forget_task(task_id)

    def _next_attempt(self, wanted_id: int, result: str) -> None:
        self._service.update_item(
            wanted_id,
            expected_state=STATE_DOWNLOADING,
            state=STATE_SEARCHING,
            current_task_id=None,
            last_result=result,
        )
        self._changed(wanted_id)
        self.wake()

    @staticmethod
    def _failure_is_local(task: Any, message: str | None) -> bool:
        """Whether a failure happened on our side rather than with the release.

        A release that downloads but holds no usable book still fails in post-processing;
        only failures naming our destination or output count as local.
        """
        if getattr(task, "last_error_type", None) not in _RELEASE_FAILURE_TYPES:
            return True
        return bool(message and _LOCAL_FAILURE_PATTERN.search(message))

    def _notify_local_failure(self, item: dict[str, Any], message: str | None) -> None:
        from shelfmark.core.notifications import (
            NotificationContext,
            NotificationEvent,
            notify_admin,
        )

        try:
            notify_admin(
                NotificationEvent.DOWNLOAD_FAILED,
                NotificationContext(
                    event=NotificationEvent.DOWNLOAD_FAILED,
                    title=item["title"],
                    author=item.get("author") or "Unknown author",
                    content_type=WANTED_CONTENT_TYPE,
                    error_message=message,
                ),
            )
        except (RuntimeError, TypeError, ValueError) as exc:
            logger.warning("Wanted: could not notify about %s: %s", item["title"], exc)

    # -- delivery ------------------------------------------------------------------

    def _delivery_groups(
        self, wanted_id: int, task: Any
    ) -> tuple[list[dict[str, Any]], list[list[dict[str, Any]]]]:
        """Pending subscribers allowed this download, grouped by delivery target.

        Returns the group the download itself delivers to (those sharing the target of
        the user it runs for, who may have left since) and every other group.
        """
        source = str(getattr(task, "source", "") or "")
        receivers = self._users_allowed(self._pending_subscribers(wanted_id), source)
        groups: dict[str, list[dict[str, Any]]] = {}
        for sub in receivers:
            target = delivery_target(_positive_int(sub.get("user_id")), sub.get("username"))
            groups.setdefault(target, []).append(sub)
        owner_target = delivery_target(
            _positive_int(getattr(task, "user_id", None)), getattr(task, "username", None)
        )
        owner_group = groups.pop(owner_target, [])
        return owner_group, list(groups.values())

    def on_downloaded_file(self, task: Any, path: Path) -> None:
        """Hold a copy of a wanted download for every other delivery target."""
        wanted_id = _positive_int(getattr(task, "wanted_id", None))
        task_id = getattr(task, "task_id", None)
        if wanted_id is None or not isinstance(task_id, str):
            return
        item = self._service.get_item(wanted_id)
        if item is None or item.get("current_task_id") != task_id:
            return
        _owner_group, extra_groups = self._delivery_groups(wanted_id, task)
        if not extra_groups:
            return
        digest = hashlib.sha256(task_id.encode()).hexdigest()[:16]
        held: list[tuple[list[dict[str, Any]], Path]] = []
        try:
            for index, group in enumerate(extra_groups, start=1):
                target_dir = _held_copies_dir() / f"{digest}-{index}"
                held.append((group, target_dir / path.name))
                _copy_path(path, target_dir)
        except OSError:
            for _group, copy in held:
                _remove_path(copy.parent)
            raise
        with self._delivery_lock:
            # The download may have been cancelled while we copied.
            active = task_id in self._payloads
            previous = self._held.pop(task_id, []) if active else held
            if active:
                self._held[task_id] = held
        for _group, copy in previous:
            _remove_path(copy.parent)
        if active:
            logger.info(
                "Wanted: holding %d extra delivery copy(ies) of %s", len(held), item["title"]
            )

    def _forget_task(self, task_id: str) -> None:
        with self._delivery_lock:
            self._payloads.pop(task_id, None)
            held = self._held.pop(task_id, [])
            extra = self._extras.pop(task_id, None)
        copies = [copy for _group, copy in held] + ([extra[2]] if extra else [])
        for copy in copies:
            _remove_path(copy.parent)

    def _queue_extra_delivery(
        self,
        wanted_id: int,
        task_id: str,
        index: int,
        group: list[dict[str, Any]],
        copy: Path,
        payload: dict[str, Any],
    ) -> bool:
        # No "#": the id ends up in cover URLs, where it would start a fragment.
        extra_id = f"{task_id}~wanted-{wanted_id}-{index}"
        user_ids = [sub.get("user_id") for sub in group]
        with self._delivery_lock:
            self._extras[extra_id] = (wanted_id, user_ids, copy)
        owner = group[0]
        success, error = self._queue_release(
            {**payload, "source_id": extra_id},
            0,
            user_id=_positive_int(owner.get("user_id")),
            username=owner.get("username"),
            wanted_id=wanted_id,
            staged_path=str(copy),
        )
        if not success:
            logger.warning("Wanted: could not queue a delivery of %s: %s", task_id, error)
            with self._delivery_lock:
                self._extras.pop(extra_id, None)
            return False
        return True

    def _deliveries_in_flight(self, wanted_id: int) -> int:
        with self._delivery_lock:
            return sum(1 for extra in self._extras.values() if extra[0] == wanted_id)

    def _deliver(self, item: dict[str, Any], task: Any) -> None:
        wanted_id = item["id"]
        task_id = str(getattr(task, "task_id", "") or "")
        owner_group, _others = self._delivery_groups(wanted_id, task)
        if owner_group:
            owner_group_ids = [sub.get("user_id") for sub in owner_group]
            self._service.mark_delivered(wanted_id, owner_group_ids, task_id)
            self._notify_obtained(item, owner_group_ids, reason="downloaded")
        with self._delivery_lock:
            held = self._held.pop(task_id, [])
            payload = self._payloads.get(task_id)
        for index, (group, copy) in enumerate(held, start=1):
            if payload is None or not self._queue_extra_delivery(
                wanted_id, task_id, index, group, copy, payload
            ):
                _remove_path(copy.parent)
        self._cycles.pop(wanted_id, None)
        self._settle_deliveries(item)

    def _extra_delivery_ended(
        self,
        extra: tuple[int, list[int | None], Path],
        task_id: str,
        status: QueueStatus,
        task: Any,
    ) -> None:
        wanted_id, user_ids, copy = extra
        # Delivered, or the subscribers stay waiting for a download of their own.
        _remove_path(copy.parent)
        item = self._service.get_item(wanted_id)
        if status == QueueStatus.COMPLETE and item is not None:
            self._service.mark_delivered(wanted_id, user_ids, task_id)
            self._notify_obtained(item, user_ids, reason="downloaded")
        else:
            message = getattr(task, "status_message", None)
            stalled = isinstance(message, str) and message.startswith(_STALLED_MESSAGE_PREFIX)
            if status == QueueStatus.CANCELLED and not stalled and item is not None:
                self._reject_delivered_release(item, task_id, task)
        if item is not None:
            self._settle_deliveries(item)

    def _reject_delivered_release(self, item: dict[str, Any], extra_id: str, task: Any) -> None:
        """A cancelled delivery rejects its release, as cancelling the download would."""
        source_id = extra_id.split("~wanted-", 1)[0]
        source = str(getattr(task, "source", "") or "")
        self._service.record_attempt(
            item["id"],
            cycle=int(item.get("search_count") or 0),
            source=source,
            source_id=source_id,
            match_key=None,
            release_title=getattr(task, "title", None),
            file_format=getattr(task, "format", None),
            size=getattr(task, "size", None),
            indexer=None,
        )
        self._service.finish_attempt(item["id"], source_id, ATTEMPT_REJECTED, "Delivery cancelled")

    def _settle_deliveries(self, item: dict[str, Any]) -> None:
        """Decide an item's state once its download finished delivering."""
        wanted_id = item["id"]
        if self._deliveries_in_flight(wanted_id):
            self._service.update_item(
                wanted_id,
                state=STATE_DOWNLOADING,
                current_task_id=None,
                last_result="Delivering to the other subscribers",
            )
            self._changed(wanted_id)
            return
        if self._pending_subscribers(wanted_id):
            # Someone may not download from this source, had no copy to receive, or their
            # delivery failed; keep looking for them.
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
