"""Wanted list API routes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from flask import Flask, Response, jsonify, request, session

from shelfmark.core.logger import setup_logger
from shelfmark.core.request_helpers import normalize_positive_int, now_utc_iso
from shelfmark.core.wanted_access import WANTED_CONTENT_TYPE, user_may_download
from shelfmark.core.wanted_config import wanted_enabled, wanted_sources
from shelfmark.core.wanted_service import (
    STATE_OBTAINED,
    STATE_PAUSED,
    STATE_WAITING,
    WantedService,
    WantedServiceError,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from flask.typing import ResponseReturnValue

    from shelfmark.core.user_db import UserDB

logger = setup_logger(__name__)


def _error(message: str, status_code: int, code: str | None = None) -> tuple[Response, int]:
    payload: dict[str, Any] = {"error": message}
    if code is not None:
        payload["code"] = code
    return jsonify(payload), status_code


def serialize_item(
    item: dict[str, Any], *, viewer_id: int | None, is_admin: bool
) -> dict[str, Any]:
    """Item as the frontend sees it; subscriber details only for admins."""
    book = item.get("book_data") or {}
    subscribers = item.get("subscribers") or []
    own = next((sub for sub in subscribers if sub.get("user_id") == viewer_id), None)
    data: dict[str, Any] = {
        "id": item["id"],
        "title": item["title"],
        "author": item.get("author"),
        "preview": book.get("preview"),
        "year": book.get("year"),
        "provider": item["provider"],
        "provider_id": item["provider_id"],
        "state": item["state"],
        "search_count": item.get("search_count", 0),
        "last_search_at": item.get("last_search_at"),
        "next_search_at": item.get("next_search_at"),
        "last_result": item.get("last_result"),
        "obtained_at": item.get("obtained_at"),
        "obtained_reason": item.get("obtained_reason"),
        "attempt_count": item.get("attempt_count", 0),
        "created_at": item.get("created_at"),
        "subscriber_count": len(subscribers),
        "subscribed": own is not None,
        "languages": own.get("languages") if own else None,
    }
    if is_admin:
        data["current_task_id"] = item.get("current_task_id")
        data["subscribers"] = [
            {
                "user_id": sub.get("user_id"),
                "username": sub.get("username"),
                "languages": sub.get("languages"),
                "delivered_at": sub.get("delivered_at"),
            }
            for sub in subscribers
        ]
    return data


def register_wanted_routes(
    app: Flask,
    user_db: UserDB | None,
    wanted_service: WantedService,
    *,
    resolve_auth_mode: Callable[[], str],
    on_change: Callable[[int | None], None] | None = None,
) -> None:
    """Register /api/wanted routes. ``on_change`` is told which item changed."""

    def _changed(wanted_id: int | None) -> None:
        if on_change is not None:
            on_change(wanted_id)

    def _viewer() -> tuple[int | None, bool, ResponseReturnValue | None]:
        auth_mode = resolve_auth_mode()
        if auth_mode == "none":
            return None, True, None
        if "user_id" not in session:
            return None, False, _error("Unauthorized", 401)
        is_admin = bool(session.get("is_admin", False))
        db_user_id = normalize_positive_int(session.get("db_user_id"))
        if db_user_id is None and not is_admin:
            return (
                None,
                False,
                _error("User identity is unavailable", 403, code="user_identity_unavailable"),
            )
        return db_user_id, is_admin, None

    def _can_bookmark(viewer_id: int | None, is_admin: bool) -> bool:
        if not wanted_enabled():
            return False
        from shelfmark.release_sources import list_available_sources

        available = {
            source["name"]
            for source in list_available_sources()
            if source["enabled"] and WANTED_CONTENT_TYPE in source["supported_content_types"]
        }
        auth_mode = resolve_auth_mode()
        return any(
            user_may_download(user_db, viewer_id, source, auth_mode=auth_mode, is_admin=is_admin)
            for source in wanted_sources()
            if source in available
        )

    def _item_for_viewer(
        wanted_id: int, viewer_id: int | None, is_admin: bool
    ) -> dict[str, Any] | None:
        item = wanted_service.get_item(wanted_id)
        if item is None:
            return None
        if is_admin or any(
            sub.get("user_id") == viewer_id for sub in wanted_service.list_subscribers(wanted_id)
        ):
            return item
        return None

    @app.route("/api/wanted/status", methods=["GET"])
    def api_wanted_status() -> ResponseReturnValue:
        viewer_id, is_admin, gate = _viewer()
        if gate is not None:
            return gate
        return jsonify(
            {"enabled": wanted_enabled(), "can_bookmark": _can_bookmark(viewer_id, is_admin)}
        )

    @app.route("/api/wanted", methods=["GET"])
    def api_wanted_list() -> ResponseReturnValue:
        viewer_id, is_admin, gate = _viewer()
        if gate is not None:
            return gate
        items = wanted_service.list_items(user_id=viewer_id, include_all=is_admin)
        return jsonify(
            {
                "items": [
                    serialize_item(item, viewer_id=viewer_id, is_admin=is_admin) for item in items
                ]
            }
        )

    @app.route("/api/wanted", methods=["POST"])
    def api_wanted_add() -> ResponseReturnValue:
        viewer_id, is_admin, gate = _viewer()
        if gate is not None:
            return gate
        if not wanted_enabled():
            return _error("The wanted list is disabled", 403, code="wanted_disabled")
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return _error("Request body must be a JSON object", 400)
        book = payload.get("book")
        content_type = book.get("content_type") if isinstance(book, dict) else None
        if content_type not in (None, WANTED_CONTENT_TYPE):
            return _error("Only ebooks can be bookmarked", 400, code="wanted_ebooks_only")
        if not _can_bookmark(viewer_id, is_admin):
            return _error(
                "You may not download from any wanted source", 403, code="wanted_forbidden"
            )
        try:
            item, created = wanted_service.add(
                user_id=viewer_id, book_data=book, languages=payload.get("languages")
            )
        except WantedServiceError as exc:
            return _error(str(exc), exc.status_code)
        logger.info(
            "Wanted: %s %s (item %s)",
            "bookmarked" if created else "joined",
            item["title"],
            item["id"],
        )
        _changed(item["id"])
        item["subscribers"] = wanted_service.list_subscribers(item["id"])
        return jsonify(
            {
                "item": serialize_item(item, viewer_id=viewer_id, is_admin=is_admin),
                "created": created,
            }
        ), (201 if created else 200)

    @app.route("/api/wanted/<int:wanted_id>", methods=["DELETE"])
    def api_wanted_remove(wanted_id: int) -> ResponseReturnValue:
        viewer_id, is_admin, gate = _viewer()
        if gate is not None:
            return gate
        if _item_for_viewer(wanted_id, viewer_id, is_admin) is None:
            return _error("Wanted item not found", 404)
        if is_admin and request.args.get("scope") == "all":
            wanted_service.delete_item(wanted_id)
        elif not wanted_service.remove_subscriber(wanted_id, viewer_id):
            return _error("You have not bookmarked this book", 404)
        _changed(wanted_id)
        return jsonify({"status": "removed"})

    @app.route("/api/wanted/<int:wanted_id>", methods=["PATCH"])
    def api_wanted_update(wanted_id: int) -> ResponseReturnValue:
        viewer_id, is_admin, gate = _viewer()
        if gate is not None:
            return gate
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or "languages" not in payload:
            return _error("Expected a JSON object with languages", 400)
        if _item_for_viewer(wanted_id, viewer_id, is_admin) is None:
            return _error("Wanted item not found", 404)
        try:
            updated = wanted_service.set_languages(wanted_id, viewer_id, payload["languages"])
        except WantedServiceError as exc:
            return _error(str(exc), exc.status_code)
        if not updated:
            return _error("You have not bookmarked this book", 404)
        _changed(wanted_id)
        return jsonify({"status": "updated"})

    @app.route("/api/wanted/obtained", methods=["DELETE"])
    def api_wanted_clear_obtained() -> ResponseReturnValue:
        viewer_id, is_admin, gate = _viewer()
        if gate is not None:
            return gate
        cleared = wanted_service.clear_obtained(viewer_id, everyone=is_admin)
        _changed(None)
        return jsonify({"status": "cleared", "cleared": cleared})

    @app.route("/api/wanted/<int:wanted_id>/search", methods=["POST"])
    def api_wanted_search_now(wanted_id: int) -> ResponseReturnValue:
        viewer_id, is_admin, gate = _viewer()
        if gate is not None:
            return gate
        item = _item_for_viewer(wanted_id, viewer_id, is_admin)
        if item is None:
            return _error("Wanted item not found", 404)
        if item["state"] != STATE_WAITING:
            return _error(f"Cannot search while the item is {item['state']}", 409)
        wanted_service.update_item(
            wanted_id, expected_state=STATE_WAITING, next_search_at=now_utc_iso()
        )
        _changed(wanted_id)
        return jsonify({"status": "scheduled"})

    def _set_paused(wanted_id: int, *, paused: bool) -> ResponseReturnValue:
        viewer_id, is_admin, gate = _viewer()
        if gate is not None:
            return gate
        if not is_admin:
            return _error("Admin access required", 403)
        item = _item_for_viewer(wanted_id, viewer_id, is_admin)
        if item is None:
            return _error("Wanted item not found", 404)
        if paused:
            if item["state"] in (STATE_PAUSED, STATE_OBTAINED):
                return jsonify({"status": item["state"]})
            # A running download keeps going; pausing only stops new searches.
            if item["state"] != STATE_WAITING:
                return _error(f"Cannot pause while the item is {item['state']}", 409)
            wanted_service.update_item(wanted_id, expected_state=STATE_WAITING, state=STATE_PAUSED)
        else:
            if item["state"] != STATE_PAUSED:
                return jsonify({"status": item["state"]})
            wanted_service.update_item(
                wanted_id,
                expected_state=STATE_PAUSED,
                state=STATE_WAITING,
                backoff_level=0,
                next_search_at=now_utc_iso(),
            )
        _changed(wanted_id)
        return jsonify({"status": STATE_PAUSED if paused else STATE_WAITING})

    @app.route("/api/wanted/<int:wanted_id>/pause", methods=["POST"])
    def api_wanted_pause(wanted_id: int) -> ResponseReturnValue:
        return _set_paused(wanted_id, paused=True)

    @app.route("/api/wanted/<int:wanted_id>/resume", methods=["POST"])
    def api_wanted_resume(wanted_id: int) -> ResponseReturnValue:
        return _set_paused(wanted_id, paused=False)

    @app.route("/api/wanted/<int:wanted_id>/blocklist", methods=["DELETE"])
    def api_wanted_clear_blocklist(wanted_id: int) -> ResponseReturnValue:
        viewer_id, is_admin, gate = _viewer()
        if gate is not None:
            return gate
        if not is_admin:
            return _error("Admin access required", 403)
        if _item_for_viewer(wanted_id, viewer_id, is_admin) is None:
            return _error("Wanted item not found", 404)
        cleared = wanted_service.clear_blocklist(wanted_id)
        _changed(wanted_id)
        return jsonify({"status": "cleared", "cleared": cleared})

    @app.route("/api/wanted/<int:wanted_id>/attempts", methods=["GET"])
    def api_wanted_attempts(wanted_id: int) -> ResponseReturnValue:
        viewer_id, is_admin, gate = _viewer()
        if gate is not None:
            return gate
        if _item_for_viewer(wanted_id, viewer_id, is_admin) is None:
            return _error("Wanted item not found", 404)
        attempts = wanted_service.list_attempts(wanted_id)
        if not is_admin:
            hidden = {"source_id", "match_key", "indexer"}
            attempts = [
                {key: value for key, value in attempt.items() if key not in hidden}
                for attempt in attempts
            ]
        return jsonify({"attempts": attempts})

    @app.route("/api/wanted/<int:wanted_id>/attempts/<int:attempt_id>/block", methods=["DELETE"])
    def api_wanted_unblock_release(wanted_id: int, attempt_id: int) -> ResponseReturnValue:
        viewer_id, is_admin, gate = _viewer()
        if gate is not None:
            return gate
        if not is_admin:
            return _error("Admin access required", 403)
        if _item_for_viewer(wanted_id, viewer_id, is_admin) is None:
            return _error("Wanted item not found", 404)
        cleared = wanted_service.unblock_release(wanted_id, attempt_id)
        _changed(wanted_id)
        return jsonify({"status": "cleared", "cleared": cleared})
