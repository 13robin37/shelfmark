from __future__ import annotations

import pytest

from shelfmark.metadata_providers import parse_release_date


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2027-03-12", "2027-03-12"),
        ("2027-03-12T00:00:00Z", "2027-03-12"),
        ("March 12, 2027", "2027-03-12"),
        ("Mar 12, 2027", "2027-03-12"),
        ("12 March 2027", "2027-03-12"),
        ("2027", None),
        ("", None),
        (None, None),
        ("soon", None),
    ],
)
def test_parse_release_date(value, expected):
    assert parse_release_date(value) == expected
