"""Tests for the discovery core controller."""

import asyncio
import logging
from collections.abc import Iterator
from ipaddress import ip_address
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from zeroconf import (  # type: ignore[attr-defined]  # these DNS classes are missing in __all__
    DNSAddress,
    DNSCache,
    DNSOutgoing,
    DNSPointer,
    DNSService,
    DNSText,
    IPVersion,
    RecordUpdate,
    RecordUpdateListener,
    ServiceStateChange,
    current_time_millis,
)
from zeroconf.asyncio import AsyncZeroconf
from zeroconf.const import (
    _CLASS_IN,
    _CLASS_IN_UNIQUE,
    _FLAGS_TC,
    _TYPE_A,
    _TYPE_PTR,
    _TYPE_SRV,
    _TYPE_TXT,
)

from music_assistant.constants import CONF_SERVER_ID, VERBOSE_LOG_LEVEL
from music_assistant.mass import MusicAssistant


class StubUpnpProvider:
    """Minimal provider stub for SSDP discovery tests."""

    def __init__(self) -> None:
        """Initialize the provider stub."""
        self.instance_id = "stub_upnp"
        self.name = "Stub UPNP"
        self.available = True
        self.logger = MagicMock()
        self.manifest = MagicMock(mdns_discovery=None, upnp_discovery=["roku:ecp"])
        self.on_mdns_service_state_change = AsyncMock()
        self.on_upnp_service_discovered = AsyncMock()


SERVER_ID = "0a1b2c3d4e5f60718293a4b5c6d7e8f9"
FALLBACK_HOSTNAME = "musicassistant-0a1b2c.local."


def _prepare_mass_service(mass: MusicAssistant, version: str = "2.11.0") -> MagicMock:
    """
    Prepare the server for advertising itself, with a mocked zeroconf instance.

    :param mass: The Music Assistant instance.
    :param version: The server version, a release build by default.
    """
    mass.version = version
    mass.config.set(CONF_SERVER_ID, SERVER_ID)
    mass.webserver = MagicMock(
        publish_ip="192.168.1.10",
        publish_addresses=["192.168.1.10", "fd00::10"],
        publish_port=8095,
        base_url="http://192.168.1.10:8095",
    )
    mock_zc = MagicMock(spec=AsyncZeroconf)
    mock_zc.zeroconf = MagicMock()
    mock_zc.async_register_service = AsyncMock()
    mock_zc.async_update_service = AsyncMock()
    mass.discovery._aiozc = mock_zc
    return mock_zc


def _mock_address_resolver(*addresses: str) -> MagicMock:
    """
    Return an AddressResolver stand-in whose lookups get the given addresses as answer.

    :param addresses: The addresses the network answers with.
    """
    resolver = MagicMock()
    resolver.async_request = AsyncMock(return_value=bool(addresses))
    resolver.addresses_by_version.return_value = [ip_address(addr).packed for addr in addresses]
    return MagicMock(return_value=resolver)


@pytest.mark.parametrize(
    ("controller_level", "library_level"),
    [
        (VERBOSE_LOG_LEVEL, logging.DEBUG),
        (logging.DEBUG, logging.INFO),
        (logging.INFO, logging.WARNING),
    ],
)
def test_discovery_library_logger_levels(
    mass_minimal: MusicAssistant, controller_level: int, library_level: int
) -> None:
    """Discovery library loggers should only expose debug output at VERBOSE."""
    controller_logger = mass_minimal.discovery.logger
    library_loggers = [
        logging.getLogger("async_upnp_client"),
        logging.getLogger("zeroconf"),
    ]
    previous_controller_level = controller_logger.level
    previous_library_levels = [logger.level for logger in library_loggers]
    try:
        controller_logger.setLevel(controller_level)
        mass_minimal.discovery._configure_library_loggers()

        assert all(logger.level == library_level for logger in library_loggers)
    finally:
        controller_logger.setLevel(previous_controller_level)
        for logger, previous_level in zip(library_loggers, previous_library_levels, strict=True):
            logger.setLevel(previous_level)


