"""Tests for the in-place config copies staying in sync with raw config writes."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

from music_assistant_models.config_entries import (
    ConfigEntry,
    ConfigValueType,
    CoreConfig,
    PlayerConfig,
    ProviderConfig,
)
from music_assistant_models.enums import ConfigEntryType, PlayerType, ProviderType

from music_assistant.constants import (
    CONF_ENTRY_LOG_LEVEL,
    CONF_PROVIDERS,
    CONF_VOLUME_NORMALIZATION_TARGET,
)
from music_assistant.models.provider import Provider

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

CORE_DOMAIN = "faketestcore"
PLAYER_ID = "test_copy_player"
PROVIDER_INSTANCE = "test_copy_provider"


def _entry(key: str, entry_type: ConfigEntryType, default: ConfigValueType) -> ConfigEntry:
    return ConfigEntry(key=key, type=entry_type, default_value=default, required=False)


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


async def test_provider_get_config_value_prefers_persisted_value(
    mass_minimal: MusicAssistant,
) -> None:
    """A provider reads a persisted value instead of its stale config snapshot."""
    entries = _provider_config_entries()
    mass_minimal.config.set(
        f"{CONF_PROVIDERS}/{PROVIDER_INSTANCE}",
        _raw_provider_conf(PROVIDER_INSTANCE, "persisted"),
    )
    provider = _create_provider(mass_minimal, entries, api_token="stale")

    assert provider.get_config_value("api_token") == "persisted"


async def test_provider_get_config_value_decrypts_persisted_secure_value(
    mass_minimal: MusicAssistant,
) -> None:
    """A provider transparently decrypts a persisted secure config value."""
    entries = [
        CONF_ENTRY_LOG_LEVEL,
        _entry("api_secret", ConfigEntryType.SECURE_STRING, None),
    ]
    mass_minimal.config.set(
        f"{CONF_PROVIDERS}/{PROVIDER_INSTANCE}",
        _raw_provider_conf(PROVIDER_INSTANCE),
    )
    mass_minimal.config.set_raw_provider_config_value(
        PROVIDER_INSTANCE, "api_secret", "persisted-secret", encrypted=True
    )
    provider = _create_provider(mass_minimal, entries, api_secret="stale-secret")

    assert provider.get_config_value("api_secret") == "persisted-secret"


async def test_provider_get_config_value_falls_back_to_active_config(
    mass_minimal: MusicAssistant,
) -> None:
    """A provider uses active values and defaults when no persisted value exists."""
    entries = [
        CONF_ENTRY_LOG_LEVEL,
        _entry("active_value", ConfigEntryType.STRING, None),
        _entry("active_default", ConfigEntryType.STRING, "entry-default"),
    ]
    mass_minimal.config.set(
        f"{CONF_PROVIDERS}/{PROVIDER_INSTANCE}",
        _raw_provider_conf(PROVIDER_INSTANCE, unknown="store-only"),
    )
    provider = _create_provider(mass_minimal, entries, active_value="snapshot-value")

    assert provider.get_config_value("active_value") == "snapshot-value"
    assert provider.get_config_value("active_default") == "entry-default"
    assert provider.get_config_value("unknown", "explicit-default") == "explicit-default"


async def test_provider_update_config_value_syncs_initializing_snapshot(
    mass_minimal: MusicAssistant,
) -> None:
    """A provider-originated write updates its snapshot before registration completes."""
    entries = _provider_config_entries()
    mass_minimal.config.set(
        f"{CONF_PROVIDERS}/{PROVIDER_INSTANCE}",
        _raw_provider_conf(PROVIDER_INSTANCE, "old"),
    )
    provider = _create_provider(mass_minimal, entries, api_token="old")
    get_provider = MagicMock(return_value=None)
    mass_minimal.get_provider = get_provider  # type: ignore[method-assign]

    provider._update_config_value("api_token", "new")

    assert (
        mass_minimal.config.get_raw_provider_config_value(PROVIDER_INSTANCE, "api_token") == "new"
    )
    assert provider.config.values["api_token"].value == "new"
    get_provider.assert_called_once_with(PROVIDER_INSTANCE, return_unavailable=True)


async def test_provider_encrypted_update_keeps_raw_snapshot_value(
    mass_minimal: MusicAssistant,
) -> None:
    """An encrypted provider write stores raw ciphertext while reads return plaintext."""
    entries = [
        CONF_ENTRY_LOG_LEVEL,
        _entry("api_secret", ConfigEntryType.SECURE_STRING, None),
    ]
    mass_minimal.config.set(
        f"{CONF_PROVIDERS}/{PROVIDER_INSTANCE}",
        _raw_provider_conf(PROVIDER_INSTANCE),
    )
    provider = _create_provider(mass_minimal, entries)
    get_provider = MagicMock(return_value=None)
    mass_minimal.get_provider = get_provider  # type: ignore[method-assign]

    provider._update_config_value("api_secret", "new-secret", encrypted=True)

    raw_value = mass_minimal.config.get_raw_provider_config_value(PROVIDER_INSTANCE, "api_secret")
    assert isinstance(raw_value, str)
    assert raw_value != "new-secret"
    assert provider.config.values["api_secret"].value == raw_value
    assert provider.get_config_value("api_secret") == "new-secret"


async def test_save_provider_config_syncs_loaded_available_provider(
    mass_minimal: MusicAssistant,
) -> None:
    """
    The bulk config-save path keeps a loaded, available provider's copy in sync too.

    Regression test for the store<->snapshot invariant: whichever write path persists a
    provider config change, a loaded provider's in-place config copy must reflect it.
    """
    instance = PROVIDER_INSTANCE
    entries = _provider_config_entries()
    mass_minimal.config.set(f"{CONF_PROVIDERS}/{instance}", _raw_provider_conf(instance, "old"))
    manifest = MagicMock(domain="test", type=ProviderType.MUSIC)
    provider = Provider(
        mass_minimal,
        manifest,
        cast("ProviderConfig", ProviderConfig.parse(entries, _raw_provider_conf(instance, "old"))),
    )
    provider.available = True
    mass_minimal.get_provider = MagicMock(return_value=provider)  # type: ignore[method-assign]
    mass_minimal.call_later = MagicMock()  # type: ignore[method-assign] # don't schedule a real reload

    async def _get_provider_config(_instance_id: str) -> ProviderConfig:
        raw_conf = mass_minimal.config.get(f"{CONF_PROVIDERS}/{instance}")
        return cast("ProviderConfig", ProviderConfig.parse(entries, raw_conf))

    with patch.object(mass_minimal.config, "get_provider_config", side_effect=_get_provider_config):
        await mass_minimal.config.save_provider_config(
            "test", {"api_token": "new"}, instance_id=instance
        )

    assert provider.config.get_value("api_token") == "new"
    assert mass_minimal.config.get_raw_provider_config_value(instance, "api_token") == "new"


async def test_save_provider_config_reloads_loaded_unavailable_provider(
    mass_minimal: MusicAssistant,
) -> None:
    """
    The bulk config-save path reloads (rather than patches) an unavailable loaded provider.

    An unavailable provider instance never receives a direct config-copy sync; instead the
    save must trigger a full reload with the freshly persisted values, so the replacement
    provider instance can never end up with a stale copy.
    """
    instance = PROVIDER_INSTANCE
    entries = _provider_config_entries()
    mass_minimal.config.set(f"{CONF_PROVIDERS}/{instance}", _raw_provider_conf(instance, "old"))
    manifest = MagicMock(domain="test", type=ProviderType.MUSIC)
    provider = Provider(
        mass_minimal,
        manifest,
        cast("ProviderConfig", ProviderConfig.parse(entries, _raw_provider_conf(instance, "old"))),
    )
    provider.available = False  # loaded, but currently unavailable (e.g. failed token refresh)

    def _get_provider(
        _instance: str, return_unavailable: bool = False, **_kwargs: object
    ) -> object | None:
        return provider if return_unavailable else None

    mass_minimal.get_provider = MagicMock(side_effect=_get_provider)  # type: ignore[method-assign]

    async def _get_provider_config(_instance_id: str) -> ProviderConfig:
        raw_conf = mass_minimal.config.get(f"{CONF_PROVIDERS}/{instance}")
        return cast("ProviderConfig", ProviderConfig.parse(entries, raw_conf))

    with (
        patch.object(mass_minimal.config, "get_provider_config", side_effect=_get_provider_config),
        patch.object(mass_minimal, "load_provider_config", AsyncMock()) as mock_load,
    ):
        await mass_minimal.config.save_provider_config(
            "test", {"api_token": "new"}, instance_id=instance
        )

    # the value is persisted before the reload is triggered...
    assert mass_minimal.config.get_raw_provider_config_value(instance, "api_token") == "new"
    # ...and the reload receives that same, up-to-date value, so the replacement
    # provider instance created by the reload can never end up with a stale copy
    mock_load.assert_awaited_once()
    assert mock_load.await_args is not None
    reloaded_config = mock_load.await_args.args[0]
    assert reloaded_config.get_value("api_token") == "new"
    # the stale (unavailable) instance itself is never patched in place
    assert provider.config.get_value("api_token") == "old"


def _provider_config_entries() -> list[ConfigEntry]:
    return [CONF_ENTRY_LOG_LEVEL, _entry("api_token", ConfigEntryType.STRING, "old")]


def _create_provider(
    mass: MusicAssistant,
    entries: list[ConfigEntry],
    **values: ConfigValueType,
) -> Provider:
    """Create a provider with an active config snapshot."""
    manifest = MagicMock(domain="test", type=ProviderType.MUSIC)
    config = cast(
        "ProviderConfig",
        ProviderConfig.parse(entries, _raw_provider_conf(PROVIDER_INSTANCE, **values)),
    )
    return Provider(mass, manifest, config)


def _raw_provider_conf(
    instance_id: str, api_token: ConfigValueType = None, **values: ConfigValueType
) -> dict[str, object]:
    if api_token is not None:
        values["api_token"] = api_token
    return {
        "type": ProviderType.MUSIC.value,
        "domain": "test",
        "instance_id": instance_id,
        "enabled": True,
        "values": values,
    }
