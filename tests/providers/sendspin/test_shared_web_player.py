"""Tests for the shared-player setting of Sendspin web/app players."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

import pytest
from music_assistant_models.config_entries import ConfigEntry, PlayerConfig
from music_assistant_models.enums import ConfigEntryType, PlayerType
from music_assistant_models.player import DeviceInfo

from music_assistant.constants import CONF_EXPOSE_PLAYER_TO_HA, CONF_HIDE_IN_UI
from music_assistant.providers.sendspin.constants import CONF_SHARED_PLAYER
from music_assistant.providers.sendspin.player import SendspinPlayer

if TYPE_CHECKING:
    from typing import Any

    from aiosendspin.models.core import ClientHelloPayload
    from aiosendspin.server.client import SendspinClient

    from music_assistant.mass import MusicAssistant
    from music_assistant.providers.sendspin.provider import SendspinProvider


def _player(
    *, web_player: bool, virtual: bool = False, shared: bool | None = None
) -> SendspinPlayer:
    player = SendspinPlayer.__new__(SendspinPlayer)
    player._player_id = "web-client"
    player._cache = {}
    player.is_web_player = web_player
    player._provider = cast(
        "SendspinProvider", SimpleNamespace(is_virtual_player=lambda _player_id: virtual)
    )
    _set_shared(player, shared)
    return player


def _set_shared(player: SendspinPlayer, shared: bool | None) -> None:
    """Put the setting in the config store the way a save does, or leave it absent for None."""
    values = {} if shared is None else {CONF_SHARED_PLAYER: shared}
    player.mass = cast(
        "MusicAssistant",
        SimpleNamespace(
            config=SimpleNamespace(
                get_raw_player_config_value=lambda _player_id, key, default=None: values.get(
                    key, default
                )
            )
        ),
    )


def _visibility(player: SendspinPlayer) -> tuple[bool, bool, bool]:
    """Return the (private, hidden by default, exposed to HA by default) flags."""
    return (
        player._attr_private,
        player._attr_hidden_by_default,
        player._attr_expose_to_ha_by_default,
    )


def test_a_web_player_is_private_by_default() -> None:
    """A web/app player belongs to its device: private, hidden and not in HA by default."""
    player = _player(web_player=True)
    player._apply_standalone_visibility()
    assert _visibility(player) == (True, True, False)
    assert player._attr_type is PlayerType.PLAYER


def test_sharing_changes_only_privacy() -> None:
    """Sharing preserves visibility defaults and the standalone player type."""
    player = _player(web_player=True, shared=True)
    player._apply_standalone_visibility()
    assert _visibility(player) == (False, True, False)
    # still a web player: it must not become a protocol player waiting to be linked
    assert player.is_web_player is True
    assert player._attr_type is PlayerType.PLAYER


def test_a_virtual_player_ignores_the_shared_setting() -> None:
    """A virtual player announces itself as a web player, but has no device to share."""
    player = _player(web_player=True, virtual=True, shared=True)
    player._apply_standalone_visibility()
    assert _visibility(player) == (True, True, False)
    assert player._attr_type is PlayerType.PLAYER


@pytest.mark.parametrize("shared", [None, False, True])
def test_a_device_player_is_never_private(shared: bool | None) -> None:
    """A Sendspin device stays a shared protocol player, whatever the setting holds."""
    player = _player(web_player=False, shared=shared)
    player._apply_standalone_visibility()
    assert _visibility(player) == (False, False, True)
    assert player._attr_type is PlayerType.PROTOCOL


def test_the_shared_entry_is_only_offered_to_web_players() -> None:
    """Devices and virtual players have no use for the setting, so they do not get it."""
    entries = _player(web_player=True)._get_shared_player_config_entries()
    assert [(e.key, e.type, e.default_value) for e in entries] == [
        (CONF_SHARED_PLAYER, ConfigEntryType.BOOLEAN, False)
    ]
    assert _player(web_player=False)._get_shared_player_config_entries() == []
    assert _player(web_player=True, virtual=True)._get_shared_player_config_entries() == []


async def test_a_config_change_applies_without_a_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Toggling the setting takes effect on the config update, not on the next hello."""
    player = _player(web_player=True)
    player._apply_standalone_visibility()
    assert _visibility(player) == (True, True, False)
    monkeypatch.setattr(player, "_apply_preferred_format", AsyncMock())
    monkeypatch.setattr(player, "_apply_static_delay", AsyncMock())
    _set_shared(player, True)
    await player.on_config_updated()
    assert _visibility(player) == (False, True, False)
    _set_shared(player, False)
    await player.on_config_updated()
    assert _visibility(player) == (True, True, False)


