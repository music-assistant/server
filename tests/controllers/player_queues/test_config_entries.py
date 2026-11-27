"""Tests for the player-queues config-entry builders (config.py)."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING
from unittest.mock import Mock, patch

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.config_entries import ProviderAccess
from music_assistant_models.enums import (
    ConfigEntryType,
    CrossfadeMode,
    ProviderFeature,
    ProviderSharing,
    ProviderType,
)

from music_assistant.constants import (
    CONF_CROSSFADE_DURATION,
    CONF_CROSSFADE_MODE,
    CONF_VALUE_DISABLED,
    CONF_VALUE_ENABLED,
    CONF_VALUE_GLOBAL,
    CONF_VOLUME_NORMALIZATION,
)
from music_assistant.controllers.music import MusicController
from music_assistant.controllers.player_queues.config import (
    CATEGORY_AUTOPLAY,
    CATEGORY_CLICK_ACTIONS,
    CATEGORY_CROSSFADE,
    CATEGORY_DEFAULT_ENQUEUE_OPTION,
    CATEGORY_ITEMS_TO_SELECT,
    CATEGORY_PLAYBACK,
    CATEGORY_SMART_SHUFFLE,
    CATEGORY_VOLUME_NORMALIZATION,
    core_config_entries,
    queue_config_entries,
)
from music_assistant.controllers.player_queues.constants import (
    CLICK_ACTION_BROWSE,
    CLICK_ACTION_PLAY,
    CONF_AUTOPLAY_MODE,
    CONF_DEFAULT_CLICK_ACTION_ALBUM,
    CONF_DEFAULT_CLICK_ACTION_ARTIST,
    CONF_DEFAULT_CLICK_ACTION_GENRE,
    CONF_DEFAULT_CLICK_ACTION_PLAYLIST,
    CONF_DEFAULT_CLICK_ACTION_RADIO,
    CONF_DEFAULT_CLICK_ACTION_TRACK,
    CONF_DEFAULT_ENQUEUE_OPTION_ALBUM,
    CONF_DEFAULT_ENQUEUE_OPTION_ARTIST,
    CONF_DEFAULT_ENQUEUE_OPTION_GENRE,
    CONF_DEFAULT_ENQUEUE_OPTION_LIVE_SOURCES,
    CONF_DEFAULT_ENQUEUE_OPTION_PLAYLIST,
    CONF_DEFAULT_ENQUEUE_OPTION_TRACK,
    CONF_DEFAULT_ENQUEUE_ORDER_ARTIST,
    CONF_DEFAULT_ENQUEUE_SELECT_ALBUM,
    CONF_DEFAULT_ENQUEUE_SELECT_ARTIST,
    CONF_DEFAULT_PLAY_ACTION_ALBUM_TRACK,
    CONF_DEFAULT_PLAY_ACTION_PLAYLIST_TRACK,
    CONF_PAUSE_TIMEOUT,
    CONF_SMART_SHUFFLE_ARTIST_RECENCY,
    CONF_SMART_SHUFFLE_DUPLICATE_GAP,
    CONF_SMART_SHUFFLE_ENABLED,
    CONF_SMART_SHUFFLE_OPTIMIZE_SMART_FADES,
    CONF_SMART_SHUFFLE_SONG_RECENCY,
    PLAY_ACTION_PLAY_FROM_HERE,
    PLAY_ACTION_PLAY_TRACK,
)
from tests.common import set_music_source_access

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry

    from music_assistant.mass import MusicAssistant


def _mass(*, similar_tracks: bool, smart_fades: bool) -> Mock:
    """Build a mock MusicAssistant with the provider/smart-fades signals the builder reads."""
    provider = Mock()
    provider.supported_features = {ProviderFeature.SIMILAR_TRACKS} if similar_tracks else set()
    mass = Mock()
    mass.music.providers = [provider]
    mass.get_providers_supporting_feature.side_effect = lambda feature: (
        [provider] if feature in provider.supported_features else []
    )
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


def test_core_config_entries_artist_selection() -> None:
    """An artist prefers its library tracks by default; no selection lists its whole catalogue."""
    entry = _by_key(core_config_entries(_mass(similar_tracks=True, smart_fades=True)))[
        CONF_DEFAULT_ENQUEUE_SELECT_ARTIST
    ]
    assert entry.default_value == "prefer_library"
    assert [option.value for option in entry.options] == [
        "top_tracks",
        "library_tracks",
        "prefer_library",
        "library_album_tracks",
    ]


def test_core_config_entries_artist_order() -> None:
    """An artist plays as shuffled tracks by default, with whole albums as the alternatives."""
    entry = _by_key(core_config_entries(_mass(similar_tracks=True, smart_fades=True)))[
        CONF_DEFAULT_ENQUEUE_ORDER_ARTIST
    ]
    assert entry.default_value == "shuffled_tracks"
    assert [option.value for option in entry.options] == [
        "shuffled_tracks",
        "random_albums",
        "albums_by_release",
    ]
    assert entry.category == CATEGORY_ITEMS_TO_SELECT


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
    # The ordering option is opt-in by default.
    smart_fades_ordering = by_key[CONF_SMART_SHUFFLE_OPTIMIZE_SMART_FADES]
    assert smart_fades_ordering.default_value == CONF_VALUE_DISABLED
    assert {opt.value for opt in smart_fades_ordering.options} == {
        CONF_VALUE_ENABLED,
        CONF_VALUE_DISABLED,
    }
    volume_normalization = by_key[CONF_VOLUME_NORMALIZATION]
    assert volume_normalization.default_value == CONF_VALUE_ENABLED
    assert CONF_VALUE_GLOBAL not in {opt.value for opt in volume_normalization.options}
    # the global-only settings live only on the core schema
    assert CONF_CROSSFADE_DURATION in by_key
    assert CONF_SMART_SHUFFLE_SONG_RECENCY in by_key
    pause_timeout = by_key[CONF_PAUSE_TIMEOUT]
    assert pause_timeout.default_value == 30
    assert pause_timeout.range == (30, 240)
    assert pause_timeout.category == CATEGORY_PLAYBACK
    assert pause_timeout.advanced


def test_core_config_entries_section_order() -> None:
    """The sections come in the order clients show them, with what a click does first."""
    entries = core_config_entries(_mass(similar_tracks=True, smart_fades=True))
    assert list(dict.fromkeys(entry.category for entry in entries)) == [
        CATEGORY_CLICK_ACTIONS,
        CATEGORY_DEFAULT_ENQUEUE_OPTION,
        CATEGORY_ITEMS_TO_SELECT,
        CATEGORY_SMART_SHUFFLE,
        CATEGORY_AUTOPLAY,
        CATEGORY_CROSSFADE,
        CATEGORY_VOLUME_NORMALIZATION,
        CATEGORY_PLAYBACK,
    ]


@pytest.mark.parametrize("per_queue", [False, True], ids=["global", "queue"])
def test_config_entries_have_no_text_only_entries(per_queue: bool) -> None:
    """Neither schema carries a text-only entry: each explanation is in a setting's help text."""
    mass = _mass(similar_tracks=True, smart_fades=True)
    entries = queue_config_entries(mass) if per_queue else core_config_entries(mass)
    assert [entry.key for entry in entries if entry.type == ConfigEntryType.LABEL] == []


