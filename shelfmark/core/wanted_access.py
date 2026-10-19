"""Who may bookmark books, and to whom a wanted download may go."""

from __future__ import annotations

from typing import TYPE_CHECKING

from shelfmark.core.request_helpers import coerce_bool, load_users_request_policy_settings
from shelfmark.core.request_policy import (
    PolicyMode,
    merge_request_policy_settings,
    resolve_policy_mode,
)

if TYPE_CHECKING:
    from shelfmark.core.user_db import UserDB

WANTED_CONTENT_TYPE = "ebook"


def user_may_download(
    user_db: UserDB | None, user_id: int | None, source: str, *, auth_mode: str
) -> bool:
    """Whether ``user_id`` may download ebooks from ``source`` without a request.

    The session-free counterpart of the guard on /api/releases/download, so the
    background job can apply it to every subscriber.
    """
    if auth_mode == "none" or user_db is None or user_id is None:
        return True
    user = user_db.get_user(user_id=user_id)
    if user is None:
        return False
    if user.get("role") == "admin":
        return True
    global_settings = load_users_request_policy_settings()
    user_settings = user_db.get_user_settings(user_id)
    effective = merge_request_policy_settings(global_settings, user_settings)
    if not coerce_bool(effective.get("REQUESTS_ENABLED"), default=False):
        return True
    mode = resolve_policy_mode(
        source=source,
        content_type=WANTED_CONTENT_TYPE,
        global_settings=global_settings,
        user_settings=user_settings,
    )
    return mode == PolicyMode.DOWNLOAD
