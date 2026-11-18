"""
Tests for the VRT payload parsers.

The provider tests mock the API client, so these cover the layer underneath it: turning
VRT's GraphQL shapes into typed objects. The payload fragments mirror what the live API
returns, including the awkward parts (missing fields, wrong types, nested season tabs).
"""

from __future__ import annotations

from typing import Any

from music_assistant.providers.vrt_max.models import VrtSeason
from music_assistant.providers.vrt_max.parsers import (
    _brand_display_name,
    _collect_presenters,
    _collect_seasons,
    _favourite_id,
    _first_meta,
    _first_node_type,
    _link,
    _parse_duration,
    _parse_episode_tile,
    _parse_header,
    _parse_iso,
    _parse_program_tile,
    _presenters_from_header,
)


def _tile(**overrides: Any) -> dict[str, Any]:
    """Build an episode/program tile node as the tile lists return one."""
    node = {
        "title": "Sweet summer sundays",
        "action": {"__typename": "LinkAction", "link": "/vrtmax/luister/radio/s/show~1-2/ep/"},
        "image": {"templateUrl": "https://images.vrt.be/orig/2026/06/30/abc.jpg"},
    }
    node.update(overrides)
    return node


# --- brand names -------------------------------------------------------------------


def test_brand_display_name_maps_known_slugs() -> None:
    """A known brand slug becomes the channel's real name."""
    assert _brand_display_name("stubru") == "Studio Brussel"
    assert _brand_display_name("vrtnws") == "VRT NWS"


def test_brand_display_name_prettifies_unknown_slugs() -> None:
    """An unknown slug is prettified rather than dropped, so new channels still show."""
    assert _brand_display_name("radio-bene") == "Radio Bene"


def test_brand_display_name_rejects_non_strings() -> None:
    """A missing or non-string brand yields None."""
    assert _brand_display_name(None) is None
    assert _brand_display_name("") is None
    assert _brand_display_name(42) is None


# --- presenters --------------------------------------------------------------------


def test_presenters_from_header_drops_media_kind_and_channel() -> None:
    """The breadcrumb's media-kind label and channel name are not presenters."""
    header = {
        "secondaryMeta": [
            {"value": "radio"},
            {"value": "Radio 1"},
            {"value": "Alice Example"},
        ]
    }

    assert _presenters_from_header(header, "Radio 1") == ("Alice Example",)


def test_presenters_from_header_drops_season_counts() -> None:
    """A 'Seizoen N' entry is a season count, not a presenter."""
    header = {"secondaryMeta": [{"value": "Seizoen 3"}, {"value": "Bob"}]}

    assert _presenters_from_header(header, None) == ("Bob",)


def test_presenters_from_header_handles_missing_header() -> None:
    """A missing or malformed header yields no presenters."""
    assert _presenters_from_header(None, None) == ()
    assert _presenters_from_header({}, None) == ()


def test_collect_presenters_walks_nested_components() -> None:
    """Presenters live in a PresentersList nested two levels inside the page tabs."""
    components = [
        {
            "__typename": "ContainerNavigation",
            "items": [
                {
                    "components": [
                        {
                            "__typename": "PresentersList",
                            "presenters": [{"title": "Alice"}, {"title": "Bob"}],
                        }
                    ]
                }
            ],
        }
    ]

    assert _collect_presenters(components) == ("Alice", "Bob")


def test_collect_presenters_deduplicates() -> None:
    """A presenter listed twice appears once."""
    components = [
        {
            "__typename": "ContainerNavigation",
            "items": [
                {
                    "components": [
                        {"__typename": "PresentersList", "presenters": [{"title": "Alice"}]},
                        {"__typename": "PresentersList", "presenters": [{"title": "Alice"}]},
                    ]
                }
            ],
        }
    ]

    assert _collect_presenters(components) == ("Alice",)


# --- seasons -----------------------------------------------------------------------


def _episode_list(component_id: str, title: str | None) -> dict[str, Any]:
    """Build a PaginatedTileList holding episode tiles."""
    return {
        "__typename": "PaginatedTileList",
        "componentId": component_id,
        "title": title,
        "paginatedItems": {"edges": [{"node": {"__typename": "RadioEpisodeTile"}}]},
    }


def test_collect_seasons_finds_nested_season_tabs() -> None:
    """Multi-season podcasts nest their episode lists inside season navigation."""
    components = [
        {
            "__typename": "ContainerNavigation",
            "items": [{"components": [_episode_list("comp-s2", "Seizoen 2")]}],
        }
    ]
    seasons: list[VrtSeason] = []

    _collect_seasons(components, seasons)

    assert seasons == [VrtSeason(title="Seizoen 2", component_id="comp-s2")]


