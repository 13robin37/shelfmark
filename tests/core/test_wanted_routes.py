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

    _login(env, env["admin"], is_admin=True)
    admin_items = client.get("/api/wanted").get_json()["items"]
    assert [sub["username"] for sub in admin_items[0]["subscribers"]] == ["alice", "bob"]


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
