"""
Regression tests for protocol-prefixed config values not reverting to default.

Reproduces music-assistant/support#5685: for a player that streams via an audio
protocol (e.g. a Sonos player whose audio output is its DLNA protocol), the config
UI exposes protocol-specific entries as virtual entries keyed
``<protocol_id>||protocol||<actual_key>``. Setting such an entry (e.g.
``flow_mode_sample_rate`` / ``crossfade_different_sample_rates``) to a non-default
value saved fine, but setting it back to the default did not stick.

The config of the protocol is the canonical store for these values. A redundant
copy persisted on the player shadowed the protocol's value once it was reset to
its default (the copy on the player lingered while the protocol dropped the
now-default value), so the old value kept reappearing.
"""

from copy import deepcopy
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from music_assistant_models.enums import PlayerFeature, PlayerType, ProtocolRole

from music_assistant.constants import (
    CONF_DEVICE_INFO,
    CONF_ENABLED,
    CONF_FLOW_MODE_SAMPLE_RATE,
    CONF_LAST_SEEN,
    CONF_PLAYER_PROTOCOLS,
    CONF_PLAYERS,
    CONF_PREFER_WAV_FOR_LIVE_SOURCES,
    CONF_PROTOCOL_KEY_SPLITTER,
    CONF_REQUIRES_FIXED_CONTENT_LENGTH,
    CONF_UNDERLYING_PLAYER_ID,
    FLOW_MODE_SAMPLE_RATE_BIT_PERFECT,
    FLOW_MODE_SAMPLE_RATE_SMART,
)
from music_assistant.mass import MusicAssistant
from music_assistant.models.player import Player

from .helpers import attach_on_config

# a player that was first seen after the split: none of its protocols has its id
PARENT_ID = "pl0123456789ab"
NATIVE_ID = "sonos_123"
CHILD_ID = "dlna_AABBCCDDEEFF"
AIRPLAY_ID = "ap_AABBCCDDEEFF"
SENDSPIN_ID = "spb_AABBCCDDEEFF"
PREFIXED_KEY = f"{CHILD_ID}{CONF_PROTOCOL_KEY_SPLITTER}{CONF_FLOW_MODE_SAMPLE_RATE}"

# PLAY_MEDIA is required for the audio/protocol config entries to be emitted.
FEATURES = {PlayerFeature.VOLUME_SET, PlayerFeature.PLAY_MEDIA}


def _store_protocol(
    mass: MusicAssistant, protocol_id: str, provider: str, role: ProtocolRole, **values: Any
) -> None:
    """Store the config of a protocol of the player."""
    mass.config.create_protocol_record(
        protocol_id, provider=provider, player_id=PARENT_ID, role=role, default_name=None
    )
    for key, value in values.items():
        mass.config.set_protocol_value(protocol_id, key, value)


def _setup_parent_with_protocol_child(mass: MusicAssistant) -> Player:
    """Attach the native protocol and a DLNA protocol of one device, with their configs."""
    player, _ = attach_on_config(
        mass, "sonos", NATIVE_ID, "Living Room", player_id=PARENT_ID, features=set(FEATURES)
    )
    attach_on_config(
        mass,
        "dlna",
        CHILD_ID,
        "Living Room DLNA",
        role=ProtocolRole.AUDIO,
        player=player,
        features=set(FEATURES),
    )
    mass.config.create_player_record(
        PARENT_ID, provider="sonos", player_type=PlayerType.PLAYER, default_name="Living Room"
    )
    _store_protocol(mass, NATIVE_ID, "sonos", ProtocolRole.NATIVE)
    _store_protocol(mass, CHILD_ID, "dlna", ProtocolRole.AUDIO)
    return player


def _parent_stored_values(mass: MusicAssistant) -> dict[str, Any]:
    """Return the raw persisted config values of the player, without its device metadata."""
    raw = mass.config.get(f"{CONF_PLAYERS}/{PARENT_ID}") or {}
    return {
        key: value
        for key, value in raw.get("values", {}).items()
        if key not in (CONF_DEVICE_INFO, CONF_LAST_SEEN)
    }


