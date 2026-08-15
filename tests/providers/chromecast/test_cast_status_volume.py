"""
Tests for how ChromecastPlayer reports volume from a CastStatus.

A combo device (e.g. a TV or amplifier with a Cast endpoint next to its own
protocol) reports volume 0 through its Cast interface whenever no Cast app is
running, even though its real volume is set through the other interface. That
0 is not authoritative and must not be reported as a hard mute, so an idle
Cast player reporting 0 is treated as "unknown" instead.
"""

from __future__ import annotations

from typing import cast
from unittest.mock import MagicMock

from pychromecast import IDLE_APP_ID

from music_assistant.providers.chromecast.constants import MASS_APP_ID
from music_assistant.providers.chromecast.player import ChromecastPlayer


def _handle_cast_status(fake: MagicMock, status: MagicMock) -> None:
    ChromecastPlayer._handle_cast_status(cast("ChromecastPlayer", fake), status)


def _fake_player(app_id: str | None) -> MagicMock:
    """
    Build a MagicMock standing in for an ungrouped ChromecastPlayer.

    :param app_id: Cast application id the (mocked) Chromecast currently reports.
    """
    fake = MagicMock()
    fake.mass.closing = False
    fake.cc.app_id = app_id
    fake.cast_info.is_multichannel_group = False
    fake.cast_info.is_audio_group = False
    fake.on_app_status_changed = None
    return fake


def _cast_status(*, volume_level: float, volume_muted: bool = False) -> MagicMock:
    """
    Build a CastStatus as the receiver reports it.

    :param volume_level: Volume level (0..1) as reported by the Cast interface.
    :param volume_muted: Mute state as reported by the Cast interface.
    """
    status = MagicMock()
    status.app_id = None
    status.volume_level = volume_level
    status.volume_muted = volume_muted
    return status


def test_idle_cast_reporting_zero_volume_is_unknown() -> None:
    """No Cast app running and a reported volume of 0 is not authoritative."""
    fake = _fake_player(app_id=None)

    _handle_cast_status(fake, _cast_status(volume_level=0.0))

    assert fake._attr_volume_level is None


def test_idle_cast_reporting_nonzero_volume_is_kept() -> None:
    """No Cast app running but a non-zero reported volume is still real."""
    fake = _fake_player(app_id=IDLE_APP_ID)

    _handle_cast_status(fake, _cast_status(volume_level=0.35))

    assert fake._attr_volume_level == 35


def test_active_cast_reporting_zero_volume_is_kept() -> None:
    """A Cast app actively rendering and reporting 0 volume means a genuine mute."""
    fake = _fake_player(app_id=MASS_APP_ID)

    _handle_cast_status(fake, _cast_status(volume_level=0.0))

    assert fake._attr_volume_level == 0