def test_sharing_round_trip_preserves_visibility_overrides() -> None:
    """Saving and reloading either sharing mode preserves stored visibility overrides."""
    player = _player(web_player=True)
    entries = [
        ConfigEntry(key=key, type=ConfigEntryType.BOOLEAN, default_value=default)
        for key, default in (
            (CONF_SHARED_PLAYER, False),
            (CONF_HIDE_IN_UI, True),
            (CONF_EXPOSE_PLAYER_TO_HA, False),
        )
    ]
    raw: dict[str, Any] = {
        "player_id": player.player_id,
        "provider": "sendspin",
        "values": {CONF_HIDE_IN_UI: False, CONF_EXPOSE_PLAYER_TO_HA: True},
    }
    for shared in (True, False, True):
        config = cast("PlayerConfig", PlayerConfig.parse(entries, raw))
        config.update({CONF_SHARED_PLAYER: shared})
        player.set_config(config)
        _set_shared(player, shared)
        player._apply_standalone_visibility()
        assert _visibility(player) == (not shared, True, False)
        assert player.hide_in_ui is False
        assert player.expose_to_ha is True
        raw = config.to_raw()
        assert raw["values"][CONF_HIDE_IN_UI] is False
        assert raw["values"][CONF_EXPOSE_PLAYER_TO_HA] is True


def _hello(*, product_name: str, manufacturer: str) -> ClientHelloPayload:
    """Build a client hello the way a device or a Music Assistant app announces itself."""
    return cast(
        "ClientHelloPayload",
        SimpleNamespace(
            name="hello name",
            device_info=SimpleNamespace(
                product_name=product_name,
                manufacturer=manufacturer,
                software_version="1.0.0",
                mac_address=None,
            ),
            player_support=None,
        ),
    )


def _refresh(player: SendspinPlayer, hello: ClientHelloPayload) -> None:
    """Run the hello through the real refresh, as every (re)connect does."""
    player._attr_device_info = DeviceInfo()
    player._refresh_client_info(cast("SendspinClient", SimpleNamespace(info=hello)), hello)


@pytest.mark.parametrize(
    ("product_name", "manufacturer", "web_player"),
    [
        # the desktop app reports the user's own player name as its product name
        ("Living room PC", "Music Assistant", True),
        ("Web Browser", "Mozilla", True),
        ("PWA", "Google", True),
        ("Smart Speaker", "ESPHome", False),
    ],
)
def test_a_hello_decides_whether_the_client_is_a_web_player(
    product_name: str, manufacturer: str, web_player: bool
) -> None:
    """The hello classifies the client, and a web/app player is private by default."""
    player = _player(web_player=False)
    _refresh(player, _hello(product_name=product_name, manufacturer=manufacturer))
    assert player.is_web_player is web_player
    assert _visibility(player) == ((True, True, False) if web_player else (False, False, True))
    assert player._attr_type is (PlayerType.PLAYER if web_player else PlayerType.PROTOCOL)


def test_a_reconnecting_shared_web_player_stays_shared() -> None:
    """The setting is read on every hello, so a reconnect does not make the player private."""
    player = _player(web_player=False, shared=True)
    _refresh(player, _hello(product_name="Living room PC", manufacturer="Music Assistant"))
    assert player.is_web_player is True
    assert _visibility(player) == (False, True, False)
    assert player._attr_type is PlayerType.PLAYER


def test_the_stored_setting_is_read_before_the_config_is_loaded() -> None:
    """The first hello runs before the config entries exist and must still see the setting."""
    player = _player(web_player=False, shared=True)
    # the base config a player is constructed with: stored values are not parsed into it
    player._config = cast(
        "PlayerConfig", PlayerConfig.parse([], {"player_id": "web-client", "provider": "sendspin"})
    )
    _refresh(player, _hello(product_name="Living room PC", manufacturer="Music Assistant"))
    assert _visibility(player) == (False, True, False)
