"""Unit tests for the AirPlay Receiver provider (deterministic AirPlay port)."""

from music_assistant.providers.airplay_receiver import airplay_receiver_port


def test_airplay_receiver_port_is_deterministic() -> None:
    """The port derivation must be stable across processes/restarts (unlike ``hash()``)."""
    # pinned expectations guard against accidental changes to the derivation:
    # the AirPlay provider relies on reproducing these ports from config alone
    assert airplay_receiver_port("airplay_receiver--test1234") == 7745
    assert airplay_receiver_port("airplay_receiver--aabbccdd") == 7038


def test_airplay_receiver_port_stays_in_expected_range() -> None:
    """Derived ports stay within the 7000-7999 AirPlay range for any instance id."""
    for index in range(50):
        port = airplay_receiver_port(f"airplay_receiver--instance{index}")
        assert 7000 <= port <= 7999