def test_config_entries_fine_tuning_is_advanced() -> None:
    """Fine-tuning only shows with the advanced settings; the main settings always show."""
    mass = _mass(similar_tracks=True, smart_fades=True)
    core = core_config_entries(mass)
    assert {entry.key for entry in core if entry.advanced and not entry.hidden} == {
        CONF_DEFAULT_ENQUEUE_OPTION_ARTIST,
        CONF_DEFAULT_ENQUEUE_OPTION_ALBUM,
        CONF_DEFAULT_ENQUEUE_OPTION_TRACK,
        CONF_DEFAULT_ENQUEUE_OPTION_GENRE,
        CONF_DEFAULT_ENQUEUE_OPTION_LIVE_SOURCES,
        CONF_DEFAULT_ENQUEUE_OPTION_PLAYLIST,
        CONF_DEFAULT_ENQUEUE_SELECT_ARTIST,
        CONF_DEFAULT_ENQUEUE_SELECT_ALBUM,
        CONF_SMART_SHUFFLE_OPTIMIZE_SMART_FADES,
        CONF_SMART_SHUFFLE_SONG_RECENCY,
        CONF_SMART_SHUFFLE_ARTIST_RECENCY,
        CONF_SMART_SHUFFLE_DUPLICATE_GAP,
        CONF_PAUSE_TIMEOUT,
    }
    queue = queue_config_entries(mass)
    assert {entry.key for entry in queue if entry.advanced} == {
        CONF_SMART_SHUFFLE_OPTIMIZE_SMART_FADES
    }


def test_queue_config_entries_offer_global_option() -> None:
    """Each per-queue headline setting defaults to 'global'; global-only knobs are absent."""
    by_key = _by_key(queue_config_entries(_mass(similar_tracks=True, smart_fades=True)))
    for key in (
        CONF_SMART_SHUFFLE_ENABLED,
        CONF_SMART_SHUFFLE_OPTIMIZE_SMART_FADES,
        CONF_VOLUME_NORMALIZATION,
        CONF_CROSSFADE_MODE,
        CONF_AUTOPLAY_MODE,
    ):
        entry = by_key[key]
        assert entry.default_value == CONF_VALUE_GLOBAL
        assert CONF_VALUE_GLOBAL in {opt.value for opt in entry.options}
    # crossfade duration, the recency windows and the pause timeout are global-only (not
    # overridable per queue)
    assert CONF_CROSSFADE_DURATION not in by_key
    assert CONF_SMART_SHUFFLE_SONG_RECENCY not in by_key
    assert CONF_PAUSE_TIMEOUT not in by_key


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


