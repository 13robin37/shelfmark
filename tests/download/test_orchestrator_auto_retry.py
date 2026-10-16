"""Opt-in automatic retry of failed Anna's Archive / Libgen downloads."""

from __future__ import annotations

from threading import Event
from unittest.mock import MagicMock

import pytest

from shelfmark.core.models import DownloadTask, QueueStatus
from shelfmark.core.queue import BookQueue


class _AvailableSource:
    display_name = "Test Source"

    def is_available(self):
        return True


class _FakeConfig:
    def __init__(self, values: dict[str, object]) -> None:
        self._values = values

    def get(self, key: str, default: object = None) -> object:
        return self._values.get(key, default)


_ENABLED = {
    "AUTO_RETRY_FAILED_DOWNLOADS": True,
    "AUTO_RETRY_MAX_ATTEMPTS": 2,
    "AUTO_RETRY_INITIAL_DELAY": 60,
    "AUTO_RETRY_MAX_DELAY": 1800,
}


@pytest.fixture
def orchestrator(monkeypatch):
    import shelfmark.download.orchestrator as orchestrator

    monkeypatch.setattr(orchestrator, "get_source", lambda _source: _AvailableSource())
    monkeypatch.setattr(orchestrator, "ws_manager", None)
    monkeypatch.setattr(orchestrator, "_auto_retry_counts", {})
    monkeypatch.setattr(orchestrator, "_auto_retry_due", {})
    # No jitter, so delays are exact.
    monkeypatch.setattr(orchestrator._RNG, "uniform", lambda _low, _high: 1.0)
    return orchestrator


def _setup(orchestrator, monkeypatch, *, source="direct_download", settings=None):
    task = DownloadTask(task_id="task-1", source=source, title="Flaky")
    queue = BookQueue()
    queue.add(task)
    queue.get_next()  # the coordinator has picked it up
    terminal: list[QueueStatus] = []
    queue.set_terminal_status_hook(lambda _id, status, _task: terminal.append(status))

    handler = MagicMock()

    def _download(_task, _cancel_flag, _progress_callback, status_callback):
        status_callback("error", "All sources failed")
        return None

    handler.download.side_effect = _download
    monkeypatch.setattr(orchestrator, "book_queue", queue)
    monkeypatch.setattr(orchestrator, "get_handler", lambda _source: handler)
    monkeypatch.setattr(
        orchestrator, "config", _FakeConfig(_ENABLED if settings is None else settings)
    )
    return task, queue, handler, terminal


def _fail_once(orchestrator, queue, task):
    orchestrator._process_single_download(task.task_id, Event())
    return queue.get_task_status(task.task_id)


def test_disabled_by_default_fails_immediately(orchestrator, monkeypatch):
    task, queue, _handler, terminal = _setup(orchestrator, monkeypatch, settings={})

    assert _fail_once(orchestrator, queue, task) == QueueStatus.ERROR
    assert terminal == [QueueStatus.ERROR]
    assert orchestrator._auto_retry_due == {}


def test_failed_download_is_parked_then_requeued_after_its_delay(orchestrator, monkeypatch):
    task, queue, _handler, terminal = _setup(orchestrator, monkeypatch)
    clock = [1000.0]
    monkeypatch.setattr(orchestrator.time, "monotonic", lambda: clock[0])

    assert _fail_once(orchestrator, queue, task) == QueueStatus.QUEUED
    assert terminal == []  # no "failed" notification / history entry yet
    assert task.status_message == "All sources failed - retry 1/2 in 60s"
    # Parked outside the priority queue: nothing to pick up while it waits.
    assert queue.get_next() is None

    orchestrator._requeue_due_auto_retries(now=1059.0)
    assert queue.get_next() is None

    orchestrator._requeue_due_auto_retries(now=1060.0)
    assert task.last_error_message is None
    assert queue.get_next() == (task.task_id, queue._cancel_flags[task.task_id])


def test_delay_doubles_and_retries_stop_at_the_limit(orchestrator, monkeypatch):
    task, queue, handler, terminal = _setup(orchestrator, monkeypatch)
    clock = [0.0]
    monkeypatch.setattr(orchestrator.time, "monotonic", lambda: clock[0])

    assert _fail_once(orchestrator, queue, task) == QueueStatus.QUEUED
    assert orchestrator._auto_retry_due[task.task_id] == 60.0
    orchestrator._requeue_due_auto_retries(now=60.0)
    queue.get_next()

    clock[0] = 100.0
    assert _fail_once(orchestrator, queue, task) == QueueStatus.QUEUED
    assert orchestrator._auto_retry_due[task.task_id] == 100.0 + 120.0
    assert task.status_message.endswith("retry 2/2 in 2 min")
    orchestrator._requeue_due_auto_retries(now=220.0)
    queue.get_next()

    assert _fail_once(orchestrator, queue, task) == QueueStatus.ERROR
    assert terminal == [QueueStatus.ERROR]
    assert handler.download.call_count == 3
    assert orchestrator._auto_retry_counts == {}


def test_delay_is_capped(orchestrator):
    assert orchestrator._auto_retry_delay(1, 60.0, 300.0) == 60.0
    assert orchestrator._auto_retry_delay(3, 60.0, 300.0) == 240.0
    assert orchestrator._auto_retry_delay(4, 60.0, 300.0) == 300.0


