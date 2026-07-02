"""Tests for the player-queues config-entry builders (config.py)."""

from __future__ import annotations

from unittest.mock import Mock

from music_assistant_models.enums import CrossfadeMode, ProviderFeature

from music_assistant.constants import CONF_CROSSFADE_MODE
from music_assistant.controllers.player_queues.config import (
    core_config_entries,
    queue_config_entries,
)
from music_assistant.controllers.player_queues.constants import (
    CONF_AUTOPLAY_MODE,
    CONF_DEFAULT_ENQUEUE_OPTION_ALBUM,
    CONF_DEFAULT_ENQUEUE_OPTION_TRACK,
)


def _mass(*, similar_tracks: bool, smart_fades: bool) -> Mock:
    """Build a mock MusicAssistant with the provider/smart-fades signals the builder reads."""
    provider = Mock()
    provider.supported_features = {ProviderFeature.SIMILAR_TRACKS} if similar_tracks else set()
    mass = Mock()
    mass.music.providers = [provider]
    mass.streams.smart_fades_available = smart_fades
    return mass


def test_core_config_entries_enqueue_defaults() -> None:
    """A track defaults to PLAY while other media types default to REPLACE."""
    by_key = {entry.key: entry for entry in core_config_entries()}
    assert by_key[CONF_DEFAULT_ENQUEUE_OPTION_TRACK].default_value == "play"
    assert by_key[CONF_DEFAULT_ENQUEUE_OPTION_ALBUM].default_value == "replace"


def test_queue_config_smart_crossfade_unavailable() -> None:
    """Without smart fades the smart-crossfade option is disabled and standard is the default."""
    entries = queue_config_entries(_mass(similar_tracks=True, smart_fades=False))
    crossfade = next(entry for entry in entries if entry.key == CONF_CROSSFADE_MODE)
    assert crossfade.default_value == CrossfadeMode.STANDARD_CROSSFADE.value
    smart = next(
        opt for opt in crossfade.options if opt.value == CrossfadeMode.SMART_CROSSFADE.value
    )
    assert smart.disabled is True


def test_queue_config_smart_crossfade_available() -> None:
    """With smart fades available, smart crossfade becomes the default and stays enabled."""
    entries = queue_config_entries(_mass(similar_tracks=True, smart_fades=True))
    crossfade = next(entry for entry in entries if entry.key == CONF_CROSSFADE_MODE)
    assert crossfade.default_value == CrossfadeMode.SMART_CROSSFADE.value
    smart = next(
        opt for opt in crossfade.options if opt.value == CrossfadeMode.SMART_CROSSFADE.value
    )
    assert smart.disabled is False


def test_queue_config_similar_autoplay_disabled_without_provider() -> None:
    """The 'similar' autoplay option is disabled when no provider can supply similar tracks."""
    entries = queue_config_entries(_mass(similar_tracks=False, smart_fades=True))
    autoplay = next(entry for entry in entries if entry.key == CONF_AUTOPLAY_MODE)
    similar = next(opt for opt in autoplay.options if opt.value == "similar")
    assert similar.disabled is True
