"""
Tests for the cleanup that runs when a player, one of its protocols or a provider is removed.

A removed player takes the configs of its protocols, its DSP and queue settings and its
saved queue with it, also when none of its protocols is attached (a disabled player, or
one whose provider is not loaded), and leaves what belongs to other players alone. A
removed protocol takes its config with it, and its player when no other protocol of it is
left. Removing a player provider removes its protocols that way. A removed player also
disappears from the per user access filters, which would otherwise keep pointing at
something that no longer exists.
"""

import asyncio
from typing import cast
from unittest.mock import MagicMock

from music_assistant_models.enums import PlayerType, ProtocolRole

from music_assistant.constants import (
    CONF_GROUP_MEMBERS,
    CONF_PLAYER_DSP,
    CONF_PLAYER_PROTOCOLS,
    CONF_PLAYER_QUEUES,
    CONF_PLAYERS,
    CONF_PROVIDERS,
)
from music_assistant.controllers.player_queues.constants import (
    CACHE_CATEGORY_PLAYER_QUEUE_ITEMS,
    CACHE_CATEGORY_PLAYER_QUEUE_STATE,
)
from music_assistant.helpers.json import json_loads
from music_assistant.mass import MusicAssistant
from music_assistant.models.player import Player
from tests.common import MockProtocol, MockProvider, attach_mock

# a player that was first seen after the split, and two protocols of its device
PLAYER_ID = "pl0123456789ab"
NATIVE_ID = "native_1"
AUDIO_ID = "sendspin_1"
# a real, non-builtin player provider with no config entries of its own, so a raw
# provider config can be stored without going through the setup flow; it is
# single-instance, so its instance id equals its domain
PLAYER_PROVIDER_DOMAIN = "dlna"
OTHER_PROVIDER_INSTANCE_ID = "other_provider"


def _store_player(
    mass: MusicAssistant,
    player_id: str = PLAYER_ID,
    *,
    enabled: bool = True,
    provider: str = OTHER_PROVIDER_INSTANCE_ID,
) -> None:
    """Store the config of a player with customised DSP and queue settings."""
    mass.config.create_player_record(
        player_id,
        provider=provider,
        player_type=PlayerType.PLAYER,
        default_name="Player",
        enabled=enabled,
    )
    mass.config.set(f"{CONF_PLAYER_DSP}/{player_id}", {"enabled": True})
    mass.config.set(
        f"{CONF_PLAYER_QUEUES}/{player_id}",
        {"queue_id": player_id, "values": {"crossfade_duration": 9}},
    )


def _store_protocol(
    mass: MusicAssistant,
    protocol_id: str,
    player_id: str | None = PLAYER_ID,
    *,
    provider: str = OTHER_PROVIDER_INSTANCE_ID,
    role: ProtocolRole = ProtocolRole.NATIVE,
) -> None:
    """Store the config of a protocol of a player."""
    mass.config.create_protocol_record(
        protocol_id, provider=provider, player_id=player_id, role=role, default_name="Player"
    )


def _stored_protocols(mass: MusicAssistant) -> list[str]:
    """Return the ids of the stored protocol configs."""
    return sorted(mass.config.get(CONF_PLAYER_PROTOCOLS, {}))


async def _attach(
    mass: MusicAssistant, protocol_id: str = NATIVE_ID
) -> tuple[Player, MockProtocol]:
    """Attach a mock protocol to the stored player, with a queue of its own."""
    provider = MockProvider(
        OTHER_PROVIDER_INSTANCE_ID,
        instance_id=OTHER_PROVIDER_INSTANCE_ID,
        mass=cast("MagicMock", mass),
    )
    player, protocol = attach_mock(provider, protocol_id, "Player", player_id=PLAYER_ID)
    await mass.player_queues.on_player_register(player)
    return player, protocol


async def _store_queue_cache(mass: MusicAssistant, player_id: str) -> None:
    """Store cached queue state and items for the given player."""
    for category in (CACHE_CATEGORY_PLAYER_QUEUE_STATE, CACHE_CATEGORY_PLAYER_QUEUE_ITEMS):
        await mass.cache.set(
            key=player_id,
            data={"queue_id": player_id},
            provider="player_queues",
            category=category,
            persistent=True,
        )


async def _get_queue_cache(mass: MusicAssistant, player_id: str) -> list[object]:
    """Return the cached queue state and items for the given player."""
    return [
        await mass.cache.get(key=player_id, provider="player_queues", category=category)
        for category in (CACHE_CATEGORY_PLAYER_QUEUE_STATE, CACHE_CATEGORY_PLAYER_QUEUE_ITEMS)
    ]


