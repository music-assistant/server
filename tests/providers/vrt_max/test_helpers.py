"""Tests for the VRT MAX provider's pure browse-id helper functions."""

from __future__ import annotations

from music_assistant.providers.vrt_max.provider import (
    _decode,
    _encode,
    _has_tracklist,
    _program_id_from_episode,
)


def test_program_id_from_radio_episode() -> None:
    """A radio-archive episode id trims one trailing (episode) segment."""
    episode_id = (
        "/vrtmax/luister/radio/s/sweet-summer-sundays~11-207/sweet-summer-sundays~11-38720-0/"
    )
    assert (
        _program_id_from_episode(episode_id)
        == "/vrtmax/luister/radio/s/sweet-summer-sundays~11-207/"
    )


def test_program_id_from_podcast_episode() -> None:
    """A podcast episode id trims two trailing (season + episode) segments."""
    episode_id = "/vrtmax/podcasts/radio-1/h/het-fortuin-carlier/1/1--een-begrafenis/"
    assert _program_id_from_episode(episode_id) == "/vrtmax/podcasts/radio-1/h/het-fortuin-carlier/"


def test_has_tracklist_true_for_radio_archive() -> None:
    """Only radio-archive episode ids expose a played-songs tracklist."""
    assert _has_tracklist("/vrtmax/luister/radio/s/show~1-2/show~1-3-0/") is True


def test_has_tracklist_false_for_podcast() -> None:
    """Podcast episodes never expose a played-songs tracklist."""
    assert _has_tracklist("/vrtmax/podcasts/radio-1/h/pod/1/1--ep/") is False


def test_encode_decode_round_trip() -> None:
    """_encode/_decode round-trip, and the encoded form is URL/path safe."""
    # This value's standard base64 encoding contains '+', '/' and '=' padding.
    value = "Ga~_r29l?J"
    encoded = _encode(value)
    assert "=" not in encoded
    assert "+" not in encoded
    assert "/" not in encoded
    assert _decode(encoded) == value
