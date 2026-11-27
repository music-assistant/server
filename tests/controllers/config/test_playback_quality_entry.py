"""Tests for the playback quality entry the config controller gives a protocol."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import ContentType, PlayerFeature

from music_assistant.constants import CONF_PLAYBACK_QUALITY, PlaybackQuality
from tests.common import MockProtocol

from .helpers import attach_on_config

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry

    from music_assistant.mass import MusicAssistant

# PLAY_MEDIA is required for the audio/protocol config entries to be emitted.
FEATURES = {PlayerFeature.VOLUME_SET, PlayerFeature.PLAY_MEDIA}


class _LossyOnlyProtocol(MockProtocol):
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


class _NoLevelsProtocol(MockProtocol):
    """A protocol that picks its format with its own settings."""

    @property
    def playback_quality_codecs(self) -> dict[PlaybackQuality, ContentType]:
        """Return no levels."""
        return {}


async def _playback_quality_entry(
    mass: MusicAssistant, protocol_class: type[MockProtocol], domain: str = "dlna"
) -> ConfigEntry | None:
    """Return the playback quality entry of a new protocol of the given class, if it has one."""
    _, protocol = attach_on_config(
        mass, domain, "proto_1", features=set(FEATURES), protocol_class=protocol_class
    )
    entries = await mass.config._get_protocol_config_entries(protocol)
    return next((entry for entry in entries if entry.key == CONF_PLAYBACK_QUALITY), None)


async def test_entry_lists_every_level_with_the_protocol_default(
    mass_minimal: MusicAssistant,
) -> None:
    """A protocol with the default codecs can pick every level, lossless compressed by default."""
    entry = await _playback_quality_entry(mass_minimal, MockProtocol)

    assert entry is not None
    assert [(option.value, option.disabled) for option in entry.options] == [
        ("lossless_uncompressed", False),
        ("lossless_compressed", False),
        ("compressed", False),
    ]
    assert entry.default_value == "lossless_compressed"
    assert entry.advanced is False
    assert entry.hidden is False
    assert entry.requires_reload is True


async def test_entry_lists_a_level_the_protocol_cannot_play_as_disabled(
    mass_minimal: MusicAssistant,
) -> None:
    """A level the protocol leaves out is still listed, but disabled."""
    entry = await _playback_quality_entry(mass_minimal, _LossyOnlyProtocol)

    assert entry is not None
    assert [(option.value, option.disabled) for option in entry.options] == [
        ("lossless_uncompressed", False),
        ("lossless_compressed", True),
        ("compressed", False),
    ]
    assert entry.default_value == "compressed"


async def test_entry_is_offered_to_a_non_http_protocol(mass_minimal: MusicAssistant) -> None:
    """The entry does not depend on the protocol streaming over HTTP."""
    entry = await _playback_quality_entry(mass_minimal, MockProtocol, domain="airplay")

    assert entry is not None


async def test_no_entry_for_a_protocol_without_levels(mass_minimal: MusicAssistant) -> None:
    """A protocol that offers no level gets no playback quality setting."""
    assert await _playback_quality_entry(mass_minimal, _NoLevelsProtocol) is None
