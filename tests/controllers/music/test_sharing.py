"""Public sharing policy and endpoint regressions using synthetic account data."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from music_assistant_models.auth import Scope, User, UserRole
from music_assistant_models.config_entries import ProviderAccess
from music_assistant_models.enums import LinkType, MediaType, ProviderSharing, ProviderType
from music_assistant_models.errors import InvalidDataError, MediaNotFoundError
from music_assistant_models.media_items import (
    Album,
    Artist,
    BrowseFolder,
    MediaItemLink,
    ProviderMapping,
    Radio,
    Track,
)
from music_assistant_models.unique_list import UniqueList

from music_assistant.controllers.music import MusicController
from music_assistant.controllers.music.media.base import SUPPRESS_MEDIA_ITEM_UPDATES
from music_assistant.controllers.music.sharing import public_provider_url, share_text
from music_assistant.helpers.api import APICommandHandler
from music_assistant.providers.qobuz import QobuzProvider
from music_assistant.providers.soundcloud import SoundcloudMusicProvider
from music_assistant.providers.ytmusic import YoutubeMusicProvider
from tests.common import set_music_source_access as configure_music_source_access

SPOTIFY_ID = "4cOdK2wGLETKBW3PvgPWqT"
USER_PATH = "music_assistant.controllers.music.controller.get_current_user"


def set_music_source_access(
    mass: Any,
    access: dict[str, ProviderAccess | None],
    domains: dict[str, str] | None = None,
) -> None:
    """Configure sources with explicit provider domains for synthetic instance ids."""
    configure_music_source_access(mass, access)
    for instance, config in mass.config.get("providers").items():
        config["domain"] = (domains or {}).get(instance, "spotify")


def mapping(
    domain: str, item_id: str = "123", url: str | None = None, instance: str = "account"
) -> ProviderMapping:
    """Create a synthetic mapping."""
    return ProviderMapping(
        item_id=item_id, provider_domain=domain, provider_instance=instance, url=url
    )


@pytest.fixture
def controller() -> MusicController:
    """Create a controller without starting services or opening a database."""
    result = MusicController.__new__(MusicController)
    result.mass = Mock()
    result.mass.get_provider.return_value = Mock(
        domain="spotify", instance_id="account", type=ProviderType.MUSIC
    )
    set_music_source_access(result.mass, {"account": None})
    return result


@pytest.mark.parametrize(
    ("domain", "host"),
    [
        ("spotify", "open.spotify.com"),
        ("tidal", "tidal.com"),
        ("qobuz", "open.qobuz.com"),
        ("deezer", "www.deezer.com"),
    ],
)
@pytest.mark.parametrize("kind", [MediaType.ARTIST, MediaType.ALBUM, MediaType.TRACK])
def test_id_based_urls(domain: str, host: str, kind: MediaType) -> None:
    """Catalog URLs work without stored URL fields."""
    item_id = SPOTIFY_ID if domain == "spotify" else "12345"
    assert (
        public_provider_url(kind, mapping(domain, item_id))
        == f"https://{host}/{kind.value}/{item_id}"
    )


@pytest.mark.parametrize("domain", ["spotify", "tidal", "qobuz", "deezer", "ytmusic"])
@pytest.mark.parametrize(
    "item_id",
    [
        "",
        "../123",
        "123?api_key=secret",
        "https://127.0.0.1/audio",
        "123/stream",
        "123\n",
        "\uff11\uff12\uff13",
        "123#token",
    ],
)
def test_invalid_ids(domain: str, item_id: str) -> None:
    """IDs cannot inject URL paths, query strings or credentials."""
    assert (
        public_provider_url(MediaType.TRACK, mapping(domain, item_id), publicly_visible=True)
        is None
    )


@pytest.mark.parametrize(
    ("kind", "item_id", "url"),
    [
        (MediaType.ARTIST, "UC" + "a" * 22, "https://music.youtube.com/channel/UC" + "a" * 22),
        (MediaType.ALBUM, "MPREb_test", "https://music.youtube.com/browse/MPREb_test"),
    ],
)
def test_youtube_ids(kind: MediaType, item_id: str, url: str) -> None:
    """YouTube uses distinct album, channel, video and playlist identifiers."""
    assert public_provider_url(kind, mapping("ytmusic", item_id)) == url


def test_youtube_album_playlist() -> None:
    """Preserve the album audio playlist identity when the provider supplied it."""
    url = "https://music.youtube.com/playlist?list=OLAK5uy_test"
    assert public_provider_url(MediaType.ALBUM, mapping("ytmusic", "MPREb_test", url)) == url


@pytest.mark.parametrize(
    "item_id",
    [
        "LM",
        "LM🎵account",
        "SE",
        "RDPN",
        "RDTMAK5uy_mix",
        "RDTMAK5uy_mix🎵account",
        "VLprivate",
        "FEmusic_library_privately_owned_tracks",
    ],
)
def test_youtube_personal_collections(item_id: str) -> None:
    """Personal collections never turn into purported public pages."""
    assert public_provider_url(MediaType.PLAYLIST, mapping("ytmusic", item_id)) is None


@pytest.mark.parametrize(
    ("domain", "kind", "item_id", "url", "expected"),
    [
        (
            "apple_music",
            MediaType.ARTIST,
            "657515",
            "https://music.apple.com/gb/artist/name/657515?token=secret",
            "https://music.apple.com/gb/artist/657515",
        ),
        (
            "apple_music",
            MediaType.ALBUM,
            "1109714933",
            "https://music.apple.com/us/album/in-rainbows/1109714933",
            "https://music.apple.com/us/album/1109714933",
        ),
        (
            "apple_music",
            MediaType.TRACK,
            "987654321",
            "https://music.apple.com/us/album/name/1109714933?i=987654321&at=secret",
            "https://music.apple.com/us/album/1109714933?i=987654321",
        ),
        (
            "apple_music",
            MediaType.TRACK,
            "987654321",
            "https://music.apple.com/us/song/name/987654321",
            "https://music.apple.com/us/song/987654321",
        ),
        (
            "soundcloud",
            MediaType.ARTIST,
            "12",
            "https://soundcloud.com/some-artist?token=secret",
            "https://soundcloud.com/some-artist",
        ),
        (
            "bandcamp",
            MediaType.ARTIST,
            "12",
            "https://test.bandcamp.com/music",
            "https://test.bandcamp.com",
        ),
        (
            "bandcamp",
            MediaType.ALBUM,
            "123",
            "https://test.bandcamp.com/album/test-album?token=secret",
            "https://test.bandcamp.com/album/test-album",
        ),
        (
            "bandcamp",
            MediaType.TRACK,
            "123",
            "http://test.bandcamp.com/track/test-track#fragment",
            "https://test.bandcamp.com/track/test-track",
        ),
    ],
)
def test_verified_website_shapes(
    domain: str, kind: MediaType, item_id: str, url: str, expected: str
) -> None:
    """Reconstruct only known website routes, retaining actual track identity."""
    assert public_provider_url(kind, mapping(domain, item_id, url)) == expected


@pytest.mark.parametrize("domain", ["apple_music", "soundcloud", "bandcamp"])
@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/track/123",
        "https://[::1]/track/123",
        "https://192.168.1.2/track/123",
        "https://localhost/track/123",
        "https://server.local/track/123",
        "https://user:secret@soundcloud.com/artist/track",
        "https://soundcloud.com:443/artist/track",
        "https://soundcloud.com.evil.test/artist/track",
        "https://soundcloud.com/artist/track/s-secret",
        "https://soundcloud.com/artist/track?secret_token=s-secret",
        "https://api.soundcloud.com/tracks/123/stream",
        "https://soundcloud.com/tracks/123",
        "https://soundcloud.com/artist/download",
        "https://test.bandcamp.com/download/track",
        "https://test.bandcamp.com/track/test.mp3",
        "https://music.apple.com/us/api/123",
        "https://music.apple.com/us/album/name/123?i=999",
        "https://music.apple.com/us/song/999",
        "https://music.apple.com/us/song/name/123/extra",
        "https://soundcloud.com/artist/%2e%2e",
        "https://soundcloud.com\\@evil.test/artist/track",
        "https://soundcloud.com/artist/track\n",
        "https://[invalid",
        "file:///private/audio.mp3",
        "https://test.bandcamp.com/track/../download",
    ],
)
def test_unsafe_stored_urls(domain: str, url: str) -> None:
    """Protected, malformed, private, stream and API locations fail closed."""
    assert public_provider_url(MediaType.TRACK, mapping(domain, url=url)) is None


@pytest.mark.parametrize(
    "domain",
    [
        "jellyfin",
        "plex",
        "filesystem_local",
        "phishin",
        "pocketcasts",
        "podcastfeed",
        "podcast_index",
        "nugs",
        "internet_archive",
        "unsupported",
    ],
)
def test_no_generic_mapping_passthrough(domain: str) -> None:
    """Stored addresses never pass through, even when a catalog ID produces a page."""
    for url in [
        "https://example.org/audio.mp3?api_key=secret",
        "https://open.spotify.com/track/" + SPOTIFY_ID,
    ]:
        expected = "https://archive.org/details/123" if domain == "internet_archive" else None
        assert public_provider_url(MediaType.TRACK, mapping(domain, url=url)) == expected


async def test_endpoint_multiple_links(controller: Any) -> None:
    """Library IDs stay distinct; mapping URLs dedupe across accounts in stable order."""
    set_music_source_access(
        controller.mass,
        {"spotify-a": None, "spotify-b": None, "tidal": None},
        {"tidal": "tidal"},
    )
    maps = {
        mapping("spotify", SPOTIFY_ID, instance="spotify-b"),
        mapping("spotify", SPOTIFY_ID, instance="spotify-a"),
        mapping("tidal", "42", instance="tidal"),
    }
    item = Track(item_id="library-id", provider="library", name="Song", provider_mappings=maps)
    controller.get_item_by_uri = AsyncMock(return_value=item)
    controller.mass.get_provider.side_effect = lambda instance: Mock(
        domain="tidal" if instance == "tidal" else "spotify",
        instance_id=instance,
        type=ProviderType.MUSIC,
    )
    result = await controller.get_item_share_info("library://track/library-id")
    assert result == {
        "title": "Song",
        "text": None,
        "links": [
            {
                "provider_domain": "spotify",
                "provider_instance": "spotify-a",
                "url": "https://open.spotify.com/track/" + SPOTIFY_ID,
            },
            {
                "provider_domain": "tidal",
                "provider_instance": "tidal",
                "url": "https://tidal.com/track/42",
            },
        ],
    }
    controller.get_item_by_uri.assert_awaited_once_with(
        "library://track/library-id", allow_update_metadata=False, recursive=False
    )
    assert json.loads(json.dumps(result)) == result


async def test_provider_item_lookup(controller: Any) -> None:
    """A library miss uses a strict provider lookup with updates suppressed."""
    item = Track(
        item_id=SPOTIFY_ID,
        provider="account",
        name="Song",
        provider_mappings={mapping("spotify", SPOTIFY_ID)},
    )

    async def lookup(*_args: object, **kwargs: object) -> Track:
        assert SUPPRESS_MEDIA_ITEM_UPDATES.get()
        assert kwargs["strict_provider_instance"] is True
        assert kwargs["allow_fallback"] is False
        return item

    controller.tracks = Mock()
    controller.tracks.get_library_item_by_prov_id = AsyncMock(return_value=None)
    controller.tracks.get_provider_item = AsyncMock(side_effect=lookup)
    result = await controller.get_item_share_info(item.uri)
    assert result["links"][0]["url"].endswith(SPOTIFY_ID)
    controller.tracks.get_provider_item.assert_awaited_once()
    controller.mass.metadata.schedule_update_metadata.assert_not_called()
    assert not SUPPRESS_MEDIA_ITEM_UPDATES.get()


@pytest.mark.parametrize("domain", ["filesystem_local", "plex", "jellyfin"])
async def test_text_fallback(controller: Any, domain: str) -> None:
    """Private media shares only artist/title/album, excluding paths and credentials."""
    set_music_source_access(controller.mass, {"account": None}, {"account": domain})
    controller.mass.get_provider.return_value.domain = domain
    item = Track(
        item_id="123",
        provider="library",
        name="Song",
        provider_mappings={mapping(domain, url="http://192.168.1.1/audio.mp3?api_key=secret")},
        artists=UniqueList(
            [Artist(item_id="a", provider="account", name="Artist", provider_mappings=set())]
        ),
        album=Album(item_id="b", provider="account", name="Album", provider_mappings=set()),
    )
    controller.get_item_by_uri = AsyncMock(return_value=item)
    result = await controller.get_item_share_info(item.uri)
    assert result == {"title": "Song", "text": "Artist - Song (Album)", "links": []}
    assert "secret" not in json.dumps(result)


async def test_hidden_provider_before_lookup(controller: Any) -> None:
    """A forbidden source fails before any provider or item fetch."""
    user = User(user_id="reader", username="reader", role=UserRole.USER)
    set_music_source_access(
        controller.mass, {"account": ProviderAccess(owner="other", sharing=ProviderSharing.PRIVATE)}
    )
    controller.get_item_by_uri = AsyncMock()
    with (
        patch(USER_PATH, return_value=user),
        pytest.raises(MediaNotFoundError, match="Item not found"),
    ):
        await controller.get_item_share_info("account://track/123")
    controller.get_item_by_uri.assert_not_awaited()


async def test_hidden_library_mappings(controller: Any) -> None:
    """Filter hidden mappings even when the provider resolver falls back to another account."""
    user = User(user_id="reader", username="reader", role=UserRole.USER)
    set_music_source_access(
        controller.mass,
        {"hidden": ProviderAccess(owner="other", sharing=ProviderSharing.PRIVATE), "account": None},
    )
    item = Track(
        item_id="123",
        provider="library",
        name="Song",
        provider_mappings={
            mapping("spotify", SPOTIFY_ID, instance="hidden"),
            mapping("spotify", "a" * 22),
        },
    )
    controller.get_item_by_uri = AsyncMock(return_value=item)
    with patch(USER_PATH, return_value=user):
        result = await controller.get_item_share_info(item.uri)
    assert [link["provider_instance"] for link in result["links"]] == ["account"]
    item.provider_mappings = {mapping("spotify", SPOTIFY_ID, instance="hidden")}
    with patch(USER_PATH, return_value=user), pytest.raises(MediaNotFoundError):
        await controller.get_item_share_info(item.uri)


@pytest.mark.parametrize(
    "uri",
    [
        "https://example.org/audio.mp3?api_key=secret",
        "https://open.spotify.com/track/" + SPOTIFY_ID,
        "builtin://radio/https://example.org/audio",
        "account://folder/123",
        "account://unknown/123",
        "account://audio_source/123",
        "/private/audio.mp3",
        "spotify:track:" + SPOTIFY_ID,
    ],
)
async def test_reject_before_lookup(controller: Any, uri: str) -> None:
    """Sharing never probes arbitrary URLs, folders, files or audio sources."""
    controller.get_item_by_uri = AsyncMock()
    with pytest.raises(InvalidDataError):
        await controller.get_item_share_info(uri)
    controller.get_item_by_uri.assert_not_awaited()


async def test_lookup_failure_resets_suppression(controller: Any) -> None:
    """Exceptions leave the caller's library update context intact."""
    controller.get_item_by_uri = AsyncMock(side_effect=MediaNotFoundError("missing"))
    with pytest.raises(MediaNotFoundError):
        await controller.get_item_share_info("library://track/123")
    assert not SUPPRESS_MEDIA_ITEM_UPDATES.get()


