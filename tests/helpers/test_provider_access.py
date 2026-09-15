"""Tests for deriving a user's music sources from the access records on the sources."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.config_entries import ProviderAccess
from music_assistant_models.enums import ProviderSharing

from music_assistant.constants import CONF_PROVIDERS
from music_assistant.helpers.provider_access import (
    access_allows,
    derived_provider_filter,
    own_music_sources,
    playback_instance_for,
    playback_sources,
    source_access,
    source_owner,
    visible_music_sources,
    visible_playback_sources,
)
from tests.common import set_music_source_access

OWNER = "user-owner"
MEMBER = "user-member"
GUEST = "user-guest"


def _user(user_id: str, role: str = UserRole.USER) -> User:
    return User(user_id=user_id, username=user_id, role=role)


def _mass(providers: list[MagicMock] | None = None) -> MagicMock:
    """
    Return a mocked server without any music source configured.

    :param providers: The loaded provider instances; a service without one of them is
        never narrowed for playback.
    """
    mass = MagicMock()
    loaded = providers or []
    mass.get_provider.side_effect = lambda instance_id, **_kwargs: next(
        (prov for prov in loaded if prov.instance_id == instance_id), None
    )
    mass.get_provider_instances.side_effect = lambda domain, **_kwargs: [
        prov for prov in loaded if prov.domain == domain
    ]
    set_music_source_access(mass, {})
    return mass


def _provider(instance_id: str, is_streaming: bool) -> MagicMock:
    """Return a loaded music provider instance of the service in the given instance id."""
    provider = MagicMock()
    provider.instance_id = instance_id
    provider.domain = instance_id.split("--", maxsplit=1)[0]
    provider.is_streaming_provider = is_streaming
    return provider


@pytest.mark.parametrize(
    ("sharing", "expected"),
    [
        (ProviderSharing.EVERYONE, True),
        (ProviderSharing.MEMBERS, False),
        (ProviderSharing.SELECTED, False),
        (ProviderSharing.PRIVATE, False),
    ],
)
def test_access_allows_anonymous_reaches_everyone_sources_only(
    sharing: ProviderSharing, expected: bool
) -> None:
    """Anonymous playback may only use sources shared with everyone."""
    access = ProviderAccess(owner=OWNER, sharing=sharing, shared_users=[MEMBER])
    assert access_allows(access, None) is expected


def test_access_allows_household_source_for_everyone() -> None:
    """A source without an access record is a household source."""
    assert access_allows(None, None) is True
    assert access_allows(None, _user(GUEST, UserRole.GUEST)) is True


def test_access_allows_owner_of_a_private_source() -> None:
    """The owner may always use its own source, however narrowly it is shared."""
    access = ProviderAccess(owner=OWNER, sharing=ProviderSharing.PRIVATE)
    assert access_allows(access, _user(OWNER)) is True
    assert access_allows(access, _user(MEMBER)) is False


def test_access_allows_members_excludes_guests() -> None:
    """MEMBERS covers every role but guest."""
    access = ProviderAccess(owner=OWNER, sharing=ProviderSharing.MEMBERS)
    assert access_allows(access, _user(MEMBER)) is True
    assert access_allows(access, _user("service", UserRole.SERVICE)) is True
    assert access_allows(access, _user("admin", UserRole.ADMIN)) is True
    assert access_allows(access, _user(GUEST, UserRole.GUEST)) is False


def test_access_allows_selected_users_only() -> None:
    """SELECTED covers the listed users only, whatever their role."""
    access = ProviderAccess(
        owner=OWNER, sharing=ProviderSharing.SELECTED, shared_users=[MEMBER, GUEST]
    )
    assert access_allows(access, _user(MEMBER)) is True
    assert access_allows(access, _user(GUEST, UserRole.GUEST)) is True
    assert access_allows(access, _user("someone-else")) is False


def test_visible_music_sources_returns_none_when_everything_is_visible() -> None:
    """A user that may see every music source gets the 'no filter' sentinel."""
    mass = _mass()
    set_music_source_access(
        mass,
        {
            "builtin": None,
            "spotify--aaaa": ProviderAccess(owner=OWNER, sharing=ProviderSharing.MEMBERS),
        },
    )
    assert visible_music_sources(mass, _user(MEMBER)) is None


def test_visible_music_sources_narrows_to_the_allowed_sources() -> None:
    """A hidden source narrows the result to the sources the user may see."""
    mass = _mass()
    set_music_source_access(
        mass,
        {
            "builtin": None,
            "spotify--aaaa": ProviderAccess(owner=OWNER, sharing=ProviderSharing.PRIVATE),
            "tidal--bbbb": ProviderAccess(owner=MEMBER, sharing=ProviderSharing.PRIVATE),
        },
    )
    assert visible_music_sources(mass, _user(MEMBER)) == ["builtin", "tidal--bbbb"]
    assert visible_music_sources(mass, _user(OWNER)) == ["builtin", "spotify--aaaa"]


def test_visible_music_sources_skips_non_music_configs() -> None:
    """Only music sources carry visibility; other provider types are never listed."""
    mass = _mass()
    set_music_source_access(
        mass, {"spotify--aaaa": ProviderAccess(owner=OWNER, sharing=ProviderSharing.PRIVATE)}
    )
    raw_configs = mass.config.get(CONF_PROVIDERS, {})
    raw_configs["airplay"] = {"type": "player", "domain": "airplay", "instance_id": "airplay"}
    assert visible_music_sources(mass, _user(MEMBER)) == []


def test_visible_music_sources_hides_a_malformed_record() -> None:
    """An access record that can not be read hides its source instead of exposing it."""
    mass = _mass()
    set_music_source_access(mass, {"spotify--aaaa": None, "tidal--bbbb": None})
    mass.config.get(CONF_PROVIDERS, {})["spotify--aaaa"]["access"] = "not-a-record"
    assert visible_music_sources(mass, _user(MEMBER)) == ["tidal--bbbb"]


def test_visible_playback_sources_for_anonymous_playback() -> None:
    """Anonymous playback reaches household and everyone-shared sources only."""
    mass = _mass()
    set_music_source_access(
        mass,
        {
            "builtin": None,
            "spotify--aaaa": ProviderAccess(owner=OWNER, sharing=ProviderSharing.EVERYONE),
            "tidal--bbbb": ProviderAccess(owner=OWNER, sharing=ProviderSharing.MEMBERS),
        },
    )
    assert visible_playback_sources(mass, None) == ["builtin", "spotify--aaaa"]


def test_visible_playback_sources_drops_another_account_of_an_own_service() -> None:
    """Owning a source of a service keeps playback off the other accounts of it."""
    mass = _mass([_provider("spotify--mine", is_streaming=True)])
    set_music_source_access(
        mass,
        {
            "builtin": None,
            "spotify--mine": ProviderAccess(owner=OWNER, sharing=ProviderSharing.PRIVATE),
            "spotify--theirs": ProviderAccess(owner=MEMBER, sharing=ProviderSharing.EVERYONE),
            "tidal--theirs": ProviderAccess(owner=MEMBER, sharing=ProviderSharing.EVERYONE),
            "qobuz--theirs": ProviderAccess(owner=MEMBER, sharing=ProviderSharing.PRIVATE),
        },
    )

    # browsing still reaches the shared account of the service the user has
    assert visible_music_sources(mass, _user(OWNER)) == [
        "builtin",
        "spotify--mine",
        "spotify--theirs",
        "tidal--theirs",
    ]
    assert visible_playback_sources(mass, _user(OWNER)) == [
        "builtin",
        "spotify--mine",
        "tidal--theirs",
    ]


def test_visible_playback_sources_narrows_a_streaming_service_only() -> None:
    """Accounts of a streaming service share one catalog, local sources are separate libraries."""
    mass = _mass(
        [
            _provider("filesystem_local--mine", is_streaming=False),
            _provider("filesystem_local--household", is_streaming=False),
            _provider("spotify--mine", is_streaming=True),
            _provider("spotify--theirs", is_streaming=True),
        ]
    )
    set_music_source_access(
        mass,
        {
            "filesystem_local--mine": ProviderAccess(owner=OWNER, sharing=ProviderSharing.PRIVATE),
            "filesystem_local--household": None,
            "spotify--mine": ProviderAccess(owner=OWNER, sharing=ProviderSharing.PRIVATE),
            "spotify--theirs": ProviderAccess(owner=MEMBER, sharing=ProviderSharing.EVERYONE),
        },
    )

    assert visible_playback_sources(mass, _user(OWNER)) == [
        "filesystem_local--mine",
        "filesystem_local--household",
        "spotify--mine",
    ]


def test_visible_playback_sources_leaves_a_service_without_an_instance_alone() -> None:
    """With no instance of a service loaded, nothing of it can play, so nothing is narrowed."""
    mass = _mass()
    set_music_source_access(
        mass,
        {
            "spotify--mine": ProviderAccess(owner=OWNER, sharing=ProviderSharing.PRIVATE),
            "spotify--theirs": ProviderAccess(owner=MEMBER, sharing=ProviderSharing.EVERYONE),
        },
    )

    assert visible_playback_sources(mass, _user(OWNER)) is None


def test_visible_playback_sources_ignores_a_disabled_own_service() -> None:
    """A disabled source of a service leaves the shared account of it playable."""
    mass = _mass([_provider("spotify--theirs", is_streaming=True)])
    set_music_source_access(
        mass,
        {
            "spotify--mine": ProviderAccess(owner=OWNER, sharing=ProviderSharing.PRIVATE),
            "spotify--theirs": ProviderAccess(owner=MEMBER, sharing=ProviderSharing.EVERYONE),
        },
    )
    mass.config.get(CONF_PROVIDERS, {})["spotify--mine"]["enabled"] = False

    assert visible_playback_sources(mass, _user(OWNER)) is None


def test_visible_playback_sources_narrows_a_user_that_sees_everything() -> None:
    """A user without any hidden source still gets an explicit set once one is dropped."""
    mass = _mass([_provider("spotify--mine", is_streaming=True)])
    set_music_source_access(
        mass,
        {
            "builtin": None,
            "spotify--mine": ProviderAccess(owner=OWNER, sharing=ProviderSharing.PRIVATE),
            "spotify--theirs": ProviderAccess(owner=MEMBER, sharing=ProviderSharing.EVERYONE),
        },
    )

    assert visible_music_sources(mass, _user(OWNER)) is None
    assert visible_playback_sources(mass, _user(OWNER)) == ["builtin", "spotify--mine"]


def test_visible_playback_sources_leaves_anonymous_playback_alone() -> None:
    """Anonymous playback owns nothing, so every open account of a service stays usable."""
    mass = _mass()
    set_music_source_access(
        mass,
        {
            "spotify--mine": ProviderAccess(owner=OWNER, sharing=ProviderSharing.EVERYONE),
            "spotify--theirs": ProviderAccess(owner=MEMBER, sharing=ProviderSharing.EVERYONE),
        },
    )

    assert visible_playback_sources(mass, None) is None


def test_playback_instance_for_keeps_a_source_the_user_may_use() -> None:
    """A source within the playback set serves its own items."""
    mass = _mass([_provider("spotify--mine", is_streaming=True)])
    set_music_source_access(
        mass, {"spotify--mine": ProviderAccess(owner=OWNER, sharing=ProviderSharing.PRIVATE)}
    )

    assert playback_instance_for(mass, "spotify--mine", ["spotify--mine"]) == "spotify--mine"
    assert playback_instance_for(mass, "spotify--theirs", None) == "spotify--theirs"


def test_playback_instance_for_swaps_in_the_own_account_of_a_service() -> None:
    """An account left out of the playback set is served by the user's own account of it."""
    mass = _mass(
        [
            _provider("spotify--mine", is_streaming=True),
            _provider("spotify--theirs", is_streaming=True),
        ]
    )
    set_music_source_access(
        mass,
        {
            "spotify--mine": ProviderAccess(owner=OWNER, sharing=ProviderSharing.PRIVATE),
            "spotify--theirs": ProviderAccess(owner=MEMBER, sharing=ProviderSharing.EVERYONE),
        },
    )

    assert playback_instance_for(mass, "spotify--theirs", ["spotify--mine"]) == "spotify--mine"


