"""Tests for the wanted list background job."""

from __future__ import annotations

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
        self.in_library = False
        self.policy = {"REQUESTS_ENABLED": False}
        self.changed: list[int | None] = []
        self.scheduler = ws.WantedScheduler(
            self.service,
            self.user_db,
            resolve_auth_mode=lambda: "builtin",
            queue_release=self._queue_release,
            task_lookup=self._lookup,
            on_change=self.changed.append,
        )

    def _queue_release(self, payload, priority, *, user_id=None, username=None, **kwargs):
        del priority
        self.queued.append((payload, user_id, username))
        self.queued_kwargs.append(kwargs)
        return self.queue_result

    def _lookup(self, task_id):
        if task_id in self.queue_statuses:
            return self.queue_statuses[task_id]
        for payload, user_id, _username in self.queued:
            if payload["source_id"] == task_id:
                kwargs = self.queued_kwargs[self.queued.index((payload, user_id, _username))]
                return QueueStatus.DOWNLOADING, SimpleNamespace(
                    wanted_id=kwargs.get("wanted_id"), task_id=task_id, user_id=user_id
                )
        return None, None

    def _search(self, source, book, **kwargs):
        del book, kwargs
        releases, error = self.results.get(source, ([], None))
        return (None if error else object()), releases, error

    def add(self, user=None, **book):
        item, _ = self.service.add(user_id=(user or self.alice)["id"], book_data={**BOOK, **book})
        return item["id"]

    def item(self, wanted_id):
        return self.service.get_item(wanted_id)

    def finish(self, wanted_id, status, message=None, **fields):
        item = self.item(wanted_id)
        payload, user_id, _username = self.queued[-1]
        task = SimpleNamespace(
            wanted_id=wanted_id,
            task_id=item["current_task_id"],
            source=payload["source"],
            user_id=user_id,
            status_message=message,
            last_error_type="StatusCallbackError" if status == QueueStatus.ERROR else None,
            staged_path=None,
        )
        for key, value in fields.items():
            setattr(task, key, value)
        self.scheduler.on_task_terminal(item["current_task_id"], status, task)


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


def test_a_failed_attempt_moves_on_to_the_next_release(env):
    wanted_id = env.add()
    env.scheduler.tick()

    env.finish(wanted_id, QueueStatus.ERROR, "Server error")
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
        env.service.update_item(wanted_id, next_search_at="2000-01-01T00:00:00+00:00")
        env.scheduler.tick()
        env.finish(wanted_id, QueueStatus.ERROR)
        env.scheduler.tick()
    env.service.update_item(wanted_id, next_search_at="2000-01-01T00:00:00+00:00")
    env.scheduler.tick()

    assert len(env.queued) == 2
    assert env.item(wanted_id)["last_result"].endswith("no matching release")


def test_completion_marks_the_item_obtained_and_delivered(env):
    wanted_id = env.add()
    env.add(user=env.bob)
    env.scheduler.tick()

    env.finish(wanted_id, QueueStatus.COMPLETE)

    item = env.item(wanted_id)
    assert item["state"] == STATE_OBTAINED
    assert item["obtained_reason"] == "downloaded"
    assert all(sub["delivered_at"] for sub in env.service.list_subscribers(wanted_id))


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


def test_a_book_already_in_the_library_is_not_downloaded(env):
    env.in_library = True
    wanted_id = env.add()

    env.scheduler.tick()

    assert env.queued == []
    item = env.item(wanted_id)
    assert item["state"] == STATE_OBTAINED
    assert item["obtained_reason"] == "in_library"


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
    env.service.update_item(wanted_id, next_search_at="2000-01-01T00:00:00+00:00")
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
    env.service.update_item(wanted_id, next_search_at="2999-01-01T00:00:00+00:00", backoff_level=2)

    with patch("shelfmark.core.settings_registry.load_config_file", return_value={"changed": True}):
        env.scheduler.tick()

    item = env.item(wanted_id)
    # Searched again at once, and the interval ladder started over from its first step.
    assert item["search_count"] == 2
    assert item["backoff_level"] == 1


