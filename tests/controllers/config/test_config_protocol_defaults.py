"""
Regression tests for protocol-prefixed config values not reverting to default.

Reproduces music-assistant/support#5685: for a player that streams via a linked
protocol (e.g. a Sonos/Universal parent whose audio output is a separate DLNA
"protocol player"), the config UI exposes protocol-specific entries as virtual
entries keyed ``<protocol_player_id>||protocol||<actual_key>``. Setting such an
entry (e.g. ``flow_mode_sample_rate`` / ``crossfade_different_sample_rates``) to
a non-default value saved fine, but setting it back to the default did not stick.

The linked protocol player is the canonical store for these values. A redundant
copy persisted on the parent shadowed the protocol player's value once it was
reset to its default (the parent copy lingered while the protocol player dropped
the now-default value), so the old value kept reappearing.
"""

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.enums import PlayerFeature, PlayerType, ProviderType

from music_assistant.constants import (
    CONF_FLOW_MODE_SAMPLE_RATE,
    CONF_PREFER_WAV_FOR_LIVE_SOURCES,
    CONF_PROTOCOL_KEY_SPLITTER,
    FLOW_MODE_SAMPLE_RATE_BIT_PERFECT,
    FLOW_MODE_SAMPLE_RATE_SMART,
)
from music_assistant.mass import MusicAssistant
from music_assistant.models.player import DeviceInfo, LinkedOutputProtocol, Player

PARENT_ID = "sonos_123"
CHILD_ID = "dlna_AABBCCDDEEFF"
PREFIXED_KEY = f"{CHILD_ID}{CONF_PROTOCOL_KEY_SPLITTER}{CONF_FLOW_MODE_SAMPLE_RATE}"


class _TestProvider:
    """Minimal PlayerProvider stand-in backed by the real MusicAssistant."""

    def __init__(self, mass: MusicAssistant, domain: str) -> None:
        """Initialize the test provider."""
        self.mass = mass
        self.domain = domain
        self.instance_id = domain
        self.translation_owner = f"provider.{domain}"
        self.name = f"{domain.title()} Provider"
        self.available = True
        self.logger = logging.getLogger(f"test.{domain}")
        self.manifest = MagicMock()
        self.manifest.domain = domain
        self.manifest.name = self.name
        self.manifest.type = ProviderType.PLAYER
        # registered in mass._providers, so the stand-in must survive the provider
        # bookkeeping that mass.stop()/unload_provider applies to every provider
        self.type = ProviderType.PLAYER
        self.players: list[Player] = []

    async def unload(self, is_removed: bool = False) -> None:
        """Unload the provider (nothing to clean up)."""


class _TestPlayer(Player):
    """Minimal concrete Player for driving the real ConfigController."""

    def __init__(
        self,
        provider: _TestProvider,
        player_id: str,
        name: str,
        player_type: PlayerType,
    ) -> None:
        """Initialize the test player."""
        super().__init__(provider, player_id)  # type: ignore[arg-type]
        self._attr_name = name
        self._attr_type = player_type
        self._attr_available = True
        self._attr_powered = True
        # PLAY_MEDIA is required for the audio/protocol config entries to be emitted.
        self._attr_supported_features = {PlayerFeature.VOLUME_SET, PlayerFeature.PLAY_MEDIA}
        self._attr_device_info = DeviceInfo(model="Test Model", manufacturer="Test Manufacturer")
        self._cache.clear()
        self.update_state(signal_event=False)

    async def stop(self) -> None:
        """Stop playback - required abstract method."""


