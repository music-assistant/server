"""Unit tests for QQ Music provider utility helpers."""

# mypy: ignore-errors

from __future__ import annotations

import asyncio
from logging import INFO
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.enums import MediaType, ProviderFeature
from music_assistant_models.errors import (
    LoginFailed,
    MediaNotFoundError,
    ResourceTemporarilyUnavailable,
    UnplayableMediaError,
)
from music_assistant_models.media_items import Album
from qqmusic_api.models.lyric import GetLyricResponse
from qqmusic_api.models.request import Credential
from qqmusic_api.models.singer import (
    HomepageHeaderResponse,
    HomepageTabDetailResponse,
    SingerAlbumListResponse,
)
from qqmusic_api.models.song import (
    GetCdnDispatchResponse,
    GetSongDetailResponse,
    GetSongUrlsResponse,
)
from qqmusic_api.models.songlist import CreateDeleteSonglistResp
from qqmusic_api.modules.song import SongFileType

from music_assistant.controllers.cache import BYPASS_CACHE
from music_assistant.providers.qqmusic import (
    _CDN_DISPATCH_CACHE_KEY,
    SUPPORTED_FEATURES,
    QQMusicProvider,
    _store_credential,
)
from music_assistant.providers.qqmusic.constants import (
    CONF_CREDENTIAL_JSON,
    CONF_QUALITY,
    QUALITY_HI_RES,
)


def test_parse_playlist_id_composite() -> None:
    """Composite playlist id should parse into dissid/dirid."""
    provider = QQMusicProvider.__new__(QQMusicProvider)
    dissid, dirid = provider._parse_playlist_id("12345:678")
    assert dissid == 12345
    assert dirid == 678


def test_parse_playlist_id_legacy_single() -> None:
    """Single numeric playlist id should fallback to dirid 0."""
    provider = QQMusicProvider.__new__(QQMusicProvider)
    dissid, dirid = provider._parse_playlist_id("12345")
    assert dissid == 12345
    assert dirid == 0


def test_parse_playlist_id_invalid_raises() -> None:
    """Invalid playlist id should raise ValueError."""
    provider = QQMusicProvider.__new__(QQMusicProvider)
    with pytest.raises(ValueError, match="invalid literal"):
        provider._parse_playlist_id("invalid:value")


def test_supported_features_include_phase1_capabilities() -> None:
    """QQ Music should expose phase-1 capabilities in provider features."""
    assert ProviderFeature.SIMILAR_TRACKS in SUPPORTED_FEATURES
    assert ProviderFeature.PLAYLIST_CREATE in SUPPORTED_FEATURES
    assert ProviderFeature.PLAYLIST_TRACKS_EDIT in SUPPORTED_FEATURES
    assert ProviderFeature.LYRICS in SUPPORTED_FEATURES


def test_store_credential_writes_only_current_format() -> None:
    """New logins persist the complete SDK credential without legacy scalar fields."""
    values = {}

    _store_credential(
        values,
        Credential.model_validate(
            {
                "musicid": 123,
                "musickey": "key",
                "str_musicid": "123",
                "encryptUin": "encrypted-uin",
                "loginType": 2,
            }
        ),
    )

    assert set(values) == {CONF_CREDENTIAL_JSON}


def test_persist_credential_writes_only_current_format() -> None:
    """Credential refreshes do not recreate retired scalar setup values."""
    provider = QQMusicProvider.__new__(QQMusicProvider)
    provider._credential = Credential.model_validate(
        {"musicid": 123, "musickey": "key", "str_musicid": "123", "loginType": 2}
    )
    provider._update_setup_data = Mock()

    provider._persist_credential()

    provider._update_setup_data.assert_called_once()
    assert provider._update_setup_data.call_args.args[0] == CONF_CREDENTIAL_JSON


@pytest.mark.asyncio
async def test_run_with_session_translates_sdk_validation_error() -> None:
    """Malformed typed SDK responses become a temporary provider error."""
    provider = QQMusicProvider.__new__(QQMusicProvider)
    provider._api_semaphore = asyncio.Semaphore(1)
    provider._ensure_valid_credential = AsyncMock()

    async def _malformed_response() -> None:
        GetSongUrlsResponse.model_validate({"midurlinfo": ["invalid"]})

    with pytest.raises(
        ResourceTemporarilyUnavailable, match="QQ Music API returned invalid data"
    ) as exc:
        await provider._run_with_session(_malformed_response())

    assert exc.value.backoff_time == 30