async def test_reject_resolved_folder(controller: Any) -> None:
    """An unexpected folder response cannot become sharing data."""
    controller.get_item_by_uri = AsyncMock(
        return_value=BrowseFolder(item_id="123", provider="account", name="Folder")
    )
    with pytest.raises(InvalidDataError):
        await controller.get_item_share_info("library://track/123")


async def test_radio_station_identity(controller: Any) -> None:
    """Radio sharing reads station metadata and never asks for stream/ICY details."""
    set_music_source_access(controller.mass, {"account": None}, {"account": "jellyfin"})
    controller.mass.get_provider.return_value.domain = "jellyfin"
    station = Radio(
        item_id="station",
        provider="library",
        name="Station",
        provider_mappings={mapping("jellyfin", url="https://example.org/live.mp3")},
    )
    controller.get_item_by_uri = AsyncMock(return_value=station)
    assert await controller.get_item_share_info(station.uri) == {
        "title": "Station",
        "text": "Station",
        "links": [],
    }
    controller.mass.streams.get_stream_details.assert_not_called()


def test_endpoint_auth_contract(controller: Any) -> None:
    """The API dispatcher requires authentication and library read scope."""
    handler = APICommandHandler.parse(
        "music/share_item",
        controller.get_item_share_info,
        authenticated=controller.get_item_share_info.api_authenticated,
        required_scope=controller.get_item_share_info.api_required_scope,
    )
    assert handler.authenticated
    assert handler.required_scope == Scope.LIBRARY_READ
    assert controller.get_item_share_info.api_cmd == "music/share_item"