def _assert_player_wiped(mass: MusicAssistant, player_id: str = PLAYER_ID) -> None:
    """Assert that nothing is stored for a player anymore."""
    for tree in (CONF_PLAYERS, CONF_PLAYER_DSP, CONF_PLAYER_QUEUES):
        assert mass.config.get(f"{tree}/{player_id}") is None
    assert mass.config.get_protocol_ids_of_player(player_id) == []


def _store_provider_config(mass: MusicAssistant) -> None:
    """Store a raw config for the player provider under test."""
    mass.config.set(
        f"{CONF_PROVIDERS}/{PLAYER_PROVIDER_DOMAIN}",
        {
            "type": "player",
            "domain": PLAYER_PROVIDER_DOMAIN,
            "instance_id": PLAYER_PROVIDER_DOMAIN,
            "enabled": True,
            "name": "DLNA",
            "values": {},
        },
    )


async def test_remove_of_a_disabled_player_wipes_the_configs_of_its_protocols(
    mass: MusicAssistant,
) -> None:
    """A player without attached protocols takes the configs of all its protocols with it."""
    _store_player(mass, enabled=False)
    _store_protocol(mass, NATIVE_ID)
    _store_protocol(mass, AUDIO_ID, role=ProtocolRole.AUDIO)
    await _store_queue_cache(mass, PLAYER_ID)

    await mass.config.remove_player_config(PLAYER_ID)

    _assert_player_wiped(mass)
    assert _stored_protocols(mass) == []
    assert await _get_queue_cache(mass, PLAYER_ID) == [None, None]


async def test_remove_leaves_what_belongs_to_another_player_alone(mass: MusicAssistant) -> None:
    """A protocol of another player keeps its config and stays attached to that player."""
    _store_player(mass)
    _store_protocol(mass, NATIVE_ID)
    _store_player(mass, "pl_other")
    _store_protocol(mass, "other_1", "pl_other")
    provider = MockProvider(
        OTHER_PROVIDER_INSTANCE_ID,
        instance_id=OTHER_PROVIDER_INSTANCE_ID,
        mass=cast("MagicMock", mass),
    )
    other_player, other = attach_mock(provider, "other_1", "Other", player_id="pl_other")

    await mass.players.remove(PLAYER_ID)

    _assert_player_wiped(mass)
    assert _stored_protocols(mass) == ["other_1"]
    assert mass.config.get(f"{CONF_PLAYERS}/pl_other") is not None
    assert mass.config.get(f"{CONF_PLAYER_QUEUES}/pl_other") is not None
    assert other.player is other_player
    assert mass.players.get_player("pl_other") is other_player


async def test_remove_of_a_live_player_detaches_its_protocols_and_wipes_its_queue(
    mass: MusicAssistant,
) -> None:
    """A removed player is gone with its queue, and its protocol is told and let go."""
    _store_player(mass)
    _store_protocol(mass, NATIVE_ID)
    _, protocol = await _attach(mass)
    removed: list[str] = []

    async def _on_remove() -> None:
        removed.append(protocol.protocol_id)

    protocol.on_remove = _on_remove  # type: ignore[method-assign]
    await _store_queue_cache(mass, PLAYER_ID)

    await mass.config.remove_player_config(PLAYER_ID)

    assert mass.players.get_player(PLAYER_ID) is None
    assert mass.players.get_protocol(NATIVE_ID) is None
    assert protocol.player is None
    assert removed == [NATIVE_ID]
    assert mass.player_queues.get(PLAYER_ID) is None
    _assert_player_wiped(mass)
    assert _stored_protocols(mass) == []
    assert await _get_queue_cache(mass, PLAYER_ID) == [None, None]


async def test_removed_protocol_that_is_not_attached_leaves_the_outputs_of_its_player(
    mass: MusicAssistant,
) -> None:
    """A protocol whose config is removed is no output of its player anymore."""
    _store_player(mass)
    _store_protocol(mass, NATIVE_ID)
    _store_protocol(mass, AUDIO_ID, role=ProtocolRole.AUDIO)
    player, _ = await _attach(mass)
    player.protocol_ids.add(AUDIO_ID)
    player.refresh_state(signal_event=False)
    assert AUDIO_ID in {output.output_protocol_id for output in player._output_protocols}

    await mass.players.remove_protocol(AUDIO_ID)

    assert _stored_protocols(mass) == [NATIVE_ID]
    assert AUDIO_ID not in player.protocol_ids
    assert AUDIO_ID not in {output.output_protocol_id for output in player._output_protocols}
    assert AUDIO_ID not in {protocol.protocol_id for protocol in player.state.protocols}
    assert mass.players.get_player(PLAYER_ID) is player