def test_get_candidate_file_types_hires_with_fallback_chain() -> None:
    """Hi-Res preference should fall back to FLAC -> MP3 320 -> MP3 128."""
    provider = QQMusicProvider.__new__(QQMusicProvider)
    provider.config = SimpleNamespace(  # type: ignore[attr-defined]
        get_value=lambda key: QUALITY_HI_RES if key == CONF_QUALITY else None
    )

    candidates = provider._get_candidate_file_types()
    assert candidates == [
        SongFileType.MASTER,
        SongFileType.FLAC,
        SongFileType.MP3_320,
        SongFileType.MP3_128,
    ]


def test_get_stream_audio_format_for_master() -> None:
    """MASTER stream type should map to 24-bit/192kHz FLAC format."""
    provider = QQMusicProvider.__new__(QQMusicProvider)

    stream_format = provider._get_stream_audio_format(SongFileType.MASTER)
    assert stream_format.content_type.value == "flac"
    assert stream_format.bit_depth == 24
    assert stream_format.sample_rate == 192000


def test_parse_artist_uses_sdk_model_fields() -> None:
    """Artist parser consumes canonical SDK field names."""
    provider = QQMusicProvider.__new__(QQMusicProvider)
    provider.manifest = SimpleNamespace(domain="qqmusic")  # type: ignore[attr-defined]
    provider.config = SimpleNamespace(instance_id="qqmusic_instance")  # type: ignore[attr-defined]
    artist = provider._parse_artist(
        {
            "mid": "003Nz2So3XXYek",
            "name": "<em>王力宏</em>",
            "subtitle": "华语流行男歌手",
        }
    )
    assert artist.item_id == "003Nz2So3XXYek"
    assert artist.name == "王力宏"
    assert artist.metadata.description == "华语流行男歌手"


def test_parse_artist_uses_sdk_avatar_and_description() -> None:
    """Artist parser accepts canonical SDK avatar fields."""
    provider = QQMusicProvider.__new__(QQMusicProvider)
    provider.manifest = SimpleNamespace(domain="qqmusic")  # type: ignore[attr-defined]
    provider.config = SimpleNamespace(instance_id="qqmusic_instance")  # type: ignore[attr-defined]
    artist = provider._parse_artist(
        {
            "mid": "003Nz2So3XXYek",
            "name": "王力宏",
            "avatar_url": "//y.qq.com/music/photo_new/T001R500x500M000003Nz2So3XXYek.jpg",
            "desc": "华语流行歌手",
        }
    )
    assert artist.name == "王力宏"
    assert artist.metadata.images
    assert artist.metadata.images[0].path.startswith("https://")


def test_parse_playlist_name_strips_highlight_tags() -> None:
    """Playlist parser strips highlighting from canonical title."""
    provider = QQMusicProvider.__new__(QQMusicProvider)
    provider.manifest = SimpleNamespace(domain="qqmusic")  # type: ignore[attr-defined]
    provider.config = SimpleNamespace(instance_id="qqmusic_instance")  # type: ignore[attr-defined]
    playlist = provider._parse_playlist({"id": 123, "title": "<em>王力宏</em>精选"})
    assert playlist.name == "王力宏精选"


def test_parse_playlist_detail_fields() -> None:
    """Playlist parser supports canonical SDK fields."""
    provider = QQMusicProvider.__new__(QQMusicProvider)
    provider.manifest = SimpleNamespace(domain="qqmusic")  # type: ignore[attr-defined]
    provider.config = SimpleNamespace(instance_id="qqmusic_instance")  # type: ignore[attr-defined]
    playlist = provider._parse_playlist(
        {
            "id": 7843129912,
            "dirid": 10,
            "title": "我的收藏",
            "desc": "这是歌单简介",
            "picurl": "//y.qq.com/music/photo_new/T003R500x500M0007843129912.jpg",
            "creator": {"nick": "Alice"},
        }
    )
    assert playlist.name == "我的收藏"
    assert playlist.owner == "Alice"
    assert playlist.metadata.description == "这是歌单简介"
    assert playlist.metadata.images
    assert playlist.metadata.images[0].path.startswith("https://")


