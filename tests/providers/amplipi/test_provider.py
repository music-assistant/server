"""Tests for the AmpliPi player provider."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.errors import PlayerCommandFailed, SetupFailedError
from pyamplipi.error import AmpliPiUnreachableError

from music_assistant.providers.amplipi import setup, setup_flow
from music_assistant.providers.amplipi.constants import CONF_HOST, MA_STREAM_NAME, MA_STREAM_TYPE
from music_assistant.providers.amplipi.provider import AmpliPiPlayerProvider


def _zone(zone_id: int, disabled: bool = False) -> SimpleNamespace:
    """Build a lightweight stand-in for a pyamplipi Zone."""
    return SimpleNamespace(
        id=zone_id, name=f"Zone {zone_id}", source_id=-1, disabled=disabled, mute=False, vol=-30
    )


def _provider() -> AmpliPiPlayerProvider:
    """Build a provider instance without running the (heavy) base __init__."""
    prov = AmpliPiPlayerProvider.__new__(AmpliPiPlayerProvider)
    prov.config = MagicMock()
    prov.config.instance_id = "amplipi_test"
    prov.logger = MagicMock()
    prov.api = MagicMock()
    prov.mass = MagicMock()
    prov._players = {}
    prov._ma_streams = {}
    prov._streams = []
    prov._stream_locks = {}
    return prov


class TestHandleAsyncInit:
    """Test provider connection/initialisation."""

    async def test_builds_endpoint_and_connects(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A bare host should be turned into a full http://<host>/api endpoint."""
        prov = AmpliPiPlayerProvider.__new__(AmpliPiPlayerProvider)
        prov.config = MagicMock()
        prov.config.get_value.return_value = "amplipi.local"
        prov.mass = MagicMock()
        # setup_data is unset here, so get_setup_value falls through to config.get_value
        prov.config.values = {}
        prov.mass.config.get.return_value = None
        prov.mass.config.get_raw_provider_config_value.return_value = None
        fake_api = MagicMock()
        fake_api.get_status = AsyncMock(return_value="STATUS")
        created: dict[str, object] = {}

        def fake_ctor(**kwargs: object) -> MagicMock:
            created.update(kwargs)
            return fake_api

        monkeypatch.setattr("music_assistant.providers.amplipi.provider.AmpliPi", fake_ctor)
        await prov.handle_async_init()

        assert created["endpoint"] == "http://amplipi.local/api"
        assert prov._status == "STATUS"
        assert prov._players == {}
        assert prov._ma_streams == {}

    async def test_full_url_host_passed_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A host already containing a scheme is used verbatim as the endpoint."""
        prov = AmpliPiPlayerProvider.__new__(AmpliPiPlayerProvider)
        prov.config = MagicMock()
        prov.config.get_value.return_value = "http://1.2.3.4/api"
        prov.mass = MagicMock()
        # setup_data is unset here, so get_setup_value falls through to config.get_value
        prov.config.values = {}
        prov.mass.config.get.return_value = None
        prov.mass.config.get_raw_provider_config_value.return_value = None
        fake_api = MagicMock()
        fake_api.get_status = AsyncMock(return_value="STATUS")
        created: dict[str, object] = {}
        monkeypatch.setattr(
            "music_assistant.providers.amplipi.provider.AmpliPi",
            lambda **kwargs: (created.update(kwargs), fake_api)[1],
        )
        await prov.handle_async_init()
        assert created["endpoint"] == "http://1.2.3.4/api"

    async def test_schemed_host_without_path_gets_api(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A schemed host with no path (incl. https) keeps its scheme and gains /api."""
        prov = AmpliPiPlayerProvider.__new__(AmpliPiPlayerProvider)
        prov.config = MagicMock()
        prov.config.get_value.return_value = "https://amplipi.local/"
        prov.mass = MagicMock()
        # setup_data is unset here, so get_setup_value falls through to config.get_value
        prov.config.values = {}
        prov.mass.config.get.return_value = None
        prov.mass.config.get_raw_provider_config_value.return_value = None
        fake_api = MagicMock()
        fake_api.get_status = AsyncMock(return_value="STATUS")
        created: dict[str, object] = {}
        monkeypatch.setattr(
            "music_assistant.providers.amplipi.provider.AmpliPi",
            lambda **kwargs: (created.update(kwargs), fake_api)[1],
        )
        await prov.handle_async_init()
        assert created["endpoint"] == "https://amplipi.local/api"

    async def test_http_prefixed_bare_host_gets_scheme(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bare host that merely starts with 'http' must still get a scheme and /api."""
        prov = AmpliPiPlayerProvider.__new__(AmpliPiPlayerProvider)
        prov.config = MagicMock()
        prov.config.get_value.return_value = "http-livingroom.local"
        prov.mass = MagicMock()
        # setup_data is unset here, so get_setup_value falls through to config.get_value
        prov.config.values = {}
        prov.mass.config.get.return_value = None
        prov.mass.config.get_raw_provider_config_value.return_value = None
        fake_api = MagicMock()
        fake_api.get_status = AsyncMock(return_value="STATUS")
        created: dict[str, object] = {}
        monkeypatch.setattr(
            "music_assistant.providers.amplipi.provider.AmpliPi",
            lambda **kwargs: (created.update(kwargs), fake_api)[1],
        )
        await prov.handle_async_init()
        assert created["endpoint"] == "http://http-livingroom.local/api"

    async def test_connection_failure_raises_setup_failed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failure to fetch the initial status must raise SetupFailedError."""
        prov = AmpliPiPlayerProvider.__new__(AmpliPiPlayerProvider)
        prov.config = MagicMock()
        prov.config.get_value.return_value = "amplipi.local"
        prov.mass = MagicMock()
        # setup_data is unset here, so get_setup_value falls through to config.get_value
        prov.config.values = {}
        prov.mass.config.get.return_value = None
        prov.mass.config.get_raw_provider_config_value.return_value = None
        fake_api = MagicMock()
        fake_api.get_status = AsyncMock(side_effect=AmpliPiUnreachableError("no route"))
        monkeypatch.setattr(
            "music_assistant.providers.amplipi.provider.AmpliPi", lambda **_kwargs: fake_api
        )
        with pytest.raises(SetupFailedError):
            await prov.handle_async_init()


class TestLifecycle:
    """Test loaded_in_mass / unload."""

    async def test_loaded_in_mass_adopts_streams(self) -> None:
        """loaded_in_mass should fetch streams, re-adopt MA streams, discover and poll."""
        prov = _provider()
        prov.api.get_streams = AsyncMock(
            return_value=[
                SimpleNamespace(id=7, name=f"{MA_STREAM_NAME} 0", type=MA_STREAM_TYPE),
                SimpleNamespace(id=8, name=f"{MA_STREAM_NAME} 2", type=MA_STREAM_TYPE),
                SimpleNamespace(id=9, name="Groove Salad", type=MA_STREAM_TYPE),
                SimpleNamespace(id=None, name=f"{MA_STREAM_NAME} 3", type=MA_STREAM_TYPE),
                SimpleNamespace(id=10, name=f"{MA_STREAM_NAME} x", type=MA_STREAM_TYPE),
                SimpleNamespace(id=11, name=f"{MA_STREAM_NAME} Radio", type="fileplayer"),
            ]
        )
        prov.discover_players = AsyncMock()  # type: ignore[method-assign]
        task_obj = object()
        prov.mass.create_task = MagicMock(return_value=task_obj)  # type: ignore[method-assign]

        await prov.loaded_in_mass()

        prov.discover_players.assert_awaited_once()
        assert prov._poll_task is task_obj
        # only well-formed MA streams with an id are adopted, keyed by their source id
        assert prov._ma_streams == {0: 7, 2: 8}

    async def test_unload_reload_keeps_streams(self) -> None:
        """A plain reload cancels (and awaits) polling and unregisters players, keeping streams."""
        prov = _provider()

        async def _poll() -> None:
            await asyncio.sleep(3600)

        task = asyncio.ensure_future(_poll())
        prov._poll_task = task
        player = MagicMock()
        player.player_id = "amplipi_test_zone_0"
        prov._players = {0: player}
        prov.mass.players.unregister = AsyncMock()  # type: ignore[method-assign]
        prov.api.delete_stream = AsyncMock()

        await prov.unload()

        # the poll task is cancelled and awaited to completion
        assert task.cancelled()
        prov.mass.players.unregister.assert_awaited_once_with("amplipi_test_zone_0")
        assert prov._players == {}
        prov.api.delete_stream.assert_not_awaited()

    async def test_unload_removed_deletes_streams(self) -> None:
        """When the provider is removed, its MA streams are deleted from the controller."""
        prov = _provider()
        prov._poll_task = None
        prov._players = {}
        prov.api.get_streams = AsyncMock(
            return_value=[SimpleNamespace(id=7, name=f"{MA_STREAM_NAME} 0", type=MA_STREAM_TYPE)]
        )
        prov.api.delete_stream = AsyncMock()

        await prov.unload(is_removed=True)

        prov.api.delete_stream.assert_awaited_once_with(7)


class TestEnsureStream:
    """Test the per-source MA internetradio stream management."""

    async def test_reuses_existing_stream(self) -> None:
        """An already-created stream should be re-pointed at the new url, not recreated."""
        prov = _provider()
        prov._ma_streams = {0: 42}
        prov.api.set_stream = AsyncMock()
        prov.api.create_stream = AsyncMock()

        result = await prov.ensure_stream(0, "http://ma/new.flac")

        assert result == 42
        prov.api.set_stream.assert_awaited_once()
        prov.api.create_stream.assert_not_awaited()

    async def test_creates_stream_and_looks_up_id(self) -> None:
        """A new stream is created, then its id resolved from the streams list."""
        prov = _provider()
        prov.api.create_stream = AsyncMock()
        prov.api.get_streams = AsyncMock(
            return_value=[
                SimpleNamespace(id=7, name=f"{MA_STREAM_NAME} 1", type=MA_STREAM_TYPE),
                SimpleNamespace(id=8, name="Groove Salad", type=MA_STREAM_TYPE),
            ]
        )
        result = await prov.ensure_stream(1, "http://ma/x.flac")

        assert result == 7
        assert prov._ma_streams[1] == 7

    async def test_create_failure_raises(self) -> None:
        """If the new stream cannot be found after creation, raise PlayerCommandFailed."""
        prov = _provider()
        prov.api.create_stream = AsyncMock()
        prov.api.get_streams = AsyncMock(return_value=[])
        with pytest.raises(PlayerCommandFailed):
            await prov.ensure_stream(2, "http://ma/x.flac")


class TestDiscoverAndHelpers:
    """Test discover_players and small helpers."""

    async def test_discover_registers_enabled_zones(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Disabled zones and zones without an id are skipped; the rest are registered."""
        prov = _provider()
        prov._status = SimpleNamespace(
            zones=[
                _zone(0),
                _zone(1, disabled=True),
                SimpleNamespace(id=None, disabled=False),
                _zone(2),
            ],
            sources=[],
        )
        prov.mass.players.register_or_update = AsyncMock()  # type: ignore[method-assign]
        monkeypatch.setattr(
            "music_assistant.providers.amplipi.provider.AmpliPiZonePlayer",
            lambda _provider, zone_id: SimpleNamespace(
                player_id=f"z{zone_id}", update_from_status=MagicMock()
            ),
        )
        await prov.discover_players()

        assert set(prov._players) == {0, 2}
        assert prov.mass.players.register_or_update.await_count == 2

    async def test_discover_skips_already_registered(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A zone already in _players is not registered again."""
        prov = _provider()
        prov._players = {0: MagicMock()}
        prov._status = SimpleNamespace(zones=[_zone(0)], sources=[])
        prov.mass.players.register_or_update = AsyncMock()  # type: ignore[method-assign]
        monkeypatch.setattr(
            "music_assistant.providers.amplipi.provider.AmpliPiZonePlayer",
            lambda _provider, _zone_id: SimpleNamespace(player_id="z0"),
        )
        await prov.discover_players()
        prov.mass.players.register_or_update.assert_not_awaited()

    def test_status_property(self) -> None:
        """The status property returns the last polled status."""
        prov = _provider()
        prov._status = "STATUS"
        assert prov.status == "STATUS"

    def test_zone_id_for(self) -> None:
        """zone_id_for maps a player_id back to its zone id, or None when unknown."""
        prov = _provider()
        prov._players = {
            0: SimpleNamespace(player_id="a"),  # type: ignore[dict-item]
            1: SimpleNamespace(player_id="b"),  # type: ignore[dict-item]
        }
        assert prov.zone_id_for("b") == 1
        assert prov.zone_id_for("missing") is None

    def test_selectable_streams_filters(self) -> None:
        """Native streams + RCA inputs are selectable; MA streams and fileplayer are excluded."""
        prov = _provider()
        prov._streams = [
            SimpleNamespace(id=996, name="Input 1", type="rca"),
            SimpleNamespace(id=1000, name="Groove Salad", type="internetradio"),
            SimpleNamespace(id=1008, name="External Media", type="fileplayer"),
            SimpleNamespace(id=1009, name=f"{MA_STREAM_NAME} 3", type="internetradio"),
            SimpleNamespace(id=None, name="No id", type="airplay"),
            # a user stream that merely starts with the MA name prefix stays selectable
            SimpleNamespace(id=1010, name=f"{MA_STREAM_NAME} Radio", type="internetradio"),
        ]
        assert {s.id for s in prov.selectable_streams()} == {996, 1000, 1010}


class TestPollLoop:
    """Test the polling loop's success and error handling."""

    async def test_poll_success_updates_players(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A successful poll refreshes status, streams and every player."""
        prov = _provider()
        new_status = SimpleNamespace(zones=[_zone(0)], sources=[])
        prov.api.get_status = AsyncMock(return_value=new_status)
        prov.api.get_streams = AsyncMock(return_value=[])
        prov.discover_players = AsyncMock()  # type: ignore[method-assign]
        player = MagicMock()
        prov._players = {0: player}
        monkeypatch.setattr(
            "music_assistant.providers.amplipi.provider.asyncio.sleep",
            AsyncMock(side_effect=[None, asyncio.CancelledError()]),
        )
        with pytest.raises(asyncio.CancelledError):
            await prov._poll_loop()

        assert prov._status == new_status
        prov.discover_players.assert_awaited()
        player.update_from_status.assert_called_with(new_status)

    async def test_poll_propagates_cancellation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A CancelledError raised by the status fetch must propagate, not be swallowed."""
        prov = _provider()
        prov.api.get_status = AsyncMock(side_effect=asyncio.CancelledError())
        prov._players = {}
        monkeypatch.setattr(
            "music_assistant.providers.amplipi.provider.asyncio.sleep",
            AsyncMock(side_effect=[None]),
        )
        with pytest.raises(asyncio.CancelledError):
            await prov._poll_loop()
        cast("MagicMock", prov.logger).warning.assert_not_called()

    async def test_poll_error_marks_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A failed poll marks every player unavailable and continues."""
        prov = _provider()
        prov.api.get_status = AsyncMock(side_effect=AmpliPiUnreachableError("boom"))
        player = MagicMock()
        prov._players = {0: player}
        monkeypatch.setattr(
            "music_assistant.providers.amplipi.provider.asyncio.sleep",
            AsyncMock(side_effect=[None, asyncio.CancelledError()]),
        )
        with pytest.raises(asyncio.CancelledError):
            await prov._poll_loop()

        player.set_unavailable.assert_called_once()
        cast("MagicMock", prov.logger).warning.assert_called()


class TestRemoveMaStreams:
    """Test cleanup of MA-created streams."""

    async def test_removes_only_ma_streams(self) -> None:
        """Only streams named with the MA prefix, of MA_STREAM_TYPE, with an id, are deleted."""
        prov = _provider()
        prov._ma_streams = {0: 7}
        prov.api.get_streams = AsyncMock(
            return_value=[
                SimpleNamespace(id=7, name=f"{MA_STREAM_NAME} 1", type=MA_STREAM_TYPE),
                SimpleNamespace(id=8, name="Groove Salad", type=MA_STREAM_TYPE),
                SimpleNamespace(id=None, name=f"{MA_STREAM_NAME} 2", type=MA_STREAM_TYPE),
                # a user stream that merely starts with the same name prefix must survive
                SimpleNamespace(id=9, name=f"{MA_STREAM_NAME} Radio", type="fileplayer"),
            ]
        )
        prov.api.delete_stream = AsyncMock()

        await prov._remove_ma_streams()

        prov.api.delete_stream.assert_awaited_once_with(7)
        assert prov._ma_streams == {}


class TestModuleEntryPoints:
    """Test the provider module setup / config entry hooks."""

    async def test_setup_returns_provider(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """setup() should construct and return an AmpliPiPlayerProvider."""
        monkeypatch.setattr(
            "music_assistant.providers.amplipi.AmpliPiPlayerProvider",
            lambda *_args: "PROVIDER",
        )
        result = await setup(MagicMock(), MagicMock(), MagicMock())
        assert result == "PROVIDER"  # type: ignore[comparison-overlap]

    async def test_get_config_entries_has_no_setup_entries(self) -> None:
        """The host moved to the setup flow, so the options entries no longer expose it."""
        entries = await _provider().get_config_entries()
        assert all(e.key != CONF_HOST for e in entries)

    async def test_setup_flow_exposes_host(self) -> None:
        """The setup flow must collect a required Host entry."""
        host = next(e for e in setup_flow._ENTRIES if e.key == CONF_HOST)
        assert host.required is True
