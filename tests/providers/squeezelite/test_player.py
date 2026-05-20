"""Unit tests for the squeezelite per-child codec resolver."""

import pytest

from music_assistant.providers.squeezelite.player import _resolve_child_codec


@pytest.mark.parametrize(
    ("config_codec", "helo_codecs", "expected"),
    [
        # Config wins over HELO when set, even if it picks an unsupported codec.
        # The user override is symmetric with the solo-path resolver:
        # a deliberate "output_codec=flac" on a Boom is accepted (and will
        # be silent — same trap as solo, by design).
        ("mp3", ("pcm", "mp3", "flc"), "mp3"),
        ("flac", ("pcm", "mp3"), "flac"),
        # HELO-only path: pick the highest-preference codec the player supports.
        # Modern Squeezelite / SqueezeESP32 announces FLAC support → FLAC.
        (None, ("pcm", "mp3", "flc"), "flac"),
        # Classic Squeezeboxes (Boom/Radio/Touch) advertise only "pcm, mp3" via
        # HELO — picking MP3 here is the fix for the silent-audio bug.
        (None, ("pcm", "mp3"), "mp3"),
        # No HELO data and no config → slimproto baseline.
        (None, (), "mp3"),
        # Unrecognised HELO codes (e.g. ogg/wma) are ignored, baseline applies.
        (None, ("ogg", "wma"), "mp3"),
    ],
)
def test_resolve_child_codec(
    config_codec: str | None,
    helo_codecs: tuple[str, ...],
    expected: str,
) -> None:
    """Per-child codec resolution covers config-override and HELO-fallback paths."""
    assert _resolve_child_codec(config_codec, helo_codecs) == expected
