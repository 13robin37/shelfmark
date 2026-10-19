"""Wanted list API routes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from flask import Flask, Response, jsonify, request, session

from shelfmark.core.logger import setup_logger
from shelfmark.core.request_helpers import (
    normalize_optional_text,
    normalize_positive_int,
    now_utc_iso,
)
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
    from shelfmark.download.wanted_scheduler import WantedScheduler

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
        "release_date": book.get("release_date"),
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


def serialize_watch(
    watch: dict[str, Any], *, viewer_id: int | None, is_admin: bool
) -> dict[str, Any]:
    """A watched series, author or list as the frontend sees it."""
    data: dict[str, Any] = {
        "id": watch["id"],
        "own": watch.get("user_id") == viewer_id,
        "label": watch["label"],
        "provider": watch["provider"],
        "fields": watch["fields"],
        "last_synced_at": watch.get("last_synced_at"),
        "last_result": watch.get("last_result"),
        "created_at": watch.get("created_at"),
    }
    if is_admin:
        data["user_id"] = watch.get("user_id")
        data["username"] = watch.get("username")
    return data


def register_wanted_routes(
    app: Flask,
    user_db: UserDB | None,
    wanted_service: WantedService,
    *,
    resolve_auth_mode: Callable[[], str],
    on_change: Callable[[int | None], None] | None = None,
    scheduler: WantedScheduler | None = None,
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

    def _subscribed(wanted_id: int, viewer_id: int | None) -> bool:
        return any(
            sub.get("user_id") == viewer_id for sub in wanted_service.list_subscribers(wanted_id)
        )

    @app.route("/api/wanted/<int:wanted_id>/evaluate", methods=["POST"])
    def api_wanted_evaluate(wanted_id: int) -> ResponseReturnValue:
        from shelfmark.download.wanted_scheduler import release_from_dict

        viewer_id, is_admin, gate = _viewer()
        if gate is not None:
            return gate
        if scheduler is None or _item_for_viewer(wanted_id, viewer_id, is_admin) is None:
            return _error("Wanted item not found", 404)
        payload = request.get_json(silent=True)
        raw = payload.get("releases") if isinstance(payload, dict) else None
        if not isinstance(raw, list):
            return _error("Expected a JSON object with releases", 400)
        releases = [release for entry in raw[:2000] if (release := release_from_dict(entry))]
        return jsonify({"reasons": scheduler.evaluate(wanted_id, releases)})

    @app.route("/api/wanted/<int:wanted_id>/grab", methods=["POST"])
    def api_wanted_grab(wanted_id: int) -> ResponseReturnValue:
        from shelfmark.download.wanted_scheduler import release_from_dict

        viewer_id, is_admin, gate = _viewer()
        if gate is not None:
            return gate
        if not wanted_enabled():
            return _error("The wanted list is disabled", 403, code="wanted_disabled")
        if scheduler is None or not _subscribed(wanted_id, viewer_id):
            return _error("You have not bookmarked this book", 404)
        payload = request.get_json(silent=True)
        release = release_from_dict(payload.get("release") if isinstance(payload, dict) else None)
        if release is None:
            return _error("Expected a JSON object with a release", 400)
        auth_mode = resolve_auth_mode()
        if not user_may_download(
            user_db, viewer_id, release.source, auth_mode=auth_mode, is_admin=is_admin
        ):
            return _error("You may not download from this source", 403, code="wanted_forbidden")
        user = user_db.get_user(user_id=viewer_id) if user_db and viewer_id else None
        owner = {"user_id": viewer_id, "username": user.get("username") if user else None}
        queued, error = scheduler.grab(wanted_id, release, owner)
        if not queued:
            return _error(error or "Could not queue the release", 409)
        _changed(wanted_id)
        return jsonify({"status": "queued", "task_id": release.source_id})

    def _watch_target(payload: dict[str, Any]) -> tuple[str, dict[str, str], str]:
        from shelfmark.core.wanted_watches import normalize_watch

        provider, fields = normalize_watch(payload.get("provider"), payload.get("fields"))
        label = normalize_optional_text(payload.get("label")) or next(iter(fields.values()))
        return provider, fields, label

    @app.route("/api/wanted/watches", methods=["GET"])
    def api_wanted_watches() -> ResponseReturnValue:
        viewer_id, is_admin, gate = _viewer()
        if gate is not None:
            return gate
        watches = wanted_service.list_watches(user_id=viewer_id, include_all=is_admin)
        return jsonify(
            {
                "watches": [
                    serialize_watch(w, viewer_id=viewer_id, is_admin=is_admin) for w in watches
                ]
            }
        )

    @app.route("/api/wanted/watches", methods=["POST"])
    def api_wanted_watch() -> ResponseReturnValue:
        from shelfmark.core.wanted_watches import WatchError, book_key, fetch_watch_books

        viewer_id, is_admin, gate = _viewer()
        if gate is not None:
            return gate
        if not wanted_enabled():
            return _error("The wanted list is disabled", 403, code="wanted_disabled")
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return _error("Request body must be a JSON object", 400)
        try:
            provider, fields, label = _watch_target(payload)
        except WatchError as exc:
            return _error(str(exc), 400)
        owner_id = viewer_id
        approved_by: int | None = None
        requested_owner = normalize_positive_int(payload.get("user_id"))
        if requested_owner is not None and requested_owner != viewer_id:
            # Admins may watch for someone else; their approval lets it download.
            if not is_admin:
                return _error("Admin access required", 403)
            if user_db is None or user_db.get_user(user_id=requested_owner) is None:
                return _error("User not found", 404)
            owner_id = requested_owner
            approved_by = viewer_id or None
        elif not _can_bookmark(viewer_id, is_admin):
            return _error(
                "You may not download from any wanted source", 403, code="wanted_forbidden"
            )
        try:
            books = fetch_watch_books(provider, fields)
        except Exception as exc:  # noqa: BLE001 - report a provider failure to the user
            logger.warning("Wanted: could not read %s: %s", label, exc)
            return _error(f"Could not read {label}", 502)
        # What is there now is what the user sees; a watch adds what comes later.
        watch, created = wanted_service.add_watch(
            user_id=owner_id,
            provider=provider,
            fields=fields,
            label=label,
            seen=sorted({book_key(book) for book in books}),
            approved_by=approved_by,
        )
        return jsonify({"watch": serialize_watch(watch, viewer_id=viewer_id, is_admin=is_admin)}), (
            201 if created else 200
        )

    @app.route("/api/wanted/watches/<int:watch_id>", methods=["DELETE"])
    def api_wanted_unwatch(watch_id: int) -> ResponseReturnValue:
        viewer_id, is_admin, gate = _viewer()
        if gate is not None:
            return gate
        watch = wanted_service.get_watch(watch_id)
        if watch is None or not (is_admin or watch.get("user_id") == viewer_id):
            return _error("Watch not found", 404)
        wanted_service.delete_watch(watch_id)
        return jsonify({"status": "removed"})

    @app.route("/api/wanted/bulk", methods=["POST"])
    def api_wanted_bulk() -> ResponseReturnValue:
        from shelfmark.core import library_index
        from shelfmark.core.wanted_watches import (
            WatchError,
            book_data_from_metadata,
            fetch_watch_books,
        )

        viewer_id, is_admin, gate = _viewer()
        if gate is not None:
            return gate
        if not wanted_enabled():
            return _error("The wanted list is disabled", 403, code="wanted_disabled")
        if not _can_bookmark(viewer_id, is_admin):
            return _error(
                "You may not download from any wanted source", 403, code="wanted_forbidden"
            )
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return _error("Request body must be a JSON object", 400)
        try:
            provider, fields, label = _watch_target(payload)
        except WatchError as exc:
            return _error(str(exc), 400)
        try:
            books = fetch_watch_books(provider, fields)
        except Exception as exc:  # noqa: BLE001 - report a provider failure to the user
            logger.warning("Wanted: could not read %s: %s", label, exc)
            return _error(f"Could not read {label}", 502)
        counts = {"bookmarked": 0, "in_library": 0, "failed": 0}
        for book in books:
            try:
                in_library = library_index.is_in_library(book, WANTED_CONTENT_TYPE)
            except OSError, RuntimeError, ValueError:
                in_library = False
            if in_library:
                counts["in_library"] += 1
                continue
            try:
                wanted_service.add(user_id=viewer_id, book_data=book_data_from_metadata(book))
                counts["bookmarked"] += 1
            except WantedServiceError, RuntimeError:
                counts["failed"] += 1
        logger.info("Wanted: bookmarked %d book(s) of %s", counts["bookmarked"], label)
        _changed(None)
        return jsonify(counts)

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
        if scheduler is not None:
            # Asked for by a person: search even before the release date.
            scheduler.search_now(wanted_id)
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
        from shelfmark.release_sources import get_source_display_name

        attempts = wanted_service.list_attempts(wanted_id)
        labels = {
            source: get_source_display_name(source) for source in {a["source"] for a in attempts}
        }
        for attempt in attempts:
            attempt["source_label"] = labels[attempt["source"]]
        if not is_admin:
            # Error text can name trackers or carry download URLs.
            hidden = {"source_id", "match_key", "indexer", "error_message"}
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