def test_releases_from_sources_a_subscriber_may_not_use_are_skipped(env):
    env.policy = {
        "REQUESTS_ENABLED": True,
        "REQUEST_POLICY_DEFAULT_EBOOK": "download",
        "REQUEST_POLICY_RULES": [
            {"source": "direct_download", "content_type": "ebook", "mode": "request_release"}
        ],
    }
    env.add()

    env.scheduler.tick()

    assert [payload["source"] for payload, _, _ in env.queued] == ["libgen"]


def test_a_release_that_cannot_be_queued_is_skipped_without_a_strike(env):
    env.queue_result = (False, "Source unavailable")
    env.queue_statuses = dict.fromkeys(("aa-1", "aa-2", "libgen:1"), (None, None))
    wanted_id = env.add()

    env.scheduler.tick()

    assert len(env.queued) == 3
    assert {attempt["status"] for attempt in env.service.list_attempts(wanted_id)} == {"cancelled"}
    assert env.item(wanted_id)["state"] == STATE_WAITING


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


def test_disabled_wanted_list_does_nothing(env):
    env.add()
    with patch.object(ws, "wanted_enabled", return_value=False):
        env.scheduler.tick()
    assert env.queued == []


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


class _Task(SimpleNamespace):
    pass


def _downloaded(env, wanted_id, tmp_path):
    item = env.item(wanted_id)
    payload, user_id, _ = env.queued[-1]
    book = tmp_path / "download" / "Dune.epub"
    book.parent.mkdir(exist_ok=True)
    book.write_text("book")
    task = _Task(
        wanted_id=wanted_id,
        task_id=item["current_task_id"],
        source=payload["source"],
        user_id=user_id,
        status_message=None,
    )
    env.scheduler.on_downloaded_file(task, book)
    return task


@pytest.fixture
def two_targets(env, tmp_path):
    targets = {env.alice["id"]: "folder:/books/alice", env.bob["id"]: "email:bob@example.com"}
    with (
        patch.object(ws, "delivery_target", side_effect=lambda user_id, _name: targets[user_id]),
        patch.object(ws, "_held_copies_dir", return_value=tmp_path / "held"),
    ):
        yield targets


def test_another_target_gets_its_own_delivery_task_from_a_copy(env, two_targets, tmp_path):
    del two_targets
    wanted_id = env.add()
    env.add(user=env.bob)
    env.scheduler.tick()
    task = _downloaded(env, wanted_id, tmp_path)

    env.scheduler.on_task_terminal(task.task_id, QueueStatus.COMPLETE, task)

    delivery, user_id, username = env.queued[-1]
    assert (user_id, username) == (env.bob["id"], "bob")
    assert delivery["source_id"] == f"aa-1~wanted-{wanted_id}-1"
    assert env.queued_kwargs[-1]["wanted_id"] == wanted_id
    assert env.queued_kwargs[-1]["staged_path"].endswith("Dune.epub")
    # Bob counts as delivered only once his delivery task completes.
    assert env.item(wanted_id)["state"] == STATE_DOWNLOADING
    extra = SimpleNamespace(wanted_id=wanted_id, task_id=delivery["source_id"], status_message=None)
    env.scheduler.on_task_terminal(delivery["source_id"], QueueStatus.COMPLETE, extra)

    subscribers = env.service.list_subscribers(wanted_id)
    assert [sub["delivery_task_id"] for sub in subscribers] == ["aa-1", delivery["source_id"]]
    assert env.item(wanted_id)["state"] == STATE_OBTAINED


def test_copies_are_dropped_when_the_download_fails(env, two_targets, tmp_path):
    del two_targets
    wanted_id = env.add()
    env.add(user=env.bob)
    env.scheduler.tick()
    task = _downloaded(env, wanted_id, tmp_path)
    assert any((tmp_path / "held").rglob("Dune.epub"))

    env.scheduler.on_task_terminal(task.task_id, QueueStatus.ERROR, task)

    assert not any((tmp_path / "held").rglob("Dune.epub"))


