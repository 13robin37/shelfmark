"""Wanted list settings registration."""

from shelfmark.core.settings_registry import (
    CheckboxField,
    HeadingField,
    NumberField,
    OrderableListField,
    SelectField,
    SettingsField,
    TextField,
    register_settings,
)
from shelfmark.core.wanted_config import (
    DEFAULT_BACKOFF_HOURS,
    DEFAULT_MAX_SEARCHES_PER_HOUR,
    default_format_order,
    default_source_order,
)

_SOURCE_LABELS = {
    "direct_download": "Anna's Archive / Direct Download",
    "libgen": "Libgen",
    "prowlarr": "Prowlarr",
    "newznab": "Newznab",
    "irc": "IRC",
}


def _source_options() -> list[dict[str, object]]:
    from shelfmark.release_sources import list_available_sources

    options: list[dict[str, object]] = []
    for source in list_available_sources():
        if "ebook" not in source["supported_content_types"]:
            continue
        name = source["name"]
        options.append(
            {
                "id": name,
                "label": _SOURCE_LABELS.get(name, source["display_name"]),
                "description": None if source["enabled"] else "Source is not enabled",
            }
        )
    return options


def _format_options() -> list[dict[str, object]]:
    return [{"id": entry["id"], "label": entry["id"].upper()} for entry in default_format_order()]


def _cutoff_options() -> list[dict[str, str]]:
    return [
        {"value": "", "label": "First preferred format"},
        *({"value": entry["id"], "label": entry["id"].upper()} for entry in default_format_order()),
    ]


@register_settings("wanted", "Wanted List", icon="eye", order=8)
def wanted_settings() -> list[SettingsField]:
    """Settings for bookmarked books that download automatically."""
    return [
        CheckboxField(
            key="WANTED_ENABLED",
            label="Enable Wanted List",
            description=(
                "Let users bookmark ebooks. Shelfmark then searches the sources below in the "
                "background, downloads the best matching release and keeps looking until the "
                "book is obtained, including when new sources or releases appear."
            ),
            default=False,
        ),
        HeadingField(
            key="wanted_sources_heading",
            title="Sources and Formats",
            description=(
                "Every enabled source is searched on each pass; releases from all of them are "
                "tried best first. Only users allowed to download directly can bookmark."
            ),
        ),
        OrderableListField(
            key="WANTED_SOURCES",
            label="Sources",
            description=(
                "Sources searched for bookmarked books, in order of preference. IRC is off by "
                "default: its results are cached for 30 days and identical searches are limited."
            ),
            options=_source_options,
            default=default_source_order(),
            show_when={"field": "WANTED_ENABLED", "value": True},
        ),
        OrderableListField(
            key="WANTED_PREFERRED_FORMATS",
            label="Preferred Formats",
            description=(
                "Formats in order of preference. Only enabled formats are downloaded "
                "automatically; PDF and comic formats are off by default. Formats must also be "
                "allowed under Supported Book Formats."
            ),
            options=_format_options,
            default=default_format_order(),
            show_when={"field": "WANTED_ENABLED", "value": True},
        ),
        SelectField(
            key="WANTED_FORMAT_CUTOFF",
            label="Good Enough Format",
            description=(
                "While waiting for a better format, only releases in this format or a preferred "
                "one above it are downloaded."
            ),
            options=_cutoff_options,
            default="",
            show_when={"field": "WANTED_ENABLED", "value": True},
        ),
        NumberField(
            key="WANTED_FORMAT_WAIT_DAYS",
            label="Wait for Good Enough Format (days)",
            description=(
                "Days after bookmarking during which lower formats are skipped. 0 downloads the "
                "best format found right away."
            ),
            default=0,
            min_value=0,
            max_value=90,
            show_when={"field": "WANTED_ENABLED", "value": True},
        ),
        HeadingField(
            key="wanted_schedule_heading",
            title="Search Schedule",
            description="Background searches always give way to searches users run themselves.",
        ),
        TextField(
            key="WANTED_SEARCH_BACKOFF_HOURS",
            label="Search Intervals (hours)",
            description=(
                "Hours to wait between searches for a book that was not found, comma separated. "
                "The last value repeats for as long as the book stays bookmarked."
            ),
            default=", ".join(f"{hours:g}" for hours in DEFAULT_BACKOFF_HOURS),
            placeholder="1, 6, 24",
            show_when={"field": "WANTED_ENABLED", "value": True},
        ),
        NumberField(
            key="WANTED_MAX_SEARCHES_PER_HOUR",
            label="Max Searches per Hour",
            description="Upper limit for background searches across all bookmarked books.",
            default=DEFAULT_MAX_SEARCHES_PER_HOUR,
            min_value=1,
            max_value=60,
            show_when={"field": "WANTED_ENABLED", "value": True},
        ),
    ]
