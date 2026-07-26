"""Tests for utility/helper functions."""

import os
import signal
import subprocess
import sys
from ipaddress import IPv4Address, IPv6Address
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import (
    MusicAssistantError,
    SetupFailedError,
    UnsupportedSystemError,
)
from yarl import URL
from zeroconf import InterfaceChoice, IPVersion

from music_assistant.helpers import _ml_inference_probe, uri, util
from music_assistant.helpers.aiohttp_client import encoded_request_url


def test_version_extract() -> None:
    """Test the extraction of version from title."""
    test_str = "Bam Bam (feat. Ed Sheeran)"
    title, version = util.parse_title_and_version(test_str)
    assert title == "Bam Bam"
    assert version == ""
    test_str = "Bam Bam (feat. Ed Sheeran) - Karaoke Version"
    title, version = util.parse_title_and_version(test_str)
    assert title == "Bam Bam"
    assert version == "Karaoke Version"
    test_str = "Bam Bam (feat. Ed Sheeran) [Karaoke Version]"
    title, version = util.parse_title_and_version(test_str)
    assert title == "Bam Bam"
    assert version == "Karaoke Version"
    test_str = "SuperSong (2011 Remaster)"
    title, version = util.parse_title_and_version(test_str)
    assert title == "SuperSong"
    assert version == "2011 Remaster"
    test_str = "SuperSong (Live at Wembley)"
    title, version = util.parse_title_and_version(test_str)
    assert title == "SuperSong"
    assert version == "Live at Wembley"
    test_str = "SuperSong (Instrumental)"
    title, version = util.parse_title_and_version(test_str)
    assert title == "SuperSong"
    assert version == "Instrumental"
    test_str = "SuperSong (Explicit)"
    title, version = util.parse_title_and_version(test_str)
    assert title == "SuperSong"
    assert version == ""
    # Version keywords in main title should NOT be stripped (only in parentheses)
    test_str = "Great live unplugged song"
    title, version = util.parse_title_and_version(test_str)
    assert title == "Great live unplugged song"
    assert version == ""
    test_str = "I Do (featuring Sonny of P.O.D.) (Album Version)"
    title, version = util.parse_title_and_version(test_str)
    assert title == "I Do"
    assert version == "Album Version"
    test_str = "Get Up Stand Up (Phunk Investigation instrumental club mix)"
    title, version = util.parse_title_and_version(test_str)
    assert title == "Get Up Stand Up"
    assert version == "Phunk Investigation instrumental club mix"
    # Complex case: non-version part + version part with 'mix' keyword
    test_str = "Lovin' You More (That Big Track) (Mosquito Chillout mix)"
    title, version = util.parse_title_and_version(test_str)
    assert title == "Lovin' You More (That Big Track)"
    assert version == "Mosquito Chillout mix"
    # Nested parentheses inside the version should be preserved
    test_str = "Fiji (Oliver Smith Remix (Mixed))"
    title, version = util.parse_title_and_version(test_str)
    assert title == "Fiji"
    assert version == "Oliver Smith Remix (Mixed)"


def test_with_handling_in_titles() -> None:
    """Test 'with' handling - preserved in title, stripped as featuring credit."""
    # 'with you' (preserved as title word)
    test_str = "CCF (I'm Gonna Stay with You)"
    title, version = util.parse_title_and_version(test_str)
    assert title == "CCF (I'm Gonna Stay with You)"
    assert version == ""
    # 'with someone' (preserved as title word)
    test_str = "Ever Fallen in Love (With Someone You Shouldn't've)"
    title, version = util.parse_title_and_version(test_str)
    assert title == "Ever Fallen in Love (With Someone You Shouldn't've)"
    assert version == ""
    # 'with u' (preserved as title word)
    test_str = "Dance (With U)"
    title, version = util.parse_title_and_version(test_str)
    assert title == "Dance (With U)"
    assert version == ""
    # 'with the' (preserved as title word)
    test_str = "Girl (With the Patent Leather Face)"
    title, version = util.parse_title_and_version(test_str)
    assert title == "Girl (With the Patent Leather Face)"
    assert version == ""
    # 'with you' - different phrasing (preserved as title word)
    test_str = "Rockin' Around (With You)"
    title, version = util.parse_title_and_version(test_str)
    assert title == "Rockin' Around (With You)"
    assert version == ""
    # 'with no' (preserved as title word)
    test_str = "Ain't Gonna Bump No More (With No Big Fat Woman)"
    title, version = util.parse_title_and_version(test_str)
    assert title == "Ain't Gonna Bump No More (With No Big Fat Woman)"
    assert version == ""
    # 'with that' - not in WITH_TITLE_WORDS but not stripped because it doesn't start with "with "
    test_str = "The Catastrophe (Good Luck with That Man)"
    title, version = util.parse_title_and_version(test_str)
    assert title == "The Catastrophe (Good Luck with That Man)"
    assert version == ""
    # 'with [artist name]' - should still be stripped (not a title word)
    test_str = "Great Song (with John Smith)"
    title, version = util.parse_title_and_version(test_str)
    assert title == "Great Song"
    assert version == ""
    # 'with [artist name]' in brackets - should still be stripped
    test_str = "Great Song [with Jane Doe]"
    title, version = util.parse_title_and_version(test_str)
    assert title == "Great Song"
    assert version == ""
    # Title word preserved + version extracted from dash notation
    test_str = "CCF (I'm Gonna Stay with You) - Live Version"
    title, version = util.parse_title_and_version(test_str)
    assert title == "CCF (I'm Gonna Stay with You)"
    assert version == "Live Version"
    # Title word preserved + version extracted from brackets
    test_str = "Dance (With U) [Remix]"
    title, version = util.parse_title_and_version(test_str)
    assert title == "Dance (With U)"
    assert version == "Remix"


