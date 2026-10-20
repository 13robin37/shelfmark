"""Tests for the wanted list background job."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from shelfmark.core.models import QueueStatus
from shelfmark.core.user_db import UserDB
from shelfmark.core.wanted_service import (
    STATE_DOWNLOADING,
    STATE_OBTAINED,
    STATE_SEARCHING,
    STATE_WAITING,
    WantedService,
)
from shelfmark.download import wanted_scheduler as ws
from shelfmark.metadata_providers import BookMetadata
from shelfmark.release_sources import Release

BOOK = {"title": "Dune", "author": "Frank Herbert", "provider": "hardcover", "provider_id": "1"}
SOURCES = [
    {"name": "direct_download", "enabled": True, "supported_content_types": ["ebook"]},
    {"name": "libgen", "enabled": True, "supported_content_types": ["ebook"]},
]
DUE = "2000-01-01T00:00:00+00:00"
LATER = "2999-01-01T00:00:00+00:00"
DENY_ALL = {"REQUESTS_ENABLED": True, "REQUEST_POLICY_DEFAULT_EBOOK": "request_release"}
NO_DIRECT_DOWNLOAD = {
    "REQUESTS_ENABLED": True,
    "REQUEST_POLICY_DEFAULT_EBOOK": "download",
    "REQUEST_POLICY_RULES": [
        {"source": "direct_download", "content_type": "ebook", "mode": "request_release"}
    ],
}


def _release(source_id, *, source="direct_download", title="Dune", fmt="epub"):
    return Release(source=source, source_id=source_id, title=title, format=fmt)


class _Env:
    def __init__(self, tmp_path):
        db_path = str(tmp_path / "users.db")
        self.user_db = UserDB(db_path)
        self.user_db.initialize()
        self.service = WantedService(db_path)
        self.alice = self.user_db.create_user(username="alice")
        self.bob = self.user_db.create_user(username="bob")
        self.queued: list[tuple[dict, int | None, str | None]] = []
        self.queued_kwargs: list[dict] = []
        self.queue_statuses: dict[str, tuple] = {}
        self.queue_result: tuple[bool, str | None] = (True, None)
        self.results: dict[str, tuple[list[Release], str | None]] = {
            "direct_download": ([_release("aa-1"), _release("aa-2", fmt="mobi")], None),
            "libgen": ([_release("libgen:1", source="libgen")], None),
        }
        self.search_kwargs: list[dict] = []
        self.in_library = False
        self.policy = {"REQUESTS_ENABLED": False}
        self.changed: list[int | None] = []
        self.cancelled: list[str] = []
        self.scheduler = ws.WantedScheduler(
            self.service,
            self.user_db,
            resolve_auth_mode=lambda: "builtin",
            queue_release=self._queue_release,
            task_lookup=self._lookup,
            cancel_task=self._cancel,
            on_change=self.changed.append,
        )

    def _cancel(self, task_id):
        self.cancelled.append(task_id)
        return True

    def _queue_release(self, payload, priority, *, user_id=None, username=None, **kwargs):
        del priority
        self.queued.append((payload, user_id, username))
        self.queued_kwargs.append(kwargs)
        return self.queue_result

    def _lookup(self, task_id):
        if task_id in self.queue_statuses:
            return self.queue_statuses[task_id]
        for (payload, user_id, _username), kwargs in zip(
            self.queued, self.queued_kwargs, strict=True
        ):
            if payload["source_id"] == task_id:
                return QueueStatus.DOWNLOADING, SimpleNamespace(
                    wanted_id=kwargs.get("wanted_id"), task_id=task_id, user_id=user_id
                )
        return None, None

    def _search(self, source, book, **kwargs):
        del book
        self.search_kwargs.append(kwargs)
        releases, error = self.results.get(source, ([], None))
        return (None if error else object()), releases, error

    def add(self, user=None, **book):
        item, _ = self.service.add(user_id=(user or self.alice)["id"], book_data={**BOOK, **book})
        return item["id"]

    def item(self, wanted_id):
        return self.service.get_item(wanted_id)

    def pending(self, wanted_id):
        subscribers = self.service.list_subscribers(wanted_id)
        return [sub["user_id"] for sub in subscribers if not sub["delivered_at"]]

    def task(self, wanted_id, **fields):
        """The queue task of the item's current download, as the queue reports it."""
        payload, user_id, _username = self.queued[-1]
        task = SimpleNamespace(
            wanted_id=wanted_id,
            task_id=self.item(wanted_id)["current_task_id"],
            source=payload["source"],
            user_id=user_id,
            status_message=None,
            last_error_type=None,
            staged_path=None,
        )
        for key, value in fields.items():
            setattr(task, key, value)
        return task

    def finish(self, wanted_id, status, message=None, **fields):
        fields.setdefault(
            "last_error_type", "StatusCallbackError" if status == QueueStatus.ERROR else None
        )
        task = self.task(wanted_id, status_message=message, **fields)
        self.scheduler.on_task_terminal(task.task_id, status, task)


@pytest.fixture
def env(tmp_path):
    env = _Env(tmp_path)
    book = BookMetadata(
        provider="hardcover", provider_id="1", title="Dune", authors=["Frank Herbert"]
    )
    patches = [
        patch.object(ws, "wanted_enabled", return_value=True),
        patch.object(ws, "wanted_sources", return_value=["direct_download", "libgen"]),
        patch.object(ws, "supported_formats", return_value=frozenset({"epub", "mobi"})),
        patch.object(ws, "preferred_formats", return_value=(("epub", "mobi"), frozenset())),
        patch.object(ws, "backoff_hours", return_value=(1.0, 6.0, 24.0)),
        patch.object(ws, "max_searches_per_hour", return_value=60),
        patch.object(ws.WantedScheduler, "_resolve_book", staticmethod(lambda _data: book)),
        patch.object(ws.WantedScheduler, "_search_languages", staticmethod(lambda _subs: None)),
        patch("shelfmark.release_sources.list_available_sources", return_value=SOURCES),
        patch("shelfmark.core.release_search.search_source_releases", side_effect=env._search),
        patch("shelfmark.core.library_index.is_in_library", side_effect=lambda *_a: env.in_library),
        patch("shelfmark.download.warmup.seconds_since_user_search", return_value=None),
        patch("shelfmark.core.settings_registry.load_config_file", side_effect=lambda _tab: {}),
        patch(
            "shelfmark.core.wanted_access.load_users_request_policy_settings",
            side_effect=lambda: env.policy,
        ),
    ]
    for active in patches:
        active.start()
    yield env
    for active in patches:
        active.stop()


