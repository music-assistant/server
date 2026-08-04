"""Tests for the network addressing helpers on the streams controller."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from music_assistant.controllers.streams.controller import StreamsController

DEVICE_IP = "192.168.1.50"
DEVICE_IP_V6 = "fd00::50"


def _streams_controller(
    bind_ip: str = "0.0.0.0",
    configured_publish_ip: str | None = None,
) -> StreamsController:
    """Build a streams controller with only its addressing state populated."""
    mass = MagicMock()
    mass.config.get_raw_core_config_value.return_value = "GLOBAL"
    controller = StreamsController(mass)
    controller._bind_ip = bind_ip
    controller._configured_publish_ip = configured_publish_ip
    return controller


class TestGetSourceIp:
    """get_source_ip returns a local, bindable address on the player-facing network."""

    @pytest.mark.asyncio
    async def test_concrete_bind_ip_wins_for_every_device(self) -> None:
        """An operator pin narrows the whole server, so no routing lookup is needed."""
        controller = _streams_controller(bind_ip="192.168.1.5")
        with patch(
            "music_assistant.controllers.streams.controller.get_source_ip_for_target",
            new=AsyncMock(),
        ) as routing_lookup:
            assert await controller.get_source_ip(DEVICE_IP) == "192.168.1.5"
        routing_lookup.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_concrete_bind_ip_rejected_for_other_ip_family(self) -> None:
        """A bind IP of the wrong family cannot carry traffic to the device."""
        controller = _streams_controller(bind_ip="192.168.1.5")
        assert await controller.get_source_ip(DEVICE_IP_V6) is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("bind_ip", "device_ip", "source_ip"),
        [("0.0.0.0", DEVICE_IP, "192.168.1.5"), ("::", DEVICE_IP_V6, "fd00::5")],
    )
    async def test_wildcard_bind_ip_resolves_per_device_route(
        self, bind_ip: str, device_ip: str, source_ip: str
    ) -> None:
        """
        Without an operator pin, the interface that routes to the device is used.

        Every wildcard bind IP takes this path, not just the IPv4 one: a wildcard is not
        itself a bindable address, so handing it to a caller would be a broken result.

        :param bind_ip: Wildcard bind IP the streamserver is bound to.
        :param device_ip: IP address of the device the traffic is meant for.
        :param source_ip: Local address the per-device route lookup resolves to.
        """
        controller = _streams_controller(bind_ip=bind_ip)
        with patch(
            "music_assistant.controllers.streams.controller.get_source_ip_for_target",
            new=AsyncMock(return_value=source_ip),
        ) as routing_lookup:
            assert await controller.get_source_ip(device_ip) == source_ip
        routing_lookup.assert_awaited_once_with(device_ip)

    @pytest.mark.asyncio
    async def test_unroutable_target_pins_nothing(self) -> None:
        """An inconclusive routing lookup leaves the choice to the routing table."""
        controller = _streams_controller(bind_ip="0.0.0.0")
        with patch(
            "music_assistant.controllers.streams.controller.get_source_ip_for_target",
            new=AsyncMock(return_value=""),
        ):
            assert await controller.get_source_ip(DEVICE_IP) is None

    @pytest.mark.asyncio
    async def test_shared_consumer_without_target_pins_nothing(self) -> None:
        """A caller serving every player cannot be narrowed without an operator pin."""
        controller = _streams_controller(bind_ip="0.0.0.0")
        assert await controller.get_source_ip() is None

    @pytest.mark.asyncio
    async def test_shared_consumer_honours_concrete_bind_ip(self) -> None:
        """An operator pin still applies to a caller that serves every player."""
        controller = _streams_controller(bind_ip="192.168.1.5")
        assert await controller.get_source_ip() == "192.168.1.5"


class TestGetPublishIp:
    """get_publish_ip only hands out an address the user actually configured."""

    def test_configured_publish_ip_is_returned(self) -> None:
        """An explicit publish IP is a reachability statement and is honoured verbatim."""
        controller = _streams_controller(configured_publish_ip="10.45.0.20")
        assert controller.get_publish_ip(DEVICE_IP) == "10.45.0.20"

    def test_auto_detected_publish_ip_is_not_returned(self) -> None:
        """An auto-detected publish IP is only a guess and must never be advertised."""
        controller = _streams_controller()
        controller.publish_ip = "10.45.0.20"
        assert controller.get_publish_ip(DEVICE_IP) is None

    def test_configured_publish_ip_of_other_ip_family_is_rejected(self) -> None:
        """An address the device cannot even parse is worse than none at all."""
        controller = _streams_controller(configured_publish_ip="10.45.0.20")
        assert controller.get_publish_ip(DEVICE_IP_V6) is None


@pytest.mark.asyncio
async def test_diagnostics_report_whether_publish_ip_is_configured() -> None:
    """Diagnostics distinguish a configured publish IP from an auto-detected one."""
    assert (await _streams_controller().get_diagnostics())["publish_ip_configured"] is False
    controller = _streams_controller(configured_publish_ip="10.45.0.20")
    assert (await controller.get_diagnostics())["publish_ip_configured"] is True