async def test_uri_parsing() -> None:
    """Test parsing of URI."""
    # test regular uri
    test_uri = "spotify://track/123456789"
    media_type, provider, item_id = await uri.parse_uri(test_uri)
    assert media_type == MediaType.TRACK
    assert provider == "spotify"
    assert item_id == "123456789"
    # test spotify uri
    test_uri = "spotify:track:123456789"
    media_type, provider, item_id = await uri.parse_uri(test_uri)
    assert media_type == MediaType.TRACK
    assert provider == "spotify"
    assert item_id == "123456789"
    # test public play/open url
    test_uri = "https://open.spotify.com/playlist/5lH9NjOeJvctAO92ZrKQNB?si=04a63c8234ac413e"
    media_type, provider, item_id = await uri.parse_uri(test_uri)
    assert media_type == MediaType.PLAYLIST
    assert provider == "spotify"
    assert item_id == "5lH9NjOeJvctAO92ZrKQNB"
    # test filename with slashes as item_id
    test_uri = "filesystem://track/Artist/Album/Track.flac"
    media_type, provider, item_id = await uri.parse_uri(test_uri)
    assert media_type == MediaType.TRACK
    assert provider == "filesystem"
    assert item_id == "Artist/Album/Track.flac"
    # test regular url to builtin provider
    test_uri = "http://radiostream.io/stream.mp3"
    media_type, provider, item_id = await uri.parse_uri(test_uri)
    assert media_type == MediaType.UNKNOWN
    assert provider == "builtin"
    assert item_id == "http://radiostream.io/stream.mp3"
    # test local file to builtin provider
    test_uri = __file__
    media_type, provider, item_id = await uri.parse_uri(test_uri)
    assert media_type == MediaType.UNKNOWN
    assert provider == "builtin"
    assert item_id == __file__
    # test invalid uri
    with pytest.raises(MusicAssistantError):
        await uri.parse_uri("invalid://blah")


async def test_apple_music_uri_parsing() -> None:
    """Test parsing of Apple Music share URLs."""
    # station — should resolve as PLAYLIST (is_dynamic)
    media_type, provider, item_id = await uri.parse_uri(
        "https://music.apple.com/de/station/dead-sara-essentials/ra.331701075"
    )
    assert media_type == MediaType.PLAYLIST
    assert provider == "apple_music"
    assert item_id == "ra.331701075"
    # playlist
    media_type, provider, item_id = await uri.parse_uri(
        "https://music.apple.com/de/playlist/disturbed-essentials/pl.5d641aa29c5d4cc49b474d7d100996ec"
    )
    assert media_type == MediaType.PLAYLIST
    assert provider == "apple_music"
    assert item_id == "pl.5d641aa29c5d4cc49b474d7d100996ec"
    # album
    media_type, provider, item_id = await uri.parse_uri(
        "https://music.apple.com/de/album/some-album/1234567890"
    )
    assert media_type == MediaType.ALBUM
    assert provider == "apple_music"
    assert item_id == "1234567890"
    # artist
    media_type, provider, item_id = await uri.parse_uri(
        "https://music.apple.com/de/artist/dead-sara/123456789"
    )
    assert media_type == MediaType.ARTIST
    assert provider == "apple_music"
    assert item_id == "123456789"
    # song
    media_type, provider, item_id = await uri.parse_uri(
        "https://music.apple.com/de/song/my-song/987654321"
    )
    assert media_type == MediaType.TRACK
    assert provider == "apple_music"
    assert item_id == "987654321"
    # trailing slash stripped
    media_type, provider, item_id = await uri.parse_uri(
        "https://music.apple.com/de/station/some-station/ra.111222333/"
    )
    assert media_type == MediaType.PLAYLIST
    assert item_id == "ra.111222333"
    # query string stripped (non-track query params)
    media_type, provider, item_id = await uri.parse_uri(
        "https://music.apple.com/de/album/some-album/1234567890?itsct=music_box"
    )
    assert media_type == MediaType.ALBUM
    assert item_id == "1234567890"
    # track share link: album URL with ?i=<track_id>
    media_type, provider, item_id = await uri.parse_uri(
        "https://music.apple.com/de/album/some-album/1234567890?i=987654321"
    )
    assert media_type == MediaType.TRACK
    assert provider == "apple_music"
    assert item_id == "987654321"
    # track share link with additional query params
    media_type, _, item_id = await uri.parse_uri(
        "https://music.apple.com/de/album/some-album/1234567890?itsct=music_box&i=111222333"
    )
    assert media_type == MediaType.TRACK
    assert item_id == "111222333"