@pytest.fixture
def notes():
    """Records sent notifications as (event, user id or None for the admin, detail)."""
    sent: list[tuple[str, int | None, str | None]] = []
    with (
        patch(
            "shelfmark.core.notifications.notify_admin",
            side_effect=lambda event, ctx: sent.append((event.value, None, ctx.detail)),
        ),
        patch(
            "shelfmark.core.notifications.notify_user",
            side_effect=lambda user_id, event, ctx: sent.append((event.value, user_id, ctx.detail)),
        ),
        patch("shelfmark.release_sources.get_source_display_name", return_value="Anna's Archive"),
    ):
        yield sent


def test_a_cycle_queues_the_best_release_for_the_first_subscriber(env):
    wanted_id = env.add()

    env.scheduler.tick()

    payload, user_id, username = env.queued[0]
    assert payload["source_id"] == "aa-1"
    assert env.queued_kwargs[0] == {"wanted_id": wanted_id}
    assert payload["content_type"] == "ebook"
    assert (user_id, username) == (env.alice["id"], "alice")
    item = env.item(wanted_id)
    assert item["state"] == STATE_DOWNLOADING
    assert item["current_task_id"] == "aa-1"
    assert item["search_count"] == 1
    assert item["searched_sources"] == {"answered": ["direct_download", "libgen"], "failed": []}
    # All languages are asked for explicitly, and listeners hear of the change.
    assert env.search_kwargs and all(kw["languages"] == ["all"] for kw in env.search_kwargs)
    assert wanted_id in env.changed


@pytest.mark.parametrize("message", ["Server error", "No supported file formats found"])
def test_a_failed_attempt_is_a_strike_and_moves_on_to_the_next_release(env, message):
    wanted_id = env.add()
    env.scheduler.tick()

    env.finish(wanted_id, QueueStatus.ERROR, message)
    assert env.item(wanted_id)["state"] == STATE_SEARCHING
    env.scheduler.tick()

    # Same score and format: Anna's Archive ranks above Libgen in the source order.
    assert [payload["source_id"] for payload, _, _ in env.queued] == ["aa-1", "libgen:1"]
    assert env.item(wanted_id)["state"] == STATE_DOWNLOADING
    attempts = env.service.list_attempts(wanted_id)
    assert [attempt["status"] for attempt in attempts] == ["queued", "error"]


def test_every_release_failing_ends_the_cycle_with_backoff(env):
    wanted_id = env.add()
    env.scheduler.tick()
    for _ in range(3):
        env.finish(wanted_id, QueueStatus.ERROR)
        env.scheduler.tick()

    item = env.item(wanted_id)
    assert len(env.queued) == 3
    assert item["state"] == STATE_WAITING
    assert item["backoff_level"] == 1
    assert item["next_search_at"] > item["last_search_at"]


def test_a_release_failing_in_two_cycles_is_not_tried_a_third_time(env):
    env.results = {"direct_download": ([_release("aa-1")], None)}
    wanted_id = env.add()
    for _ in range(2):
        env.service.update_item(wanted_id, next_search_at=DUE)
        env.scheduler.tick()
        env.finish(wanted_id, QueueStatus.ERROR)
        env.scheduler.tick()
    env.service.update_item(wanted_id, next_search_at=DUE)
    env.scheduler.tick()

    assert len(env.queued) == 2
    assert env.item(wanted_id)["last_result"].endswith("no matching release")


def test_completion_marks_the_item_obtained_delivered_and_notifies(env, notes):
    wanted_id = env.add()
    env.add(user=env.bob)
    env.scheduler.tick()
    notes.clear()

    env.finish(wanted_id, QueueStatus.COMPLETE)

    item = env.item(wanted_id)
    assert item["state"] == STATE_OBTAINED
    assert item["obtained_reason"] == "downloaded"
    assert env.pending(wanted_id) == []
    assert sorted((user_id or 0, detail) for _event, user_id, detail in notes) == [
        (0, "was downloaded"),
        (env.alice["id"], "was downloaded"),
        (env.bob["id"], "was downloaded"),
    ]


def test_a_cancel_rejects_the_release_and_moves_on(env):
    wanted_id = env.add()
    env.scheduler.tick()
    env.finish(wanted_id, QueueStatus.CANCELLED, "Download stalled (no activity for 300s)")
    assert env.item(wanted_id)["state"] == STATE_SEARCHING

    env.scheduler.tick()
    env.finish(wanted_id, QueueStatus.CANCELLED, "Cancelled")

    assert env.item(wanted_id)["state"] == STATE_SEARCHING
    # The cancelled release is never offered again; the stalled one only skips this cycle.
    excluded = env.service.excluded_release_keys(wanted_id, 99)
    assert "id:libgen:libgen:1" in excluded
    assert "id:direct_download:aa-1" not in excluded
    env.scheduler.tick()
    assert env.queued[-1][0]["source_id"] == "aa-2"


def test_a_book_already_in_the_library_is_not_downloaded(env, notes):
    env.in_library = True
    wanted_id = env.add()

    env.scheduler.tick()

    assert env.queued == []
    item = env.item(wanted_id)
    assert item["state"] == STATE_OBTAINED
    assert item["obtained_reason"] == "in_library"
    assert [detail for _e, user_id, detail in notes if user_id] == ["is already in your library"]