def test_discovery_library_loggers_follow_an_inherited_level(mass_minimal: MusicAssistant) -> None:
    """A controller logger without a level of its own sets the libraries from the one it inherits."""
    controller_logger = mass_minimal.discovery.logger
    assert controller_logger.parent is not None
    loggers = [
        controller_logger,
        controller_logger.parent,
        logging.getLogger("async_upnp_client"),
        logging.getLogger("zeroconf"),
    ]
    previous_levels = [logger.level for logger in loggers]
    try:
        controller_logger.setLevel(logging.NOTSET)
        controller_logger.parent.setLevel(logging.INFO)
        mass_minimal.discovery._configure_library_loggers()

        assert [logger.level for logger in loggers[2:]] == [logging.WARNING, logging.WARNING]
    finally:
        for logger, previous_level in zip(loggers, previous_levels, strict=True):
            logger.setLevel(previous_level)


async def test_run_provider_discovery_dispatches_upnp_callbacks(mass: MusicAssistant) -> None:
    """Provider-targeted discovery should fan SSDP results back into the provider callback."""
    provider = StubUpnpProvider()
    mass._providers[provider.instance_id] = provider  # type: ignore[assignment]

    async def fake_async_search(callback: Any, search_target: str, target: Any = None) -> None:
        del target
        await callback(
            {
                "st": search_target,
                "usn": "uuid:roku-123::roku:ecp",
                "_host": "192.168.1.25",
            }
        )

    with patch(
        "music_assistant.controllers.discovery.controller.async_upnp_search",
        new=AsyncMock(side_effect=fake_async_search),
    ) as mock_async_search:
        await mass.discovery.run_provider_discovery(provider)  # type: ignore[arg-type]

    assert mock_async_search.await_count == 1
    assert mock_async_search.await_args is not None
    assert mock_async_search.await_args.kwargs["search_target"] == "roku:ecp"
    provider.on_upnp_service_discovered.assert_awaited_once()
    mass._providers.pop(provider.instance_id, None)
    mass.discovery.on_provider_unload(provider.instance_id)


async def test_discovery_controller_owns_async_zeroconf(mass_minimal: MusicAssistant) -> None:
    """The shared AsyncZeroconf instance should be created by the discovery controller."""
    mass_minimal.config.set(
        "core/discovery",
        {"domain": "discovery", "values": {"zeroconf_interfaces": "all"}},
    )
    mass_minimal.webserver = MagicMock(
        publish_ip="127.0.0.1",
        publish_addresses=["127.0.0.1"],
        publish_port=8095,
        base_url="http://127.0.0.1:8095",
    )
    mock_zc = MagicMock(spec=AsyncZeroconf)
    mock_zc.async_register_service = AsyncMock()
    mock_zc.async_update_service = AsyncMock()
    mock_zc.async_unregister_service = AsyncMock()
    mock_zc.async_close = AsyncMock()

    # Stub the resolved zeroconf args so the assertion below does not depend on
    # the host's network adapters or platform (both are covered by the util tests)
    zc_args = {"ip_version": IPVersion.All, "interfaces": ["192.168.1.10", "fd00::1%2"]}

    with (
        patch(
            "music_assistant.controllers.discovery.controller.AsyncZeroconf",
            return_value=mock_zc,
        ) as mock_async_zeroconf,
        patch(
            "music_assistant.controllers.discovery.controller.get_ip_pton",
            new=AsyncMock(return_value=b"\x7f\x00\x00\x01"),
        ),
        patch(
            "music_assistant.controllers.discovery.controller.get_zeroconf_args",
            autospec=True,
            return_value=zc_args,
        ) as mock_get_zeroconf_args,
    ):
        await mass_minimal.discovery.start_zeroconf()
        await mass_minimal.discovery.setup(await mass_minimal.config.get_core_config("discovery"))
        assert mass_minimal.discovery.aiozc is mock_zc

    await mass_minimal.discovery.close()

    mock_get_zeroconf_args.assert_called_once_with(True)
    mock_async_zeroconf.assert_called_once_with(
        ip_version=zc_args["ip_version"],
        interfaces=zc_args["interfaces"],
    )


