"""Sharing keeps the authorized account across real library lookup suspension points."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from copy import deepcopy
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.config_entries import ProviderAccess
from music_assistant_models.enums import ProviderSharing, ProviderType
from music_assistant_models.errors import MediaNotFoundError, ProviderUnavailableError
from music_assistant_models.media_items import Album, Artist, ProviderMapping, Track, UniqueList

from music_assistant.controllers.music.constants import CACHE_CATEGORY_SHARE_VISIBILITY
from music_assistant.controllers.webserver.helpers.auth_middleware import (
    get_current_user,
    set_current_user,
)
from music_assistant.mass import MusicAssistant
from music_assistant.providers.apple_music.parsers import parse_artist
from tests.common import set_music_source_access
from tests.controllers.music.test_musicbrainz_release_group import (
    RELEASE_GROUP_ID,
    SPOTIFY_ALBUM_ID,
    SPOTIFY_ALBUM_URL,
    _edition,
    _release,
)
from tests.controllers.music.test_sharing import SPOTIFY_ID, USER_PATH, mapping

YOUTUBE_ID = "AjXQiKP5kMs"


@pytest.fixture
async def reader() -> AsyncIterator[None]:
    """Use the real request context for source checks throughout nested controllers."""
    previous = get_current_user()
    set_current_user(User(user_id="reader", username="reader", role=UserRole.USER))
    try:
        yield
    finally:
        set_current_user(previous)


@pytest.mark.parametrize("source", ["spotify", "spotify--accessible"])
@pytest.mark.parametrize("disconnect", ["offline", "removed", "private", "config_removed"])
async def test_disconnected_share_source_never_fetches_hidden_account(
    music_mass: MusicAssistant, source: str, disconnect: str
) -> None:
    """A library miss cannot redirect a sharing request to another user's live account."""
    private = ProviderAccess(owner="other", sharing=ProviderSharing.PRIVATE)
    set_music_source_access(music_mass, {"spotify--accessible": None, "spotify--hidden": private})
    providers = {}
    for instance in ("spotify--accessible", "spotify--hidden"):
        providers[instance] = Mock(
            instance_id=instance,
            domain="spotify",
            type=ProviderType.MUSIC,
            available=True,
            is_streaming_provider=True,
            get_artist=AsyncMock(
                return_value=Artist(
                    item_id=SPOTIFY_ID,
                    provider=instance,
                    name="Artist",
                    provider_mappings={mapping("spotify", SPOTIFY_ID, instance=instance)},
                )
            ),
        )
    music_mass._providers.update(providers)
    lookup = music_mass.music.artists.get_library_item_by_prov_id
    lookup_started, disconnected = asyncio.Event(), asyncio.Event()

    async def library_lookup(item_id: str, provider_id: str) -> Artist | None:
        lookup_started.set()
        await disconnected.wait()
        return await lookup(item_id, provider_id)

    user = User(user_id="reader", username="reader", role=UserRole.USER)
    with (
        patch(USER_PATH, return_value=user),
        patch.object(
            music_mass.music.artists, "get_library_item_by_prov_id", side_effect=library_lookup
        ),
    ):
        request = asyncio.create_task(
            music_mass.music.get_item_share_info(f"{source}://artist/{SPOTIFY_ID}")
        )
        await asyncio.wait_for(lookup_started.wait(), timeout=5)
        if disconnect == "config_removed":
            _revoke_sources(music_mass, ["spotify--accessible"], disconnect)
        elif disconnect == "removed":
            music_mass._providers.pop("spotify--accessible")
        elif disconnect == "private":
            music_mass.config.set("providers/spotify--accessible/access", private.to_dict())
        else:
            providers["spotify--accessible"].available = False
        disconnected.set()
        with pytest.raises((MediaNotFoundError, ProviderUnavailableError)):
            await request
    providers["spotify--hidden"].get_artist.assert_not_awaited()
    providers["spotify--accessible"].get_artist.assert_not_awaited()


