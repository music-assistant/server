"""Tests for the Home Assistant lookups behind the Hue Lights Sync ZHA backend."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from music_assistant.providers.hue_entertainment import zigbee
from music_assistant.providers.hue_entertainment.zigbee import (
    ZhaDevice,
    ZhaLight,
    ZhaUnavailableError,
    hue_lights,
    stream_info,
    zha_available,
)

from .fakes import ZIGGURAT_URL, FakeHass, FakeMass, zha_device


async def _url(mass: Any) -> str:
    url, _devices = await stream_info(mass)
    return url


async def test_ziggurat_url_kept_when_its_host_resolves() -> None:
    """A Ziggurat host name that resolves is used as ZHA reports it."""
    mass: Any = FakeMass(FakeHass())

    assert await _url(mass) == ZIGGURAT_URL
    mass.loop.getaddrinfo.assert_awaited_once_with("3ccd9485-ziggurat", None)


async def test_ziggurat_url_moves_to_the_internal_url_host() -> None:
    """An unresolvable add-on host name is replaced by the host of HA's internal URL."""
    mass: Any = FakeMass(FakeHass(), resolves=False)

    assert await _url(mass) == "ws://192.168.1.10:9999"


async def test_ziggurat_url_falls_back_to_the_plugin_url_host() -> None:
    """Without an internal URL, the host the plugin connects to is used."""
    mass: Any = FakeMass(FakeHass(internal_url=None), resolves=False)

    assert await _url(mass) == "ws://homeassistant.lan:9999"


async def test_ziggurat_url_falls_back_when_network_url_fails() -> None:
    """A failing ``network/url`` command also falls back to the plugin URL host."""
    hass = FakeHass()
    hass.commands["network/url"] = RuntimeError("unauthorized")
    mass: Any = FakeMass(hass, resolves=False)

    assert await _url(mass) == "ws://homeassistant.lan:9999"


async def test_ziggurat_url_brackets_an_ipv6_host() -> None:
    """An IPv6 internal URL host is bracketed in the Ziggurat URL."""
    mass: Any = FakeMass(FakeHass(internal_url="http://[fd00::1]:8123"), resolves=False)

    assert await _url(mass) == "ws://[fd00::1]:9999"


async def test_ziggurat_url_falls_back_when_resolving_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host name lookup that hangs counts as not resolving."""
    monkeypatch.setattr(zigbee, "_RESOLVE_TIMEOUT_S", 0.01)

    async def _hang(*_args: Any) -> list[Any]:
        await asyncio.sleep(10)
        return []

    mass: Any = FakeMass(FakeHass())
    mass.loop.getaddrinfo.side_effect = _hang

    assert await _url(mass) == "ws://192.168.1.10:9999"


async def test_ziggurat_url_kept_as_addon() -> None:
    """An add-on resolves Ziggurat's add-on host name, so the URL is used without a lookup."""
    mass: Any = FakeMass(FakeHass(), addon=True, resolves=False)

    assert await _url(mass) == ZIGGURAT_URL
    mass.loop.getaddrinfo.assert_not_awaited()


async def test_stream_info_returns_address_and_name_per_ieee() -> None:
    """The network address (number or hex string) and display name are returned per IEEE."""
    hass = FakeHass(
        [
            zha_device("00:17:88:01:0C:BF:17:C8", "Signe", nwk="0x1A2B", user_given_name="Desk"),
            zha_device("00:17:88:01:0a:00:00:02", "Hue go", nwk=0x3C4D),
            zha_device("00:17:88:01:0a:00:00:03", "Broken", nwk="not-hex"),
        ]
    )
    mass: Any = FakeMass(hass)

    _url, devices = await stream_info(mass)

    assert devices == {
        "00:17:88:01:0c:bf:17:c8": ZhaDevice(0x1A2B, "Desk"),
        "00:17:88:01:0a:00:00:02": ZhaDevice(0x3C4D, "Hue go"),
    }


@pytest.mark.parametrize(
    ("mass", "reason"),
    [
        (FakeMass(None), "not connected"),
        (FakeMass(FakeHass(connected=False)), "not connected"),
        (FakeMass(FakeHass(ziggurat_path="/dev/ttyUSB0")), "Ziggurat"),
    ],
    ids=["no_hass", "hass_disconnected", "other_radio"],
)
async def test_stream_info_needs_hass_with_zha_on_ziggurat(mass: Any, reason: str) -> None:
    """Without a connected Home Assistant plugin, or with ZHA on another radio, it raises."""
    with pytest.raises(ZhaUnavailableError, match=reason):
        await stream_info(mass)


@pytest.mark.parametrize(
    ("mass", "available"),
    [
        (FakeMass(FakeHass()), True),
        (FakeMass(FakeHass(ziggurat_path="wss://ziggurat.lan:9999")), True),
        (FakeMass(None), False),
        (FakeMass(FakeHass(connected=False)), False),
        (FakeMass(FakeHass(ziggurat_path="/dev/ttyUSB0")), False),
    ],
    ids=["ws", "wss", "no_hass", "hass_disconnected", "other_radio"],
)
async def test_zha_available(mass: Any, available: bool) -> None:
    """ZHA is available with a connected Home Assistant plugin and a websocket radio path."""
    assert await zha_available(mass) is available


async def test_zha_unavailable_when_zha_does_not_answer() -> None:
    """An error from Home Assistant (e.g. ZHA not set up) counts as not available."""
    hass = FakeHass()
    hass.commands["zha/network/settings"] = RuntimeError("Unknown command")

    mass: Any = FakeMass(hass)

    assert await zha_available(mass) is False


async def test_hue_lights_keeps_only_entertainment_lamps() -> None:
    """Only Hue lamps with the entertainment cluster on endpoint 11 and a light entity are kept."""
    hass: Any = FakeHass(
        [
            zha_device("aa", "Signe", nwk="0x0001", area_id="living"),
            zha_device("bb", "Go", nwk=2, manufacturer="Philips", area_id="kitchen"),
            zha_device("cc", "White ambiance", clusters=("0x0000", "0x0006")),
            zha_device("dd", "Wrong endpoint", endpoint="242"),
            zha_device("ee", "IKEA bulb", manufacturer="IKEA of Sweden"),
            zha_device("ff", "Hue switch", entities=("sensor.hue_switch_battery",)),
            zha_device("gg", "No address", nwk="zz"),
            zha_device("hh", "Bloom", user_given_name="Bedside", area_id="kitchen"),
        ],
        areas={"living": "Living room", "kitchen": "Kitchen"},
    )

    lights = await hue_lights(hass)

    assert lights == [
        ZhaLight("hh", "Bedside", "Kitchen"),
        ZhaLight("bb", "Go", "Kitchen"),
        ZhaLight("aa", "Signe", "Living room"),
    ]
