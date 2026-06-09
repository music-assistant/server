"""Tests for utility/helper functions."""

from ipaddress import IPv4Address, IPv6Address
from unittest.mock import MagicMock, patch

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MusicAssistantError
from zeroconf import InterfaceChoice, IPVersion

from music_assistant.helpers import uri, util


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
    """Create a mock ifaddr.Adapter.

    :param name: Adapter name.
    :param ipv4_addrs: List of IPv4 address strings.
    :param ipv6_addrs: List of (address, flowinfo, scope_id) tuples for IPv6.
    """
    adapter = MagicMock()
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