def test_format_ip_for_url() -> None:
    """Test IPv6 bracket wrapping for URLs (RFC 2732)."""
    # IPv4 should pass through unchanged
    assert util.format_ip_for_url("192.168.1.1") == "192.168.1.1"
    assert util.format_ip_for_url("10.0.0.1") == "10.0.0.1"
    assert util.format_ip_for_url("0.0.0.0") == "0.0.0.0"
    # IPv6 should be wrapped in brackets
    assert util.format_ip_for_url("::1") == "[::1]"
    assert util.format_ip_for_url("fe80::1") == "[fe80::1]"
    assert util.format_ip_for_url("2001:db8::1") == "[2001:db8::1]"
    assert util.format_ip_for_url("fd00::cafe:1") == "[fd00::cafe:1]"


def _mock_service_info(ipv4_addrs: list[str], ipv6_addrs: list[str]) -> MagicMock:
    """Create a mock AsyncServiceInfo with ip_addresses_by_version."""
    mock_info = MagicMock()

    def ip_addresses_by_version(version: IPVersion) -> list[IPv4Address | IPv6Address]:
        if version == IPVersion.V4Only:
            return [IPv4Address(a) for a in ipv4_addrs]
        if version == IPVersion.V6Only:
            return [IPv6Address(a) for a in ipv6_addrs]
        return [IPv4Address(a) for a in ipv4_addrs] + [IPv6Address(a) for a in ipv6_addrs]

    mock_info.ip_addresses_by_version = ip_addresses_by_version
    return mock_info


def test_get_primary_ip_address_from_zeroconf_prefer_ipv4() -> None:
    """Test zeroconf IP extraction preferring IPv4 (default)."""
    mock_info = _mock_service_info(["192.168.1.100"], ["fd00::1"])
    result = util.get_primary_ip_address_from_zeroconf(mock_info, prefer_ipv6=False)
    assert result == "192.168.1.100"


def test_get_primary_ip_address_from_zeroconf_prefer_ipv6() -> None:
    """Test zeroconf IP extraction preferring IPv6."""
    mock_info = _mock_service_info(["192.168.1.100"], ["fd00::1"])
    result = util.get_primary_ip_address_from_zeroconf(mock_info, prefer_ipv6=True)
    assert result == "fd00::1"


def test_get_primary_ip_address_from_zeroconf_ipv6_fallback() -> None:
    """Test zeroconf IP extraction falls back to IPv6 when no IPv4 available."""
    mock_info = _mock_service_info([], ["fd00::1"])
    result = util.get_primary_ip_address_from_zeroconf(mock_info, prefer_ipv6=False)
    assert result == "fd00::1"


def test_get_primary_ip_address_from_zeroconf_ipv4_fallback() -> None:
    """Test zeroconf IP extraction falls back to IPv4 when no IPv6 available."""
    mock_info = _mock_service_info(["192.168.1.100"], [])
    result = util.get_primary_ip_address_from_zeroconf(mock_info, prefer_ipv6=True)
    assert result == "192.168.1.100"


def test_get_primary_ip_address_from_zeroconf_skips_link_local() -> None:
    """Test zeroconf IP extraction skips loopback and link-local addresses."""
    mock_info = _mock_service_info(
        ["127.0.0.1", "169.254.1.1", "192.168.1.100"],
        ["::1", "fe80::1", "fd00::1"],
    )
    # IPv4 preferred: should skip 127.x and 169.254.x
    assert (
        util.get_primary_ip_address_from_zeroconf(mock_info, prefer_ipv6=False) == "192.168.1.100"
    )
    # IPv6 preferred: should skip ::1 and fe80::
    assert util.get_primary_ip_address_from_zeroconf(mock_info, prefer_ipv6=True) == "fd00::1"


def test_get_primary_ip_address_from_zeroconf_no_addresses() -> None:
    """Test zeroconf IP extraction returns None when no addresses available."""
    mock_info = _mock_service_info([], [])
    assert util.get_primary_ip_address_from_zeroconf(mock_info) is None
    assert util.get_primary_ip_address_from_zeroconf(mock_info, prefer_ipv6=True) is None