@pytest.mark.usefixtures("reader")
@pytest.mark.parametrize("revocation", ["private", "removed"])
@pytest.mark.parametrize("public", [False, True])
@pytest.mark.parametrize("sources", ["youtube", "both", "youtube_survives", "duplicate_spotify"])
async def test_share_rechecks_all_sources_after_visibility_read(
    music_mass_with_cache: MusicAssistant, revocation: str, public: bool, sources: str
) -> None:
    """A cache await cannot retain revoked links, instances or otherwise private metadata."""
    mass = music_mass_with_cache
    mass.streams = Mock(audio_analysis=Mock(get_track_audio_metadata=AsyncMock(return_value=None)))
    instances = ["ytmusic--account"]
    if sources != "youtube":
        instances.append("spotify--account")
    if sources == "duplicate_spotify":
        instances.append("spotify--second")
    set_music_source_access(mass, dict.fromkeys(instances))
    track = Track(
        item_id=YOUTUBE_ID,
        provider="ytmusic--account",
        name="Protected title",
        provider_mappings={
            mapping(
                instance.split("--")[0],
                YOUTUBE_ID if instance.startswith("ytmusic") else SPOTIFY_ID,
                instance=instance,
            )
            for instance in instances
        },
        artists=UniqueList(
            [
                Artist(
                    item_id="artist",
                    provider="ytmusic--account",
                    name="Artist",
                    provider_mappings={mapping("ytmusic", "artist", instance="ytmusic--account")},
                )
            ]
        ),
    )
    library_track = await mass.music.tracks.add_item_to_library(track)
    assert library_track.uri is not None
    providers = {
        instance: Mock(
            instance_id=instance,
            domain=instance.split("--")[0],
            type=ProviderType.MUSIC,
            available=True,
            is_streaming_provider=True,
            get_track=AsyncMock(return_value=track),
        )
        for instance in instances
    }
    mass._providers.update(providers)
    await mass.cache.set(
        f"track.{YOUTUBE_ID}",
        public,
        provider="ytmusic--account",
        category=CACHE_CATEGORY_SHARE_VISIBILITY,
    )
    assert mass.cache.database is not None
    get_row = mass.cache.database.get_row
    reading, resume = asyncio.Event(), asyncio.Event()

    async def visibility_read(*args: Any, **kwargs: Any) -> Any:
        row = await get_row(*args, **kwargs)
        if row and row["category"] == CACHE_CATEGORY_SHARE_VISIBILITY:
            reading.set()
            await resume.wait()
        return row

    with patch.object(mass.cache.database, "get_row", side_effect=visibility_read):
        request = asyncio.create_task(mass.music.get_item_share_info(library_track.uri))
        request.add_done_callback(lambda _: reading.set())
        try:
            await asyncio.wait_for(reading.wait(), timeout=5)
            if request.done():
                await request
                pytest.fail("Sharing finished before reading visibility")
            revoked = (
                ["spotify--account"]
                if sources == "youtube_survives"
                else [instance for instance in instances if instance != "spotify--second"]
            )
            _revoke_sources(mass, revoked, revocation)
            resume.set()
            if sources in {"youtube", "both"}:
                with pytest.raises(MediaNotFoundError):
                    await request
            else:
                result = await request
                expected = []
                if sources == "duplicate_spotify":
                    expected = [
                        {
                            "provider_domain": "spotify",
                            "provider_instance": "spotify--second",
                            "url": f"https://open.spotify.com/track/{SPOTIFY_ID}",
                        }
                    ]
                elif public:
                    expected = [
                        {
                            "provider_domain": "ytmusic",
                            "provider_instance": "ytmusic--account",
                            "url": f"https://music.youtube.com/watch?v={YOUTUBE_ID}",
                        }
                    ]
                assert result == {
                    "title": "Protected title",
                    "text": None if expected else "Artist - Protected title",
                    "links": expected,
                }
        finally:
            resume.set()
            await asyncio.gather(request, return_exceptions=True)
    for provider in providers.values():
        provider.get_track.assert_not_awaited()


@pytest.mark.usefixtures("reader")
@pytest.mark.parametrize("revocation", ["private", "removed", "config_removed", "offline"])
async def test_musicbrainz_rechecks_candidate_after_library_lookup(
    music_mass_with_cache: MusicAssistant, revocation: str
) -> None:
    """Real release resolution must not fetch an account revoked during its library miss."""
    await assert_candidate_source_rechecked(
        music_mass_with_cache,
        revocation,
        lambda: music_mass_with_cache.music.get_item_share_info(
            f"musicbrainz://album/{RELEASE_GROUP_ID}"
        ),
    )


