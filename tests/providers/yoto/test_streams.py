"""Stream resolution tests for the Yoto provider."""
# ruff: noqa: D101, D102, D103, D107, N815

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import pytest
from aiohttp import web
from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.streamdetails import MultiPartPath

from music_assistant.providers.yoto.catalogue import (
    Catalogue,
    CatalogueCard,
    CatalogueTrack,
    encode_track_id,
)
from music_assistant.providers.yoto.client import YotoAdapter
from music_assistant.providers.yoto.provider import YotoProvider


@dataclass
class FakeTrack:
    key: str
    title: str = "Moon Story"
    duration: int = 42
    format: str = "aac"
    channels: str = "stereo"
    trackUrl: str | None = None
    icon: str | None = None
    type: str = "audio"


@dataclass
class FakeChapter:
    key: str
    tracks: dict[str, FakeTrack] = field(default_factory=dict)


@dataclass
class FakeCard:
    id: str
    chapters: dict[str, FakeChapter] = field(default_factory=dict)


@dataclass
class FakeToken:
    refresh_token: str = "fixture-refresh"


class FakeStreamAPI:
    def __init__(self) -> None:
        self.token = FakeToken()
        self.library: dict[str, Any] = {
            "card-alpha": FakeCard(
                "card-alpha",
                {
                    "chapter-a": FakeChapter(
                        "chapter-a",
                        {
                            "track-a": FakeTrack("track-a", duration=42),
                            "track-b": FakeTrack("track-b", title="Moon Ending", duration=18),
                        },
                    )
                },
            )
        }
        self.groups: dict[str, Any] = {}
        self.detail_calls = 0

    def set_refresh_token(self, refresh_token: str) -> None:
        self.token = FakeToken(refresh_token)

    async def check_and_refresh_token(self) -> FakeToken:
        return self.token

    async def update_card_detail(self, card_id: str) -> None:
        self.detail_calls += 1
        for track in self.library[card_id].chapters["chapter-a"].tracks.values():
            track.trackUrl = (
                f"https://secure-media.example/{track.key}.m4a?"
                f"signature=fixture-{self.detail_calls}"
            )

    async def update_library(self) -> None:
        pass

    async def update_groups(self) -> None:
        pass


def _provider(adapter: YotoAdapter, item_id: str, *, category: str | None = None) -> YotoProvider:
    provider = object.__new__(YotoProvider)
    cast("Any", provider).config = SimpleNamespace(instance_id="yoto-instance")
    cast("Any", provider).mass = SimpleNamespace(
        streams=SimpleNamespace(base_url="http://music-assistant.example")
    )
    provider.adapter = adapter
    provider.catalogue = Catalogue(
        cards={
            "card-alpha": CatalogueCard(
                item_id="card-alpha",
                title="Moshi Moon",
                tracks=(
                    CatalogueTrack(
                        item_id=item_id,
                        card_id="card-alpha",
                        chapter_key="chapter-a",
                        track_key="track-a",
                        title="Moon Story",
                        chapter_title="Moon Chapter",
                        duration=42,
                        chapter_number=1,
                        track_number=1,
                        format="aac",
                        channels="stereo",
                    ),
                    CatalogueTrack(
                        item_id=encode_track_id("card-alpha", "chapter-a", "track-b"),
                        card_id="card-alpha",
                        chapter_key="chapter-a",
                        track_key="track-b",
                        title="Moon Ending",
                        chapter_title="Moon Chapter",
                        duration=18,
                        chapter_number=1,
                        track_number=2,
                        format="aac",
                        channels="stereo",
                    ),
                ),
                category=category,
            )
        }
    )
    return provider


@pytest.mark.asyncio
async def test_stream_resolution_refetches_each_time_and_returns_http_aac_details() -> None:
    api = FakeStreamAPI()
    adapter = YotoAdapter("fixture-client", "fixture-refresh", api=api)
    item_id = encode_track_id("card-alpha", "chapter-a", "track-a")
    provider = _provider(adapter, item_id)

    first = await provider.get_stream_details(item_id, MediaType.TRACK)
    second = await provider.get_stream_details(item_id, MediaType.TRACK)

    assert api.detail_calls == 2
    assert first.stream_type is StreamType.HTTP
    assert first.audio_format.content_type is ContentType.AAC
    assert first.duration == 42
    assert first.path != second.path
    assert "signature=fixture-1" in str(first.path)
    assert "signature=fixture-2" in str(second.path)


@pytest.mark.asyncio
async def test_signed_stream_is_not_added_to_catalogue_metadata_or_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    api = FakeStreamAPI()
    adapter = YotoAdapter("fixture-client", "fixture-refresh", api=api)
    item_id = encode_track_id("card-alpha", "chapter-a", "track-a")
    provider = _provider(adapter, item_id)

    with caplog.at_level(logging.DEBUG):
        details = await provider.get_stream_details(item_id, MediaType.TRACK)

    assert details.path
    assert "secure-media" not in repr(provider.catalogue)
    assert "signature=" not in caplog.text
    assert "secure-media" not in caplog.text


