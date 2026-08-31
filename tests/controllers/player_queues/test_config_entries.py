"""Tests for the player-queues config-entry builders (config.py)."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING
from unittest.mock import Mock

from music_assistant_models.enums import CrossfadeMode, ProviderFeature

from music_assistant.constants import (
    CONF_CROSSFADE_DURATION,
    CONF_CROSSFADE_MODE,
    CONF_VALUE_DISABLED,
    CONF_VALUE_ENABLED,
    CONF_VALUE_GLOBAL,
    CONF_VOLUME_NORMALIZATION,
)
from music_assistant.controllers.player_queues.config import (
    CATEGORY_CLICK_ACTIONS,
    CATEGORY_DEFAULT_ENQUEUE_OPTION,
    CATEGORY_ITEMS_TO_SELECT,
    core_config_entries,
    queue_config_entries,
)
from music_assistant.controllers.player_queues.constants import (
    CLICK_ACTION_BROWSE,
    CLICK_ACTION_PLAY,
    CONF_AUTOPLAY_ENABLED,
    CONF_AUTOPLAY_MODE,
    CONF_CROSSFADE_ENABLED,
    CONF_DEFAULT_CLICK_ACTION_ALBUM,
    CONF_DEFAULT_CLICK_ACTION_ARTIST,
    CONF_DEFAULT_CLICK_ACTION_GENRE,
    CONF_DEFAULT_CLICK_ACTION_PLAYLIST,
    CONF_DEFAULT_CLICK_ACTION_RADIO,
    CONF_DEFAULT_CLICK_ACTION_TRACK,
    CONF_DEFAULT_ENQUEUE_OPTION_ALBUM,
    CONF_DEFAULT_ENQUEUE_OPTION_TRACK,
    CONF_DEFAULT_ENQUEUE_SELECT_ARTIST,
    CONF_DEFAULT_PLAY_ACTION_ALBUM_TRACK,
    CONF_DEFAULT_PLAY_ACTION_PLAYLIST_TRACK,
    CONF_SMART_SHUFFLE_ENABLED,
    CONF_SMART_SHUFFLE_SONG_RECENCY,
    PLAY_ACTION_PLAY_FROM_HERE,
    PLAY_ACTION_PLAY_TRACK,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry


def _mass(*, similar_tracks: bool, smart_fades: bool) -> Mock:
    """Build a mock MusicAssistant with the provider/smart-fades signals the builder reads."""
    provider = Mock()
    provider.supported_features = {ProviderFeature.SIMILAR_TRACKS} if similar_tracks else set()
    mass = Mock()
    mass.music.providers = [provider]
    mass.streams.smart_fades_available = smart_fades
    return mass


def _by_key(entries: Iterable[ConfigEntry]) -> dict[str, ConfigEntry]:
    return {entry.key: entry for entry in entries}


def test_core_config_entries_enqueue_defaults() -> None:
    """A track defaults to PLAY, other media types to REPLACE, in their tidied categories."""
    by_key = _by_key(core_config_entries(_mass(similar_tracks=True, smart_fades=True)))
    assert by_key[CONF_DEFAULT_ENQUEUE_OPTION_TRACK].default_value == "play"
    assert by_key[CONF_DEFAULT_ENQUEUE_OPTION_ALBUM].default_value == "replace"
    # the enqueue entries now live in their own dedicated categories
    assert by_key[CONF_DEFAULT_ENQUEUE_SELECT_ARTIST].category == CATEGORY_ITEMS_TO_SELECT
    assert by_key[CONF_DEFAULT_ENQUEUE_OPTION_TRACK].category == CATEGORY_DEFAULT_ENQUEUE_OPTION


def test_core_config_entries_click_actions() -> None:
    """
    The click actions clients read: browsing by default, and a track playing on from where it sits.

    This is a cross-client contract rather than something the server itself reads, so the keys,
    their option values and their defaults are pinned here: a client resolving a value that is no
    longer offered would silently fall back to its own behaviour.
    """
    by_key = _by_key(core_config_entries(_mass(similar_tracks=True, smart_fades=True)))
    click_keys = (
        CONF_DEFAULT_CLICK_ACTION_ARTIST,
        CONF_DEFAULT_CLICK_ACTION_ALBUM,
        CONF_DEFAULT_CLICK_ACTION_TRACK,
        CONF_DEFAULT_CLICK_ACTION_GENRE,
        CONF_DEFAULT_CLICK_ACTION_RADIO,
        CONF_DEFAULT_CLICK_ACTION_PLAYLIST,
    )
    for key in click_keys:
        assert by_key[key].default_value == CLICK_ACTION_BROWSE
        assert {opt.value for opt in by_key[key].options} == {
            CLICK_ACTION_BROWSE,
            CLICK_ACTION_PLAY,
        }
    for key in (CONF_DEFAULT_PLAY_ACTION_ALBUM_TRACK, CONF_DEFAULT_PLAY_ACTION_PLAYLIST_TRACK):
        assert by_key[key].default_value == PLAY_ACTION_PLAY_FROM_HERE
        assert {opt.value for opt in by_key[key].options} == {
            PLAY_ACTION_PLAY_FROM_HERE,
            PLAY_ACTION_PLAY_TRACK,
        }
    for key in (
        *click_keys,
        CONF_DEFAULT_PLAY_ACTION_ALBUM_TRACK,
        CONF_DEFAULT_PLAY_ACTION_PLAYLIST_TRACK,
    ):
        assert by_key[key].category == CATEGORY_CLICK_ACTIONS
        # a hidden entry would never reach the settings UI these are configured from
        assert by_key[key].hidden is False


def test_core_config_entries_global_feature_defaults() -> None:
    """Global (core) feature settings carry no 'global' option and keep the legacy defaults."""
    by_key = _by_key(core_config_entries(_mass(similar_tracks=True, smart_fades=True)))
    smart_shuffle = by_key[CONF_SMART_SHUFFLE_ENABLED]
    assert smart_shuffle.default_value == CONF_VALUE_DISABLED
    assert {opt.value for opt in smart_shuffle.options} == {CONF_VALUE_ENABLED, CONF_VALUE_DISABLED}
    volume_normalization = by_key[CONF_VOLUME_NORMALIZATION]
    assert volume_normalization.default_value == CONF_VALUE_ENABLED
    assert CONF_VALUE_GLOBAL not in {opt.value for opt in volume_normalization.options}
    # the crossfade/autoplay runtime-toggle starting values: global-only, no 'global' option
    crossfade_enabled = by_key[CONF_CROSSFADE_ENABLED]
    assert crossfade_enabled.default_value == CONF_VALUE_DISABLED
    assert {opt.value for opt in crossfade_enabled.options} == {
        CONF_VALUE_ENABLED,
        CONF_VALUE_DISABLED,
    }
    autoplay_enabled = by_key[CONF_AUTOPLAY_ENABLED]
    assert autoplay_enabled.default_value == CONF_VALUE_ENABLED
    assert {opt.value for opt in autoplay_enabled.options} == {
        CONF_VALUE_ENABLED,
        CONF_VALUE_DISABLED,
    }
    # the global-only settings live only on the core schema
    assert CONF_CROSSFADE_DURATION in by_key
    assert CONF_SMART_SHUFFLE_SONG_RECENCY in by_key


def test_queue_config_entries_offer_global_option() -> None:
    """Each per-queue headline setting defaults to 'global'; global-only knobs are absent."""
    by_key = _by_key(queue_config_entries(_mass(similar_tracks=True, smart_fades=True)))
    for key in (
        CONF_SMART_SHUFFLE_ENABLED,
        CONF_VOLUME_NORMALIZATION,
        CONF_CROSSFADE_MODE,
        CONF_AUTOPLAY_MODE,
    ):
        entry = by_key[key]
        assert entry.default_value == CONF_VALUE_GLOBAL
        assert CONF_VALUE_GLOBAL in {opt.value for opt in entry.options}
    # crossfade duration and the recency windows are global-only (not overridable per queue)
    assert CONF_CROSSFADE_DURATION not in by_key
    assert CONF_SMART_SHUFFLE_SONG_RECENCY not in by_key
    # the crossfade/autoplay runtime toggles are global-only too: there is no per-queue setting
    assert CONF_CROSSFADE_ENABLED not in by_key
    assert CONF_AUTOPLAY_ENABLED not in by_key


def test_queue_config_smart_crossfade_unavailable() -> None:
    """Without smart fades the smart option is disabled and standard is the global default."""
    mass = _mass(similar_tracks=True, smart_fades=False)
    core = _by_key(core_config_entries(mass))
    assert core[CONF_CROSSFADE_MODE].default_value == CrossfadeMode.STANDARD_CROSSFADE.value
    queue_crossfade = _by_key(queue_config_entries(mass))[CONF_CROSSFADE_MODE]
    smart = next(
        opt for opt in queue_crossfade.options if opt.value == CrossfadeMode.SMART_CROSSFADE.value
    )
    assert smart.disabled is True


def test_queue_config_smart_crossfade_available() -> None:
    """With smart fades available, smart crossfade is the global default and stays enabled."""
    mass = _mass(similar_tracks=True, smart_fades=True)
    core = _by_key(core_config_entries(mass))
    assert core[CONF_CROSSFADE_MODE].default_value == CrossfadeMode.SMART_CROSSFADE.value
    queue_crossfade = _by_key(queue_config_entries(mass))[CONF_CROSSFADE_MODE]
    smart = next(
        opt for opt in queue_crossfade.options if opt.value == CrossfadeMode.SMART_CROSSFADE.value
    )
    assert smart.disabled is False


def test_queue_config_similar_autoplay_disabled_without_provider() -> None:
    """The 'similar' autoplay option is disabled when no provider can supply similar tracks."""
    entries = queue_config_entries(_mass(similar_tracks=False, smart_fades=True))
    autoplay = next(entry for entry in entries if entry.key == CONF_AUTOPLAY_MODE)
    similar = next(opt for opt in autoplay.options if opt.value == "similar")
    assert similar.disabled is True
