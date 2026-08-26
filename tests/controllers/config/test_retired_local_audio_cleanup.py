"""
Tests for the one-shot cleanup of the retired local_audio provider.

The provider was builtin and enumerated every output device of the host, so a machine
that merely has a sound card carries a provider config and one player config per device.
Now that the provider is retired and fails to load, those artefacts would raise a
retirement banner at a user who never played a note through them. The cleanup decides on
evidence of playback: a playlog row keyed to the player, or a persisted queue that holds
something. Anything less and the whole lot is removed; anything more and it all stays.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

from music_assistant.constants import (
    CONF_PLAYER_DSP,
    CONF_PLAYER_QUEUES,
    CONF_PLAYERS,
    CONF_PROTOCOL_PARENT_ID,
    CONF_PROVIDERS,
    CONF_RETIRED_LOCAL_AUDIO_CLEANED,
    DB_TABLE_PLAYLOG,
)
from music_assistant.controllers.config.retired_local_audio import cleanup_retired_local_audio
from music_assistant.controllers.player_queues.constants import (
    CACHE_CATEGORY_PLAYER_QUEUE_ITEMS,
    CACHE_CATEGORY_PLAYER_QUEUE_STATE,
)
from tests.conftest import full_mass_context

if TYPE_CHECKING:
    import pathlib

    import pytest

    from music_assistant.mass import MusicAssistant

ANALOG_ID = "local_audio_analog"
HDMI_ID = "local_audio_hdmi"
BRIDGE_ID = "spb_analog"
OTHER_PLAYER_ID = "cast_kitchen"


def _store_install(mass: MusicAssistant, provider_enabled: bool = True) -> None:
    """Store the config shape a pre-retirement install with a sound card carries."""
    mass.config.set(
        f"{CONF_PROVIDERS}/local_audio",
        {
            "type": "player",
            "domain": "local_audio",
            "instance_id": "local_audio",
            "enabled": provider_enabled,
            "name": "Local Audio Out",
            "values": {},
        },
    )
    for player_id in (ANALOG_ID, HDMI_ID):
        mass.config.set(
            f"{CONF_PLAYERS}/{player_id}",
            {
                "player_id": player_id,
                "provider": "local_audio",
                "player_type": "player",
                "enabled": True,
                "values": {},
            },
        )
        mass.config.set(f"{CONF_PLAYER_QUEUES}/{player_id}", {"queue_id": player_id, "values": {}})
        mass.config.set(f"{CONF_PLAYER_DSP}/{player_id}", {"enabled": True})
    # the sendspin bridge child that the local audio player was linked to
    mass.config.set(
        f"{CONF_PLAYERS}/{BRIDGE_ID}",
        {
            "player_id": BRIDGE_ID,
            "provider": "sendspin",
            "player_type": "protocol",
            "enabled": True,
            "values": {CONF_PROTOCOL_PARENT_ID: ANALOG_ID},
        },
    )
    # an unrelated player that must survive untouched
    mass.config.set(
        f"{CONF_PLAYERS}/{OTHER_PLAYER_ID}",
        {
            "player_id": OTHER_PLAYER_ID,
            "provider": "chromecast",
            "player_type": "player",
            "enabled": True,
            "values": {},
        },
    )
    # the full boot of the fixture already ran (and marked) the cleanup
    mass.config.remove(CONF_RETIRED_LOCAL_AUDIO_CLEANED)


async def _store_playlog_entry(mass: MusicAssistant, player_id: str) -> None:
    """Store a playlog row that credits the given player/queue with a playback."""
    await mass.music.database.insert(
        DB_TABLE_PLAYLOG,
        {
            "item_id": "track_1",
            "provider": "spotify",
            "media_type": "track",
            "name": "Some Track",
            "userid": "someuser",
            "queue_id": player_id,
            "timestamp": 1700000000,
            "fully_played": True,
            "seconds_played": 180,
        },
    )


async def _store_queue_cache(
    mass: MusicAssistant, player_id: str, state: dict[str, Any], items: list[Any]
) -> None:
    """Store the persisted queue state and items of the given player."""
    await mass.cache.set(
        key=player_id,
        data=state,
        provider="player_queues",
        category=CACHE_CATEGORY_PLAYER_QUEUE_STATE,
        persistent=True,
    )
    await mass.cache.set(
        key=player_id,
        data=items,
        provider="player_queues",
        category=CACHE_CATEGORY_PLAYER_QUEUE_ITEMS,
        persistent=True,
    )


def _empty_queue_state(player_id: str) -> dict[str, Any]:
    """Return the state a registered but never used queue is flushed with on shutdown."""
    return {
        "cache_format_version": 1,
        "queue": {"queue_id": player_id, "active": False, "items": 0},
        "enqueued_media_items": [],
        "credited_albums": [],
        "source_items": [],
        "userid": None,
    }


async def _read_queue_cache(mass: MusicAssistant, player_id: str) -> list[Any]:
    """Return the persisted queue state and items of the given player."""
    return [
        await mass.cache.get(key=player_id, provider="player_queues", category=category)
        for category in (CACHE_CATEGORY_PLAYER_QUEUE_STATE, CACHE_CATEGORY_PLAYER_QUEUE_ITEMS)
    ]


async def _wait_for_queue_cache_purge(mass: MusicAssistant, player_id: str) -> None:
    """Wait for the (scheduled) purge of the given player's persisted queue."""
    deadline = asyncio.get_running_loop().time() + 5.0
    while await _read_queue_cache(mass, player_id) != [None, None]:
        assert asyncio.get_running_loop().time() < deadline, "saved queue was not purged"
        await asyncio.sleep(0.01)