def test_nothing_found_waits_and_source_errors_retry_sooner(env):
    env.results = {"direct_download": ([], None), "libgen": ([], "libgen: down")}
    wanted_id = env.add()

    env.scheduler.tick()

    item = env.item(wanted_id)
    assert item["state"] == STATE_WAITING
    # A failed source keeps the first interval and does not climb the ladder.
    assert item["backoff_level"] == 0
    assert "libgen failed" in item["last_result"]

    # Failing again is an outage, not a blip: the usual backoff applies.
    env.service.update_item(wanted_id, next_search_at=DUE)
    env.scheduler.tick()
    assert env.item(wanted_id)["backoff_level"] == 1


def test_background_search_gives_way_to_users_and_the_hourly_limit(env):
    env.add()
    with patch("shelfmark.download.warmup.seconds_since_user_search", return_value=10.0):
        env.scheduler.tick()
    assert env.queued == []

    with patch.object(ws, "max_searches_per_hour", return_value=1):
        env.scheduler.tick()
        env.add(provider_id="2", title="Dune")
        env.scheduler.tick()
    assert len(env.queued) == 1


def test_a_source_change_makes_waiting_items_due(env):
    env.results = {}
    wanted_id = env.add()
    env.scheduler.tick()
    env.service.update_item(wanted_id, next_search_at=LATER, backoff_level=2)

    with patch("shelfmark.core.settings_registry.load_config_file", return_value={"changed": True}):
        env.scheduler.tick()

    item = env.item(wanted_id)
    # Searched again at once, and the interval ladder started over from its first step.
    assert item["search_count"] == 2
    assert item["backoff_level"] == 1


def test_releases_from_sources_a_subscriber_may_not_use_are_skipped(env):
    env.policy = NO_DIRECT_DOWNLOAD
    env.add()

    env.scheduler.tick()

    assert [payload["source"] for payload, _, _ in env.queued] == ["libgen"]


@pytest.mark.parametrize(
    ("error", "aa_1_in_queue"),
    [
        ("Source unavailable", (None, None)),
        # Queued by someone else: not taken over as this item's download.
        (
            "Release is already in the download queue",
            (QueueStatus.DOWNLOADING, SimpleNamespace(wanted_id=None)),
        ),
    ],
)
def test_a_release_that_cannot_be_queued_is_skipped_without_a_strike(env, error, aa_1_in_queue):
    env.queue_result = (False, error)
    env.queue_statuses = {"aa-1": aa_1_in_queue, "aa-2": (None, None), "libgen:1": (None, None)}
    wanted_id = env.add()

    env.scheduler.tick()

    assert len(env.queued) == 3
    assert {attempt["status"] for attempt in env.service.list_attempts(wanted_id)} == {"cancelled"}
    assert env.item(wanted_id)["state"] == STATE_WAITING
    assert env.item(wanted_id)["last_result"].endswith(f"could not queue: {error}")


def test_an_old_candidate_list_is_searched_afresh(env):
    wanted_id = env.add()
    env.scheduler.tick()
    env.finish(wanted_id, QueueStatus.ERROR)
    env.scheduler._cycles[wanted_id].started -= ws.CANDIDATES_MAX_AGE_SECONDS + 1

    env.scheduler._continue_cycles()

    assert env.item(wanted_id)["state"] == STATE_WAITING
    assert wanted_id not in env.scheduler._cycles


def test_terminal_events_for_other_tasks_are_ignored(env):
    wanted_id = env.add()
    env.scheduler.tick()

    stale = SimpleNamespace(
        wanted_id=wanted_id, task_id="old", source="libgen", status_message=None
    )
    env.scheduler.on_task_terminal("old", QueueStatus.ERROR, stale)
    env.scheduler.on_task_terminal("aa-1", QueueStatus.ERROR, SimpleNamespace(wanted_id=None))

    assert env.item(wanted_id)["state"] == STATE_DOWNLOADING


def test_switching_off_lets_running_downloads_end_without_starting_new_ones(env):
    wanted_id = env.add()
    env.scheduler.tick()
    with patch.object(ws, "wanted_enabled", return_value=False):
        env.finish(wanted_id, QueueStatus.ERROR, "Download failed")
        env.scheduler.tick()

    item = env.item(wanted_id)
    assert len(env.queued) == 1
    assert item["state"] == STATE_WAITING
    assert item["next_search_at"] is not None


def test_a_download_nobody_wants_any_more_is_cancelled(env):
    wanted_id = env.add()
    env.add(user=env.bob)
    env.scheduler.tick()

    env.service.remove_subscriber(wanted_id, env.alice["id"])
    env.scheduler.tick()
    assert env.cancelled == []

    env.service.remove_subscriber(wanted_id, env.bob["id"])
    env.scheduler.tick()
    assert env.cancelled == ["aa-1"]


@pytest.mark.parametrize(
    ("fmt", "downloaded"),
    [
        ("pdf", False),
        # Archives are unpacked afterwards, so they pass a format restriction.
        ("zip", True),
    ],
)
def test_only_enabled_formats_are_downloaded(env, fmt, downloaded):
    env.results = {"direct_download": ([_release("aa-x", fmt=fmt)], None)}
    with patch.object(ws, "supported_formats", return_value=frozenset({"epub", fmt})):
        wanted_id = env.add()
        env.scheduler.tick()

    assert [payload["source_id"] for payload, _, _ in env.queued] == (
        ["aa-x"] if downloaded else []
    )
    assert env.item(wanted_id)["state"] == (STATE_DOWNLOADING if downloaded else STATE_WAITING)


def test_the_library_only_counts_for_folder_targets(env):
    env.in_library = True
    targets = {env.alice["id"]: "folder:/books", env.bob["id"]: "email:bob@example.com"}
    with patch.object(ws, "delivery_target", side_effect=lambda user_id, _name: targets[user_id]):
        wanted_id = env.add()
        env.add(user=env.bob)
        env.scheduler.tick()

    assert env.pending(wanted_id) == [env.bob["id"]]
    assert env.queued[-1][1] == env.bob["id"]