def _make_mock_adapter(
    name: str,
    ipv4_addrs: list[str] | None = None,
    ipv6_addrs: list[tuple[str, int, int]] | None = None,
) -> MagicMock:
    """
    Create a mock ifaddr.Adapter.

    :param name: Adapter name.
    :param ipv4_addrs: List of IPv4 address strings.
    :param ipv6_addrs: List of (address, flowinfo, scope_id) tuples for IPv6.
    """
    adapter = MagicMock()
    adapter.name = name
    adapter.nice_name = name
    ips = []
    for addr in ipv4_addrs or []:
        ip_mock = MagicMock()
        ip_mock.is_IPv6 = False
        ip_mock.ip = addr
        ips.append(ip_mock)
    for addr_tuple in ipv6_addrs or []:
        ip_mock = MagicMock()
        ip_mock.is_IPv6 = True
        ip_mock.ip = addr_tuple
        ips.append(ip_mock)
    adapter.ips = ips
    return adapter


def test_get_zeroconf_args_dual_stack() -> None:
    """Test zeroconf args on a dual-stack host."""
    adapters = [
        _make_mock_adapter("eth0", ["192.168.1.10"], [("fd00::1", 0, 2)]),
    ]
    with patch("music_assistant.helpers.util.ifaddr.get_adapters", return_value=adapters):
        result = util.get_zeroconf_args(use_all_interfaces=False)
    assert result["ip_version"] == IPVersion.All
    assert isinstance(result["interfaces"], list)
    assert "192.168.1.10" in result["interfaces"]


def test_get_zeroconf_args_ipv4_only() -> None:
    """Test zeroconf args on an IPv4-only host."""
    adapters = [
        _make_mock_adapter("eth0", ["192.168.1.10"]),
    ]
    with patch("music_assistant.helpers.util.ifaddr.get_adapters", return_value=adapters):
        result = util.get_zeroconf_args(use_all_interfaces=False)
    assert result["ip_version"] == IPVersion.V4Only
    assert result["interfaces"] == InterfaceChoice.Default


def test_get_zeroconf_args_ipv6_only() -> None:
    """Test zeroconf args on an IPv6-only host."""
    adapters = [
        _make_mock_adapter("eth0", ipv6_addrs=[("fd00::1", 0, 2)]),
    ]
    with patch("music_assistant.helpers.util.ifaddr.get_adapters", return_value=adapters):
        result = util.get_zeroconf_args(use_all_interfaces=False)
    assert result["ip_version"] == IPVersion.V6Only
    assert isinstance(result["interfaces"], list)


def test_get_zeroconf_args_skips_loopback() -> None:
    """Test that loopback addresses are excluded from interface detection."""
    adapters = [
        _make_mock_adapter("lo", ["127.0.0.1"], [("::1", 0, 0)]),
        _make_mock_adapter("eth0", ["192.168.1.10"]),
    ]
    with patch("music_assistant.helpers.util.ifaddr.get_adapters", return_value=adapters):
        result = util.get_zeroconf_args(use_all_interfaces=False)
    # Should be IPv4-only (only loopback IPv6 found, which is excluded)
    assert result["ip_version"] == IPVersion.V4Only


def test_get_zeroconf_args_all_interfaces() -> None:
    """Test zeroconf args with use_all_interfaces=True."""
    adapters = [
        _make_mock_adapter("eth0", ["192.168.1.10"], [("fd00::1", 0, 2)]),
    ]
    with patch("music_assistant.helpers.util.ifaddr.get_adapters", return_value=adapters):
        result = util.get_zeroconf_args(use_all_interfaces=True)
    assert result["ip_version"] == IPVersion.All
    assert isinstance(result["interfaces"], list)
    assert "192.168.1.10" in result["interfaces"]


def test_interface_name_for_ip_ipv4_match() -> None:
    """An IPv4 address returns the name of the interface that holds it."""
    adapters = [
        _make_mock_adapter("lo", ["127.0.0.1"]),
        _make_mock_adapter("eth0", ["192.168.1.10"]),
    ]
    with patch("music_assistant.helpers.util.ifaddr.get_adapters", return_value=adapters):
        assert util.interface_name_for_ip("192.168.1.10") == "eth0"


def test_interface_name_for_ip_ipv6_match() -> None:
    """An IPv6 address (stored as an (addr, flowinfo, scope_id) tuple) resolves by its address."""
    adapters = [
        _make_mock_adapter("eth0", ipv6_addrs=[("fd00::1", 0, 2)]),
    ]
    with patch("music_assistant.helpers.util.ifaddr.get_adapters", return_value=adapters):
        assert util.interface_name_for_ip("fd00::1") == "eth0"


def test_interface_name_for_ip_no_match() -> None:
    """An address that no interface holds returns None."""
    adapters = [
        _make_mock_adapter("eth0", ["192.168.1.10"]),
    ]
    with patch("music_assistant.helpers.util.ifaddr.get_adapters", return_value=adapters):
        assert util.interface_name_for_ip("10.0.0.1") is None