def _stored_configs(mass: MusicAssistant) -> Any:
    """Return a copy of everything stored for players and protocols."""
    return deepcopy((mass.config.get(CONF_PLAYERS), mass.config.get(CONF_PLAYER_PROTOCOLS)))


def _setup_derived_protocol_players(
    mass: MusicAssistant,
    *,
    airplay_enabled: bool = True,
    airplay_provider_loaded: bool = True,
) -> Player:
    """Add an AirPlay protocol and a Sendspin protocol that rides on it, switched off."""
    player = _setup_parent_with_protocol_child(mass)
    attach_on_config(
        mass,
        "airplay",
        AIRPLAY_ID,
        "Living Room AirPlay",
        role=ProtocolRole.AUDIO,
        player=player,
        features=set(FEATURES),
    )
    if not airplay_provider_loaded:
        del mass._providers["airplay"]
    _store_protocol(mass, AIRPLAY_ID, "airplay", ProtocolRole.AUDIO, enabled=airplay_enabled)
    # the user switched the derived output off, so its protocol is not attached
    _store_protocol(
        mass,
        SENDSPIN_ID,
        "sendspin",
        ProtocolRole.AUDIO,
        enabled=False,
        **{CONF_UNDERLYING_PLAYER_ID: AIRPLAY_ID},
    )
    return player


def _assert_nothing_was_passed_on(mass: MusicAssistant) -> None:
    """Assert that the players controller was not told about a config change."""
    cast("AsyncMock", mass.players.on_player_config_change).assert_not_awaited()
    cast("AsyncMock", mass.players.on_protocol_config_change).assert_not_awaited()


async def test_enabling_derived_protocol_rejects_disabled_underlying_player(
    mass_minimal: MusicAssistant,
) -> None:
    """Enabling a derived output rejects a disabled underlying player without writing."""
    parent = _setup_derived_protocol_players(mass_minimal, airplay_enabled=False)
    before = _stored_configs(mass_minimal)

    with pytest.raises(RuntimeError, match=r"underlying player .* is disabled"):
        await mass_minimal.config.save_player_config(
            parent.player_id,
            {f"{SENDSPIN_ID}{CONF_PROTOCOL_KEY_SPLITTER}{CONF_ENABLED}": True},
        )

    assert _stored_configs(mass_minimal) == before
    _assert_nothing_was_passed_on(mass_minimal)


async def test_enabling_derived_protocol_rejects_unloaded_underlying_provider(
    mass_minimal: MusicAssistant,
) -> None:
    """Enabling a derived output rejects an unavailable underlying provider without writing."""
    parent = _setup_derived_protocol_players(mass_minimal, airplay_provider_loaded=False)
    before = _stored_configs(mass_minimal)

    with pytest.raises(RuntimeError, match=r"underlying player provider .* is disabled"):
        await mass_minimal.config.save_player_config(
            parent.player_id,
            {f"{SENDSPIN_ID}{CONF_PROTOCOL_KEY_SPLITTER}{CONF_ENABLED}": True},
        )

    assert _stored_configs(mass_minimal) == before
    _assert_nothing_was_passed_on(mass_minimal)


async def test_enabling_derived_protocol_rejects_conflicting_same_request_values(
    mass_minimal: MusicAssistant,
) -> None:
    """A derived enable and underlying disable in one request are rejected atomically."""
    parent = _setup_derived_protocol_players(mass_minimal)
    before = _stored_configs(mass_minimal)

    with pytest.raises(RuntimeError, match=r"underlying player .* is disabled"):
        await mass_minimal.config.save_player_config(
            parent.player_id,
            {
                f"{SENDSPIN_ID}{CONF_PROTOCOL_KEY_SPLITTER}{CONF_ENABLED}": True,
                f"{AIRPLAY_ID}{CONF_PROTOCOL_KEY_SPLITTER}{CONF_ENABLED}": False,
            },
        )

    assert _stored_configs(mass_minimal) == before
    _assert_nothing_was_passed_on(mass_minimal)


