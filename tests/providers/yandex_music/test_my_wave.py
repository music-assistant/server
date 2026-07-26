"""Tests for My Wave (Моя волна) browse and rotor feedback helpers."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import ProviderMapping
from music_assistant_models.media_items import Track as MATrack

from music_assistant.providers.yandex_music.constants import (
    CONF_ACTION_DELETE_WAVE_PRESET,
    CONF_ACTION_SAVE_WAVE_PRESET,
    RADIO_TRACK_ID_SEP,
    ROTOR_STATION_MY_WAVE,
)
from music_assistant.providers.yandex_music.parsers import parse_playlist
from music_assistant.providers.yandex_music.provider import (
    YandexMusicProvider,
    _parse_radio_item_id,
    _WaveState,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType


def test_parse_radio_item_id_plain_track_id() -> None:
    """Plain track_id returns (track_id, None)."""
    assert _parse_radio_item_id("12345") == ("12345", None)
    assert _parse_radio_item_id("0") == ("0", None)


def test_parse_radio_item_id_composite() -> None:
    """Composite track_id@station_id returns (track_id, station_id)."""
    assert _parse_radio_item_id(f"12345{RADIO_TRACK_ID_SEP}{ROTOR_STATION_MY_WAVE}") == (
        "12345",
        ROTOR_STATION_MY_WAVE,
    )
    assert _parse_radio_item_id("99@user:custom") == ("99", "user:custom")


def test_wave_state_has_session_fields() -> None:
    """_WaveState exposes session_id, playlist_next_cursor, prefetched, settings."""
    state = _WaveState()
    # Session-based rotor API identifiers
    assert state.session_id is None
    # Legacy stations-based identifier retained during migration
    assert state.batch_id is None
    # Pagination cursor for virtual playlist pages
    assert state.playlist_next_cursor is None
    # Prefetch buffer for future-batch tracks
    assert state.prefetched == []
    # Persistent station settings (diversity/moodEnergy/language)
    assert state.settings == {}
    # Once-per-session flag
    assert state.radio_started_sent is False


def test_wave_state_is_per_instance_isolated() -> None:
    """Each _WaveState has its own mutable containers (no shared class state)."""
    a, b = _WaveState(), _WaveState()
    a.seen_track_ids.add("1")
    a.prefetched.append("x")
    a.settings["diversity"] = "discover"
    assert b.seen_track_ids == set()
    assert b.prefetched == []
    assert b.settings == {}


# -- _fetch_rotor_session_batch (session-API helper) --------------------------


@pytest.mark.asyncio
async def test_fetch_rotor_session_batch_starts_session_on_first_call() -> None:
    """First call creates a rotor session and records session_id + batch_id on wave state."""
    provider = Mock(spec=YandexMusicProvider)
    provider.client = AsyncMock()
    provider.client.rotor_session_new = AsyncMock(
        return_value=("sess_1", ["track1", "track2"], "batch_a")
    )
    provider.client.rotor_session_tracks = AsyncMock()
    wave = _WaveState()

    tracks, batch_id = await YandexMusicProvider._fetch_rotor_session_batch(
        provider, wave, ROTOR_STATION_MY_WAVE
    )

    provider.client.rotor_session_new.assert_awaited_once_with(ROTOR_STATION_MY_WAVE, settings=None)
    provider.client.rotor_session_tracks.assert_not_awaited()
    assert wave.session_id == "sess_1"
    assert wave.batch_id == "batch_a"
    assert tracks == ["track1", "track2"]
    assert batch_id == "batch_a"


@pytest.mark.asyncio
async def test_fetch_rotor_session_batch_passes_wave_settings_to_session_new() -> None:
    """Session creation forwards wave.settings (diversity/moodEnergy/language) as seeds."""
    provider = Mock(spec=YandexMusicProvider)
    provider.client = AsyncMock()
    provider.client.rotor_session_new = AsyncMock(return_value=("s", [], "b"))
    wave = _WaveState()
    wave.settings = {"diversity": "discover", "moodEnergy": "calm"}

    await YandexMusicProvider._fetch_rotor_session_batch(provider, wave, ROTOR_STATION_MY_WAVE)

    _, kwargs = provider.client.rotor_session_new.await_args
    assert kwargs["settings"] == {"diversity": "discover", "moodEnergy": "calm"}


@pytest.mark.asyncio
async def test_fetch_rotor_session_batch_paginates_via_session_tracks_after_first_call() -> None:
    """Once session_id is set, subsequent calls use rotor_session_tracks with last_track_id."""
    provider = Mock(spec=YandexMusicProvider)
    provider.client = AsyncMock()
    provider.client.rotor_session_new = AsyncMock()
    provider.client.rotor_session_tracks = AsyncMock(return_value=(["t3"], "batch_b"))
    wave = _WaveState()
    wave.session_id = "sess_1"
    wave.last_track_id = "42"

    tracks, _batch_id = await YandexMusicProvider._fetch_rotor_session_batch(
        provider, wave, ROTOR_STATION_MY_WAVE
    )

    provider.client.rotor_session_new.assert_not_awaited()
    provider.client.rotor_session_tracks.assert_awaited_once_with("sess_1", current_track_id="42")
    assert wave.batch_id == "batch_b"
    assert tracks == ["t3"]


@pytest.mark.asyncio
async def test_fetch_rotor_session_batch_returns_empty_when_session_new_fails() -> None:
    """When session creation returns None session_id, wave is not mutated and result is empty."""
    provider = Mock(spec=YandexMusicProvider)
    provider.client = AsyncMock()
    provider.client.rotor_session_new = AsyncMock(return_value=(None, [], None))
    wave = _WaveState()

    tracks, batch_id = await YandexMusicProvider._fetch_rotor_session_batch(
        provider, wave, ROTOR_STATION_MY_WAVE
    )

    assert wave.session_id is None
    assert tracks == []
    assert batch_id is None


@pytest.mark.asyncio
async def test_fetch_rotor_session_batch_works_with_track_seed_station() -> None:
    """get_similar_tracks uses station 'track:{id}' — same session machinery."""
    provider = Mock(spec=YandexMusicProvider)
    provider.client = AsyncMock()
    provider.client.rotor_session_new = AsyncMock(return_value=("s", ["t"], "b"))
    wave = _WaveState()

    await YandexMusicProvider._fetch_rotor_session_batch(provider, wave, "track:9999")

    provider.client.rotor_session_new.assert_awaited_once_with("track:9999", settings=None)
    assert wave.session_id == "s"


# -- ynison compatibility wrapper ---------------------------------------------


@pytest.mark.asyncio
async def test_get_rotor_station_tracks_wrapper_delegates_to_session_batch() -> None:
    """
    Ynison-facing wrapper routes through _fetch_rotor_session_batch.

    This keeps ynison on the session API (long-lived radioSessionId, shared
    wave state, prefetch) without any code change on its side — the
    ``(tracks, batch_id)`` shape stays the same.
    """
    wave = _WaveState()
    provider = Mock(spec=YandexMusicProvider)
    provider._get_wave_state = Mock(return_value=wave)
    provider._fetch_rotor_session_batch = AsyncMock(return_value=(["t1", "t2"], "batch_1"))

    tracks, batch_id = await YandexMusicProvider.get_rotor_station_tracks(
        provider, "genre:rock", queue=None
    )

    provider._get_wave_state.assert_called_once_with("genre:rock")
    provider._fetch_rotor_session_batch.assert_awaited_once_with(wave, "genre:rock")
    assert tracks == ["t1", "t2"]
    assert batch_id == "batch_1"


@pytest.mark.asyncio
async def test_get_rotor_station_tracks_wrapper_records_queue_as_cursor() -> None:
    """Ynison's queue= arg becomes wave.last_track_id so the next call paginates."""
    wave = _WaveState()
    provider = Mock(spec=YandexMusicProvider)
    provider._get_wave_state = Mock(return_value=wave)
    provider._fetch_rotor_session_batch = AsyncMock(return_value=([], None))

    await YandexMusicProvider.get_rotor_station_tracks(provider, "mood:calm", queue="42")

    assert wave.last_track_id == "42"
    provider._fetch_rotor_session_batch.assert_awaited_once_with(wave, "mood:calm")