@pytest.mark.parametrize("capability", ["DEFAULT", "NO AVX"])
def test_ml_inference_probe_rejects_no_avx2(capability: str) -> None:
    """A no-AVX2 CPU is rejected before any kernel runs (safe to check in-process)."""
    with patch("torch.backends.cpu.get_cpu_capability", return_value=capability):
        assert _ml_inference_probe.run_probe() == _ml_inference_probe.PROBE_NO_AVX2


def test_ml_inference_probe_subprocess_runs_to_a_clean_verdict() -> None:
    """
    End-to-end: the probe runs out-of-process and exits with a defined verdict, never a crash.

    Spawning a subprocess (rather than calling run_probe() inline) is the same isolation the
    production check relies on, so a hypothetical native crash on a misconfigured host fails
    this one test instead of taking down the session. On an x86 host with AVX2 this exercises
    the kernels and returns PROBE_CAPABLE; elsewhere it returns PROBE_NO_AVX2.
    """
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-m", _ml_inference_probe.__name__],
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert result.returncode in (
        _ml_inference_probe.PROBE_CAPABLE,
        _ml_inference_probe.PROBE_NO_AVX2,
    ), result.stderr.decode()


@pytest.mark.parametrize(
    ("returncode", "translation_key"),
    [
        (_ml_inference_probe.PROBE_CAPABLE, None),
        (_ml_inference_probe.PROBE_NO_AVX2, "unsupported_system_avx2"),
        (-signal.SIGILL, "unsupported_system_ml_inference_failed"),
        (-signal.SIGSEGV, "unsupported_system_ml_inference_failed"),
        (-signal.SIGABRT, "unsupported_system_ml_inference_failed"),
        (-signal.SIGKILL, None),  # external/OOM kill is not a CPU fault -> fail open
        (1, None),  # unexpected clean exit -> fail open
        (None, None),  # spawn failure or timeout -> fail open
    ],
)
async def test_verify_cpu_supports_ml_inference_x86(
    returncode: int | None, translation_key: str | None
) -> None:
    """On x86 the probe verdict vetoes only on no-AVX2 or a fatal signal; anything else fails open."""
    with (
        patch("music_assistant.helpers.util.platform.machine", return_value="x86_64"),
        patch(
            "music_assistant.helpers.util._run_ml_inference_probe",
            AsyncMock(return_value=returncode),
        ),
    ):
        if translation_key is not None:
            with pytest.raises(UnsupportedSystemError) as err:
                await util.verify_cpu_supports_ml_inference()
            assert err.value.translation_key == translation_key
            assert err.value.translation_args == []
        else:
            await util.verify_cpu_supports_ml_inference()


async def test_verify_cpu_supports_ml_inference_arm() -> None:
    """ARM machines pass without spawning the probe (QNNPACK backend works there)."""
    with (
        patch("music_assistant.helpers.util.platform.machine", return_value="aarch64"),
        patch("music_assistant.helpers.util._run_ml_inference_probe", AsyncMock()) as probe,
    ):
        await util.verify_cpu_supports_ml_inference()
    probe.assert_not_called()


@pytest.mark.parametrize(
    ("returncode", "expected"),
    [
        (-signal.SIGILL, -signal.SIGILL),
        (0, 0),
    ],
)
async def test_run_ml_inference_probe_returncode(returncode: int, expected: int) -> None:
    """The probe runner reports the subprocess exit code (negative when a signal killed it)."""
    proc = AsyncMock()
    proc.wait = AsyncMock(return_value=returncode)
    proc.returncode = returncode
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        assert await util._run_ml_inference_probe() == expected


async def test_run_ml_inference_probe_spawn_failure() -> None:
    """A spawn failure is reported as None so the caller fails open."""
    with patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=OSError("boom"))):
        assert await util._run_ml_inference_probe() is None


async def test_run_ml_inference_probe_timeout() -> None:
    """A probe that overruns the timeout is killed and reported as None."""
    proc = AsyncMock()
    proc.kill = MagicMock()
    with (
        patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)),
        patch("asyncio.wait_for", AsyncMock(side_effect=TimeoutError)),
    ):
        assert await util._run_ml_inference_probe() is None
    proc.kill.assert_called_once()


def test_unsupported_system_error_is_setup_failed() -> None:
    """UnsupportedSystemError must subclass SetupFailedError so existing handling applies."""
    assert issubclass(UnsupportedSystemError, SetupFailedError)


@pytest.mark.parametrize(
    ("cpu_cores", "min_cpu_cores", "should_raise"),
    [
        (4, 4, False),
        (8, 4, False),
        (2, 4, True),
        (1, 4, True),
        (1, 0, False),  # 0 disables the check
    ],
)
async def test_verify_system_meets_requirements_cpu(
    cpu_cores: int, min_cpu_cores: int, should_raise: bool
) -> None:
    """The CPU-core gate raises UnsupportedSystemError below the minimum."""
    with (
        patch("music_assistant.helpers.util.os.process_cpu_count", return_value=cpu_cores),
        patch("music_assistant.helpers.util.get_total_system_memory", return_value=64.0),
    ):
        if should_raise:
            with pytest.raises(UnsupportedSystemError):
                await util.verify_system_meets_requirements(
                    feature_name="X", min_cpu_cores=min_cpu_cores
                )
        else:
            await util.verify_system_meets_requirements(
                feature_name="X", min_cpu_cores=min_cpu_cores
            )