def test_parse_track_album_mapping_uses_sdk_album_fields() -> None:
    """Track parser maps a canonical SDK album model dump."""
    provider = QQMusicProvider.__new__(QQMusicProvider)
    provider.manifest = SimpleNamespace(domain="qqmusic")  # type: ignore[attr-defined]
    provider.config = SimpleNamespace(instance_id="qqmusic_instance")  # type: ignore[attr-defined]

    track = provider._parse_track(
        {
            "mid": "003aAYrm3GE0Ac",
            "title": "稻香",
            "singer": [{"mid": "0025NhlN2yWrP4", "name": "周杰伦"}],
            "album": {"mid": "001qu4I30eVFYb", "title": "魔杰座"},
        }
    )
    assert track.album is not None
    assert isinstance(track.album, Album)
    assert track.album.item_id == "001qu4I30eVFYb"
    assert track.album.name == "魔杰座"


def test_parse_track_sets_version_and_description() -> None:
    """Track parser should map subtitle to version and desc to metadata description."""
    provider = QQMusicProvider.__new__(QQMusicProvider)
    provider.manifest = SimpleNamespace(domain="qqmusic")  # type: ignore[attr-defined]
    provider.config = SimpleNamespace(instance_id="qqmusic_instance")  # type: ignore[attr-defined]

    track = provider._parse_track(
        {
            "mid": "003aAYrm3GE0Ac",
            "title": "唯一",
            "title_extra": "Live",
            "desc": "热门现场版",
            "singer": [{"mid": "003Nz2So3XXYek", "name": "王力宏"}],
        }
    )
    assert track.name == "唯一"
    assert track.version == "Live"
    assert track.metadata.description == "热门现场版"


def test_parse_track_sets_max_quality_from_file_info() -> None:
    """Track parser should expose max supported quality on provider mapping."""
    provider = QQMusicProvider.__new__(QQMusicProvider)
    provider.manifest = SimpleNamespace(domain="qqmusic")  # type: ignore[attr-defined]
    provider.config = SimpleNamespace(instance_id="qqmusic_instance")  # type: ignore[attr-defined]

    track = provider._parse_track(
        {
            "mid": "003aAYrm3GE0Ac",
            "title": "唯一",
            "singer": [{"mid": "003Nz2So3XXYek", "name": "王力宏"}],
            "file": {
                "size_flac": 51200000,
                "size_320mp3": 12000000,
                "size_128mp3": 4000000,
            },
        }
    )
    provider_mapping = next(iter(track.provider_mappings))
    assert provider_mapping.audio_format.content_type.value == "flac"
    assert provider_mapping.audio_format.bit_depth == 16
    assert provider_mapping.audio_format.sample_rate == 44100
    assert provider_mapping.details is None


def test_parse_track_sets_master_quality_when_available() -> None:
    """Track parser should prefer master quality when QQ reports it in size_new."""
    provider = QQMusicProvider.__new__(QQMusicProvider)
    provider.manifest = SimpleNamespace(domain="qqmusic")  # type: ignore[attr-defined]
    provider.config = SimpleNamespace(instance_id="qqmusic_instance")  # type: ignore[attr-defined]

    track = provider._parse_track(
        {
            "mid": "003aAYrm3GE0Ac",
            "title": "唯一",
            "singer": [{"mid": "003Nz2So3XXYek", "name": "王力宏"}],
            "file": {
                "size_new": [123456789, 0, 0, 0, 0, 0],
            },
        }
    )
    provider_mapping = next(iter(track.provider_mappings))
    assert provider_mapping.audio_format.content_type.value == "flac"
    assert provider_mapping.audio_format.bit_depth == 24
    assert provider_mapping.audio_format.sample_rate == 192000
    assert provider_mapping.details == "Hi-Res"