async def test_mass_service_advertises_webserver_publish_addresses(
    mass_minimal: MusicAssistant,
) -> None:
    """The advertisement must contain exactly the webserver's publish addresses."""
    mass_minimal.webserver = MagicMock(
        publish_ip="192.168.1.10",
        publish_addresses=["192.168.1.10", "fd00::10"],
        publish_port=8095,
        base_url="http://192.168.1.10:8095",
    )
    mock_zc = MagicMock(spec=AsyncZeroconf)
    mock_zc.async_register_service = AsyncMock()
    mock_zc.async_update_service = AsyncMock()
    mass_minimal.discovery._aiozc = mock_zc

    with (
        patch(
            "music_assistant.controllers.discovery.controller.AsyncServiceInfo"
        ) as mock_service_info,
        patch(
            "music_assistant.controllers.discovery.controller.get_ip_pton",
            new=AsyncMock(side_effect=lambda ip: ip.encode()),
        ),
    ):
        await mass_minimal.discovery._register_mass_service()

    assert mock_service_info.call_args.kwargs["addresses"] == [b"192.168.1.10", b"fd00::10"]


async def test_refresh_mdns_browser_follows_registered_manifests(
    mass_minimal: MusicAssistant,
) -> None:
    """A refresh replaces the mDNS browser by one for the manifests registered at that time."""
    mass_minimal.discovery._aiozc = MagicMock()
    mass_minimal._provider_manifests["first"] = MagicMock(mdns_discovery=["_first._tcp.local."])

    with patch(
        "music_assistant.controllers.discovery.controller.AsyncServiceBrowser"
    ) as mock_browser:
        first_browser = MagicMock(async_cancel=AsyncMock())
        mock_browser.return_value = first_browser
        await mass_minimal.discovery.refresh_mdns_browser()
        assert mock_browser.call_args.args[1] == ["_first._tcp.local."]

        del mass_minimal._provider_manifests["first"]
        mass_minimal._provider_manifests["second"] = MagicMock(
            mdns_discovery=["_second._tcp.local."]
        )
        mock_browser.return_value = MagicMock(async_cancel=AsyncMock())
        await mass_minimal.discovery.refresh_mdns_browser()

    first_browser.async_cancel.assert_awaited_once()
    assert mock_browser.call_args.args[1] == ["_second._tcp.local."]
    assert mass_minimal.discovery._mdns_browser is mock_browser.return_value


async def test_mass_service_refreshes_on_core_state_update(
    mass_minimal: MusicAssistant,
) -> None:
    """A server info change must refresh the already registered mdns record."""
    mass_minimal.webserver = MagicMock(
        publish_ip="192.168.1.10",
        publish_addresses=["192.168.1.10"],
        publish_port=8095,
        base_url="http://192.168.1.10:8095",
    )
    mock_zc = MagicMock(spec=AsyncZeroconf)
    mock_zc.async_register_service = AsyncMock()
    mock_zc.async_update_service = AsyncMock()
    mass_minimal.discovery._aiozc = mock_zc
    mass_minimal.discovery._mass_service_info = MagicMock()

    with (
        patch("music_assistant.controllers.discovery.controller.AsyncServiceInfo"),
        patch(
            "music_assistant.controllers.discovery.controller.get_ip_pton",
            new=AsyncMock(side_effect=lambda ip: ip.encode()),
        ),
    ):
        await mass_minimal.discovery._on_core_state_updated(MagicMock())

    mock_zc.async_update_service.assert_awaited_once()
    mock_zc.async_register_service.assert_not_awaited()