@pytest.mark.parametrize(
    ("total_gb", "min_memory_gb", "should_raise"),
    [
        (8.0, 8.0, False),
        (16.0, 8.0, False),
        (4.0, 6.0, True),
        (3.5, 6.0, True),
        # the gate applies the reporting tolerance: 3.8GB clears a 4GB minimum (within 8%),
        # 3.6GB does not (below the 3.68GB floor) -- guards against reverting to strict `<`
        (3.8, 4.0, False),
        (3.6, 4.0, True),
        (0.0, 8.0, False),  # 0.0 == unknown memory -> fail open, never block
        (2.0, 0.0, False),  # 0 disables the check
    ],
)
async def test_verify_system_meets_requirements_memory(
    total_gb: float, min_memory_gb: float, should_raise: bool
) -> None:
    """The RAM gate raises below the minimum (within tolerance) but fails open when unknown (0.0)."""
    with (
        patch("music_assistant.helpers.util.os.process_cpu_count", return_value=16),
        patch("music_assistant.helpers.util.get_total_system_memory", return_value=total_gb),
    ):
        if should_raise:
            with pytest.raises(UnsupportedSystemError):
                await util.verify_system_meets_requirements(
                    feature_name="X", min_memory_gb=min_memory_gb
                )
        else:
            await util.verify_system_meets_requirements(
                feature_name="X", min_memory_gb=min_memory_gb
            )


@pytest.mark.parametrize(
    ("total_gb", "target_gb", "expected"),
    [
        (4.0, 4.0, True),
        (3.8, 4.0, True),  # a "4GB" host reports ~3.8GB -> still meets a 4GB target
        (3.7, 4.0, True),  # just above the 8% tolerance floor (3.68GB)
        (3.6, 4.0, False),  # below the tolerance floor
        (7.7, 8.0, True),  # an "8GB" host reporting ~7.7GB meets an 8GB target
        (7.3, 8.0, False),  # below the 8GB tolerance floor (7.36GB)
        (0.0, 4.0, True),  # unknown memory -> fail open
        (2.0, 0.0, True),  # no requirement -> always met
    ],
)
def test_meets_memory_target(total_gb: float, target_gb: float, expected: bool) -> None:
    """A nominal RAM target is met within the reporting tolerance; unknown/zero fail open."""
    assert util.meets_memory_target(total_gb, target_gb) is expected


async def test_verify_system_meets_requirements_ml_inference() -> None:
    """require_ml_inference runs the capability probe after the RAM/CPU checks."""
    with (
        patch("music_assistant.helpers.util.os.process_cpu_count", return_value=16),
        patch("music_assistant.helpers.util.get_total_system_memory", return_value=64.0),
        patch("music_assistant.helpers.util.platform.machine", return_value="x86_64"),
        patch(
            "music_assistant.helpers.util._run_ml_inference_probe",
            AsyncMock(return_value=_ml_inference_probe.PROBE_NO_AVX2),
        ),
    ):
        # capable RAM/CPU but the probe rejects: only raises when the ML check is requested
        with pytest.raises(UnsupportedSystemError):
            await util.verify_system_meets_requirements(
                feature_name="X", min_cpu_cores=4, min_memory_gb=8.0, require_ml_inference=True
            )
        await util.verify_system_meets_requirements(
            feature_name="X", min_cpu_cores=4, min_memory_gb=8.0
        )


async def test_unsupported_system_error_translation() -> None:
    """Each raise path carries the right translation key + ordered args (feature name first)."""
    with (
        patch("music_assistant.helpers.util.os.process_cpu_count", return_value=2),
        patch("music_assistant.helpers.util.get_total_system_memory", return_value=64.0),
        pytest.raises(UnsupportedSystemError) as cpu_err,
    ):
        await util.verify_system_meets_requirements(feature_name="Smart Fades", min_cpu_cores=4)
    assert cpu_err.value.translation_key == "unsupported_system_cpu_cores"
    assert cpu_err.value.translation_args == ["Smart Fades", 4, 2]

    with (
        patch("music_assistant.helpers.util.os.process_cpu_count", return_value=16),
        patch("music_assistant.helpers.util.get_total_system_memory", return_value=2.0),
        pytest.raises(UnsupportedSystemError) as mem_err,
    ):
        await util.verify_system_meets_requirements(feature_name="Smart Fades", min_memory_gb=8.0)
    assert mem_err.value.translation_key == "unsupported_system_memory"
    assert mem_err.value.translation_args == ["Smart Fades", "8", "2.0"]

    with (
        patch("music_assistant.helpers.util.platform.machine", return_value="x86_64"),
        patch(
            "music_assistant.helpers.util._run_ml_inference_probe",
            AsyncMock(return_value=_ml_inference_probe.PROBE_NO_AVX2),
        ),
        pytest.raises(UnsupportedSystemError) as avx_err,
    ):
        await util.verify_cpu_supports_ml_inference()
    assert avx_err.value.translation_key == "unsupported_system_avx2"
    assert avx_err.value.translation_args == []