@pytest.mark.usefixtures("reader")
@pytest.mark.parametrize("outcome", ["online", "library_offline", "disconnect", "private"])
async def test_domain_share_selects_and_pins_authorized_online_account(
    music_mass: MusicAssistant, outcome: str
) -> None:
    """A parser domain URI prefers an allowed online account and keeps offline library hits."""
    mass = music_mass
    private = ProviderAccess(owner="other", sharing=ProviderSharing.PRIVATE)
    set_music_source_access(
        mass,
        {
            "apple_music--offline": None,
            "apple_music--hidden": private,
            "apple_music--online": None,
        },
    )
    providers = {}
    for instance in ("apple_music--offline", "apple_music--hidden", "apple_music--online"):
        provider = Mock(
            instance_id=instance,
            domain="apple_music",
            type=ProviderType.MUSIC,
            available=instance != "apple_music--offline",
            is_streaming_provider=True,
        )
        artist = parse_artist(
            provider,
            {
                "id": "123",
                "attributes": {
                    "name": "Artist",
                    "url": "https://music.apple.com/us/artist/artist/123",
                },
            },
        )
        assert isinstance(artist, Artist)
        assert artist.provider == "apple_music"
        provider.get_artist = AsyncMock(return_value=artist)
        providers[instance] = provider
    mass._providers.update(providers)
    artist = providers["apple_music--online"].get_artist.return_value
    assert isinstance(artist, Artist)
    assert artist.uri is not None
    assert mass.get_provider("apple_music") is providers["apple_music--hidden"]
    if outcome == "library_offline":
        await mass.music.artists.add_item_to_library(
            providers["apple_music--offline"].get_artist.return_value
        )
        providers["apple_music--online"].available = False
    lookup = mass.music.artists.get_library_item_by_prov_id
    looked_up = []

    async def library_lookup(item_id: str, instance: str) -> Artist | None:
        looked_up.append(instance)
        result = await lookup(item_id, instance)
        if outcome in {"disconnect", "private"}:
            _revoke_sources(
                mass, ["apple_music--online"], "offline" if outcome == "disconnect" else outcome
            )
            providers["apple_music--offline"].available = True
        return result

    with patch.object(
        mass.music.artists, "get_library_item_by_prov_id", side_effect=library_lookup
    ):
        if outcome in {"disconnect", "private"}:
            with pytest.raises((MediaNotFoundError, ProviderUnavailableError)):
                await mass.music.get_item_share_info(artist.uri)
        else:
            result = await mass.music.get_item_share_info(artist.uri)
            expected_instance = (
                "apple_music--offline" if outcome == "library_offline" else "apple_music--online"
            )
            assert result["links"][0]["provider_instance"] == expected_instance
    assert "apple_music--hidden" not in looked_up
    if outcome != "library_offline":
        assert looked_up[0] == "apple_music--online"
    providers["apple_music--offline"].get_artist.assert_not_awaited()
    providers["apple_music--hidden"].get_artist.assert_not_awaited()
    if outcome == "online":
        assert set(looked_up) == {"apple_music--offline", "apple_music--online"}
        providers["apple_music--online"].get_artist.assert_awaited_once_with("123")
    else:
        providers["apple_music--online"].get_artist.assert_not_awaited()


@pytest.mark.usefixtures("reader")
@pytest.mark.parametrize("revocation", ["private", "config_removed", "removed"])
async def test_musicbrainz_rechecks_queued_barcode_source(
    music_mass: MusicAssistant, revocation: str
) -> None:
    """A barcode task queued before another task revokes its source must not fetch it."""
    await assert_queued_barcode_source_rechecked(
        music_mass,
        revocation,
        lambda: music_mass.music.get_item_share_info(f"musicbrainz://album/{RELEASE_GROUP_ID}"),
    )


def _revoke_sources(mass: MusicAssistant, instances: list[str], revocation: str) -> None:
    """Replace source configuration or disconnect exact instances during a suspended read."""
    configs = deepcopy(mass.config.get("providers"))
    for instance in instances:
        if revocation == "private":
            configs[instance]["access"] = ProviderAccess(
                owner="other", sharing=ProviderSharing.PRIVATE
            ).to_dict()
        elif revocation in {"removed", "config_removed"}:
            configs.pop(instance, None)
            if revocation == "removed":
                mass._providers.pop(instance, None)
        else:
            mass._providers[instance].available = False
    mass.config.set("providers", configs)


