"""Tests for picking the Subsonic provider instance a play is scrobbled to."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.config_entries import ProviderAccess
from music_assistant_models.enums import MediaType, ProviderFeature, ProviderSharing
from music_assistant_models.media_items import Podcast, PodcastEpisode, ProviderMapping, Track
from music_assistant_models.playback_progress_report import MediaItemPlaybackProgressReport

from music_assistant.mass import MusicAssistant
from music_assistant.providers.opensubsonic.parsers import EP_CHAN_SEP
from music_assistant.providers.opensubsonic.sonic_provider import OpenSonicProvider
from music_assistant.providers.subsonic_scrobble import (
    SUPPORTED_FEATURES,
    SubsonicScrobbleEventHandler,
    SubsonicScrobbleProvider,
)
from tests.common import set_music_source_access

INSTANCE_A = "opensubsonic--aaaa"
INSTANCE_B = "opensubsonic--bbbb"
USER_ID = "user-1"
OTHER_USER_ID = "user-2"


def _track() -> Track:
    """Build a library track that maps to two instances of the same Subsonic server."""
    return Track(
        item_id="1",
        provider="library",
        name="Track",
        provider_mappings={
            ProviderMapping(
                item_id="a-42", provider_domain="opensubsonic", provider_instance=INSTANCE_A
            ),
            ProviderMapping(
                item_id="b-42", provider_domain="opensubsonic", provider_instance=INSTANCE_B
            ),
        },
    )


def _episode() -> PodcastEpisode:
    """Build a library podcast episode whose Subsonic id carries the channel id (see parse_episode)."""
    podcast = Podcast(
        item_id="10",
        provider="library",
        name="Podcast",
        provider_mappings={
            ProviderMapping(
                item_id="chan-1", provider_domain="opensubsonic", provider_instance=INSTANCE_B
            ),
        },
    )
    return PodcastEpisode(
        item_id="7",
        provider="library",
        name="Episode",
        position=1,
        podcast=podcast,
        provider_mappings={
            ProviderMapping(
                item_id=f"chan-1{EP_CHAN_SEP}ep-7",
                provider_domain="opensubsonic",
                provider_instance=INSTANCE_B,
            ),
        },
    )


def _report() -> MediaItemPlaybackProgressReport:
    """Build the report of a library track that was played to the end."""
    return MediaItemPlaybackProgressReport(
        uri="library://track/1",
        media_type=MediaType.TRACK,
        name="Track",
        duration=200,
        seconds_played=200,
        fully_played=True,
        is_playing=False,
        userid=USER_ID,
    )


def _user(user_id: str = USER_ID) -> User:
    return User(user_id=user_id, username=user_id, role=UserRole.USER)


def _private(owner: str) -> ProviderAccess:
    return ProviderAccess(owner=owner, sharing=ProviderSharing.PRIVATE)


def _shared(owner: str) -> ProviderAccess:
    return ProviderAccess(owner=owner, sharing=ProviderSharing.EVERYONE)


@pytest.fixture
def providers() -> dict[str, Mock]:
    """One mocked OpenSonicProvider per instance, both loaded and available."""
    provs = {INSTANCE_A: Mock(spec=OpenSonicProvider), INSTANCE_B: Mock(spec=OpenSonicProvider)}
    for instance_id, prov in provs.items():
        prov.instance_id = instance_id
        prov.domain = "opensubsonic"
        prov.available = True
        prov.is_streaming_provider = True
        prov.conn = Mock()
        prov.conn.scrobble = AsyncMock()
    return provs


@pytest.fixture
def mass(providers: dict[str, Mock]) -> Mock:
    """Mock the server: library lookup, provider registry and user lookup."""
    mass = Mock()
    mass.music.get_library_item_by_prov_id = AsyncMock(return_value=_track())
    mass.get_provider.side_effect = lambda instance_id, **_kwargs: providers.get(instance_id)
    mass.webserver.auth.get_user = AsyncMock(return_value=None)
    # both instances are household sources unless a test says otherwise
    set_music_source_access(mass, {INSTANCE_A: None, INSTANCE_B: None})
    return mass


@pytest.fixture
def handler(mass: Mock) -> SubsonicScrobbleEventHandler:
    """Event handler under test, with default plugin config."""
    config = Mock()
    config.get_value.side_effect = lambda _key, default=None: default
    return SubsonicScrobbleEventHandler(mass, logging.getLogger(__name__), config)


async def test_prefers_the_instance_the_playing_user_owns(
    handler: SubsonicScrobbleEventHandler, mass: Mock, providers: dict[str, Mock]
) -> None:
    """A user owning instance B is credited on B, whatever the mapping order."""
    set_music_source_access(
        mass, {INSTANCE_A: _shared(OTHER_USER_ID), INSTANCE_B: _private(USER_ID)}
    )
    mass.webserver.auth.get_user.return_value = _user()

    prov, item_id = await handler._get_subsonic_provider_and_item_id(
        MediaType.TRACK, "library", "1", USER_ID
    )

    assert prov is providers[INSTANCE_B]
    assert item_id == "b-42"
    mass.webserver.auth.get_user.assert_awaited_once_with(USER_ID)


async def test_never_reports_to_another_members_private_instance(
    handler: SubsonicScrobbleEventHandler, mass: Mock, providers: dict[str, Mock]
) -> None:
    """An instance the playing user may not use is dropped instead of credited."""
    set_music_source_access(
        mass, {INSTANCE_A: _private(OTHER_USER_ID), INSTANCE_B: _private(USER_ID)}
    )
    mass.webserver.auth.get_user.return_value = _user()

    prov, item_id = await handler._get_subsonic_provider_and_item_id(
        MediaType.TRACK, "library", "1", USER_ID
    )

    assert prov is providers[INSTANCE_B]
    assert item_id == "b-42"


async def test_without_user_any_household_instance_is_used(
    handler: SubsonicScrobbleEventHandler, mass: Mock, providers: dict[str, Mock]
) -> None:
    """No user on the report keeps the previous behaviour: any household instance."""
    prov, item_id = await handler._get_subsonic_provider_and_item_id(
        MediaType.TRACK, "library", "1", None
    )

    assert prov in providers.values()
    assert item_id in {"a-42", "b-42"}
    mass.webserver.auth.get_user.assert_not_awaited()


async def test_user_owning_no_instance_falls_back(
    handler: SubsonicScrobbleEventHandler, mass: Mock, providers: dict[str, Mock]
) -> None:
    """A user that owns no Subsonic instance but may use both does not stop scrobbling."""
    set_music_source_access(
        mass, {INSTANCE_A: _shared(OTHER_USER_ID), INSTANCE_B: _shared(OTHER_USER_ID)}
    )
    mass.webserver.auth.get_user.return_value = _user()

    prov, _ = await handler._get_subsonic_provider_and_item_id(
        MediaType.TRACK, "library", "1", USER_ID
    )

    assert prov in providers.values()


async def test_unavailable_own_instance_scrobbles_nowhere(
    handler: SubsonicScrobbleEventHandler, mass: Mock, providers: dict[str, Mock]
) -> None:
    """If the user's own instance is not loaded, no other account gets the play."""
    set_music_source_access(
        mass, {INSTANCE_A: _private(OTHER_USER_ID), INSTANCE_B: _private(USER_ID)}
    )
    mass.webserver.auth.get_user.return_value = _user()
    mass.get_provider.side_effect = lambda instance_id, **_kwargs: (
        None if instance_id == INSTANCE_B else providers.get(instance_id)
    )

    prov, item_id = await handler._get_subsonic_provider_and_item_id(
        MediaType.TRACK, "library", "1", USER_ID
    )

    assert prov is None
    assert item_id == "1"