async def _setup_parent_with_protocol_child(mass: MusicAssistant) -> _TestPlayer:
    """Register a native parent player linked to a DLNA protocol child player."""
    sonos_provider = _TestProvider(mass, "sonos")
    dlna_provider = _TestProvider(mass, "dlna")
    mass._providers[sonos_provider.instance_id] = sonos_provider  # type: ignore[assignment]
    mass._providers[dlna_provider.instance_id] = dlna_provider  # type: ignore[assignment]
    mass._provider_manifests[sonos_provider.domain] = sonos_provider.manifest
    mass._provider_manifests[dlna_provider.domain] = dlna_provider.manifest

    # building the players auto-creates their (real) config roots via create_default_player_config
    parent = _TestPlayer(sonos_provider, PARENT_ID, "Living Room", PlayerType.PLAYER)
    child = _TestPlayer(dlna_provider, CHILD_ID, "Living Room DLNA", PlayerType.PROTOCOL)

    mass.players._players[PARENT_ID] = parent
    mass.players._players[CHILD_ID] = child

    # link the DLNA protocol output to the parent so the virtual prefixed entry is emitted
    parent.set_linked_output_protocols(
        [
            LinkedOutputProtocol(
                output_protocol_id=CHILD_ID,
                protocol_domain="dlna",
                priority=50,
            )
        ]
    )
    # output_protocols is a cached property populated during __init__'s update_state
    # (native-only at that point); drop the cache so the freshly linked protocol shows up
    parent._cache.clear()

    # avoid driving the full player-manager reload machinery
    mass.players.on_player_config_change = AsyncMock()  # type: ignore[method-assign]
    return parent


def _parent_stored_values(mass: MusicAssistant) -> dict[str, Any]:
    """Return the parent player's raw persisted config values."""
    raw = mass.config.get(f"players/{PARENT_ID}") or {}
    values: dict[str, Any] = raw.get("values", {})
    return values


async def test_protocol_prefixed_reset_to_default_sticks(mass: MusicAssistant) -> None:
    """Resetting a protocol-prefixed value to its default must stick on read-back."""
    await _setup_parent_with_protocol_child(mass)

    # sanity: the virtual prefixed entry is exposed and defaults to "smart"
    config = await mass.config.get_player_config(PARENT_ID)
    assert config.values[PREFIXED_KEY].value == FLOW_MODE_SAMPLE_RATE_SMART

    # set the prefixed value to a non-default, alongside an unrelated parent change
    await mass.config.save_player_config(
        PARENT_ID,
        {PREFIXED_KEY: FLOW_MODE_SAMPLE_RATE_BIT_PERFECT, "tts_pre_announce": False},
    )
    config = await mass.config.get_player_config(PARENT_ID)
    assert config.values[PREFIXED_KEY].value == FLOW_MODE_SAMPLE_RATE_BIT_PERFECT
    # canonical store is the child protocol player; the parent must not carry a copy
    assert (
        mass.config.get_raw_player_config_value(CHILD_ID, CONF_FLOW_MODE_SAMPLE_RATE)
        == FLOW_MODE_SAMPLE_RATE_BIT_PERFECT
    )
    assert PREFIXED_KEY not in _parent_stored_values(mass)

    # reset the prefixed value back to its default
    await mass.config.save_player_config(PARENT_ID, {PREFIXED_KEY: FLOW_MODE_SAMPLE_RATE_SMART})

    # the child drops the now-default value, and the parent retains no shadowing copy
    assert mass.config.get_raw_player_config_value(CHILD_ID, CONF_FLOW_MODE_SAMPLE_RATE) is None
    assert PREFIXED_KEY not in _parent_stored_values(mass)
    config = await mass.config.get_player_config(PARENT_ID)
    assert config.values[PREFIXED_KEY].value == FLOW_MODE_SAMPLE_RATE_SMART


