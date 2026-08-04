"""Tests for music_assistant.helpers.util helpers."""

import asyncio
import socket
import time
from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest
from music_assistant_models.media_items import Album, ProviderMapping, Track

from music_assistant.helpers import util
from music_assistant.helpers.util import (
    get_source_ip_for_target,
    guard_single_request,
    import_module_in_thread,
    is_port_in_use,
    load_provider_module,
    sanitize_http_header_value,
    select_announce_addresses,
    select_free_port,
)
from music_assistant.mass import MusicAssistant
from music_assistant.models.music_provider import MusicProvider

GUARDED_PROVIDER_ID = "test_guarded_prov"


class TestGetSourceIpForTarget:
    """get_source_ip_for_target reports the interface the routing table egresses from."""

    @pytest.mark.asyncio
    async def test_returns_routing_lookup_result(self) -> None:
        """The address the kernel picks for the target is handed back as-is."""
        with patch("music_assistant.helpers.util.socket.socket") as mock_socket:
            sock = mock_socket.return_value.__enter__.return_value
            sock.getsockname.return_value = ("10.10.20.106", 0)
            result = await get_source_ip_for_target("10.10.20.31")
        assert result == "10.10.20.106"

    @pytest.mark.asyncio
    async def test_returns_empty_string_when_unroutable(self) -> None:
        """A target with no route resolves to nothing rather than a guess."""
        with patch("music_assistant.helpers.util.socket.socket") as mock_socket:
            sock = mock_socket.return_value.__enter__.return_value
            sock.connect.side_effect = OSError("no route to host")
            result = await get_source_ip_for_target("10.10.20.31")
        assert result == ""


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

    @pytest.mark.asyncio
    async def test_module_lock_collision_is_retried_once(self) -> None:
        """A deadlock reported by the import machinery is retried, not passed on."""
        module = MagicMock()
        attempts = 0

        def _import(*_args: object) -> MagicMock:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("deadlock detected by _ModuleLock('requests.structures')")
            return module

        with patch("music_assistant.helpers.util.importlib.import_module", _import):
            assert await import_module_in_thread("some_module") is module
        assert attempts == 2

    @pytest.mark.asyncio
    async def test_other_runtime_errors_are_not_retried(self) -> None:
        """An unrelated RuntimeError from the module body is passed on as-is."""
        with (
            patch(
                "music_assistant.helpers.util.importlib.import_module",
                side_effect=RuntimeError("boom"),
            ) as import_mock,
            pytest.raises(RuntimeError, match="boom"),
        ):
            await import_module_in_thread("some_module")
        assert import_mock.call_count == 1


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


class TestSelectAnnounceAddresses:
    """select_announce_addresses picks the addresses that are safe to announce."""

    def test_ipv6_addresses_are_left_out(self) -> None:
        """A dual-stack host announces its IPv4 addresses only, in their original order."""
        assert select_announce_addresses(
            ["192.168.1.10", "fd00::10", "10.0.0.5", "2001:db8::1"]
        ) == ["192.168.1.10", "10.0.0.5"]

    def test_ipv6_only_host_announces_ipv6(self) -> None:
        """A host without any IPv4 address falls back to announcing its IPv6 addresses."""
        assert select_announce_addresses(["fd00::10", "2001:db8::1"]) == [
            "fd00::10",
            "2001:db8::1",
        ]

    def test_ipv4_only_host_is_unchanged(self) -> None:
        """The common IPv4-only host announces exactly what it publishes."""
        assert select_announce_addresses(["192.168.1.10"]) == ["192.168.1.10"]

    def test_empty_input_yields_no_addresses(self) -> None:
        """Nothing to publish means nothing to announce."""
        assert select_announce_addresses([]) == []


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