# -- wave-mode preset routing -------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_rotor_session_batch_resolves_wave_mode_preset_settings() -> None:
    """A station key like 'user:onyourwave#discover' translates to settingDiversity=discover."""
    provider = Mock(spec=YandexMusicProvider)
    provider.client = AsyncMock()
    provider.client.rotor_session_new = AsyncMock(return_value=("sess_1", [], "batch_a"))
    wave = _WaveState()

    await YandexMusicProvider._fetch_rotor_session_batch(
        provider, wave, f"{ROTOR_STATION_MY_WAVE}#discover"
    )

    provider.client.rotor_session_new.assert_awaited_once_with(
        ROTOR_STATION_MY_WAVE, settings={"diversity": "discover"}
    )
    assert wave.session_id == "sess_1"


@pytest.mark.asyncio
async def test_fetch_rotor_session_batch_preset_merges_with_explicit_wave_settings() -> None:
    """Explicit wave.settings overrides preset settings on the same key."""
    provider = Mock(spec=YandexMusicProvider)
    provider.client = AsyncMock()
    provider.client.rotor_session_new = AsyncMock(return_value=("s", [], "b"))
    wave = _WaveState()
    wave.settings = {"diversity": "popular"}  # overrides preset

    await YandexMusicProvider._fetch_rotor_session_batch(
        provider, wave, f"{ROTOR_STATION_MY_WAVE}#discover"
    )

    _, kwargs = provider.client.rotor_session_new.await_args
    # wave.settings wins over preset
    assert kwargs["settings"] == {"diversity": "popular"}