def test_playback_instance_for_skips_an_account_that_can_not_play() -> None:
    """An account without a loaded provider plays nothing, so it never stands in."""
    mass = _mass([_provider("spotify--theirs", is_streaming=True)])
    set_music_source_access(
        mass,
        {
            "spotify--mine": ProviderAccess(owner=OWNER, sharing=ProviderSharing.PRIVATE),
            "spotify--theirs": ProviderAccess(owner=MEMBER, sharing=ProviderSharing.EVERYONE),
        },
    )

    assert playback_instance_for(mass, "spotify--theirs", ["spotify--mine"]) is None


def test_playback_instance_for_never_swaps_a_local_source() -> None:
    """Instances of a local source are libraries of their own, so neither stands in."""
    mass = _mass(
        [
            _provider("filesystem_local--mine", is_streaming=False),
            _provider("filesystem_local--theirs", is_streaming=False),
        ]
    )
    set_music_source_access(
        mass,
        {
            "filesystem_local--mine": ProviderAccess(owner=OWNER, sharing=ProviderSharing.PRIVATE),
            "filesystem_local--theirs": ProviderAccess(
                owner=MEMBER, sharing=ProviderSharing.EVERYONE
            ),
        },
    )

    assert (
        playback_instance_for(mass, "filesystem_local--theirs", ["filesystem_local--mine"]) is None
    )