def test_fallback_has_no_addresses() -> None:
    """Labels cannot introduce raw URL credentials into text fallback."""
    item = Track(
        item_id="123",
        provider="library",
        name="Song https://host/audio?api_key=secret\n",
        provider_mappings=set(),
    )
    assert share_text(item) == "Song"


async def test_builtin_provider_alias_cannot_probe(controller: Any) -> None:
    """A builtin instance alias cannot sneak a raw URL into item lookup."""
    controller.mass.get_provider.return_value.domain = "builtin"
    controller.get_item_by_uri = AsyncMock()
    with pytest.raises(InvalidDataError):
        await controller.get_item_share_info("builtin-instance://radio/https://example.org/live")
    controller.get_item_by_uri.assert_not_awaited()


async def test_resolved_provider_must_be_visible(controller: Any) -> None:
    """A domain lookup may not resolve through a hidden loaded account."""
    user = User(user_id="reader", username="reader", role=UserRole.USER)
    set_music_source_access(
        controller.mass, {"account": ProviderAccess(owner="other", sharing=ProviderSharing.PRIVATE)}
    )
    controller.get_item_by_uri = AsyncMock()
    with patch(USER_PATH, return_value=user), pytest.raises(MediaNotFoundError):
        await controller.get_item_share_info("spotify://track/" + SPOTIFY_ID)
    controller.get_item_by_uri.assert_not_awaited()