def test_a_shared_target_needs_no_copy(env, tmp_path):
    with (
        patch.object(ws, "delivery_target", return_value="folder:/books"),
        patch.object(ws, "_held_copies_dir", return_value=tmp_path / "held"),
    ):
        wanted_id = env.add()
        env.add(user=env.bob)
        env.scheduler.tick()
        task = _downloaded(env, wanted_id, tmp_path)
        env.scheduler.on_task_terminal(task.task_id, QueueStatus.COMPLETE, task)

    assert not (tmp_path / "held").exists()
    assert len(env.queued) == 1
    assert all(sub["delivered_at"] for sub in env.service.list_subscribers(wanted_id))


def test_a_target_without_a_copy_keeps_its_subscriber_waiting(env, two_targets):
    del two_targets
    wanted_id = env.add()
    env.add(user=env.bob)
    env.scheduler.tick()
    item = env.item(wanted_id)
    task = _Task(
        wanted_id=wanted_id,
        task_id=item["current_task_id"],
        source="direct_download",
        user_id=env.alice["id"],
        status_message=None,
    )

    env.scheduler.on_task_terminal(task.task_id, QueueStatus.COMPLETE, task)

    item = env.item(wanted_id)
    assert item["state"] == STATE_WAITING
    pending = [
        sub["user_id"] for sub in env.service.list_subscribers(wanted_id) if not sub["delivered_at"]
    ]
    assert pending == [env.bob["id"]]


def test_obtaining_notifies_the_admin_and_the_delivered_subscribers(env):
    sent: list[tuple[str, int | None, str]] = []
    wanted_id = env.add()
    env.add(user=env.bob)
    env.scheduler.tick()
    with (
        patch(
            "shelfmark.core.notifications.notify_admin",
            side_effect=lambda event, ctx: sent.append(("admin", None, ctx.detail)),
        ),
        patch(
            "shelfmark.core.notifications.notify_user",
            side_effect=lambda user_id, event, ctx: sent.append(("user", user_id, ctx.detail)),
        ),
    ):
        env.finish(wanted_id, QueueStatus.COMPLETE)

    assert sorted(sent, key=str) == sorted(
        [
            ("admin", None, "was downloaded"),
            ("user", env.alice["id"], "was downloaded"),
            ("user", env.bob["id"], "was downloaded"),
        ],
        key=str,
    )


def test_a_book_in_the_library_notifies_with_that_detail(env):
    env.in_library = True
    env.add()
    details: list[str] = []
    with (
        patch("shelfmark.core.notifications.notify_admin"),
        patch(
            "shelfmark.core.notifications.notify_user",
            side_effect=lambda _uid, _event, ctx: details.append(ctx.detail),
        ),
    ):
        env.scheduler.tick()

    assert details == ["is already in your library"]


def test_changes_are_reported_to_listeners(env):
    wanted_id = env.add()
    env.scheduler.tick()

    assert wanted_id in env.changed


def test_a_local_failure_is_no_strike_and_backs_off(env):
    wanted_id = env.add()
    env.scheduler.tick()
    with patch("shelfmark.core.notifications.notify_admin") as notify_admin:
        env.finish(
            wanted_id,
            QueueStatus.ERROR,
            "Destination not writable: /books (Permission denied)",
            staged_path="/tmp/shelfmark/Dune.epub",
        )

    item = env.item(wanted_id)
    assert item["state"] == STATE_WAITING
    assert item["backoff_level"] == 1
    assert "could not be delivered" in item["last_result"]
    assert env.service.list_attempts(wanted_id)[0]["status"] == "cancelled"
    notify_admin.assert_called_once()


def test_a_release_without_a_usable_book_is_a_strike_and_moves_on(env):
    wanted_id = env.add()
    env.scheduler.tick()
    env.finish(
        wanted_id,
        QueueStatus.ERROR,
        "No supported file formats found",
        staged_path="/tmp/shelfmark/Dune.zip",
    )
    env.scheduler.tick()

    assert [payload["source_id"] for payload, _, _ in env.queued] == ["aa-1", "libgen:1"]
    assert env.service.list_attempts(wanted_id)[-1]["status"] == "error"