@pytest.mark.parametrize("per_queue", [False, True], ids=["global", "queue"])
def test_similar_autoplay_disabled_without_provider(per_queue: bool) -> None:
    """Both similarity options are disabled when no provider supports either feature."""
    mass = _mass(similar_tracks=False, smart_fades=True)
    entries = queue_config_entries(mass) if per_queue else core_config_entries(mass)
    autoplay = next(entry for entry in entries if entry.key == CONF_AUTOPLAY_MODE)
    for mode in ("similar", "similar_artists"):
        similar = next(opt for opt in autoplay.options if opt.value == mode)
        assert similar.disabled is True


@pytest.mark.parametrize("per_queue", [False, True], ids=["global", "queue"])
@pytest.mark.parametrize(
    ("feature", "mode"),
    [
        (ProviderFeature.SIMILAR_TRACKS, "similar"),
        (ProviderFeature.SIMILAR_ARTISTS, "similar_artists"),
    ],
)
@pytest.mark.parametrize(
    ("provider_type", "available", "supports_feature", "expected"),
    [
        (ProviderType.METADATA, True, True, True),
        (ProviderType.MUSIC, True, True, True),
        (ProviderType.PLUGIN, True, True, True),
        (ProviderType.METADATA, False, True, False),
        (ProviderType.MUSIC, False, True, False),
        (ProviderType.PLUGIN, False, True, False),
        (ProviderType.METADATA, True, False, False),
        (ProviderType.MUSIC, True, False, False),
        (ProviderType.PLUGIN, True, False, False),
        (ProviderType.PLAYER, True, True, False),
    ],
)
async def test_similar_autoplay_provider_availability(
    mass_minimal: MusicAssistant,
    per_queue: bool,
    provider_type: ProviderType,
    available: bool,
    feature: ProviderFeature,
    mode: str,
    supports_feature: bool,
    expected: bool,
) -> None:
    """Both schemas detect available, eligible providers separately for each similarity feature."""
    mass = mass_minimal
    mass.music = MusicController(mass)
    mass.streams = Mock(smart_fades_available=False)
    mass._providers = {
        "search": Mock(
            type=ProviderType.MUSIC,
            instance_id="search",
            available=True,
            supported_features={ProviderFeature.SEARCH},
            priority=50,
        ),
        "similar": Mock(
            type=provider_type,
            instance_id="similar",
            available=available,
            supported_features={feature} if supports_feature else {ProviderFeature.SEARCH},
            priority=50,
        ),
    }

    entries = queue_config_entries(mass) if per_queue else core_config_entries(mass)
    autoplay = _by_key(entries)[CONF_AUTOPLAY_MODE]
    similar = next(opt for opt in autoplay.options if opt.value == mode)
    assert similar.disabled is (not expected)
    other = next(
        opt for opt in autoplay.options if opt.value in {"similar", "similar_artists"} - {mode}
    )
    assert other.disabled is True


@pytest.mark.parametrize("per_queue", [False, True], ids=["global", "queue"])
@pytest.mark.parametrize("visible", [False, True], ids=["hidden", "visible"])
@pytest.mark.parametrize(
    ("feature", "mode"),
    [
        (ProviderFeature.SIMILAR_TRACKS, "similar"),
        (ProviderFeature.SIMILAR_ARTISTS, "similar_artists"),
    ],
)
async def test_similar_autoplay_uses_server_provider_capabilities(
    mass_minimal: MusicAssistant,
    per_queue: bool,
    visible: bool,
    feature: ProviderFeature,
    mode: str,
) -> None:
    """Similarity availability reflects server capabilities regardless of music source visibility."""
    mass = mass_minimal
    mass.music = MusicController(mass)
    mass.streams = Mock(smart_fades_available=False)
    mass._providers = {
        "similar": Mock(
            type=ProviderType.MUSIC,
            instance_id="similar",
            available=True,
            supported_features={feature},
            priority=50,
        )
    }
    set_music_source_access(
        mass,
        {"similar": None if visible else ProviderAccess(sharing=ProviderSharing.PRIVATE)},
    )
    user = User(user_id="listener", username="listener", role=UserRole.USER)

    with patch("music_assistant.controllers.music.controller.get_current_user", return_value=user):
        entries = queue_config_entries(mass) if per_queue else core_config_entries(mass)

    autoplay = _by_key(entries)[CONF_AUTOPLAY_MODE]
    similar = next(opt for opt in autoplay.options if opt.value == mode)
    assert similar.disabled is False


def test_queue_config_similar_artists_autoplay_needs_a_provider() -> None:
    """The 'similar artists' autoplay option is offered when a provider has similar artists."""
    mass = _mass(similar_tracks=True, smart_fades=True)
    autoplay = _by_key(queue_config_entries(mass))[CONF_AUTOPLAY_MODE]
    option = next(opt for opt in autoplay.options if opt.value == "similar_artists")
    assert option.disabled is True

    mass.music.providers[0].supported_features = {ProviderFeature.SIMILAR_ARTISTS}
    autoplay = _by_key(queue_config_entries(mass))[CONF_AUTOPLAY_MODE]
    option = next(opt for opt in autoplay.options if opt.value == "similar_artists")
    assert option.disabled is False
