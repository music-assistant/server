"""Tests for the Snapcast provider."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bidict import bidict
from music_assistant_models.enums import MediaType
from music_assistant_models.player import PlayerMedia

from music_assistant.constants import CONF_LOG_LEVEL, VERBOSE_LOG_LEVEL
from music_assistant.models.player_provider import PlayerProvider
from music_assistant.providers.snapcast.constants import MASS_STREAM_PREFIX
from music_assistant.providers.snapcast.provider import SnapCastProvider


def _provider_with_log_level(level: int) -> SnapCastProvider:
    provider = SnapCastProvider.__new__(SnapCastProvider)
    provider.logger = logging.getLogger("snapcast-provider-test")
    provider.logger.setLevel(level)
    return provider


@pytest.mark.parametrize(
    ("provider_level", "snapcast_level"),
    [
        (logging.INFO, logging.WARNING),
        (logging.DEBUG, logging.INFO),
        (VERBOSE_LOG_LEVEL, logging.DEBUG),
    ],
)
def test_snapcast_library_log_level(provider_level: int, snapcast_level: int) -> None:
    """Snapcast logging stays quieter than the provider unless verbose logging is enabled."""
    provider = _provider_with_log_level(provider_level)

    with patch("music_assistant.providers.snapcast.provider.logging.getLogger") as get_logger:
        provider._set_snapcast_log_level()

    get_logger.assert_called_once_with("snapcast")
    get_logger.return_value.setLevel.assert_called_once_with(snapcast_level)


async def test_log_level_config_update_realigns_snapcast_logger() -> None:
    """A log-level-only config update immediately realigns Snapcast logging."""
    provider = _provider_with_log_level(logging.INFO)
    config = MagicMock()
    changed_keys = {f"values/{CONF_LOG_LEVEL}"}

    with (
        patch.object(PlayerProvider, "update_config", new_callable=AsyncMock) as update_config,
        patch.object(SnapCastProvider, "_set_snapcast_log_level") as set_log_level,
    ):
        await provider.update_config(config, changed_keys)

    update_config.assert_awaited_once_with(config, changed_keys)
    set_log_level.assert_called_once_with()


async def test_unload_unregisters_mdns_services() -> None:
    """Unloading the provider unregisters the mdns records of the built-in Snapserver."""
    provider = _provider_with_log_level(logging.INFO)
    provider.mass = MagicMock()
    unregister = AsyncMock()
    provider.mass.discovery.aiozc.async_unregister_service = unregister
    provider._snapserver = MagicMock(clients=[])
    provider._snapcast_ma_streams = {}
    provider._snapserver_runner = None
    infos = {"-http": MagicMock(), "": MagicMock()}
    provider._zc_services = dict(infos)

    await provider.unload()

    assert [call.args[0] for call in unregister.await_args_list] == list(infos.values())
    assert provider._zc_services == {}


def test_lost_connection_arms_the_reload_under_the_load_task_id() -> None:
    """A lost SnapServer connection arms the reload so a (re)load starting first cancels it."""
    provider = _provider_with_log_level(logging.INFO)
    provider.config = MagicMock(instance_id="snapcast--test")
    provider.mass = MagicMock(closing=False)
    provider._stop_called = False

    provider._handle_disconnect(ConnectionError("connection lost"))

    retry = provider.mass.call_later.call_args
    assert retry.args == (5, provider.mass.load_provider, "snapcast--test")
    assert retry.kwargs == {"allow_retry": True, "task_id": "load_provider_snapcast--test"}


def _provider_with_clients(
    *identifiers: str,
) -> tuple[SnapCastProvider, MagicMock, list[MagicMock]]:
    """Return a provider whose snapserver has a client for each identifier, and its mass."""
    provider = _provider_with_log_level(logging.INFO)
    provider.mass = mass = MagicMock()
    provider._ids_map = bidict({})
    clients = [MagicMock(identifier=identifier) for identifier in identifiers]
    provider._snapserver = MagicMock(clients=clients, groups=[])
    provider._snapserver.client.side_effect = {c.identifier: c for c in clients}.get
    return provider, mass, clients


async def test_discovery_attaches_the_clients_without_an_attached_protocol() -> None:
    """A client whose player was re-enabled is attached again; attached or disabled ones not."""
    provider, mass, clients = _provider_with_clients("attached", "enabled", "disabled")
    attached = MagicMock()
    mass.players.get_protocol.side_effect = {"ma_attached": attached}.get
    mass.players.wants_protocol.side_effect = lambda protocol_id: protocol_id != "ma_disabled"

    with (
        patch("music_assistant.providers.snapcast.provider.SnapCastPlayer") as player_class,
        patch("music_assistant.providers.snapcast.provider.asyncio.run_coroutine_threadsafe"),
    ):
        await provider.discover_players()

    player_class.assert_called_once_with(
        provider=provider, protocol_id="ma_enabled", snap_client=clients[1]
    )
    mass.players.register.assert_called_once_with(player_class.return_value)
    clients[1].set_callback.assert_called_once_with(player_class.return_value._handle_player_update)
    clients[0].set_callback.assert_not_called()
    clients[2].set_callback.assert_not_called()


def _group_media(shared: bool) -> tuple[SnapCastProvider, PlayerMedia]:
    """Return a provider and media a group with a minted player id hands its members."""
    provider = _provider_with_log_level(logging.INFO)
    provider.mass = MagicMock()
    provider._snapcast_ma_streams = {}
    provider._snapcast_ma_streams_lock = asyncio.Lock()
    provider._controlscript_available = False
    provider._use_builtin_server = False
    group_protocol = MagicMock()
    group_protocol.get_shared_stream.return_value = (
        MagicMock(aclose=AsyncMock()) if shared else None
    )
    group = MagicMock(player_id="pl0123456789ab", own_protocol=group_protocol)
    provider.mass.players.get_player.side_effect = {group.player_id: group}.get
    media = PlayerMedia(
        uri="http://ma.local/ugp/ugp_abc.mp3?player_id=member",
        media_type=MediaType.FLOW_STREAM,
        source_id=group.player_id,
    )
    return provider, media


async def test_the_shared_stream_of_a_group_is_one_snapcast_stream() -> None:
    """Members of a group that renders one stream share one Snapcast stream, named by the group."""
    provider, media = _group_media(shared=True)

    with patch("music_assistant.providers.snapcast.provider.SnapcastMAStream") as stream_class:
        stream_class.return_value.setup = AsyncMock()
        await provider.get_snapcast_media_stream(media)

    assert stream_class.call_args.kwargs["stream_name"] == f"{MASS_STREAM_PREFIX}pl0123456789ab"
    assert stream_class.call_args.kwargs["destroy_on_stop"] is True


async def test_other_media_of_a_group_gets_a_stream_of_its_own() -> None:
    """Media a group does not share is no shared stream, also when it names the group."""
    provider, media = _group_media(shared=False)

    with patch("music_assistant.providers.snapcast.provider.SnapcastMAStream") as stream_class:
        stream_class.return_value.setup = AsyncMock()
        await provider.get_snapcast_media_stream(media)

    assert "pl0123456789ab" not in stream_class.call_args.kwargs["stream_name"]