def test_a_worker_exception_counts_as_local(env):
    wanted_id = env.add()
    env.scheduler.tick()
    with patch("shelfmark.core.notifications.notify_admin"):
        env.finish(
            wanted_id, QueueStatus.ERROR, "Download failed: KeyError", last_error_type="KeyError"
        )

    assert env.item(wanted_id)["state"] == STATE_WAITING


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
    task = SimpleNamespace(
        wanted_id=wanted_id,
        task_id="aa-1",
        source="direct_download",
        user_id=env.alice["id"],
        status_message=None,
    )
    env.queue_statuses["aa-1"] = (QueueStatus.COMPLETE, task)

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

    old = SimpleNamespace(
        wanted_id=wanted_id,
        task_id="aa-1",
        source="direct_download",
        user_id=env.alice["id"],
        status_message=None,
    )
    env.scheduler.on_task_terminal("aa-1", QueueStatus.COMPLETE, old)

    assert env.item(wanted_id)["state"] == STATE_OBTAINED


def test_all_languages_are_searched_explicitly(env):
    seen = []
    with (
        patch.object(ws.WantedScheduler, "_search_languages", staticmethod(lambda _subs: None)),
        patch(
            "shelfmark.core.release_search.search_source_releases",
            side_effect=lambda source, book, **kwargs: (
                seen.append(kwargs["languages"]) or env._search(source, book)
            ),
        ),
    ):
        env.add()
        env.scheduler.tick()

    assert seen and all(languages == ["all"] for languages in seen)


def test_a_release_queued_by_someone_else_is_not_taken_over(env):
    env.queue_result = (False, "Release is already in the download queue")
    env.queue_statuses = {
        "aa-1": (QueueStatus.DOWNLOADING, SimpleNamespace(wanted_id=None)),
        "aa-2": (None, None),
        "libgen:1": (None, None),
    }
    wanted_id = env.add()

    env.scheduler.tick()

    assert env.item(wanted_id)["state"] == STATE_WAITING
    assert {attempt["status"] for attempt in env.service.list_attempts(wanted_id)} == {"cancelled"}


def test_a_failed_extra_delivery_keeps_its_subscriber_waiting(env, two_targets, tmp_path):
    del two_targets
    wanted_id = env.add()
    env.add(user=env.bob)
    env.scheduler.tick()
    task = _downloaded(env, wanted_id, tmp_path)
    env.scheduler.on_task_terminal(task.task_id, QueueStatus.COMPLETE, task)
    assert env.item(wanted_id)["state"] == STATE_DOWNLOADING

    extra_id = env.queued[-1][0]["source_id"]
    extra = SimpleNamespace(
        wanted_id=wanted_id, task_id=extra_id, status_message="Email send failed"
    )
    env.scheduler.on_task_terminal(extra_id, QueueStatus.ERROR, extra)

    item = env.item(wanted_id)
    assert item["state"] == STATE_WAITING
    pending = [
        sub["user_id"] for sub in env.service.list_subscribers(wanted_id) if not sub["delivered_at"]
    ]
    assert pending == [env.bob["id"]]
    assert not any((tmp_path / "held").rglob("Dune.epub"))


def test_an_extra_delivery_that_vanishes_is_reconciled(env, two_targets, tmp_path):
    del two_targets
    wanted_id = env.add()
    env.add(user=env.bob)
    env.scheduler.tick()
    task = _downloaded(env, wanted_id, tmp_path)
    env.scheduler.on_task_terminal(task.task_id, QueueStatus.COMPLETE, task)
    env.queue_statuses[env.queued[-1][0]["source_id"]] = (None, None)

    env.scheduler._reconcile_downloads()

    assert env.item(wanted_id)["state"] == STATE_WAITING


def test_no_copy_when_the_owner_no_longer_waits(env, two_targets, tmp_path):
    del two_targets
    wanted_id = env.add()
    env.add(user=env.bob)
    env.scheduler.tick()
    env.service.remove_subscriber(wanted_id, env.alice["id"])

    task = _downloaded(env, wanted_id, tmp_path)
    env.scheduler.on_task_terminal(task.task_id, QueueStatus.COMPLETE, task)

    # Bob's target is "extra" now: he gets his own delivery task from a copy.
    assert env.queued[-1][1] == env.bob["id"]
    assert env.queued_kwargs[-1]["staged_path"].endswith("Dune.epub")


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
