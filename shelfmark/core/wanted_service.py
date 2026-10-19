"""Persistence for the wanted list: bookmarked books, their subscribers and attempts."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

from shelfmark.core.logger import setup_logger
from shelfmark.core.request_helpers import normalize_optional_text, now_utc_iso
from shelfmark.core.text_match import isbn_variants, normalize_isbn

logger = setup_logger(__name__)

STATE_WAITING = "waiting"
STATE_SEARCHING = "searching"
STATE_DOWNLOADING = "downloading"
STATE_OBTAINED = "obtained"
STATE_PAUSED = "paused"
WANTED_STATES = frozenset(
    {STATE_WAITING, STATE_SEARCHING, STATE_DOWNLOADING, STATE_OBTAINED, STATE_PAUSED}
)

ATTEMPT_QUEUED = "queued"
ATTEMPT_COMPLETE = "complete"
ATTEMPT_ERROR = "error"
ATTEMPT_CANCELLED = "cancelled"
# A user cancelled this release's download: never offer it again for this item.
ATTEMPT_REJECTED = "rejected"

# A release that failed in this many separate search cycles is not tried again - until
# its failures are this old, since an outage can look like a bad release.
BLOCK_AFTER_FAILED_CYCLES = 2
BLOCK_EXPIRY_DAYS = 30

MAX_BOOK_DATA_BYTES = 10 * 1024

_BOOK_DATA_KEYS = (
    "title",
    "author",
    "authors",
    "provider",
    "provider_id",
    "isbn_13",
    "isbn_10",
    "year",
    "preview",
    "series_name",
    "series_position",
    "series_count",
    "subtitle",
    "language",
    "source_url",
    "search_title",
    "search_author",
    "titles_by_language",
)

_UPDATABLE_ITEM_COLUMNS = frozenset(
    {
        "state",
        "current_task_id",
        "search_count",
        "backoff_level",
        "last_search_at",
        "next_search_at",
        "last_result",
        "searched_sources",
        "obtained_at",
        "obtained_reason",
        "book_data",
    }
)


class WantedServiceError(ValueError):
    """Invalid wanted-list input, carrying the HTTP status to answer with."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        """Store the message and HTTP status code."""
        super().__init__(message)
        self.status_code = status_code


def release_attempt_keys(source: str, source_id: str, match_key: str | None) -> set[str]:
    """Keys under which an attempted release is remembered."""
    keys = {f"id:{source}:{source_id}"}
    if match_key:
        keys.add(f"match:{match_key}")
    return keys


def normalize_book_data(book_data: object) -> dict[str, Any]:
    """Validate a bookmarked book and keep only the fields the wanted list uses."""
    if not isinstance(book_data, dict):
        msg = "book must be an object"
        raise WantedServiceError(msg)
    normalized = {key: book_data[key] for key in _BOOK_DATA_KEYS if book_data.get(key) is not None}
    provider_id = normalized.get("provider_id")
    if isinstance(provider_id, int) and not isinstance(provider_id, bool):
        normalized["provider_id"] = str(provider_id)
    for key in ("title", "provider", "provider_id"):
        if normalize_optional_text(normalized.get(key)) is None:
            msg = f"book.{key} is required"
            raise WantedServiceError(msg)
    if not normalize_optional_text(normalized.get("author")):
        authors = normalized.get("authors")
        if isinstance(authors, list) and authors:
            normalized["author"] = ", ".join(str(author) for author in authors)
    if len(json.dumps(normalized)) > MAX_BOOK_DATA_BYTES:
        msg = "book data is too large"
        raise WantedServiceError(msg)
    return normalized


def canonical_isbn13(isbn_13: object, isbn_10: object = None) -> str | None:
    """The ISBN-13 form of a book's ISBN, so ISBN-10 and ISBN-13 records compare."""
    normalized = normalize_isbn(isbn_13)
    if len(normalized) == 13:
        return normalized
    for variant in isbn_variants(isbn_10) | isbn_variants(isbn_13):
        if len(variant) == 13:
            return variant
    return None