def test_parse_track_sets_ogg_quality_when_mp3_fields_missing() -> None:
    """Track parser should still expose quality label from OGG-only file metadata."""
    provider = QQMusicProvider.__new__(QQMusicProvider)
    provider.manifest = SimpleNamespace(domain="qqmusic")  # type: ignore[attr-defined]
    provider.config = SimpleNamespace(instance_id="qqmusic_instance")  # type: ignore[attr-defined]

    track = provider._parse_track(
        {
            "mid": "003aAYrm3GE0Ac",
            "title": "唯一",
            "singer": [{"mid": "003Nz2So3XXYek", "name": "王力宏"}],
            "file": {
                "size_192ogg": 8000000,
                "size_96ogg": 4000000,
            },
        }
    )
    provider_mapping = next(iter(track.provider_mappings))
    assert provider_mapping.audio_format.content_type.value == "ogg"
    assert provider_mapping.details is None


def test_parse_album_sets_version_and_description() -> None:
    """Album parser maps canonical SDK subtitle and description fields."""
    provider = QQMusicProvider.__new__(QQMusicProvider)
    provider.manifest = SimpleNamespace(domain="qqmusic")  # type: ignore[attr-defined]
    provider.config = SimpleNamespace(instance_id="qqmusic_instance")  # type: ignore[attr-defined]

    album = provider._parse_album(
        {
            "mid": "001qu4I30eVFYb",
            "title": "唯一",
            "subtitle": "纪念版",
            "desc": "经典专辑",
            "singers": [{"mid": "003Nz2So3XXYek", "name": "王力宏"}],
        }
    )
    assert album.name == "唯一"
    assert album.version == "纪念版"
    assert album.metadata.description == "经典专辑"


@pytest.mark.asyncio
async def test_get_config_entries_exposes_quality_option_without_actions() -> None:
    """Migrated config entries expose only the quality option and no auth/QR actions."""
    provider = QQMusicProvider.__new__(QQMusicProvider)
    entries = await provider.get_config_entries()
    quality_entry = next(entry for entry in entries if entry.key == CONF_QUALITY)
    assert [option.value for option in quality_entry.options] == [
        "mp3_128",
        "mp3_320",
        "flac",
        "hi_res",
    ]
    # the action-driven QR/auth pseudo-flow entries are gone after the setup-flow migration
    assert all(entry.action is None for entry in entries)


@pytest.mark.asyncio
async def test_get_artist_albums_uses_typed_album_tab() -> None:
    """Artist albums are read from the public Homepage AlbumTab response."""
    provider = QQMusicProvider.__new__(QQMusicProvider)
    provider.manifest = SimpleNamespace(domain="qqmusic")  # type: ignore[attr-defined]
    provider.config = SimpleNamespace(instance_id="qqmusic_instance")  # type: ignore[attr-defined]
    provider._qq_singer = SimpleNamespace(  # type: ignore[attr-defined]
        get_tab_detail=AsyncMock(
            return_value=HomepageTabDetailResponse.model_validate(
                {
                    "TabID": "album",
                    "HasMore": 0,
                    "NeedShowTab": 1,
                    "Order": 0,
                    "TabList": [],
                    "AlbumTab": {
                        "TypeList": {"DefaultID": 0, "ItemList": []},
                        "AlbumList": [
                            {
                                "albumID": 1,
                                "albumMid": "album_mid",
                                "albumName": "Album",
                                "singerName": "Artist",
                            }
                        ],
                    },
                }
            )
        )
    )

    async def _run_with_session(coro):
        return await coro

    provider._run_with_session = _run_with_session  # type: ignore[attr-defined]
    albums = await QQMusicProvider.get_artist_albums.__wrapped__(provider, "artist")

    assert albums[0].item_id == "album_mid"
    assert albums[0].artists[0].item_id == "artist"
    assert albums[0].artists[0].name == "Artist"


