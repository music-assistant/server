"""
Tests for the one-shot conversion of the user filters into music source access records.

Which music sources a user could use was an allow-list of provider instance ids on the
user; it now lives on the source itself, as its owner plus who it is shared with. The
conversion has to reproduce exactly what each user could see: a source nobody was kept
out of becomes a household source, a source exactly one restricted user was given
becomes theirs, and everything else is shared with the users that could reach it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.auth import UserRole
from music_assistant_models.config_entries import ProviderAccess
from music_assistant_models.enums import ProviderSharing

from music_assistant.constants import CONF_PROVIDER_ACCESS_MIGRATED, CONF_PROVIDERS
from music_assistant.controllers.config.provider_access_migration import (
    _normalized_filter,
    migrate_provider_access,
)
from music_assistant.helpers.json import json_dumps, json_loads
from music_assistant.helpers.provider_access import visible_music_sources
from tests.common import set_music_source_access

if TYPE_CHECKING:
    from music_assistant_models.auth import User

    from music_assistant.mass import MusicAssistant


async def _add_user(
    mass: MusicAssistant,
    username: str,
    sources: list[str] | None = None,
    role: UserRole = UserRole.USER,
) -> User:
    """
    Add a user that is restricted to the given music sources, or to nothing at all.

    :param mass: The MusicAssistant instance to add the user to.
    :param username: The username of the new user.
    :param sources: The provider instance ids the user's filter names, if any.
    :param role: The role of the new user.
    """
    user = await mass.webserver.auth.create_user(username=username, role=role)
    if sources:
        await mass.webserver.auth.database.update(
            "users", {"user_id": user.user_id}, {"provider_filter": json_dumps(sources)}
        )
    return user


async def _stored_filters(mass: MusicAssistant) -> list[list[str]]:
    """Return the stored provider filter of every user."""
    rows = await mass.webserver.auth.database.get_rows("users", limit=0)
    return [json_loads(row["provider_filter"]) for row in rows]


def _access(mass: MusicAssistant, instance_id: str) -> ProviderAccess | None:
    """Return the access record stored on the given music source, if it carries one."""
    if raw_access := mass.config.get(f"{CONF_PROVIDERS}/{instance_id}/access"):
        return ProviderAccess.from_dict(raw_access)
    return None


def _visible(mass: MusicAssistant, user: User, sources: list[str]) -> list[str]:
    """
    Return which of the given music sources the user may see.

    :param mass: The MusicAssistant instance holding the sources.
    :param user: The user to resolve the music sources for.
    :param sources: The instance ids to report on, ignoring the sources of the install.
    """
    visible = visible_music_sources(mass, user)
    return sources if visible is None else [x for x in sources if x in visible]


def _prepare(mass: MusicAssistant, sources: list[str]) -> None:
    """
    Give the server the given music sources, none of which carries an access record yet.

    :param mass: The MusicAssistant instance to store the sources on.
    :param sources: The instance ids of the music sources to store.
    """
    set_music_source_access(mass, dict.fromkeys(sources))
    # the full boot of the fixture already ran (and marked) the conversion
    mass.config.remove(CONF_PROVIDER_ACCESS_MIGRATED)


async def test_install_without_restrictions_keeps_everything_visible(
    mass: MusicAssistant,
) -> None:
    """Without a single restricted user every source becomes a household source."""
    _prepare(mass, ["spotify--one", "tidal--two"])
    user = await _add_user(mass, "plain")

    await migrate_provider_access(mass)

    for instance_id in ("spotify--one", "tidal--two"):
        assert _access(mass, instance_id) == ProviderAccess(sharing=ProviderSharing.EVERYONE)
    assert visible_music_sources(mass, user) is None
    assert mass.config.get(CONF_PROVIDER_ACCESS_MIGRATED) is True


async def test_restricted_user_becomes_the_owner(mass: MusicAssistant) -> None:
    """A source given to exactly one restricted user becomes theirs, the rest is shared."""
    _prepare(mass, ["spotify--alice", "tidal--house"])
    alice = await _add_user(mass, "alice", ["spotify--alice"])
    bob = await _add_user(mass, "bob")

    await migrate_provider_access(mass)

    # bob was unrestricted, so alice's source stays visible to him as well
    assert _access(mass, "spotify--alice") == ProviderAccess(
        owner=alice.user_id, sharing=ProviderSharing.EVERYONE
    )
    assert _access(mass, "tidal--house") == ProviderAccess(
        sharing=ProviderSharing.SELECTED, shared_users=[bob.user_id]
    )
    # which is exactly what both of them could see before
    assert _visible(mass, alice, ["spotify--alice", "tidal--house"]) == ["spotify--alice"]
    assert visible_music_sources(mass, bob) is None


async def test_source_of_two_restricted_users_has_no_owner(mass: MusicAssistant) -> None:
    """A source that several restricted users were given belongs to none of them."""
    _prepare(mass, ["spotify--shared", "tidal--dave"])
    alice = await _add_user(mass, "alice", ["spotify--shared"])
    carol = await _add_user(mass, "carol", ["spotify--shared"])
    await _add_user(mass, "dave", ["tidal--dave"])

    await migrate_provider_access(mass)

    assert _access(mass, "spotify--shared") == ProviderAccess(
        sharing=ProviderSharing.SELECTED,
        shared_users=sorted([alice.user_id, carol.user_id]),
    )


async def test_a_guest_does_not_become_the_owner_of_a_source(mass: MusicAssistant) -> None:
    """A guest can not own a music source, so it is only shared with the one it was given."""
    _prepare(mass, ["spotify--guest", "tidal--dave"])
    system_user = await mass.webserver.auth.get_homeassistant_system_user()
    guest = await _add_user(mass, "party_guest", ["spotify--guest"], role=UserRole.GUEST)
    await _add_user(mass, "dave", ["tidal--dave"])

    await migrate_provider_access(mass)

    assert _access(mass, "spotify--guest") == ProviderAccess(
        sharing=ProviderSharing.SELECTED,
        shared_users=sorted([guest.user_id, system_user.user_id]),
    )
    # which is exactly what the guest could see before
    assert _visible(mass, guest, ["spotify--guest", "tidal--dave"]) == ["spotify--guest"]


async def test_filter_of_removed_sources_leaves_the_user_unrestricted(
    mass: MusicAssistant,
) -> None:
    """A filter that names only sources that are gone must not lock its user out."""
    _prepare(mass, ["tidal--house"])
    stale = await _add_user(mass, "stale", ["spotify--removed"])

    await migrate_provider_access(mass)

    assert _access(mass, "tidal--house") == ProviderAccess(sharing=ProviderSharing.EVERYONE)
    assert visible_music_sources(mass, stale) is None


async def test_filter_follows_collapsed_plugin_instance(mass: MusicAssistant) -> None:
    """A filter naming a collapsed plugin instance keeps its user restricted."""
    _prepare(mass, ["spotify--live"])
    mass.config.set(
        f"{CONF_PROVIDERS}/spotify_connect",
        {"type": "plugin", "domain": "spotify_connect", "instance_id": "spotify_connect"},
    )
    collapsed = await _add_user(mass, "collapsed", ["spotify_connect--abcd1234"])
    bob = await _add_user(mass, "bob")

    await migrate_provider_access(mass)

    # the entry follows the collapsed instance, so the user is still restricted to a
    # plugin and reaches no music source at all
    assert _access(mass, "spotify--live") == ProviderAccess(
        sharing=ProviderSharing.SELECTED, shared_users=[bob.user_id]
    )
    assert _visible(mass, collapsed, ["spotify--live"]) == []


async def test_builtin_source_is_skipped(mass: MusicAssistant) -> None:
    """The builtin provider serves the household and never carries an access record."""
    _prepare(mass, ["builtin", "spotify--alice"])
    alice = await _add_user(mass, "alice", ["spotify--alice"])
    await _add_user(mass, "bob")

    await migrate_provider_access(mass)

    # a restricted user whose filter did not name it gains it, which is accepted
    assert _access(mass, "builtin") is None
    assert _visible(mass, alice, ["builtin"]) == ["builtin"]
    assert _access(mass, "spotify--alice") is not None


async def test_existing_record_is_left_alone(mass: MusicAssistant) -> None:
    """A source that already carries a record keeps it, whatever the filters say."""
    stored = ProviderAccess(owner="someone", sharing=ProviderSharing.PRIVATE)
    set_music_source_access(mass, {"spotify--own": stored, "tidal--house": None})
    mass.config.remove(CONF_PROVIDER_ACCESS_MIGRATED)
    await _add_user(mass, "alice", ["spotify--own"])

    await migrate_provider_access(mass)

    assert _access(mass, "spotify--own") == stored
    assert _access(mass, "tidal--house") is not None


async def test_rerun_does_not_widen_the_records(mass: MusicAssistant) -> None:
    """A conversion that ran but was not marked done leaves its records as they are."""
    _prepare(mass, ["spotify--alice", "tidal--house"])
    alice = await _add_user(mass, "alice", ["spotify--alice"])
    await _add_user(mass, "dave", ["tidal--house"])
    await migrate_provider_access(mass)
    converted = _access(mass, "spotify--alice")

    mass.config.remove(CONF_PROVIDER_ACCESS_MIGRATED)
    await migrate_provider_access(mass)

    assert _access(mass, "spotify--alice") == converted
    assert converted == ProviderAccess(
        owner=alice.user_id, sharing=ProviderSharing.SELECTED, shared_users=[]
    )


async def test_unreadable_sources_do_not_stop_the_conversion(mass: MusicAssistant) -> None:
    """A source with a broken record or an unknown domain is hidden, the rest converts."""
    _prepare(mass, ["spotify--alice"])
    mass.config.set(
        f"{CONF_PROVIDERS}/gone--forever",
        {"type": "music", "domain": "gone", "instance_id": "gone--forever"},
    )
    mass.config.set(
        f"{CONF_PROVIDERS}/tidal--broken",
        {
            "type": "music",
            "domain": "tidal",
            "instance_id": "tidal--broken",
            # a shared_users that is not a list makes the record unreadable
            "access": {"shared_users": 5},
        },
    )
    alice = await _add_user(mass, "alice", ["spotify--alice"])

    await migrate_provider_access(mass)

    assert _access(mass, "spotify--alice") is not None
    assert _access(mass, "gone--forever") == ProviderAccess(sharing=ProviderSharing.PRIVATE)
    assert mass.config.get(f"{CONF_PROVIDERS}/tidal--broken/access") == {"shared_users": 5}
    # a record nobody can read hides its source instead of exposing it
    assert _visible(mass, alice, ["tidal--broken"]) == []
    assert mass.config.get(CONF_PROVIDER_ACCESS_MIGRATED) is True


async def test_filters_are_cleared_and_the_marker_stops_a_second_run(
    mass: MusicAssistant,
) -> None:
    """The filters are dropped once converted, and the marker keeps them that way."""
    _prepare(mass, ["spotify--alice", "tidal--house"])
    await _add_user(mass, "alice", ["spotify--alice"])
    await _add_user(mass, "bob")

    await migrate_provider_access(mass)

    assert await _stored_filters(mass) == [[], []]
    # a source added later must not be converted against the (now empty) filters
    set_music_source_access(mass, {"qobuz--new": None})
    await migrate_provider_access(mass)
    assert _access(mass, "qobuz--new") is None


async def test_system_user_is_a_shared_user_of_a_selected_source(mass: MusicAssistant) -> None:
    """The Home Assistant system user is unrestricted, so it keeps every source it had."""
    _prepare(mass, ["spotify--alice", "tidal--dave"])
    system_user = await mass.webserver.auth.get_homeassistant_system_user()
    await _add_user(mass, "alice", ["spotify--alice"])
    dave = await _add_user(mass, "dave", ["tidal--dave"])

    await migrate_provider_access(mass)

    assert _access(mass, "tidal--dave") == ProviderAccess(
        owner=dave.user_id,
        sharing=ProviderSharing.SELECTED,
        shared_users=[system_user.user_id],
    )


async def test_an_unreadable_filter_leaves_the_user_unrestricted(mass: MusicAssistant) -> None:
    """A filter column that no longer holds a JSON list must not lock its user out."""
    _prepare(mass, ["spotify--one"])
    garbage = await _add_user(mass, "garbage")
    await mass.webserver.auth.database.update(
        "users", {"user_id": garbage.user_id}, {"provider_filter": "not json"}
    )

    await migrate_provider_access(mass)

    assert _access(mass, "spotify--one") == ProviderAccess(sharing=ProviderSharing.EVERYONE)
    assert visible_music_sources(mass, garbage) is None


def test_only_a_stored_list_of_sources_restricts_a_user() -> None:
    """Whatever else the filter column holds, including NULL, reads as unrestricted."""
    for stored in (None, "", "not json", '"spotify--one"', "{}"):
        assert _normalized_filter({"provider_filter": stored}, {"spotify--one"}) == set()