@pytest.mark.parametrize(
    ("cpu_cores", "total_gb", "expected"),
    [
        (4, 6.0, True),  # meets both recommended thresholds
        (8, 16.0, True),
        (2, 6.0, False),  # below recommended cores
        (4, 4.0, False),  # below recommended RAM
        (4, 0.0, True),  # unknown memory -> fail open, same as the gate
    ],
)
def test_system_meets_requirements(cpu_cores: int, total_gb: float, expected: bool) -> None:
    """The non-raising predicate mirrors the gate's RAM/CPU checks, failing open on unknown RAM."""
    with (
        patch("music_assistant.helpers.util.os.process_cpu_count", return_value=cpu_cores),
        patch("music_assistant.helpers.util.get_total_system_memory", return_value=total_gb),
    ):
        assert util.system_meets_requirements(min_memory_gb=6.0, min_cpu_cores=4) is expected


@pytest.mark.parametrize(
    ("machine", "expected"),
    [
        ("aarch64", True),
        ("arm64", True),
        ("armv7l", True),
        ("x86_64", False),
        ("AMD64", False),
    ],
)
def test_is_arm(machine: str, expected: bool) -> None:
    """is_arm recognizes 32/64-bit ARM and rejects x86."""
    with patch("music_assistant.helpers.util.platform.machine", return_value=machine):
        assert util.is_arm() is expected


@pytest.mark.parametrize(
    ("cpu_count", "expected"),
    [(1, 1), (2, 1), (4, 1), (8, 2), (12, 3), (32, 8)],
)
def test_inference_thread_budget(cpu_count: int, expected: int) -> None:
    """The inference thread budget is a quarter of the cores, never below one."""
    with (
        patch("music_assistant.helpers.util.os.process_cpu_count", return_value=cpu_count),
        patch.dict(os.environ, {}, clear=False),
    ):
        os.environ.pop("OMP_NUM_THREADS", None)
        assert util.inference_thread_budget() == expected


def test_inference_thread_budget_follows_operator_override() -> None:
    """An operator-supplied OMP_NUM_THREADS becomes the torch budget too, so the two agree."""
    with (
        patch("music_assistant.helpers.util.os.process_cpu_count", return_value=32),
        patch.dict(os.environ, {"OMP_NUM_THREADS": "2"}, clear=False),
    ):
        assert util.inference_thread_budget() == 2


def test_cap_native_thread_pools_sets_env() -> None:
    """The native pool caps are published to the environment for load-time pickup."""
    env_vars = (
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
        "VECLIB_MAXIMUM_THREADS",
    )
    with (
        patch("music_assistant.helpers.util.os.process_cpu_count", return_value=8),
        patch.dict(os.environ, dict.fromkeys(env_vars, ""), clear=False),
    ):
        for env_var in env_vars:
            del os.environ[env_var]
        assert util.cap_native_thread_pools() == 2
        for env_var in env_vars:
            assert os.environ[env_var] == "2"


def test_cap_native_thread_pools_respects_operator_value() -> None:
    """An operator-supplied cap is kept, reported back, and applied to the other pools."""
    with (
        patch("music_assistant.helpers.util.os.process_cpu_count", return_value=8),
        patch.dict(os.environ, {"OMP_NUM_THREADS": "1"}, clear=False),
    ):
        os.environ.pop("OPENBLAS_NUM_THREADS", None)
        assert util.cap_native_thread_pools() == 1
        assert os.environ["OMP_NUM_THREADS"] == "1"
        assert os.environ["OPENBLAS_NUM_THREADS"] == "1"


# 4/8/2 GiB expressed in bytes, for cgroup fixture files.
_GIB = 1024**3


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (str(4 * _GIB), 4.0),
        (str(8 * _GIB), 8.0),
        ("max", None),  # v2 unlimited sentinel
        ("", None),  # empty file
        (str(1 << 62), None),  # v1 unlimited sentinel
        ("0", None),  # zero is not a real limit
        ("-1", None),  # negative is not a real limit
        ("not-a-number", None),
    ],
)
def test_read_cgroup_limit_file(tmp_path: Path, raw: str, expected: float | None) -> None:
    """A cgroup limit file parses to GB, treating max/sentinel/garbage as no limit."""
    limit_file = tmp_path / "memory.max"
    limit_file.write_text(raw)
    assert util._read_cgroup_limit_file(str(limit_file)) == expected