async def test_removing_the_last_protocol_removes_its_player(mass: MusicAssistant) -> None:
    """A player whose device is away goes once the config of its last protocol is removed."""
    _store_player(mass)
    _store_protocol(mass, AUDIO_ID, role=ProtocolRole.AUDIO)
    await _store_queue_cache(mass, PLAYER_ID)

    await mass.players.remove_protocol(AUDIO_ID)

    _assert_player_wiped(mass)
    assert await _get_queue_cache(mass, PLAYER_ID) == [None, None]


async def test_remove_provider_config_wipes_its_players(mass: MusicAssistant) -> None:
    """Removing a provider also wipes the players that only had protocols of it."""
    _store_provider_config(mass)
    _store_player(mass, provider=PLAYER_PROVIDER_DOMAIN)
    _store_protocol(mass, NATIVE_ID, provider=PLAYER_PROVIDER_DOMAIN)
    await _store_queue_cache(mass, PLAYER_ID)

    await mass.config.remove_provider_config(PLAYER_PROVIDER_DOMAIN)

    _assert_player_wiped(mass)
    assert _stored_protocols(mass) == []
    assert await _get_queue_cache(mass, PLAYER_ID) == [None, None]


async def test_remove_provider_config_keeps_a_player_with_other_protocols(
    mass: MusicAssistant,
) -> None:
    """A player keeps its config, its settings and its queue on the protocols of others."""
    _store_provider_config(mass)
    _store_player(mass, provider=PLAYER_PROVIDER_DOMAIN)
    _store_protocol(mass, NATIVE_ID, provider=PLAYER_PROVIDER_DOMAIN)
    _store_protocol(mass, AUDIO_ID, role=ProtocolRole.AUDIO)
    _store_player(mass, "pl_other")
    _store_protocol(mass, "other_1", "pl_other")
    await _store_queue_cache(mass, PLAYER_ID)

    await mass.config.remove_provider_config(PLAYER_PROVIDER_DOMAIN)

    assert _stored_protocols(mass) == ["other_1", AUDIO_ID]
    for player_id in (PLAYER_ID, "pl_other"):
        assert mass.config.get(f"{CONF_PLAYERS}/{player_id}") is not None
        assert mass.config.get(f"{CONF_PLAYER_DSP}/{player_id}") is not None
        assert mass.config.get(f"{CONF_PLAYER_QUEUES}/{player_id}") is not None
    assert await _get_queue_cache(mass, PLAYER_ID) == [
        {"queue_id": PLAYER_ID},
        {"queue_id": PLAYER_ID},
    ]


async def test_remove_drops_the_player_from_the_member_lists_of_the_groups(
    mass: MusicAssistant,
) -> None:
    """A group names its members by player id, and loses a member that is removed."""
    _store_player(mass)
    _store_protocol(mass, NATIVE_ID)
    _store_player(mass, "pl_group")
    _store_protocol(mass, "group_1", "pl_group", role=ProtocolRole.VIRTUAL)
    mass.config.set_protocol_value("group_1", CONF_GROUP_MEMBERS, [PLAYER_ID, "pl_other"])
    # the id of the protocol of the player is no member, whatever a group lists
    _store_player(mass, "pl_group_2")
    _store_protocol(mass, "group_2", "pl_group_2", role=ProtocolRole.VIRTUAL)
    mass.config.set_protocol_value("group_2", CONF_GROUP_MEMBERS, [NATIVE_ID, "pl_other"])

    await mass.players.remove(PLAYER_ID)

    assert mass.config.get_protocol_value("group_1", CONF_GROUP_MEMBERS) == ["pl_other"]
    assert mass.config.get_protocol_value("group_2", CONF_GROUP_MEMBERS) == [
        NATIVE_ID,
        "pl_other",
    ]
    assert mass.config.get_player_value("pl_group", CONF_GROUP_MEMBERS) is None


async def _get_user_player_filter(mass: MusicAssistant, user_id: str) -> list[str]:
    """Read the raw player filter of the given user."""
    row = await mass.webserver.auth.database.get_row("users", {"user_id": user_id})
    assert row is not None
    return cast("list[str]", json_loads(row["player_filter"]))


async def test_remove_player_config_strips_user_player_filter(mass: MusicAssistant) -> None:
    """Removing a player also removes it from the access filters of restricted users."""
    _store_player(mass)
    user = await mass.webserver.auth.create_user(
        username="restricted", player_filter=[PLAYER_ID, "other_player"]
    )

    await mass.config.remove_player_config(PLAYER_ID)

    # the filter cleanup is scheduled by the (non-async) config wipe
    deadline = asyncio.get_running_loop().time() + 5.0
    while await _get_user_player_filter(mass, user.user_id) != ["other_player"]:
        assert asyncio.get_running_loop().time() < deadline, "player filter was not cleaned up"
        await asyncio.sleep(0.01)