def _assert_kept(mass: MusicAssistant) -> None:
    """Assert the whole local_audio configuration is still in place."""
    assert mass.config.get(f"{CONF_PROVIDERS}/local_audio") is not None
    assert mass.config.get(f"{CONF_PLAYERS}/{ANALOG_ID}") is not None
    assert mass.config.get(f"{CONF_PLAYERS}/{HDMI_ID}") is not None


async def test_unused_install_is_torched(mass: MusicAssistant) -> None:
    """An install that never played through a sound card loses every local_audio artefact."""
    _store_install(mass)
    await _store_queue_cache(mass, ANALOG_ID, _empty_queue_state(ANALOG_ID), [])

    await cleanup_retired_local_audio(mass)

    assert mass.config.get(f"{CONF_PROVIDERS}/local_audio") is None
    for player_id in (ANALOG_ID, HDMI_ID):
        assert mass.config.get(f"{CONF_PLAYERS}/{player_id}") is None
        assert mass.config.get(f"{CONF_PLAYER_QUEUES}/{player_id}") is None
        assert mass.config.get(f"{CONF_PLAYER_DSP}/{player_id}") is None
    # the orphaned sendspin bridge child follows its dead parent
    assert mass.config.get(f"{CONF_PLAYERS}/{BRIDGE_ID}") is None
    assert mass.config.get(f"{CONF_PLAYERS}/{OTHER_PLAYER_ID}") is not None
    assert mass.config.get(CONF_RETIRED_LOCAL_AUDIO_CLEANED) is True
    await _wait_for_queue_cache_purge(mass, ANALOG_ID)


async def test_empty_saved_queue_is_no_evidence(mass: MusicAssistant) -> None:
    """
    A persisted queue with an empty payload does not count as use.

    The queues controller flushes the state of every registered queue on each clean
    shutdown, so the row exists on any install that ever booted with a sound card.
    """
    _store_install(mass)
    for player_id in (ANALOG_ID, HDMI_ID):
        await _store_queue_cache(mass, player_id, _empty_queue_state(player_id), [])

    await cleanup_retired_local_audio(mass)

    assert mass.config.get(f"{CONF_PROVIDERS}/local_audio") is None
    assert mass.config.get(f"{CONF_PLAYERS}/{ANALOG_ID}") is None
    assert mass.config.get(f"{CONF_PLAYERS}/{HDMI_ID}") is None


async def test_disabled_provider_config_is_torched_too(mass: MusicAssistant) -> None:
    """A disabled provider config is no signal of use; the evidence rule alone decides."""
    _store_install(mass, provider_enabled=False)

    await cleanup_retired_local_audio(mass)

    assert mass.config.get(f"{CONF_PROVIDERS}/local_audio") is None
    assert mass.config.get(f"{CONF_PLAYERS}/{ANALOG_ID}") is None


