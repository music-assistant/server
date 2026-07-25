"""Unit tests for QQ Music provider utility helpers."""

# mypy: ignore-errors

from __future__ import annotations

from types import SimpleNamespace

import pytest
from music_assistant_models.enums import ProviderFeature
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import Album
from qqmusic_api.modules.song import SongFileType

from music_assistant.providers.qqmusic import (
    SUPPORTED_FEATURES,
    QQMusicProvider,
    get_config_entries,
)
from music_assistant.providers.qqmusic.constants import (
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


def test_extract_song_id_from_track_payload() -> None:
    """Track parser helper should extract numeric song id from common keys."""
    provider = QQMusicProvider.__new__(QQMusicProvider)
    assert provider._extract_song_id({"id": 123}) == 123
    assert provider._extract_song_id({"songid": "456"}) == 456
    assert provider._extract_song_id({"songID": "789"}) == 789
    assert provider._extract_song_id({"song_id": "bad"}) is None


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


def test_get_artist_mapping_with_string_singer_returns_none() -> None:
    """String-only singer payload should not create clickable artist mapping."""
    provider = QQMusicProvider.__new__(QQMusicProvider)
    provider.config = SimpleNamespace(instance_id="qqmusic_instance")  # type: ignore[attr-defined]
    mapping = provider._get_artist_mapping("王力宏")
    assert mapping is None


def test_parse_artist_with_singer_name_and_highlight() -> None:
    """Artist parser should map singerName and strip search highlight tags."""
    provider = QQMusicProvider.__new__(QQMusicProvider)
    provider.manifest = SimpleNamespace(domain="qqmusic")  # type: ignore[attr-defined]
    provider.config = SimpleNamespace(instance_id="qqmusic_instance")  # type: ignore[attr-defined]
    artist = provider._parse_artist(
        {
            "singerMid": "003Nz2So3XXYek",
            "singerName": "<em>王力宏</em>",
            "subtitle": "华语流行男歌手",
        }
    )
    assert artist.item_id == "003Nz2So3XXYek"
    assert artist.name == "王力宏"
    assert artist.metadata.description == "华语流行男歌手"


def test_parse_artist_uses_avatar_and_description() -> None:
    """Artist parser should accept avatar url fields."""
    provider = QQMusicProvider.__new__(QQMusicProvider)
    provider.manifest = SimpleNamespace(domain="qqmusic")  # type: ignore[attr-defined]
    provider.config = SimpleNamespace(instance_id="qqmusic_instance")  # type: ignore[attr-defined]
    artist = provider._parse_artist(
        {
            "singerMid": "003Nz2So3XXYek",
            "Name": "王力宏",
            "Avatar": "//y.qq.com/music/photo_new/T001R500x500M000003Nz2So3XXYek.jpg",
            "desc": "华语流行歌手",
        }
    )
    assert artist.name == "王力宏"
    assert artist.metadata.images
    assert artist.metadata.images[0].path.startswith("https://")


def test_parse_playlist_name_strips_highlight_tags() -> None:
    """Playlist parser should strip highlight tags from search title."""
    provider = QQMusicProvider.__new__(QQMusicProvider)
    provider.manifest = SimpleNamespace(domain="qqmusic")  # type: ignore[attr-defined]
    provider.config = SimpleNamespace(instance_id="qqmusic_instance")  # type: ignore[attr-defined]
    playlist = provider._parse_playlist({"id": 123, "title": "<em>王力宏</em>精选"})
    assert playlist.name == "王力宏精选"


def test_parse_playlist_detail_fields() -> None:
    """Playlist parser should support dirname, normalized cover and description fields."""
    provider = QQMusicProvider.__new__(QQMusicProvider)
    provider.manifest = SimpleNamespace(domain="qqmusic")  # type: ignore[attr-defined]
    provider.config = SimpleNamespace(instance_id="qqmusic_instance")  # type: ignore[attr-defined]
    playlist = provider._parse_playlist(
        {
            "dissid": 7843129912,
            "dirid": 10,
            "dirname": "我的收藏",
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


def test_parse_track_album_mapping_accepts_album_mid_variants() -> None:
    """Track parser should map album when album dict uses albumMid-style fields."""
    provider = QQMusicProvider.__new__(QQMusicProvider)
    provider.manifest = SimpleNamespace(domain="qqmusic")  # type: ignore[attr-defined]
    provider.config = SimpleNamespace(instance_id="qqmusic_instance")  # type: ignore[attr-defined]

    track = provider._parse_track(
        {
            "mid": "003aAYrm3GE0Ac",
            "title": "稻香",
            "singer": [{"mid": "0025NhlN2yWrP4", "name": "周杰伦"}],
            "album": {"albumMid": "001qu4I30eVFYb", "title": "魔杰座"},
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
    """Album parser should map album subtitle/version and description fields."""
    provider = QQMusicProvider.__new__(QQMusicProvider)
    provider.manifest = SimpleNamespace(domain="qqmusic")  # type: ignore[attr-defined]
    provider.config = SimpleNamespace(instance_id="qqmusic_instance")  # type: ignore[attr-defined]

    album = provider._parse_album(
        {
            "albumMid": "001qu4I30eVFYb",
            "title": "唯一",
            "albumTranName": "纪念版",
            "description": "经典专辑",
            "singer": [{"singerMid": "003Nz2So3XXYek", "singerName": "王力宏"}],
        }
    )
    assert album.name == "唯一"
    assert album.version == "纪念版"
    assert album.metadata.description == "经典专辑"


def test_parse_track_album_mapping_accepts_album_id_fallback() -> None:
    """Track parser should map album from track-level albumID/albumTitle fields."""
    provider = QQMusicProvider.__new__(QQMusicProvider)
    provider.manifest = SimpleNamespace(domain="qqmusic")  # type: ignore[attr-defined]
    provider.config = SimpleNamespace(instance_id="qqmusic_instance")  # type: ignore[attr-defined]

    track = provider._parse_track(
        {
            "mid": "003aAYrm3GE0Ac",
            "title": "稻香",
            "albumID": 123456,
            "albumTitle": "魔杰座",
            "singerMid": "0025NhlN2yWrP4",
            "singerName": "周杰伦",
        }
    )
    assert track.album is not None
    assert isinstance(track.album, Album)
    assert track.album.item_id == "123456"
    assert track.album.name == "魔杰座"
    assert track.artists
    assert track.artists[0].name == "周杰伦"


@pytest.mark.asyncio
async def test_get_config_entries_exposes_quality_option_without_actions() -> None:
    """Migrated config entries expose only the quality option and no auth/QR actions."""
    entries = await get_config_entries(mass=None)
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
async def test_get_artist_albums_fallback_uses_album_list_on_tab_error() -> None:
    """Artist albums should fallback to album_list when AlbumTab endpoint fails."""
    provider = QQMusicProvider.__new__(QQMusicProvider)

    class _DummyTabType:
        ALBUM = "album"

    async def _raise_not_found(*_args, **_kwargs):
        raise MediaNotFoundError("not found")

    async def _get_album_list(*_args, **_kwargs):
        return {
            "albumList": [
                {"albumMID": "alb_mid_1", "name": "专辑A"},
                {"mid": "alb_mid_2", "name": "专辑B"},
            ]
        }

    provider._qq_singer = SimpleNamespace(  # type: ignore[attr-defined]
        TabType=_DummyTabType,
        get_tab_detail=_raise_not_found,
        get_album_list=_get_album_list,
        get_album_list_all=lambda *_args, **_kwargs: [],
    )

    async def _run_with_session(coro):
        return await coro

    provider._run_with_session = _run_with_session  # type: ignore[attr-defined]
    provider._extract_items = QQMusicProvider._extract_items.__get__(provider, QQMusicProvider)  # type: ignore[attr-defined]
    provider._parse_album = lambda item: str(item.get("albumMID") or item.get("mid"))  # type: ignore[attr-defined]

    albums = await QQMusicProvider.get_artist_albums.__wrapped__(provider, "003Nz2So3XXYek")
    assert albums == ["alb_mid_1", "alb_mid_2"]
