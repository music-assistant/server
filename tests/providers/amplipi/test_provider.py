"""Tests for the AmpliPi player provider."""

from __future__ import annotations

import asyncio
from ipaddress import IPv4Address, IPv6Address
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.errors import PlayerCommandFailed, SetupFailedError
from pyamplipi.error import AmpliPiUnreachableError
from zeroconf import IPVersion, ServiceStateChange

from music_assistant.providers.amplipi import mdns, setup, setup_flow
from music_assistant.providers.amplipi.constants import (
    CONF_HOST,
    CONF_MDNS_NAME,
    DEFAULT_HOST,
    MA_STREAM_NAME,
    MA_STREAM_TYPE,
    MDNS_TYPE,
)
from music_assistant.providers.amplipi.provider import AmpliPiPlayerProvider

if TYPE_CHECKING:
    from zeroconf.asyncio import AsyncServiceInfo


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


def _discovery_info(
    name: str = "amplipi-B8:27:EB:8F:8D:85._amplipi._tcp.local.",
    server: str = "amplipi.local.",
    address: str | None = "192.168.11.148",
    v6_address: str | None = None,
) -> AsyncServiceInfo:
    """Build a lightweight stand-in for the resolved mDNS record of an AmpliPi."""
    v4 = [IPv4Address(address)] if address else []
    v6 = [IPv6Address(v6_address)] if v6_address else []
    info = SimpleNamespace(
        name=name,
        server=server,
        async_request=AsyncMock(return_value=True),
        ip_addresses_by_version=lambda version: v4 if version == IPVersion.V4Only else v6,
        parsed_addresses=lambda: [str(a) for a in v4 + v6],
    )
    return cast("AsyncServiceInfo", info)


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


class TestHostToOffer:
    """Test which discovered controller seeds the host field in the setup flow."""

    def test_prefers_the_advertised_hostname(self) -> None:
        """
        The hostname outlives a DHCP lease, so it wins over the advertised address.

        It is preferred even where the system resolver cannot resolve it: the provider
        connects over mass.http_session, whose resolver answers .local from mDNS.
        """
        assert setup_flow._host_to_offer([_discovery_info()], set()) == "amplipi.local"

    def test_falls_back_to_the_default_without_discovery(self) -> None:
        """With no AmpliPi on the network the user still gets the conventional hostname."""
        assert setup_flow._host_to_offer([], set()) == DEFAULT_HOST

    def test_falls_back_to_the_address_without_a_hostname(self) -> None:
        """Only a record carrying no hostname at all falls through to its address."""
        info = _discovery_info(server="")
        assert setup_flow._host_to_offer([info], set()) == "192.168.11.148"

    def test_ipv6_address_is_bracketed(self) -> None:
        """The provider builds "http://<host>/api", which needs a bracketed IPv6 literal."""
        info = _discovery_info(server="", address=None, v6_address="fd00::1")
        assert setup_flow._host_to_offer([info], set()) == "[fd00::1]"

    def test_skips_controllers_another_instance_is_set_up_for(self) -> None:
        """A second instance should be offered the controller that is still free."""
        first = _discovery_info(name="amplipi-aa._amplipi._tcp.local.")
        second = _discovery_info(name="amplipi-bb._amplipi._tcp.local.", server="amplipi-2.local.")
        assert setup_flow._host_to_offer([first, second], {first.name.lower()}) == "amplipi-2.local"

    def test_offers_nothing_when_every_controller_is_taken(self) -> None:
        """Offering a taken controller would only steer the user into a duplicate."""
        info = _discovery_info()
        assert setup_flow._host_to_offer([info], {info.name.lower()}) == ""


