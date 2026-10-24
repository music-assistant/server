"""Unit tests for SongPal volume scaling and power-retry handling."""

from __future__ import annotations

import unittest

from music_assistant.providers.songpal.constants import ERROR_REQUEST_RETRY
from music_assistant.providers.songpal.util import (
    device_volume_from_level,
    is_bravia_device,
    is_main_zone,
    is_power_retry,
    level_from_device_volume,
    normalize_mac,
    parse_manual_endpoints,
    playback_state_name,
    player_id_from_macs,
    replace_url_host,
    select_main_zone,
)


class _Error(Exception):
    def __init__(self, code: int | None) -> None:
        super().__init__("device error")
        self.code = code


class _Output:
    def __init__(self, output: str | None) -> None:
        self.output = output


class VolumeScalingTest(unittest.TestCase):
    def test_home_assistant_fraction_matches_level(self) -> None:
        # Home Assistant set_volume_level(0.5) with maxVolume 100 sends 50.
        self.assertEqual(device_volume_from_level(50, 100), 50)
        # set_volume_level(0.6) with maxVolume 100 sends 60.
        self.assertEqual(device_volume_from_level(60, 100), 60)

    def test_reported_level_uses_max_volume(self) -> None:
        # volume 28 of maxVolume 95 is round(28 / 95 * 100) == 29.
        self.assertEqual(level_from_device_volume(28, 95), 29)
        # The reverse step lands back on the device value.
        self.assertEqual(device_volume_from_level(29, 95), 28)

    def test_zero_max_volume(self) -> None:
        self.assertEqual(device_volume_from_level(40, 0), 0)
        self.assertEqual(level_from_device_volume(10, 0), 0)

    def test_min_volume_is_not_part_of_the_scale(self) -> None:
        self.assertEqual(device_volume_from_level(0, 50), 0)
        self.assertEqual(device_volume_from_level(100, 50), 50)


class PowerRetryTest(unittest.TestCase):
    def test_error_40000_is_already_in_the_requested_state(self) -> None:
        self.assertEqual(ERROR_REQUEST_RETRY, 40000)
        self.assertTrue(is_power_retry(_Error(40000)))

    def test_other_errors_are_not_swallowed(self) -> None:
        self.assertFalse(is_power_retry(_Error(1)))
        self.assertFalse(is_power_retry(_Error(None)))
        self.assertFalse(is_power_retry(RuntimeError("offline")))


class DiscoveryHelperTest(unittest.TestCase):
    def test_mac_prefers_wired_address(self) -> None:
        self.assertEqual(
            player_id_from_macs("aa-bb-cc-dd-ee-ff", "11:22:33:44:55:66"),
            "AA:BB:CC:DD:EE:FF",
        )
        self.assertEqual(player_id_from_macs(None, "112233445566"), "11:22:33:44:55:66")

    def test_null_mac_is_rejected(self) -> None:
        self.assertIsNone(normalize_mac("00:00:00:00:00:00"))
        self.assertIsNone(normalize_mac("not-a-mac"))

    def test_bravia_services_are_rejected(self) -> None:
        self.assertTrue(is_bravia_device(["audio", "video"]))
        self.assertTrue(is_bravia_device(["videoScreen"]))
        self.assertFalse(is_bravia_device(["audio", "avContent", "system"]))

    def test_manual_endpoints_keep_http_urls(self) -> None:
        raw = "\n".join(
            [
                "http://192.168.1.20:10000/sony",
                "",
                "# comment",
                "not a url",
                "ftp://192.168.1.21/sony",
            ]
        )
        self.assertEqual(
            parse_manual_endpoints(raw),
            ["http://192.168.1.20:10000/sony"],
        )

    def test_main_zone_does_not_match_zone_10(self) -> None:
        self.assertTrue(is_main_zone("extOutput:zone?zone=1"))
        self.assertFalse(is_main_zone("extOutput:zone?zone=10"))
        self.assertTrue(is_main_zone(None))
        self.assertEqual(select_main_zone([_Output("zone=2"), _Output("zone=1")]).output, "zone=1")

    def test_stream_host_is_rewritten_to_an_ip(self) -> None:
        self.assertEqual(
            replace_url_host("http://music.local:8095/flow/1", "192.168.1.10"),
            "http://192.168.1.10:8095/flow/1",
        )
        self.assertEqual(
            replace_url_host("http://192.168.1.10:8095/flow/1", "10.0.0.1"),
            "http://192.168.1.10:8095/flow/1",
        )

    def test_playback_state_mapping(self) -> None:
        self.assertEqual(playback_state_name("PLAYING", is_idle=False), "playing")
        self.assertEqual(playback_state_name("PAUSED_PLAYBACK", is_idle=False), "paused")
        self.assertEqual(playback_state_name("STOPPED", is_idle=False), "idle")
        self.assertEqual(playback_state_name("PLAYING", is_idle=True), "idle")
        self.assertIsNone(playback_state_name(None, is_idle=False))


if __name__ == "__main__":
    unittest.main()
