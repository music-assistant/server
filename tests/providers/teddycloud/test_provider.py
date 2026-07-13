"""Tests for the TeddyCloud music provider."""

from __future__ import annotations

from typing import Any, Self
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.providers.teddycloud import SUPPORTED_FEATURES
from music_assistant.providers.teddycloud.constants import TAF_HEADER_SIZE
from music_assistant.providers.teddycloud.provider import (
    TeddyCloudProvider,
    _parse_taf_header,
    _read_varint,
)


def _encode_varint(value: int) -> bytes:
    """Encode an int as a protobuf base-128 varint."""
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _make_taf_header(num_bytes: int, page_nums: list[int]) -> bytes:
    """
    Build a synthetic 4096-byte TAF header.

    Encodes field 2 (num_bytes, varint) and field 4 (track_page_nums, packed varints),
    prefixed by the 4-byte big-endian protobuf length and zero-padded to the header size.
    """
    proto = bytearray()
    proto += b"\x10" + _encode_varint(num_bytes)  # field 2, wire type 0 (varint)
    packed = b"".join(_encode_varint(p) for p in page_nums)
    proto += b"\x22" + _encode_varint(len(packed)) + packed  # field 4, wire type 2 (packed)
    header = len(proto).to_bytes(4, "big") + bytes(proto)
    return header.ljust(TAF_HEADER_SIZE, b"\x00")


class _FakeResponse:
    """Minimal async-context-manager stand-in for an aiohttp response."""

    def __init__(self, data: bytes = b"", json_payload: Any = None) -> None:
        self._data = data
        self._json = json_payload

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    def raise_for_status(self) -> None:
        """No-op: the fake response is always OK."""

    async def json(self, content_type: str | None = None) -> Any:
        return self._json

    @property
    def content(self) -> Mock:
        reader = Mock()
        reader.readexactly = AsyncMock(return_value=self._data)
        return reader


@pytest.fixture
def mass_mock() -> Mock:
    """Return a mock MusicAssistant instance."""
    mass = Mock()
    mass.http_session = Mock()
    mass.http_session_no_ssl = Mock()
    return mass


@pytest.fixture
def manifest_mock() -> Mock:
    """Return a mock provider manifest."""
    manifest = Mock()
    manifest.domain = "teddycloud"
    return manifest


@pytest.fixture
def config_mock() -> Mock:
    """Return a mock provider config."""
    config = Mock()
    config.name = "TeddyCloud Test"
    config.instance_id = "teddycloud_test"
    config.enabled = True
    # the base provider reads the log level from config on init; return a valid level.
    config.get_value.side_effect = lambda key, default=None: (
        "INFO" if key == "log_level" else default
    )
    return config


@pytest.fixture
def provider(mass_mock: Mock, manifest_mock: Mock, config_mock: Mock) -> TeddyCloudProvider:
    """Return a TeddyCloudProvider with the state handle_async_init would set (no network)."""
    prov = TeddyCloudProvider(mass_mock, manifest_mock, config_mock, SUPPORTED_FEATURES)
    prov.base_url = "http://teddycloud.local"
    prov.verify_ssl = True
    prov._auth = None
    prov._tags = {}
    prov._tags_fetched_at = 0.0
    prov._durations = {}
    return prov


def _tag(**overrides: Any) -> dict[str, Any]:
    """Return a representative TeddyCloud tag dict, with optional overrides."""
    tag: dict[str, Any] = {
        "uid": "0011223344556677",
        "tonieInfo": {
            "series": "Willow Hollow Tales",
            "episode": "The Copper Lantern",
            "tracks": ["Story One", "Story Two"],
            "picture": "https://example.com/pic.png",
            "language": "de-de",
        },
        "trackSeconds": [0, 180, 360, 540],
        "source": "/library/by/audioID/1600000000.taf",
        "audioUrl": "/content/download/000000/0011223344556677?skip_header=true",
    }
    tag.update(overrides)
    return tag


# --- protobuf / TAF header parsing -------------------------------------------------- #


def test_read_varint_roundtrip() -> None:
    """Encoded varints decode back to their original values."""
    for value in (0, 1, 127, 128, 300, 16384, 1_615_475_317):
        decoded, pos = _read_varint(_encode_varint(value), 0)
        assert decoded == value
        assert pos == len(_encode_varint(value))