async def test_protocol_prefixed_reset_to_default_sticks(mass_minimal: MusicAssistant) -> None:
    """Resetting a protocol-prefixed value to its default must stick on read-back."""
    _setup_parent_with_protocol_child(mass_minimal)

    # sanity: the virtual prefixed entry is exposed and defaults to "smart"
    config = await mass_minimal.config.get_player_config(PARENT_ID)
    assert config.values[PREFIXED_KEY].value == FLOW_MODE_SAMPLE_RATE_SMART

    # set the prefixed value to a non-default, alongside an unrelated parent change
    await mass_minimal.config.save_player_config(
        PARENT_ID,
        {PREFIXED_KEY: FLOW_MODE_SAMPLE_RATE_BIT_PERFECT, "tts_pre_announce": False},
    )
    config = await mass_minimal.config.get_player_config(PARENT_ID)
    assert config.values[PREFIXED_KEY].value == FLOW_MODE_SAMPLE_RATE_BIT_PERFECT
    # canonical store is the child protocol player; the parent must not carry a copy
    assert (
        mass_minimal.config.get_protocol_value(CHILD_ID, CONF_FLOW_MODE_SAMPLE_RATE)
        == FLOW_MODE_SAMPLE_RATE_BIT_PERFECT
    )
    assert PREFIXED_KEY not in _parent_stored_values(mass_minimal)

    # reset the prefixed value back to its default
    await mass_minimal.config.save_player_config(
        PARENT_ID, {PREFIXED_KEY: FLOW_MODE_SAMPLE_RATE_SMART}
    )

    # the child drops the now-default value, and the parent retains no shadowing copy
    assert mass_minimal.config.get_protocol_value(CHILD_ID, CONF_FLOW_MODE_SAMPLE_RATE) is None
    assert PREFIXED_KEY not in _parent_stored_values(mass_minimal)
    config = await mass_minimal.config.get_player_config(PARENT_ID)
    assert config.values[PREFIXED_KEY].value == FLOW_MODE_SAMPLE_RATE_SMART


async def test_stale_prefixed_copy_does_not_shadow_default(mass_minimal: MusicAssistant) -> None:
    """
    A pre-existing stale prefixed copy on the parent must not shadow the default.

    Covers installs that already accumulated a redundant prefixed value before the
    fix: the protocol player holds the default (no stored value), so the config must
    read back as the default regardless of the lingering parent copy.
    """
    _setup_parent_with_protocol_child(mass_minimal)

    # simulate a stale copy as written by older versions
    mass_minimal.config.set(
        f"{CONF_PLAYERS}/{PARENT_ID}/values/{PREFIXED_KEY}", FLOW_MODE_SAMPLE_RATE_BIT_PERFECT
    )
    assert PREFIXED_KEY in _parent_stored_values(mass_minimal)
    # child protocol player is at its default (nothing stored)
    assert mass_minimal.config.get_protocol_value(CHILD_ID, CONF_FLOW_MODE_SAMPLE_RATE) is None

    # read-back must reflect the protocol player's default, not the stale parent copy
    config = await mass_minimal.config.get_player_config(PARENT_ID)
    assert config.values[PREFIXED_KEY].value == FLOW_MODE_SAMPLE_RATE_SMART


async def test_full_form_save_does_not_persist_prefixed_copy_on_parent(
    mass_minimal: MusicAssistant,
) -> None:
    """A full-form save must keep protocol-prefixed entries off the parent config."""
    _setup_parent_with_protocol_child(mass_minimal)

    await mass_minimal.config.save_player_config(
        PARENT_ID,
        {PREFIXED_KEY: FLOW_MODE_SAMPLE_RATE_BIT_PERFECT, "tts_pre_announce": False},
    )

    stored = _parent_stored_values(mass_minimal)
    # parent-level (non-protocol) change is fine to persist
    assert stored.get("tts_pre_announce") is False
    # protocol-prefixed entries are virtual mirrors of the child and must never live on the
    # config of the player: its own record, nor that of its own protocol
    own_values = mass_minimal.config.get_protocol_record(NATIVE_ID) or {}
    for values in (stored, own_values.get("values") or {}):
        assert not any(CONF_PROTOCOL_KEY_SPLITTER in key for key in values)
    assert (
        mass_minimal.config.get_protocol_value(CHILD_ID, CONF_FLOW_MODE_SAMPLE_RATE)
        == FLOW_MODE_SAMPLE_RATE_BIT_PERFECT
    )