@pytest.mark.parametrize("wake", ["rights", "auth_mode"])
def test_an_item_nobody_may_download_rests_until_rights_change(env, wake):
    env.policy = DENY_ALL
    wanted_id = env.add()
    env.scheduler.tick()

    item = env.item(wanted_id)
    assert env.queued == []
    assert item["state"] == STATE_WAITING
    assert item["next_search_at"] is None
    assert "No source" in item["last_result"]

    if wake == "rights":
        env.user_db.set_user_settings(env.alice["id"], {"REQUEST_POLICY_DEFAULT_EBOOK": "download"})
    else:
        env.scheduler._resolve_auth_mode = lambda: "none"
    env.scheduler.tick()
    assert len(env.queued) == 1
    assert env.queued[-1][1] == env.alice["id"]


def test_release_payload_matches_the_release_modal():
    release = Release(
        source="prowlarr",
        source_id="idx:guid",
        title="Dune",
        format="epub",
        size="1 MB",
        language="en",
        indexer="idx",
        seeders=5,
        extra={"downloads": 3, "author": "Frank Herbert"},
    )
    payload = ws.release_to_queue_payload(release, {**BOOK, "year": "1965"})

    assert payload == {
        "source": "prowlarr",
        "source_id": "idx:guid",
        "title": "Dune",
        "author": "Frank Herbert",
        "year": "1965",
        "format": "epub",
        "size": "1 MB",
        "downloads": 3,
        "indexer": "idx",
        "seeders": 5,
        "extra": {"downloads": 3, "author": "Frank Herbert"},
        "content_type": "ebook",
        "language": "en",
    }


# -- delivery to several targets ---------------------------------------------------


def _downloaded(env, wanted_id, tmp_path):
    """The current download finished its file; extra targets get held copies of it."""
    book = tmp_path / "download" / "Dune.epub"
    book.parent.mkdir(exist_ok=True)
    book.write_text("book")
    env.scheduler.on_downloaded_file(env.task(wanted_id), book)


def _held_copies(tmp_path):
    return list((tmp_path / "held").rglob("Dune.epub"))


@pytest.fixture
def two_targets(env, tmp_path):
    targets = {env.alice["id"]: "folder:/books/alice", env.bob["id"]: "email:bob@example.com"}
    with (
        patch.object(ws, "delivery_target", side_effect=lambda user_id, _name: targets[user_id]),
        patch.object(ws, "_held_copies_dir", return_value=tmp_path / "held"),
    ):
        yield targets


def _delivering_to_bob(env, tmp_path):
    """Alice's download completed; Bob's delivery task from a copy is queued."""
    wanted_id = env.add()
    env.add(user=env.bob)
    env.scheduler.tick()
    _downloaded(env, wanted_id, tmp_path)
    env.finish(wanted_id, QueueStatus.COMPLETE)
    return wanted_id, env.queued[-1][0]["source_id"]


@pytest.mark.usefixtures("two_targets")
def test_another_target_gets_its_own_delivery_task_from_a_copy(env, tmp_path):
    wanted_id, extra_id = _delivering_to_bob(env, tmp_path)

    _delivery, user_id, username = env.queued[-1]
    assert (user_id, username) == (env.bob["id"], "bob")
    assert extra_id == f"aa-1~wanted-{wanted_id}-1"
    assert env.queued_kwargs[-1]["wanted_id"] == wanted_id
    assert env.queued_kwargs[-1]["staged_path"].endswith("Dune.epub")
    # Bob counts as delivered only once his delivery task completes.
    assert env.item(wanted_id)["state"] == STATE_DOWNLOADING
    extra = SimpleNamespace(wanted_id=wanted_id, task_id=extra_id, status_message=None)
    env.scheduler.on_task_terminal(extra_id, QueueStatus.COMPLETE, extra)

    subscribers = env.service.list_subscribers(wanted_id)
    assert [sub["delivery_task_id"] for sub in subscribers] == ["aa-1", extra_id]
    assert env.item(wanted_id)["state"] == STATE_OBTAINED
    assert not any((tmp_path / "held").iterdir())


@pytest.mark.usefixtures("two_targets")
def test_copies_are_dropped_when_the_download_fails(env, tmp_path):
    wanted_id = env.add()
    env.add(user=env.bob)
    env.scheduler.tick()
    _downloaded(env, wanted_id, tmp_path)
    assert _held_copies(tmp_path)

    env.finish(wanted_id, QueueStatus.ERROR)

    assert not _held_copies(tmp_path)


@pytest.mark.parametrize("owner_leaves", [False, True])
def test_a_shared_target_needs_no_copy(env, tmp_path, owner_leaves):
    with (
        patch.object(ws, "delivery_target", return_value="folder:/books"),
        patch.object(ws, "_held_copies_dir", return_value=tmp_path / "held"),
    ):
        wanted_id = env.add()
        env.add(user=env.bob)
        env.scheduler.tick()
        if owner_leaves:
            env.service.remove_subscriber(wanted_id, env.alice["id"])
        _downloaded(env, wanted_id, tmp_path)
        env.finish(wanted_id, QueueStatus.COMPLETE)

    # The download lands in the folder everyone shares, so it delivers to all of them.
    assert not (tmp_path / "held").exists()
    assert len(env.queued) == 1
    subscribers = env.service.list_subscribers(wanted_id)
    assert subscribers and all(sub["delivery_task_id"] == "aa-1" for sub in subscribers)
    assert env.item(wanted_id)["state"] == STATE_OBTAINED


