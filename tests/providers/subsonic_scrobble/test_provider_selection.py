"""Tests for picking the Subsonic provider instance a play is scrobbled to."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import ProviderMapping, Track
from music_assistant_models.playback_progress_report import MediaItemPlaybackProgressReport

from music_assistant.providers.opensubsonic.sonic_provider import OpenSonicProvider
from music_assistant.providers.subsonic_scrobble import SubsonicScrobbleEventHandler

INSTANCE_A = "opensubsonic--aaaa"
INSTANCE_B = "opensubsonic--bbbb"
USER_ID = "user-1"


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


def _user_with_filter(provider_filter: list[str]) -> Mock:
    user = Mock()
    user.provider_filter = provider_filter
    return user


@pytest.fixture
def providers() -> dict[str, Mock]:
    """One mocked OpenSonicProvider per instance."""
    provs = {INSTANCE_A: Mock(spec=OpenSonicProvider), INSTANCE_B: Mock(spec=OpenSonicProvider)}
    for prov in provs.values():
        prov.conn = Mock()
        prov.conn.scrobble = AsyncMock()
    return provs


@pytest.fixture
def mass(providers: dict[str, Mock]) -> Mock:
    """Mock the server: library lookup, provider registry and user lookup."""
    mass = Mock()
    mass.music.get_library_item_by_prov_id = AsyncMock(return_value=_track())
    mass.get_provider.side_effect = lambda instance_id: providers.get(instance_id)
    mass.webserver.auth.get_user = AsyncMock(return_value=None)
    return mass


@pytest.fixture
def handler(mass: Mock) -> SubsonicScrobbleEventHandler:
    """Event handler under test, with default plugin config."""
    config = Mock()
    config.get_value.side_effect = lambda _key, default=None: default
    return SubsonicScrobbleEventHandler(mass, logging.getLogger(__name__), config)


async def test_prefers_instance_in_playing_users_filter(
    handler: SubsonicScrobbleEventHandler, mass: Mock, providers: dict[str, Mock]
) -> None:
    """A user filtered to instance B is credited on B, whatever the mapping order."""
    mass.webserver.auth.get_user.return_value = _user_with_filter(["builtin", INSTANCE_B])

    prov, item_id = await handler._get_subsonic_provider_and_item_id(
        MediaType.TRACK, "library", "1", USER_ID
    )

    assert prov is providers[INSTANCE_B]
    assert item_id == "b-42"
    mass.webserver.auth.get_user.assert_awaited_once_with(USER_ID)


async def test_without_user_any_instance_is_used(
    handler: SubsonicScrobbleEventHandler, mass: Mock, providers: dict[str, Mock]
) -> None:
    """No user on the report keeps the previous behaviour: any mapped instance."""
    prov, item_id = await handler._get_subsonic_provider_and_item_id(
        MediaType.TRACK, "library", "1", None
    )

    assert prov in providers.values()
    assert item_id in {"a-42", "b-42"}
    mass.webserver.auth.get_user.assert_not_awaited()


async def test_filter_without_subsonic_instance_falls_back(
    handler: SubsonicScrobbleEventHandler, mass: Mock, providers: dict[str, Mock]
) -> None:
    """A filter that names no Subsonic instance does not stop scrobbling."""
    mass.webserver.auth.get_user.return_value = _user_with_filter(["builtin"])

    prov, _ = await handler._get_subsonic_provider_and_item_id(
        MediaType.TRACK, "library", "1", USER_ID
    )

    assert prov in providers.values()


async def test_unavailable_preferred_instance_scrobbles_nowhere(
    handler: SubsonicScrobbleEventHandler, mass: Mock, providers: dict[str, Mock]
) -> None:
    """A filter is an allowlist: if the user's instance is not loaded, no other account gets the play."""
    mass.webserver.auth.get_user.return_value = _user_with_filter([INSTANCE_B])
    mass.get_provider.side_effect = lambda instance_id: (
        None if instance_id == INSTANCE_B else providers.get(instance_id)
    )

    prov, item_id = await handler._get_subsonic_provider_and_item_id(
        MediaType.TRACK, "library", "1", USER_ID
    )

    assert prov is None
    assert item_id == "1"


async def test_provider_uri_is_not_redirected(
    handler: SubsonicScrobbleEventHandler, mass: Mock, providers: dict[str, Mock]
) -> None:
    """An item played straight from an instance is credited to that instance."""
    mass.webserver.auth.get_user.return_value = _user_with_filter([INSTANCE_B])

    prov, item_id = await handler._get_subsonic_provider_and_item_id(
        MediaType.TRACK, INSTANCE_A, "a-42", USER_ID
    )

    assert prov is providers[INSTANCE_A]
    assert item_id == "a-42"


async def test_scrobble_reaches_the_users_instance(
    handler: SubsonicScrobbleEventHandler, mass: Mock, providers: dict[str, Mock]
) -> None:
    """The full path: a finished library track is submitted to the playing user's account."""
    mass.webserver.auth.get_user.return_value = _user_with_filter([INSTANCE_B])
    report = MediaItemPlaybackProgressReport(
        uri="library://track/1",
        media_type=MediaType.TRACK,
        name="Track",
        duration=200,
        seconds_played=200,
        fully_played=True,
        is_playing=False,
        userid=USER_ID,
    )

    await handler._scrobble(report)

    providers[INSTANCE_B].conn.scrobble.assert_awaited_once()
    providers[INSTANCE_A].conn.scrobble.assert_not_awaited()
    assert providers[INSTANCE_B].conn.scrobble.await_args.args[0] == "b-42"
