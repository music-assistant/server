"""Tests for the AcoustidLookupProvider."""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from music_assistant_models.enums import MediaType, StreamType
from music_assistant_models.errors import UnsupportedSystemError

from music_assistant.constants import CONF_LOG_LEVEL
from music_assistant.helpers.datetime import utc_timestamp
from music_assistant.models.audio_analysis import AudioAnalysisData
from music_assistant.models.audio_analysis_provider import AnalysisSessionData
from music_assistant.providers.acoustid_lookup.provider import (
    CONF_ANALYSE_STREAMING,
    CONF_API_KEY,
    CONF_MIN_SCORE,
    CONF_WRITE_TAGS_BACK,
    NO_MATCH_ANALYSIS_VERSION,
    NO_MATCH_RETRY_DAYS,
    AcoustidLookupProvider,
    _AcoustidSessionData,
    _parse_response,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_provider(
    *,
    api_key: str | None = "TESTKEY",
    min_score: float = 0.85,
    write_tags_back: bool = False,
    analyse_streaming: bool = True,
) -> AcoustidLookupProvider:
    """Build a provider with a mocked MusicAssistant infrastructure."""
    mass = MagicMock()

    def _default_get_provider(provider_id: Any, **_kwargs: Any) -> Any:
        # Source-provider lookup feeds the write_access gate in post_analysis; default to
        # a writeable filesystem-like source so tag-write paths are exercised by default.
        if provider_id == "filesystem_local_test":
            return MagicMock(write_access=True)
        return None

    mass.get_provider = MagicMock(side_effect=_default_get_provider)
    mass.streams.audio_analysis.get_audio_analysis_version = AsyncMock(return_value=None)
    mass.streams.audio_analysis.get_audio_analysis = AsyncMock(return_value=None)
    mass.streams.audio_analysis.set_audio_analysis = AsyncMock()
    mass.music.tracks.set_identifiers = AsyncMock()
    mass.music.albums.set_release_group = AsyncMock()
    mass.streams.audio_analysis.get_extra_data_for_album_tracks = AsyncMock(return_value=[])
    mass.music.tracks.get_library_item_by_prov_id = AsyncMock(return_value=None)
    mass.music.albums.get_library_item = AsyncMock(return_value=None)
    mass.music.albums.get_library_album_tracks = AsyncMock(return_value=[])
    mass.cache.get = AsyncMock(return_value=None)
    mass.cache.set = AsyncMock()
    mass.http_session = MagicMock()

    manifest = MagicMock()
    manifest.domain = "acoustid_lookup"

    config = MagicMock()
    config.instance_id = "acoustid_lookup_test"
    config.values = {}
    config_lookup: dict[str, Any] = {
        CONF_LOG_LEVEL: "GLOBAL",
        CONF_API_KEY: api_key,
        CONF_MIN_SCORE: min_score,
        CONF_WRITE_TAGS_BACK: write_tags_back,
        CONF_ANALYSE_STREAMING: analyse_streaming,
    }
    config.get_value = MagicMock(side_effect=lambda key: config_lookup.get(key, "GLOBAL"))

    return AcoustidLookupProvider(mass, manifest, config, set())


def _make_streamdetails(
    *,
    stream_type: StreamType = StreamType.LOCAL_FILE,
    media_type: MediaType = MediaType.TRACK,
    path: str | None = "/music/track.flac",
    duration: int | None = 180,
) -> MagicMock:
    sd = MagicMock()
    sd.item_id = "track-1"
    sd.provider = "filesystem_local_test"
    sd.uri = "library://track/track-1"
    sd.media_type = media_type
    sd.stream_type = stream_type
    sd.path = path
    sd.duration = duration
    return sd


def _make_audio_format(
    *, bit_depth: int = 16, sample_rate: int = 44100, channels: int = 2
) -> MagicMock:
    fmt = MagicMock()
    fmt.bit_depth = bit_depth
    fmt.sample_rate = sample_rate
    fmt.channels = channels
    return fmt


class _FakeFingerprinter:
    """Stand-in for acoustid.chromaprint.Fingerprinter."""

    def __init__(self) -> None:
        self.start_args: tuple[int, int] | None = None
        self.fed: list[bytes] = []
        self.finished = False

    def start(self, sample_rate: int, channels: int) -> None:
        self.start_args = (sample_rate, channels)

    def feed(self, data: bytes) -> None:
        self.fed.append(bytes(data))

    def finish(self) -> bytes:
        self.finished = True
        return b"FAKEFINGERPRINT"


def _install_fake_chromaprint(monkeypatch: pytest.MonkeyPatch, fp: _FakeFingerprinter) -> None:
    """Patch _create_fingerprinter to return the given fake, started with the session's format."""

    def fake_create(
        _self: AcoustidLookupProvider, sample_rate: int, channels: int
    ) -> _FakeFingerprinter:
        fp.start(int(sample_rate), int(channels))
        return fp

    monkeypatch.setattr(AcoustidLookupProvider, "_create_fingerprinter", fake_create)


def _install_unidentified_track(provider: AcoustidLookupProvider) -> None:
    """Make the provider's library lookup return a track with no MBID or ISRC."""
    track = MagicMock(mbid=None)
    track.get_external_id.return_value = None
    cast("MagicMock", provider.mass.music.tracks).get_library_item_by_prov_id = AsyncMock(
        return_value=track
    )


class _FakeFingerprintError(Exception):
    """Stand-in for chromaprint.FingerprintError."""


def _install_chromaprint_module(monkeypatch: pytest.MonkeyPatch, *, failing_call: str) -> None:
    """
    Inject a stand-in ``chromaprint`` module so the real _create_fingerprinter runs.

    :param failing_call: Fingerprinter method ("start", "feed" or "finish") that
        raises, letting a test drive one native error path.
    """

    class _Fingerprinter:
        def start(self, sample_rate: int, channels: int) -> None:
            if failing_call == "start":
                raise _FakeFingerprintError("start failed")

        def feed(self, data: bytes) -> None:
            if failing_call == "feed":
                raise _FakeFingerprintError("feed failed")

        def finish(self) -> bytes:
            if failing_call == "finish":
                raise _FakeFingerprintError("finish failed")
            return b"FAKEFINGERPRINT"

    module = ModuleType("chromaprint")
    module.FingerprintError = _FakeFingerprintError  # type: ignore[attr-defined]
    module.Fingerprinter = _Fingerprinter  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "chromaprint", module)