def test_read_cgroup_limit_file_missing(tmp_path: Path) -> None:
    """A missing cgroup limit file yields None rather than raising."""
    assert util._read_cgroup_limit_file(str(tmp_path / "absent")) is None


def test_cgroup_limit_v2(tmp_path: Path) -> None:
    """Cgroup v2 memory.max at the mount root is read (namespaced container case)."""
    (tmp_path / "memory.max").write_text(str(4 * _GIB))
    # proc file absent -> rel is None -> falls back to the mount root.
    limit = util._get_cgroup_memory_limit_gb(
        cgroup_root=str(tmp_path), proc_cgroup=str(tmp_path / "absent")
    )
    assert limit == 4.0


def test_cgroup_limit_v2_uses_min_across_hierarchy(tmp_path: Path) -> None:
    """The effective v2 limit is the smallest memory.max across the cgroup and its ancestors."""
    (tmp_path / "memory.max").write_text(str(8 * _GIB))  # root cap
    leaf = tmp_path / "leaf"
    leaf.mkdir()
    (leaf / "memory.max").write_text(str(2 * _GIB))  # tighter leaf cap wins
    proc = tmp_path / "proc_cgroup"
    proc.write_text("0::/leaf\n")
    limit = util._get_cgroup_memory_limit_gb(cgroup_root=str(tmp_path), proc_cgroup=str(proc))
    assert limit == 2.0


def test_cgroup_limit_v2_walks_ancestors(tmp_path: Path) -> None:
    """A parent slice's memory.max caps the limit even when the leaf cgroup is unlimited."""
    leaf = tmp_path / "system.slice" / "ma.service"
    leaf.mkdir(parents=True)
    (leaf / "memory.max").write_text("max")  # leaf is unlimited...
    (tmp_path / "system.slice" / "memory.max").write_text(str(4 * _GIB))  # ...ancestor caps it
    (tmp_path / "memory.max").write_text("max")
    proc = tmp_path / "proc_cgroup"
    proc.write_text("0::/system.slice/ma.service\n")
    limit = util._get_cgroup_memory_limit_gb(cgroup_root=str(tmp_path), proc_cgroup=str(proc))
    assert limit == 4.0


def test_cgroup_limit_v1_fallback(tmp_path: Path) -> None:
    """With no v2 file, the v1 memory controller limit is used."""
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "memory.limit_in_bytes").write_text(str(4 * _GIB))
    proc = tmp_path / "proc_cgroup"
    proc.write_text("3:memory:/\n")
    limit = util._get_cgroup_memory_limit_gb(cgroup_root=str(tmp_path), proc_cgroup=str(proc))
    assert limit == 4.0


def test_cgroup_limit_none_when_unset(tmp_path: Path) -> None:
    """No cgroup files present -> no limit detected."""
    assert (
        util._get_cgroup_memory_limit_gb(
            cgroup_root=str(tmp_path), proc_cgroup=str(tmp_path / "absent")
        )
        is None
    )


@pytest.mark.parametrize(
    ("host_gb", "cgroup_gb", "platform", "expected"),
    [
        (16.0, 4.0, "linux", 4.0),  # container limit below host -> use the limit
        (8.0, 16.0, "linux", 8.0),  # limit above host -> host wins
        (8.0, None, "linux", 8.0),  # no limit -> host RAM
        (0.0, 4.0, "linux", 0.0),  # host unknown -> unknown (fail open)
        (8.0, 4.0, "darwin", 8.0),  # non-linux never consults cgroups
    ],
)
def test_get_total_system_memory(
    host_gb: float, cgroup_gb: float | None, platform: str, expected: float
) -> None:
    """Total memory is min(host RAM, cgroup limit) on Linux; host RAM elsewhere."""
    with (
        patch("music_assistant.helpers.util._get_host_memory_gb", return_value=host_gb),
        patch("music_assistant.helpers.util._get_cgroup_memory_limit_gb", return_value=cgroup_gb),
        patch("music_assistant.helpers.util.sys.platform", platform),
    ):
        assert util.get_total_system_memory() == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # plain URLs are left as strings for yarl to normalise
        ("http://host/path", "http://host/path"),
        ("http://host/path?a=1&b=2", "http://host/path?a=1&b=2"),
        # already-encoded URLs are wrapped so yarl keeps the escapes verbatim
        ("http://host/stream?token=ab%2Fcd", URL("http://host/stream?token=ab%2Fcd", encoded=True)),
        ("http://host/with%20space", URL("http://host/with%20space", encoded=True)),
    ],
)
def test_encoded_request_url(url: str, expected: str | URL) -> None:
    """A pre-encoded URL is preserved as-is; a plain URL is left untouched."""
    result = encoded_request_url(url)
    assert result == expected
    assert type(result) is type(expected)
    # the percent-escapes must survive intact for auth-bearing stream URLs
    assert str(result) == url
