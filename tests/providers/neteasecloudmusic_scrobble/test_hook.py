"""Tests for the NetEase Cloud Music scrobbler's playback report hook."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.enums import MediaType, ProviderFeature
from music_assistant_models.errors import SetupFailedError
from music_assistant_models.media_items import ProviderMapping, Track
from music_assistant_models.playback_progress_report import MediaItemPlaybackProgressReport

from music_assistant.providers.neteasecloudmusic import NeteaseCloudMusicProvider
from music_assistant.providers.neteasecloudmusic_scrobble import (
    SUPPORTED_FEATURES,
    NeteaseScrobbleHandler,
    NeteaseScrobbleProvider,
    setup,
)
from tests.common import set_music_source_access

INSTANCE_A = "neteasecloudmusic--aaaa"
INSTANCE_B = "neteasecloudmusic--bbbb"
DOMAIN = "neteasecloudmusic"
USER_ID = "user-1"
OTHER_USER_ID = "user-2"


def _track() -> Track:
    """Build a library track that maps to two instances of the same NetEase source."""
    return Track(
        item_id="1",
        provider="library",
        name="Track",
        provider_mappings={
            ProviderMapping(item_id="a-42", provider_domain=DOMAIN, provider_instance=INSTANCE_A),
            ProviderMapping(item_id="b-42", provider_domain=DOMAIN, provider_instance=INSTANCE_B),
        },
    )


def _report(
    uri: str = "library://track/1",
    *,
    seconds_played: int = 200,
    fully_played: bool = True,
    is_playing: bool = False,
    userid: str | None = USER_ID,
) -> MediaItemPlaybackProgressReport:
    """Build a playback progress report for a track."""
    return MediaItemPlaybackProgressReport(
        uri=uri,
        media_type=MediaType.TRACK,
        name="Track",
        duration=200,
        seconds_played=seconds_played,
        fully_played=fully_played,
        is_playing=is_playing,
        player_id="player-1",
        userid=userid,
    )


@pytest.fixture
def providers() -> dict[str, Mock]:
    """One mocked NeteaseCloudMusicProvider per instance, both loaded and available."""
    provs = {
        INSTANCE_A: Mock(spec=NeteaseCloudMusicProvider),
        INSTANCE_B: Mock(spec=NeteaseCloudMusicProvider),
    }
    for instance_id, prov in provs.items():
        prov.instance_id = instance_id
        prov.domain = DOMAIN
        prov.available = True
        prov.is_streaming_provider = True
        prov.cookie = "MUSIC_U=secret"
        prov.api_client = Mock(
            get=AsyncMock(
                side_effect=lambda path, **_kwargs: (
                    {"songs": [{"al": {"id": 456}}]} if path == "/song/detail" else {"code": 200}
                )
            )
        )
    return provs


@pytest.fixture
def mass(providers: dict[str, Mock]) -> Mock:
    """Mock the server: library lookup, provider registry, cache and user lookup."""
    mass = Mock()
    mass.music.get_library_item_by_prov_id = AsyncMock(return_value=_track())
    mass.get_provider.side_effect = lambda instance_id, **_kwargs: providers.get(instance_id)
    mass.get_provider_instances.side_effect = lambda domain, **_kwargs: [
        prov for prov in providers.values() if prov.domain == domain
    ]
    mass.webserver.auth.get_user = AsyncMock(return_value=None)
    stored: dict[str, object] = {}

    async def cache_get(key: str, default: object = None, **_kwargs: object) -> object:
        """Return the previously stored value for the key, if any."""
        return stored.get(key, default)

    async def cache_set(key: str, data: object, **_kwargs: object) -> None:
        """Store the value for the key."""
        stored[key] = data

    mass.cache.get = AsyncMock(side_effect=cache_get)
    mass.cache.set = AsyncMock(side_effect=cache_set)
    # both instances are household sources unless a test says otherwise
    set_music_source_access(mass, {INSTANCE_A: None, INSTANCE_B: None})
    return mass


def _config() -> Mock:
    config = Mock()
    config.get_value.side_effect = lambda _key, default=None: default
    return config


@pytest.fixture
def handler(mass: Mock) -> NeteaseScrobbleHandler:
    """Event handler under test, with default plugin config."""
    return NeteaseScrobbleHandler(mass, logging.getLogger(__name__), _config())


async def test_library_track_resolves_to_a_mapped_instance(
    handler: NeteaseScrobbleHandler, mass: Mock, providers: dict[str, Mock]
) -> None:
    """A library track is credited to one of the NetEase instances it maps to."""
    mass.webserver.auth.get_user.return_value = Mock()

    prov, track_id = await handler._get_ncm_provider_and_track_id(
        MediaType.TRACK, "library", "1", USER_ID
    )

    assert prov in providers.values()
    assert track_id in {"a-42", "b-42"}


async def test_without_user_any_household_instance_is_used(
    handler: NeteaseScrobbleHandler, mass: Mock, providers: dict[str, Mock]
) -> None:
    """No user on the report keeps the previous behaviour: any household instance."""
    prov, track_id = await handler._get_ncm_provider_and_track_id(
        MediaType.TRACK, "library", "1", None
    )

    assert prov in providers.values()
    assert track_id in {"a-42", "b-42"}
    mass.webserver.auth.get_user.assert_not_awaited()


async def test_provider_uri_is_not_redirected(
    handler: NeteaseScrobbleHandler, providers: dict[str, Mock]
) -> None:
    """An item played straight from an instance is credited to that instance."""
    prov, track_id = await handler._get_ncm_provider_and_track_id(
        MediaType.TRACK, INSTANCE_A, "a-42", USER_ID
    )

    assert prov is providers[INSTANCE_A]
    assert track_id == "a-42"


async def test_library_item_without_netease_mapping_is_skipped(
    handler: NeteaseScrobbleHandler, mass: Mock
) -> None:
    """A library item not linked to NetEase is not reported and the user is not looked up."""
    mass.music.get_library_item_by_prov_id.return_value = Track(
        item_id="1",
        provider="library",
        name="Track",
        provider_mappings={
            ProviderMapping(
                item_id="f-1", provider_domain="filesystem_local", provider_instance="fs"
            )
        },
    )

    prov, track_id = await handler._get_ncm_provider_and_track_id(
        MediaType.TRACK, "library", "1", USER_ID
    )

    assert prov is None
    assert track_id == "1"


async def test_scrobble_submits_to_the_resolved_instance(
    handler: NeteaseScrobbleHandler, providers: dict[str, Mock]
) -> None:
    """The full path: a direct-uri play is submitted to that NetEase instance."""
    await handler._scrobble(_report(uri=f"{INSTANCE_A}://track/42", seconds_played=180))

    scrobble = next(
        call
        for call in providers[INSTANCE_A].api_client.get.await_args_list
        if call.args[0] == "/scrobble"
    )
    assert scrobble.kwargs["params"] == {
        "id": "42",
        "sourceid": "456",
        "time": 180,
        "cookie": "MUSIC_U=secret",
    }
    assert scrobble.kwargs["cookie"] == "MUSIC_U=secret"
    assert providers[INSTANCE_B].api_client.get.await_args_list == []


async def test_album_source_id_is_cached(
    handler: NeteaseScrobbleHandler, providers: dict[str, Mock]
) -> None:
    """The album id lookup for a track hits the api only once."""
    await handler._scrobble(_report(uri=f"{INSTANCE_A}://track/42"))
    await handler._scrobble(_report(uri=f"{INSTANCE_A}://track/42"))

    detail_calls = [
        call
        for call in providers[INSTANCE_A].api_client.get.await_args_list
        if call.args[0] == "/song/detail"
    ]
    assert len(detail_calls) == 1


async def test_album_source_id_accepts_the_album_field(
    handler: NeteaseScrobbleHandler, providers: dict[str, Mock]
) -> None:
    """Compatible backends return the album under "album" instead of "al"."""
    providers[INSTANCE_A].api_client.get.side_effect = lambda path, **_kwargs: (
        {"songs": [{"album": {"id": 789}}]} if path == "/song/detail" else {"code": 200}
    )

    await handler._scrobble(_report(uri=f"{INSTANCE_A}://track/42"))

    scrobble = next(
        call
        for call in providers[INSTANCE_A].api_client.get.await_args_list
        if call.args[0] == "/scrobble"
    )
    assert scrobble.kwargs["params"]["sourceid"] == "789"


async def test_hook_reports_to_the_account_of_the_playing_user(
    mass: Mock, providers: dict[str, Mock]
) -> None:
    """The provider hook submits a played item to the account that served it."""
    provider = NeteaseScrobbleProvider(
        mass, Mock(domain="neteasecloudmusic_scrobble"), _config(), SUPPORTED_FEATURES
    )
    await provider.loaded_in_mass()

    await provider.on_media_item_played(_report(uri=f"{INSTANCE_B}://track/42"))

    assert ProviderFeature.SCROBBLE in provider.supported_features
    assert "/scrobble" in {
        call.args[0] for call in providers[INSTANCE_B].api_client.get.await_args_list
    }
    assert providers[INSTANCE_A].api_client.get.await_args_list == []


async def test_plugin_has_no_source_selection_entry() -> None:
    """The plugin exposes only the shared scrobbler options, no source picker."""
    mass = Mock()
    mass.webserver.auth.list_users = AsyncMock(return_value=[])
    mass.players.all_players.return_value = []
    provider = NeteaseScrobbleProvider(
        mass, Mock(domain="neteasecloudmusic_scrobble"), _config(), SUPPORTED_FEATURES
    )

    entries = await provider.get_config_entries()

    assert {entry.key for entry in entries} == {
        "suffix_version",
        "scrobble_users",
        "scrobble_players",
    }


async def test_setup_requires_a_netease_source() -> None:
    """Adding the plugin without a NetEase Cloud Music source fails fast."""
    mass = Mock()
    mass.get_provider.return_value = None

    with pytest.raises(SetupFailedError):
        await setup(mass, Mock(), _config())


async def test_setup_succeeds_with_a_netease_source(providers: dict[str, Mock]) -> None:
    """With a NetEase source present the provider instance is created."""
    mass = Mock()
    mass.get_provider.return_value = providers[INSTANCE_A]

    provider = await setup(mass, Mock(domain="neteasecloudmusic_scrobble"), _config())

    assert isinstance(provider, NeteaseScrobbleProvider)