async def test_never_reports_through_another_account_of_the_same_server(
    handler: SubsonicScrobbleEventHandler, mass: Mock, providers: dict[str, Mock]
) -> None:
    """
    The user's own instance being unavailable does not hand the play to the other account.

    `get_provider` stands in another instance of the same server for an unavailable one,
    which here would submit the play to a Subsonic account the user may not use.
    """
    set_music_source_access(
        mass, {INSTANCE_A: _private(OTHER_USER_ID), INSTANCE_B: _private(USER_ID)}
    )
    mass.webserver.auth.get_user.return_value = _user()
    providers[INSTANCE_B].available = False
    mass._providers = providers
    mass.get_provider = MusicAssistant.get_provider.__get__(mass)

    prov, item_id = await handler._get_subsonic_provider_and_item_id(
        MediaType.TRACK, "library", "1", USER_ID
    )

    assert prov is None
    assert item_id == "1"


async def test_provider_uri_is_not_redirected(
    handler: SubsonicScrobbleEventHandler, mass: Mock, providers: dict[str, Mock]
) -> None:
    """An item played straight from an instance is credited to that instance."""
    set_music_source_access(
        mass, {INSTANCE_A: _shared(OTHER_USER_ID), INSTANCE_B: _private(USER_ID)}
    )
    mass.webserver.auth.get_user.return_value = _user()

    prov, item_id = await handler._get_subsonic_provider_and_item_id(
        MediaType.TRACK, INSTANCE_A, "a-42", USER_ID
    )

    assert prov is providers[INSTANCE_A]
    assert item_id == "a-42"


