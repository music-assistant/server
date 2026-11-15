"""Tests for which protocol of a player renders its audio and receives its audio-path commands."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from music_assistant_models.enums import IdentifierType, PlayerFeature, ProtocolRole

from music_assistant.constants import CONF_PREFERRED_OUTPUT_PROTOCOL
from music_assistant.controllers.players import PlayerController
from tests.common import MockProvider, attach_mock, mock_player_mass

DEVICE = {IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"}


def _controller() -> tuple[PlayerController, MagicMock, dict[str, str]]:
    """
    Return a players controller on a mocked server, that server, and the preferred outputs.

    The preferred output protocol of a player is read from the returned dict, by player id.
    """
    mass = mock_player_mass()
    mass.config.get_raw_core_config_value = MagicMock(return_value="GLOBAL")
    preferred: dict[str, str] = {}

    def _get_player_value(player_id: str, key: str, default: Any = None) -> Any:
        if key == CONF_PREFERRED_OUTPUT_PROTOCOL:
            return preferred.get(player_id, default)
        return default

    mass.config.get_player_value = MagicMock(side_effect=_get_player_value)
    controller = PlayerController(mass)
    mass.players = controller
    return controller, mass, preferred


class TestSelectBestOutputProtocol:
    """Tests for output protocol selection logic."""

    def test_select_native_when_preferred_is_native(self) -> None:
        """Test that native protocol is selected when user prefers native."""
        controller, mass, preferred = _controller()
        provider = MockProvider("sonos", mass=mass)
        player, native = attach_mock(provider, "sonos_123", "Kantoor", identifiers=DEVICE)
        native._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        preferred[player.player_id] = "native"

        selected, output_protocol = controller._select_best_output_protocol(player)

        assert selected is native
        assert output_protocol is None  # None means native playback

    def test_select_dlna_when_preferred_is_dlna(self) -> None:
        """Test that DLNA protocol is selected when user prefers DLNA."""
        controller, mass, preferred = _controller()
        sonos = MockProvider("sonos", mass=mass)
        dlna = MockProvider("dlna", mass=mass)
        player, native = attach_mock(sonos, "sonos_123", "Kantoor", identifiers=DEVICE)
        native._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        _, dlna_protocol = attach_mock(
            dlna,
            "dlna_AABBCCDDEEFF",
            "Kantoor DLNA",
            role=ProtocolRole.AUDIO,
            player=player,
            identifiers=DEVICE,
        )
        preferred[player.player_id] = "dlna_AABBCCDDEEFF"

        selected, output_protocol = controller._select_best_output_protocol(player)

        # Should select DLNA, not native
        assert selected is dlna_protocol
        assert output_protocol is not None
        assert output_protocol.output_protocol_id == "dlna_AABBCCDDEEFF"

    def test_select_airplay_when_preferred_is_airplay(self) -> None:
        """Test that AirPlay protocol is selected when user prefers AirPlay."""
        controller, mass, preferred = _controller()
        sonos = MockProvider("sonos", mass=mass)
        airplay = MockProvider("airplay", mass=mass)
        dlna = MockProvider("dlna", mass=mass)
        player, native = attach_mock(sonos, "sonos_123", "Kantoor", identifiers=DEVICE)
        native._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        _, airplay_protocol = attach_mock(
            airplay,
            "airplay_AABBCCDDEEFF",
            "Kantoor AirPlay",
            role=ProtocolRole.AUDIO,
            player=player,
            identifiers=DEVICE,
        )
        attach_mock(
            dlna,
            "dlna_AABBCCDDEEFF",
            "Kantoor DLNA",
            role=ProtocolRole.AUDIO,
            player=player,
            identifiers=DEVICE,
        )
        preferred[player.player_id] = "airplay_AABBCCDDEEFF"

        selected, output_protocol = controller._select_best_output_protocol(player)

        # the user preference overrides the native playback and the priority
        assert selected is airplay_protocol
        assert output_protocol is not None
        assert output_protocol.output_protocol_id == "airplay_AABBCCDDEEFF"

    def test_fallback_to_native_when_auto(self) -> None:
        """Test that native playback is used when preference is auto."""
        controller, mass, preferred = _controller()
        provider = MockProvider("sonos", mass=mass)
        player, native = attach_mock(provider, "sonos_123", "Kantoor", identifiers=DEVICE)
        native._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        preferred[player.player_id] = "auto"

        selected, output_protocol = controller._select_best_output_protocol(player)

        assert selected is native
        assert output_protocol is None  # None means native playback

    def test_skip_protocol_that_needs_setup(self) -> None:
        """Test that a protocol which still needs setup is never selected."""
        controller, mass, _ = _controller()
        airplay = MockProvider("airplay", mass=mass)
        dlna = MockProvider("dlna", mass=mass)
        # a device without native playback, played through its audio protocols
        player, airplay_protocol = attach_mock(
            airplay,
            "airplay_AABBCCDDEEFF",
            "Living Room TV AirPlay",
            role=ProtocolRole.AUDIO,
            identifiers=DEVICE,
        )
        # discovered, but still awaits pairing
        airplay_protocol._attr_needs_setup = True
        _, dlna_protocol = attach_mock(
            dlna,
            "dlna_AABBCCDDEEFF",
            "Living Room TV DLNA",
            role=ProtocolRole.AUDIO,
            player=player,
            identifiers=DEVICE,
        )

        selected, output_protocol = controller._select_best_output_protocol(player)

        # Should skip the unpaired AirPlay output and fall through to DLNA
        assert selected is dlna_protocol
        assert output_protocol is not None
        assert output_protocol.output_protocol_id == "dlna_AABBCCDDEEFF"

        # and the unpaired output must be reported as unavailable
        player.refresh_state(signal_event=False)
        airplay_output = next(
            output
            for output in player._output_protocols
            if output.output_protocol_id == "airplay_AABBCCDDEEFF"
        )
        assert airplay_output.available is False


class TestGetControlTarget:
    """Tests for resolving the target of an audio-path command."""

    @staticmethod
    def _audio_only_player(mass: MagicMock, *protocols: tuple[str, str]) -> dict[str, Any]:
        """
        Return the protocols of a player that only has audio protocols, by id, with the player.

        Every protocol supports announcements.

        :param mass: The mocked server.
        :param protocols: The domain and the id of each protocol, attached in that order.
        """
        attached: dict[str, Any] = {}
        player = None
        for domain, protocol_id in protocols:
            player, protocol = attach_mock(
                MockProvider(domain, mass=mass),
                protocol_id,
                f"Kantoor {domain}",
                role=ProtocolRole.AUDIO,
                player=player,
            )
            protocol._attr_supported_features.add(PlayerFeature.PLAY_ANNOUNCEMENT)
            attached[protocol_id] = protocol
        attached["player"] = player
        return attached

    def test_fallback_follows_protocol_priority(self) -> None:
        """
        Without an active output the fallback picks the highest priority protocol.

        An announcement on an idle player reaches this path, so it has to land on the
        same output that regular playback selection would have chosen.
        """
        controller, mass, _ = _controller()
        # sendspin attaches first, but airplay outranks it
        attached = self._audio_only_player(
            mass,
            ("sendspin", "sendspin_AABBCCDDEEFF"),
            ("airplay", "airplay_AABBCCDDEEFF"),
        )

        target = controller._get_control_target(
            attached["player"], PlayerFeature.PLAY_ANNOUNCEMENT, require_active=False
        )
        assert target is attached["airplay_AABBCCDDEEFF"]

    def test_fallback_follows_preferred_output_protocol(self) -> None:
        """An explicitly preferred output beats the priority order."""
        controller, mass, preferred = _controller()
        attached = self._audio_only_player(
            mass,
            ("airplay", "airplay_AABBCCDDEEFF"),
            ("sendspin", "sendspin_AABBCCDDEEFF"),
        )
        # airplay outranks sendspin, but the user picked sendspin
        preferred[attached["player"].player_id] = "sendspin_AABBCCDDEEFF"

        target = controller._get_control_target(
            attached["player"], PlayerFeature.PLAY_ANNOUNCEMENT, require_active=False
        )
        assert target is attached["sendspin_AABBCCDDEEFF"]

    def test_fallback_ignores_preference_for_another_players_output(self) -> None:
        """A preference left behind by a relink does not steal another speaker's output."""
        controller, mass, preferred = _controller()
        attached = self._audio_only_player(mass, ("sendspin", "sendspin_AABBCCDDEEFF"))
        # the preference now points at an output belonging to a different speaker
        other = self._audio_only_player(mass, ("airplay", "airplay_998877665544"))
        assert other["player"] is not attached["player"]
        preferred[attached["player"].player_id] = "airplay_998877665544"

        target = controller._get_control_target(
            attached["player"], PlayerFeature.PLAY_ANNOUNCEMENT, require_active=False
        )
        assert target is attached["sendspin_AABBCCDDEEFF"]

    def test_require_active_skips_the_fallback(self) -> None:
        """With require_active there is no fallback to an idle audio protocol."""
        controller, mass, _ = _controller()
        attached = self._audio_only_player(mass, ("airplay", "airplay_AABBCCDDEEFF"))

        assert (
            controller._get_control_target(
                attached["player"], PlayerFeature.PLAY_ANNOUNCEMENT, require_active=True
            )
            is None
        )
