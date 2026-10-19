"""Reading the newest releases (RSS feeds) of Prowlarr indexers."""

from __future__ import annotations

from unittest.mock import MagicMock

import shelfmark.release_sources.prowlarr.api as prowlarr_api
import shelfmark.release_sources.prowlarr.source as prowlarr_source
from shelfmark.release_sources.prowlarr.api import ProwlarrClient, ProwlarrSearchError
from shelfmark.release_sources.prowlarr.source import ProwlarrSource

_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:torznab="http://torznab.com/schemas/2015/feed"><channel>
<item><title>Frank Herbert - Dune (epub)</title><guid>g1</guid><size>1000</size>
<link>https://example.org/1.torrent</link></item>
</channel></rss>"""


class _Response:
    ok = True
    status_code = 200
    text = _FEED

    def raise_for_status(self):
        return None


def test_a_feed_read_is_a_search_without_a_query(monkeypatch):
    monkeypatch.setattr(prowlarr_api.config, "get", lambda key, default=None: default)
    client = ProwlarrClient("http://prowlarr", "key")
    calls = []
    monkeypatch.setattr(
        client._session, "get", lambda **kwargs: calls.append(kwargs) or _Response()
    )

    results = client.torznab_recent(indexer_id=4, categories=[7000])

    assert calls[0]["url"] == "http://prowlarr/api/v1/indexer/4/newznab"
    assert calls[0]["params"] == {"t": "search", "limit": 100, "cat": "7000"}
    assert results[0]["title"] == "Frank Herbert - Dune (epub)"
    assert results[0]["indexerId"] == 4


def test_recent_releases_read_every_selected_indexer(monkeypatch):
    monkeypatch.setattr(prowlarr_source.config, "get", lambda key, default=None: default)
    client = MagicMock()
    client.indexer_timeout = 30
    client.get_enabled_indexers_detailed.return_value = [
        {"id": 1, "name": "A", "priority": 25, "capabilities": {"categories": [{"id": 7000}]}},
        {"id": 2, "name": "B", "priority": 25, "capabilities": {"categories": [{"id": 7000}]}},
        {
            "id": 3,
            "name": "No feed",
            "supportsRss": False,
            "capabilities": {"categories": [{"id": 7000}]},
        },
    ]
    client.get_enriched_indexer_ids.return_value = []
    client.get_disabled_indexers.return_value = {}

    def recent(*, indexer_id, categories):
        if indexer_id == 2:
            raise ProwlarrSearchError("down")
        return [{"title": "Dune", "guid": "g1", "indexerId": 1, "indexer": "A", "size": 10}]

    client.torznab_recent.side_effect = recent
    source = ProwlarrSource()
    monkeypatch.setattr(source, "_get_client", lambda: client)

    releases = source.recent_releases("ebook")

    assert [release.title for release in releases] == ["Dune"]
    # Indexer 3 has no feed; indexer 2 failed without sinking indexer 1.
    assert client.torznab_recent.call_count == 2


def test_no_feed_without_prowlarr(monkeypatch):
    source = ProwlarrSource()
    monkeypatch.setattr(source, "_get_client", lambda: None)
    assert source.recent_releases("ebook") is None


def test_newznab_feeds_read_every_connection(monkeypatch):
    from shelfmark.release_sources.newznab.source import NewznabSource, _NamedClient

    client = MagicMock()
    client.recent.return_value = [
        {"title": "Dune", "guid": "g1", "downloadUrl": "https://x/1.nzb", "size": 10}
    ]
    source = NewznabSource()
    monkeypatch.setattr(
        source,
        "_get_clients",
        lambda: [_NamedClient(name="Indexer", connection_id="abc", client=client)],
    )

    releases = source.recent_releases("ebook")

    assert [release.indexer for release in releases] == ["Indexer"]
    assert client.recent.call_args.kwargs["categories"]
