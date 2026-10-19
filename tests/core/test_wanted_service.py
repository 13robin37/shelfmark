"""Tests for the wanted list persistence layer."""

from __future__ import annotations

import pytest

from shelfmark.core.user_db import UserDB
from shelfmark.core.wanted_service import (
    ATTEMPT_ERROR,
    STATE_DOWNLOADING,
    STATE_OBTAINED,
    STATE_SEARCHING,
    STATE_WAITING,
    WantedService,
    WantedServiceError,
)


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "users.db")
    UserDB(path).initialize()
    return path


@pytest.fixture
def service(db_path):
    return WantedService(db_path)


@pytest.fixture
def users(db_path):
    user_db = UserDB(db_path)
    return [user_db.create_user(username=f"user{index}") for index in range(3)]


def _book(**overrides):
    book = {
        "title": "Dune",
        "author": "Frank Herbert",
        "provider": "hardcover",
        "provider_id": "123",
        "isbn_13": "978-0-441-17271-9",
    }
    book.update(overrides)
    return book


def test_add_creates_a_due_item_with_its_subscriber(service, users):
    item, created = service.add(user_id=users[0]["id"], book_data=_book(), languages=["en"])

    assert created is True
    assert item["state"] == STATE_WAITING
    assert item["next_search_at"] is not None
    assert item["isbn_13"] == "9780441172719"
    subscribers = service.list_subscribers(item["id"])
    assert [(sub["user_id"], sub["languages"]) for sub in subscribers] == [(users[0]["id"], ["en"])]


def test_a_second_user_joins_the_same_item(service, users):
    first, _ = service.add(user_id=users[0]["id"], book_data=_book())
    second, created = service.add(user_id=users[1]["id"], book_data=_book())
    again, created_again = service.add(user_id=users[1]["id"], book_data=_book())

    assert created is False
    assert created_again is False
    assert first["id"] == second["id"] == again["id"]
    assert len(service.list_subscribers(first["id"])) == 2


def test_the_same_book_from_another_provider_joins_by_isbn(service, users):
    first, _ = service.add(user_id=users[0]["id"], book_data=_book())
    other, created = service.add(
        user_id=users[1]["id"],
        book_data=_book(provider="openlibrary", provider_id="OL1W", isbn_13="9780441172719"),
    )

    assert created is False
    assert other["id"] == first["id"]


def test_removing_the_last_subscriber_deletes_the_item(service, users):
    item, _ = service.add(user_id=users[0]["id"], book_data=_book())
    service.add(user_id=users[1]["id"], book_data=_book())

    assert service.remove_subscriber(item["id"], users[0]["id"]) is True
    assert service.get_item(item["id"]) is not None
    assert service.remove_subscriber(item["id"], users[1]["id"]) is True
    assert service.get_item(item["id"]) is None


def test_a_new_subscriber_reopens_an_obtained_item(service, users):
    item, _ = service.add(user_id=users[0]["id"], book_data=_book())
    service.update_item(item["id"], state=STATE_OBTAINED, next_search_at=None, backoff_level=2)

    reopened, _ = service.add(user_id=users[1]["id"], book_data=_book())

    assert reopened["state"] == STATE_WAITING
    assert reopened["backoff_level"] == 0
    assert reopened["next_search_at"] is not None


def test_no_auth_mode_subscriber_without_user_id(service):
    item, created = service.add(user_id=None, book_data=_book())
    _, created_again = service.add(user_id=None, book_data=_book())

    assert created is True
    assert created_again is False
    assert [sub["user_id"] for sub in service.list_subscribers(item["id"])] == [None]
    assert service.list_items(user_id=None, include_all=False)[0]["id"] == item["id"]


@pytest.mark.parametrize(
    "book",
    [
        "not a dict",
        {"title": "Dune", "provider": "hardcover"},
        {"provider": "hardcover", "provider_id": "1"},
        {"title": "x" * 20_000, "provider": "hardcover", "provider_id": "1"},
    ],
)
def test_invalid_books_are_rejected(service, book):
    with pytest.raises(WantedServiceError):
        service.add(user_id=None, book_data=book)


def test_list_items_shows_only_own_items_unless_admin(service, users):
    mine, _ = service.add(user_id=users[0]["id"], book_data=_book())
    service.add(user_id=users[1]["id"], book_data=_book(provider_id="456", isbn_13=None))

    own = service.list_items(user_id=users[0]["id"], include_all=False)
    everything = service.list_items(user_id=users[0]["id"], include_all=True)

    assert [item["id"] for item in own] == [mine["id"]]
    assert len(everything) == 2
    assert all("subscribers" in item for item in everything)