@pytest.mark.asyncio
async def test_get_artist_albums_falls_back_to_typed_album_list() -> None:
    """An empty AlbumTab falls back to QQ Music's typed album-list endpoint."""
    provider = QQMusicProvider.__new__(QQMusicProvider)
    get_album_list = AsyncMock(
        return_value=SingerAlbumListResponse.model_validate(
            {
                "singerMid": "artist",
                "total": 1,
                "albumList": [{"albumID": 1, "albumMid": "fallback_mid", "albumName": "Album"}],
            }
        )
    )
    provider._qq_singer = SimpleNamespace(  # type: ignore[attr-defined]
        get_tab_detail=AsyncMock(
            return_value=HomepageTabDetailResponse.model_validate(
                {
                    "TabID": "album",
                    "HasMore": 0,
                    "NeedShowTab": 0,
                    "Order": 0,
                    "TabList": [],
                }
            )
        ),
        get_album_list=get_album_list,
    )

    async def _run_with_session(coro):
        return await coro

    provider._run_with_session = _run_with_session  # type: ignore[attr-defined]
    provider._parse_album = lambda item: item["mid"]  # type: ignore[attr-defined]

    albums = await QQMusicProvider.get_artist_albums.__wrapped__(provider, "artist")

    assert albums == ["fallback_mid"]
    get_album_list.assert_awaited_once_with("artist", num=100, page=1)


@pytest.mark.asyncio
async def test_get_artist_albums_falls_back_when_album_tab_is_unavailable() -> None:
    """An unsupported AlbumTab uses QQ Music's typed album-list endpoint."""
    provider = QQMusicProvider.__new__(QQMusicProvider)
    get_album_list = AsyncMock(
        return_value=SingerAlbumListResponse.model_validate(
            {
                "singerMid": "artist",
                "total": 1,
                "albumList": [{"albumID": 1, "albumMid": "fallback_mid", "albumName": "Album"}],
            }
        )
    )
    provider._qq_singer = SimpleNamespace(  # type: ignore[attr-defined]
        get_tab_detail=AsyncMock(side_effect=MediaNotFoundError("AlbumTab is unavailable")),
        get_album_list=get_album_list,
    )

    async def _run_with_session(coro):
        return await coro

    provider._run_with_session = _run_with_session  # type: ignore[attr-defined]
    provider._parse_album = lambda item: item["mid"]  # type: ignore[attr-defined]

    albums = await QQMusicProvider.get_artist_albums.__wrapped__(provider, "artist")

    assert albums == ["fallback_mid"]
    get_album_list.assert_awaited_once_with("artist", num=100, page=1)


@pytest.mark.asyncio
async def test_get_artist_uses_typed_homepage_response() -> None:
    """The provider reads singer detail from the public typed response field."""
    provider = QQMusicProvider.__new__(QQMusicProvider)
    provider.manifest = SimpleNamespace(domain="qqmusic")  # type: ignore[attr-defined]
    provider.config = SimpleNamespace(instance_id="qqmusic_instance")  # type: ignore[attr-defined]
    provider._qq_singer = SimpleNamespace(  # type: ignore[attr-defined]
        get_info=AsyncMock(
            return_value=HomepageHeaderResponse.model_validate(
                {
                    "Status": 0,
                    "Info": {
                        "Singer": {
                            "SingerID": 1,
                            "SingerMid": "artist",
                            "Name": "Artist",
                            "SingerType": 0,
                            "SingerPic": "https://image.example/artist.jpg",
                        },
                        "BaseInfo": {
                            "EncryptedUin": "",
                            "BackgroundImage": "",
                            "Avatar": "",
                            "Name": "Artist",
                            "IsHost": 0,
                            "IsSinger": 1,
                            "UserType": 0,
                        },
                    },
                    "TabDetail": {},
                }
            )
        )
    )

    async def _run_with_session(coro):
        return await coro

    provider._run_with_session = _run_with_session  # type: ignore[attr-defined]

    artist = await QQMusicProvider.get_artist.__wrapped__(provider, "artist")

    assert artist.item_id == "artist"
    assert artist.name == "Artist"
    assert artist.metadata.images[0].path == "https://image.example/artist.jpg"