def test_own_music_sources_and_source_owner() -> None:
    """Ownership is read straight off the access record."""
    mass = _mass()
    access = ProviderAccess(owner=OWNER, sharing=ProviderSharing.MEMBERS)
    set_music_source_access(mass, {"builtin": None, "spotify--aaaa": access})
    assert own_music_sources(mass, _user(OWNER)) == ["spotify--aaaa"]
    assert own_music_sources(mass, _user(MEMBER)) == []
    assert own_music_sources(mass, None) == []
    assert source_owner(mass, "spotify--aaaa") == OWNER
    assert source_owner(mass, "builtin") is None
    assert source_access(mass, "spotify--aaaa") == access
    assert source_access(mass, "builtin") is None


def test_derived_provider_filter_is_empty_when_nothing_is_hidden() -> None:
    """The legacy filter shape spells 'sees everything' as an empty list."""
    mass = _mass()
    set_music_source_access(
        mass,
        {
            "spotify--aaaa": None,
            "tidal--bbbb": ProviderAccess(owner=OWNER, sharing=ProviderSharing.PRIVATE),
        },
    )
    assert derived_provider_filter(mass, _user(OWNER)) == []
    assert derived_provider_filter(mass, _user(MEMBER)) == ["spotify--aaaa"]


async def test_playback_sources_of_a_queue() -> None:
    """The queue's recorded playback user decides which sources may serve it."""
    mass = _mass()
    set_music_source_access(
        mass,
        {
            "builtin": None,
            "spotify--aaaa": ProviderAccess(owner=OWNER, sharing=ProviderSharing.PRIVATE),
            "tidal--bbbb": ProviderAccess(owner=MEMBER, sharing=ProviderSharing.PRIVATE),
        },
    )
    mass.player_queues.queue_data_or_none = MagicMock(return_value=MagicMock(userid=OWNER))
    mass.webserver.auth.get_user = AsyncMock(return_value=_user(OWNER))

    allowed, preferred = await playback_sources(mass, "queue-1")

    assert allowed == ["builtin", "spotify--aaaa"]
    assert preferred == ["spotify--aaaa"]