async def test_metadata_links_are_not_share_contract(controller: Any) -> None:
    """Public-looking metadata links never override unsupported provider policy."""
    set_music_source_access(controller.mass, {"account": None}, {"account": "jellyfin"})
    controller.mass.get_provider.return_value.domain = "jellyfin"
    item = Track(
        item_id="123", provider="library", name="Song", provider_mappings={mapping("jellyfin")}
    )
    item.metadata.links = {
        MediaItemLink(type=LinkType.WEBSITE, url="https://open.spotify.com/track/" + SPOTIFY_ID)
    }
    controller.get_item_by_uri = AsyncMock(return_value=item)
    assert (await controller.get_item_share_info("library://track/123"))["links"] == []


@pytest.mark.parametrize("domain", ["ytmusic", "qobuz"])
async def test_private_provider_playlist(controller: Any, domain: str) -> None:
    """Real provider parsers cannot turn private playlists into public destinations."""
    provider = Mock(domain=domain, instance_id="account", name="Account")
    if domain == "ytmusic":
        item = YoutubeMusicProvider._parse_playlist(
            provider, {"id": "PLprivate123", "title": "Private playlist", "privacy": "PRIVATE"}
        )
    else:
        provider._user_auth_info = {"user": {"id": 1}}
        provider._QobuzProvider__get_image.return_value = None
        item = QobuzProvider._parse_playlist(
            provider,
            {
                "id": 123,
                "name": "Private playlist",
                "owner": {"id": 1, "name": "Account"},
                "is_collaborative": False,
                "is_public": 0,
            },
        )
    assert next(iter(item.provider_mappings)).is_unique
    set_music_source_access(controller.mass, {"account": None}, {"account": domain})
    controller.get_item_by_uri = AsyncMock(return_value=item)
    assert await controller.get_item_share_info("library://playlist/123") == {
        "title": "Private playlist",
        "text": "Private playlist",
        "links": [],
    }


