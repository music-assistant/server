"""Tests for the in-place config copies staying in sync with raw config writes."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock

from music_assistant_models.config_entries import (
    ConfigEntry,
    CoreConfig,
    PlayerConfig,
    ProviderConfig,
)
from music_assistant_models.enums import ConfigEntryType, PlayerType, ProviderType

from music_assistant.constants import CONF_PROVIDERS, CONF_VOLUME_NORMALIZATION_TARGET

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

CORE_DOMAIN = "faketestcore"
PLAYER_ID = "test_copy_player"
PROVIDER_INSTANCE = "test_copy_provider"


def _entry(key: str, entry_type: ConfigEntryType, default: object) -> ConfigEntry:
    return ConfigEntry(key=key, type=entry_type, default_value=default, required=False)  # type: ignore[arg-type]


async def test_core_controllers_hold_active_config(mass: MusicAssistant) -> None:
    """At startup every core controller holds its active (parsed) config."""
    assert mass.streams.config.get_value(CONF_VOLUME_NORMALIZATION_TARGET) == -14
    assert mass.webserver.config.domain == "webserver"
    assert mass.player_queues.config.domain == "player_queues"


async def test_set_raw_core_config_value_updates_controller_copy(
    mass_minimal: MusicAssistant,
) -> None:
    """A raw core config write is reflected in the controller's in-place config copy."""
    controller = SimpleNamespace(
        config=cast(
            "CoreConfig",
            CoreConfig.parse(
                [_entry("magic_number", ConfigEntryType.INTEGER, 42)], {"domain": CORE_DOMAIN}
            ),
        )
    )
    setattr(mass_minimal, CORE_DOMAIN, controller)
    mass_minimal.config.set_raw_core_config_value(CORE_DOMAIN, "magic_number", 5)
    assert controller.config.get_value("magic_number") == 5
    assert mass_minimal.config.get_raw_core_config_value(CORE_DOMAIN, "magic_number") == 5


async def test_set_raw_core_config_value_without_controller_copy(
    mass_minimal: MusicAssistant,
) -> None:
    """Raw core config writes for a module without an (initialized) controller still work."""
    mass_minimal.config.set_raw_core_config_value("nonexistent_module", "some_key", 1)
    setattr(mass_minimal, CORE_DOMAIN, SimpleNamespace())  # controller without config (pre-setup)
    mass_minimal.config.set_raw_core_config_value(CORE_DOMAIN, "some_key", 2)
    assert mass_minimal.config.get_raw_core_config_value(CORE_DOMAIN, "some_key") == 2


async def test_set_raw_player_config_value_updates_player_copy(
    mass_minimal: MusicAssistant,
) -> None:
    """A raw player config write is reflected in the player's in-place config copy."""
    mass_minimal.config.create_default_player_config(PLAYER_ID, "test_instance", PlayerType.PLAYER)
    player = MagicMock()
    player.config = cast(
        "PlayerConfig",
        PlayerConfig.parse(
            [_entry("fancy_option", ConfigEntryType.STRING, "foo")],
            {"player_id": PLAYER_ID, "provider": "test_instance"},
        ),
    )
    mass_minimal.players = MagicMock()
    mass_minimal.players.get_player.return_value = player
    mass_minimal.config.set_raw_player_config_value(PLAYER_ID, "fancy_option", "bar")
    assert player.config.get_value("fancy_option") == "bar"
    # a write for a key without config entry only lands in storage
    mass_minimal.config.set_raw_player_config_value(PLAYER_ID, "unknown_key", "baz")
    assert mass_minimal.config.get_raw_player_config_value(PLAYER_ID, "unknown_key") == "baz"


async def test_set_raw_provider_config_value_updates_provider_copy(
    mass_minimal: MusicAssistant,
) -> None:
    """A raw provider config write is reflected in the provider's in-place config copy."""
    provider = MagicMock()
    provider.config = cast(
        "ProviderConfig",
        ProviderConfig.parse(
            [
                _entry("api_token", ConfigEntryType.STRING, None),
                _entry("api_secret", ConfigEntryType.SECURE_STRING, None),
            ],
            {
                "type": ProviderType.MUSIC,
                "domain": "test",
                "instance_id": PROVIDER_INSTANCE,
            },
        ),
    )
    mass_minimal.get_provider = MagicMock(return_value=provider)  # type: ignore[method-assign]
    mass_minimal.config.set(
        f"{CONF_PROVIDERS}/{PROVIDER_INSTANCE}", {"instance_id": PROVIDER_INSTANCE}
    )
    mass_minimal.config.set_raw_provider_config_value(PROVIDER_INSTANCE, "api_token", "abc")
    assert provider.config.get_value("api_token") == "abc"
    # encrypted writes store the encrypted value; get_value transparently decrypts
    mass_minimal.config.set_raw_provider_config_value(
        PROVIDER_INSTANCE, "api_secret", "s3cret", encrypted=True
    )
    assert provider.config.get_value("api_secret") == "s3cret"


async def test_set_raw_provider_config_value_syncs_unavailable_provider(
    mass_minimal: MusicAssistant,
) -> None:
    """A raw write syncs the in-place copy even when the provider is currently unavailable."""
    provider = MagicMock()
    provider.config = cast(
        "ProviderConfig",
        ProviderConfig.parse(
            [_entry("api_token", ConfigEntryType.STRING, None)],
            {
                "type": ProviderType.MUSIC,
                "domain": "test",
                "instance_id": PROVIDER_INSTANCE,
            },
        ),
    )

    # mimic get_provider: an unavailable instance is only returned with return_unavailable=True
    def _get_provider(
        _instance: str, return_unavailable: bool = False, **_kwargs: object
    ) -> object | None:
        return provider if return_unavailable else None

    mass_minimal.get_provider = MagicMock(side_effect=_get_provider)  # type: ignore[method-assign]
    mass_minimal.config.set(
        f"{CONF_PROVIDERS}/{PROVIDER_INSTANCE}", {"instance_id": PROVIDER_INSTANCE}
    )
    mass_minimal.config.set_raw_provider_config_value(PROVIDER_INSTANCE, "api_token", "abc")
    assert provider.config.get_value("api_token") == "abc"
