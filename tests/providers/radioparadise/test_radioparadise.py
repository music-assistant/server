"""Tests for the Radio Paradise music provider."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.streams.audio import StreamsAudio
from music_assistant.controllers.streams.constants import (
    STREAMDETAILS_INBAND_TITLE_HANDOFF_KEY,
    STREAMDETAILS_INBAND_TITLE_KEY,
)
from music_assistant.providers.radioparadise import SUPPORTED_FEATURES
from music_assistant.providers.radioparadise.helpers import find_song_by_stream_title
from music_assistant.providers.radioparadise.provider import RadioParadiseProvider


def _block() -> dict[str, Any]:
    """Return a play API block payload with two songs."""
    return {
        "sched_time_millis": 1_000_000,
        "song": {
            "0": {
                "artist": "Mary Gauthier",
                "title": "Mercy Now",
                "event": "100",
                "elapsed": 0,
                "duration": 200_000,
                "album": "Mercy Now",
                "year": 2005,
                "cover": "covers/l/1.jpg",
            },
            "1": {
                "artist": "Mark Ambor",
                "title": "Good to Be",
                "event": "101",
                "elapsed": 200_000,
                "duration": 180_000,
                "album": "Good to Be",
                "year": 2024,
                "cover": "covers/l/2.jpg",
            },
        },
    }


def _stream_details(*, opt_in: bool = False) -> StreamDetails:
    """Return minimal StreamDetails for the Mellow Mix channel."""
    return StreamDetails(
        item_id="1",
        provider="radioparadise_test",
        audio_format=AudioFormat(content_type=ContentType.FLAC, channels=2),
        media_type=MediaType.RADIO,
        stream_type=StreamType.HTTP,
        path="http://example.invalid/stream",
        allow_seek=False,
        can_seek=False,
        duration=0,
        data={STREAMDETAILS_INBAND_TITLE_HANDOFF_KEY: True} if opt_in else None,
    )


@pytest.fixture
def provider() -> RadioParadiseProvider:
    """Return a RadioParadiseProvider wired to mocks for unit testing."""
    manifest = MagicMock()
    manifest.domain = "radioparadise"
    manifest.name = "Radio Paradise"

    config = MagicMock()
    config.instance_id = "radioparadise_test"
    config.values = {}

    def _get_value(key: str, default: Any = None) -> Any:
        if key == "log_level":
            return "GLOBAL"
        return default

    config.get_value.side_effect = _get_value
    return RadioParadiseProvider(MagicMock(), manifest, config, SUPPORTED_FEATURES)


# ---------------------------------------------------------------------------
# find_song_by_stream_title


def test_find_song_matches_normalized() -> None:
    """Case and whitespace differences do not prevent a match."""
    songs = _block()["song"]
    assert find_song_by_stream_title(songs, "mary gauthier  -  MERCY now") == songs["0"]


def test_find_song_joins_instead_of_splitting_dashed_artist() -> None:
    """A dash inside the artist name cannot break matching (the ICY string is never split)."""
    songs = {"0": {"artist": "Jay - Z", "title": "Song A", "event": "1"}}
    assert find_song_by_stream_title(songs, "Jay - Z - Song A") == songs["0"]


def test_find_song_no_match_returns_none() -> None:
    """Unknown or blank titles resolve to None."""
    songs = _block()["song"]
    assert find_song_by_stream_title(songs, "Commercial-free - Listener-supported") is None
    assert find_song_by_stream_title(songs, " ") is None


# ---------------------------------------------------------------------------
# _update_stream_metadata


async def test_icy_title_matched_from_cached_block(provider: RadioParadiseProvider) -> None:
    """A cached-block hit publishes enriched metadata without any API call."""
    sd = _stream_details()
    sd.data = {
        "block_data": _block(),
        STREAMDETAILS_INBAND_TITLE_KEY: "Mary Gauthier - Mercy Now",
    }
    provider._fetch_json = AsyncMock()  # type: ignore[method-assign]

    await provider._update_stream_metadata(sd, 0)

    provider._fetch_json.assert_not_awaited()
    assert sd.stream_metadata is not None
    assert sd.stream_metadata.title == "Mercy Now"
    assert sd.stream_metadata.artist == "Mary Gauthier"

    # The second tick alternates the artist slot to the upcoming-track info.
    await provider._update_stream_metadata(sd, 10)
    assert sd.stream_metadata.artist == "Up Next: Mark Ambor - Good to Be"


async def test_block_refetch_is_validated_and_cached(provider: RadioParadiseProvider) -> None:
    """On cache miss the block is fetched, accepted when it contains the title, and cached."""
    sd = _stream_details()
    sd.data = {STREAMDETAILS_INBAND_TITLE_KEY: "Mark Ambor - Good to Be"}
    block = _block()
    provider._fetch_json = AsyncMock(return_value=block)  # type: ignore[method-assign]

    await provider._update_stream_metadata(sd, 0)

    provider._fetch_json.assert_awaited_once()
    assert sd.data is not None
    assert sd.data["block_data"] is block
    assert sd.stream_metadata is not None
    assert sd.stream_metadata.title == "Good to Be"


async def test_unmatched_title_shows_verbatim_and_retries(
    provider: RadioParadiseProvider,
) -> None:
    """A title missing from a (stale/wobbling) block shows verbatim; lookups keep retrying."""
    sd = _stream_details()
    sd.data = {
        "block_data": _block(),
        STREAMDETAILS_INBAND_TITLE_KEY: "Madrugada - This Old House",
    }
    stale_block = _block()  # does not contain the title
    provider._fetch_json = AsyncMock(return_value=stale_block)  # type: ignore[method-assign]

    await provider._update_stream_metadata(sd, 0)

    assert sd.stream_metadata is not None
    assert sd.stream_metadata.title == "Madrugada - This Old House"
    assert sd.stream_metadata.artist is None

    # Second tick with the same unmatched title: re-fetch, but no re-publish.
    sd.stream_metadata = None
    await provider._update_stream_metadata(sd, 10)
    assert provider._fetch_json.await_count == 2
    assert sd.stream_metadata is None


async def test_no_icy_title_falls_back_to_schedule_guess(
    provider: RadioParadiseProvider,
) -> None:
    """Without an in-band title the legacy schedule-derived lookup is used."""
    sd = _stream_details()
    block = _block()
    provider._get_channel_metadata = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "current": block["song"]["0"],
            "next": block["song"]["1"],
            "block_data": block,
        }
    )

    await provider._update_stream_metadata(sd, 0)

    provider._get_channel_metadata.assert_awaited_once()
    assert sd.stream_metadata is not None
    assert sd.stream_metadata.title == "Mercy Now"


async def test_get_stream_details_opts_in_and_seeds_block_cache(
    provider: RadioParadiseProvider,
) -> None:
    """Initial enriched metadata opts in and keeps its block for ICY matching."""
    block = _block()
    provider._get_channel_metadata = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "current": block["song"]["0"],
            "next": block["song"]["1"],
            "block_data": block,
        }
    )

    details = await provider.get_stream_details("1", MediaType.RADIO)

    assert isinstance(details.path, str)
    assert details.path.endswith("mellow-flacm")
    assert details.stream_metadata_update_callback == provider._update_stream_metadata
    assert details.stream_metadata_update_interval == 10
    assert details.data is not None
    assert details.data[STREAMDETAILS_INBAND_TITLE_HANDOFF_KEY] is True
    assert details.data["block_data"] is block
    assert details.stream_metadata is not None
    assert details.stream_metadata.title == "Mercy Now"


async def test_controller_to_provider_inband_handoff(
    provider: RadioParadiseProvider,
) -> None:
    """The controller's captured ICY title is consumed by Radio Paradise enrichment."""
    sd = _stream_details(opt_in=True)
    audio = StreamsAudio(MagicMock())
    audio_mass = cast("Any", audio.mass)
    audio_mass.metadata.normalize_radio_artist_name.side_effect = lambda artist: artist
    audio_mass.metadata.get_radio_stream_station_image.return_value = None
    audio._parse_icy_metadata(b"StreamTitle='Mary Gauthier - Mercy Now';", sd)
    assert sd.data is not None
    sd.data["block_data"] = _block()

    await provider._update_stream_metadata(sd, 0)

    assert sd.stream_metadata is not None
    assert sd.stream_metadata.title == "Mercy Now"
    assert sd.stream_metadata.artist == "Mary Gauthier"


async def test_unmatched_title_then_match_restarts_artist_view(
    provider: RadioParadiseProvider,
) -> None:
    """A station-break title followed by a match resets the upcoming alternation."""
    sd = _stream_details(opt_in=True)
    assert sd.data is not None
    sd.data["block_data"] = _block()
    provider._fetch_json = AsyncMock(return_value=_block())  # type: ignore[method-assign]

    sd.data[STREAMDETAILS_INBAND_TITLE_KEY] = "Station break"
    await provider._update_stream_metadata(sd, 0)
    assert sd.stream_metadata is not None
    assert sd.stream_metadata.title == "Station break"

    sd.data[STREAMDETAILS_INBAND_TITLE_KEY] = "Mark Ambor - Good to Be"
    await provider._update_stream_metadata(sd, 10)

    assert sd.stream_metadata is not None
    assert sd.stream_metadata.title == "Good to Be"
    assert sd.stream_metadata.artist == "Mark Ambor"
    assert sd.data is not None
    assert "last_verbatim_title" not in sd.data
    assert sd.data["show_upcoming"] is True