@pytest.mark.parametrize("domain", ["spotify", "filesystem_local", "jellyfin"])
async def test_configured_unavailable_source(controller: Any, domain: str) -> None:
    """Library sharing keeps authorized metadata and public links while sources are offline."""
    set_music_source_access(controller.mass, {"account": None}, {"account": domain})
    controller.mass.config.get("providers")["account"]["enabled"] = False
    controller.mass.get_provider.return_value = None
    item = Track(
        item_id="123",
        provider="library",
        name="Song",
        provider_mappings={mapping(domain, SPOTIFY_ID)},
    )
    controller.get_item_by_uri = AsyncMock(return_value=item)
    user = User(user_id="reader", username="reader", role=UserRole.USER)
    with patch(USER_PATH, return_value=user):
        result = await controller.get_item_share_info(item.uri)
    assert result["text"] == (None if domain == "spotify" else "Song")
    assert bool(result["links"]) == (domain == "spotify")


@pytest.mark.parametrize(
    "source",
    [None, {"type": "player", "domain": "spotify"}, {"type": "music", "domain": "jellyfin"}],
)
async def test_mapping_requires_configured_matching_source(controller: Any, source: Any) -> None:
    """Orphaned mappings and mismatched source identities do not grant access."""
    configs = controller.mass.config.get("providers")
    configs.clear()
    if source:
        configs["account"] = source
    controller.get_item_by_uri = AsyncMock(
        return_value=Track(
            item_id="123",
            provider="library",
            name="Song",
            provider_mappings={mapping("spotify", SPOTIFY_ID)},
        )
    )
    with pytest.raises(MediaNotFoundError):
        await controller.get_item_share_info("library://track/123")