def test_collect_seasons_skips_scheduled_tabs() -> None:
    """A 'Gepland' tab holds broadcasts that have not aired and cannot be played."""
    components = [
        {
            "__typename": "ContainerNavigation",
            "items": [
                {"title": "Gepland", "components": [_episode_list("comp-future", "Gepland")]},
                {"title": "Herbeluister", "components": [_episode_list("comp-past", "S1")]},
            ],
        }
    ]
    seasons: list[VrtSeason] = []

    _collect_seasons(components, seasons)

    assert [s.component_id for s in seasons] == ["comp-past"]


def test_collect_seasons_ignores_non_episode_lists() -> None:
    """A tile list of programmes rather than episodes is not a season."""
    components = [
        {
            "__typename": "PaginatedTileList",
            "componentId": "comp-programs",
            "paginatedItems": {"edges": [{"node": {"__typename": "RadioProgramTile"}}]},
        }
    ]
    seasons: list[VrtSeason] = []

    _collect_seasons(components, seasons)

    assert seasons == []


# --- tiles -------------------------------------------------------------------------


def test_parse_program_tile() -> None:
    """A programme tile maps to page id, title, description and artwork."""
    tile = _parse_program_tile(_tile(description="A show about things"))

    assert tile is not None
    assert tile.page_id == "/vrtmax/luister/radio/s/show~1-2/ep/"
    assert tile.title == "Sweet summer sundays"
    assert tile.description == "A show about things"
    assert tile.image_url == "https://images.vrt.be/w1280hx/2026/06/30/abc.jpg"


def test_parse_program_tile_requires_a_link_and_title() -> None:
    """A tile without a usable link or title is skipped rather than half-parsed."""
    assert _parse_program_tile(_tile(action=None)) is None
    assert _parse_program_tile(_tile(title="")) is None


def test_parse_episode_tile_reads_progress_and_duration() -> None:
    """An episode tile carries the user's progress and a formatted duration."""
    episode = _parse_episode_tile(
        _tile(
            formattedDuration="1 u 5 min",
            primaryMeta=[{"value": "Zondag 9 augustus"}],
            progress={"completed": True, "progressInSeconds": 42},
        )
    )

    assert episode is not None
    assert episode.duration == 3900
    assert episode.date_label == "Zondag 9 augustus"
    assert episode.fully_played is True
    assert episode.resume_position == 42


def test_parse_episode_tile_without_progress() -> None:
    """An episode with no progress object defaults to unplayed at position zero."""
    episode = _parse_episode_tile(_tile())

    assert episode is not None
    assert episode.fully_played is False
    assert episode.resume_position == 0


# --- small helpers -----------------------------------------------------------------


def test_parse_duration_formats() -> None:
    """Durations are given as Dutch formatted strings."""
    assert _parse_duration("60 min") == 3600
    assert _parse_duration("1 u 5 min") == 3900
    assert _parse_duration("2 uur") == 7200
    assert _parse_duration("") == 0
    assert _parse_duration(None) == 0


def test_parse_iso_accepts_vrt_timestamps() -> None:
    """VRT timestamps parse, and anything else yields None rather than raising."""
    assert _parse_iso("2026-08-09T10:00:00.000Z") is not None
    assert _parse_iso("not a date") is None
    assert _parse_iso(None) is None


def test_link_reads_the_link_action() -> None:
    """The page path comes from the tile's LinkAction."""
    assert _link(_tile()) == "/vrtmax/luister/radio/s/show~1-2/ep/"
    assert _link({"action": {"__typename": "SomethingElse"}}) is None


def test_first_meta_returns_first_non_empty_value() -> None:
    """The date label is the first usable primaryMeta entry."""
    assert _first_meta([{"value": ""}, {"value": "Zondag 9 augustus"}]) == "Zondag 9 augustus"
    assert _first_meta([]) is None


def test_first_node_type_reads_the_tile_typename() -> None:
    """The tile type identifies what a paginated list holds."""
    items = {"edges": [{"node": {"__typename": "RadioProgramTile"}}]}

    assert _first_node_type(items) == "RadioProgramTile"
    assert _first_node_type({"edges": []}) is None
    assert _first_node_type(None) is None


def test_parse_header_reads_description_and_image() -> None:
    """A page header carries the rich description and the artwork."""
    description, image = _parse_header(
        {
            "richDescription": {"text": "  A show about things  "},
            "image": {"templateUrl": "https://images.vrt.be/orig/2026/06/30/abc.jpg"},
        }
    )

    assert description == "A show about things"
    assert image == "https://images.vrt.be/w1280hx/2026/06/30/abc.jpg"


def test_parse_header_handles_missing_header() -> None:
    """A missing header yields no description and no image."""
    assert _parse_header(None) == (None, None)


def test_favourite_id_only_accepts_podcast_tiles() -> None:
    """A favourite that is not a programme/podcast tile is not a library podcast."""
    node = _tile(__typename="PodcastProgramTile")
    assert _favourite_id(node) == "/vrtmax/luister/radio/s/show~1-2/ep/"
    assert _favourite_id(_tile(__typename="AudioLivestreamTile")) is None
    assert _favourite_id(None) is None