class TestControllerMatchesHost:
    """Test tying a configured host to a discovered controller."""

    @pytest.mark.parametrize(
        "host",
        ["amplipi.local", "AMPLIPI.LOCAL", "http://amplipi.local/api", "192.168.11.148:80"],
    )
    def test_matches_hostname_and_address_in_any_form(self, host: str) -> None:
        """The host may be a bare host, host:port or full URL, by name or by address."""
        assert mdns.controller_matches_host(_discovery_info(), host)

    @pytest.mark.parametrize("host", ["amplipi-2.local", "10.0.0.5", "http://[fd00::1]/api"])
    def test_rejects_other_hosts(self, host: str) -> None:
        """A host behind a custom name or another address is not this controller."""
        assert not mdns.controller_matches_host(_discovery_info(), host)


class TestHostnameOf:
    """Test extracting the hostname part of whatever the user typed as host."""

    @pytest.mark.parametrize(
        ("host", "expected"),
        [
            ("AmpliPi.local", "amplipi.local"),
            ("192.168.11.148:80", "192.168.11.148"),
            ("https://amplipi.local/api", "amplipi.local"),
            ("[fd00::1]", "fd00::1"),
        ],
    )
    def test_extracts_the_hostname(self, host: str, expected: str) -> None:
        """A bare host, host:port, bracketed IPv6 literal or full URL all yield the hostname."""
        assert mdns._hostname_of(host) == expected

    @pytest.mark.parametrize("host", ["", "[bad"])
    def test_unparseable_host_yields_nothing(self, host: str) -> None:
        """An empty or malformed host cannot be matched, but must not blow up the flow."""
        assert mdns._hostname_of(host) is None