def _merge_book_data(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Fill gaps in a stored book with what another provider knows about it."""
    merged = {**incoming, **existing}
    localized = {
        **(incoming.get("titles_by_language") or {}),
        **(existing.get("titles_by_language") or {}),
    }
    if localized:
        merged["titles_by_language"] = localized
    return merged


def _normalize_languages(languages: object) -> list[str] | None:
    if languages is None:
        return None
    if not isinstance(languages, list) or not all(isinstance(code, str) for code in languages):
        msg = "languages must be a list of language codes"
        raise WantedServiceError(msg)
    cleaned = [code.strip() for code in languages if code.strip()]
    return cleaned or None


def _load_json(value: object) -> Any:
    if not isinstance(value, str) or not value:
        return None
    try:
        return json.loads(value)
    except ValueError:
        return None


class WantedService:
    """SQLite-backed store for wanted items, subscribers and download attempts."""

    def __init__(self, db_path: str) -> None:
        """Initialize the service with the users database path."""
        self._db_path = db_path
        self._lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @staticmethod
    def _row_to_item(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        item["book_data"] = _load_json(item.get("book_data")) or {}
        item["searched_sources"] = _load_json(item.get("searched_sources")) or {}
        return item

    @staticmethod
    def _row_to_subscriber(row: sqlite3.Row) -> dict[str, Any]:
        subscriber = dict(row)
        subscriber["languages"] = _load_json(subscriber.get("languages"))
        return subscriber

    def _get_item(self, conn: sqlite3.Connection, wanted_id: int) -> dict[str, Any] | None:
        row = conn.execute("SELECT * FROM wanted_items WHERE id = ?", (wanted_id,)).fetchone()
        return self._row_to_item(row)

    def _find_existing(
        self, conn: sqlite3.Connection, book: dict[str, Any]
    ) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT * FROM wanted_items WHERE provider = ? AND provider_id = ?",
            (book["provider"], str(book["provider_id"])),
        ).fetchone()
        if row is not None:
            return self._row_to_item(row)
        isbn = book.get("isbn_13")
        if not isbn:
            return None
        row = conn.execute(
            "SELECT * FROM wanted_items WHERE isbn_13 = ? ORDER BY id LIMIT 1", (isbn,)
        ).fetchone()
        return self._row_to_item(row)

    def add(
        self,
        *,
        user_id: int | None,
        book_data: object,
        languages: object = None,
    ) -> tuple[dict[str, Any], bool]:
        """Bookmark a book for ``user_id``, joining an existing entry for the same book.

        Returns the item and whether a new item was created.
        """
        book = normalize_book_data(book_data)
        book["isbn_13"] = canonical_isbn13(book.get("isbn_13"), book.get("isbn_10"))
        book["isbn_10"] = normalize_isbn(book.get("isbn_10")) or None
        book = {key: value for key, value in book.items() if value is not None}
        normalized_languages = _normalize_languages(languages)
        now = now_utc_iso()

        with self._lock:
            conn = self._connect()
            try:
                existing = self._find_existing(conn, book)
                created = existing is None
                if existing is None:
                    cursor = conn.execute(
                        """INSERT INTO wanted_items (
                               provider, provider_id, isbn_13, isbn_10, title, author,
                               book_data, state, next_search_at, created_at, updated_at
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            book["provider"],
                            str(book["provider_id"]),
                            book.get("isbn_13"),
                            book.get("isbn_10"),
                            book["title"],
                            book.get("author"),
                            json.dumps(book),
                            STATE_WAITING,
                            now,
                            now,
                            now,
                        ),
                    )
                    wanted_id = int(cursor.lastrowid or 0)
                else:
                    wanted_id = int(existing["id"])
                    merged = _merge_book_data(existing["book_data"], book)
                    if merged != existing["book_data"]:
                        conn.execute(
                            "UPDATE wanted_items SET book_data = ?, "
                            "isbn_13 = COALESCE(isbn_13, ?), isbn_10 = COALESCE(isbn_10, ?), "
                            "updated_at = ? WHERE id = ?",
                            (
                                json.dumps(merged),
                                book.get("isbn_13"),
                                book.get("isbn_10"),
                                now,
                                wanted_id,
                            ),
                        )

                subscriber = conn.execute(
                    "SELECT id FROM wanted_subscribers "
                    "WHERE wanted_id = ? AND IFNULL(user_id, 0) = IFNULL(?, 0)",
                    (wanted_id, user_id),
                ).fetchone()
                if subscriber is None:
                    conn.execute(
                        "INSERT INTO wanted_subscribers (wanted_id, user_id, languages, created_at) "
                        "VALUES (?, ?, ?, ?)",
                        (
                            wanted_id,
                            user_id,
                            json.dumps(normalized_languages) if normalized_languages else None,
                            now,
                        ),
                    )
                    if existing is not None and existing["state"] == STATE_OBTAINED:
                        # Someone new wants it, so it is wanted again until they have it.
                        conn.execute(
                            "UPDATE wanted_items SET state = ?, next_search_at = ?, "
                            "backoff_level = 0, last_result = NULL, obtained_at = NULL, "
                            "obtained_reason = NULL, updated_at = ? WHERE id = ?",
                            (STATE_WAITING, now, now, wanted_id),
                        )
                    elif existing is not None:
                        # A resting item (nobody could download it) may be searchable now.
                        conn.execute(
                            "UPDATE wanted_items SET next_search_at = ?, updated_at = ? "
                            "WHERE id = ? AND state = ? AND next_search_at IS NULL",
                            (now, now, wanted_id, STATE_WAITING),
                        )
                elif normalized_languages is not None:
                    conn.execute(
                        "UPDATE wanted_subscribers SET languages = ? WHERE id = ?",
                        (json.dumps(normalized_languages), subscriber["id"]),
                    )
                conn.commit()
                item = self._get_item(conn, wanted_id)
            finally:
                conn.close()
        if item is None:
            msg = "Failed to load wanted item"
            raise RuntimeError(msg)
        return item, created

    def remove_subscriber(self, wanted_id: int, user_id: int | None) -> bool:
        """Drop ``user_id``'s bookmark; the item goes once nobody wants it."""
        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.execute(
                    "DELETE FROM wanted_subscribers "
                    "WHERE wanted_id = ? AND IFNULL(user_id, 0) = IFNULL(?, 0)",
                    (wanted_id, user_id),
                )
                removed = cursor.rowcount > 0
                remaining = conn.execute(
                    "SELECT COUNT(*) FROM wanted_subscribers WHERE wanted_id = ?", (wanted_id,)
                ).fetchone()[0]
                if remaining == 0:
                    conn.execute("DELETE FROM wanted_items WHERE id = ?", (wanted_id,))
                conn.commit()
                return removed
            finally:
                conn.close()

    def delete_item(self, wanted_id: int) -> bool:
        """Delete an item with its subscribers and attempts."""
        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.execute("DELETE FROM wanted_items WHERE id = ?", (wanted_id,))
                conn.commit()
                return cursor.rowcount > 0
            finally:
                conn.close()

    def get_item(self, wanted_id: int) -> dict[str, Any] | None:
        """Return one item, or None."""
        conn = self._connect()
        try:
            return self._get_item(conn, wanted_id)
        finally:
            conn.close()

    def list_subscribers(self, wanted_id: int) -> list[dict[str, Any]]:
        """Subscribers of an item with their usernames, oldest first."""
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT s.*, u.username AS username, u.role AS role
                   FROM wanted_subscribers s LEFT JOIN users u ON u.id = s.user_id
                   WHERE s.wanted_id = ? ORDER BY s.created_at, s.id""",
                (wanted_id,),
            ).fetchall()
            return [self._row_to_subscriber(row) for row in rows]
        finally:
            conn.close()

    def all_subscriber_ids(self) -> set[int]:
        """Users with at least one bookmark."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT DISTINCT user_id FROM wanted_subscribers WHERE user_id IS NOT NULL"
            ).fetchall()
            return {int(row["user_id"]) for row in rows}
        finally:
            conn.close()

    def list_items(self, *, user_id: int | None, include_all: bool) -> list[dict[str, Any]]:
        """Items visible to a viewer, newest first, each with subscribers and attempt count."""
        conn = self._connect()
        try:
            if include_all:
                rows = conn.execute("SELECT * FROM wanted_items ORDER BY created_at DESC, id DESC")
            else:
                rows = conn.execute(
                    """SELECT i.* FROM wanted_items i
                       JOIN wanted_subscribers s ON s.wanted_id = i.id
                       WHERE IFNULL(s.user_id, 0) = IFNULL(?, 0)
                       ORDER BY i.created_at DESC, i.id DESC""",
                    (user_id,),
                )
            items = [item for row in rows.fetchall() if (item := self._row_to_item(row))]
            if not items:
                return []
            ids = [item["id"] for item in items]
            placeholders = ", ".join("?" for _ in ids)
            subscriber_rows = conn.execute(
                f"""SELECT s.*, u.username AS username FROM wanted_subscribers s
                    LEFT JOIN users u ON u.id = s.user_id
                    WHERE s.wanted_id IN ({placeholders}) ORDER BY s.created_at, s.id""",  # noqa: S608
                ids,
            ).fetchall()
            attempt_rows = conn.execute(
                f"SELECT wanted_id, COUNT(*) AS n FROM wanted_attempts "  # noqa: S608
                f"WHERE wanted_id IN ({placeholders}) GROUP BY wanted_id",
                ids,
            ).fetchall()
        finally:
            conn.close()

        subscribers: dict[int, list[dict[str, Any]]] = {}
        for row in subscriber_rows:
            subscribers.setdefault(int(row["wanted_id"]), []).append(self._row_to_subscriber(row))
        attempts = {int(row["wanted_id"]): int(row["n"]) for row in attempt_rows}
        for item in items:
            item["subscribers"] = subscribers.get(item["id"], [])
            item["attempt_count"] = attempts.get(item["id"], 0)
        return items

    def find_for_books(
        self, books: list[dict[str, Any]], *, user_id: int | None
    ) -> dict[int, dict[str, Any]]:
        """Wanted state for metadata books, keyed by their index in ``books``.

        A book matches by provider id or by ISBN, so a book bookmarked from another
        metadata provider still shows as wanted.
        """
        keys = [
            (str(book["provider"]), str(book["provider_id"]))
            for book in books
            if book.get("provider") and book.get("provider_id")
        ]
        isbns = {
            isbn
            for book in books
            if (isbn := canonical_isbn13(book.get("isbn_13"), book.get("isbn_10")))
        }
        if not keys and not isbns:
            return {}
        clauses = ["(provider = ? AND provider_id = ?)" for _ in keys]
        params: list[Any] = [value for key in keys for value in key]
        if isbns:
            clauses.append(f"isbn_13 IN ({', '.join('?' for _ in isbns)})")
            params.extend(sorted(isbns))
        conn = self._connect()
        try:
            rows = conn.execute(
                f"""SELECT i.id, i.provider, i.provider_id, i.isbn_13, i.state,
                       EXISTS (SELECT 1 FROM wanted_subscribers s WHERE s.wanted_id = i.id
                               AND IFNULL(s.user_id, 0) = IFNULL(?, 0)) AS subscribed
                    FROM wanted_items i WHERE {" OR ".join(clauses)}""",  # noqa: S608
                [user_id, *params],
            ).fetchall()
        finally:
            conn.close()
        by_key = {(row["provider"], row["provider_id"]): row for row in rows}
        by_isbn = {row["isbn_13"]: row for row in rows if row["isbn_13"]}
        result: dict[int, dict[str, Any]] = {}
        for index, book in enumerate(books):
            row = by_key.get((str(book.get("provider")), str(book.get("provider_id"))))
            if row is None:
                isbn = canonical_isbn13(book.get("isbn_13"), book.get("isbn_10"))
                row = by_isbn.get(isbn) if isbn else None
            if row is not None:
                result[index] = {
                    "id": row["id"],
                    "state": row["state"],
                    "subscribed": bool(row["subscribed"]),
                }
        return result

    def prune_orphans(self) -> int:
        """Delete items nobody wants any more (their last subscriber's account was deleted)."""
        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.execute(
                    "DELETE FROM wanted_items WHERE NOT EXISTS ("
                    "SELECT 1 FROM wanted_subscribers s WHERE s.wanted_id = wanted_items.id)"
                )
                conn.commit()
                return cursor.rowcount
            finally:
                conn.close()

    def items_in_state(self, state: str) -> list[dict[str, Any]]:
        """All items currently in ``state``."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM wanted_items WHERE state = ? ORDER BY id", (state,)
            ).fetchall()
            return [item for row in rows if (item := self._row_to_item(row))]
        finally:
            conn.close()

    def due_items(self, now: str, limit: int) -> list[dict[str, Any]]:
        """Waiting items whose next search is due, most overdue first."""
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT * FROM wanted_items
                   WHERE state = ? AND next_search_at IS NOT NULL AND next_search_at <= ?
                     AND EXISTS (SELECT 1 FROM wanted_subscribers s
                                 WHERE s.wanted_id = wanted_items.id)
                   ORDER BY next_search_at, id LIMIT ?""",
                (STATE_WAITING, now, limit),
            ).fetchall()
            return [item for row in rows if (item := self._row_to_item(row))]
        finally:
            conn.close()

    def update_item(
        self,
        wanted_id: int,
        *,
        expected_state: str | None = None,
        **fields: Any,
    ) -> dict[str, Any] | None:
        """Update item columns; None when the item is gone or not in ``expected_state``."""
        unknown = set(fields) - _UPDATABLE_ITEM_COLUMNS
        if unknown:
            msg = f"Unknown wanted item fields: {sorted(unknown)}"
            raise ValueError(msg)
        values = dict(fields)
        for key in ("book_data", "searched_sources"):
            if key in values and values[key] is not None and not isinstance(values[key], str):
                values[key] = json.dumps(values[key])
        values["updated_at"] = now_utc_iso()
        assignments = ", ".join(f"{column} = ?" for column in values)
        params: list[Any] = [*values.values(), wanted_id]
        where = "id = ?"
        if expected_state is not None:
            where += " AND state = ?"
            params.append(expected_state)
        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.execute(
                    f"UPDATE wanted_items SET {assignments} WHERE {where}",  # noqa: S608
                    params,
                )
                conn.commit()
                if cursor.rowcount == 0:
                    return None
                return self._get_item(conn, wanted_id)
            finally:
                conn.close()

    def mark_all_due(self) -> int:
        """Make every waiting item due now and restart its backoff (a source changed)."""
        now = now_utc_iso()
        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.execute(
                    "UPDATE wanted_items SET next_search_at = ?, backoff_level = 0, updated_at = ? "
                    "WHERE state = ?",
                    (now, now, STATE_WAITING),
                )
                conn.commit()
                return cursor.rowcount
            finally:
                conn.close()

    def reset_interrupted(self) -> int:
        """After a restart, put searching/downloading items back to waiting."""
        now = now_utc_iso()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE wanted_attempts SET status = ?, error_message = ?, finished_at = ? "
                    "WHERE status = ?",
                    (ATTEMPT_CANCELLED, "Interrupted by restart", now, ATTEMPT_QUEUED),
                )
                cursor = conn.execute(
                    "UPDATE wanted_items SET state = ?, current_task_id = NULL, "
                    "next_search_at = ?, updated_at = ? WHERE state IN (?, ?)",
                    (STATE_WAITING, now, now, STATE_SEARCHING, STATE_DOWNLOADING),
                )
                conn.commit()
                return cursor.rowcount
            finally:
                conn.close()

    def record_attempt(
        self,
        wanted_id: int,
        *,
        cycle: int,
        source: str,
        source_id: str,
        match_key: str | None,
        release_title: str | None,
        file_format: str | None,
        size: str | None,
        indexer: str | None,
    ) -> int:
        """Log a release queued for an item."""
        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.execute(
                    """INSERT INTO wanted_attempts (
                           wanted_id, cycle, source, source_id, match_key, release_title,
                           format, size, indexer, status, created_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        wanted_id,
                        cycle,
                        source,
                        source_id,
                        match_key,
                        release_title,
                        file_format,
                        size,
                        indexer,
                        ATTEMPT_QUEUED,
                        now_utc_iso(),
                    ),
                )
                conn.commit()
                return int(cursor.lastrowid or 0)
            finally:
                conn.close()

    def finish_attempt(
        self, wanted_id: int, source_id: str, status: str, error_message: str | None = None
    ) -> None:
        """Record how the open attempt for ``source_id`` ended."""
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """UPDATE wanted_attempts SET status = ?, error_message = ?, finished_at = ?
                       WHERE id = (SELECT id FROM wanted_attempts
                                   WHERE wanted_id = ? AND source_id = ? AND status = ?
                                   ORDER BY id DESC LIMIT 1)""",
                    (status, error_message, now_utc_iso(), wanted_id, source_id, ATTEMPT_QUEUED),
                )
                conn.commit()
            finally:
                conn.close()

    def excluded_release_keys(self, wanted_id: int, cycle: int) -> set[str]:
        """Keys of releases not to try now: blocked for good, or already tried this cycle."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT cycle, source, source_id, match_key, status, finished_at "
                "FROM wanted_attempts WHERE wanted_id = ?",
                (wanted_id,),
            ).fetchall()
        finally:
            conn.close()
        expiry = (datetime.now(UTC) - timedelta(days=BLOCK_EXPIRY_DAYS)).isoformat(
            timespec="seconds"
        )
        failed_cycles: dict[str, set[int]] = {}
        excluded: set[str] = set()
        for row in rows:
            keys = release_attempt_keys(row["source"], row["source_id"], row["match_key"])
            if row["cycle"] == cycle or row["status"] == ATTEMPT_REJECTED:
                excluded |= keys
            if row["status"] == ATTEMPT_ERROR and (row["finished_at"] or "") >= expiry:
                for key in keys:
                    failed_cycles.setdefault(key, set()).add(int(row["cycle"]))
        excluded |= {
            key for key, cycles in failed_cycles.items() if len(cycles) >= BLOCK_AFTER_FAILED_CYCLES
        }
        return excluded

    def list_attempts(self, wanted_id: int, limit: int = 50) -> list[dict[str, Any]]:
        """Most recent attempts for an item."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM wanted_attempts WHERE wanted_id = ? ORDER BY id DESC LIMIT ?",
                (wanted_id, limit),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def clear_blocklist(self, wanted_id: int) -> int:
        """Forget failed and rejected attempts so those releases may be tried again."""
        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.execute(
                    "DELETE FROM wanted_attempts WHERE wanted_id = ? AND status IN (?, ?)",
                    (wanted_id, ATTEMPT_ERROR, ATTEMPT_REJECTED),
                )
                conn.commit()
                return cursor.rowcount
            finally:
                conn.close()

    def mark_delivered(
        self, wanted_id: int, user_ids: list[int | None], task_id: str | None
    ) -> None:
        """Record that these subscribers have the book."""
        if not user_ids:
            return
        now = now_utc_iso()
        with self._lock:
            conn = self._connect()
            try:
                conn.executemany(
                    "UPDATE wanted_subscribers SET delivered_at = ?, delivery_task_id = ? "
                    "WHERE wanted_id = ? AND IFNULL(user_id, 0) = IFNULL(?, 0)",
                    [(now, task_id, wanted_id, user_id) for user_id in user_ids],
                )
                conn.commit()
            finally:
                conn.close()