def _install_lookup_response(
    monkeypatch: pytest.MonkeyPatch, response: dict[str, Any] | None
) -> None:
    """Patch the cached/throttled _lookup to return a fixed dict (or None)."""

    async def fake_lookup(
        _self: AcoustidLookupProvider, *_args: Any, **_kwargs: Any
    ) -> dict[str, Any] | None:
        return response

    monkeypatch.setattr(AcoustidLookupProvider, "_lookup", fake_lookup)


def _make_library_album(
    *,
    name: str = "Silver Thunderbird",
    existing_rg: str | None = None,
) -> MagicMock:
    """Build a library Album mock with get/add_external_id wiring."""
    album = MagicMock()
    album.name = name
    album.item_id = "42"
    storage: dict[str, str] = {}
    if existing_rg:
        storage["musicbrainz_releasegroupid"] = existing_rg

    def get_ext(key: Any) -> str | None:
        return storage.get(getattr(key, "value", key))

    def add_ext(key: Any, value: str) -> None:
        storage[getattr(key, "value", key)] = value

    album.get_external_id.side_effect = get_ext
    album.add_external_id.side_effect = add_ext
    return album


def _make_album_tracks(count: int, provider_instance: str = "filesystem_local_test") -> list[Any]:
    """Build N Track mocks each with one provider mapping in our provider."""
    tracks = []
    for i in range(count):
        pm = MagicMock()
        pm.provider_instance = provider_instance
        pm.provider_domain = "filesystem_local"
        pm.item_id = f"native-{i}"
        t = MagicMock()
        t.provider_mappings = [pm]
        tracks.append(t)
    return tracks


def _wire_album_for_consensus(
    provider: AcoustidLookupProvider,
    *,
    album: MagicMock,
    album_tracks: list[Any],
    extras: list[dict[str, Any]],
) -> None:
    """Glue an album + tracks + AcoustID extras into the provider's mass mock."""
    library_track = MagicMock()
    library_track.album = MagicMock(item_id="42")
    cast("MagicMock", provider.mass.music.tracks).get_library_item_by_prov_id = AsyncMock(
        return_value=library_track
    )
    cast("MagicMock", provider.mass.music.albums).get_library_item = AsyncMock(return_value=album)
    cast("MagicMock", provider.mass.music.albums).get_library_album_tracks = AsyncMock(
        return_value=album_tracks
    )
    cast(
        "MagicMock", provider.mass.streams.audio_analysis
    ).get_extra_data_for_album_tracks = AsyncMock(return_value=extras)


def _rg(
    rg_id: str, title: str = "Silver Thunderbird", primary_type: str = "Album"
) -> dict[str, Any]:
    """Shortcut for a release-group entry inside ``extras``."""
    return {"id": rg_id, "title": title, "primary_type": primary_type}


# ---------------------------------------------------------------------------
# Provider core
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "provider_kwargs",
        "streamdetails_kwargs",
        "track_mbid",
        "track_isrc",
        "track_in_library",
        "expected",
    ),
    [
        pytest.param({}, {}, "0000-mbid", None, True, False, id="mbid_already_present"),
        pytest.param({}, {}, None, "USRC17607839", True, False, id="isrc_already_present"),
        pytest.param({}, {}, None, None, False, False, id="no_library_row"),
        pytest.param(
            {},
            {"media_type": MediaType.PODCAST_EPISODE},
            None,
            None,
            False,
            False,
            id="non_track_media",
        ),
        pytest.param(
            {"analyse_streaming": False},
            {"stream_type": StreamType.HTTP},
            None,
            None,
            True,
            False,
            id="streaming_toggle_off",
        ),
        pytest.param(
            {"analyse_streaming": True},
            {"stream_type": StreamType.HTTP},
            None,
            None,
            True,
            True,
            id="streaming_toggle_on",
        ),
        pytest.param({}, {}, None, None, True, True, id="local_file_mbid_missing"),
        # No user-supplied key still proceeds — the shared AcoustID key is used.
        pytest.param({"api_key": None}, {}, None, None, True, True, id="no_user_api_key"),
    ],
)
async def test_start_analysis_gates(
    monkeypatch: pytest.MonkeyPatch,
    *,
    provider_kwargs: dict[str, Any],
    streamdetails_kwargs: dict[str, Any],
    track_mbid: str | None,
    track_isrc: str | None,
    track_in_library: bool,
    expected: bool,
) -> None:
    """start_analysis accepts the session or skips for each precondition."""
    provider = _make_provider(**provider_kwargs)
    if track_in_library:
        track = MagicMock()
        track.mbid = track_mbid
        track.get_external_id.return_value = track_isrc
        cast("MagicMock", provider.mass.music.tracks).get_library_item_by_prov_id = AsyncMock(
            return_value=track
        )
    _install_fake_chromaprint(monkeypatch, _FakeFingerprinter())

    accepted = await provider.start_analysis(
        "session", _make_streamdetails(**streamdetails_kwargs), _make_audio_format()
    )

    assert accepted is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("track_mbid", "track_isrc", "expected_extra"),
    [
        pytest.param(
            "0000-mbid", None, {"source": "existing_tags", "mbid": "0000-mbid"}, id="mbid_only"
        ),
        pytest.param(
            None,
            "USRC17607839",
            {"source": "existing_tags", "isrc": "USRC17607839"},
            id="isrc_only",
        ),
        pytest.param(
            "0000-mbid",
            "USRC17607839",
            {"source": "existing_tags", "mbid": "0000-mbid", "isrc": "USRC17607839"},
            id="mbid_and_isrc",
        ),
    ],
)
async def test_start_analysis_records_existing_identifiers(
    monkeypatch: pytest.MonkeyPatch,
    *,
    track_mbid: str | None,
    track_isrc: str | None,
    expected_extra: dict[str, Any],
) -> None:
    """An already-identified track is recorded as analyzed (no fingerprinting, no retry)."""
    provider = _make_provider()
    track = MagicMock()
    track.mbid = track_mbid
    track.get_external_id.return_value = track_isrc
    cast("MagicMock", provider.mass.music.tracks).get_library_item_by_prov_id = AsyncMock(
        return_value=track
    )
    _install_fake_chromaprint(monkeypatch, _FakeFingerprinter())
    set_aa = cast("AsyncMock", provider.mass.streams.audio_analysis.set_audio_analysis)

    accepted = await provider.start_analysis("session", _make_streamdetails(), _make_audio_format())

    # session is declined (no streaming) but a result row is recorded
    assert accepted is False
    set_aa.assert_awaited_once()
    assert set_aa.await_args is not None
    kwargs = set_aa.await_args.kwargs
    assert kwargs["aa_provider_domain"] == provider.domain
    assert kwargs["item_id"] == "track-1"
    analysis = kwargs["analysis"]
    assert analysis.extra_data == expected_extra
    # permanent result — never re-collected by the background scan
    assert "retry_after" not in analysis.extra_data


