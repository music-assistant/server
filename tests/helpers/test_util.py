"""Tests for music_assistant.helpers.util helpers."""

import asyncio
from collections.abc import Iterator
from unittest.mock import patch

import pytest

from music_assistant.helpers import util
from music_assistant.helpers.util import (
    get_source_ip_for_target,
    load_provider_module,
    select_free_port,
)


class TestGetSourceIpForTarget:
    """get_source_ip_for_target resolves a bindable, device-reachable source IP."""

    @pytest.mark.asyncio
    async def test_prefers_explicit_bind_ip(self) -> None:
        """A concrete (non-wildcard) bind_ip is honoured as-is."""
        result = await get_source_ip_for_target(
            "10.10.20.31", bind_ip="10.0.0.5", publish_ip="10.45.0.20"
        )
        assert result == "10.0.0.5"

    @pytest.mark.asyncio
    async def test_uses_routing_lookup_when_bind_ip_wildcard(self) -> None:
        """With a wildcard bind_ip, the per-device routing lookup result wins."""
        with patch("music_assistant.helpers.util.socket.socket") as mock_socket:
            sock = mock_socket.return_value.__enter__.return_value
            sock.getsockname.return_value = ("10.10.20.106", 0)
            result = await get_source_ip_for_target(
                "10.10.20.31", bind_ip="0.0.0.0", publish_ip="10.45.0.20"
            )
        assert result == "10.10.20.106"

    @pytest.mark.asyncio
    async def test_falls_back_to_publish_ip_on_lookup_failure(self) -> None:
        """If the routing lookup cannot resolve, fall back to publish_ip."""
        with patch("music_assistant.helpers.util.socket.socket") as mock_socket:
            sock = mock_socket.return_value.__enter__.return_value
            sock.connect.side_effect = OSError("no route to host")
            result = await get_source_ip_for_target(
                "10.10.20.31", bind_ip="0.0.0.0", publish_ip="10.45.0.20"
            )
        assert result == "10.45.0.20"

    @pytest.mark.asyncio
    async def test_falls_back_to_bind_ip_when_no_publish_ip(self) -> None:
        """With no publish_ip and an inconclusive lookup, return the bind_ip as last resort."""
        with patch("music_assistant.helpers.util.socket.socket") as mock_socket:
            sock = mock_socket.return_value.__enter__.return_value
            sock.connect.side_effect = OSError("no route to host")
            result = await get_source_ip_for_target("10.10.20.31", bind_ip="0.0.0.0", publish_ip="")
        assert result == "0.0.0.0"


class TestSelectFreePort:
    """select_free_port hands out distinct ports even under concurrent calls."""

    @pytest.fixture(autouse=True)
    def _clear_reservations(self) -> Iterator[None]:
        """Ensure no port reservation state leaks between tests."""
        util._reserved_ports.clear()
        yield
        util._reserved_ports.clear()

    @pytest.mark.asyncio
    async def test_concurrent_calls_get_distinct_ports(self) -> None:
        """Instances starting simultaneously must not be handed the same port."""
        # All ports report free, mimicking instances that haven't bound yet.
        with patch("music_assistant.helpers.util.is_port_in_use", return_value=False):
            ports = await asyncio.gather(*(select_free_port(38800, 38900) for _ in range(5)))
        assert len(set(ports)) == len(ports)

    @pytest.mark.asyncio
    async def test_expired_reservation_is_reusable(self) -> None:
        """A reservation past its TTL is released so the port can be handed out again."""
        with patch("music_assistant.helpers.util.is_port_in_use", return_value=False):
            first = await select_free_port(38800, 38900)
            # force the reservation to look expired so the next call can reuse it
            util._reserved_ports[first] = 0.0
            second = await select_free_port(38800, 38900)
        assert first == second


class TestLoadProviderModule:
    """load_provider_module verifies pinned requirements before importing the provider."""

    @pytest.fixture(autouse=True)
    def _clear_checked_requirements(self) -> Iterator[None]:
        """Ensure no requirement-check state leaks between tests."""
        util._checked_requirements.clear()
        yield
        util._checked_requirements.clear()

    @pytest.mark.asyncio
    async def test_requirement_with_extras_not_reinstalled(self) -> None:
        """A requirement with extras is version-checked on the bare package name."""
        with (
            patch(
                "music_assistant.helpers.util.get_package_version", return_value="6.1.1"
            ) as version_mock,
            patch("music_assistant.helpers.util.install_package") as install_mock,
            patch("music_assistant.helpers.util.importlib.import_module"),
        ):
            await load_provider_module("sendspin", ["aiosendspin[server]==6.1.1"])
        version_mock.assert_awaited_once_with("aiosendspin")
        install_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_outdated_requirement_installed_with_extras_preserved(self) -> None:
        """An outdated requirement is (re)installed with the full requirement string."""
        with (
            patch("music_assistant.helpers.util.get_package_version", return_value="6.0.0"),
            patch("music_assistant.helpers.util.install_package") as install_mock,
            patch("music_assistant.helpers.util.importlib.import_module"),
        ):
            await load_provider_module("sendspin", ["aiosendspin[server]==6.1.1"])
        install_mock.assert_awaited_once_with("aiosendspin[server]==6.1.1")

    @pytest.mark.asyncio
    async def test_requirement_checked_only_once(self) -> None:
        """Repeated loads of the same provider don't re-run the version check."""
        with (
            patch(
                "music_assistant.helpers.util.get_package_version", return_value="6.1.1"
            ) as version_mock,
            patch("music_assistant.helpers.util.install_package") as install_mock,
            patch("music_assistant.helpers.util.importlib.import_module"),
        ):
            await load_provider_module("sendspin", ["aiosendspin[server]==6.1.1"])
            await load_provider_module("sendspin", ["aiosendspin[server]==6.1.1"])
        version_mock.assert_awaited_once()
        install_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_failed_install_is_retried_on_next_load(self) -> None:
        """A failed install is not marked as checked, so the next load retries it."""
        with (
            patch("music_assistant.helpers.util.get_package_version", return_value=None),
            patch(
                "music_assistant.helpers.util.install_package",
                side_effect=RuntimeError("install failed"),
            ) as install_mock,
            patch("music_assistant.helpers.util.importlib.import_module"),
        ):
            with pytest.raises(RuntimeError):
                await load_provider_module("sendspin", ["aiosendspin[server]==6.1.1"])
            install_mock.side_effect = None
            await load_provider_module("sendspin", ["aiosendspin[server]==6.1.1"])
        assert install_mock.await_count == 2