async def test_playback_sources_of_an_anonymous_queue() -> None:
    """A queue that never had a user falls back to anonymous playback."""
    mass = _mass()
    set_music_source_access(
        mass,
        {
            "builtin": None,
            "spotify--aaaa": ProviderAccess(owner=OWNER, sharing=ProviderSharing.PRIVATE),
        },
    )
    mass.player_queues.queue_data_or_none = MagicMock(return_value=MagicMock(userid=None))

    allowed, preferred = await playback_sources(mass, "queue-1")

    assert allowed == ["builtin"]
    assert preferred == []


async def test_playback_sources_prefers_the_users_own_account_of_a_service() -> None:
    """The queue plays through the user's own account, never the shared one beside it."""
    mass = _mass([_provider("spotify--mine", is_streaming=True)])
    set_music_source_access(
        mass,
        {
            "builtin": None,
            "spotify--mine": ProviderAccess(owner=OWNER, sharing=ProviderSharing.PRIVATE),
            "spotify--theirs": ProviderAccess(owner=MEMBER, sharing=ProviderSharing.EVERYONE),
        },
    )
    mass.player_queues.queue_data_or_none = MagicMock(return_value=MagicMock(userid=OWNER))
    mass.webserver.auth.get_user = AsyncMock(return_value=_user(OWNER))

    allowed, preferred = await playback_sources(mass, "queue-1")

    assert allowed == ["builtin", "spotify--mine"]
    assert preferred == ["spotify--mine"]


async def test_playback_sources_skips_a_disabled_own_source() -> None:
    """A disabled source of the user is no playback target to steer to."""
    mass = _mass([_provider("spotify--theirs", is_streaming=True)])
    set_music_source_access(
        mass,
        {
            "spotify--mine": ProviderAccess(owner=OWNER, sharing=ProviderSharing.PRIVATE),
            "spotify--theirs": ProviderAccess(owner=MEMBER, sharing=ProviderSharing.EVERYONE),
        },
    )
    mass.config.get(CONF_PROVIDERS, {})["spotify--mine"]["enabled"] = False
    mass.player_queues.queue_data_or_none = MagicMock(return_value=MagicMock(userid=OWNER))
    mass.webserver.auth.get_user = AsyncMock(return_value=_user(OWNER))

    allowed, preferred = await playback_sources(mass, "queue-1")

    assert allowed is None
    assert preferred == []
