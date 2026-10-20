"""Orchestrator hooks used by the wanted list."""

from __future__ import annotations

from pathlib import Path
from threading import Event
from unittest.mock import MagicMock

import pytest

import shelfmark.download.orchestrator as orchestrator
from shelfmark.core.models import DownloadTask
from shelfmark.core.queue import BookQueue


class _AvailableSource:
    display_name = "Test Source"

    def is_available(self):
        return True


@pytest.fixture
def queue(monkeypatch):
    queue = BookQueue()
    monkeypatch.setattr(orchestrator, "book_queue", queue)
    monkeypatch.setattr(orchestrator, "ws_manager", None)
    monkeypatch.setattr(orchestrator, "get_source", lambda _source: _AvailableSource())
    return queue


def test_downloaded_file_listener_sees_the_file_before_post_processing(monkeypatch, tmp_path):
    downloaded = tmp_path / "Dune.epub"
    downloaded.write_text("book")
    task = DownloadTask(task_id="md5", source="direct_download", title="Dune")
    queue = MagicMock()
    queue.get_task.return_value = task
    handler = MagicMock()
    handler.download.return_value = str(downloaded)
    order: list[str] = []
    monkeypatch.setattr(orchestrator, "book_queue", queue)
    monkeypatch.setattr(orchestrator, "get_handler", lambda _source: handler)
    monkeypatch.setattr(orchestrator, "get_source", lambda _source: _AvailableSource())
    monkeypatch.setattr(
        orchestrator,
        "post_process_download",
        lambda *_args, **_kwargs: order.append("post") or "folder://done",
    )
    orchestrator.set_downloaded_file_listener(lambda _task, path: order.append(f"seen:{path.name}"))
    try:
        assert orchestrator._download_task("md5", Event()) == "folder://done"
    finally:
        orchestrator.set_downloaded_file_listener(None)

    assert order == ["seen:Dune.epub", "post"]


def test_release_data_cannot_smuggle_a_staged_path_or_wanted_id(queue):
    orchestrator.queue_release(
        {
            "source": "direct_download",
            "source_id": "md5",
            "title": "Dune",
            "_wanted_id": 3,
            "_staged_path": "/etc/passwd",
            "staged_path": "/etc/passwd",
        }
    )

    task = queue.get_task("md5")
    assert task.staged_path is None
    assert task.wanted_id is None


def test_re_adding_a_task_cancelled_while_queued_dispatches_it_once():
    queue = BookQueue()
    task = DownloadTask(task_id="md5", source="direct_download", title="Dune")
    queue.add(task)
    queue.cancel_download("md5")

    assert queue.add(task) is True
    first = queue.get_next()
    assert first is not None and first[0] == "md5"
    assert queue.get_next() is None


def test_a_staged_delivery_task_only_post_processes_its_copy(monkeypatch, tmp_path, queue):
    copy = tmp_path / "held" / "Dune.epub"
    copy.parent.mkdir()
    copy.write_text("book")
    handler = MagicMock()
    seen: list[Path] = []
    monkeypatch.setattr(orchestrator, "get_handler", lambda _source: handler)
    monkeypatch.setattr(
        orchestrator,
        "post_process_download",
        lambda temp_file, *_args, **_kwargs: seen.append(temp_file) or "folder://done",
    )

    ok, error = orchestrator.queue_release(
        {"source": "direct_download", "source_id": "md5~wanted-3-1", "title": "Dune"},
        user_id=2,
        username="bob",
        wanted_id=3,
        staged_path=str(copy),
    )
    task = queue.get_task("md5~wanted-3-1")

    assert (ok, error) == (True, None)
    assert task.wanted_id == 3
    assert task.staged_path == str(copy)
    assert orchestrator._task_to_dict(task)["wanted_id"] == 3
    assert orchestrator._download_task("md5~wanted-3-1", Event()) == "folder://done"
    handler.download.assert_not_called()
    assert seen == [copy]
    assert task.can_retry_without_staged_source is False
    assert orchestrator.serialize_task_for_retry(task)["can_retry_without_staged_source"] is False