@pytest.mark.usefixtures("two_targets")
def test_a_failed_extra_delivery_keeps_its_subscriber_waiting(env, tmp_path):
    wanted_id, extra_id = _delivering_to_bob(env, tmp_path)
    extra = SimpleNamespace(
        wanted_id=wanted_id, task_id=extra_id, status_message="Email send failed"
    )

    env.scheduler.on_task_terminal(extra_id, QueueStatus.ERROR, extra)

    assert env.item(wanted_id)["state"] == STATE_WAITING
    assert env.pending(wanted_id) == [env.bob["id"]]
    assert not _held_copies(tmp_path)


@pytest.mark.usefixtures("two_targets")
def test_an_extra_delivery_that_vanishes_is_reconciled(env, tmp_path):
    wanted_id, extra_id = _delivering_to_bob(env, tmp_path)
    env.queue_statuses[extra_id] = (None, None)

    env.scheduler._reconcile_downloads()

    assert env.item(wanted_id)["state"] == STATE_WAITING


@pytest.mark.usefixtures("two_targets")
def test_a_cancelled_extra_delivery_rejects_the_release(env, tmp_path):
    wanted_id, extra_id = _delivering_to_bob(env, tmp_path)
    extra = SimpleNamespace(
        wanted_id=wanted_id, task_id=extra_id, source="direct_download", status_message="Cancelled"
    )

    env.scheduler.on_task_terminal(extra_id, QueueStatus.CANCELLED, extra)

    assert env.item(wanted_id)["state"] == STATE_WAITING
    excluded = env.service.excluded_release_keys(wanted_id, 99)
    assert "id:direct_download:aa-1" in excluded


@pytest.mark.usefixtures("two_targets")
def test_no_copy_when_the_owner_no_longer_waits(env, tmp_path):
    wanted_id = env.add()
    env.add(user=env.bob)
    env.scheduler.tick()
    env.service.remove_subscriber(wanted_id, env.alice["id"])

    _downloaded(env, wanted_id, tmp_path)
    env.finish(wanted_id, QueueStatus.COMPLETE)

    # Bob's target is "extra" now: he gets his own delivery task from a copy.
    assert env.queued[-1][1] == env.bob["id"]
    assert env.queued_kwargs[-1]["staged_path"].endswith("Dune.epub")


@pytest.mark.usefixtures("two_targets")
def test_an_unhandled_end_of_an_unwanted_task_is_cleaned_up(env, tmp_path):
    wanted_id = env.add()
    env.add(user=env.bob)
    env.scheduler.tick()
    _downloaded(env, wanted_id, tmp_path)
    env.service.delete_item(wanted_id)
    env.queue_statuses["aa-1"] = (None, SimpleNamespace(wanted_id=wanted_id))

    env.scheduler.tick()

    assert env.cancelled == []
    assert env.scheduler._payloads == {}
    assert not _held_copies(tmp_path)


def test_a_user_template_makes_every_user_a_target(env):
    with (
        patch.object(
            ws.config,
            "get",
            side_effect=lambda key, default=None, user_id=None: {
                "BOOKS_OUTPUT_MODE": "folder",
                "TEMPLATE_ORGANIZE": "{User}/{Author}/{Title}",
            }.get(key, default),
        ),
        patch("shelfmark.core.utils.get_destination", return_value="/books"),
    ):
        assert ws.delivery_target(1, "alice") != ws.delivery_target(2, "bob")


# -- failures on our side and reconciliation ---------------------------------------


@pytest.mark.parametrize(
    ("message", "error_type"),
    [
        ("Destination not writable: /books (Permission denied)", "StatusCallbackError"),
        ("Grimmory upload failed (500)", "StatusCallbackError"),
        ("Failed to add to qBittorrent: connection refused", "StatusCallbackError"),
        # A worker exception happened on our side, whatever its message says.
        ("Download failed: KeyError", "KeyError"),
    ],
)
def test_a_local_failure_is_no_strike_and_backs_off(env, notes, message, error_type):
    wanted_id = env.add()
    env.scheduler.tick()
    notes.clear()

    env.finish(wanted_id, QueueStatus.ERROR, message, last_error_type=error_type)

    item = env.item(wanted_id)
    assert item["state"] == STATE_WAITING
    assert item["backoff_level"] == 1
    assert "could not be delivered" in item["last_result"]
    assert env.service.list_attempts(wanted_id)[0]["status"] == "cancelled"
    assert [event for event, user_id, _ in notes if user_id is None] == ["download_failed"]


def test_a_lost_task_is_reconciled(env):
    wanted_id = env.add()
    env.scheduler.tick()
    env.queue_statuses["aa-1"] = (None, None)

    env.scheduler.tick()

    assert env.queued[-1][0]["source_id"] == "libgen:1"
    assert env.service.list_attempts(wanted_id)[-1]["status"] == "cancelled"


def test_a_missed_terminal_event_is_reconciled(env):
    wanted_id = env.add()
    env.scheduler.tick()
    env.queue_statuses["aa-1"] = (QueueStatus.COMPLETE, env.task(wanted_id))

    env.scheduler.tick()

    assert env.item(wanted_id)["state"] == STATE_OBTAINED


def test_queue_failure_after_the_task_was_added_still_counts(env):
    env.queue_result = (False, "broadcast failed")
    wanted_id = env.add()
    env.queue_statuses["aa-1"] = (QueueStatus.QUEUED, SimpleNamespace(wanted_id=wanted_id))

    env.scheduler.tick()

    assert len(env.queued) == 1
    assert env.item(wanted_id)["current_task_id"] == "aa-1"


def test_a_manually_retried_old_attempt_still_delivers(env):
    wanted_id = env.add()
    env.scheduler.tick()
    env.finish(wanted_id, QueueStatus.ERROR)
    env.scheduler.tick()
    env.service.update_item(wanted_id, state=STATE_WAITING, current_task_id=None)

    old = env.task(wanted_id, task_id="aa-1", source="direct_download")
    env.scheduler.on_task_terminal("aa-1", QueueStatus.COMPLETE, old)

    assert env.item(wanted_id)["state"] == STATE_OBTAINED