def test_libgen_downloads_are_retried(orchestrator, monkeypatch):
    task, queue, _handler, _terminal = _setup(orchestrator, monkeypatch, source="libgen")
    assert _fail_once(orchestrator, queue, task) == QueueStatus.QUEUED


def test_other_sources_are_not_retried(orchestrator, monkeypatch):
    task, queue, _handler, _terminal = _setup(orchestrator, monkeypatch, source="prowlarr")
    assert _fail_once(orchestrator, queue, task) == QueueStatus.ERROR


def test_post_processing_failures_are_not_retried(orchestrator, monkeypatch, tmp_path):
    task, queue, handler, _terminal = _setup(orchestrator, monkeypatch)
    downloaded = tmp_path / "book.epub"
    downloaded.write_text("book")
    handler.download.side_effect = None
    handler.download.return_value = str(downloaded)
    monkeypatch.setattr(orchestrator, "post_process_download", lambda *_a, **_k: None)

    assert _fail_once(orchestrator, queue, task) == QueueStatus.ERROR


def test_cancelling_a_parked_download_drops_its_retry(orchestrator, monkeypatch):
    task, queue, _handler, terminal = _setup(orchestrator, monkeypatch)
    monkeypatch.setattr(orchestrator.time, "monotonic", lambda: 0.0)

    assert _fail_once(orchestrator, queue, task) == QueueStatus.QUEUED
    assert orchestrator.cancel_download(task.task_id) is True
    assert terminal == [QueueStatus.CANCELLED]
    assert orchestrator._auto_retry_due == {}

    orchestrator._requeue_due_auto_retries(now=10_000.0)
    assert queue.get_task_status(task.task_id) == QueueStatus.CANCELLED
    assert queue.get_next() is None


def test_a_parked_task_cancelled_behind_our_back_is_not_requeued(orchestrator, monkeypatch):
    task, queue, _handler, _terminal = _setup(orchestrator, monkeypatch)
    monkeypatch.setattr(orchestrator.time, "monotonic", lambda: 0.0)

    assert _fail_once(orchestrator, queue, task) == QueueStatus.QUEUED
    queue.cancel_download(task.task_id)  # bypasses the orchestrator's bookkeeping

    orchestrator._requeue_due_auto_retries(now=10_000.0)
    assert queue.get_task_status(task.task_id) == QueueStatus.CANCELLED
    assert queue.get_next() is None
    assert orchestrator._auto_retry_counts == {}


def test_manual_retry_resets_the_automatic_budget(orchestrator, monkeypatch):
    task, queue, _handler, _terminal = _setup(orchestrator, monkeypatch)
    orchestrator._auto_retry_counts[task.task_id] = 2
    queue.update_status(task.task_id, QueueStatus.ERROR)

    ok, _error = orchestrator.retry_download(task.task_id)

    assert ok is True
    assert orchestrator._auto_retry_counts == {}


def test_a_cancel_landing_before_parking_wins(orchestrator, monkeypatch):
    task, queue, handler, terminal = _setup(orchestrator, monkeypatch)

    def _download(_task, _cancel_flag, _progress_callback, _status_callback):
        # The user cancels after the attempt failed but before the flag is checked again.
        queue.cancel_download(task.task_id)
        return None

    handler.download.side_effect = _download
    cancel_flag = Event()  # the worker's own flag was not the one set
    orchestrator._download_task(task.task_id, cancel_flag)
    orchestrator._cleanup_progress_tracking(task.task_id)

    assert orchestrator._schedule_auto_retry(task.task_id) is True
    assert queue.get_task_status(task.task_id) == QueueStatus.CANCELLED
    assert terminal == [QueueStatus.CANCELLED]
    assert orchestrator._auto_retry_due == {}


def test_a_cancel_landing_before_requeue_wins(orchestrator, monkeypatch):
    task, queue, _handler, _terminal = _setup(orchestrator, monkeypatch)
    monkeypatch.setattr(orchestrator.time, "monotonic", lambda: 0.0)
    assert _fail_once(orchestrator, queue, task) == QueueStatus.QUEUED

    real_enqueue = queue.enqueue_existing

    def _cancel_first(task_id, **kwargs):
        queue.cancel_download(task_id)
        return real_enqueue(task_id, **kwargs)

    monkeypatch.setattr(queue, "enqueue_existing", _cancel_first)
    orchestrator._requeue_due_auto_retries(now=10_000.0)

    assert queue.get_task_status(task.task_id) == QueueStatus.CANCELLED
    assert queue.get_next() is None


def test_a_parked_task_is_not_reported_as_active(orchestrator, monkeypatch):
    task, queue, _handler, _terminal = _setup(orchestrator, monkeypatch)
    assert queue.get_active_downloads() == [task.task_id]

    assert _fail_once(orchestrator, queue, task) == QueueStatus.QUEUED
    assert queue.get_active_downloads() == []
    assert orchestrator._task_to_dict(task)["auto_retry_pending"] is True


def test_a_parked_task_keeps_a_new_priority_for_its_requeue(orchestrator, monkeypatch):
    task, queue, _handler, _terminal = _setup(orchestrator, monkeypatch)
    monkeypatch.setattr(orchestrator.time, "monotonic", lambda: 0.0)
    assert _fail_once(orchestrator, queue, task) == QueueStatus.QUEUED

    assert queue.set_priority(task.task_id, -5) is True
    orchestrator._requeue_due_auto_retries(now=10_000.0)

    assert queue.get_queue_order()[0]["priority"] == -5