@pytest.mark.asyncio
async def test_audiobook_stream_uses_fresh_per_part_redirects_with_seekable_combined_timeline() -> (
    None
):
    api = FakeStreamAPI()
    adapter = YotoAdapter("fixture-client", "fixture-refresh", api=api)
    item_id = encode_track_id("card-alpha", "chapter-a", "track-a")
    provider = _provider(adapter, item_id, category="stories")

    details = await provider.get_stream_details("card-alpha", MediaType.AUDIOBOOK)

    assert api.detail_calls == 0
    assert details.media_type is MediaType.AUDIOBOOK
    assert details.stream_type is StreamType.HTTP
    assert details.duration == 60
    assert details.allow_seek
    assert details.can_seek
    assert isinstance(details.path, list)
    assert all(isinstance(part, MultiPartPath) for part in details.path)
    assert [part.duration for part in details.path] == [42, 18]
    assert all("secure-media" not in part.path for part in details.path)
    part_queries = [parse_qs(urlsplit(part.path).query) for part in details.path]
    session_id = part_queries[0]["session_id"][0]
    assert len(session_id) >= 32
    assert [query["session_id"] for query in part_queries] == [[session_id], [session_id]]
    assert [query["part"] for query in part_queries] == [["0"], ["1"]]
    assert all("item_id" not in query for query in part_queries)

    request = cast("web.Request", SimpleNamespace(query={"session_id": session_id, "part": "0"}))
    with pytest.raises(web.HTTPFound) as first_redirect:
        await provider._handle_audiobook_part_request(request)
    with pytest.raises(web.HTTPFound) as second_redirect:
        await provider._handle_audiobook_part_request(request)

    assert api.detail_calls == 2
    assert "signature=fixture-1" in str(first_redirect.value.location)
    assert "signature=fixture-2" in str(second_redirect.value.location)


@pytest.mark.asyncio
async def test_audiobook_stream_rejects_mixed_codecs_before_playback() -> None:
    api = FakeStreamAPI()
    adapter = YotoAdapter("fixture-client", "fixture-refresh", api=api)
    item_id = encode_track_id("card-alpha", "chapter-a", "track-a")
    provider = _provider(adapter, item_id, category="stories")
    mixed_track = provider.catalogue.cards["card-alpha"].tracks[1]
    object.__setattr__(mixed_track, "format", "mp3")

    with pytest.raises(MediaNotFoundError, match="incompatible audio properties"):
        await provider.get_stream_details("card-alpha", MediaType.AUDIOBOOK)

    assert api.detail_calls == 0


@pytest.mark.asyncio
async def test_stream_resolution_rejects_wrong_type_missing_track_and_missing_url() -> None:
    api = FakeStreamAPI()
    adapter = YotoAdapter("fixture-client", "fixture-refresh", api=api)
    item_id = encode_track_id("card-alpha", "chapter-a", "track-a")
    provider = _provider(adapter, item_id)

    with pytest.raises(MediaNotFoundError):
        await provider.get_stream_details(item_id, MediaType.ALBUM)
    with pytest.raises(MediaNotFoundError):
        await provider.get_stream_details("not-a-track", MediaType.TRACK)

    async def no_url(_card_id: str) -> None:
        api.detail_calls += 1

    cast("Any", api).update_card_detail = no_url
    api.library["card-alpha"].chapters["chapter-a"].tracks["track-a"].trackUrl = None
    with pytest.raises(MediaNotFoundError, match="stream is unavailable"):
        await provider.get_stream_details(item_id, MediaType.TRACK)


@pytest.mark.asyncio
async def test_audiobook_redirect_never_reuses_a_stale_signed_url() -> None:
    api = FakeStreamAPI()
    adapter = YotoAdapter("fixture-client", "fixture-refresh", api=api)
    item_id = encode_track_id("card-alpha", "chapter-a", "track-a")
    provider = _provider(adapter, item_id, category="stories")
    track = api.library["card-alpha"].chapters["chapter-a"].tracks["track-a"]
    track.trackUrl = "https://secure-media.example/stale.m4a?signature=stale"

    async def detail_refresh_without_track_update(_card_id: str) -> None:
        api.detail_calls += 1

    cast("Any", api).update_card_detail = detail_refresh_without_track_update

    details = await provider.get_stream_details("card-alpha", MediaType.AUDIOBOOK)
    assert isinstance(details.path, list)
    query = parse_qs(urlsplit(details.path[0].path).query)
    request = SimpleNamespace(
        query={"session_id": query["session_id"][0], "part": query["part"][0]}
    )
    with pytest.raises(web.HTTPNotFound):
        await provider._handle_audiobook_part_request(cast("web.Request", request))

    assert api.detail_calls == 1
    assert track.trackUrl is None


@pytest.mark.asyncio
async def test_audiobook_part_session_rejects_expiry_and_invalid_part() -> None:
    api = FakeStreamAPI()
    adapter = YotoAdapter("fixture-client", "fixture-refresh", api=api)
    item_id = encode_track_id("card-alpha", "chapter-a", "track-a")
    provider = _provider(adapter, item_id, category="stories")
    details = await provider.get_stream_details("card-alpha", MediaType.AUDIOBOOK)
    assert isinstance(details.path, list)
    query = parse_qs(urlsplit(details.path[0].path).query)
    session_id = query["session_id"][0]

    with pytest.raises(web.HTTPNotFound):
        await provider._handle_audiobook_part_request(
            cast("web.Request", SimpleNamespace(query={"session_id": session_id, "part": "99"}))
        )

    provider._audiobook_sessions[session_id].card_id = "different-card"
    with pytest.raises(web.HTTPNotFound):
        await provider._handle_audiobook_part_request(
            cast("web.Request", SimpleNamespace(query={"session_id": session_id, "part": "0"}))
        )
    provider._audiobook_sessions[session_id].card_id = "card-alpha"

    provider._audiobook_sessions[session_id].expires_at = 0
    with pytest.raises(web.HTTPGone):
        await provider._handle_audiobook_part_request(
            cast("web.Request", SimpleNamespace(query={"session_id": session_id, "part": "0"}))
        )

    assert session_id not in provider._audiobook_sessions
    assert api.detail_calls == 0