@pytest.mark.asyncio
async def test_get_track_reads_typed_lyric_response() -> None:
    """Lyrics are consumed from the SDK model, which performs its own decoding."""
    provider = QQMusicProvider.__new__(QQMusicProvider)
    provider.manifest = SimpleNamespace(domain="qqmusic")  # type: ignore[attr-defined]
    provider.config = SimpleNamespace(instance_id="qqmusic_instance")  # type: ignore[attr-defined]
    provider.logger = Mock(level=INFO)
    provider._qq_song = SimpleNamespace(
        get_detail=AsyncMock(return_value=_stream_detail_response())
    )  # type: ignore[attr-defined]
    provider._qq_lyric = SimpleNamespace(
        get_lyric=AsyncMock(
            return_value=GetLyricResponse.model_validate(
                {
                    "songID": 1,
                    "lyric": "[00:01.00]line",
                    "trans": "[00:01.00]translation",
                }
            )
        )
    )

    async def _run_with_session(coro):
        return await coro

    provider._run_with_session = _run_with_session  # type: ignore[attr-defined]

    track = await QQMusicProvider.get_track.__wrapped__(provider, "track")

    assert track.metadata.lrc_lyrics == "[00:01.00]line"
    assert track.metadata.lyrics == "[00:01.00]line\n\n[00:01.00]translation"


def _stream_detail_response() -> GetSongDetailResponse:
    """Build a minimal public 0.8.2 track-detail response."""
    return GetSongDetailResponse.model_validate(
        {
            "track_info": {
                "id": 1,
                "mid": "track",
                "name": "Track",
                "type": 0,
                "singer": [],
                "album": {},
                "mv": {},
                "file": {},
                "pay": {},
                "interval": 0,
                "isonly": 0,
                "language": 0,
                "genre": 0,
                "index_cd": 0,
                "index_album": 0,
                "status": 0,
                "label": "",
                "bpm": 0,
                "ov": 0,
                "sa": 0,
                "es": "",
                "vs": [],
                "vi": [],
                "vf": [],
            }
        }
    )


def _url_response(
    *, result: int = 0, purl: str = "/M500track.mp3", expiration: int = 47
) -> GetSongUrlsResponse:
    """Build a public 0.8.2 song-URL response."""
    return GetSongUrlsResponse.model_validate(
        {
            "expiration": expiration,
            "midurlinfo": [
                {
                    "songmid": "track",
                    "filename": "M500track.mp3",
                    "purl": purl,
                    "vkey": "vkey",
                    "ekey": "",
                    "result": result,
                }
            ],
        }
    )


def _dispatch_response(
    *,
    sip: tuple[str, ...] = ("https://cdn.example/",),
    retcode: int = 0,
    refresh_time: int = 60,
    expiration: int = 90,
    cache_time: int = 120,
) -> GetCdnDispatchResponse:
    """Build a public 0.8.2 CDN dispatch response."""
    return GetCdnDispatchResponse.model_validate(
        {
            "retcode": retcode,
            "sip": list(sip),
            "keepalivefile": "keepalive",
            "refreshTime": refresh_time,
            "expiration": expiration,
            "cacheTime": cache_time,
        }
    )


class _FakeCache:
    """Minimal shared cache implementation for CDN dispatch tests."""

    def __init__(self) -> None:
        """Initialize the in-memory data and observable cache calls."""
        self.data: dict[tuple[str, str], object] = {}
        self.get = AsyncMock(side_effect=self._get)
        self.set = AsyncMock(side_effect=self._set)

    async def _get(
        self, key: str, *, provider: str = "default", allow_bypass: bool = False, **_kwargs: object
    ) -> object | None:
        """Return cached data, respecting the central refresh context."""
        if allow_bypass and BYPASS_CACHE.get():
            return None
        return self.data.get((provider, key))

    async def _set(
        self, key: str, data: object, *, provider: str = "default", **_kwargs: object
    ) -> None:
        """Store serializable test data."""
        self.data[(provider, key)] = data


def _stream_provider() -> QQMusicProvider:
    """Create a provider with no-op session handling for stream tests."""
    provider = QQMusicProvider.__new__(QQMusicProvider)
    provider.config = SimpleNamespace(  # type: ignore[attr-defined]
        instance_id="qqmusic_instance", get_value=lambda _key: "mp3_128"
    )
    provider._credential = Mock()
    provider.logger = Mock()
    provider.mass = SimpleNamespace(cache=_FakeCache())
    provider._cdn_dispatch_lock = asyncio.Lock()

    async def _run_with_session(coro):
        return await coro

    provider._run_with_session = _run_with_session  # type: ignore[attr-defined]
    return provider