@pytest.mark.asyncio
async def test_start_analysis_skips_within_no_match_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A track whose prior no-match result is still inside its cooldown is declined."""
    provider = _make_provider()
    track = MagicMock(mbid=None)
    track.get_external_id.return_value = None
    cast("MagicMock", provider.mass.music.tracks).get_library_item_by_prov_id = AsyncMock(
        return_value=track
    )
    cast("MagicMock", provider.mass.streams.audio_analysis).get_audio_analysis = AsyncMock(
        return_value=AudioAnalysisData(extra_data={"retry_after": int(utc_timestamp()) + 3600})
    )
    fp = _FakeFingerprinter()
    _install_fake_chromaprint(monkeypatch, fp)

    accepted = await provider.start_analysis("session", _make_streamdetails(), _make_audio_format())

    assert accepted is False
    # declined before any fingerprinting work
    assert fp.start_args is None


@pytest.mark.asyncio
async def test_start_analysis_skips_multichannel(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    A multichannel (e.g. 5.1) file is declined before any fingerprinting work.

    Feeding chromaprint multichannel audio trips a C-level assertion that aborts the
    process, so the session must be refused up-front rather than risk the crash.
    """
    provider = _make_provider()
    track = MagicMock(mbid=None)
    track.get_external_id.return_value = None
    cast("MagicMock", provider.mass.music.tracks).get_library_item_by_prov_id = AsyncMock(
        return_value=track
    )
    fp = _FakeFingerprinter()
    _install_fake_chromaprint(monkeypatch, fp)

    accepted = await provider.start_analysis(
        "session", _make_streamdetails(), _make_audio_format(channels=6)
    )

    assert accepted is False
    # declined before the fingerprinter was ever started
    assert fp.start_args is None


@pytest.mark.asyncio
async def test_start_analysis_retries_after_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Once the cooldown has elapsed, the track is accepted for a fresh lookup."""
    provider = _make_provider()
    track = MagicMock(mbid=None)
    track.get_external_id.return_value = None
    cast("MagicMock", provider.mass.music.tracks).get_library_item_by_prov_id = AsyncMock(
        return_value=track
    )
    cast("MagicMock", provider.mass.streams.audio_analysis).get_audio_analysis = AsyncMock(
        return_value=AudioAnalysisData(extra_data={"retry_after": int(utc_timestamp()) - 3600})
    )
    _install_fake_chromaprint(monkeypatch, _FakeFingerprinter())

    accepted = await provider.start_analysis("session", _make_streamdetails(), _make_audio_format())

    assert accepted is True


@pytest.mark.asyncio
async def test_finalize_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """A high-score match yields mbid + acoustid + candidates + release_groups."""
    provider = _make_provider()
    _install_fake_chromaprint(monkeypatch, _FakeFingerprinter())
    track = MagicMock(mbid=None)
    track.get_external_id.return_value = None
    cast("MagicMock", provider.mass.music.tracks).get_library_item_by_prov_id = AsyncMock(
        return_value=track
    )

    session_id = "session-final"
    await provider._start_analysis(session_id, _make_streamdetails(), _make_audio_format())
    provider._sessions[session_id] = AnalysisSessionData(
        streamdetails=_make_streamdetails(),
        audio_format=_make_audio_format(),
    )
    provider._data[session_id].pcm_seconds_fed = 30.0

    _install_lookup_response(
        monkeypatch,
        {
            "status": "ok",
            "results": [
                {
                    "id": "acoustid-1",
                    "score": 0.97,
                    "recordings": [
                        {
                            "id": "mbid-rich",
                            "title": "Song",
                            "artists": [{}],
                            "releases": [{"id": "rel-a"}],
                            "releasegroups": [
                                {"id": "rg-a", "title": "Album", "type": "Album"},
                            ],
                        },
                    ],
                },
            ],
        },
    )

    result = await provider._finalize(session_id)

    assert result is not None
    assert result.extra_data is not None
    assert result.extra_data["mbid"] == "mbid-rich"
    assert result.extra_data["acoustid"] == "acoustid-1"
    assert result.extra_data["match_score"] == pytest.approx(0.97)
    assert result.extra_data["release_groups"] == [
        {
            "id": "rg-a",
            "title": "Album",
            "primary_type": "Album",
            "secondary_types": [],
            "artists": [],
        }
    ]
    assert result.extra_data["candidates"][0]["acoustid"] == "acoustid-1"


@pytest.mark.asyncio
async def test_finalize_rejects_low_score(monkeypatch: pytest.MonkeyPatch) -> None:
    """A best-result score below the threshold yields a retryable no-match marker."""
    provider = _make_provider(min_score=0.85)
    _install_fake_chromaprint(monkeypatch, _FakeFingerprinter())
    track = MagicMock(mbid=None)
    track.get_external_id.return_value = None
    cast("MagicMock", provider.mass.music.tracks).get_library_item_by_prov_id = AsyncMock(
        return_value=track
    )

    session_id = "session-lowscore"
    await provider._start_analysis(session_id, _make_streamdetails(), _make_audio_format())
    provider._sessions[session_id] = AnalysisSessionData(
        streamdetails=_make_streamdetails(),
        audio_format=_make_audio_format(),
    )
    provider._data[session_id].pcm_seconds_fed = 10.0

    _install_lookup_response(
        monkeypatch,
        {
            "status": "ok",
            "results": [
                {
                    "id": "acoustid-low",
                    "score": 0.5,
                    "recordings": [{"id": "mbid-low", "title": "Song"}],
                }
            ],
        },
    )

    # _finalize persists a no-match result itself and returns None so the base class
    # does not overwrite it at the current analysis_version
    assert await provider._finalize(session_id) is None
    set_aa = cast("AsyncMock", provider.mass.streams.audio_analysis.set_audio_analysis)
    set_aa.assert_awaited_once()
    assert set_aa.await_args is not None
    kwargs = set_aa.await_args.kwargs
    # stored below the current version so it is re-offered, and gated on a future retry_after
    assert kwargs["analysis_version"] == NO_MATCH_ANALYSIS_VERSION
    now = int(utc_timestamp())
    retry_after = kwargs["analysis"].extra_data["retry_after"]
    assert now < retry_after <= now + NO_MATCH_RETRY_DAYS * 86400 + 5


# ---------------------------------------------------------------------------
# Chromaprint binding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_init_imports_the_native_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing libchromaprint surfaces as a provider load failure, not a per-track error."""
    provider = _make_provider()
    imported: list[str] = []

    async def fake_import(name: str, _package: str | None = None) -> ModuleType:
        imported.append(name)
        raise ImportError("couldn't find libchromaprint")

    monkeypatch.setattr(
        "music_assistant.providers.acoustid_lookup.provider.import_module_in_thread", fake_import
    )

    # UnsupportedSystemError marks the failure as permanent, so the loader reports it
    # to the user instead of retrying a library that will not appear on its own.
    with pytest.raises(UnsupportedSystemError):
        await provider.handle_async_init()
    assert imported == ["chromaprint"]


