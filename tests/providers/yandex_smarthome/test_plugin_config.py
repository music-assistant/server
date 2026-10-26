"""Tests for setup-data and runtime-option separation."""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from music_assistant.providers.yandex_smarthome.constants import (
    CONF_CLOUD_CONNECTION_TOKEN,
    CONF_CLOUD_INSTANCE_ID,
    CONF_CLOUD_INSTANCE_PASSWORD,
    CONF_CONNECTION_TYPE,
    CONF_DIRECT_ACCESS_TOKEN,
    CONF_DIRECT_CLIENT_SECRET,
    CONF_EXPOSED_PLAYERS,
    CONF_EXPOSED_PLAYLISTS,
    CONF_INSTANCE_NAME,
    CONF_SKILL_ID,
    CONF_SKILL_TOKEN,
    CONNECTION_TYPE_DIRECT,
)
from music_assistant.providers.yandex_smarthome.plugin import YandexSmartHomePlugin


def _plugin() -> YandexSmartHomePlugin:
    """Build a plugin with lightweight runtime collaborators."""
    mass = mock.MagicMock()
    mass.players.all_players.return_value = [
        SimpleNamespace(state=SimpleNamespace(player_id="player-1", name="Kitchen"))
    ]
    config_values = {
        CONF_INSTANCE_NAME: "Living room",
        CONF_EXPOSED_PLAYERS: ["player-1"],
        CONF_EXPOSED_PLAYLISTS: ["library://playlist/1"],
    }
    config = mock.MagicMock()
    config.get_value.side_effect = lambda key, *_: config_values.get(key)
    return YandexSmartHomePlugin(mass, mock.MagicMock(), config, set())


async def test_config_entries_only_contain_runtime_options() -> None:
    """Credentials stay in setup data and do not reappear in options."""
    plugin = _plugin()
    with mock.patch(
        "music_assistant.providers.yandex_smarthome.plugin.fetch_playlist_options",
        new_callable=mock.AsyncMock,
        return_value=[],
    ):
        entries = await plugin.get_config_entries()

    assert [entry.key for entry in entries] == [
        CONF_INSTANCE_NAME,
        CONF_EXPOSED_PLAYERS,
        CONF_EXPOSED_PLAYLISTS,
    ]
    assert entries[1].options[0].value == "player-1"


async def test_init_reads_credentials_from_setup_and_options_from_config() -> None:
    """Runtime initialization uses each storage surface for its intended values."""
    plugin = _plugin()
    setup_values = {
        CONF_CONNECTION_TYPE: CONNECTION_TYPE_DIRECT,
        CONF_CLOUD_INSTANCE_PASSWORD: "cloud-password",
        CONF_CLOUD_CONNECTION_TOKEN: "connection-token",
        CONF_CLOUD_INSTANCE_ID: "cloud-id",
        CONF_SKILL_ID: "skill-id",
        CONF_SKILL_TOKEN: "skill-token",
        CONF_DIRECT_ACCESS_TOKEN: "access-token",
        CONF_DIRECT_CLIENT_SECRET: "client-secret",
    }
    with mock.patch.object(
        plugin, "get_setup_value", side_effect=lambda key, *_: setup_values.get(key)
    ) as get_setup_value:
        await plugin.handle_async_init()

    assert plugin._connection_type == CONNECTION_TYPE_DIRECT
    assert plugin._instance_name == "Living room"
    assert plugin._direct_client_secret == "client-secret"
    assert plugin._exposed_ids == {"player-1"}
    setup_keys = {call.args[0] for call in get_setup_value.call_args_list}
    assert CONF_INSTANCE_NAME not in setup_keys
    assert CONF_EXPOSED_PLAYERS not in setup_keys


def test_setup_value_keeps_legacy_config_fallback() -> None:
    """Instances created before setup flows still read their saved credentials."""
    plugin = _plugin()
    with mock.patch.object(
        plugin.config,
        "get_value",
        side_effect=lambda key, *_: {CONF_CONNECTION_TYPE: CONNECTION_TYPE_DIRECT}.get(key),
    ):
        assert plugin.get_setup_value(CONF_CONNECTION_TYPE) == CONNECTION_TYPE_DIRECT


async def test_direct_token_rotation_updates_setup_data_immediately() -> None:
    """A token minted by the Direct OAuth endpoint is persisted as setup data."""
    plugin = _plugin()
    plugin._direct_client_secret = "client-secret"
    plugin._direct_access_token = ""
    plugin._instance_name = "Living room"
    plugin._exposed_ids = None
    plugin._exposed_playlists = ()
    plugin._skill_id = ""
    plugin._skill_token = None
    with (
        mock.patch.object(plugin, "_update_setup_data") as update_setup_data,
        mock.patch(
            "music_assistant.providers.yandex_smarthome.plugin.DirectConnectionHandler"
        ) as handler_cls,
    ):
        await plugin._start_direct_mode()
        handler_cls.call_args.kwargs["on_token_created"]("new-token")

    update_setup_data.assert_called_once_with(CONF_DIRECT_ACCESS_TOKEN, "new-token", immediate=True)