async def test_scrobble_reaches_the_users_instance(
    handler: SubsonicScrobbleEventHandler, mass: Mock, providers: dict[str, Mock]
) -> None:
    """The full path: a finished library track is submitted to the playing user's account."""
    set_music_source_access(
        mass, {INSTANCE_A: _private(OTHER_USER_ID), INSTANCE_B: _private(USER_ID)}
    )
    mass.webserver.auth.get_user.return_value = _user()

    await handler._scrobble(_report())

    providers[INSTANCE_B].conn.scrobble.assert_awaited_once()
    providers[INSTANCE_A].conn.scrobble.assert_not_awaited()
    assert providers[INSTANCE_B].conn.scrobble.await_args.args[0] == "b-42"


async def test_hook_reports_to_the_account_of_the_playing_user(
    mass: Mock, providers: dict[str, Mock]
) -> None:
    """The provider hook submits a played item to the account of the user that played it."""
    set_music_source_access(
        mass, {INSTANCE_A: _private(OTHER_USER_ID), INSTANCE_B: _private(USER_ID)}
    )
    mass.webserver.auth.get_user.return_value = _user()
    config = Mock()
    config.get_value.side_effect = lambda _key, default=None: default
    provider = SubsonicScrobbleProvider(
        mass, Mock(domain="subsonic_scrobble"), config, SUPPORTED_FEATURES
    )
    await provider.loaded_in_mass()

    await provider.on_media_item_played(_report())

    assert ProviderFeature.SCROBBLE in provider.supported_features
    providers[INSTANCE_B].conn.scrobble.assert_awaited_once()
    providers[INSTANCE_A].conn.scrobble.assert_not_awaited()
    assert providers[INSTANCE_B].conn.scrobble.await_args.args[0] == "b-42"


async def test_library_item_without_subsonic_mapping_skips_user_lookup(
    handler: SubsonicScrobbleEventHandler, mass: Mock
) -> None:
    """No Subsonic mapping at all: nothing to report, and the user is not even looked up."""
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

    prov, item_id = await handler._get_subsonic_provider_and_item_id(
        MediaType.TRACK, "library", "1", USER_ID
    )

    assert prov is None
    assert item_id == "1"
    mass.webserver.auth.get_user.assert_not_awaited()


async def test_deleted_or_disabled_user_falls_back_to_household_instances(
    handler: SubsonicScrobbleEventHandler, mass: Mock, providers: dict[str, Mock]
) -> None:
    """A user id that no longer resolves is treated as anonymous playback."""
    mass.webserver.auth.get_user.return_value = None

    prov, item_id = await handler._get_subsonic_provider_and_item_id(
        MediaType.TRACK, "library", "1", "user-gone"
    )

    assert prov in providers.values()
    assert item_id in {"a-42", "b-42"}
    mass.webserver.auth.get_user.assert_awaited_once_with("user-gone")


async def test_user_seeing_every_instance_keeps_previous_behaviour(
    handler: SubsonicScrobbleEventHandler, mass: Mock, providers: dict[str, Mock]
) -> None:
    """A user that may use every instance is not steered to any of them."""
    mass.webserver.auth.get_user.return_value = _user()

    prov, _ = await handler._get_subsonic_provider_and_item_id(
        MediaType.TRACK, "library", "1", USER_ID
    )

    assert prov in providers.values()


async def test_library_podcast_episode_id_drops_the_channel_prefix(
    handler: SubsonicScrobbleEventHandler, mass: Mock, providers: dict[str, Mock]
) -> None:
    """A library episode is credited to the user's instance with the bare Subsonic episode id."""
    mass.music.get_library_item_by_prov_id.return_value = _episode()
    set_music_source_access(
        mass, {INSTANCE_A: _private(OTHER_USER_ID), INSTANCE_B: _private(USER_ID)}
    )
    mass.webserver.auth.get_user.return_value = _user()

    prov, item_id = await handler._get_subsonic_provider_and_item_id(
        MediaType.PODCAST_EPISODE, "library", "7", USER_ID
    )

    assert prov is providers[INSTANCE_B]
    assert item_id == "ep-7"


async def test_provider_podcast_episode_id_drops_the_channel_prefix(
    handler: SubsonicScrobbleEventHandler, providers: dict[str, Mock]
) -> None:
    """An episode played straight from an instance also loses the channel id before the report."""
    prov, item_id = await handler._get_subsonic_provider_and_item_id(
        MediaType.PODCAST_EPISODE, INSTANCE_A, f"chan-1{EP_CHAN_SEP}ep-3", USER_ID
    )

    assert prov is providers[INSTANCE_A]
    assert item_id == "ep-3"