@pytest.mark.asyncio
async def test_fingerprinter_start_failure_declines_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chromaprint error while starting the fingerprinter declines the session."""
    _install_chromaprint_module(monkeypatch, failing_call="start")
    provider = _make_provider()
    _install_unidentified_track(provider)

    accepted = await provider._start_analysis(
        "session", _make_streamdetails(), _make_audio_format()
    )

    assert accepted is False


@pytest.mark.asyncio
async def test_chromaprint_error_while_feeding_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    """A chromaprint error from feed() marks the session errored instead of propagating."""
    _install_chromaprint_module(monkeypatch, failing_call="feed")
    provider = _make_provider()
    _install_unidentified_track(provider)
    session_id = "session-feed"
    assert await provider._start_analysis(session_id, _make_streamdetails(), _make_audio_format())

    await provider.process_pcm_chunk(session_id, b"\x00\x01" * 100)

    assert provider._data[session_id].error is not None


@pytest.mark.asyncio
async def test_chromaprint_error_while_finishing_is_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chromaprint error from finish() yields no result instead of propagating."""
    _install_chromaprint_module(monkeypatch, failing_call="finish")
    provider = _make_provider()
    _install_unidentified_track(provider)
    session_id = "session-finish"
    assert await provider._start_analysis(session_id, _make_streamdetails(), _make_audio_format())
    provider._data[session_id].pcm_seconds_fed = 30.0

    assert await provider._finalize(session_id) is None


@pytest.mark.asyncio
async def test_feed_type_error_is_caught_without_a_native_fingerprinter() -> None:
    """The feed() guard still catches TypeError when no chromaprint errors are registered."""
    provider = _make_provider()
    assert provider._fingerprint_errors == ()

    class _RejectingFingerprinter:
        def feed(self, data: bytes) -> None:
            raise TypeError("unsupported buffer")

    provider._data["session"] = _AcoustidSessionData(
        fingerprinter=_RejectingFingerprinter(),
        sample_rate=44100,
        channels=2,
        sample_width=2,
        track_duration=180,
    )

    await provider.process_pcm_chunk("session", b"\x00\x01" * 100)

    assert provider._data["session"].error is not None