def test_find_for_books_reports_state_and_subscription(service, users):
    item, _ = service.add(user_id=users[0]["id"], book_data=_book())

    states = service.find_for_books(
        [("hardcover", "123"), ("hardcover", "999")], user_id=users[1]["id"]
    )

    assert states == {
        ("hardcover", "123"): {"id": item["id"], "state": STATE_WAITING, "subscribed": False}
    }


def test_due_items_and_mark_all_due(service, users):
    item, _ = service.add(user_id=users[0]["id"], book_data=_book())
    service.update_item(item["id"], next_search_at="2999-01-01T00:00:00+00:00", backoff_level=3)

    assert service.due_items("2100-01-01T00:00:00+00:00", 10) == []
    assert service.mark_all_due() == 1
    due = service.due_items("2999-12-31T00:00:00+00:00", 10)
    assert [entry["id"] for entry in due] == [item["id"]]
    assert due[0]["backoff_level"] == 0


def test_update_item_respects_expected_state(service, users):
    item, _ = service.add(user_id=users[0]["id"], book_data=_book())

    assert (
        service.update_item(item["id"], expected_state=STATE_SEARCHING, state=STATE_WAITING) is None
    )
    updated = service.update_item(item["id"], expected_state=STATE_WAITING, state=STATE_SEARCHING)
    assert updated is not None
    assert updated["state"] == STATE_SEARCHING
    with pytest.raises(ValueError, match="Unknown"):
        service.update_item(item["id"], title="nope")


def _attempt(service, wanted_id, cycle, source_id="md5a", match_key="m"):
    service.record_attempt(
        wanted_id,
        cycle=cycle,
        source="direct_download",
        source_id=source_id,
        match_key=match_key,
        release_title="Dune",
        file_format="epub",
        size="1 MB",
        indexer=None,
    )


def test_a_release_is_blocked_after_failing_in_two_cycles(service, users):
    item, _ = service.add(user_id=users[0]["id"], book_data=_book())
    key = "id:direct_download:md5a"

    _attempt(service, item["id"], cycle=1)
    service.finish_attempt(item["id"], "md5a", ATTEMPT_ERROR, "boom")
    # Skipped for the rest of cycle 1, tried again in cycle 2.
    assert key in service.excluded_release_keys(item["id"], 1)
    assert key not in service.excluded_release_keys(item["id"], 2)

    _attempt(service, item["id"], cycle=2)
    service.finish_attempt(item["id"], "md5a", ATTEMPT_ERROR, "boom again")
    assert key in service.excluded_release_keys(item["id"], 3)
    assert "match:m" in service.excluded_release_keys(item["id"], 3)

    assert service.clear_blocklist(item["id"]) == 2
    assert key not in service.excluded_release_keys(item["id"], 3)


def test_reset_interrupted_returns_items_to_waiting(service, users):
    item, _ = service.add(user_id=users[0]["id"], book_data=_book())
    service.update_item(item["id"], state=STATE_DOWNLOADING, current_task_id="md5a")
    _attempt(service, item["id"], cycle=1)

    assert service.reset_interrupted() == 1
    reset = service.get_item(item["id"])
    assert reset["state"] == STATE_WAITING
    assert reset["current_task_id"] is None
    assert [attempt["status"] for attempt in service.list_attempts(item["id"])] == ["cancelled"]
    # An interrupted attempt is not a strike.
    assert "id:direct_download:md5a" not in service.excluded_release_keys(item["id"], 2)


def test_mark_delivered_and_find_by_task(service, users):
    item, _ = service.add(user_id=users[0]["id"], book_data=_book())
    service.update_item(item["id"], current_task_id="md5a")

    assert service.find_by_task("md5a")["id"] == item["id"]
    service.mark_delivered(item["id"], [users[0]["id"]], "md5a")
    assert service.list_subscribers(item["id"])[0]["delivered_at"] is not None


def test_deleting_a_user_drops_their_subscription(service, users, db_path):
    item, _ = service.add(user_id=users[0]["id"], book_data=_book())
    service.add(user_id=users[1]["id"], book_data=_book())

    UserDB(db_path).delete_user(users[0]["id"])

    assert [sub["user_id"] for sub in service.list_subscribers(item["id"])] == [users[1]["id"]]