@pytest.mark.parametrize(
    ("answers", "expected_hostname"),
    [
        ((), "musicassistant.local."),
        (("192.168.1.10", "fd00::10"), "musicassistant.local."),
        (("192.168.1.20",), FALLBACK_HOSTNAME),
        (("192.168.1.10", "192.168.1.20"), FALLBACK_HOSTNAME),
    ],
    ids=["free", "own-addresses", "other-host", "own-and-other-host"],
)
async def test_mass_service_claims_hostname_only_when_free(
    mass_minimal: MusicAssistant, answers: tuple[str, ...], expected_hostname: str
) -> None:
    """A release build advertises on musicassistant.local unless another host answers for it."""
    mock_zc = _prepare_mass_service(mass_minimal)
    mock_resolver = _mock_address_resolver(*answers)

    with patch(
        "music_assistant.controllers.discovery.controller.AddressResolver", new=mock_resolver
    ):
        await mass_minimal.discovery._register_mass_service()

    mock_resolver.assert_called_once_with("musicassistant.local.")
    assert mock_zc.async_register_service.await_args.args[0].server == expected_hostname


@pytest.mark.parametrize("version", ["0.0.0", "2.11.0.dev2026111601"])
async def test_mass_service_dev_build_uses_own_hostname(
    mass_minimal: MusicAssistant, version: str
) -> None:
    """A dev build advertises on its own hostname, without probing for the friendly one."""
    mock_zc = _prepare_mass_service(mass_minimal, version)
    mock_resolver = _mock_address_resolver()

    with patch(
        "music_assistant.controllers.discovery.controller.AddressResolver", new=mock_resolver
    ):
        await mass_minimal.discovery._register_mass_service()

    mock_resolver.assert_not_called()
    assert mock_zc.async_register_service.await_args.args[0].server == FALLBACK_HOSTNAME


async def test_mass_service_refresh_keeps_hostname(mass_minimal: MusicAssistant) -> None:
    """A server info change re-advertises on the hostname chosen at startup, without a probe."""
    mock_zc = _prepare_mass_service(mass_minimal)
    with patch(
        "music_assistant.controllers.discovery.controller.AddressResolver",
        new=_mock_address_resolver("192.168.1.20"),
    ):
        await mass_minimal.discovery._register_mass_service()

    mock_resolver = _mock_address_resolver()
    with patch(
        "music_assistant.controllers.discovery.controller.AddressResolver", new=mock_resolver
    ):
        await mass_minimal.discovery._on_core_state_updated(MagicMock())

    mock_resolver.assert_not_called()
    assert mock_zc.async_update_service.await_args.args[0].server == FALLBACK_HOSTNAME


async def test_mass_service_registers_after_setup(mass_minimal: MusicAssistant) -> None:
    """Setup does not wait for the hostname probe, and a refresh during the probe waits for it."""
    mock_zc = _prepare_mass_service(mass_minimal)
    probe_answered = asyncio.Event()

    async def wait_for_answer(*_args: Any) -> bool:
        await probe_answered.wait()
        return False

    mock_resolver = _mock_address_resolver()
    mock_resolver.return_value.async_request = AsyncMock(side_effect=wait_for_answer)
    with (
        patch(
            "music_assistant.controllers.discovery.controller.AddressResolver", new=mock_resolver
        ),
        # resolve the addresses without yielding, so each registration runs straight to the probe
        patch(
            "music_assistant.controllers.discovery.controller.get_ip_pton",
            new=AsyncMock(side_effect=lambda ip: ip_address(ip).packed),
        ),
    ):
        await mass_minimal.discovery.setup(await mass_minimal.config.get_core_config("discovery"))
        mock_zc.async_register_service.assert_not_awaited()

        refresh = asyncio.create_task(mass_minimal.discovery._on_core_state_updated(MagicMock()))
        await asyncio.sleep(0)
        probe_answered.set()
        await refresh

    mock_resolver.assert_called_once()
    mock_zc.async_register_service.assert_awaited_once()
    mock_zc.async_update_service.assert_awaited_once()
    assert mock_zc.async_update_service.await_args.args[0].server == "musicassistant.local."