@pytest.mark.parametrize(
    "domain",
    ["spotify", "tidal", "deezer", "qobuz", "ytmusic", "apple_music", "soundcloud", "bandcamp"],
)
def test_playlists_use_text_only(domain: str) -> None:
    """A playlist identifier or website URL does not prove public visibility."""
    assert public_provider_url(MediaType.PLAYLIST, mapping(domain, SPOTIFY_ID)) is None


@pytest.mark.parametrize("uri", [None, 123, [], {}, "account://not_a_media_type/123"])
async def test_invalid_uri_is_invalid_data(controller: Any, uri: Any) -> None:
    """Invalid input uses the API's client-error exception before lookup."""
    controller.get_item_by_uri = AsyncMock()
    with pytest.raises(InvalidDataError):
        await controller.get_item_share_info(uri)
    controller.get_item_by_uri.assert_not_awaited()


@pytest.mark.parametrize("visibility", ["private", "public"])
async def test_soundcloud_tracks_use_text(controller: Any, visibility: str) -> None:
    """Parsed track permalinks do not retain enough information to prove public visibility."""
    provider = Mock(domain="soundcloud", instance_id="account")
    provider._soundcloud.get_user_details = AsyncMock(return_value={"id": 12})
    provider._parse_artist = AsyncMock(
        return_value=Artist(
            item_id="12", provider="soundcloud", name="Artist", provider_mappings=set()
        )
    )
    item = await SoundcloudMusicProvider._parse_track(
        provider,
        {
            "id": 123,
            "title": "Private draft",
            "duration": 120000,
            "permalink_url": "https://soundcloud.com/artist/private-draft",
            "sharing": visibility,
            "secret_token": "s-private-secret" if visibility == "private" else None,
            "user": {"id": 12},
        },
    )
    assert next(iter(item.provider_mappings)).url == "https://soundcloud.com/artist/private-draft"
    set_music_source_access(controller.mass, {"account": None}, {"account": "soundcloud"})
    controller.mass.get_provider.return_value.domain = "soundcloud"
    controller.tracks = Mock()
    controller.tracks.get_library_item_by_prov_id = AsyncMock(return_value=None)
    controller.tracks.get_provider_item = AsyncMock(return_value=item)
    assert await controller.get_item_share_info(item.uri) == {
        "title": "Private draft",
        "text": "Artist - Private draft",
        "links": [],
    }