# ---------------------------------------------------------------------------
# post_analysis side effects
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("write_tags_back", "expect_tag_writes"),
    [(False, False), (True, True)],
    ids=["tags_disabled", "tags_enabled"],
)
async def test_post_analysis_persists_and_writes_tags(
    monkeypatch: pytest.MonkeyPatch,
    *,
    write_tags_back: bool,
    expect_tag_writes: bool,
) -> None:
    """post_analysis must always persist to library, and write tags only when enabled."""
    write_tags = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "music_assistant.providers.acoustid_lookup.provider.write_identifier_tags",
        write_tags,
    )

    provider = _make_provider(write_tags_back=write_tags_back)
    streamdetails = _make_streamdetails()
    analysis = AudioAnalysisData(extra_data={"mbid": "mbid-x", "acoustid": "acoustid-x"})

    consensus_calls: list[Any] = []

    async def fake_consensus(_self: AcoustidLookupProvider, sd: Any, **_kwargs: Any) -> None:
        consensus_calls.append(sd)

    monkeypatch.setattr(AcoustidLookupProvider, "_maybe_set_album_release_group", fake_consensus)

    await provider.post_analysis(streamdetails, analysis)

    cast("AsyncMock", provider.mass.music.tracks.set_identifiers).assert_awaited_once_with(
        item_id=streamdetails.item_id,
        provider_instance_id_or_domain=streamdetails.provider,
        mbid="mbid-x",
        acoustid="acoustid-x",
        isrcs=[],
    )
    # Album consensus is a pure DB write and must run regardless of write_tags_back.
    assert consensus_calls == [streamdetails]
    if expect_tag_writes:
        write_tags.assert_awaited_once_with(
            "/music/track.flac",
            mbid="mbid-x",
            acoustid="acoustid-x",
            isrcs=[],
            artist_mbids=[],
        )
    else:
        write_tags.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "expected_isrcs", "expected_artist_mbids"),
    [
        pytest.param(
            "returns",
            ["USAB12345678", "USCD87654321"],
            ["artist-mbid-1", "artist-mbid-2"],
            id="mb_returns_extras",
        ),
        pytest.param("missing", [], [], id="mb_provider_missing"),
        pytest.param("raises", [], [], id="mb_raises"),
    ],
)
async def test_post_analysis_mb_enrichment(
    monkeypatch: pytest.MonkeyPatch,
    *,
    scenario: str,
    expected_isrcs: list[str],
    expected_artist_mbids: list[str],
) -> None:
    """ISRCs and artist MBIDs from MusicBrainz get applied as DB/file tags when available."""
    write_tags = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "music_assistant.providers.acoustid_lookup.provider.write_identifier_tags",
        write_tags,
    )

    provider = _make_provider(write_tags_back=True)
    source_mock = MagicMock(write_access=True)
    mb_provider: MagicMock | None
    if scenario == "returns":
        artist_credit = [MagicMock(artist=MagicMock(id=mbid)) for mbid in expected_artist_mbids]
        mb_recording = MagicMock(isrcs=expected_isrcs, artist_credit=artist_credit)
        mb_provider = MagicMock()
        mb_provider.get_recording_details = AsyncMock(return_value=mb_recording)
    elif scenario == "missing":
        mb_provider = None
    else:
        mb_provider = MagicMock()
        mb_provider.get_recording_details = AsyncMock(side_effect=aiohttp.ClientError("synthetic"))

    def _get_provider(provider_id: Any, **_kwargs: Any) -> Any:
        if provider_id == "filesystem_local_test":
            return source_mock
        return mb_provider

    provider.mass.get_provider = MagicMock(side_effect=_get_provider)  # type: ignore[method-assign]

    await provider.post_analysis(
        _make_streamdetails(),
        AudioAnalysisData(extra_data={"mbid": "mbid-x", "acoustid": "acoustid-x"}),
    )

    set_ids = cast("AsyncMock", provider.mass.music.tracks.set_identifiers)
    assert set_ids.await_args is not None
    assert set_ids.await_args.kwargs["isrcs"] == expected_isrcs
    write_tags.assert_awaited_once_with(
        "/music/track.flac",
        mbid="mbid-x",
        acoustid="acoustid-x",
        isrcs=expected_isrcs,
        artist_mbids=expected_artist_mbids,
    )


