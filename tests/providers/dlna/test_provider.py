"""Tests for the DLNA player provider."""

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from async_upnp_client.exceptions import UpnpConnectionError
from music_assistant_models.errors import InvalidDataError

from music_assistant.constants import CONF_ENTRY_MANUAL_DISCOVERY_IPS
from music_assistant.providers.dlna import provider as provider_module
from music_assistant.providers.dlna.player import DLNAPlayer
from music_assistant.providers.dlna.provider import DLNAPlayerProvider, _validate_manual_addresses

UDN = "uuid:renderer-123"
DESCRIPTION_URL = "http://192.168.1.10/description.xml"
IP_ADDRESS = "192.168.1.10"
MEDIA_RENDERER = "urn:schemas-upnp-org:device:MediaRenderer:1"
RENDERER_RESPONSE = {
    "st": MEDIA_RENDERER,
    "usn": f"{UDN}::{MEDIA_RENDERER}",
    "location": DESCRIPTION_URL,
}


def _provider_with_manual_addresses(addresses: list[str]) -> Any:
    """Create a provider with the given manual addresses configured."""
    provider = cast("Any", DLNAPlayerProvider.__new__(DLNAPlayerProvider))
    provider.config = MagicMock()
    provider.config.get_value.return_value = addresses
    provider.config.instance_id = "dlna"
    provider.mass = MagicMock()
    provider.get_protocol = MagicMock(return_value=None)
    provider.logger = MagicMock()
    provider._manual_udns = {}
    provider._manual_rescan_task_id = "dlna_manual_rescan_dlna"
    provider.upnp_factory = MagicMock()
    provider.upnp_factory.async_create_device = AsyncMock(return_value=MagicMock(udn=UDN))
    provider._device_discovered = AsyncMock()
    return provider


def _mock_ssdp_search(
    monkeypatch: pytest.MonkeyPatch, responses: list[dict[str, str]]
) -> AsyncMock:
    """Patch the SSDP search to answer with the given responses."""

    async def fake_ssdp_search(callback: Any, **_kwargs: Any) -> None:
        for response in responses:
            await callback(response)

    ssdp_search = AsyncMock(side_effect=fake_ssdp_search)
    monkeypatch.setattr(provider_module, "async_search", ssdp_search)
    return ssdp_search


async def test_on_upnp_service_discovered_handles_media_renderer() -> None:
    """SSDP discovery should only forward valid MediaRenderer devices."""
    provider = cast("Any", DLNAPlayerProvider.__new__(DLNAPlayerProvider))
    provider.config = MagicMock()
    provider.config.get_value.return_value = False
    provider._device_discovered = AsyncMock()

    await provider.on_upnp_service_discovered(
        "ssdp:all",
        {
            "st": "urn:schemas-upnp-org:device:MediaRenderer:1",
            "usn": "uuid:renderer-123::urn:schemas-upnp-org:device:MediaRenderer:1",
            "location": "http://192.168.1.10/description.xml",
        },
    )

    provider._device_discovered.assert_awaited_once_with(
        "uuid:renderer-123",
        "http://192.168.1.10/description.xml",
    )


def _provider(attached: DLNAPlayer | None = None) -> Any:
    """
    Return a provider with a mocked server.

    :param attached: The attached protocol of the renderer the tests discover, if any.
    """
    provider = cast("Any", DLNAPlayerProvider.__new__(DLNAPlayerProvider))
    provider.lock = asyncio.Lock()
    provider._ignored_udns = set()
    provider.mass = MagicMock()
    provider.mass.players.wants_protocol.return_value = True
    provider.logger = MagicMock()
    provider.get_protocol = MagicMock(return_value=attached)
    return provider


async def test_a_renderer_without_an_attached_protocol_is_set_up() -> None:
    """A renderer found again after its player was removed or enabled is set up again."""
    provider = _provider()

    with patch("music_assistant.providers.dlna.provider.DLNAPlayer") as player_class:
        player_class.return_value.setup = AsyncMock(return_value=True)
        await provider._device_discovered(UDN, DESCRIPTION_URL)

    player_class.assert_called_once_with(
        provider=provider, protocol_id=UDN, description_url=DESCRIPTION_URL
    )
    player_class.return_value.setup.assert_awaited_once()


async def test_a_connected_renderer_is_left_alone() -> None:
    """Discovering a connected renderer again sets nothing up."""
    attached = MagicMock(spec=DLNAPlayer)
    attached.description_url = DESCRIPTION_URL
    attached.available = True
    attached.setup = AsyncMock()
    provider = _provider(attached)

    await provider._device_discovered(UDN, DESCRIPTION_URL)

    attached.setup.assert_not_awaited()


async def test_a_renderer_the_server_does_not_want_is_not_set_up() -> None:
    """Nothing connects to a renderer of a disabled player."""
    provider = _provider()
    provider.mass.players.wants_protocol.return_value = False

    with patch("music_assistant.providers.dlna.provider.DLNAPlayer") as player_class:
        await provider._device_discovered(UDN, DESCRIPTION_URL)

    provider.mass.players.wants_protocol.assert_called_once_with(UDN)
    player_class.assert_not_called()


@pytest.mark.parametrize(
    ("addresses", "valid"),
    [
        ([], True),
        (["192.0.2.10"], True),
        ([" 192.0.2.10 ", ""], True),
        (["http://192.0.2.10:8080/description.xml"], True),
        (["https://renderer.local/desc.xml"], True),
        (["192.0.2.10", "http://192.0.2.11/description.xml"], True),
        (["#4323r2$2"], False),
        (["192.0.2.300"], False),
        (["renderer.local"], False),
        (["http://"], False),
        (["ftp://192.0.2.10/description.xml"], False),
        (["192.0.2.10", "invalid"], False),
        ("192.0.2.10", False),
    ],
)
def test_validate_manual_addresses(addresses: Any, valid: bool) -> None:
    """Only IPv4 addresses and http(s) description URLs are accepted."""
    assert _validate_manual_addresses(addresses) is valid


