"""Tests for telling the zones of a Sonos chassis apart by their UUID."""

from __future__ import annotations

import pytest

from music_assistant.constants import MAIN_ZONE
from music_assistant.helpers.upnp import get_sonos_zone


@pytest.mark.parametrize(
    ("sonos_uuid", "expected"),
    [
        ("RINCON_F0F6C193E5DD01400", MAIN_ZONE),
        ("rincon_f0f6c193e5dd01400", MAIN_ZONE),
        # the headphone zone of an Amp or soundbar
        ("RINCON_F0F6C193E5DD01500", "RINCON_F0F6C193E5DD01500"),
        # a renderer UDN still carrying its suffix, and UUIDs of other devices
        ("RINCON_F0F6C193E5DD01400_MR", None),
        ("dlna-player", None),
        ("", None),
    ],
)
def test_get_sonos_zone(sonos_uuid: str, expected: str | None) -> None:
    """The speaker's own UUID is the main zone, another port its own zone, anything else none."""
    assert get_sonos_zone(sonos_uuid) == expected