@pytest.mark.asyncio
async def test_fetch_rotor_session_batch_unknown_preset_strips_suffix_no_settings() -> None:
    """Unknown '#<x>' suffix is stripped from the station key and no extra settings are sent."""
    provider = Mock(spec=YandexMusicProvider)
    provider.client = AsyncMock()
    provider.client.rotor_session_new = AsyncMock(return_value=(None, [], None))
    wave = _WaveState()

    await YandexMusicProvider._fetch_rotor_session_batch(
        provider, wave, f"{ROTOR_STATION_MY_WAVE}#does_not_exist"
    )

    # Base station is used; unknown preset yields empty settings → settings=None.
    provider.client.rotor_session_new.assert_awaited_once_with(ROTOR_STATION_MY_WAVE, settings=None)


# -- _parse_my_wave_track with explicit station_key --------------------------


# -- prefetch next batch (P6) -------------------------------------------------


@pytest.mark.asyncio
async def test_prefetch_rotor_session_fills_prefetched_when_idle() -> None:
    """With an active session + cursor and no prefetched tracks, fills wave.prefetched."""
    provider = Mock(spec=YandexMusicProvider)
    provider.client = AsyncMock()
    provider.client.rotor_session_tracks = AsyncMock(return_value=(["t1", "t2"], "batch_b"))
    wave = _WaveState()
    wave.session_id = "sess_1"
    wave.last_track_id = "42"
    provider._wave_states = {ROTOR_STATION_MY_WAVE: wave}

    await YandexMusicProvider._prefetch_rotor_session(provider, ROTOR_STATION_MY_WAVE)

    provider.client.rotor_session_tracks.assert_awaited_once_with("sess_1", current_track_id="42")
    assert wave.prefetched == ["t1", "t2"]


@pytest.mark.asyncio
async def test_prefetch_rotor_session_noop_without_session() -> None:
    """Prefetch does nothing when the station has no active session_id."""
    provider = Mock(spec=YandexMusicProvider)
    provider.client = AsyncMock()
    provider.client.rotor_session_tracks = AsyncMock()
    wave = _WaveState()
    provider._wave_states = {ROTOR_STATION_MY_WAVE: wave}

    await YandexMusicProvider._prefetch_rotor_session(provider, ROTOR_STATION_MY_WAVE)

    provider.client.rotor_session_tracks.assert_not_awaited()
    assert wave.prefetched == []