# -- races and settings changes ----------------------------------------------------


def test_someone_joining_while_an_item_starts_to_rest_wakes_it(env):
    env.policy = DENY_ALL
    admin = env.user_db.create_user(username="carol", role="admin")
    wanted_id = env.add()

    def join_during_the_pass(*_args):
        env.add(user=admin)
        return False

    with patch("shelfmark.core.library_index.is_in_library", side_effect=join_during_the_pass):
        env.scheduler.tick()
    assert env.item(wanted_id)["next_search_at"] is not None

    env.scheduler.tick()
    assert env.queued[-1][1] == admin["id"]


def test_new_users_without_bookmarks_do_not_reset_backoff(env):
    env.results = {}
    wanted_id = env.add()
    env.scheduler.tick()
    level = env.item(wanted_id)["backoff_level"]

    env.user_db.create_user(username="dave")
    env.scheduler.tick()

    assert env.item(wanted_id)["backoff_level"] == level


def test_switching_off_during_a_search_queues_nothing(env):
    enabled = iter([True, False])
    with patch.object(ws, "wanted_enabled", side_effect=lambda: next(enabled, False)):
        wanted_id = env.add()
        env.scheduler.tick()

    assert env.queued == []
    assert env.item(wanted_id)["state"] == STATE_WAITING
    assert env.item(wanted_id)["next_search_at"] is not None


# -- found notice and format wait --------------------------------------------------


def test_finding_a_release_is_announced_once_per_search(env, notes):
    wanted_id = env.add()
    env.scheduler.tick()
    env.finish(wanted_id, QueueStatus.ERROR, "Download failed")
    env.scheduler.tick()

    assert len(env.queued) == 2
    assert notes == [
        ("wanted_grabbed", None, "Anna's Archive"),
        ("wanted_grabbed", env.alice["id"], "Anna's Archive"),
    ]


def test_the_found_notice_skips_subscribers_the_source_cannot_serve(env, notes):
    env.policy = NO_DIRECT_DOWNLOAD
    env.user_db.set_user_settings(
        env.bob["id"],
        {
            "REQUEST_POLICY_RULES": [
                {"source": "direct_download", "content_type": "ebook", "mode": "download"}
            ]
        },
    )
    env.add()
    env.add(user=env.bob)

    env.scheduler.tick()

    assert env.queued[-1][0]["source"] == "direct_download"
    assert [user_id for _e, user_id, _d in notes if user_id is not None] == [env.bob["id"]]


def _joined(env, wanted_id, when):
    """Pretend the only subscriber bookmarked the item at ``when``."""
    subscriber = {**env.service.list_subscribers(wanted_id)[0], "created_at": when}
    return patch.object(env.service, "list_subscribers", return_value=[subscriber])


def test_lower_formats_wait_and_the_next_search_comes_no_later_than_the_wait_ends(env):
    env.results = {"direct_download": ([_release("aa-mobi", fmt="mobi")], None)}
    wanted_id = env.add()
    env.service.update_item(wanted_id, backoff_level=2)
    # Bookmarked long enough ago that the 7-day wait ends in about two hours.
    joined = (datetime.now(UTC) - timedelta(days=7) + timedelta(hours=2)).isoformat()
    with patch.object(ws, "format_wait", return_value=(None, 7.0)), _joined(env, wanted_id, joined):
        env.scheduler.tick()

    item = env.item(wanted_id)
    assert env.queued == []
    assert item["state"] == STATE_WAITING
    assert item["last_result"].startswith("Only MOBI found; waiting for EPUB until ")
    next_search = datetime.fromisoformat(item["next_search_at"])
    assert next_search <= datetime.now(UTC) + timedelta(hours=2, minutes=1)


def test_the_wait_for_a_format_ends(env):
    env.results = {"direct_download": ([_release("aa-mobi", fmt="mobi")], None)}
    wanted_id = env.add()
    with patch.object(ws, "format_wait", return_value=(None, 7.0)), _joined(env, wanted_id, DUE):
        env.scheduler.tick()

    assert env.queued[-1][0]["source_id"] == "aa-mobi"


def test_a_switched_off_cutoff_still_marks_the_line(env):
    env.results = {"direct_download": ([_release("aa-mobi", fmt="mobi")], None)}
    with (
        patch.object(ws, "format_wait", return_value=("pdf", 7.0)),
        patch.object(ws, "format_order", return_value=("epub", "mobi", "pdf")),
    ):
        env.add()
        env.scheduler.tick()

    assert env.queued[-1][0]["source_id"] == "aa-mobi"


# -- release dates -----------------------------------------------------------------


def test_an_unreleased_book_waits_without_searching(env):
    wanted_id = env.add(release_date="2999-03-12")

    env.scheduler.tick()

    item = env.item(wanted_id)
    assert env.search_kwargs == []
    assert item["state"] == STATE_WAITING
    assert item["search_count"] == 0
    # The date is checked again within a week rather than trusted until 2999.
    next_search = datetime.fromisoformat(item["next_search_at"])
    assert next_search <= datetime.now(UTC) + timedelta(days=7, minutes=1)


def test_a_release_date_found_in_the_metadata_defers_the_search(env):
    future = BookMetadata(
        provider="hardcover",
        provider_id="1",
        title="Dune",
        authors=["Frank Herbert"],
        release_date="2999-03-12",
    )
    with patch.object(ws.WantedScheduler, "_resolve_book", staticmethod(lambda _data: future)):
        wanted_id = env.add()
        env.scheduler.tick()

    item = env.item(wanted_id)
    assert env.queued == []
    assert item["book_data"]["release_date"] == "2999-03-12"
    assert item["search_count"] == 0
    assert item["last_result"] == "Not released yet"