async def test_injected_protocol_entry_resolves_under_origin_provider(
    mass_minimal: MusicAssistant,
) -> None:
    """Injected protocol entries carry their origin provider's owner + bare key, not the host's."""
    _setup_parent_with_protocol_child(mass_minimal)

    # config/players/get (parsed PlayerConfig) path
    config = await mass_minimal.config.get_player_config(PARENT_ID)
    entry = config.values[PREFIXED_KEY]
    assert entry.translation_owner == "provider.dlna"
    assert entry.translation_key == CONF_FLOW_MODE_SAMPLE_RATE

    # config/players/get_entries (raw entries) path
    entries = await mass_minimal.config.get_player_config_entries(PARENT_ID)
    proto_entry = next(entry for entry in entries if entry.key == PREFIXED_KEY)
    assert proto_entry.translation_owner == "provider.dlna"
    assert proto_entry.translation_key == CONF_FLOW_MODE_SAMPLE_RATE


async def test_player_entries_resolve_under_their_own_provider(
    mass_minimal: MusicAssistant,
) -> None:
    """A player's own entries carry its provider's namespace, so its strings.json is consulted."""
    _setup_parent_with_protocol_child(mass_minimal)

    entries = await mass_minimal.config.get_player_config_entries(PARENT_ID)
    own_entries = [entry for entry in entries if CONF_PROTOCOL_KEY_SPLITTER not in entry.key]

    assert own_entries
    assert {entry.translation_owner for entry in own_entries} == {"provider.sonos"}


async def test_live_source_wav_preference_is_only_exposed_for_http_players(
    mass_minimal: MusicAssistant,
) -> None:
    """Only HTTP player protocols expose the live source WAV preference."""
    _setup_parent_with_protocol_child(mass_minimal)
    parent_entries = await mass_minimal.config.get_player_config_entries(PARENT_ID)
    dlna_key = f"{CHILD_ID}{CONF_PROTOCOL_KEY_SPLITTER}{CONF_PREFER_WAV_FOR_LIVE_SOURCES}"
    assert next(entry for entry in parent_entries if entry.key == dlna_key).default_value is False

    _, sendspin = attach_on_config(
        mass_minimal,
        "sendspin",
        "sendspin_1",
        "Sendspin Player",
        role=ProtocolRole.AUDIO,
        features=set(FEATURES),
    )

    entries = await mass_minimal.config._get_protocol_config_entries(sendspin)

    assert CONF_PREFER_WAV_FOR_LIVE_SOURCES not in {entry.key for entry in entries}


async def test_http_players_get_no_fixed_content_length_entry_by_default(
    mass_minimal: MusicAssistant,
) -> None:
    """The fixed content length setting is only there where a provider offers it itself."""
    _setup_parent_with_protocol_child(mass_minimal)
    child = mass_minimal.players.get_protocol(CHILD_ID)
    assert child is not None

    entries = await mass_minimal.config.get_player_config_entries(PARENT_ID)
    child_entries = await mass_minimal.config._get_protocol_config_entries(child)

    # the form of the player holds the entries of its own protocol and, prefixed, of the child
    keys = {entry.key for entry in entries}
    assert not [key for key in keys if key.endswith(CONF_REQUIRES_FIXED_CONTENT_LENGTH)]
    assert CONF_REQUIRES_FIXED_CONTENT_LENGTH not in {entry.key for entry in child_entries}