@pytest.mark.asyncio
async def test_prefetch_rotor_session_noop_without_cursor() -> None:
    """Prefetch bails when session exists but no last_track_id cursor yet."""
    provider = Mock(spec=YandexMusicProvider)
    provider.client = AsyncMock()
    provider.client.rotor_session_tracks = AsyncMock()
    wave = _WaveState()
    wave.session_id = "sess_1"  # but last_track_id still None
    provider._wave_states = {ROTOR_STATION_MY_WAVE: wave}

    await YandexMusicProvider._prefetch_rotor_session(provider, ROTOR_STATION_MY_WAVE)

    provider.client.rotor_session_tracks.assert_not_awaited()
    assert wave.prefetched == []


@pytest.mark.asyncio
async def test_prefetch_rotor_session_noop_when_already_prefilled() -> None:
    """Prefetch skips work when wave.prefetched already has items (avoid rate burn)."""
    provider = Mock(spec=YandexMusicProvider)
    provider.client = AsyncMock()
    provider.client.rotor_session_tracks = AsyncMock()
    wave = _WaveState()
    wave.session_id = "sess_1"
    wave.last_track_id = "42"
    wave.prefetched = ["existing_track"]
    provider._wave_states = {ROTOR_STATION_MY_WAVE: wave}

    await YandexMusicProvider._prefetch_rotor_session(provider, ROTOR_STATION_MY_WAVE)

    provider.client.rotor_session_tracks.assert_not_awaited()


# -- rotor feedback on library_add (P5) ---------------------------------------


@pytest.mark.asyncio
async def test_library_add_track_from_wave_also_sends_rotor_like() -> None:
    """library_add for a track from a wave session sends both users.like and rotor.like."""
    provider = Mock(spec=YandexMusicProvider)
    provider.instance_id = "yandex_music_instance"
    provider.logger = Mock()
    provider.client = AsyncMock()
    provider.client.like_track = AsyncMock(return_value=True)
    composite = f"12345{RADIO_TRACK_ID_SEP}{ROTOR_STATION_MY_WAVE}"
    provider._get_provider_item_id = Mock(return_value=composite)
    # Share a session so like is routed to rotor_session_feedback
    wave = _WaveState()
    wave.session_id = "sess_1"
    wave.batch_id = "batch_a"
    provider._wave_states = {ROTOR_STATION_MY_WAVE: wave}
    provider._get_wave_state = Mock(return_value=wave)
    provider._send_wave_feedback = AsyncMock(return_value=True)

    item = MATrack(
        item_id=composite,
        provider="yandex_music_instance",
        name="Test",
        provider_mappings={
            ProviderMapping(
                item_id=composite,
                provider_domain="yandex_music",
                provider_instance="yandex_music_instance",
            )
        },
    )
    item.media_type = MediaType.TRACK

    result = await YandexMusicProvider.library_add(provider, item)

    assert result is True
    provider.client.like_track.assert_awaited_once_with("12345")
    provider._send_wave_feedback.assert_awaited_once()
    args, kwargs = provider._send_wave_feedback.await_args
    assert args[0] is wave
    assert args[1] == ROTOR_STATION_MY_WAVE
    assert args[2] == "like"
    assert kwargs == {"track_id": "12345"}


@pytest.mark.asyncio
async def test_library_add_track_without_station_skips_rotor_feedback() -> None:
    """Plain track_id (no station suffix) does NOT trigger rotor feedback."""
    provider = Mock(spec=YandexMusicProvider)
    provider.instance_id = "yandex_music_instance"
    provider.logger = Mock()
    provider.client = AsyncMock()
    provider.client.like_track = AsyncMock(return_value=True)
    provider._get_provider_item_id = Mock(return_value="12345")
    provider._send_wave_feedback = AsyncMock()

    item = MATrack(
        item_id="12345",
        provider="yandex_music_instance",
        name="Test",
        provider_mappings={
            ProviderMapping(
                item_id="12345",
                provider_domain="yandex_music",
                provider_instance="yandex_music_instance",
            )
        },
    )
    item.media_type = MediaType.TRACK

    await YandexMusicProvider.library_add(provider, item)

    provider.client.like_track.assert_awaited_once_with("12345")
    provider._send_wave_feedback.assert_not_awaited()


