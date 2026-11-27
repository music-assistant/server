"""Tests for ``PlaybackQuality`` and the codec a ``PlayerProtocol`` is streamed in."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import ContentType

from music_assistant.constants import CONF_PLAYBACK_QUALITY, PlaybackQuality
from music_assistant.models.player_protocol import PlayerProtocol


class _LossyOnlyProtocol(PlayerProtocol):
    """A protocol that plays the uncompressed and the compressed level only."""

    @property
    def playback_quality_codecs(self) -> dict[PlaybackQuality, ContentType]:
        """Return the codecs of the levels it plays."""
        return {
            PlaybackQuality.LOSSLESS_UNCOMPRESSED: ContentType.WAV,
            PlaybackQuality.COMPRESSED: ContentType.AAC,
        }

    @property
    def default_playback_quality(self) -> PlaybackQuality:
        """Return its default level."""
        return PlaybackQuality.COMPRESSED


class _NoLevelsProtocol(PlayerProtocol):
    """A protocol that picks its format with its own settings."""

    @property
    def playback_quality_codecs(self) -> dict[PlaybackQuality, ContentType]:
        """Return no levels."""
        return {}


@pytest.mark.parametrize(
    ("codec", "quality"),
    [
        (ContentType.WAV, PlaybackQuality.LOSSLESS_UNCOMPRESSED),
        (ContentType.PCM_S16LE, PlaybackQuality.LOSSLESS_UNCOMPRESSED),
        (ContentType.FLAC, PlaybackQuality.LOSSLESS_COMPRESSED),
        (ContentType.ALAC, PlaybackQuality.LOSSLESS_COMPRESSED),
        (ContentType.MP3, PlaybackQuality.COMPRESSED),
        (ContentType.AAC, PlaybackQuality.COMPRESSED),
    ],
)
def test_from_codec(codec: ContentType, quality: PlaybackQuality) -> None:
    """A codec belongs to the level of its compression."""
    assert PlaybackQuality.from_codec(codec) is quality


@pytest.mark.parametrize(
    ("protocol_class", "stored", "codec"),
    [
        (PlayerProtocol, "lossless_uncompressed", ContentType.WAV),
        (PlayerProtocol, "compressed", ContentType.MP3),
        (_LossyOnlyProtocol, "compressed", ContentType.AAC),
        (_LossyOnlyProtocol, "lossless_uncompressed", ContentType.WAV),
    ],
)
def test_output_codec_is_the_codec_of_the_configured_level(
    protocol_class: type[PlayerProtocol], stored: str, codec: ContentType
) -> None:
    """The protocol is streamed in its codec of the level it is set to."""
    assert _protocol(protocol_class, stored).output_codec == codec


@pytest.mark.parametrize(
    ("protocol_class", "stored", "codec"),
    [
        (PlayerProtocol, None, ContentType.FLAC),
        (PlayerProtocol, "best", ContentType.FLAC),
        (_LossyOnlyProtocol, None, ContentType.AAC),
        # a level the protocol does not offer, stored before it stopped offering it
        (_LossyOnlyProtocol, "lossless_compressed", ContentType.AAC),
        (_NoLevelsProtocol, "compressed", ContentType.FLAC),
    ],
)
def test_output_codec_falls_back_to_the_codec_of_the_default_level(
    protocol_class: type[PlayerProtocol], stored: str | None, codec: ContentType
) -> None:
    """Without a level the protocol offers, it is streamed in the codec of its default level."""
    assert _protocol(protocol_class, stored).output_codec == codec


def _protocol(protocol_class: type[PlayerProtocol], stored: str | None) -> PlayerProtocol:
    """Return a protocol of the given class whose config holds the given playback quality."""
    # the protocol's __init__ depends on a full provider/mass wiring; output_codec only
    # reads the config
    protocol = object.__new__(protocol_class)
    config = MagicMock()
    config.get_value = MagicMock(
        side_effect=lambda key, default=None: stored if key == CONF_PLAYBACK_QUALITY else default
    )
    protocol._config = config
    return protocol