@pytest.mark.asyncio
async def test_post_analysis_consensus_silent_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A crash in the consensus helper must not lose the per-track persistence."""
    provider = _make_provider()

    async def boom(_self: AcoustidLookupProvider, _streamdetails: Any, **_kwargs: Any) -> None:
        raise RuntimeError("synthetic")

    monkeypatch.setattr(AcoustidLookupProvider, "_maybe_set_album_release_group", boom)

    await provider.post_analysis(
        _make_streamdetails(),
        AudioAnalysisData(
            extra_data={"mbid": "mbid-x", "acoustid": "acoustid-x", "release_groups": []}
        ),
    )

    cast("AsyncMock", provider.mass.music.tracks.set_identifiers).assert_awaited_once()


# ---------------------------------------------------------------------------
# Album-level release-group consensus
# ---------------------------------------------------------------------------


_NEWS_OF_THE_WORLD_QUEEN_RGS = [
    {
        "id": "rg-future-boy",
        "title": "News of the World",
        "primary_type": "Album",
        "secondary_types": [],
        "artists": ["Future Boy"],
    },
    {
        "id": "rg-queen",
        "title": "News of the World",
        "primary_type": "Album",
        "secondary_types": [],
        "artists": ["Queen"],
    },
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "album_name",
        "track_provider_instances",
        "extras",
        "track_artist",
        "expected_rg_id",
    ),
    [
        # Strong coverage: every analysed track votes for the same RG.
        pytest.param(
            "Silver Thunderbird",
            ["filesystem_local_test"] * 10,
            [{"release_groups": [_rg("rg-st")]}] * 10,
            None,
            "rg-st",
            id="full_coverage",
        ),
        # Singleton voter with a name-matching RG.
        pytest.param(
            "Silver Thunderbird",
            ["filesystem_local_test"],
            [{"release_groups": [_rg("rg-st")]}],
            None,
            "rg-st",
            id="singleton_with_match",
        ),
        # 5 of 6 voters agree; one outlier on a different RG.
        pytest.param(
            "Silver Thunderbird",
            ["filesystem_local_test"] * 6,
            [{"release_groups": [_rg("rg-st")]}] * 5
            + [{"release_groups": [_rg("rg-other", title="Other")]}],
            None,
            "rg-st",
            id="tolerates_one_missing",
        ),
        # Quorum denominator excludes tracks served by a different provider.
        pytest.param(
            "Silver Thunderbird",
            ["filesystem_local_test"] * 2 + ["some_other_provider"] * 2,
            [{"release_groups": [_rg("rg-st")]}] * 2,
            None,
            "rg-st",
            id="quorum_scoped_to_this_provider",
        ),
        # Asymmetric substring: user "The Platinum Collection" vs MB's longer form.
        pytest.param(
            "The Platinum Collection",
            ["filesystem_local_test"] * 2,
            [
                {
                    "release_groups": [
                        _rg(
                            "rg-platinum",
                            title="Greatest Hits I, II & III: The Platinum Collection",
                        )
                    ]
                }
            ]
            * 2,
            None,
            "rg-platinum",
            id="asymmetric_substring_match",
        ),
        # Exact title beats substring when both survive.
        pytest.param(
            "The Platinum Collection",
            ["filesystem_local_test"] * 2,
            [
                {
                    "release_groups": [
                        _rg(
                            "rg-substring",
                            title="Greatest Hits I, II & III: The Platinum Collection",
                        ),
                        _rg("rg-exact", title="The Platinum Collection"),
                    ]
                }
            ]
            * 2,
            None,
            "rg-exact",
            id="exact_beats_substring",
        ),
        # Same-titled RGs from different artists; expected_artist picks the matching one.
        pytest.param(
            "News Of The World",
            ["filesystem_local_test"],
            [{"release_groups": list(_NEWS_OF_THE_WORLD_QUEEN_RGS)}],
            "Queen",
            "rg-queen",
            id="artist_filter_picks_matching_artist",
        ),
        # RG with no captured artist info is treated as compatible — older payload shape.
        pytest.param(
            "News Of The World",
            ["filesystem_local_test"],
            [
                {
                    "release_groups": [
                        {
                            "id": "rg-unknown-artist",
                            "title": "News of the World",
                            "primary_type": "Album",
                            "secondary_types": [],
                        }
                    ]
                }
            ],
            "Queen",
            "rg-unknown-artist",
            id="artist_filter_lets_unknown_artist_pass",
        ),
    ],
)
async def test_consensus_writes_release_group(
    *,
    album_name: str,
    track_provider_instances: list[str],
    extras: list[dict[str, Any]],
    track_artist: str | None,
    expected_rg_id: str,
) -> None:
    """Each consensus path that produces a winner writes the RG to the album row."""
    provider = _make_provider()
    album = _make_library_album(name=album_name)
    album_tracks = [
        _make_album_tracks(1, provider_instance=inst)[0] for inst in track_provider_instances
    ]
    _wire_album_for_consensus(provider, album=album, album_tracks=album_tracks, extras=extras)
    if track_artist is not None:
        _wire_track_for_mb_lookup(provider, track_name="any", artist_name=track_artist)

    await provider._maybe_set_album_release_group(_make_streamdetails())

    cast("AsyncMock", provider.mass.music.albums.set_release_group).assert_awaited_once_with(
        42, expected_rg_id
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("album_name", "album_kwargs", "track_provider_instances", "extras"),
    [
        # Album already has a release-group — idempotent skip before tally.
        pytest.param(
            "Silver Thunderbird",
            {"existing_rg": "rg-pre-existing"},
            ["filesystem_local_test"] * 6,
            [{"release_groups": [_rg("rg-st")]}] * 6,
            id="album_already_has_releasegroup",
        ),
        # Below 50% quorum — 4 of 12 voted.
        pytest.param(
            "Silver Thunderbird",
            {},
            ["filesystem_local_test"] * 12,
            [{"release_groups": [_rg("rg-st")]}] * 4,
            id="below_50pct_quorum",
        ),
        # Top coverage below required — 8 voters split 2/2/2/2 across distinct RGs.
        pytest.param(
            "Silver Thunderbird",
            {},
            ["filesystem_local_test"] * 8,
            [{"release_groups": [_rg(f"rg-{i // 2}", title=f"Title {i // 2}")]} for i in range(8)],
            id="top_coverage_below_required",
        ),
        # No survivor title matches the album — voting tracks share RGs, all wrong names.
        pytest.param(
            "Silver Thunderbird",
            {},
            ["filesystem_local_test"] * 6,
            [
                {
                    "release_groups": [
                        _rg("rg-here-now", title="Here & Now"),
                        _rg("rg-box", title="Box Set"),
                    ]
                }
            ]
            * 6,
            id="no_survivor_title_matches",
        ),
        # User tag is more specific than MB title — asymmetric substring rejects.
        pytest.param(
            "Greatest Hits: 40 Trips Around The Sun",
            {},
            ["filesystem_local_test"],
            [{"release_groups": [_rg("rg-generic", title="Greatest Hits")]}],
            id="user_longer_substring_rejected",
        ),
        # Album has zero library tracks served by this provider.
        pytest.param(
            "Silver Thunderbird",
            {},
            ["some_other_provider"] * 3,
            [],
            id="no_tracks_served_by_this_provider",
        ),
    ],
)
async def test_consensus_abstains(
    *,
    album_name: str,
    album_kwargs: dict[str, Any],
    track_provider_instances: list[str],
    extras: list[dict[str, Any]],
) -> None:
    """Each guard refuses to write the release-group for the right reason."""
    provider = _make_provider()
    album = _make_library_album(name=album_name, **album_kwargs)
    album_tracks = [
        _make_album_tracks(1, provider_instance=inst)[0] for inst in track_provider_instances
    ]
    _wire_album_for_consensus(provider, album=album, album_tracks=album_tracks, extras=extras)

    await provider._maybe_set_album_release_group(_make_streamdetails())

    cast("AsyncMock", provider.mass.music.albums.set_release_group).assert_not_awaited()


def _wire_track_for_mb_lookup(
    provider: AcoustidLookupProvider, *, track_name: str, artist_name: str
) -> None:
    """Give the library track stub a name and a single named artist."""
    library_track = MagicMock()
    library_track.name = track_name
    library_track.album = MagicMock(item_id="42")
    library_track.artists = [MagicMock(name=artist_name)]
    library_track.artists[0].name = artist_name
    cast("MagicMock", provider.mass.music.tracks).get_library_item_by_prov_id = AsyncMock(
        return_value=library_track
    )


def _install_mb_search(
    provider: AcoustidLookupProvider,
    *,
    rg_id: str | None,
    rg_title: str | None,
) -> MagicMock:
    """Wire mass.get_provider('musicbrainz', ...) to a mock with a fake search()."""
    mb_provider = MagicMock()
    if rg_id is None:
        mb_provider.search = AsyncMock(return_value=None)
    else:
        rg = MagicMock(id=rg_id, title=rg_title)
        artist = MagicMock()
        recording = MagicMock()
        mb_provider.search = AsyncMock(return_value=(artist, rg, recording))
    cast("MagicMock", provider.mass).get_provider = MagicMock(return_value=mb_provider)
    return mb_provider


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "album_name",
        "track_name",
        "artist_name",
        "extras",
        "mb_result",
        "expected_mb_kwargs",
        "expected_rg",
    ),
    [
        # Consensus has nothing to vote on; MB.search supplies the RG.
        pytest.param(
            "Alive and Kicking",
            "Alive and Kicking",
            "Simple Minds",
            [{"release_groups": [_rg("rg-unrelated", title="Some Compilation")]}],
            ("rg-mb-fallback", "Alive and Kicking"),
            {
                "artistname": "Simple Minds",
                "albumname": "Alive and Kicking",
                "trackname": "Alive and Kicking",
            },
            "rg-mb-fallback",
            id="consensus_abstains_mb_writes_rg",
        ),
        # Hyphen / colon / parens in the album name are flattened before the
        # MB query so Lucene's phrase match lines up with MB's stored variant.
        pytest.param(
            "My Love - Ultimate Essential Collection",
            "Beauty and the Beast",
            "Céline Dion",
            [{"release_groups": [_rg("rg-unrelated", title="Some Compilation")]}],
            ("rg-mylove", "My Love: Ultimate Essential Collection"),
            {
                "artistname": "Céline Dion",
                "albumname": "My Love Ultimate Essential Collection",
                "trackname": "Beauty and the Beast",
            },
            "rg-mylove",
            id="separators_flattened_for_lucene",
        ),
        # Consensus has already supplied the right RG; MB.search must not run.
        pytest.param(
            "Silver Thunderbird",
            "Whatever",
            "Mary Chapin Carpenter",
            [{"release_groups": [_rg("rg-st")]}, {"release_groups": [_rg("rg-st")]}],
            ("rg-decoy", "Decoy"),
            None,
            "rg-st",
            id="consensus_succeeds_mb_skipped",
        ),
        # MB.search returns an RG whose title doesn't match; refuse the write.
        pytest.param(
            "Alive and Kicking",
            "Alive and Kicking",
            "Simple Minds",
            [{"release_groups": [_rg("rg-unrelated", title="Some Compilation")]}],
            ("rg-wrong", "Something Entirely Different"),
            {
                "artistname": "Simple Minds",
                "albumname": "Alive and Kicking",
                "trackname": "Alive and Kicking",
            },
            None,
            id="mb_returns_wrong_title_refused",
        ),
        # No artist on the library track; MB query is skipped cleanly.
        pytest.param(
            "Alive and Kicking",
            "Alive and Kicking",
            None,
            [{"release_groups": [_rg("rg-unrelated", title="Some Compilation")]}],
            ("rg-decoy", "Decoy"),
            None,
            None,
            id="no_artist_mb_skipped",
        ),
    ],
)
async def test_mb_fallback(
    *,
    album_name: str,
    track_name: str,
    artist_name: str | None,
    extras: list[dict[str, Any]],
    mb_result: tuple[str, str],
    expected_mb_kwargs: dict[str, str] | None,
    expected_rg: str | None,
) -> None:
    """MB.search runs only when consensus abstains and only writes a title-matching RG."""
    provider = _make_provider()
    album = _make_library_album(name=album_name)
    album_tracks = _make_album_tracks(len(extras))
    _wire_album_for_consensus(provider, album=album, album_tracks=album_tracks, extras=extras)
    if artist_name is not None:
        _wire_track_for_mb_lookup(provider, track_name=track_name, artist_name=artist_name)
    else:
        # Library track stub with empty artists — drives the "no artist" skip path.
        library_track = MagicMock()
        library_track.name = track_name
        library_track.album = MagicMock(item_id="42")
        library_track.artists = []
        cast("MagicMock", provider.mass.music.tracks).get_library_item_by_prov_id = AsyncMock(
            return_value=library_track
        )
    mb = _install_mb_search(provider, rg_id=mb_result[0], rg_title=mb_result[1])

    await provider._maybe_set_album_release_group(_make_streamdetails())

    if expected_mb_kwargs is None:
        cast("AsyncMock", mb.search).assert_not_awaited()
    else:
        cast("AsyncMock", mb.search).assert_awaited_once_with(**expected_mb_kwargs)
    rg_writer = cast("AsyncMock", provider.mass.music.albums.set_release_group)
    if expected_rg is None:
        rg_writer.assert_not_awaited()
    else:
        rg_writer.assert_awaited_once_with(42, expected_rg)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def test_parse_response_aggregates_release_groups() -> None:
    """release_groups is the deduped union across recordings, filtered by min_score."""
    payload = {
        "status": "ok",
        "results": [
            {
                "id": "acoustid-high",
                "score": 0.9,
                "recordings": [
                    {
                        "id": "rec-a",
                        "title": "Song",
                        "releasegroups": [
                            {"id": "rg-shared", "title": "Album", "type": "Album"},
                            {"id": "rg-a-only", "title": "Sampler", "type": "Compilation"},
                            # duplicate of rg-shared must be deduped
                            {"id": "rg-shared", "title": "Album", "type": "Album"},
                        ],
                    },
                    {
                        "id": "rec-b",
                        "releasegroups": [
                            {"id": "rg-shared", "title": "Album", "type": "Album"},
                            {"id": "rg-b-only", "title": "Reissue", "type": "Album"},
                        ],
                    },
                ],
            },
            {
                "id": "acoustid-low",
                "score": 0.3,
                "recordings": [
                    {
                        "id": "rec-low",
                        "title": "Song",
                        "releasegroups": [
                            {"id": "rg-low-score", "title": "Other", "type": "Album"}
                        ],
                    }
                ],
            },
        ],
    }
    _score, _acoustid, _mbid, _candidates, _matched, release_groups = _parse_response(
        payload, min_score=0.5
    )
    assert {rg["id"] for rg in release_groups} == {"rg-shared", "rg-a-only", "rg-b-only"}


@pytest.mark.parametrize(
    (
        "expected_track_title",
        "expected_album_title",
        "payload",
        "expected_mbid",
        "expected_acoustid",
    ),
    [
        # No track title supplied — filter is a no-op, score picks the winner.
        pytest.param(
            None,
            None,
            {
                "status": "ok",
                "results": [
                    {
                        "id": "acoustid-x",
                        "score": 0.9,
                        "recordings": [
                            {"id": "rec-x", "title": "Anything", "releases": [{"id": "r"}]}
                        ],
                    }
                ],
            },
            "rec-x",
            "acoustid-x",
            id="no_hint_falls_back_to_score",
        ),
        # Hint matches one recording — that recording wins.
        pytest.param(
            "Ventura Highway",
            None,
            {
                "status": "ok",
                "results": [
                    {
                        "id": "acoustid-x",
                        "score": 0.95,
                        "recordings": [
                            {"id": "rec-match", "title": "Ventura Highway"},
                            {"id": "rec-other", "title": "Other"},
                        ],
                    }
                ],
            },
            "rec-match",
            "acoustid-x",
            id="match_picks_correct_recording",
        ),
        # No recording matches the hint anywhere — refuse the match.
        pytest.param(
            "Ventura Highway",
            None,
            {
                "status": "ok",
                "results": [
                    {
                        "id": "acoustid-x",
                        "score": 0.97,
                        "recordings": [{"id": "rec-wrong", "title": "Trouble"}],
                    }
                ],
            },
            None,
            None,
            id="no_match_refuses",
        ),
        # Lower-score result whose recording title matches must beat higher-score non-match.
        pytest.param(
            "Ventura Highway",
            None,
            {
                "status": "ok",
                "results": [
                    {
                        "id": "acoustid-high",
                        "score": 0.99,
                        "recordings": [{"id": "rec-wrong", "title": "Trouble"}],
                    },
                    {
                        "id": "acoustid-low",
                        "score": 0.85,
                        "recordings": [{"id": "rec-right", "title": "Ventura Highway"}],
                    },
                ],
            },
            "rec-right",
            "acoustid-low",
            id="prefers_title_match_over_score",
        ),
        # MB has the canonical title; user has a medley/prefix that contains it.
        pytest.param(
            "Water Song / Janie's Got a Gun",
            None,
            {
                "status": "ok",
                "results": [
                    {
                        "id": "acoustid-x",
                        "score": 0.95,
                        "recordings": [{"id": "rec-jgg", "title": "Janie's Got a Gun"}],
                    }
                ],
            },
            "rec-jgg",
            "acoustid-x",
            id="substring_fallback_matches_subtitle",
        ),
        # Single-word title must not match a longer phrase containing it (trivial-collision guard).
        pytest.param(
            "Trouble",
            None,
            {
                "status": "ok",
                "results": [
                    {
                        "id": "acoustid-x",
                        "score": 0.97,
                        "recordings": [{"id": "rec-im-in-trouble", "title": "I'm In Trouble"}],
                    }
                ],
            },
            None,
            None,
            id="substring_fallback_rejects_single_word",
        ),
        # Two results both title-match the track; the album hint picks the right release.
        pytest.param(
            "Ventura Highway",
            "History: America's Greatest Hits",
            {
                "status": "ok",
                "results": [
                    {
                        "id": "acoustid-5.1mix",
                        "score": 0.99,
                        "recordings": [
                            {
                                "id": "rec-homecoming",
                                "title": "Ventura Highway",
                                "releases": [{"id": "rel-h", "title": "Homecoming"}],
                            }
                        ],
                    },
                    {
                        "id": "acoustid-history",
                        "score": 0.85,
                        "recordings": [
                            {
                                "id": "rec-history",
                                "title": "Ventura Highway",
                                "releases": [
                                    {"id": "rel-hist", "title": "History: America's Greatest Hits"}
                                ],
                            }
                        ],
                    },
                ],
            },
            "rec-history",
            "acoustid-history",
            id="prefers_album_match_when_track_matches_tie",
        ),
        # Remaster suffix stripping — user tag and MB title differ only by a
        # version qualifier; pre-normalise strip should make them match.
        *(
            pytest.param(
                user_title,
                None,
                {
                    "status": "ok",
                    "results": [
                        {
                            "id": "acoustid-x",
                            "score": 0.99,
                            "recordings": [{"id": "rec-africa", "title": mb_title}],
                        }
                    ],
                },
                "rec-africa",
                "acoustid-x",
                id=f"remaster_strip_{case_id}",
            )
            for case_id, user_title, mb_title in (
                ("user_year_paren", "Africa (2016 Remaster)", "Africa"),
                ("mb_year_paren", "Africa", "Africa (2018 Remaster)"),
                ("user_remastered_year", "Africa (Remastered 2011)", "Africa"),
                ("user_hyphen", "Africa - Remastered", "Africa"),
                ("user_hyphen_year", "Africa - 2018 Remaster", "Africa"),
                ("both_variants", "Africa (Remaster)", "Africa (2018 Remaster)"),
                ("ampersand_user_word", "Alive And Kicking", "Alive & Kicking"),
                ("ampersand_user_amp", "Alive & Kicking", "Alive And Kicking"),
            )
        ),
        # Tiered album match: when one recording's release exactly matches the
        # user's specific album and another only substring-matches a generic
        # release, the exact match wins despite a lower fingerprint score.
        pytest.param(
            "Africa",
            "Greatest Hits: 40 Trips Around The Sun",
            {
                "status": "ok",
                "results": [
                    {
                        "id": "acoustid-substring",
                        "score": 0.99,
                        "recordings": [
                            {
                                "id": "rec-substring",
                                "title": "Africa",
                                "releases": [{"id": "rel-gh", "title": "Greatest Hits"}],
                            }
                        ],
                    },
                    {
                        "id": "acoustid-exact",
                        "score": 0.85,
                        "recordings": [
                            {
                                "id": "rec-exact",
                                "title": "Africa",
                                "releases": [
                                    {
                                        "id": "rel-40trips",
                                        "title": "Greatest Hits: 40 Trips Around The Sun",
                                    }
                                ],
                            }
                        ],
                    },
                ],
            },
            "rec-exact",
            "acoustid-exact",
            id="exact_album_match_beats_substring",
        ),
    ],
)
def test_parse_response_track_name_behaviour(
    *,
    expected_track_title: str | None,
    expected_album_title: str | None,
    payload: dict[str, Any],
    expected_mbid: str | None,
    expected_acoustid: str | None,
) -> None:
    """expected_track_title is a hard filter; expected_album_title disambiguates ties."""
    _score, acoustid, mbid, _candidates, _matched, _rgs = _parse_response(
        payload,
        expected_track_title=expected_track_title,
        expected_album_title=expected_album_title,
    )
    assert mbid == expected_mbid
    assert acoustid == expected_acoustid