async def test_playlog_entry_keeps_the_config(mass: MusicAssistant) -> None:
    """A playlog row keyed to a local_audio player proves it was used, so nothing goes."""
    _store_install(mass)
    await _store_playlog_entry(mass, HDMI_ID)

    await cleanup_retired_local_audio(mass)

    _assert_kept(mass)
    assert mass.config.get(f"{CONF_PLAYERS}/{BRIDGE_ID}") is not None
    assert mass.config.get(CONF_RETIRED_LOCAL_AUDIO_CLEANED) is True


async def test_playlog_entry_of_another_player_is_no_evidence(mass: MusicAssistant) -> None:
    """A playlog row credited to some other player says nothing about local audio."""
    _store_install(mass)
    await _store_playlog_entry(mass, OTHER_PLAYER_ID)

    await cleanup_retired_local_audio(mass)

    assert mass.config.get(f"{CONF_PROVIDERS}/local_audio") is None


async def test_non_empty_saved_queue_keeps_the_config(mass: MusicAssistant) -> None:
    """A persisted queue that still holds media proves the player was used."""
    _store_install(mass)
    state = _empty_queue_state(ANALOG_ID)
    state["enqueued_media_items"] = [{"item_id": "track_1", "provider": "spotify"}]
    await _store_queue_cache(mass, ANALOG_ID, state, [])

    await cleanup_retired_local_audio(mass)

    _assert_kept(mass)


async def test_saved_queue_items_keep_the_config(mass: MusicAssistant) -> None:
    """Cached queue items count as evidence even when the state payload looks empty."""
    _store_install(mass)
    await _store_queue_cache(
        mass, HDMI_ID, _empty_queue_state(HDMI_ID), [{"queue_item_id": "qi_1"}]
    )

    await cleanup_retired_local_audio(mass)

    _assert_kept(mass)


async def test_unreadable_library_keeps_the_config(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A question the databases cannot answer keeps everything, so the notice shows."""
    _store_install(mass)

    async def _raise(*_args: Any, **_kwargs: Any) -> int:
        raise RuntimeError("no such column: queue_id")

    monkeypatch.setattr(mass.music.database, "get_count_from_query", _raise)

    await cleanup_retired_local_audio(mass)

    _assert_kept(mass)
    assert "no such column: queue_id" in caplog.text
    # unanswered, so a later (healthy) startup gets to try again
    assert mass.config.get(CONF_RETIRED_LOCAL_AUDIO_CLEANED) is None


async def test_second_run_is_a_no_op(mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    """Once the flag is set the cleanup returns without touching the databases."""
    _store_install(mass)
    await _store_playlog_entry(mass, ANALOG_ID)
    await cleanup_retired_local_audio(mass)
    assert mass.config.get(CONF_RETIRED_LOCAL_AUDIO_CLEANED) is True

    queried = False

    async def _record(*_args: Any, **_kwargs: Any) -> int:
        nonlocal queried
        queried = True
        return 0

    monkeypatch.setattr(mass.music.database, "get_count_from_query", _record)

    await cleanup_retired_local_audio(mass)

    assert not queried
    _assert_kept(mass)


async def test_cleanup_runs_before_the_tombstone_loads(tmp_path: pathlib.Path) -> None:
    """
    A torched install never loads the tombstone, so no banner appears even briefly.

    The end state alone cannot tell whether the cleanup beat the provider load - a
    cleanup running after it would leave the same settings behind - so this watches
    the tombstone's own setup(), which is what records the INCOMPATIBLE status.
    """
    storage_path = tmp_path / "data"
    storage_path.mkdir(parents=True)
    (storage_path / "settings.json").write_text(
        json.dumps(
            {
                CONF_PROVIDERS: {
                    "local_audio": {
                        "type": "player",
                        "domain": "local_audio",
                        "instance_id": "local_audio",
                        "enabled": True,
                        "name": "Local Audio Out",
                        "values": {},
                    }
                },
                CONF_PLAYERS: {
                    ANALOG_ID: {
                        "player_id": ANALOG_ID,
                        "provider": "local_audio",
                        "player_type": "player",
                        "enabled": True,
                        "values": {},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with patch("music_assistant.providers.local_audio.setup", new=AsyncMock()) as tombstone_setup:
        async with full_mass_context(tmp_path) as mass:
            assert tombstone_setup.call_count == 0
            assert mass.config.get(f"{CONF_PROVIDERS}/local_audio") is None
            assert mass.config.get(f"{CONF_PLAYERS}/{ANALOG_ID}") is None
            assert mass.get_provider("local_audio", return_unavailable=True) is None