@pytest.mark.asyncio
async def test_get_stream_details_uses_dispatch_sip_and_sdk_ttl() -> None:
    """The typed URL response uses dispatch SIP and its explicit expiration."""
    provider = _stream_provider()
    get_song_urls = AsyncMock(return_value=_url_response())
    get_cdn_dispatch = AsyncMock(return_value=_dispatch_response())
    provider._qq_song = SimpleNamespace(  # type: ignore[attr-defined]
        get_detail=AsyncMock(return_value=_stream_detail_response()),
        get_song_urls=get_song_urls,
        get_cdn_dispatch=get_cdn_dispatch,
    )

    details = await provider.get_stream_details("track", MediaType.TRACK)

    assert details.path == "https://cdn.example/M500track.mp3"
    assert details.expiration == 47
    get_song_urls.assert_awaited_once()
    get_cdn_dispatch.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_stream_details_rejects_nonzero_url_result() -> None:
    """An authorization failure must not be turned into a playable URL."""
    provider = _stream_provider()
    get_cdn_dispatch = AsyncMock()
    provider._qq_song = SimpleNamespace(  # type: ignore[attr-defined]
        get_detail=AsyncMock(return_value=_stream_detail_response()),
        get_song_urls=AsyncMock(return_value=_url_response(result=104003)),
        get_cdn_dispatch=get_cdn_dispatch,
    )

    with pytest.raises(UnplayableMediaError):
        await provider.get_stream_details("track", MediaType.TRACK)

    get_cdn_dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_stream_details_uses_default_expiration_without_sdk_ttl() -> None:
    """An omitted SDK TTL uses MA's default rather than parsing URL query parameters."""
    provider = _stream_provider()
    provider._qq_song = SimpleNamespace(  # type: ignore[attr-defined]
        get_detail=AsyncMock(return_value=_stream_detail_response()),
        get_song_urls=AsyncMock(
            return_value=_url_response(
                purl="https://cdn.example/M500track.mp3?Expires=1", expiration=0
            )
        ),
    )

    details = await provider.get_stream_details("track", MediaType.TRACK)

    assert details.expiration == 600


@pytest.mark.asyncio
async def test_get_cdn_base_rejects_failed_dispatch() -> None:
    """A failed public dispatch response must not yield one of its SIP values."""
    provider = _stream_provider()
    provider._qq_song = SimpleNamespace(  # type: ignore[attr-defined]
        get_cdn_dispatch=AsyncMock(return_value=_dispatch_response(retcode=1))
    )

    with pytest.raises(ResourceTemporarilyUnavailable):
        await provider._get_cdn_base()


@pytest.mark.asyncio
async def test_get_cdn_base_uses_shared_cache_with_dispatch_ttl() -> None:
    """The typed dispatch SIP uses MA's shared cache and the shortest upstream TTL."""
    provider = _stream_provider()
    get_cdn_dispatch = AsyncMock(
        return_value=_dispatch_response(
            sip=("https://one.example/",), refresh_time=10, expiration=45, cache_time=90
        )
    )
    provider._qq_song = SimpleNamespace(get_cdn_dispatch=get_cdn_dispatch)  # type: ignore[attr-defined]

    assert await provider._get_cdn_base() == "https://one.example"
    assert await provider._get_cdn_base() == "https://one.example"
    get_cdn_dispatch.assert_awaited_once()
    provider.mass.cache.set.assert_awaited_once_with(
        _CDN_DISPATCH_CACHE_KEY,
        ["https://one.example"],
        expiration=10,
        provider="qqmusic_instance",
    )
    assert all(
        call.kwargs["allow_bypass"] is True for call in provider.mass.cache.get.call_args_list
    )