async def test_async_find_mdns_service_matches_exact_device_name(mass: MusicAssistant) -> None:
    """A device name must not cross-match another device whose name contains it."""
    mass.discovery.aiozc.zeroconf.cache.cache = {
        "aabbccddeeff@kelder._raop._tcp.local.": {},
        "atv kelder._raop._tcp.local.": {},
    }
    mock_info = MagicMock()
    mock_info.async_request = AsyncMock(return_value=True)
    with patch(
        "music_assistant.controllers.discovery.controller.AsyncServiceInfo",
        return_value=mock_info,
    ) as mock_service_info:
        result = await mass.discovery.async_find_mdns_service(
            "_raop._tcp.local.", "Kelder", timeout=1.0
        )

    assert result is mock_info
    # "Kelder" must resolve to its own RAOP entry, never the "ATV Kelder" one.
    mock_service_info.assert_called_once_with(
        "_raop._tcp.local.", "aabbccddeeff@kelder._raop._tcp.local."
    )


async def test_async_find_mdns_service_no_substring_match(mass_minimal: MusicAssistant) -> None:
    """Looking up a name that is only a substring of a discovered name should not match."""
    # a started server advertises itself in background tasks that also build an
    # AsyncServiceInfo, which would land in the patch below while the lookup waits
    mass_minimal.discovery._aiozc = MagicMock()
    mass_minimal.discovery.aiozc.zeroconf.cache.cache = {
        "atv kelder._raop._tcp.local.": {},
    }
    with patch(
        "music_assistant.controllers.discovery.controller.AsyncServiceInfo",
    ) as mock_service_info:
        result = await mass_minimal.discovery.async_find_mdns_service(
            "_raop._tcp.local.", "Kelder", timeout=0.1
        )

    assert result is None
    mock_service_info.assert_not_called()


async def test_async_find_mdns_service_without_filter_matches_any_instance(
    mass: MusicAssistant,
) -> None:
    """Omitting the name filter accepts whichever instance of the service type answers."""
    mass.discovery.aiozc.zeroconf.cache.cache = {
        "amplipi-b827eb8f8d85._amplipi._tcp.local.": {},
    }
    mock_info = MagicMock()
    mock_info.async_request = AsyncMock(return_value=True)
    with patch(
        "music_assistant.controllers.discovery.controller.AsyncServiceInfo",
        return_value=mock_info,
    ) as mock_service_info:
        result = await mass.discovery.async_find_mdns_service("_amplipi._tcp.local.", timeout=1.0)

    assert result is mock_info
    mock_service_info.assert_called_once_with(
        "_amplipi._tcp.local.", "amplipi-b827eb8f8d85._amplipi._tcp.local."
    )


async def test_async_find_mdns_service_tries_past_a_stale_record(mass: MusicAssistant) -> None:
    """A cached record that no longer answers must not starve the candidates behind it."""
    mass.discovery.aiozc.zeroconf.cache.cache = {
        "amplipi-one._amplipi._tcp.local.": {},
        "amplipi-two._amplipi._tcp.local.": {},
    }
    attempts: list[str] = []

    class _Info:
        def __init__(self, _service_type: str, name: str) -> None:
            self.name = name

        async def async_request(self, _zeroconf: object, timeout: float) -> bool:
            attempts.append(self.name)
            # candidates come off a set, so which name is tried first varies by hash seed;
            # fail whichever it is, after burning the whole slice it was handed
            if len(attempts) == 1:
                await asyncio.sleep(timeout / 1000)
                return False
            return True

    with patch(
        "music_assistant.controllers.discovery.controller.AsyncServiceInfo",
        new=_Info,
    ):
        result = await mass.discovery.async_find_mdns_service("_amplipi._tcp.local.", timeout=0.2)

    assert len(attempts) == 2
    assert result is not None
    assert result.name == attempts[1]