# -- user wave presets (P8) ---------------------------------------------------


def _preset_config(values: dict[str, str]) -> Mock:
    """
    Build a config stub whose get_value looks up keys in the given dict.

    Non-listed keys return None, matching MA's ``ConfigValueType | None`` contract.
    """
    config = Mock()
    config.get_value = Mock(side_effect=values.get)
    return config


def test_get_user_wave_presets_decodes_stored_json() -> None:
    """A valid JSON list in CONF_WAVE_PRESETS_DATA yields the same presets out."""
    provider = Mock(spec=YandexMusicProvider)
    provider.config = _preset_config(
        {
            "wave_presets_data": (
                '[{"name": "Morning", "diversity": "discover", "moodEnergy": "calm"},'
                ' {"name": "Evening", "language": "russian"}]'
            ),
        }
    )
    provider.logger = Mock()

    result = YandexMusicProvider._get_user_wave_presets(provider)

    assert result == [
        {"name": "Morning", "diversity": "discover", "moodEnergy": "calm"},
        {"name": "Evening", "language": "russian"},
    ]


def test_get_user_wave_presets_empty_store_returns_empty() -> None:
    """No stored data / empty string / None → empty list."""
    provider = Mock(spec=YandexMusicProvider)
    provider.config = _preset_config({"wave_presets_data": ""})
    provider.logger = Mock()

    assert YandexMusicProvider._get_user_wave_presets(provider) == []


def test_get_user_wave_presets_invalid_json_returns_empty() -> None:
    """Malformed JSON → empty list (silent; matches the settings-UI parser)."""
    provider = Mock(spec=YandexMusicProvider)
    provider.config = _preset_config({"wave_presets_data": "not-json {{{"})
    provider.logger = Mock()

    assert YandexMusicProvider._get_user_wave_presets(provider) == []


def test_get_user_wave_presets_skips_items_without_name() -> None:
    """Entries missing a name or with non-string values are silently skipped."""
    provider = Mock(spec=YandexMusicProvider)
    provider.config = _preset_config(
        {
            "wave_presets_data": (
                '[{"diversity": "discover"}, {"name": ""}, '
                '{"name": "Good", "moodEnergy": "active"}]'
            ),
        }
    )
    provider.logger = Mock()

    assert YandexMusicProvider._get_user_wave_presets(provider) == [
        {"name": "Good", "moodEnergy": "active"},
    ]


def test_get_user_wave_presets_drops_whitespace_only_values() -> None:
    """
    Whitespace-only dropdown values (e.g. hand-edited JSON) are treated as empty.

    Yandex rejects ``settingDiversity:`` with a 4xx, so the parser must not
    propagate such values. Valid values are also stripped to their canonical
    form so the downstream rotor seed builder always gets the stored string
    without surrounding whitespace.
    """
    provider = Mock(spec=YandexMusicProvider)
    provider.config = _preset_config(
        {
            "wave_presets_data": (
                '[{"name": "WS-only", "diversity": "   ",'
                ' "moodEnergy": "\\t", "language": ""},'
                ' {"name": "Trim", "diversity": "  discover  "}]'
            ),
        }
    )
    provider.logger = Mock()

    assert YandexMusicProvider._get_user_wave_presets(provider) == [
        {"name": "WS-only"},
        {"name": "Trim", "diversity": "discover"},
    ]


# -- save / delete preset actions --------------------------------------------


def _action_provider(values: dict[str, ConfigValueType]) -> Mock:
    """
    Build a provider stub whose config reads/writes go through *values*.

    Mirrors how ``handle_config_action`` reads draft/preset fields via
    ``get_config_value`` and persists results via ``_update_config_value``.
    """
    provider = Mock(spec=YandexMusicProvider)
    provider.get_config_value = Mock(
        side_effect=lambda key, default=None, **_kw: values.get(key, default)
    )

    def _update(key: str, value: ConfigValueType, **_kw: object) -> None:
        values[key] = value

    provider._update_config_value = Mock(side_effect=_update)
    provider.get_config_entries = AsyncMock(return_value=())
    return provider