class TestGuardSingleRequest:
    """guard_single_request collapses identical concurrent calls into a single request."""

    @pytest.mark.asyncio
    async def test_cancelled_caller_does_not_affect_others(
        self, mass_minimal: MusicAssistant
    ) -> None:
        """A caller giving up must not cancel the shared request for the other callers."""
        caller = _GuardedCaller(mass_minimal)
        calls = [asyncio.create_task(caller.fetch("123")) for _ in range(3)]
        # give the callers a chance to line up behind the same in-flight request
        await asyncio.sleep(0)
        assert caller.calls == 1

        calls[0].cancel()
        caller.release.set()
        results = await asyncio.gather(*calls, return_exceptions=True)

        assert isinstance(results[0], asyncio.CancelledError)
        assert results[1:] == ["result-123", "result-123"]
        assert caller.calls == 1

    @pytest.mark.asyncio
    async def test_instances_get_their_own_request(self, mass_minimal: MusicAssistant) -> None:
        """Two objects of the same class each issue their own request."""
        first = _GuardedCaller(mass_minimal)
        second = _GuardedCaller(mass_minimal)
        calls = [
            asyncio.create_task(first.fetch("123")),
            asyncio.create_task(second.fetch("123")),
        ]
        # both requests are in flight before either is released
        await asyncio.sleep(0)
        first.release.set()
        second.release.set()

        assert await asyncio.gather(*calls) == ["result-123", "result-123"]
        assert first.calls == 1
        assert second.calls == 1

    # mass.create_task pins its tasks to mass.loop, so the test must run on the loop the
    # class-scoped fixture was created on
    @pytest.mark.asyncio(loop_scope="class")
    async def test_controllers_sharing_a_method_get_their_own_request(
        self, music_mass_class: MusicAssistant
    ) -> None:
        """The same item id requested on two media controllers resolves per media type."""
        provider = _GatedMusicProvider()
        with patch.object(music_mass_class, "get_provider", return_value=provider):
            album_calls = [
                asyncio.create_task(
                    music_mass_class.music.albums.get_provider_item("123", GUARDED_PROVIDER_ID)
                )
                for _ in range(2)
            ]
            track_call = asyncio.create_task(
                music_mass_class.music.tracks.get_provider_item("123", GUARDED_PROVIDER_ID)
            )
            await asyncio.sleep(0)
            provider.release.set()
            first_album, second_album = await asyncio.gather(*album_calls)
            track = await track_call

        assert isinstance(first_album, Album)
        assert isinstance(track, Track)
        # the two album callers shared a single request, the track caller got its own
        assert first_album is second_album
        assert provider.album_calls == 1
        assert provider.track_calls == 1


class _GuardedCaller:
    """Minimal stand-in for a controller exposing a guarded request."""

    def __init__(self, mass: MusicAssistant) -> None:
        """
        Initialize the caller.

        :param mass: The MusicAssistant instance tracking the guarded request.
        """
        self.mass = mass
        self.calls = 0
        self.release = asyncio.Event()

    @guard_single_request
    async def fetch(self, item_id: str) -> str:
        """Return the result for the given item id, once released."""
        self.calls += 1
        await self.release.wait()
        return f"result-{item_id}"


class _GatedMusicProvider(MusicProvider):
    """Minimal music provider returning items only once released."""

    def __init__(self) -> None:
        """Initialize the provider."""
        self.release = asyncio.Event()
        self.album_calls = 0
        self.track_calls = 0
        self.config = MagicMock()
        self.config.instance_id = GUARDED_PROVIDER_ID
        self.manifest = MagicMock()
        self.manifest.domain = GUARDED_PROVIDER_ID
        self.logger = MagicMock()

    async def get_album(self, prov_album_id: str) -> Album:
        """Return the album for the given provider album id."""
        self.album_calls += 1
        await self.release.wait()
        return Album(
            item_id=prov_album_id,
            provider=GUARDED_PROVIDER_ID,
            name="Album",
            provider_mappings={_provider_mapping(prov_album_id)},
        )

    async def get_track(self, prov_track_id: str) -> Track:
        """Return the track for the given provider track id."""
        self.track_calls += 1
        await self.release.wait()
        return Track(
            item_id=prov_track_id,
            provider=GUARDED_PROVIDER_ID,
            name="Track",
            provider_mappings={_provider_mapping(prov_track_id)},
        )


def _provider_mapping(item_id: str) -> ProviderMapping:
    """
    Build a provider mapping for the gated test provider.

    :param item_id: The provider item id to map.
    """
    return ProviderMapping(
        item_id=item_id,
        provider_domain=GUARDED_PROVIDER_ID,
        provider_instance=GUARDED_PROVIDER_ID,
    )