def test_only_a_full_date_defers_and_odd_dates_never_break_the_scheduler():
    assert ws._search_starts({"year": "2999"}) is None
    assert ws._search_starts({"release_date": "2001-01-01"}) is None
    assert ws._search_starts({"release_date": "0001-01-01"}) is None
    assert ws._search_starts({"release_date": "not a date"}) is None
    assert ws._search_starts({"release_date": "2999-03-12"}).isoformat().startswith("2999-03-11")


def test_search_now_searches_a_book_not_out_yet(env):
    wanted_id = env.add(release_date="2999-03-12")
    env.scheduler.tick()
    assert env.queued == []

    env.service.update_item(wanted_id, next_search_at=DUE)
    env.scheduler.search_now(wanted_id)
    env.scheduler.tick()

    assert len(env.queued) == 1
    assert env.item(wanted_id)["state"] == STATE_DOWNLOADING


# -- picking a release by hand -----------------------------------------------------


def test_a_failed_pick_keeps_the_planned_search(env):
    wanted_id = env.add()
    env.service.update_item(wanted_id, next_search_at=LATER, backoff_level=3)
    env.scheduler.grab(wanted_id, _release("aa-9"), {"user_id": env.alice["id"]})

    env.finish(wanted_id, QueueStatus.ERROR, "Download failed")
    env.scheduler.tick()

    item = env.item(wanted_id)
    assert item["state"] == STATE_WAITING
    assert item["backoff_level"] == 3
    assert item["next_search_at"].startswith("2999-01-01")
    assert item["last_result"] == "The picked release failed"


def test_a_release_already_downloading_cannot_be_picked_again(env):
    first = env.add()
    second = env.add(title="Dune Messiah", provider_id="2")
    env.scheduler.tick()
    in_flight = env.item(first)["current_task_id"]

    queued, error = env.scheduler.grab(second, _release(in_flight), {"user_id": env.alice["id"]})

    assert queued is False
    assert error == "This release is already downloading"


def test_releases_are_evaluated_like_the_wanted_list_would(env):
    wanted_id = env.add()
    releases = [
        _release("aa-1"),
        _release("aa-pdf", fmt="pdf"),
        _release("aa-3", title="Dune Messiah"),
        _release("x-1", source="irc"),
    ]

    reasons = env.scheduler.evaluate(wanted_id, releases)

    assert reasons["direct_download:aa-1"] is None
    assert reasons["direct_download:aa-pdf"] == "PDF is not a wanted format"
    assert reasons["direct_download:aa-3"].startswith("Names another work")
    assert reasons["irc:x-1"] == "Source not used by the wanted list"


@pytest.mark.usefixtures("two_targets")
def test_a_picked_release_serves_only_the_user_who_picked_it(env, tmp_path):
    wanted_id = env.add()
    env.add(user=env.bob)
    env.service.update_item(wanted_id, next_search_at=LATER)

    queued, error = env.scheduler.grab(
        wanted_id, _release("aa-2", fmt="mobi"), {"user_id": env.bob["id"], "username": "bob"}
    )

    assert (queued, error) == (True, None)
    assert env.queued[-1][0]["source_id"] == "aa-2"
    assert env.queued_kwargs[-1]["wanted_id"] == wanted_id
    assert env.item(wanted_id)["state"] == STATE_DOWNLOADING
    _downloaded(env, wanted_id, tmp_path)
    env.finish(wanted_id, QueueStatus.COMPLETE)
    # Nobody vetted Bob's pick for Alice: no copy for her, she keeps waiting.
    assert len(env.queued) == 1
    assert not _held_copies(tmp_path)
    assert env.pending(wanted_id) == [env.alice["id"]]
    assert env.item(wanted_id)["state"] == STATE_WAITING


def test_a_picked_release_needs_a_waiting_item(env):
    wanted_id = env.add()
    env.service.update_item(wanted_id, state="paused")

    queued, error = env.scheduler.grab(wanted_id, _release("aa-2"), {"user_id": env.alice["id"]})

    assert queued is False
    assert "paused" in error
    assert env.queued == []


def test_releases_from_the_frontend_are_validated():
    release = ws.release_from_dict(
        {"source": "libgen", "source_id": "1", "title": "Dune", "seeders": "7", "extra": None}
    )
    assert release is not None
    assert release.seeders is None
    assert release.extra == {}
    assert ws.release_from_dict({"source": "libgen", "title": "Dune"}) is None
    assert ws.release_from_dict(["nope"]) is None


# -- indexer feeds -----------------------------------------------------------------


class _FeedSource:
    def __init__(self, releases):
        self.releases = releases
        self.reads = 0

    def recent_releases(self, content_type):
        assert content_type == "ebook"
        self.reads += 1
        return list(self.releases)


@pytest.fixture
def feed(env):
    source = _FeedSource([_release("p-1", source="prowlarr", title="Frank Herbert - Dune")])
    sources = [
        *SOURCES,
        {"name": "prowlarr", "enabled": True, "supported_content_types": ["ebook"]},
    ]
    env.results = {}
    with (
        patch.object(ws, "wanted_sources", return_value=["direct_download", "prowlarr"]),
        patch("shelfmark.release_sources.list_available_sources", return_value=sources),
        patch("shelfmark.release_sources.get_source", return_value=source),
        patch.object(ws, "feed_interval_minutes", return_value=15.0),
    ):
        yield source


def _searched_once(env, **book):
    wanted_id = env.add(**book)
    env.service.update_item(wanted_id, search_count=1, next_search_at=LATER)
    return wanted_id


def test_feeds_are_read_once_per_interval_and_each_release_matched_once(env, feed):
    wanted_id = _searched_once(env)
    env.scheduler.tick()
    # A feed release is downloaded for a waiting bookmark without a search.
    assert env.queued[-1][0]["source_id"] == "p-1"
    assert env.item(wanted_id)["state"] == STATE_DOWNLOADING
    assert len(env.scheduler._searches) == 0
    env.finish(wanted_id, QueueStatus.ERROR, "Download failed")
    env.scheduler.tick()
    assert feed.reads == 1

    env.scheduler._feed_checked_at -= 15 * 60
    env.service.update_item(
        wanted_id, state=STATE_WAITING, current_task_id=None, next_search_at=LATER
    )
    env.scheduler.tick()

    assert feed.reads == 2
    assert [payload["source_id"] for payload, _, _ in env.queued] == ["p-1"]