async def test_save_wave_preset_action_appends_and_clears_draft() -> None:
    """Save action writes the draft into JSON storage and clears draft fields."""
    values: dict[str, ConfigValueType] = {
        "wave_preset_draft_name": "Morning",
        "wave_preset_draft_diversity": "discover",
        "wave_preset_draft_mood": "calm",
        "wave_preset_draft_language": "",  # "default" dropdown → skipped
        "wave_presets_data": "",
    }

    await YandexMusicProvider.handle_config_action(
        _action_provider(values), CONF_ACTION_SAVE_WAVE_PRESET
    )

    stored_raw = values["wave_presets_data"]
    assert isinstance(stored_raw, str)
    assert json.loads(stored_raw) == [
        {"name": "Morning", "diversity": "discover", "moodEnergy": "calm"},
    ]
    assert values["wave_preset_draft_name"] is None
    assert values["wave_preset_draft_diversity"] == ""
    assert values["wave_preset_draft_mood"] == ""
    assert values["wave_preset_draft_language"] == ""


async def test_save_wave_preset_action_overwrites_same_name() -> None:
    """Saving with an existing name replaces the prior entry — no duplicates."""
    values: dict[str, ConfigValueType] = {
        "wave_preset_draft_name": "Morning",
        "wave_preset_draft_diversity": "favorite",
        "wave_preset_draft_mood": "",
        "wave_preset_draft_language": "",
        "wave_presets_data": (
            '[{"name": "Morning", "diversity": "discover"},'
            ' {"name": "Evening", "language": "russian"}]'
        ),
    }

    await YandexMusicProvider.handle_config_action(
        _action_provider(values), CONF_ACTION_SAVE_WAVE_PRESET
    )

    stored_raw = values["wave_presets_data"]
    assert isinstance(stored_raw, str)
    stored = json.loads(stored_raw)
    assert {p["name"] for p in stored} == {"Morning", "Evening"}
    morning = next(p for p in stored if p["name"] == "Morning")
    assert morning == {"name": "Morning", "diversity": "favorite"}


async def test_save_wave_preset_action_rejects_blank_name() -> None:
    """Save without a preset name raises InvalidDataError and changes nothing."""
    values: dict[str, ConfigValueType] = {
        "wave_preset_draft_name": "   ",
        "wave_presets_data": "",
    }

    with pytest.raises(InvalidDataError):
        await YandexMusicProvider.handle_config_action(
            _action_provider(values), CONF_ACTION_SAVE_WAVE_PRESET
        )
    assert values["wave_presets_data"] == ""


async def test_delete_wave_preset_action_removes_by_name() -> None:
    """Delete action drops the selected preset and clears the selector."""
    values: dict[str, ConfigValueType] = {
        "wave_preset_to_delete": "Morning",
        "wave_presets_data": (
            '[{"name": "Morning", "diversity": "discover"},'
            ' {"name": "Evening", "language": "russian"}]'
        ),
    }

    await YandexMusicProvider.handle_config_action(
        _action_provider(values), CONF_ACTION_DELETE_WAVE_PRESET
    )

    stored_raw = values["wave_presets_data"]
    assert isinstance(stored_raw, str)
    assert json.loads(stored_raw) == [{"name": "Evening", "language": "russian"}]
    assert values["wave_preset_to_delete"] == ""


async def test_delete_wave_preset_action_requires_selection() -> None:
    """No selection → InvalidDataError; storage untouched."""
    values: dict[str, ConfigValueType] = {
        "wave_preset_to_delete": "",
        "wave_presets_data": '[{"name": "Keep"}]',
    }

    with pytest.raises(InvalidDataError):
        await YandexMusicProvider.handle_config_action(
            _action_provider(values), CONF_ACTION_DELETE_WAVE_PRESET
        )
    assert values["wave_presets_data"] == '[{"name": "Keep"}]'