async def test_async_find_mdns_service_rescans_after_a_stale_request(
    mass: MusicAssistant,
) -> None:
    """An instance announcing itself while a stale record is being resolved must still be found."""
    cache = mass.discovery.aiozc.zeroconf.cache.cache = {
        "amplipi-stale._amplipi._tcp.local.": {},
    }
    attempts: list[str] = []

    class _Info:
        def __init__(self, _service_type: str, name: str) -> None:
            self.name = name

        async def async_request(self, _zeroconf: object, timeout: float) -> bool:
            attempts.append(self.name)
            if "stale" in self.name:
                # the live instance announces itself midway through the stale request
                cache["amplipi-live._amplipi._tcp.local."] = {}
                for waiter in mass.discovery._mdns_waiters:
                    waiter.set()
                await asyncio.sleep(timeout / 1000)
                return False
            return True

    with patch(
        "music_assistant.controllers.discovery.controller.AsyncServiceInfo",
        new=_Info,
    ):
        result = await mass.discovery.async_find_mdns_service("_amplipi._tcp.local.", timeout=0.2)

    assert result is not None
    assert result.name == "amplipi-live._amplipi._tcp.local."


async def test_async_find_mdns_service_preserves_at_sign_in_name(mass: MusicAssistant) -> None:
    """Only a RAOP MAC prefix is stripped, so device names containing '@' still match."""
    mass.discovery.aiozc.zeroconf.cache.cache = {
        "living@home._airplay._tcp.local.": {},
    }
    mock_info = MagicMock()
    mock_info.async_request = AsyncMock(return_value=True)
    with patch(
        "music_assistant.controllers.discovery.controller.AsyncServiceInfo",
        return_value=mock_info,
    ) as mock_service_info:
        result = await mass.discovery.async_find_mdns_service(
            "_airplay._tcp.local.", "Living@Home", timeout=1.0
        )

    assert result is mock_info
    mock_service_info.assert_called_once_with(
        "_airplay._tcp.local.", "living@home._airplay._tcp.local."
    )


AIRPLAY_TYPE = "_airplay._tcp.local."
AIRPLAY_NAME = f"kitchen.{AIRPLAY_TYPE}"
AIRPLAY_HOST = "kitchen.local."


def _airplay_records(age: float) -> list[DNSAddress | DNSPointer | DNSService | DNSText]:
    """
    Return the records of one AirPlay service, as if last heard the given seconds ago.

    :param age: How many seconds ago the records were received.
    """
    created = current_time_millis() - age * 1000
    return [
        DNSPointer(AIRPLAY_TYPE, _TYPE_PTR, _CLASS_IN, 4500, AIRPLAY_NAME, created),
        DNSService(
            AIRPLAY_NAME, _TYPE_SRV, _CLASS_IN_UNIQUE, 120, 0, 0, 7000, AIRPLAY_HOST, created
        ),
        DNSText(AIRPLAY_NAME, _TYPE_TXT, _CLASS_IN_UNIQUE, 4500, b"\x08model=x1", created),
        DNSAddress(
            AIRPLAY_HOST, _TYPE_A, _CLASS_IN_UNIQUE, 120, bytes([10, 0, 0, 5]), created=created
        ),
    ]


class StubZeroconf:
    """A zeroconf stand-in with a real cache that keeps the packets it would send."""

    def __init__(self, live_names: set[str]) -> None:
        """
        Initialize the stub.

        :param live_names: The service names whose device answers a question.
        """
        self.cache = DNSCache()
        self.started = True
        self.sent: list[DNSOutgoing] = []
        self._live_names = live_names
        self._listeners: list[RecordUpdateListener] = []

    def async_add_listener(self, listener: RecordUpdateListener, _question: object) -> None:
        """Register a record listener."""
        self._listeners.append(listener)

    def async_remove_listener(self, listener: RecordUpdateListener) -> None:
        """Remove a record listener."""
        self._listeners.remove(listener)

    def async_send(self, out: DNSOutgoing, *_args: object) -> None:
        """Keep the packet and let the live devices that are asked for answer it."""
        self.sent.append(out)
        if not any(question.name in self._live_names for question in out.questions):
            return
        records = _airplay_records(age=0)
        updates = [RecordUpdate(record, self.cache.async_get_unique(record)) for record in records]
        for listener in self._listeners:
            listener.async_update_records(self, current_time_millis(), updates)  # type: ignore[arg-type]
        self.cache.async_add_records(records)


