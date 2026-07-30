"""Tests for music_assistant.helpers.util helpers."""

import asyncio
import socket
import time
from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest

from music_assistant.helpers import util
from music_assistant.helpers.util import (
    get_source_ip_for_target,
    import_module_in_thread,
    is_port_in_use,
    load_provider_module,
    sanitize_http_header_value,
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

    @pytest.mark.asyncio
    async def test_host_is_passed_to_port_probe(self) -> None:
        """A bind address is forwarded to the availability probe."""
        with patch("music_assistant.helpers.util.is_port_in_use", return_value=False) as probe:
            port = await select_free_port(38800, 38900, host="127.0.0.1")
        probe.assert_awaited_once_with(port, host="127.0.0.1")


class TestIsPortInUse:
    """is_port_in_use can probe the exact address a server will bind."""

    @pytest.mark.asyncio
    async def test_bound_loopback_port_is_in_use(self) -> None:
        """An active IPv4 loopback listener is detected on that same address."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen(1)
            assert await is_port_in_use(sock.getsockname()[1], host="127.0.0.1")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("host", "family"),
        [("127.0.0.1", socket.AF_INET), ("::1", socket.AF_INET6)],
    )
    async def test_host_selects_matching_address_family(
        self, host: str, family: socket.AddressFamily
    ) -> None:
        """A specific bind address is probed with its matching socket family."""
        with patch("music_assistant.helpers.util.socket.socket") as socket_mock:
            await is_port_in_use(38800, host=host)
        socket_mock.assert_called_once_with(family, socket.SOCK_STREAM)
        socket_mock.return_value.__enter__.return_value.bind.assert_called_once_with((host, 38800))


class TestImportModuleInThread:
    """import_module_in_thread imports off the event loop, one import at a time."""

    @pytest.mark.asyncio
    async def test_module_is_returned(self) -> None:
        """A relative module name is resolved against the given package."""
        assert await import_module_in_thread(".util", "music_assistant.helpers") is util

    @pytest.mark.asyncio
    async def test_concurrent_imports_are_serialized(self) -> None:
        """Concurrent calls never have two imports in flight (which can deadlock)."""
        in_flight = 0
        max_in_flight = 0

        def _slow_import(*_args: object) -> MagicMock:
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            time.sleep(0.05)
            in_flight -= 1
            return MagicMock()

        with patch("music_assistant.helpers.util.importlib.import_module", _slow_import):
            await asyncio.gather(*(import_module_in_thread(f"module_{idx}") for idx in range(5)))

        assert max_in_flight == 1


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


class TestGetIpAddresses:
    """get_ip_addresses caches the (expensive) adapter enumeration for a short while."""

    @pytest.fixture(autouse=True)
    def _clean_cache(self) -> Iterator[None]:
        """Run every test against an empty module-level cache."""
        util._ip_addresses_cache.clear()
        util._ip_addresses_pending.clear()
        yield
        util._ip_addresses_cache.clear()
        util._ip_addresses_pending.clear()

    @pytest.fixture
    def enumerate_mock(self) -> Iterator[MagicMock]:
        """Replace the blocking adapter enumeration with a counting fake."""
        with patch(
            "music_assistant.helpers.util._enumerate_ip_addresses",
            return_value=("192.168.1.10",),
        ) as mock:
            yield mock

    def test_falls_back_to_loopback_without_routable_addresses(self) -> None:
        """With no routable addresses at all, loopback is returned instead of an empty tuple."""
        with patch("music_assistant.helpers.util.ifaddr.get_adapters", return_value=[]):
            assert util._enumerate_ip_addresses(include_ipv6=True) == ("127.0.0.1",)

    @pytest.mark.asyncio
    async def test_concurrent_callers_share_a_single_probe(self, enumerate_mock: MagicMock) -> None:
        """Concurrent callers within the TTL all get the result of one enumeration."""
        results = await asyncio.gather(*(util.get_ip_addresses() for _ in range(10)))
        assert all(result == ("192.168.1.10",) for result in results)
        assert enumerate_mock.call_count == 1

    @pytest.mark.asyncio
    async def test_sequential_calls_within_ttl_reuse_the_cache(
        self, enumerate_mock: MagicMock
    ) -> None:
        """A repeated call shortly after the first is served from the cache."""
        assert await util.get_ip_addresses() == ("192.168.1.10",)
        assert await util.get_ip_addresses() == ("192.168.1.10",)
        assert enumerate_mock.call_count == 1

    @pytest.mark.asyncio
    async def test_cache_is_kept_per_ipv6_flag(self, enumerate_mock: MagicMock) -> None:
        """include_ipv6 True/False are distinct probes (each cached separately)."""
        await util.get_ip_addresses(include_ipv6=False)
        await util.get_ip_addresses(include_ipv6=True)
        await util.get_ip_addresses(include_ipv6=True)
        assert enumerate_mock.call_count == 2

    @pytest.mark.asyncio
    async def test_expired_cache_triggers_a_new_probe(self, enumerate_mock: MagicMock) -> None:
        """Once the TTL passed, the next call enumerates the adapters again."""
        await util.get_ip_addresses()
        # age the cached entry beyond the TTL
        cached_at, addresses = util._ip_addresses_cache[False]
        util._ip_addresses_cache[False] = (
            cached_at - util.IP_ADDRESSES_CACHE_TTL - 1,
            addresses,
        )
        await util.get_ip_addresses()
        assert enumerate_mock.call_count == 2

    @pytest.mark.asyncio
    async def test_cancelled_caller_does_not_break_concurrent_callers(self) -> None:
        """Cancelling one caller must not cancel the shared probe for the others."""

        def slow_enumerate(_include_ipv6: bool) -> tuple[str, ...]:
            time.sleep(0.1)
            return ("192.168.1.10",)

        with patch(
            "music_assistant.helpers.util._enumerate_ip_addresses",
            side_effect=slow_enumerate,
        ) as enumerate_mock:
            task_a = asyncio.create_task(util.get_ip_addresses())
            task_b = asyncio.create_task(util.get_ip_addresses())
            # let both callers await the (same) in-flight probe, then cancel one
            await asyncio.sleep(0.02)
            task_a.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task_a
            assert await task_b == ("192.168.1.10",)
        assert enumerate_mock.call_count == 1


class TestSanitizeHttpHeaderValue:
    """sanitize_http_header_value strips characters aiohttp forbids in response headers."""

    def test_clean_value_unchanged(self) -> None:
        """A regular track name passes through untouched."""
        assert sanitize_http_header_value("AC/DC - Thunderstruck") == "AC/DC - Thunderstruck"

    def test_newline_and_carriage_return_replaced(self) -> None:
        """CR/LF (the classic header injection vector) are replaced with spaces."""
        assert sanitize_http_header_value("Artist -\r\nEvil: header") == "Artist -  Evil: header"

    def test_all_c0_control_chars_and_del_replaced(self) -> None:
        r"""
        Every char aiohttp's _FORBIDDEN_HEADER_CHARS_RE rejects is replaced.

        Regression test for https://github.com/music-assistant/support/issues/5791
        where a control char (other than \n, \r, \t) in a FLAC tag crashed
        serve_queue_item_stream with a 500.
        """
        for codepoint in [*range(0x20), 0x7F]:
            value = f"Artist - Some{chr(codepoint)}Track"
            sanitized = sanitize_http_header_value(value)
            assert sanitized == "Artist - Some Track", f"codepoint {codepoint:#04x} not replaced"

    def test_non_ascii_preserved(self) -> None:
        """Non-ASCII text is allowed in headers and must be preserved."""
        assert sanitize_http_header_value("Björk - Jóga") == "Björk - Jóga"

    def test_leading_trailing_whitespace_stripped(self) -> None:
        """Control chars at the edges don't leave dangling whitespace."""
        assert sanitize_http_header_value("\x00Artist - Track\x1f") == "Artist - Track"
