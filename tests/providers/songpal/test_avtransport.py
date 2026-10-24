"""Unit tests for the Sony AVTransport SOAP bodies."""

from __future__ import annotations

import unittest

from music_assistant.providers.songpal.avtransport import (
    AVTRANSPORT_SERVICE,
    avtransport_control_url,
    didl_metadata,
    pause_body,
    play_body,
    set_av_transport_uri_body,
    soap_action,
    stop_body,
)

_DESCRIPTION = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <device>
    <serviceList>
      <service>
        <serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>
        <controlURL>/upnp/control/AVTransport</controlURL>
      </service>
    </serviceList>
  </device>
</root>
"""


class AvTransportSoapTest(unittest.TestCase):
    def test_actions_match_the_sample_contract(self) -> None:
        self.assertEqual(
            soap_action("Play"),
            '"urn:schemas-upnp-org:service:AVTransport:1#Play"',
        )
        play = play_body()
        self.assertIn("<InstanceID>0</InstanceID>", play)
        self.assertIn("<Speed>1</Speed>", play)
        self.assertIn(f'<u:Play xmlns:u="{AVTRANSPORT_SERVICE}">', play)
        self.assertIn("<InstanceID>0</InstanceID>", pause_body())
        self.assertNotIn("<Speed>", pause_body())
        self.assertIn("<u:Stop", stop_body())

    def test_metadata_escapes_the_title_and_the_didl(self) -> None:
        body = set_av_transport_uri_body(
            "http://192.168.1.10:8095/flow?x=1&y=2", "Rock & Roll"
        )
        self.assertIn("<InstanceID>0</InstanceID>", body)
        self.assertIn("http://192.168.1.10:8095/flow?x=1&amp;y=2", body)
        self.assertIn("Rock &amp;amp; Roll", body)
        self.assertIn("object.item.audioItem.musicTrack", didl_metadata("Track"))
        self.assertIn("&lt;upnp:class&gt;", body)

    def test_control_url_is_resolved_against_the_description(self) -> None:
        location = "http://192.168.1.119:52323/description.xml"
        self.assertEqual(
            avtransport_control_url(_DESCRIPTION, location),
            "http://192.168.1.119:52323/upnp/control/AVTransport",
        )
        self.assertIsNone(avtransport_control_url("<not-xml", location))


if __name__ == "__main__":
    unittest.main()