def test_parse_taf_header_reads_num_bytes_and_last_page() -> None:
    """The header parser returns num_bytes (field 2) and the last packed page (field 4)."""
    header = _make_taf_header(num_bytes=8_192_000, page_nums=[0, 250, 500, 1000])
    num_bytes, last_page = _parse_taf_header(header)
    assert num_bytes == 8_192_000
    assert last_page == 1000


def test_parse_taf_header_too_short() -> None:
    """A truncated header yields (None, None) rather than raising."""
    assert _parse_taf_header(b"\x00\x00") == (None, None)


# --- URL handling ------------------------------------------------------------------- #


def test_content_url_strips_skip_header(provider: TeddyCloudProvider) -> None:
    """_content_url yields the RAW TAF (skip_header removed) so the header can be read."""
    url = provider._content_url(_tag())
    assert "skip_header" not in url
    assert url == "http://teddycloud.local/content/download/000000/0011223344556677"


def test_stream_url_adds_skip_header(provider: TeddyCloudProvider) -> None:
    """_stream_url serves clean OGG/Opus by forcing skip_header=true."""
    url = provider._stream_url(_tag())
    assert url.endswith("skip_header=true")
    assert url.count("skip_header=true") == 1


def test_content_url_from_source_without_audiourl(provider: TeddyCloudProvider) -> None:
    """When audioUrl is absent, the content URL is built from the source path."""
    tag = _tag(audioUrl=None, source="/library/by/audioID/1600000000.taf")
    url = provider._content_url(tag)
    assert url == ("http://teddycloud.local/content/download/library/by/audioID/1600000000.taf")


# --- chapter alignment -------------------------------------------------------------- #


def test_chapters_one_to_one(provider: TeddyCloudProvider) -> None:
    """Equal mark/name counts produce one named chapter per mark."""
    tag = _tag(trackSeconds=[0, 180], tonieInfo={"tracks": ["Story One", "Story Two"]})
    chapters = provider._chapters(tag, total_seconds=300)
    assert [c.name for c in chapters] == ["Story One", "Story Two"]
    assert [c.start for c in chapters] == [0.0, 180.0]
    assert chapters[-1].end == 300.0


def test_chapters_collapse_to_stories(provider: TeddyCloudProvider) -> None:
    """When marks are an exact multiple of names, collapse to evenly-grouped stories."""
    tag = _tag(trackSeconds=[0, 180, 360, 540], tonieInfo={"tracks": ["Story One", "Story Two"]})
    chapters = provider._chapters(tag, total_seconds=720)
    assert [c.name for c in chapters] == ["Story One", "Story Two"]
    assert [c.start for c in chapters] == [0.0, 360.0]  # group size 2 -> marks 0 and 2
    assert chapters[-1].end == 720.0


def test_chapters_generic_when_unaligned(provider: TeddyCloudProvider) -> None:
    """A non-divisible mismatch keeps every mark with generic names (no guessed labels)."""
    tag = _tag(trackSeconds=[0, 180, 360], tonieInfo={"tracks": ["Story One", "Story Two"]})
    chapters = provider._chapters(tag, total_seconds=None)
    assert [c.name for c in chapters] == ["Chapter 1", "Chapter 2", "Chapter 3"]
    assert chapters[-1].end is None  # no total -> last chapter unbounded


# --- audiobook parsing -------------------------------------------------------------- #


def test_parse_audiobook_maps_fields(provider: TeddyCloudProvider) -> None:
    """A tag maps to an Audiobook with title, series, chapters, duration and date_added."""
    book = provider._parse_audiobook(_tag(), total_seconds=720)
    assert book.item_id == "0011223344556677"
    assert book.name == "The Copper Lantern"
    assert book.publisher == "Willow Hollow Tales"
    assert list(book.authors) == ["Willow Hollow Tales"]
    assert book.duration == 720
    chapters = book.metadata.chapters
    assert chapters is not None
    assert len(chapters) == 2
    # audio_id 1600000000 is a unix timestamp -> becomes date_added
    assert book.date_added is not None


def test_parse_audiobook_custom_tonie_has_no_publisher(provider: TeddyCloudProvider) -> None:
    """A tag without a recognised series falls back to the default author, no publisher."""
    tag = _tag(tonieInfo={"tracks": ["Story One"]}, trackSeconds=[0])
    book = provider._parse_audiobook(tag, total_seconds=60)
    assert book.publisher is None
    assert list(book.authors) == ["TeddyCloud"]


# --- duration derivation ------------------------------------------------------------ #