def _mdns_provider() -> MagicMock:
    """Return a provider stand-in that discovers AirPlay services over mDNS."""
    provider = MagicMock(instance_id="stub_mdns")
    provider.manifest.mdns_discovery = [AIRPLAY_TYPE]
    provider.on_mdns_service_state_change = AsyncMock()
    return provider


@pytest.fixture
def fast_mdns_confirm() -> Iterator[None]:
    """Shorten the wait for answers to an mDNS question."""
    with (
        patch("music_assistant.controllers.discovery.controller.MDNS_CONFIRM_RESEND", 0.01),
        patch("music_assistant.controllers.discovery.controller.MDNS_CONFIRM_TIMEOUT", 0.02),
    ):
        yield


@pytest.mark.usefixtures("fast_mdns_confirm")
async def test_replay_skips_a_cached_service_that_no_longer_answers(mass: MusicAssistant) -> None:
    """A service still cached after its device went quiet is asked for and not replayed."""
    zeroconf = StubZeroconf(live_names=set())
    zeroconf.cache.async_add_records(_airplay_records(age=60))
    mass.discovery._aiozc = MagicMock(zeroconf=zeroconf)
    provider = _mdns_provider()

    await mass.discovery._replay_mdns_discovery(provider)

    provider.on_mdns_service_state_change.assert_not_awaited()
    # asked twice (with one resend), both times without the cached answers
    assert len(zeroconf.sent) == 2
    for out in zeroconf.sent:
        assert [(q.name, q.type) for q in out.questions] == [(AIRPLAY_NAME, _TYPE_SRV)]
        assert not out.answers


@pytest.mark.usefixtures("fast_mdns_confirm")
async def test_replay_hands_on_a_cached_service_that_answers(mass: MusicAssistant) -> None:
    """A service last heard a while ago is replayed once its device answers the question."""
    zeroconf = StubZeroconf(live_names={AIRPLAY_NAME})
    zeroconf.cache.async_add_records(_airplay_records(age=60))
    mass.discovery._aiozc = MagicMock(zeroconf=zeroconf)
    provider = _mdns_provider()

    await mass.discovery._replay_mdns_discovery(provider)

    assert len(zeroconf.sent) == 1
    provider.on_mdns_service_state_change.assert_awaited_once()
    name, state_change, info = provider.on_mdns_service_state_change.await_args.args
    assert (name, state_change) == (AIRPLAY_NAME, ServiceStateChange.Added)
    assert info.parsed_addresses() == ["10.0.0.5"]


async def test_replay_trusts_a_service_heard_recently(mass: MusicAssistant) -> None:
    """A service heard moments ago is replayed straight from the cache, without a question."""
    zeroconf = StubZeroconf(live_names=set())
    zeroconf.cache.async_add_records(_airplay_records(age=5))
    mass.discovery._aiozc = MagicMock(zeroconf=zeroconf)
    provider = _mdns_provider()

    await mass.discovery._replay_mdns_discovery(provider)

    assert zeroconf.sent == []
    provider.on_mdns_service_state_change.assert_awaited_once()


@pytest.mark.usefixtures("fast_mdns_confirm")
async def test_confirm_asks_in_queries_of_one_packet(mass: MusicAssistant) -> None:
    """Many services are asked for in queries of one packet each, none flagged as truncated."""
    names = {f"{'speaker' * 8}-{index}.{AIRPLAY_TYPE}" for index in range(60)}
    zeroconf = StubZeroconf(live_names=set())
    mass.discovery._aiozc = MagicMock(zeroconf=zeroconf)

    assert await mass.discovery._async_confirm_mdns_services(names) == set()

    # a responder waits 400-500 ms before it answers a truncated query (RFC 6762, section 7.2)
    packets = [packet for out in zeroconf.sent for packet in out.packets()]
    assert len(packets) == len(zeroconf.sent) > 2
    assert not any(int.from_bytes(packet[2:4]) & _FLAGS_TC for packet in packets)
    asked = [question.name for out in zeroconf.sent for question in out.questions]
    assert len(asked) == 2 * len(names)
    assert set(asked) == names