async def assert_candidate_source_rechecked(
    mass: MusicAssistant, revocation: str, resolve: Callable[[], Awaitable[object]]
) -> None:
    """Verify a resolver rechecks its candidate after a real library lookup."""
    instance = "spotify--account"
    set_music_source_access(mass, {instance: None})
    album = Album(
        item_id=SPOTIFY_ALBUM_ID,
        provider=instance,
        name="Protected album",
        provider_mappings={
            ProviderMapping(
                item_id=SPOTIFY_ALBUM_ID,
                provider_domain="spotify",
                provider_instance=instance,
            )
        },
    )
    provider = Mock(
        instance_id=instance,
        domain="spotify",
        type=ProviderType.MUSIC,
        available=True,
        is_streaming_provider=True,
        get_album=AsyncMock(return_value=album),
        supports_feature=Mock(return_value=False),
        get_album_by_external_id=AsyncMock(return_value=None),
    )
    mass._providers[instance] = provider
    musicbrainz = Mock(
        instance_id="musicbrainz",
        domain="musicbrainz",
        type=ProviderType.METADATA,
        available=True,
        browse_releases_by_release_group=AsyncMock(return_value=[_edition("edition")]),
        get_release_details=AsyncMock(
            return_value=_release("edition", barcode=None, urls=[SPOTIFY_ALBUM_URL])
        ),
    )
    mass._providers["musicbrainz"] = musicbrainz
    lookup = mass.music.albums.get_library_item_by_prov_id
    reading, resume = asyncio.Event(), asyncio.Event()

    async def library_lookup(item_id: str, provider_id: str) -> Album | None:
        result = await lookup(item_id, provider_id)
        assert provider_id == instance
        reading.set()
        await resume.wait()
        return result

    with patch.object(mass.music.albums, "get_library_item_by_prov_id", side_effect=library_lookup):
        request = asyncio.ensure_future(resolve())
        try:
            await asyncio.wait_for(reading.wait(), timeout=5)
            _revoke_sources(mass, [instance], revocation)
            resume.set()
            with pytest.raises(MediaNotFoundError):
                await request
        finally:
            resume.set()
            await asyncio.gather(request, return_exceptions=True)
    musicbrainz.get_release_details.assert_awaited_once_with("edition")
    provider.get_album.assert_not_awaited()
    provider.get_album_by_external_id.assert_not_awaited()


async def assert_queued_barcode_source_rechecked(
    mass: MusicAssistant, revocation: str, resolve: Callable[[], Awaitable[object]]
) -> None:
    """Verify a resolver rechecks barcode sources immediately before fetching."""
    instances = ["spotify--account", "tidal--account"]
    set_music_source_access(mass, dict.fromkeys(instances))

    async def first_lookup(*_args: Any) -> None:
        _revoke_sources(mass, [instances[1]], revocation)

    providers = []
    for instance in instances:
        provider = Mock(
            instance_id=instance,
            domain=instance.split("--")[0],
            type=ProviderType.MUSIC,
            available=True,
            is_streaming_provider=True,
            supports_feature=Mock(return_value=True),
            get_album_by_external_id=AsyncMock(return_value=None),
            get_album=AsyncMock(),
        )
        mass._providers[instance] = provider
        providers.append(provider)
    providers[0].get_album_by_external_id.side_effect = first_lookup
    mass._providers["musicbrainz"] = Mock(
        instance_id="musicbrainz",
        domain="musicbrainz",
        type=ProviderType.METADATA,
        available=True,
        browse_releases_by_release_group=AsyncMock(return_value=[_edition("edition")]),
        get_release_details=AsyncMock(return_value=_release("edition")),
    )
    with pytest.raises(MediaNotFoundError):
        await resolve()
    providers[0].get_album_by_external_id.assert_awaited_once()
    providers[1].get_album_by_external_id.assert_not_awaited()
    for provider in providers:
        provider.get_album.assert_not_awaited()
