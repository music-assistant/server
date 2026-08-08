"""Tests for how the builtin provider decides between a track and a radio station."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import Radio, Track

from music_assistant.helpers.tags import AudioTags
from music_assistant.providers.builtin import BuiltinProvider

STREAM_URL = "http://stream.example.com/live"


def _make_provider(icyname: str | None, duration: float | None) -> BuiltinProvider:
    """Create a provider whose stream probe reports the given ICY name and duration."""
    tags = AudioTags(
        raw={},
        sample_rate=44100,
        channels=2,
        bits_per_sample=16,
        format="mp3",
        bit_rate=320,
        duration=duration,
        tags={"icyname": icyname} if icyname else {},
        has_cover_image=False,
        filename=STREAM_URL,
    )
    prov = object.__new__(BuiltinProvider)
    prov.mass = MagicMock()
    prov.mass.config.get.side_effect = lambda _key, default=None: default
    prov.logger = MagicMock()
    prov.manifest = MagicMock()
    prov.manifest.domain = "builtin"
    prov.config = MagicMock()
    prov.config.instance_id = "builtin_1"
    prov._get_media_info = AsyncMock(return_value=tags)  # type: ignore[method-assign]
    return prov


async def test_requested_track_stays_a_track_with_an_icy_name() -> None:
    """A track added by URL is not turned into a radio station by its stream."""
    prov = _make_provider(icyname="SOME RADIO", duration=None)

    result = await prov.parse_item(STREAM_URL, requested_media_type=MediaType.TRACK)

    assert isinstance(result, Track)


async def test_unknown_media_type_still_probes_and_decides() -> None:
    """A plain URL of unknown media type is classified from what the stream reports."""
    prov = _make_provider(icyname="SOME RADIO", duration=None)

    result = await prov.parse_item(STREAM_URL, requested_media_type=MediaType.UNKNOWN)

    assert isinstance(result, Radio)


async def test_get_track_returns_a_track_for_a_stream_without_duration() -> None:
    """get_track never hands back a radio station for a stream that reports no duration."""
    prov = _make_provider(icyname=None, duration=None)

    assert isinstance(await prov.get_track(STREAM_URL), Track)
