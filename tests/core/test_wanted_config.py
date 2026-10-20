from __future__ import annotations

from unittest.mock import patch

import pytest

from shelfmark.core import wanted_config


@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        # PDF and comics are off by default.
        ({}, (("epub", "azw3", "mobi", "fb2"), {"pdf", "djvu", "cbz", "cbr"})),
        # A plain list of formats enables just them.
        ({"WANTED_PREFERRED_FORMATS": ["epub", "mobi"]}, (("epub", "mobi"), frozenset())),
    ],
)
def test_preferred_formats(settings, expected):
    with _settings(settings):
        assert wanted_config.preferred_formats() == expected


def test_unusable_numbers_fall_back_to_safe_values():
    with _settings(
        {
            "WANTED_SEARCH_BACKOFF_HOURS": "1, inf, nan, 99999999",
            "WANTED_FORMAT_WAIT_DAYS": "nan",
            "WANTED_MAX_SEARCHES_PER_HOUR": "inf",
        }
    ):
        assert wanted_config.backoff_hours() == (1.0, wanted_config.MAX_BACKOFF_HOURS)
        assert wanted_config.format_wait() == (None, 0.0)
        assert wanted_config.max_searches_per_hour() == wanted_config.DEFAULT_MAX_SEARCHES_PER_HOUR
    with _settings({"WANTED_FORMAT_WAIT_DAYS": "1e9"}):
        assert wanted_config.format_wait() == (None, wanted_config.MAX_FORMAT_WAIT_DAYS)


def _settings(settings):
    return patch.object(
        wanted_config.config,
        "get",
        side_effect=lambda key, default=None: settings.get(key, default),
    )