class TestDiscoveredControllers:
    """Test enumerating the AmpliPi controllers in the mDNS cache."""

    @staticmethod
    def _mass(cache_names: list[str]) -> MagicMock:
        mass = MagicMock()
        mass.discovery.aiozc.zeroconf.cache.cache = dict.fromkeys(cache_names)
        return mass

    async def test_returns_only_resolvable_amplipi_records(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Other service types, the bare type name and a record that no longer answers are skipped."""
        resolves = {
            "amplipi-aa._amplipi._tcp.local.": True,
            "amplipi-bb._amplipi._tcp.local.": False,
        }
        built: list[str] = []

        def fake_info(_type: str, name: str) -> SimpleNamespace:
            built.append(name)
            return SimpleNamespace(name=name, async_request=AsyncMock(return_value=resolves[name]))

        monkeypatch.setattr(mdns, "AsyncServiceInfo", fake_info)
        mass = self._mass(
            [
                "amplipi-bb._amplipi._tcp.local.",
                "_amplipi._tcp.local.",
                "printer._http._tcp.local.",
                "amplipi-aa._amplipi._tcp.local.",
            ]
        )
        result = await mdns.discovered_controllers(mass)
        assert [c.name for c in result] == ["amplipi-aa._amplipi._tcp.local."]
        assert built == ["amplipi-aa._amplipi._tcp.local.", "amplipi-bb._amplipi._tcp.local."]

    async def test_empty_cache_yields_nothing(self) -> None:
        """A network without an AmpliPi produces an empty list, not an error."""
        assert await mdns.discovered_controllers(self._mass([])) == []


class TestClaimedControllers:
    """Test collecting the controllers other AmpliPi instances are set up for."""

    @staticmethod
    def _mass(configs: dict[str, dict[str, str]], setup_values: dict[str, str]) -> MagicMock:
        """Build a mass whose stored provider configs and setup values are the given ones."""
        mass = MagicMock()
        mass.config.get.side_effect = lambda key, default=None: (
            configs if key == "providers" else default
        )
        mass.config.get_provider_setup_value.side_effect = lambda instance_id, _key: (
            setup_values.get(instance_id)
        )
        return mass

    def test_collects_other_amplipi_instances_only(self) -> None:
        """Other providers, the instance being reconfigured and unrecorded ones hold no claim."""
        mass = self._mass(
            {
                "amplipi--1": {"domain": "amplipi"},
                "amplipi--2": {"domain": "amplipi"},
                "amplipi--3": {"domain": "amplipi"},
                "sonos": {"domain": "sonos"},
            },
            {
                "amplipi--1": "amplipi-AA._amplipi._tcp.local.",
                "amplipi--2": "amplipi-bb._amplipi._tcp.local.",
                "sonos": "amplipi-cc._amplipi._tcp.local.",
            },
        )
        assert mdns.claimed_controllers(mass, "amplipi--2") == {"amplipi-aa._amplipi._tcp.local."}

    def test_a_fresh_setup_sees_every_instance(self) -> None:
        """With no instance of its own to exclude, every recorded controller is claimed."""
        mass = self._mass(
            {"amplipi--1": {"domain": "amplipi"}},
            {"amplipi--1": "amplipi-aa._amplipi._tcp.local."},
        )
        assert mdns.claimed_controllers(mass, None) == {"amplipi-aa._amplipi._tcp.local."}


class TestDiscoverControllers:
    """Test the setup flow's wait for a first controller before enumerating the cache."""

    async def test_enumerates_the_cache_once_a_controller_answers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The wait is only for the first record; the cache then yields every controller."""
        session = MagicMock()
        session.mass.discovery.async_find_mdns_service = AsyncMock(return_value=_discovery_info())
        controllers = [_discovery_info(), _discovery_info(name="amplipi-bb._amplipi._tcp.local.")]
        scan = AsyncMock(return_value=controllers)
        monkeypatch.setattr(setup_flow, "discovered_controllers", scan)
        assert await setup_flow._discover_controllers(session) == controllers
        session.mass.discovery.async_find_mdns_service.assert_awaited_once_with(
            MDNS_TYPE, timeout=setup_flow._DISCOVERY_TIMEOUT
        )
        scan.assert_awaited_once_with(session.mass)

    async def test_no_answer_yields_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without any AmpliPi answering in time, the cache is not consulted at all."""
        session = MagicMock()
        session.mass.discovery.async_find_mdns_service = AsyncMock(return_value=None)
        scan = AsyncMock()
        monkeypatch.setattr(setup_flow, "discovered_controllers", scan)
        assert await setup_flow._discover_controllers(session) == []
        scan.assert_not_awaited()


class TestMdnsBackfill:
    """Test that a pre-existing instance records its controller identity on load."""

    @staticmethod
    def _provider_with(setup_values: dict[str, str]) -> AmpliPiPlayerProvider:
        """Build a provider whose stored setup values are the given ones."""
        prov = _provider()
        prov.get_setup_value = lambda key, default=None: setup_values.get(key, default)  # type: ignore[method-assign]
        prov._update_setup_data = MagicMock()  # type: ignore[method-assign]
        return prov

    async def test_records_the_matching_controller(self) -> None:
        """An instance without a recorded identity adopts the record matching its host."""
        prov = self._provider_with({CONF_HOST: "192.168.11.148"})
        info = _discovery_info()
        await prov.on_mdns_service_state_change(info.name, ServiceStateChange.Added, info)
        cast("MagicMock", prov._update_setup_data).assert_called_once_with(
            CONF_MDNS_NAME, info.name.lower()
        )

    async def test_ignores_another_controller(self) -> None:
        """A record for a different unit must not be adopted."""
        prov = self._provider_with({CONF_HOST: "amplipi-2.local"})
        info = _discovery_info()
        await prov.on_mdns_service_state_change(info.name, ServiceStateChange.Added, info)
        cast("MagicMock", prov._update_setup_data).assert_not_called()

    async def test_keeps_an_identity_already_recorded(self) -> None:
        """Once recorded, the identity is never rewritten from later announcements."""
        prov = self._provider_with({CONF_HOST: "amplipi.local", CONF_MDNS_NAME: "recorded"})
        info = _discovery_info()
        await prov.on_mdns_service_state_change(info.name, ServiceStateChange.Added, info)
        cast("MagicMock", prov._update_setup_data).assert_not_called()

    async def test_ignores_a_removal(self) -> None:
        """A Removed event carries no record and is not an error."""
        prov = self._provider_with({CONF_HOST: "amplipi.local"})
        await prov.on_mdns_service_state_change("gone", ServiceStateChange.Removed, None)
        cast("MagicMock", prov._update_setup_data).assert_not_called()


class TestSetupFlowPrefill:
    """Test that the collected host form is seeded from discovery."""

    @staticmethod
    def _session(
        setup_data: dict[str, str], submit: list[str] | None = None, instance_id: str | None = None
    ) -> MagicMock:
        """Build a session that submits the given hosts in turn, else the prefilled value."""
        session = MagicMock()
        session.context.setup_data = setup_data
        session.context.instance_id = instance_id
        session.finish = AsyncMock(return_value={})
        submissions = list(submit or [])

        async def _form(entries: list[object], **_kwargs: object) -> dict[str, object]:
            if submissions:
                return {CONF_HOST: submissions.pop(0)}
            host = next(e for e in entries if e.key == CONF_HOST)  # type: ignore[attr-defined]
            return {CONF_HOST: host.value}  # type: ignore[attr-defined]

        session.form = AsyncMock(side_effect=_form)
        return session

    @staticmethod
    def _discover(
        monkeypatch: pytest.MonkeyPatch, controllers: list[AsyncServiceInfo], claimed: set[str]
    ) -> None:
        """Stub the network and stored-config lookups of the setup flow."""
        monkeypatch.setattr(
            setup_flow, "_discover_controllers", AsyncMock(return_value=controllers)
        )
        monkeypatch.setattr(setup_flow, "claimed_controllers", lambda *_args: claimed)

    async def test_form_is_prefilled_with_the_discovered_host(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fresh setup should offer the discovered controller rather than an empty field."""
        self._discover(monkeypatch, [_discovery_info()], set())
        session = self._session({})
        await setup_flow.run_setup(session)
        entries = session.form.await_args.args[0]
        host = next(e for e in entries if e.key == CONF_HOST)
        assert host.value == "amplipi.local"

    async def test_a_known_host_is_kept(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Reconfiguring keeps the host already collected instead of the discovered one."""
        self._discover(monkeypatch, [_discovery_info()], set())
        session = self._session({CONF_HOST: "10.0.0.5"})
        await setup_flow.run_setup(session)
        entries = session.form.await_args.args[0]
        host = next(e for e in entries if e.key == CONF_HOST)
        assert host.value == "10.0.0.5"

    async def test_records_the_controller_identity(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The matched controller's mDNS name is stored so a later setup can spot it."""
        info = _discovery_info()
        self._discover(monkeypatch, [info], set())
        session = self._session({})
        await setup_flow.run_setup(session)
        session.finish.assert_awaited_once_with(
            {CONF_HOST: "amplipi.local", CONF_MDNS_NAME: info.name.lower()}
        )

    async def test_an_unmatched_host_holds_no_identity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stale identity must not survive a reconfigure onto a host we cannot match."""
        self._discover(monkeypatch, [_discovery_info()], set())
        session = self._session({CONF_HOST: "amplipi.local", CONF_MDNS_NAME: "old"}, ["10.0.0.5"])
        await setup_flow.run_setup(session)
        session.finish.assert_awaited_once_with({CONF_HOST: "10.0.0.5"})

    async def test_rejects_a_controller_another_instance_is_set_up_for(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Submitting a taken controller re-shows the form with an error, by name or address."""
        info = _discovery_info()
        self._discover(monkeypatch, [info], {info.name.lower()})
        session = self._session({}, ["192.168.11.148", "10.0.0.5"])
        await setup_flow.run_setup(session)
        assert session.form.await_count == 2
        assert session.form.await_args_list[1].kwargs["errors"] == {CONF_HOST: "already_configured"}
        session.finish.assert_awaited_once_with({CONF_HOST: "10.0.0.5"})


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