def test_feeds_can_be_switched_off(env, feed):
    env.add()
    with patch.object(ws, "feed_interval_minutes", return_value=0.0):
        env.scheduler.tick()
    assert feed.reads == 0


def test_feeds_skip_books_not_released_yet_and_never_searched(env, feed):
    unreleased = _searched_once(env, release_date="2999-03-12")
    fresh = env.add(title="Frank Herbert - Dune", provider_id="2")
    released = _searched_once(env, provider_id="3")
    env.service.update_item(released, state="paused")

    env.scheduler.tick()

    assert env.item(unreleased)["state"] == STATE_WAITING
    # Never searched yet: its own first search runs the metadata checks first.
    assert all(payload["source"] != "prowlarr" for payload, _, _ in env.queued)
    assert env.item(fresh)["search_count"] == 1


def test_a_failed_feed_release_makes_a_full_search_due(env, feed):
    wanted_id = _searched_once(env)
    env.service.update_item(wanted_id, backoff_level=2)
    env.scheduler.tick()

    env.finish(wanted_id, QueueStatus.ERROR, "Download failed")
    env.scheduler._continue_cycles()

    item = env.item(wanted_id)
    assert item["backoff_level"] == 2
    assert item["last_result"] == "The release from the feed failed"
    assert item["next_search_at"] < "2999"


def test_a_feed_match_for_a_book_in_the_library_leaves_it_to_the_search(env, feed):
    wanted_id = _searched_once(env)
    env.in_library = True

    env.scheduler._check_feeds()

    assert env.queued == []
    assert env.item(wanted_id)["next_search_at"] < "2999"


def test_one_failing_match_does_not_cost_the_others_the_feed(env, feed):
    first = _searched_once(env)
    second = _searched_once(env, provider_id="2", isbn_13=None)
    calls = []
    original = env.scheduler._match_feed

    def flaky(item, fresh, sources):
        calls.append(item["id"])
        if item["id"] == first:
            raise RuntimeError("boom")
        return original(item, fresh, sources)

    with patch.object(env.scheduler, "_match_feed", side_effect=flaky):
        env.scheduler._check_feeds()

    assert sorted(calls) == sorted([first, second])
    assert env.item(second)["state"] == STATE_DOWNLOADING


# -- watches and approvals ---------------------------------------------------------


def _watch(env, seen):
    watch, _ = env.service.add_watch(
        user_id=env.alice["id"],
        provider="hardcover",
        fields={"series": "id:1"},
        label="D",
        seen=seen,
    )
    env.service.update_watch(watch["id"], seen=seen, last_result="", synced_at="2000")
    return watch["id"]


def test_a_watch_bookmarks_only_books_it_has_not_seen(env):
    watch_id = _watch(env, ["hardcover:1"])
    books = [
        BookMetadata(provider="hardcover", provider_id="1", title="Dune"),
        BookMetadata(provider="hardcover", provider_id="2", title="Dune Messiah"),
    ]
    with patch("shelfmark.core.wanted_watches.fetch_watch_books", return_value=(books, 2)):
        env.scheduler._sync_watch()
        env.scheduler._sync_watch()

    items = env.service.list_items(user_id=env.alice["id"], include_all=False)
    assert [item["provider_id"] for item in items] == ["2"]
    synced = env.service.get_watch(watch_id)
    assert synced["seen"] == ["hardcover:1", "hardcover:2"]
    assert synced["last_result"] == "Bookmarked 1 new book(s)"


def test_a_watch_of_someone_without_rights_bookmarks_nothing(env):
    env.policy = DENY_ALL
    watch_id = _watch(env, [])
    book = BookMetadata(provider="hardcover", provider_id="2", title="Dune Messiah")
    with patch(
        "shelfmark.core.wanted_watches.fetch_watch_books", return_value=([book], 1)
    ) as fetch:
        env.scheduler._sync_watch()

    fetch.assert_not_called()
    assert env.service.get_watch(watch_id)["last_result"].startswith("Not allowed")


def test_a_watch_made_during_an_outage_takes_its_first_read_as_the_start(env):
    watch_id = _watch(env, None)
    books = [BookMetadata(provider="hardcover", provider_id=str(n), title="Dune") for n in (1, 2)]
    with patch("shelfmark.core.wanted_watches.fetch_watch_books", return_value=(books, 2)):
        env.scheduler._sync_watch()

    assert env.service.list_items(user_id=env.alice["id"], include_all=False) == []
    assert env.service.get_watch(watch_id)["seen"] == ["hardcover:1", "hardcover:2"]


def test_a_book_that_could_not_be_bookmarked_is_tried_again(env):
    watch_id = _watch(env, [])
    book = BookMetadata(provider="hardcover", provider_id="2", title="Dune Messiah")
    with (
        patch("shelfmark.core.wanted_watches.fetch_watch_books", return_value=([book], 1)),
        patch.object(env.service, "add", side_effect=RuntimeError("db busy")),
    ):
        env.scheduler._sync_watch()

    assert env.service.get_watch(watch_id)["seen"] == []


@pytest.mark.parametrize("admin_demoted", [False, True])
def test_an_approval_lets_someone_who_must_request_download_while_its_admin_is_one(
    env, admin_demoted
):
    env.policy = DENY_ALL
    admin = env.user_db.create_user(username="root", role="admin")
    env.service.add(user_id=env.alice["id"], book_data=BOOK, approved_by=admin["id"])
    if admin_demoted:
        env.user_db.update_user(admin["id"], role="user")

    env.scheduler.tick()

    assert [user_id for _payload, user_id, _name in env.queued] == (
        [] if admin_demoted else [env.alice["id"]]
    )