async def test_manual_addresses_config_rejects_invalid_value() -> None:
    """Saving an invalid manual address is refused by the config entry itself."""
    provider = cast("Any", DLNAPlayerProvider.__new__(DLNAPlayerProvider))
    (config_entry,) = await provider.get_config_entries()

    assert config_entry.parse_value(["192.0.2.10"]) == ["192.0.2.10"]
    with pytest.raises(InvalidDataError):
        config_entry.parse_value(["#4323r2$2"])
    # The shared entry used by other providers is left untouched
    assert CONF_ENTRY_MANUAL_DISCOVERY_IPS.validate is None


async def test_no_manual_addresses_skips_search_and_rescan(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without manual addresses, no unicast search is sent and no rescan is scheduled."""
    ssdp_search = _mock_ssdp_search(monkeypatch, [])
    provider = _provider_with_manual_addresses([])

    await provider.discover_players()

    ssdp_search.assert_not_awaited()
    provider.mass.call_later.assert_not_called()


async def test_manual_ip_address_sends_unicast_search(monkeypatch: pytest.MonkeyPatch) -> None:
    """An IP address is found with a unicast SSDP search to that address."""
    ssdp_search = _mock_ssdp_search(monkeypatch, [RENDERER_RESPONSE])
    provider = _provider_with_manual_addresses([f" {IP_ADDRESS} ", ""])

    await provider.discover_players()

    ssdp_search.assert_awaited_once()
    assert ssdp_search.call_args.kwargs["target"] == (IP_ADDRESS, 1900)
    assert ssdp_search.call_args.kwargs["search_target"] == MEDIA_RENDERER
    provider._device_discovered.assert_awaited_once_with(UDN, DESCRIPTION_URL)

    provider.mass.call_later.assert_called_once()
    assert provider.mass.call_later.call_args.kwargs["task_id"] == "dlna_manual_rescan_dlna"


async def test_manual_ip_address_skips_search_when_player_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No search is sent for a manual IP address whose player is already available."""
    ssdp_search = _mock_ssdp_search(monkeypatch, [RENDERER_RESPONSE])
    provider = _provider_with_manual_addresses([IP_ADDRESS])
    provider._manual_udns[IP_ADDRESS] = UDN
    provider.get_protocol.return_value = MagicMock(available=True)

    await provider.discover_players()

    ssdp_search.assert_not_awaited()
    provider._device_discovered.assert_not_awaited()


async def test_manual_ip_address_ignores_non_renderer_responses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Responses that are not from a MediaRenderer are ignored."""
    _mock_ssdp_search(monkeypatch, [{**RENDERER_RESPONSE, "st": "upnp:rootdevice"}])
    provider = _provider_with_manual_addresses([IP_ADDRESS])

    await provider.discover_players()

    provider._device_discovered.assert_not_awaited()


async def test_manual_ip_address_survives_search_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failing unicast search is logged and retried on the next rescan."""
    ssdp_search = _mock_ssdp_search(monkeypatch, [])
    ssdp_search.side_effect = OSError("network unreachable")
    provider = _provider_with_manual_addresses([IP_ADDRESS])

    await provider.discover_players()

    provider._device_discovered.assert_not_awaited()
    provider.mass.call_later.assert_called_once()


async def test_manual_description_url_connects_device_directly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A description URL is set up directly, without an SSDP search."""
    ssdp_search = _mock_ssdp_search(monkeypatch, [])
    provider = _provider_with_manual_addresses([DESCRIPTION_URL])

    await provider.discover_players()

    ssdp_search.assert_not_awaited()
    provider.upnp_factory.async_create_device.assert_awaited_once_with(DESCRIPTION_URL)
    provider._device_discovered.assert_awaited_once_with(UDN, DESCRIPTION_URL)


async def test_manual_description_url_caches_udn() -> None:
    """The description URL is only fetched once to learn the udn."""
    provider = _provider_with_manual_addresses([DESCRIPTION_URL])

    await provider.discover_players()
    await provider.discover_players()

    provider.upnp_factory.async_create_device.assert_awaited_once_with(DESCRIPTION_URL)
    assert provider._device_discovered.await_count == 2


async def test_manual_description_url_survives_unreachable_device() -> None:
    """An offline manual description URL is skipped and retried on the next rescan."""
    provider = _provider_with_manual_addresses([DESCRIPTION_URL])
    provider.upnp_factory.async_create_device.side_effect = UpnpConnectionError("offline")

    await provider.discover_players()

    provider._device_discovered.assert_not_awaited()
    assert provider._manual_udns == {}
    provider.mass.call_later.assert_called_once()


async def test_unexpected_error_keeps_rescanning() -> None:
    """An unexpected error for one device does not stop the periodic rescan."""
    provider = _provider_with_manual_addresses([DESCRIPTION_URL])
    provider._device_discovered.side_effect = RuntimeError("boom")

    await provider.discover_players()

    provider.logger.warning.assert_called_once()
    provider.mass.call_later.assert_called_once()


async def test_unload_cancels_pending_and_running_rescan() -> None:
    """Unloading stops both a scheduled and an already started rescan."""
    provider = _provider_with_manual_addresses([DESCRIPTION_URL])

    await provider.unload()

    provider.mass.cancel_timer.assert_called_once_with("dlna_manual_rescan_dlna")
    provider.mass.cancel_task.assert_called_once_with("dlna_manual_rescan_dlna")
