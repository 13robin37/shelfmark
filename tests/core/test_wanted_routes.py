"""API tests for the wanted list routes."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from flask import Flask

from shelfmark.core.user_db import UserDB
from shelfmark.core.wanted_routes import register_wanted_routes
from shelfmark.core.wanted_service import STATE_PAUSED, STATE_WAITING, WantedService

BOOK = {"title": "Dune", "author": "Frank Herbert", "provider": "hardcover", "provider_id": "1"}
SOURCES = [
    {"name": "direct_download", "enabled": True, "supported_content_types": ["ebook"]},
    {"name": "audiobookbay", "enabled": True, "supported_content_types": ["audiobook"]},
]


@pytest.fixture
def env(tmp_path):
    db_path = str(tmp_path / "users.db")
    user_db = UserDB(db_path)
    user_db.initialize()
    service = WantedService(db_path)
    app = Flask(__name__)
    app.secret_key = "test"
    auth = {"mode": "builtin"}
    changed: list[int | None] = []
    register_wanted_routes(
        app,
        user_db,
        service,
        resolve_auth_mode=lambda: auth["mode"],
        on_change=changed.append,
    )
    patches = [
        patch("shelfmark.core.wanted_routes.wanted_enabled", return_value=True),
        patch("shelfmark.core.wanted_routes.wanted_sources", return_value=["direct_download"]),
        patch("shelfmark.release_sources.list_available_sources", return_value=SOURCES),
        patch(
            "shelfmark.core.wanted_access.load_users_request_policy_settings",
            return_value={"REQUESTS_ENABLED": False},
        ),
    ]
    for active in patches:
        active.start()
    yield {
        "app": app,
        "client": app.test_client(),
        "user_db": user_db,
        "service": service,
        "auth": auth,
        "changed": changed,
        "alice": user_db.create_user(username="alice"),
        "bob": user_db.create_user(username="bob"),
        "admin": user_db.create_user(username="root", role="admin"),
    }
    for active in patches:
        active.stop()


def _login(env, user, *, is_admin=False):
    with env["client"].session_transaction() as sess:
        sess["user_id"] = user["username"]
        sess["db_user_id"] = user["id"]
        sess["is_admin"] = is_admin


def test_requires_login(env):
    assert env["client"].get("/api/wanted").status_code == 401


def test_bookmark_list_and_join(env):
    client = env["client"]
    _login(env, env["alice"])
    response = client.post("/api/wanted", json={"book": BOOK})
    assert response.status_code == 201
    item = response.get_json()["item"]
    assert item["subscribed"] is True
    assert "subscribers" not in item
    assert env["changed"] == [item["id"]]

    _login(env, env["bob"])
    joined = client.post("/api/wanted", json={"book": BOOK})
    assert joined.status_code == 200
    assert joined.get_json()["created"] is False

    listed = client.get("/api/wanted").get_json()["items"]
    assert [(entry["id"], entry["subscriber_count"]) for entry in listed] == [(item["id"], 2)]

    assert "current_task_id" not in listed[0]

    _login(env, env["admin"], is_admin=True)
    admin_items = client.get("/api/wanted").get_json()["items"]
    assert [sub["username"] for sub in admin_items[0]["subscribers"]] == ["alice", "bob"]
    assert "current_task_id" in admin_items[0]


def test_attempts_hide_release_identities_from_non_admins(env):
    client = env["client"]
    service = env["service"]
    _login(env, env["alice"])
    item_id = client.post("/api/wanted", json={"book": BOOK}).get_json()["item"]["id"]
    service.record_attempt(
        item_id,
        cycle=1,
        source="prowlarr",
        source_id="12:https://tracker/torrent/1",
        match_key="m",
        release_title="Dune",
        file_format="epub",
        size="1 MB",
        indexer="tracker",
    )
    service.finish_attempt(item_id, "12:https://tracker/torrent/1", "error", "tracker said no")

    attempt = client.get(f"/api/wanted/{item_id}/attempts").get_json()["attempts"][0]
    assert {"source_id", "indexer", "match_key", "error_message"}.isdisjoint(attempt)
    assert attempt["source_label"]
    _login(env, env["admin"], is_admin=True)
    admin_attempt = client.get(f"/api/wanted/{item_id}/attempts").get_json()["attempts"][0]
    assert admin_attempt["source_id"] == "12:https://tracker/torrent/1"


def test_a_session_admin_may_bookmark_regardless_of_role(env):
    _login(env, env["alice"], is_admin=True)
    with patch(
        "shelfmark.core.wanted_access.load_users_request_policy_settings",
        return_value={"REQUESTS_ENABLED": True, "REQUEST_POLICY_DEFAULT_EBOOK": "request_book"},
    ):
        assert env["client"].get("/api/wanted/status").get_json()["can_bookmark"] is True


def test_status_reports_whether_the_viewer_may_bookmark(env):
    _login(env, env["alice"])
    assert env["client"].get("/api/wanted/status").get_json() == {
        "enabled": True,
        "can_bookmark": True,
    }
    with patch("shelfmark.core.wanted_routes.wanted_sources", return_value=["audiobookbay"]):
        assert env["client"].get("/api/wanted/status").get_json()["can_bookmark"] is False


def test_bookmark_rejected_when_disabled_or_not_allowed(env):
    client = env["client"]
    _login(env, env["alice"])
    with patch("shelfmark.core.wanted_routes.wanted_enabled", return_value=False):
        assert client.post("/api/wanted", json={"book": BOOK}).status_code == 403

    with patch(
        "shelfmark.core.wanted_access.load_users_request_policy_settings",
        return_value={"REQUESTS_ENABLED": True, "REQUEST_POLICY_DEFAULT_EBOOK": "request_book"},
    ):
        response = client.post("/api/wanted", json={"book": BOOK})
        assert response.status_code == 403
        assert response.get_json()["code"] == "wanted_forbidden"

    audiobook = client.post("/api/wanted", json={"book": {**BOOK, "content_type": "audiobook"}})
    assert audiobook.status_code == 400
    assert client.post("/api/wanted", json={"book": {"title": "x"}}).status_code == 400


def test_other_users_cannot_see_or_touch_an_item(env):
    client = env["client"]
    _login(env, env["alice"])
    item_id = client.post("/api/wanted", json={"book": BOOK}).get_json()["item"]["id"]

    _login(env, env["bob"])
    assert client.get("/api/wanted").get_json()["items"] == []
    assert client.delete(f"/api/wanted/{item_id}").status_code == 404
    assert client.post(f"/api/wanted/{item_id}/search").status_code == 404
    assert client.get(f"/api/wanted/{item_id}/attempts").status_code == 404


def test_removing_own_bookmark_and_admin_delete(env):
    client = env["client"]
    service = env["service"]
    _login(env, env["alice"])
    item_id = client.post("/api/wanted", json={"book": BOOK}).get_json()["item"]["id"]
    _login(env, env["bob"])
    client.post("/api/wanted", json={"book": BOOK})

    assert client.delete(f"/api/wanted/{item_id}").status_code == 200
    assert len(service.list_subscribers(item_id)) == 1

    _login(env, env["admin"], is_admin=True)
    assert client.delete(f"/api/wanted/{item_id}?scope=all").status_code == 200
    assert service.get_item(item_id) is None


def test_admin_pause_resume_and_blocklist(env):
    client = env["client"]
    service = env["service"]
    _login(env, env["alice"])
    item_id = client.post("/api/wanted", json={"book": BOOK}).get_json()["item"]["id"]
    assert client.post(f"/api/wanted/{item_id}/pause").status_code == 403
    assert client.delete(f"/api/wanted/{item_id}/blocklist").status_code == 403

    _login(env, env["admin"], is_admin=True)
    assert client.post(f"/api/wanted/{item_id}/pause").get_json()["status"] == STATE_PAUSED
    assert service.get_item(item_id)["state"] == STATE_PAUSED
    assert client.post(f"/api/wanted/{item_id}/search").status_code == 409
    assert client.post(f"/api/wanted/{item_id}/resume").get_json()["status"] == STATE_WAITING
    assert service.get_item(item_id)["state"] == STATE_WAITING
    assert client.delete(f"/api/wanted/{item_id}/blocklist").get_json()["cleared"] == 0


def test_search_now_makes_the_item_due(env):
    client = env["client"]
    service = env["service"]
    _login(env, env["alice"])
    item_id = client.post("/api/wanted", json={"book": BOOK}).get_json()["item"]["id"]
    service.update_item(item_id, next_search_at="2999-01-01T00:00:00+00:00")

    assert client.post(f"/api/wanted/{item_id}/search").status_code == 200
    assert service.get_item(item_id)["next_search_at"] < "2999"


def test_no_auth_mode_works_without_a_session(env):
    env["auth"]["mode"] = "none"
    client = env["client"]

    response = client.post("/api/wanted", json={"book": BOOK})
    assert response.status_code == 201
    items = client.get("/api/wanted").get_json()["items"]
    assert items[0]["subscribed"] is True
    assert client.delete(f"/api/wanted/{items[0]['id']}").status_code == 200


def test_languages_can_be_changed_by_a_subscriber(env):
    client = env["client"]
    _login(env, env["alice"])
    item_id = client.post("/api/wanted", json={"book": BOOK}).get_json()["item"]["id"]

    assert client.patch(f"/api/wanted/{item_id}", json={"languages": ["de"]}).status_code == 200
    assert client.get("/api/wanted").get_json()["items"][0]["languages"] == ["de"]
    assert client.patch(f"/api/wanted/{item_id}", json={"languages": "de"}).status_code == 400

    _login(env, env["bob"])
    assert client.patch(f"/api/wanted/{item_id}", json={"languages": ["en"]}).status_code == 404


def test_obtained_books_are_cleared_per_viewer(env):
    client = env["client"]
    _login(env, env["alice"])
    item_id = client.post("/api/wanted", json={"book": BOOK}).get_json()["item"]["id"]
    env["service"].update_item(item_id, state="obtained")

    assert client.delete("/api/wanted/obtained").status_code == 200
    assert env["service"].get_item(item_id) is None


def test_only_admins_allow_a_release_again(env):
    client = env["client"]
    _login(env, env["alice"])
    item_id = client.post("/api/wanted", json={"book": BOOK}).get_json()["item"]["id"]
    service = env["service"]
    service.record_attempt(
        item_id,
        cycle=1,
        source="direct_download",
        source_id="md5a",
        match_key=None,
        release_title="Dune",
        file_format="epub",
        size=None,
        indexer=None,
    )
    service.finish_attempt(item_id, "md5a", "rejected", "Cancelled")
    attempt_id = service.list_attempts(item_id)[0]["id"]
    url = f"/api/wanted/{item_id}/attempts/{attempt_id}/block"

    assert client.delete(url).status_code == 403
    _login(env, env["admin"], is_admin=True)
    assert client.delete(url).get_json()["cleared"] == 1
    assert service.list_attempts(item_id) == []


def test_grab_and_evaluate_need_a_bookmark(env):
    client = env["client"]
    _login(env, env["alice"])
    item_id = client.post("/api/wanted", json={"book": BOOK}).get_json()["item"]["id"]
    release = {"source": "direct_download", "source_id": "md5", "title": "Dune"}

    # Without a scheduler the routes answer as if the item were unknown.
    assert client.post(f"/api/wanted/{item_id}/grab", json={"release": release}).status_code == 404
    assert (
        client.post(f"/api/wanted/{item_id}/evaluate", json={"releases": [release]}).status_code
        == 404
    )


class _FakeScheduler:
    def __init__(self):
        self.grabbed = []

    def evaluate(self, wanted_id, releases):
        return {f"{r.source}:{r.source_id}": None for r in releases}

    def grab(self, wanted_id, release, owner):
        self.grabbed.append((wanted_id, release.source_id, owner))
        return True, None


def test_a_subscriber_may_pick_a_release_for_the_wanted_list(env):
    scheduler = _FakeScheduler()
    app = Flask("picked")
    app.secret_key = "test"
    register_wanted_routes(
        app,
        env["user_db"],
        env["service"],
        resolve_auth_mode=lambda: "builtin",
        scheduler=scheduler,
    )
    client = app.test_client()
    alice, bob = env["alice"], env["bob"]
    item, _ = env["service"].add(user_id=alice["id"], book_data=BOOK)
    release = {"source": "direct_download", "source_id": "md5", "title": "Dune"}

    def login(user):
        with client.session_transaction() as sess:
            sess["user_id"] = user["username"]
            sess["db_user_id"] = user["id"]
            sess["is_admin"] = False

    login(bob)
    assert (
        client.post(f"/api/wanted/{item['id']}/grab", json={"release": release}).status_code == 404
    )

    login(alice)
    response = client.post(f"/api/wanted/{item['id']}/evaluate", json={"releases": [release, 7]})
    assert response.get_json() == {"reasons": {"direct_download:md5": None}}
    assert client.post(f"/api/wanted/{item['id']}/grab", json={"release": {}}).status_code == 400
    assert (
        client.post(f"/api/wanted/{item['id']}/grab", json={"release": release}).status_code == 200
    )
    assert scheduler.grabbed == [(item["id"], "md5", {"user_id": alice["id"], "username": "alice"})]


def test_watching_and_bookmarking_a_whole_series(env):
    from shelfmark.metadata_providers import BookMetadata

    client = env["client"]
    _login(env, env["alice"])
    books = [
        BookMetadata(provider="hardcover", provider_id="1", title="Dune"),
        BookMetadata(provider="hardcover", provider_id="2", title="Dune Messiah"),
    ]
    target = {"provider": "hardcover", "fields": {"series": "id:5"}, "label": "Dune"}
    with (
        patch("shelfmark.core.wanted_watches.fetch_watch_books", return_value=(books, 2)),
        patch(
            "shelfmark.core.library_index.is_in_library",
            side_effect=lambda book, _ct: book.provider_id == "1",
        ),
    ):
        assert (
            client.post(
                "/api/wanted/watches", json={**target, "fields": {"series": "Dune"}}
            ).status_code
            == 400
        )
        created = client.post("/api/wanted/watches", json=target)
        bulk = client.post("/api/wanted/bulk", json=target)

    assert created.status_code == 201
    watch = created.get_json()["watch"]
    assert watch["own"] is True
    assert env["service"].get_watch(watch["id"])["seen"] == ["hardcover:1", "hardcover:2"]
    assert bulk.get_json() == {"bookmarked": 1, "already": 0, "in_library": 1, "failed": 0}
    assert [item["provider_id"] for item in client.get("/api/wanted").get_json()["items"]] == ["2"]

    _login(env, env["bob"])
    assert client.delete(f"/api/wanted/watches/{watch['id']}").status_code == 404
    _login(env, env["alice"])
    assert client.delete(f"/api/wanted/watches/{watch['id']}").status_code == 200


def test_only_admins_watch_for_someone_else(env):
    client = env["client"]
    target = {
        "provider": "hardcover",
        "fields": {"hardcover_list": "status:1"},
        "label": "Want to Read",
        "user_id": env["bob"]["id"],
    }
    with patch("shelfmark.core.wanted_watches.fetch_watch_books", return_value=([], 0)):
        _login(env, env["alice"])
        assert client.post("/api/wanted/watches", json=target).status_code == 403
        _login(env, env["admin"], is_admin=True)
        response = client.post("/api/wanted/watches", json=target)

    assert response.status_code == 201
    stored = env["service"].get_watch(response.get_json()["watch"]["id"])
    assert stored["user_id"] == env["bob"]["id"]
    assert stored["approved_by"] == env["admin"]["id"]


def test_a_failed_read_creates_no_watch(env):
    from shelfmark.core.wanted_watches import WatchFetchError

    client = env["client"]
    _login(env, env["alice"])
    target = {"provider": "hardcover", "fields": {"series": "id:5"}, "label": "Dune"}
    with patch(
        "shelfmark.core.wanted_watches.fetch_watch_books",
        side_effect=WatchFetchError("Hardcover returned 40 of 80 books"),
    ):
        assert client.post("/api/wanted/watches", json=target).status_code == 502
        assert client.post("/api/wanted/bulk", json=target).status_code == 502
    assert env["service"].list_watches(user_id=None, include_all=True) == []


def test_admins_bookmark_all_for_someone_else(env):
    from shelfmark.metadata_providers import BookMetadata

    client = env["client"]
    _login(env, env["admin"], is_admin=True)
    target = {
        "provider": "hardcover",
        "fields": {"hardcover_list": "status:1"},
        "label": "Want to Read",
        "user_id": env["bob"]["id"],
    }
    books = [BookMetadata(provider="hardcover", provider_id="9", title="Dune")]
    with (
        patch("shelfmark.core.wanted_watches.fetch_watch_books", return_value=(books, 1)),
        patch("shelfmark.core.library_index.is_in_library", return_value=False),
    ):
        assert client.post("/api/wanted/bulk", json=target).get_json()["bookmarked"] == 1

    item = env["service"].list_items(user_id=env["bob"]["id"], include_all=False)[0]
    assert item["subscribers"][0]["approved_by"] == env["admin"]["id"]