def test_parse_playlist_is_dynamic_flag_propagates() -> None:
    """parse_playlist honours is_dynamic=True so feed autoplaylists skip MA cache."""
    provider = Mock(spec=YandexMusicProvider)
    provider.instance_id = "yandex_music_instance"
    provider.domain = "yandex_music"
    provider.client = Mock()
    provider.client.user_id = 12345

    playlist_obj = Mock()
    playlist_obj.owner = Mock(uid=67890, name="Яндекс")
    playlist_obj.kind = 42
    playlist_obj.title = "Плейлист дня"
    playlist_obj.description = None
    playlist_obj.cover = None
    playlist_obj.track_count = 50
    playlist_obj.modified = None
    playlist_obj.created = None
    playlist_obj.tags = []

    result_dynamic = parse_playlist(provider, playlist_obj, is_dynamic=True)
    result_static = parse_playlist(provider, playlist_obj)

    assert result_dynamic.is_dynamic is True
    assert result_static.is_dynamic is False


def test_parse_my_wave_track_uses_provided_station_key_for_item_id() -> None:
    """_parse_my_wave_track stamps the supplied station_key on composite item_id."""
    # Build a minimal provider instance with the attributes _parse_my_wave_track
    # reads; don't use Mock(spec=...) because we call the real method.
    provider = Mock(spec=YandexMusicProvider)
    provider.instance_id = "yandex_music_instance"
    provider.logger = Mock()

    # Fake yandex track object
    yt = type("YTrack", (), {"id": "12345", "track_id": "12345"})()

    # Return a minimal MA Track from parse_track; _parse_my_wave_track rewrites
    # its item_id in-place to the composite form.
    base_track = MATrack(
        item_id="12345",
        provider="yandex_music_instance",
        name="Test",
        provider_mappings={
            ProviderMapping(
                item_id="12345",
                provider_domain="yandex_music",
                provider_instance="yandex_music_instance",
            )
        },
    )
    with patch(
        "music_assistant.providers.yandex_music.provider.parse_track",
        return_value=base_track,
    ):
        station_key = f"{ROTOR_STATION_MY_WAVE}#discover"
        seen: set[str] = set()
        result = YandexMusicProvider._parse_my_wave_track(
            provider, yt, seen, station_key=station_key
        )

    assert result is not None
    assert result.item_id == f"12345{RADIO_TRACK_ID_SEP}{station_key}"
    # And round-trip via _parse_radio_item_id
    assert _parse_radio_item_id(result.item_id) == ("12345", station_key)
    assert "12345" in seen


# -- _send_wave_feedback (session vs. stations API router) ---------------------


@pytest.mark.asyncio
async def test_send_wave_feedback_uses_session_api_when_session_id_present() -> None:
    """When wave.session_id is set, feedback is routed to rotor_session_feedback."""
    provider = Mock(spec=YandexMusicProvider)
    provider.client = AsyncMock()
    provider.client.rotor_session_feedback = AsyncMock(return_value=True)
    provider.client.send_rotor_station_feedback = AsyncMock()
    wave = _WaveState()
    wave.session_id = "sess_1"
    wave.batch_id = "batch_a"

    result = await YandexMusicProvider._send_wave_feedback(
        provider, wave, "user:onyourwave", "trackStarted", track_id="100"
    )

    assert result is True
    provider.client.rotor_session_feedback.assert_awaited_once_with(
        "sess_1", "trackStarted", track_id="100", total_played_seconds=None, batch_id="batch_a"
    )
    provider.client.send_rotor_station_feedback.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_wave_feedback_skips_silently_without_session() -> None:
    """
    Without ``wave.session_id`` the call is a silent no-op returning False.

    The legacy stations-based feedback endpoint is gone (returns 404), so we
    can't usefully fall back there. Callers treat the False result as
    "signal was dropped" — history reporting via play_audio still fires.
    """
    provider = Mock(spec=YandexMusicProvider)
    provider.logger = Mock()
    provider.client = AsyncMock()
    provider.client.rotor_session_feedback = AsyncMock()
    wave = _WaveState()
    wave.batch_id = "batch_a"  # session_id still None

    result = await YandexMusicProvider._send_wave_feedback(
        provider, wave, "genre:rock", "skip", track_id="9", total_played_seconds=7
    )

    assert result is False
    provider.client.rotor_session_feedback.assert_not_awaited()
    provider.logger.debug.assert_called_once()