async def test_total_seconds_from_header(
    provider: TeddyCloudProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Duration is derived from the TAF byte-rate and cached per uid."""
    # byte-rate: 8_192_000 bytes over page 1000 (=1000*4096 bytes) at t=540 -> total 1080
    header = _make_taf_header(num_bytes=8_192_000, page_nums=[0, 1000])
    monkeypatch.setattr(provider.mass.http_session, "get", Mock(return_value=_FakeResponse(header)))
    tag = _tag(trackSeconds=[0, 540])

    total = await provider._total_seconds(tag)

    assert total == 1080
    assert provider._durations[tag["uid"]] == 1080

    # a second call is served from cache (no extra HTTP call)
    monkeypatch.setattr(
        provider.mass.http_session,
        "get",
        Mock(side_effect=AssertionError("should be cached")),
    )
    assert await provider._total_seconds(tag) == 1080


async def test_total_seconds_rejects_impossible_result(
    provider: TeddyCloudProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A derived total shorter than the last chapter start is discarded."""
    header = _make_taf_header(num_bytes=1000, page_nums=[0, 1000])  # tiny file, big page
    monkeypatch.setattr(provider.mass.http_session, "get", Mock(return_value=_FakeResponse(header)))
    tag = _tag(trackSeconds=[0, 540])
    assert await provider._total_seconds(tag) is None


# --- streaming & misc --------------------------------------------------------------- #


def test_is_streaming_provider_is_false(provider: TeddyCloudProvider) -> None:
    """TeddyCloud is a self-hosted library: catalog equals contents."""
    assert provider.is_streaming_provider is False


async def test_get_stream_details(provider: TeddyCloudProvider) -> None:
    """Stream details report seekable OGG/Opus over HTTP with skip_header applied."""
    provider._get_tag = AsyncMock(return_value=_tag())  # type: ignore[method-assign]
    details = await provider.get_stream_details("0011223344556677", MediaType.AUDIOBOOK)
    assert isinstance(details, StreamDetails)
    assert details.stream_type == StreamType.HTTP
    assert details.audio_format.content_type == ContentType.OGG
    assert details.media_type == MediaType.AUDIOBOOK
    assert details.can_seek is True
    assert isinstance(details.path, str)
    assert details.path.endswith("skip_header=true")


async def test_get_library_audiobooks(provider: TeddyCloudProvider) -> None:
    """The library generator yields one Audiobook per playable tag."""
    provider._fetch_tags = AsyncMock(return_value={"0011223344556677": _tag()})  # type: ignore[method-assign]
    provider._total_seconds = AsyncMock(return_value=720)  # type: ignore[method-assign]
    books = [book async for book in provider.get_library_audiobooks()]
    assert len(books) == 1
    assert books[0].name == "The Copper Lantern"


async def test_fetch_tags_normalizes_ruid_only(
    provider: TeddyCloudProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tag with only 'ruid' (no 'uid') is normalized so downstream tag['uid'] is safe."""
    payload = {
        "tags": [
            {
                "ruid": "AABBCCDD00112233",
                "trackSeconds": [0],
                "source": "/library/by/audioID/1600000000.taf",
                "audioUrl": "/content/download/000000/AABBCCDD00112233",
            }
        ]
    }
    monkeypatch.setattr(
        provider.mass.http_session, "get", Mock(return_value=_FakeResponse(json_payload=payload))
    )

    tags = await provider._fetch_tags(force=True)

    assert "AABBCCDD00112233" in tags
    assert tags["AABBCCDD00112233"]["uid"] == "AABBCCDD00112233"  # normalized from ruid
    # parsing must not raise KeyError on tag["uid"]
    book = provider._parse_audiobook(tags["AABBCCDD00112233"], total_seconds=None)
    assert book.item_id == "AABBCCDD00112233"


async def test_handle_async_init_prepends_scheme(
    mass_mock: Mock, manifest_mock: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A server entered as a bare host/IP is normalized to an http:// base URL."""
    config = Mock()
    config.instance_id = "teddycloud_test"
    values: dict[str, Any] = {"url": "192.168.3.225", "log_level": "INFO", "verify_ssl": True}
    config.get_value.side_effect = lambda key, default=None: values.get(key, default)
    prov = TeddyCloudProvider(mass_mock, manifest_mock, config, SUPPORTED_FEATURES)
    monkeypatch.setattr(
        mass_mock.http_session, "get", Mock(return_value=_FakeResponse(json_payload={"tags": []}))
    )

    await prov.handle_async_init()

    assert prov.base_url == "http://192.168.3.225"
