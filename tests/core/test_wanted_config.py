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
    with patch.object(
        wanted_config.config,
        "get",
        side_effect=lambda key, default=None: settings.get(key, default),
    ):
        assert wanted_config.preferred_formats() == expected