async def test_stale_prefixed_copy_does_not_shadow_default(mass: MusicAssistant) -> None:
    """
    A pre-existing stale prefixed copy on the parent must not shadow the default.

    Covers installs that already accumulated a redundant prefixed value before the
    fix: the protocol player holds the default (no stored value), so the config must
    read back as the default regardless of the lingering parent copy.
    """
    await _setup_parent_with_protocol_child(mass)

    # simulate a stale copy as written by older versions
    mass.config.set(f"players/{PARENT_ID}/values/{PREFIXED_KEY}", FLOW_MODE_SAMPLE_RATE_BIT_PERFECT)
    assert PREFIXED_KEY in _parent_stored_values(mass)
    # child protocol player is at its default (nothing stored)
    assert mass.config.get_raw_player_config_value(CHILD_ID, CONF_FLOW_MODE_SAMPLE_RATE) is None

    # read-back must reflect the protocol player's default, not the stale parent copy
    config = await mass.config.get_player_config(PARENT_ID)
    assert config.values[PREFIXED_KEY].value == FLOW_MODE_SAMPLE_RATE_SMART


async def test_full_form_save_does_not_persist_prefixed_copy_on_parent(
    mass: MusicAssistant,
) -> None:
    """A full-form save must keep protocol-prefixed entries off the parent config."""
    await _setup_parent_with_protocol_child(mass)

    await mass.config.save_player_config(
        PARENT_ID,
        {PREFIXED_KEY: FLOW_MODE_SAMPLE_RATE_BIT_PERFECT, "tts_pre_announce": False},
    )

    stored = _parent_stored_values(mass)
    # parent-level (non-protocol) change is fine to persist
    assert stored.get("tts_pre_announce") is False
    # protocol-prefixed entries are virtual mirrors of the child and must never live on the parent
    assert not any(CONF_PROTOCOL_KEY_SPLITTER in key for key in stored)


async def test_injected_protocol_entry_resolves_under_origin_provider(
    mass: MusicAssistant,
) -> None:
    """Injected protocol entries carry their origin provider's owner + bare key, not the host's."""
    await _setup_parent_with_protocol_child(mass)

    # config/players/get (parsed PlayerConfig) path
    config = await mass.config.get_player_config(PARENT_ID)
    entry = config.values[PREFIXED_KEY]
    assert entry.translation_owner == "provider.dlna"
    assert entry.translation_key == CONF_FLOW_MODE_SAMPLE_RATE

    # config/players/get_entries (raw entries) path
    entries = await mass.config.get_player_config_entries(PARENT_ID)
    proto_entry = next(entry for entry in entries if entry.key == PREFIXED_KEY)
    assert proto_entry.translation_owner == "provider.dlna"
    assert proto_entry.translation_key == CONF_FLOW_MODE_SAMPLE_RATE


async def test_player_entries_resolve_under_their_own_provider(mass: MusicAssistant) -> None:
    """A player's own entries carry its provider's namespace, so its strings.json is consulted."""
    await _setup_parent_with_protocol_child(mass)

    entries = await mass.config.get_player_config_entries(PARENT_ID)
    own_entries = [entry for entry in entries if CONF_PROTOCOL_KEY_SPLITTER not in entry.key]

    assert own_entries
    assert {entry.translation_owner for entry in own_entries} == {"provider.sonos"}


async def test_live_source_wav_preference_is_only_exposed_for_http_players(
    mass: MusicAssistant,
) -> None:
    """Only HTTP player protocols expose the live source WAV preference."""
    await _setup_parent_with_protocol_child(mass)
    parent_entries = await mass.config.get_player_config_entries(PARENT_ID)
    dlna_key = f"{CHILD_ID}{CONF_PROTOCOL_KEY_SPLITTER}{CONF_PREFER_WAV_FOR_LIVE_SOURCES}"
    assert next(entry for entry in parent_entries if entry.key == dlna_key).default_value is False

    sendspin_provider = _TestProvider(mass, "sendspin")
    mass._providers[sendspin_provider.instance_id] = sendspin_provider  # type: ignore[assignment]
    mass._provider_manifests[sendspin_provider.domain] = sendspin_provider.manifest
    sendspin_player = _TestPlayer(
        sendspin_provider,
        "sendspin_1",
        "Sendspin Player",
        PlayerType.PROTOCOL,
    )

    entries = await mass.config._get_player_config_entries(sendspin_player)

    assert CONF_PREFER_WAV_FOR_LIVE_SOURCES not in {entry.key for entry in entries}
