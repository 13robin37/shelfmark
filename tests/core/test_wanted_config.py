from __future__ import annotations

from unittest.mock import patch

from shelfmark.core import wanted_config


def _with(settings):
    return patch.object(
        wanted_config.config,
        "get",
        side_effect=lambda key, default=None: settings.get(key, default),
    )


def test_pdf_and_comics_are_off_by_default():
    with _with({}):
        order, excluded = wanted_config.preferred_formats()
    assert order == ("epub", "azw3", "mobi", "fb2")
    assert excluded == {"pdf", "djvu", "cbz", "cbr"}


def test_a_plain_list_of_formats_enables_them():
    with _with({"WANTED_PREFERRED_FORMATS": ["epub", "mobi"]}):
        assert wanted_config.preferred_formats() == (("epub", "mobi"), frozenset())