@pytest.mark.asyncio
async def test_get_cdn_base_refresh_bypasses_shared_cache() -> None:
    """A forced refresh bypasses a cached dispatch SIP."""
    provider = _stream_provider()
    get_cdn_dispatch = AsyncMock(
        side_effect=[
            _dispatch_response(sip=("https://one.example/",)),
            _dispatch_response(sip=("https://two.example/",)),
        ]
    )
    provider._qq_song = SimpleNamespace(get_cdn_dispatch=get_cdn_dispatch)  # type: ignore[attr-defined]

    assert await provider._get_cdn_base() == "https://one.example"
    token = BYPASS_CACHE.set(True)
    try:
        assert await provider._get_cdn_base() == "https://two.example"
    finally:
        BYPASS_CACHE.reset(token)
    assert get_cdn_dispatch.await_count == 2


@pytest.mark.asyncio
async def test_get_cdn_base_coalesces_concurrent_cache_misses() -> None:
    """Concurrent CDN cache misses share one dispatch request."""
    provider = _stream_provider()
    dispatch_started = asyncio.Event()
    release_dispatch = asyncio.Event()

    async def _dispatch() -> GetCdnDispatchResponse:
        dispatch_started.set()
        await release_dispatch.wait()
        return _dispatch_response(sip=("https://one.example/",))

    get_cdn_dispatch = AsyncMock(side_effect=_dispatch)
    provider._qq_song = SimpleNamespace(get_cdn_dispatch=get_cdn_dispatch)  # type: ignore[attr-defined]

    first = asyncio.create_task(provider._get_cdn_base())
    await dispatch_started.wait()
    second = asyncio.create_task(provider._get_cdn_base())
    await asyncio.sleep(0)
    release_dispatch.set()

    assert await asyncio.gather(first, second) == ["https://one.example", "https://one.example"]
    get_cdn_dispatch.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_playlist_uses_typed_id_and_dirid() -> None:
    """The typed create response preserves distinct playlist and directory IDs."""
    provider = QQMusicProvider.__new__(QQMusicProvider)
    provider._credential = Mock()
    provider._qq_songlist = SimpleNamespace(
        create=AsyncMock(
            return_value=CreateDeleteSonglistResp.model_validate(
                {"retCode": 0, "result": {"tid": 123, "dirId": 456, "dirName": "New"}}
            )
        )
    )

    async def _run_with_session(coro):
        return await coro

    provider._run_with_session = _run_with_session  # type: ignore[attr-defined]
    provider.get_playlist = AsyncMock(return_value=Mock())  # type: ignore[method-assign]

    await provider.create_playlist("New", set())

    provider.get_playlist.assert_awaited_once_with("123:456")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "credential_json",
    [
        None,
        "not-json",
        Credential.model_validate(
            {"musicid": 123, "musickey": "key", "str_musicid": "123", "loginType": 2}
        ).model_dump_json(by_alias=True),
    ],
)
async def test_handle_async_init_requires_valid_complete_credential_json(
    credential_json: str | None,
) -> None:
    """Provider setup requires a complete credential_json from the app QR login."""
    provider = QQMusicProvider.__new__(QQMusicProvider)
    provider.logger = Mock(level=INFO)
    provider.get_setup_value = lambda key: {  # type: ignore[attr-defined]
        "credential_json": credential_json,
        "musicid": "123",
        "musickey": "key",
        "login_type": "2",
    }.get(key)
    provider._update_setup_data = Mock()

    with pytest.raises(LoginFailed, match="remove and re-add the integration"):
        await provider.handle_async_init()


@pytest.mark.asyncio
async def test_handle_async_init_uses_complete_credential_json() -> None:
    """A complete app credential initializes the provider."""
    provider = QQMusicProvider.__new__(QQMusicProvider)
    provider.logger = Mock(level=INFO)
    credential_json = Credential.model_validate(
        {
            "musicid": 123,
            "musickey": "key",
            "str_musicid": "123",
            "encryptUin": "encrypted-uin",
            "loginType": 2,
        }
    ).model_dump_json(by_alias=True)
    provider.get_setup_value = lambda key: {"credential_json": credential_json}.get(key)  # type: ignore[attr-defined]
    provider._update_setup_data = Mock()

    await provider.handle_async_init()

    assert provider._credential.encrypt_uin == "encrypted-uin"
    await provider._qq_client.close()
