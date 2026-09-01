# mypy: disable-error-code="attr-defined,unreachable,method-assign,misc,assignment,unused-ignore"
"""Tests for the YandexYnisonProvider."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import (
    ContentType,
    MediaType,
    PlaybackState,
    ProviderFeature,
    ProviderType,
    RepeatMode,
    SourceControl,
)
from music_assistant_models.errors import (
    InvalidDataError,
    LoginFailed,
    MediaNotFoundError,
    PlayerCommandFailed,
    ResourceTemporarilyUnavailable,
    RetriesExhausted,
    SetupFailedError,
    UnsupportedFeaturedException,
)
from music_assistant_models.media_items import AudioFormat, AudioSource
from music_assistant_models.streamdetails import StreamDetails
from ya_passport_auth import SecretStr

from music_assistant.controllers.streams.constants import STREAM_SLOT_PLAYBACK_WAIT_TIMEOUT
from music_assistant.helpers.throttle_retry import BYPASS_THROTTLER
from music_assistant.models.music_provider import MusicProvider, ProviderStreamLimitError
from music_assistant.providers.yandex_ynison.config_helpers import list_yandex_music_instances
from music_assistant.providers.yandex_ynison.constants import (
    CONF_ALLOW_PLAYER_SWITCH,
    CONF_DEVICE_ID,
    CONF_MASS_PLAYER_ID,
    CONF_STREAM_MODE,
    CONF_YM_INSTANCE,
    OUTPUT_AUTO,
    STREAM_MODE_MAX_QUALITY,
    STREAM_MODE_STABLE,
)
from music_assistant.providers.yandex_ynison.credential_source import YandexMusicCredentialSource
from music_assistant.providers.yandex_ynison.provider import (
    _API_MAX_RETRIES,
    _COMMAND_IDEMPOTENCY_TTL,
    AUDIO_SOURCE_ID,
    YandexYnisonProvider,
)
from music_assistant.providers.yandex_ynison.streaming import (
    PCM_LOSSLESS_PARAMS,
    PCM_LOSSY_PARAMS,
    make_pcm_format,
)
from music_assistant.providers.yandex_ynison.ynison_client import YnisonSendError, YnisonState


def _arm_play_media_recorder(provider: YandexYnisonProvider) -> list[tuple[str, str]]:
    """
    Replace `play_media` with a recorder and run `create_task` coros inline.

    Returns the list of (target_id, uri) tuples captured during the test.
    The inline create_task lets the scheduled `play_media` coroutine
    actually execute against the recorder.
    """
    calls: list[tuple[str, str]] = []

    async def _record(target_id: str, uri: str) -> None:
        calls.append((target_id, uri))

    provider.mass.player_queues.play_media = _record
    provider.mass.create_task = MagicMock(
        side_effect=lambda coro, *_a, **_kw: asyncio.get_event_loop().create_task(coro)
    )
    return calls


def _stub_attr(obj: object, name: str, value: Any) -> None:
    """Setattr that bypasses mypy method-assign and ruff B010."""
    setattr(obj, name, value)


def _set_stream_owner(
    provider: MagicMock,
    *streamdetails: MagicMock,
    instance_id: str = "yandex_music--test",
) -> None:
    """Assign one exact available Yandex Music owner to stream-details test doubles."""
    provider.instance_id = instance_id
    provider.available = True
    for details in streamdetails:
        details.provider = instance_id


def _make_mock_config(values: dict[str, Any] | None = None) -> MagicMock:
    """Create a mock ProviderConfig."""
    defaults: dict[str, Any] = {
        CONF_YM_INSTANCE: "ym-inst",
        CONF_MASS_PLAYER_ID: "player1",
        CONF_ALLOW_PLAYER_SWITCH: True,
        CONF_DEVICE_ID: "test-device-uuid",
        CONF_STREAM_MODE: STREAM_MODE_STABLE,
        "log_level": "GLOBAL",
    }
    if values:
        defaults.update(values)
    config = MagicMock()
    config.get_value.side_effect = defaults.get
    config.values = {}
    # Provider.__init__ now caches the AudioSource which serialises name into a
    # uri/sort_name — both expect real strings, not MagicMock attribute access.
    config.instance_id = "yandex_ynison_test"
    config.name = "Yandex Music Connect"
    return config


def _make_mock_mass() -> MagicMock:
    """Create a mock MusicAssistant instance."""
    mass = MagicMock()
    mass.cache_path = "/var/cache/test-cache"

    def _create_task(coro: object) -> MagicMock:
        if asyncio.iscoroutine(coro):
            coro.close()  # prevent RuntimeWarning for unawaited coroutine
        return MagicMock()

    mass.create_task = MagicMock(side_effect=_create_task)
    mass.subscribe = MagicMock(return_value=MagicMock())
    mass.get_providers = MagicMock(return_value=[])
    mass.config.set_raw_provider_config_value = MagicMock()
    mass.config.decrypt_string = MagicMock(side_effect=lambda value: value)

    # Cache — return None (miss) by default
    mass.cache.get = AsyncMock(return_value=None)
    mass.cache.set = AsyncMock()
    mass.cache.delete = AsyncMock()

    # Players
    mass.players.all_players = MagicMock(return_value=[])
    mass.players.get_player = MagicMock(return_value=None)
    mass.players.cmd_stop = AsyncMock()
    mass.players.cmd_pause = AsyncMock()
    mass.players.cmd_play = AsyncMock()
    mass.players.cmd_volume_set = AsyncMock()
    mass.players.trigger_player_update = MagicMock()
    mass.players.get_audio_source_session.return_value = MagicMock(
        playback_session_id="playback-session"
    )

    # Player queues
    mass.player_queues.play_media = AsyncMock()
    mass.player_queues.pause = AsyncMock()
    mass.player_queues.play = AsyncMock()

    # Streams — live metadata updates flow through update_stream_metadata
    mass.streams.update_stream_metadata = MagicMock()

    return mass


def _make_mock_manifest() -> MagicMock:
    """Create a mock ProviderManifest."""
    manifest = MagicMock()
    manifest.domain = "yandex_ynison"
    return manifest


def _make_provider(player_id: str = "player1") -> YandexYnisonProvider:
    """Create a YandexYnisonProvider with mock dependencies."""
    mass = _make_mock_mass()
    config = _make_mock_config({CONF_MASS_PLAYER_ID: player_id})
    manifest = _make_mock_manifest()
    return YandexYnisonProvider(mass, manifest, config, {ProviderFeature.AUDIO_SOURCE})


# ------------------------------------------------------------------
# Provider init
# ------------------------------------------------------------------


class TestProviderInit:
    """Tests for provider initialization."""

    def test_reads_setup_owned_identity_from_setup_data(self) -> None:
        """Using ordinary config must not ignore the linked account selected by setup."""
        mass = _make_mock_mass()
        mass.config.get.return_value = {
            CONF_YM_INSTANCE: "ym-primary",
            CONF_MASS_PLAYER_ID: "living-room",
        }
        config = _make_mock_config(
            {
                CONF_YM_INSTANCE: "__own__",
                CONF_MASS_PLAYER_ID: "stale-player",
            }
        )
        player = MagicMock()
        player.display_name = "Living room"
        mass.players.get_player.return_value = player

        provider = YandexYnisonProvider(
            mass,
            _make_mock_manifest(),
            config,
            {ProviderFeature.AUDIO_SOURCE},
        )

        assert provider._ym_instance_id == "ym-primary"
        assert provider._default_player_id == "living-room"
        assert provider._display_name == "Living room"

    def test_rejects_missing_or_legacy_own_source(self) -> None:
        """Keeping own-mode fallback must not let legacy credentials load silently."""
        for source in (None, "__own__"):
            mass = _make_mock_mass()
            mass.config.get.return_value = {
                CONF_YM_INSTANCE: source,
                CONF_MASS_PLAYER_ID: "living-room",
            }

            with pytest.raises(LoginFailed, match="Reconfigure this Ynison instance"):
                YandexYnisonProvider(
                    mass,
                    _make_mock_manifest(),
                    _make_mock_config(),
                    {ProviderFeature.AUDIO_SOURCE},
                )

    async def test_load_rejects_missing_connected_player(self) -> None:
        """A legacy setup without a concrete player must fail with a stable typed error."""
        mass = _make_mock_mass()
        mass.config.get.return_value = {
            CONF_YM_INSTANCE: "ym-primary",
            CONF_MASS_PLAYER_ID: None,
        }
        provider = YandexYnisonProvider(
            mass,
            _make_mock_manifest(),
            _make_mock_config(),
            {ProviderFeature.AUDIO_SOURCE},
        )
        _stub_attr(provider, "_resolve_token", AsyncMock(return_value=SecretStr("unused")))

        with pytest.raises(SetupFailedError) as err:
            await provider.handle_async_init()

        assert err.value.translation_key == "no_connected_player"

    def test_display_name_follows_live_connected_player(self) -> None:
        """The Ynison device name must be derived from the current player name."""
        provider = _make_provider("living-room")
        player = MagicMock()
        player.display_name = "Kitchen"
        provider.mass.players.get_player.return_value = player

        assert provider._display_name == "Kitchen"

    def test_display_name_uses_stored_player_name_on_cold_boot(self) -> None:
        """Provider startup before player registration still advertises its stored name."""
        provider = _make_provider("living-room")
        provider.mass.players.get_player.return_value = None
        provider.mass.config.get_raw_player_config_value.side_effect = lambda player_id, key: (
            "Stored kitchen" if (player_id, key) == ("living-room", "name") else None
        )

        assert provider._display_name == "Stored kitchen"

    async def test_player_rename_schedules_one_reload_only_for_changed_name(self) -> None:
        """A rename must refresh the snapshotted Ynison identity without reload loops."""
        provider = _make_provider("living-room")
        player = MagicMock()
        player.display_name = "Kitchen"
        provider.mass.players.get_player.return_value = player
        provider._advertised_name = "Kitchen"

        await provider._on_connected_player_event(MagicMock())
        provider.mass.call_later.assert_not_called()

        player.display_name = "Dining room"
        await provider._on_connected_player_event(MagicMock())

        provider.mass.call_later.assert_called_once_with(
            1,
            provider.mass.load_provider_config,
            provider.config,
            task_id=f"load_provider_{provider.instance_id}",
        )

    def test_audio_source_details(self) -> None:
        """AudioSource should be configured correctly."""
        provider = _make_provider()

        source = provider._audio_source
        # provider_mapping carries the audio_format in the new model
        mapping = next(iter(source.provider_mappings))
        assert mapping.audio_format.content_type == ContentType.PCM_S16LE
        assert mapping.audio_format.sample_rate == 44100
        assert mapping.audio_format.bit_depth == 16
        assert mapping.audio_format.channels == 2
        # capabilities default off until a matching Yandex Music provider links
        assert source.can_play_pause is False
        assert source.can_seek is False
        assert source.can_next_previous is False
        assert source.exclusive is True

    def test_device_id_persisted(self) -> None:
        """When no device_id in config, should generate and persist."""
        mass = _make_mock_mass()
        config = _make_mock_config({CONF_DEVICE_ID: None})
        manifest = _make_mock_manifest()

        provider = YandexYnisonProvider(mass, manifest, config, {ProviderFeature.AUDIO_SOURCE})

        # Should have generated a device ID and saved it
        mass.config.set_raw_provider_config_value.assert_called()
        assert provider._device_id  # non-empty

    def test_existing_device_id_used(self) -> None:
        """When device_id exists in config, should use it."""
        mass = _make_mock_mass()
        config = _make_mock_config({CONF_DEVICE_ID: "existing-uuid"})
        manifest = _make_mock_manifest()

        provider = YandexYnisonProvider(mass, manifest, config, {ProviderFeature.AUDIO_SOURCE})

        assert provider._device_id == "existing-uuid"

    def test_dynamic_mode_is_eligible_only_for_superb_and_auto_outputs(self) -> None:
        """Dynamic mode must not activate until all three eligibility gates pass."""
        config = _make_mock_config(
            {
                CONF_STREAM_MODE: STREAM_MODE_MAX_QUALITY,
                "output_sample_rate": OUTPUT_AUTO,
                "output_bit_depth": OUTPUT_AUTO,
            }
        )
        provider = YandexYnisonProvider(
            _make_mock_mass(), _make_mock_manifest(), config, {ProviderFeature.AUDIO_SOURCE}
        )
        ym = MagicMock()
        ym.get_quality.return_value = "superb"
        provider._yandex_provider = ym

        provider._resolve_stream_mode()

        assert provider._requested_stream_mode == STREAM_MODE_MAX_QUALITY
        assert provider._effective_stream_mode == STREAM_MODE_MAX_QUALITY

    @pytest.mark.parametrize(
        ("quality", "sample_rate", "bit_depth"),
        [
            ("balanced", OUTPUT_AUTO, OUTPUT_AUTO),
            ("superb", "96000", OUTPUT_AUTO),
            ("superb", OUTPUT_AUTO, "24"),
        ],
    )
    def test_ineligible_dynamic_mode_falls_back_to_stable_once(
        self,
        quality: str,
        sample_rate: str,
        bit_depth: str,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Incompatible dynamic settings must remain playable and warn only once."""
        config = _make_mock_config(
            {
                CONF_STREAM_MODE: STREAM_MODE_MAX_QUALITY,
                "output_sample_rate": sample_rate,
                "output_bit_depth": bit_depth,
            }
        )
        provider = YandexYnisonProvider(
            _make_mock_mass(), _make_mock_manifest(), config, {ProviderFeature.AUDIO_SOURCE}
        )
        ym = MagicMock()
        ym.get_quality.return_value = quality
        provider._yandex_provider = ym

        with caplog.at_level("WARNING"):
            provider._resolve_stream_mode()
            provider._resolve_stream_mode()

        assert provider._effective_stream_mode == STREAM_MODE_STABLE
        warnings = [
            record for record in caplog.records if "falling back to stable" in record.message
        ]
        assert len(warnings) == 1

    async def test_handle_async_init_uses_mass_http_session(self) -> None:
        """Ynison must reuse Music Assistant's managed HTTP session."""
        provider = _make_provider()
        shared_session = MagicMock()
        shared_session.closed = False
        _stub_attr(provider.mass, "http_session", shared_session)
        _stub_attr(
            provider,
            "_resolve_token",
            AsyncMock(return_value=SecretStr("test-token")),
        )

        await provider.handle_async_init()
        assert provider._ynison is not None

        with (
            patch(
                "music_assistant.providers.yandex_ynison.ynison_client.aiohttp.ClientSession",
                side_effect=AssertionError("private HTTP session created"),
            ),
            patch.object(
                provider._ynison,
                "_get_redirect_ticket",
                new_callable=AsyncMock,
                side_effect=LoginFailed("controlled stop"),
            ),
            pytest.raises(LoginFailed, match="controlled stop"),
        ):
            await provider._ynison.connect()


# ------------------------------------------------------------------
# Player selection
# ------------------------------------------------------------------


class TestPlayerSelection:
    """Tests for _get_target_player_id."""

    def test_configured_player_missing(self) -> None:
        """A missing configured player returns no target instead of choosing another one."""
        provider = _make_provider("gone-player")
        assert provider._get_target_player_id() is None

    def test_other_playing_player_is_never_selected_implicitly(self) -> None:
        """Removing Auto must prevent playback from jumping to an unrelated player."""
        provider = _make_provider("gone-player")
        other = MagicMock()
        other.player_id = "other-player"
        other.state.playback_state = PlaybackState.PLAYING
        provider.mass.players.all_players.return_value = [other]  # type: ignore[attr-defined]

        assert provider._get_target_player_id() is None

    def test_specific_player_exists(self) -> None:
        """Returns configured player when it exists."""
        provider = _make_provider("my-player")
        provider.mass.players.get_player.return_value = MagicMock()  # type: ignore[attr-defined]

        assert provider._get_target_player_id() == "my-player"

    def test_specific_player_missing(self) -> None:
        """Returns None when configured player no longer exists."""
        provider = _make_provider("gone-player")
        provider.mass.players.get_player.return_value = None  # type: ignore[attr-defined]

        assert provider._get_target_player_id() is None

    def test_active_player_takes_priority(self) -> None:
        """Active player takes priority over auto selection."""
        provider = _make_provider()
        provider._active_player_id = "active-one"
        provider.mass.players.get_player.return_value = MagicMock()  # type: ignore[attr-defined]

        assert provider._get_target_player_id() == "active-one"


# ------------------------------------------------------------------
# Source selection
# ------------------------------------------------------------------


class TestSourceSelection:
    """Tests for on_source_selected (the new PluginProvider hook)."""

    async def test_on_source_selected_sets_active(self) -> None:
        """Selecting source sets the active player and records the session id."""
        provider = _make_provider()

        await provider.on_source_selected("main", "new-player", "new-player", "session_1")
        assert provider._active_player_id == "new-player"
        assert provider._active_session_id == "session_1"

    async def test_bridge_selection_does_not_stop_its_own_queue_player(self) -> None:
        """Selecting a bridge consumer for the same owner must leave playback running."""
        provider = _make_provider()
        provider._active_player_id = "base-player"

        await provider.on_source_selected(
            "main",
            "spb_base-player",
            "base-player",
            "session_1",
        )

        provider.mass.players.cmd_stop.assert_not_awaited()
        assert provider._active_player_id == "spb_base-player"
        assert provider._in_use_by_player == "base-player"

    async def test_bridge_owner_is_allowed_when_player_switching_is_disabled(self) -> None:
        """A configured owner remains valid when its physical consumer is a bridge."""
        mass = _make_mock_mass()
        config = _make_mock_config(
            {
                CONF_ALLOW_PLAYER_SWITCH: False,
                CONF_MASS_PLAYER_ID: "base-player",
            }
        )
        provider = YandexYnisonProvider(
            mass,
            _make_mock_manifest(),
            config,
            {ProviderFeature.AUDIO_SOURCE},
        )
        mass.players.get_player.return_value = MagicMock()

        await provider.on_source_selected(
            "main",
            "spb_base-player",
            "base-player",
            "session_1",
        )
        await provider.on_source_selected(
            "main",
            "spb_base-player",
            "base-player",
            "session_2",
        )

        mass.player_queues.play_media.assert_not_awaited()
        assert provider._active_player_id == "spb_base-player"
        assert provider._in_use_by_player == "base-player"
        assert provider._active_session_id == "session_2"

    async def test_known_failure_stopping_previous_player_keeps_selection(self) -> None:
        """A typed player-command failure must not block a new source selection."""
        provider = _make_provider()
        provider._active_player_id = "old-player"
        provider.mass.players.cmd_stop = AsyncMock(side_effect=PlayerCommandFailed("stop failed"))

        await provider.on_source_selected("main", "new-player", "new-player", "session_1")

        assert provider._active_player_id == "new-player"
        assert provider._in_use_by_player == "new-player"

    async def test_unexpected_failure_stopping_previous_player_propagates(self) -> None:
        """An unexpected stop error must not be mistaken for an operational failure."""
        provider = _make_provider()
        provider._active_player_id = "old-player"
        provider.mass.players.cmd_stop = AsyncMock(side_effect=RuntimeError("bug"))

        with pytest.raises(RuntimeError, match="bug"):
            await provider.on_source_selected("main", "new-player", "new-player", "session_1")

        assert provider._active_player_id == "old-player"

    async def test_on_source_selected_switching_disabled(self) -> None:
        """Rejects source selection when player switching is disabled."""
        mass = _make_mock_mass()
        config = _make_mock_config({CONF_ALLOW_PLAYER_SWITCH: False})
        manifest = _make_mock_manifest()
        provider = YandexYnisonProvider(mass, manifest, config, {ProviderFeature.AUDIO_SOURCE})

        # Set default player
        provider._default_player_id = "default-player"
        mass.players.get_player.return_value = MagicMock()

        with pytest.raises(RuntimeError, match="Player switching is disabled"):
            await provider.on_source_selected("main", "other-player", "other-player", "session_1")

        # Should have redirected to the configured default via play_media
        mass.player_queues.play_media.assert_awaited()
        assert provider._active_player_id is None

    async def test_on_source_selected_disabled_redirect_not_repeated(self) -> None:
        """
        Repeated rejected selections must not re-issue the redirect play_media.

        When player switching is disabled and a non-target player keeps having
        the source selected (sendspin bridge / sync-group indirection re-triggers
        the stream under a player id that never equals the configured target),
        the redirect ``play_media`` must fire at most once per idempotency window.
        Otherwise every rejection re-issues the redirect, which re-triggers
        selection, producing an unbounded ``AudioError`` storm.
        """
        mass = _make_mock_mass()
        config = _make_mock_config({CONF_ALLOW_PLAYER_SWITCH: False})
        manifest = _make_mock_manifest()
        provider = YandexYnisonProvider(mass, manifest, config, {ProviderFeature.AUDIO_SOURCE})

        provider._default_player_id = "default-player"
        mass.players.get_player.return_value = MagicMock()

        for _ in range(3):
            with pytest.raises(RuntimeError, match="Player switching is disabled"):
                await provider.on_source_selected(
                    "main", "other-player", "other-player", "session_1"
                )

        # Three rejected selections, but the redirect fired only once.
        assert mass.player_queues.play_media.await_count == 1


# ------------------------------------------------------------------
# Clear active player
# ------------------------------------------------------------------


class TestClearActivePlayer:
    """Tests for _clear_active_player."""

    def test_clears_state(self) -> None:
        """Clearing active player resets state and triggers update."""
        provider = _make_provider()

        provider._active_player_id = "some-player"
        provider._in_use_by_player = "some-player"

        provider._clear_active_player()

        assert provider._active_player_id is None
        assert provider._in_use_by_player is None  # type: ignore[unreachable]
        provider.mass.players.trigger_player_update.assert_called_with("some-player")

    def test_gives_the_source_back_to_its_owner(self) -> None:
        """
        The player is told to let the source go, not just to stop.

        A session left on the player keeps it publishing Ynison as its source, so its
        own queue stays inactive and cannot be started again.
        """
        provider = _make_provider()
        provider._active_player_id = "some-player"
        provider._in_use_by_player = "some-player"
        provider.mass.players.get_audio_source_session.return_value.playback_session_id = (
            "generation-7"
        )

        provider._clear_active_player()

        provider.mass.players.deselect_source.assert_called_once_with(
            "some-player",
            provider_instance_id=provider.instance_id,
            source_id=AUDIO_SOURCE_ID,
            playback_session_id="generation-7",
        )

    def test_the_owner_is_released_not_the_consuming_player(self) -> None:
        """The session hangs off the owner, which is not who consumed the audio."""
        provider = _make_provider()
        # a protocol bridge streamed the audio on the owner's behalf
        provider._active_player_id = "spb_bridge_1"
        provider._in_use_by_player = "owner-player"

        provider._clear_active_player()

        provider.mass.players.deselect_source.assert_called_once_with(
            "owner-player",
            provider_instance_id=provider.instance_id,
            source_id=AUDIO_SOURCE_ID,
            playback_session_id="playback-session",
        )

    def test_nothing_is_released_when_the_source_was_not_in_use(self) -> None:
        """No owner means no session to give back."""
        provider = _make_provider()
        provider._active_player_id = "some-player"
        provider._in_use_by_player = None

        provider._clear_active_player()

        provider.mass.players.deselect_source.assert_not_called()

    def test_missing_generation_is_forwarded_as_guarded_noop(self) -> None:
        """The controller receives an empty generation instead of an unscoped release."""
        provider = _make_provider()
        provider._active_player_id = "some-player"
        provider._in_use_by_player = "some-player"
        provider.mass.players.get_audio_source_session.return_value = None

        provider._clear_active_player()

        provider.mass.players.deselect_source.assert_called_once_with(
            "some-player",
            provider_instance_id=provider.instance_id,
            source_id=AUDIO_SOURCE_ID,
            playback_session_id=None,
        )


# ------------------------------------------------------------------
# Provider matching
# ------------------------------------------------------------------


class TestProviderMatching:
    """Tests for _check_yandex_provider_match."""

    async def test_finds_yandex_music_provider(self) -> None:
        """Links to Yandex Music provider and enables playback control."""
        provider = _make_provider()

        mock_ym = MagicMock()
        mock_ym.instance_id = "ym-inst"
        mock_ym.domain = "yandex_music"
        mock_ym.type = ProviderType.MUSIC
        provider.mass.get_providers.return_value = [mock_ym]  # type: ignore[attr-defined]

        await provider._check_yandex_provider_match()

        assert provider._yandex_provider is mock_ym
        # Capability flags rebuilt on the AudioSource when a matching provider links
        assert provider._audio_source.can_play_pause is True
        assert provider._audio_source.can_seek is True
        assert provider._audio_source.can_next_previous is True

    async def test_no_matching_provider(self) -> None:
        """No linked provider disables playback control."""
        provider = _make_provider()

        provider.mass.get_providers.return_value = []  # type: ignore[attr-defined]
        await provider._check_yandex_provider_match()

        assert provider._yandex_provider is None
        assert provider._audio_source.can_play_pause is False


# ------------------------------------------------------------------
# Ynison state handling
# ------------------------------------------------------------------


class TestYnisonStateHandling:
    """Tests for _handle_ynison_state."""

    async def test_activates_on_our_device(self) -> None:
        """Activates playback when Ynison reports our device as active."""
        provider = _make_provider()

        # Setup a target player
        player = MagicMock()
        player.player_id = "player1"
        player.display_name = "Player 1"
        provider.mass.players.all_players.return_value = [player]  # type: ignore[attr-defined]
        provider.mass.players.get_player.return_value = player  # type: ignore[attr-defined]

        state = YnisonState(
            active_device_id=provider._device_id,
            player_state={
                "status": {"paused": False, "progress_ms": 5000, "duration_ms": 200000},
                "player_queue": {
                    "current_playable_index": 0,
                    "playable_list": [{"playable_id": "track1"}],
                },
            },
        )

        await provider._handle_ynison_state(state)

        assert provider._active_player_id == "player1"

    async def test_clears_on_device_switch(self) -> None:
        """Clears active player when device switches away."""
        provider = _make_provider()

        provider._active_player_id = "player1"
        provider._in_use_by_player = "player1"

        state = YnisonState(active_device_id="other-device-id")
        await provider._handle_ynison_state(state)

        assert provider._active_player_id is None
        assert provider._in_use_by_player is None  # type: ignore[unreachable]

    async def test_seek_detected_from_ynison(self) -> None:
        """Detects seek from Yandex app via progress drift."""
        provider = _make_provider()

        player = MagicMock()
        player.player_id = "player1"
        provider.mass.players.all_players.return_value = [player]  # type: ignore[attr-defined]
        provider.mass.players.get_player.return_value = player  # type: ignore[attr-defined]

        def _make_state(progress_ms: int) -> YnisonState:
            return YnisonState(
                active_device_id=provider._device_id,
                player_state={
                    "status": {
                        "paused": False,
                        "progress_ms": progress_ms,
                        "duration_ms": 200000,
                    },
                    "player_queue": {
                        "current_playable_index": 0,
                        "playable_list": [{"playable_id": "track1"}],
                    },
                },
            )

        # First state — track starts at 0ms
        await provider._handle_ynison_state(_make_state(0))
        assert provider._current_streaming_track_id == "track1"  # set eagerly on detection

        # Expire the grace period so the seek detection isn't suppressed
        provider._seek_grace_until = 0.0

        # Second state — seek to 60s (drift 60000ms > 2000ms)
        await provider._handle_ynison_state(_make_state(60000))
        assert provider._seek_position_ms == 60000
        assert provider._track_changed_event.is_set()

        # Verify force_update=True was used so the server sends a full
        # PLAYER_UPDATED event (not just a lightweight elapsed-time one)
        provider.mass.players.trigger_player_update.assert_called_with("player1", force_update=True)  # type: ignore[attr-defined]

    async def test_seek_grace_period_after_track_change(self) -> None:
        """Seek detection is suppressed during grace period after track change."""
        provider = _make_provider()

        player = MagicMock()
        player.player_id = "player1"
        provider.mass.players.all_players.return_value = [player]  # type: ignore[attr-defined]
        provider.mass.players.get_player.return_value = player  # type: ignore[attr-defined]

        def _make_state(progress_ms: int) -> YnisonState:
            return YnisonState(
                active_device_id=provider._device_id,
                player_state={
                    "status": {
                        "paused": False,
                        "progress_ms": progress_ms,
                        "duration_ms": 200000,
                    },
                    "player_queue": {
                        "current_playable_index": 0,
                        "playable_list": [{"playable_id": "track1"}],
                    },
                },
            )

        # Track starts — sets grace period
        await provider._handle_ynison_state(_make_state(0))
        assert provider._seek_grace_until > 0

        # Echo with progress=0 arrives during grace period — should NOT
        # trigger seek even though drift calculation would exceed threshold
        provider._track_changed_event.clear()
        await provider._handle_ynison_state(_make_state(0))
        assert provider._seek_position_ms == 0  # unchanged
        assert not provider._track_changed_event.is_set()  # no false seek

    async def test_progress_throttled_update(self) -> None:
        """Regular progress updates trigger player update with throttling."""
        provider = _make_provider()

        player = MagicMock()
        player.player_id = "player1"
        provider.mass.players.all_players.return_value = [player]  # type: ignore[attr-defined]
        provider.mass.players.get_player.return_value = player  # type: ignore[attr-defined]

        state = YnisonState(
            active_device_id=provider._device_id,
            player_state={
                "status": {
                    "paused": False,
                    "progress_ms": 5000,
                    "duration_ms": 200000,
                },
                "player_queue": {
                    "current_playable_index": 0,
                    "playable_list": [{"playable_id": "track1"}],
                },
            },
        )

        # First call — significant (new track) → always triggers
        await provider._handle_ynison_state(state)
        call_count_1 = provider.mass.players.trigger_player_update.call_count  # type: ignore[attr-defined]

        # Simulate same track still playing (no seek, no track change).
        # Mark as echo so the seek-detection branch stays quiet.
        state2 = YnisonState(
            active_device_id=provider._device_id,
            player_state={
                "status": {
                    "paused": False,
                    "progress_ms": 6000,
                    "duration_ms": 200000,
                },
                "player_queue": {
                    "current_playable_index": 0,
                    "playable_list": [{"playable_id": "track1"}],
                },
            },
            last_update_is_echo=True,
        )

        # Second call shortly after — throttled, no trigger
        await provider._handle_ynison_state(state2)
        call_count_2 = provider.mass.players.trigger_player_update.call_count  # type: ignore[attr-defined]

        # Force the throttle to expire
        provider._last_player_update_time = 0.0
        await provider._handle_ynison_state(state2)
        call_count_3 = provider.mass.players.trigger_player_update.call_count  # type: ignore[attr-defined]

        # First call triggered, second was throttled, third triggered
        assert call_count_1 >= 1
        assert call_count_2 == call_count_1
        assert call_count_3 > call_count_2

        # Regular (non-seek) updates should NOT use force_update
        provider.mass.players.trigger_player_update.assert_called_with(  # type: ignore[attr-defined]
            "player1", force_update=False
        )

    async def test_duration_updated_from_stream_details(self) -> None:
        """Duration is updated from stream_details and pushed to Ynison."""
        provider = _make_provider()
        # trigger_player_update needs the actual player id (bridge), not the
        # queue id — bridge players (`spb_*`) wrap the bare ALSA UUID and the
        # MA UI's state machine lives on the bridge.
        provider._active_player_id = "spb_bridge1"
        provider._in_use_by_player = "player1"
        mock_ynison = MagicMock()
        mock_ynison.update_playing_status = AsyncMock()
        mock_ynison.state.is_paused = False
        provider._ynison = mock_ynison

        stream_details = MagicMock()
        stream_details.duration = 185  # seconds

        await provider._update_metadata_from_stream(stream_details, seek_ms=30000)

        # live track-change info lives on _stream_metadata (pushed through
        # streamdetails.stream_metadata), not on the AudioSource MediaItem
        meta = provider._stream_metadata
        assert meta.duration == 185
        assert meta.elapsed_time == 30  # 30000ms → 30s
        assert provider._actual_duration_ms == 185000
        provider.mass.players.trigger_player_update.assert_called_once_with(  # type: ignore[attr-defined]
            "spb_bridge1", force_update=True
        )
        # Real duration pushed to Ynison (heartbeat — no `strict`).
        mock_ynison.update_playing_status.assert_awaited_once_with(
            progress_ms=30000, duration_ms=185000, paused=False, strict=False
        )

    async def test_signal_track_completion_advances_index(self) -> None:
        """Track completion advances index and reports status."""
        provider = _make_provider()
        mock_ynison = MagicMock()
        mock_ynison.state = YnisonState(
            active_device_id=provider._device_id,
            player_state={
                "status": {"paused": False, "progress_ms": 180000, "duration_ms": 200000},
                "player_queue": {
                    "current_playable_index": 0,
                    "playable_list": [{"playable_id": "t1"}, {"playable_id": "t2"}],
                    "entity_id": "playlist:123",
                    "entity_type": "PLAYLIST",
                },
            },
        )
        mock_ynison.update_playing_status = AsyncMock()
        mock_ynison.update_player_state = AsyncMock()
        provider._ynison = mock_ynison

        await provider._signal_track_completion()

        # 1. Reports progress=duration (`strict=True` — end-of-track signal).
        mock_ynison.update_playing_status.assert_awaited_once_with(
            progress_ms=200000, duration_ms=200000, paused=False, strict=True
        )
        # 2. Advances current_playable_index by 1
        call_args = mock_ynison.update_player_state.call_args
        sent_state = call_args.kwargs["player_state"]
        assert sent_state["player_queue"]["current_playable_index"] == 1
        assert sent_state["status"]["progress_ms"] == "0"
        assert sent_state["status"]["paused"] is False
        # Resets actual duration for next track
        assert provider._actual_duration_ms == 0

    async def test_repeat_one_restarts_current_track(self) -> None:
        """Natural completion under repeat-one restarts the same queue item."""
        provider = _make_provider()
        mock_ynison = MagicMock()
        mock_ynison.connected = True
        mock_ynison.device_id = provider._device_id
        mock_ynison.state = YnisonState(
            active_device_id=provider._device_id,
            player_state={
                "status": {"paused": False, "progress_ms": 200000, "duration_ms": 200000},
                "player_queue": {
                    "current_playable_index": 1,
                    "playable_list": [{"playable_id": "t1"}, {"playable_id": "t2"}],
                    "options": {"repeat_mode": "ONE"},
                },
            },
        )
        mock_ynison.update_playing_status = AsyncMock()
        mock_ynison.update_player_state = AsyncMock()
        provider._ynison = mock_ynison

        outcome = await provider._signal_track_completion()

        sent_state = mock_ynison.update_player_state.call_args.kwargs["player_state"]
        assert outcome == "restart"
        assert sent_state["player_queue"]["current_playable_index"] == 1
        assert sent_state["status"]["progress_ms"] == "0"

    async def test_repeat_all_wraps_in_shuffle_order(self) -> None:
        """Repeat-all wraps from the logical shuffled tail to its first item."""
        provider = _make_provider()
        mock_ynison = MagicMock()
        mock_ynison.connected = True
        mock_ynison.device_id = provider._device_id
        mock_ynison.state = YnisonState(
            active_device_id=provider._device_id,
            player_state={
                "status": {"paused": False, "progress_ms": 200000, "duration_ms": 200000},
                "player_queue": {
                    "current_playable_index": 1,
                    "playable_list": [
                        {"playable_id": "t1"},
                        {"playable_id": "t2"},
                        {"playable_id": "t3"},
                    ],
                    "shuffle_optional": {"playable_indices": [2, 0, 1]},
                    "options": {"repeat_mode": "ALL"},
                },
            },
        )
        mock_ynison.update_playing_status = AsyncMock()
        mock_ynison.update_player_state = AsyncMock()
        provider._ynison = mock_ynison

        outcome = await provider._signal_track_completion()

        sent_state = mock_ynison.update_player_state.call_args.kwargs["player_state"]
        assert outcome == "change"
        assert sent_state["player_queue"]["current_playable_index"] == 2

    async def test_explicit_next_ignores_repeat_one(self) -> None:
        """A user next command advances even when natural completion repeats one."""
        provider = _make_provider()
        mock_ynison = MagicMock()
        mock_ynison.connected = True
        mock_ynison.device_id = provider._device_id
        mock_ynison.state = YnisonState(
            active_device_id=provider._device_id,
            player_state={
                "status": {"paused": False, "progress_ms": 1000, "duration_ms": 200000},
                "player_queue": {
                    "current_playable_index": 0,
                    "playable_list": [{"playable_id": "t1"}, {"playable_id": "t2"}],
                    "options": {"repeat_mode": "ONE"},
                },
            },
        )
        mock_ynison.update_playing_status = AsyncMock()
        mock_ynison.update_player_state = AsyncMock()
        provider._ynison = mock_ynison

        await provider._on_next()

        sent_state = mock_ynison.update_player_state.call_args.kwargs["player_state"]
        assert sent_state["player_queue"]["current_playable_index"] == 1

    async def test_explicit_next_wraps_repeat_all(self) -> None:
        """A user next command wraps at the repeat-all queue boundary."""
        provider = _make_provider()
        state = _make_ynison_state(
            current_playable_index=1,
            playable_list=[{"playable_id": "t1"}, {"playable_id": "t2"}],
        )
        state.player_state["player_queue"]["options"] = {"repeat_mode": "ALL"}
        mock_ynison = _mock_ynison(state)
        provider._ynison = mock_ynison

        await provider._on_next()

        sent = mock_ynison.update_player_state.call_args.kwargs["player_state"]
        assert sent["player_queue"]["current_playable_index"] == 0

    async def test_repeat_none_publishes_terminal_pause(self) -> None:
        """Finite repeat-off completion leaves Ynison visibly paused at the end."""
        provider = _make_provider()
        state = _make_ynison_state(
            current_playable_index=0,
            playable_list=[{"playable_id": "t1"}],
            progress_ms=200000,
            duration_ms=200000,
        )
        state.player_state["player_queue"]["options"] = {"repeat_mode": "NONE"}
        mock_ynison = _mock_ynison(state)
        provider._ynison = mock_ynison

        outcome = await provider._signal_track_completion()

        assert outcome == "stop"
        assert mock_ynison.update_playing_status.await_args_list[-1].kwargs == {
            "progress_ms": 200000,
            "duration_ms": 200000,
            "paused": True,
            "strict": True,
        }

    async def test_failed_queue_advance_returns_stop(self) -> None:
        """Natural completion cannot report a transition that failed to send."""
        provider = _make_provider()
        state = _make_ynison_state(
            current_playable_index=0,
            playable_list=[{"playable_id": "t1"}, {"playable_id": "t2"}],
        )
        mock_ynison = _mock_ynison(state)
        mock_ynison.update_player_state.side_effect = YnisonSendError("down")
        provider._ynison = mock_ynison

        assert await provider._signal_track_completion() == "stop"

    async def test_signal_track_completion_no_send_full_state(self) -> None:
        """Track completion never sends full state reset."""
        provider = _make_provider()
        mock_ynison = MagicMock()
        mock_ynison.state = YnisonState(
            active_device_id=provider._device_id,
            player_state={
                "status": {"paused": False, "progress_ms": 180000, "duration_ms": 200000},
                "player_queue": {
                    "current_playable_index": 0,
                    "playable_list": [{"playable_id": "t1"}, {"playable_id": "t2"}],
                    "entity_id": "playlist:123",
                },
            },
        )
        mock_ynison.update_playing_status = AsyncMock()
        mock_ynison.update_player_state = AsyncMock()
        mock_ynison.send_full_state = AsyncMock()
        provider._ynison = mock_ynison

        await provider._signal_track_completion()

        # Must NOT send full state reset
        mock_ynison.send_full_state.assert_not_called()

    async def test_signal_track_completion_uses_actual_duration(self) -> None:
        """Track completion prefers _actual_duration_ms over stale state.duration_ms."""
        provider = _make_provider()
        provider._actual_duration_ms = 300000
        mock_ynison = MagicMock()
        mock_ynison.state = YnisonState(
            active_device_id=provider._device_id,
            player_state={
                "status": {"paused": False, "progress_ms": 180000, "duration_ms": 200000},
                "player_queue": {
                    "current_playable_index": 0,
                    "playable_list": [{"playable_id": "t1"}, {"playable_id": "t2"}],
                },
            },
        )
        mock_ynison.update_playing_status = AsyncMock()
        mock_ynison.update_player_state = AsyncMock()
        provider._ynison = mock_ynison

        await provider._signal_track_completion()

        mock_ynison.update_playing_status.assert_awaited_once_with(
            progress_ms=300000, duration_ms=300000, paused=False, strict=True
        )

    async def test_signal_track_completion_radio_replenishes_queue(self) -> None:
        """At end of RADIO queue, fetches more tracks via YM API and advances."""
        provider = _make_provider()
        mock_ynison = MagicMock()
        mock_ynison.state = YnisonState(
            active_device_id=provider._device_id,
            player_state={
                "status": {"paused": False, "progress_ms": 200000, "duration_ms": 215000},
                "player_queue": {
                    "current_playable_index": 1,
                    "playable_list": [
                        {"playable_id": "t1", "from": "radio-src"},
                        {"playable_id": "t2", "from": "radio-src"},
                    ],
                    "entity_id": "user:onyourwave",
                    "entity_type": "RADIO",
                },
            },
        )
        mock_ynison.update_playing_status = AsyncMock()
        mock_ynison.update_player_state = AsyncMock()
        provider._ynison = mock_ynison

        # Mock YM provider returning new tracks
        mock_track = MagicMock()
        mock_track.id = "t3"
        mock_track.title = "New Track"
        mock_track.albums = [MagicMock(id="a3")]
        mock_track.cover_uri = "cover3.jpg"

        mock_ym_provider = MagicMock()
        mock_ym_provider.get_rotor_station_tracks = AsyncMock(
            return_value=([mock_track], "batch-123")
        )
        provider._yandex_provider = mock_ym_provider

        await provider._signal_track_completion()

        # Fetched tracks from station
        mock_ym_provider.get_rotor_station_tracks.assert_awaited_once_with(
            "user:onyourwave", queue="t2"
        )
        # Advanced index to 2 with expanded playable_list
        call_args = mock_ynison.update_player_state.call_args
        sent_state = call_args.kwargs["player_state"]
        assert sent_state["player_queue"]["current_playable_index"] == 2
        expanded = sent_state["player_queue"]["playable_list"]
        assert len(expanded) == 3
        assert expanded[2]["playable_id"] == "t3"
        assert expanded[2]["title"] == "New Track"
        assert expanded[2]["from"] == "radio-src"

    async def test_shuffled_radio_replenishment_advances_to_new_item(self) -> None:
        """Logical shuffle tail advances to the first appended RADIO item."""
        provider = _make_provider()
        state = _make_ynison_state(
            current_playable_index=0,
            playable_list=[{"playable_id": "t1"}, {"playable_id": "t2"}],
        )
        queue = state.player_state["player_queue"]
        queue["entity_id"] = "user:wave"
        queue["entity_type"] = "RADIO"
        queue["shuffle_optional"] = {"playable_indices": [1, 0]}
        mock_ynison = _mock_ynison(state)
        provider._ynison = mock_ynison
        track = MagicMock(id="t3", title="New", albums=[], cover_uri=None)
        provider._yandex_provider = MagicMock()
        provider._yandex_provider.get_rotor_station_tracks = AsyncMock(
            return_value=([track], "batch")
        )

        assert await provider._signal_track_completion() == "change"

        sent = mock_ynison.update_player_state.call_args.kwargs["player_state"]
        assert sent["player_queue"]["current_playable_index"] == 2
        assert sent["player_queue"]["shuffle_optional"]["playable_indices"] == [1, 0, 2]

    async def test_signal_track_completion_radio_no_provider(self) -> None:
        """At end of queue without YM provider, does not crash."""
        provider = _make_provider()
        mock_ynison = MagicMock()
        mock_ynison.state = YnisonState(
            active_device_id=provider._device_id,
            player_state={
                "status": {"paused": False, "progress_ms": 200000, "duration_ms": 215000},
                "player_queue": {
                    "current_playable_index": 1,
                    "playable_list": [
                        {"playable_id": "t1"},
                        {"playable_id": "t2"},
                    ],
                    "entity_id": "user:onyourwave",
                    "entity_type": "RADIO",
                },
            },
        )
        mock_ynison.update_playing_status = AsyncMock()
        mock_ynison.update_player_state = AsyncMock()
        provider._ynison = mock_ynison
        provider._yandex_provider = None

        await provider._signal_track_completion()

        # Status reported
        mock_ynison.update_playing_status.assert_awaited_once()
        # Cannot advance — no provider to fetch tracks
        mock_ynison.update_player_state.assert_not_called()

    async def test_prefetch_on_second_to_last_track(self) -> None:
        """Pre-fetches tracks when playing second-to-last item in queue."""
        provider = _make_provider()
        mock_ynison = MagicMock()
        mock_ynison.connected = True
        mock_ynison.update_player_state = AsyncMock()
        # 4 tracks, currently at index 2 (second-to-last)
        mock_ynison.state = YnisonState(
            active_device_id=provider._device_id,
            player_state={
                "status": {"paused": False, "progress_ms": 10000, "duration_ms": 200000},
                "player_queue": {
                    "current_playable_index": 2,
                    "playable_list": [
                        {"playable_id": "t1", "from": "src"},
                        {"playable_id": "t2", "from": "src"},
                        {"playable_id": "t3", "from": "src"},
                        {"playable_id": "t4", "from": "src"},
                    ],
                    "entity_id": "user:onyourwave",
                    "entity_type": "RADIO",
                },
            },
        )
        provider._ynison = mock_ynison

        mock_track = MagicMock()
        mock_track.id = "t5"
        mock_track.title = "Prefetched"
        mock_track.albums = [MagicMock(id="a5")]
        mock_track.cover_uri = "cover5.jpg"

        mock_ym_provider = MagicMock()
        mock_ym_provider.get_rotor_station_tracks = AsyncMock(
            return_value=([mock_track], "batch-pfx")
        )
        provider._yandex_provider = mock_ym_provider

        # Use real create_task so prefetch coroutine actually runs
        provider.mass.create_task = lambda coro: asyncio.get_event_loop().create_task(coro)  # type: ignore[method-assign, assignment, misc]

        # Trigger prefetch
        provider._maybe_prefetch(
            2,
            mock_ynison.state.player_state["player_queue"]["playable_list"],
            "user:onyourwave",
            "RADIO",
        )
        assert provider._prefetch_task is not None
        await provider._prefetch_task

        # Prefetched list should contain old + new
        assert provider._prefetched_list is not None
        assert len(provider._prefetched_list) == 5
        assert provider._prefetched_list[4]["playable_id"] == "t5"

    async def test_signal_completion_uses_prefetched(self) -> None:
        """Track completion uses pre-fetched data instead of making API call."""
        provider = _make_provider()
        mock_ynison = MagicMock()
        mock_ynison.state = YnisonState(
            active_device_id=provider._device_id,
            player_state={
                "status": {"paused": False, "progress_ms": 200000, "duration_ms": 215000},
                "player_queue": {
                    "current_playable_index": 3,
                    "playable_list": [
                        {"playable_id": "t1"},
                        {"playable_id": "t2"},
                        {"playable_id": "t3"},
                        {"playable_id": "t4"},
                    ],
                    "entity_id": "user:onyourwave",
                    "entity_type": "RADIO",
                },
            },
        )
        mock_ynison.update_playing_status = AsyncMock()
        mock_ynison.update_player_state = AsyncMock()
        provider._ynison = mock_ynison

        # Simulate pre-fetched data
        prefetched = [
            {"playable_id": "t1"},
            {"playable_id": "t2"},
            {"playable_id": "t3"},
            {"playable_id": "t4"},
            {"playable_id": "t5"},
        ]
        provider._prefetched_list = prefetched

        mock_ym_provider = MagicMock()
        mock_ym_provider.get_rotor_station_tracks = AsyncMock()
        provider._yandex_provider = mock_ym_provider

        await provider._signal_track_completion()

        # Should NOT have called API — used prefetched
        mock_ym_provider.get_rotor_station_tracks.assert_not_awaited()
        # Advanced with prefetched list
        call_args = mock_ynison.update_player_state.call_args
        sent_state = call_args.kwargs["player_state"]
        assert sent_state["player_queue"]["current_playable_index"] == 4
        assert len(sent_state["player_queue"]["playable_list"]) == 5
        # Prefetch consumed
        assert provider._prefetched_list is None

    async def test_best_duration_prefers_actual(self) -> None:
        """_best_duration_ms prefers _actual_duration_ms over state.duration_ms."""
        provider = _make_provider()
        mock_ynison = MagicMock()
        mock_ynison.state = YnisonState(
            active_device_id=provider._device_id,
            player_state={
                "status": {"duration_ms": 200000},
            },
        )
        provider._ynison = mock_ynison

        # Fallback to state when actual is 0
        assert provider._best_duration_ms() == 200000

        # Prefer actual when set
        provider._actual_duration_ms = 300000
        assert provider._best_duration_ms() == 300000

        # Without ynison, only actual
        provider._ynison = None
        assert provider._best_duration_ms() == 300000
        provider._actual_duration_ms = 0
        assert provider._best_duration_ms() == 0

    async def test_wait_for_track_change_ignores_echo(self) -> None:
        """_wait_for_track_change should ignore echoes and wait for actual change."""
        provider = _make_provider()
        mock_ynison = MagicMock()
        mock_ynison.state = YnisonState(
            active_device_id=provider._device_id,
            player_state={
                "status": {"progress_ms": 248000, "duration_ms": 248000},
                "player_queue": {
                    "current_playable_index": 0,
                    "playable_list": [{"playable_id": "old_track"}],
                    "entity_id": "user:onyourwave",
                    "entity_type": "RADIO",
                },
            },
        )
        provider._ynison = mock_ynison

        async def simulate_echo_then_change() -> None:
            await asyncio.sleep(0.01)
            # First event: echo with same track (should be ignored)
            provider._track_changed_event.set()
            await asyncio.sleep(0.01)
            # Second event: actual track change
            mock_ynison.state = YnisonState(
                active_device_id=provider._device_id,
                player_state={
                    "status": {"progress_ms": 0, "duration_ms": 0},
                    "player_queue": {
                        "current_playable_index": 0,
                        "playable_list": [{"playable_id": "new_track"}],
                        "entity_id": "user:onyourwave",
                        "entity_type": "RADIO",
                    },
                },
            )
            provider._track_changed_event.set()

        task = asyncio.create_task(simulate_echo_then_change())
        result = await provider._wait_for_track_change("old_track", timeout=5.0)
        assert result is True
        await task

    async def test_wait_for_track_change_returns_immediately_if_already_advanced(
        self,
    ) -> None:
        """
        If Ynison already advanced before the call, return True without waiting.

        Regression: _wait_for_track_change used to clear _track_changed_event
        before checking state, so a state update that arrived between
        _signal_track_completion() and this method losing the signal and
        stalled for the full 30s timeout.
        """
        provider = _make_provider()
        mock_ynison = MagicMock()
        # State already shows the NEW track at the time of entry
        mock_ynison.state = YnisonState(
            active_device_id=provider._device_id,
            player_state={
                "status": {"progress_ms": 0, "duration_ms": 0},
                "player_queue": {
                    "current_playable_index": 0,
                    "playable_list": [{"playable_id": "new_track"}],
                    "entity_id": "user:onyourwave",
                    "entity_type": "RADIO",
                },
            },
        )
        provider._ynison = mock_ynison
        # Event is already set (from the _activate_playback that ran before us)
        # but pre-check in _wait_for_track_change should catch this regardless.
        provider._track_changed_event.set()

        # Tight timeout would fail if pre-check were absent — state check must
        # happen before clear()+wait().
        result = await provider._wait_for_track_change("old_track", timeout=0.1)
        assert result is True

    async def test_wait_for_track_change_timeout(self) -> None:
        """_wait_for_track_change returns False on timeout."""
        provider = _make_provider()
        mock_ynison = MagicMock()
        mock_ynison.state = YnisonState(
            active_device_id=provider._device_id,
            player_state={
                "status": {"progress_ms": 248000},
                "player_queue": {
                    "current_playable_index": 5,
                    "playable_list": [{"playable_id": "old_track"}],
                    "entity_id": "user:onyourwave",
                    "entity_type": "RADIO",
                },
            },
        )
        provider._ynison = mock_ynison

        result = await provider._wait_for_track_change("old_track", timeout=0.1)
        assert result is False

    async def test_wait_for_track_change_accepts_new_index_with_duplicate_track(self) -> None:
        """Advancement is positional when adjacent queue entries share a track ID."""
        provider = _make_provider()
        state = _make_ynison_state(
            current_playable_index=1,
            playable_list=[{"playable_id": "same"}, {"playable_id": "same"}],
        )
        provider._ynison = _mock_ynison(state)

        assert await provider._wait_for_track_change(("same", 0), timeout=0.1)


# ------------------------------------------------------------------
# ------------------------------------------------------------------
# PCM normalization (per-track ffmpeg → adaptive PCM)
# ------------------------------------------------------------------


class TestPCMNormalization:
    """Tests for per-track ffmpeg normalization to PCM."""

    async def test_stream_track_always_uses_ffmpeg(self) -> None:
        """_stream_track always normalizes through ffmpeg, even without seek."""
        provider = _make_provider()
        provider._in_use_by_player = "player1"

        mock_yandex = MagicMock()
        sd = MagicMock()
        sd.expiration = 600
        sd.duration = 200
        sd.audio_format = MagicMock()
        _set_stream_owner(mock_yandex, sd)
        mock_yandex.get_stream_details = AsyncMock(return_value=sd)

        async def _fake_audio_stream(_details: object) -> Any:
            yield b"raw-cdn-data"

        mock_yandex.get_audio_stream = _fake_audio_stream
        provider._yandex_provider = mock_yandex

        mock_ynison = MagicMock()
        mock_ynison.update_playing_status = AsyncMock()
        mock_ynison.state.is_paused = False
        provider._ynison = mock_ynison

        async def _fake_ffmpeg(**_kwargs: object) -> Any:
            yield b"pcm-normalized"

        with patch(
            "music_assistant.providers.yandex_ynison.provider.get_ffmpeg_stream",
            side_effect=_fake_ffmpeg,
        ) as mock_ffmpeg:
            collected: list[bytes] = []
            async for chunk in provider._stream_track("track:123"):
                collected.append(chunk)

        assert collected == [b"pcm-normalized"]
        mock_ffmpeg.assert_called_once()
        mock_yandex.acquire_stream_slot.assert_called_once_with(STREAM_SLOT_PLAYBACK_WAIT_TIMEOUT)
        call_kwargs = mock_ffmpeg.call_args
        # Default (no YM provider linked) → lossy profile
        assert call_kwargs.kwargs["output_format"] == provider._normalized_format
        assert call_kwargs.kwargs["output_format"].content_type == ContentType.PCM_S16LE
        # No seek args when seek_ms=0. The per-track decode no longer paces
        # itself with -re (spec 0006): MA's realtime pacer is the single pacing
        # authority, and dropping -re lets a small read-ahead absorb CDN jitter.
        args = call_kwargs.kwargs.get("extra_input_args", [])
        assert "-re" not in args
        assert "-ss" not in args

    async def test_stream_track_preserves_linked_provider_capacity_error(self) -> None:
        """A linked-provider slot timeout remains typed before the inner ffmpeg starts."""
        provider = _make_provider()
        streamdetails = MagicMock()
        streamdetails.audio_format = AudioFormat(
            content_type=ContentType.MP3,
            sample_rate=44100,
            bit_depth=16,
            channels=2,
        )
        linked_provider = MagicMock(spec=MusicProvider)
        linked_provider.max_concurrent_streams = 1
        linked_provider.name = "Yandex Music"
        linked_provider.instance_id = "yandex_music--1"
        linked_provider.available = True
        streamdetails.provider = linked_provider.instance_id
        capacity_error = ProviderStreamLimitError(
            linked_provider, STREAM_SLOT_PLAYBACK_WAIT_TIMEOUT
        )

        class _UnavailableSlot:
            async def __aenter__(self) -> None:
                raise capacity_error

            async def __aexit__(self, *_args: object) -> None:
                return None

        linked_provider.acquire_stream_slot.return_value = _UnavailableSlot()

        async def _raw_stream(_details: object) -> Any:
            yield b"raw"

        linked_provider.get_audio_stream = _raw_stream
        provider._yandex_provider = linked_provider
        _stub_attr(
            provider,
            "_get_stream_details_with_retry",
            AsyncMock(return_value=streamdetails),
        )
        _stub_attr(provider, "_update_metadata_from_stream", AsyncMock())

        with pytest.raises(ProviderStreamLimitError):
            async for _ in provider._stream_track("track:123"):
                pass

        linked_provider.acquire_stream_slot.assert_called_once_with(
            STREAM_SLOT_PLAYBACK_WAIT_TIMEOUT
        )

    async def test_stream_track_seek_adds_ss_arg(self) -> None:
        """With seek > 0, _stream_track adds -ss to ffmpeg args."""
        provider = _make_provider()
        provider._in_use_by_player = "player1"

        mock_yandex = MagicMock()
        sd = MagicMock()
        sd.expiration = 600
        sd.duration = 200
        sd.audio_format = MagicMock()
        _set_stream_owner(mock_yandex, sd)
        mock_yandex.get_stream_details = AsyncMock(return_value=sd)

        async def _fake_audio_stream(_details: object) -> Any:
            yield b"raw-data"

        mock_yandex.get_audio_stream = _fake_audio_stream
        provider._yandex_provider = mock_yandex

        mock_ynison = MagicMock()
        mock_ynison.update_playing_status = AsyncMock()
        mock_ynison.state.is_paused = False
        provider._ynison = mock_ynison

        async def _fake_ffmpeg(**_kwargs: object) -> Any:
            yield b"pcm-seeked"

        with patch(
            "music_assistant.providers.yandex_ynison.provider.get_ffmpeg_stream",
            side_effect=_fake_ffmpeg,
        ) as mock_ffmpeg:
            collected: list[bytes] = []
            async for chunk in provider._stream_track("track:123", seek_ms=5000):
                collected.append(chunk)

        assert collected == [b"pcm-seeked"]
        mock_ffmpeg.assert_called_once()
        call_kwargs = mock_ffmpeg.call_args
        args = call_kwargs.kwargs.get("extra_input_args", [])
        assert "-ss" in args
        # -re removed in spec 0006 — realtime pacer is the only pacing authority.
        assert "-re" not in args

    async def test_stream_track_logs_output_rate_and_bit_depth(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """
        The per-track stream log carries output sample rate AND bit depth.

        Spec 0006 AC6: with the passthrough fast path the declared PCM format
        IS the delivered audio, so an operator must read the output rate and
        bit depth — not just the content type — from the one stream log line
        to tell rate passthrough from a resample.
        """
        provider = _make_provider()
        provider._in_use_by_player = "player1"
        provider._normalized_params = dict(PCM_LOSSLESS_PARAMS)

        mock_yandex = MagicMock()
        sd = MagicMock()
        sd.expiration = 600
        sd.duration = 200
        sd.audio_format = MagicMock()
        _set_stream_owner(mock_yandex, sd)
        mock_yandex.get_stream_details = AsyncMock(return_value=sd)

        async def _fake_audio_stream(_details: object) -> Any:
            yield b"raw"

        mock_yandex.get_audio_stream = _fake_audio_stream
        provider._yandex_provider = mock_yandex

        mock_ynison = MagicMock()
        mock_ynison.update_playing_status = AsyncMock()
        mock_ynison.state.is_paused = False
        provider._ynison = mock_ynison

        async def _fake_ffmpeg(**_kwargs: object) -> Any:
            yield b"pcm"

        with (
            patch(
                "music_assistant.providers.yandex_ynison.provider.get_ffmpeg_stream",
                side_effect=_fake_ffmpeg,
            ),
            caplog.at_level("INFO"),
        ):
            async for _ in provider._stream_track("track:123"):
                pass

        stream_lines = [
            r.getMessage() for r in caplog.records if "Streaming track" in r.getMessage()
        ]
        assert stream_lines, "expected a 'Streaming track' log line"
        line = stream_lines[0]
        assert "44100Hz" in line
        assert "24bit" in line

    async def test_default_format_is_pcm_s16le(self) -> None:
        """Default AudioSource audio_format is PCM s16le (lossy profile)."""
        provider = _make_provider()
        mapping = next(iter(provider._audio_source.provider_mappings))
        assert mapping.audio_format.content_type == ContentType.PCM_S16LE
        assert mapping.audio_format.sample_rate == 44100
        assert mapping.audio_format.bit_depth == 16
        assert mapping.audio_format.channels == 2

    async def test_superb_quality_uses_lossless_profile(self) -> None:
        """
        When YM quality=superb and no hint, format is PCM s24le/44.1kHz.

        Spec 0006: the no-hint lossless floor is CD-rate (44.1 kHz), not
        48 kHz — a missing format hint must not upsample the common case.
        """
        provider = _make_provider()

        mock_yandex = MagicMock()
        mock_yandex.domain = "yandex_music"
        mock_yandex.type = ProviderType.MUSIC
        mock_yandex.get_quality.return_value = "superb"
        provider._yandex_provider = mock_yandex
        provider._update_normalized_format()

        mock_yandex.get_quality.assert_called_once_with()
        assert provider._normalized_format.content_type == ContentType.PCM_S24LE
        assert provider._normalized_format.sample_rate == 44100
        assert provider._normalized_format.bit_depth == 24
        # AudioSource is rebuilt with the new audio_format on the provider_mapping
        mapping = next(iter(provider._audio_source.provider_mappings))
        assert mapping.audio_format == provider._normalized_format

    async def test_balanced_quality_uses_lossy_profile(self) -> None:
        """When YM quality=balanced, format stays PCM s16le/44.1kHz."""
        provider = _make_provider()

        mock_yandex = MagicMock()
        mock_yandex.domain = "yandex_music"
        mock_yandex.type = ProviderType.MUSIC
        mock_yandex.get_quality.return_value = "balanced"
        provider._yandex_provider = mock_yandex
        provider._update_normalized_format()

        assert provider._normalized_format.content_type == ContentType.PCM_S16LE
        assert provider._normalized_format.sample_rate == 44100
        assert provider._normalized_format.bit_depth == 16

    async def test_invalid_sample_rate_override_falls_back_to_auto(self) -> None:
        """Stale/tampered output_sample_rate values fall back to auto-detected, not crash."""
        provider = _make_provider()
        provider._cfg_sample_rate = "bogus"
        provider._cfg_bit_depth = OUTPUT_AUTO

        mock_yandex = MagicMock()
        mock_yandex.domain = "yandex_music"
        mock_yandex.type = ProviderType.MUSIC
        mock_yandex.get_quality.return_value = "superb"
        provider._yandex_provider = mock_yandex
        provider._update_normalized_format()

        assert provider._normalized_format.sample_rate == 44100
        assert provider._normalized_format.bit_depth == 24
        assert provider._normalized_format.content_type == ContentType.PCM_S24LE

    async def test_invalid_bit_depth_override_falls_back_to_auto(self) -> None:
        """Off-list output_bit_depth falls back to auto base, keeping content_type consistent."""
        provider = _make_provider()
        provider._cfg_sample_rate = OUTPUT_AUTO
        # 32-bit is not offered; previously this would silently become S16LE
        provider._cfg_bit_depth = "32"

        mock_yandex = MagicMock()
        mock_yandex.domain = "yandex_music"
        mock_yandex.type = ProviderType.MUSIC
        mock_yandex.get_quality.return_value = "superb"
        provider._yandex_provider = mock_yandex
        provider._update_normalized_format()

        assert provider._normalized_format.bit_depth == 24
        assert provider._normalized_format.content_type == ContentType.PCM_S24LE

    async def test_audio_format_not_modified_by_stream(self) -> None:
        """AudioSource audio_format stays fixed (not updated from stream)."""
        provider = _make_provider()
        provider._in_use_by_player = "player1"

        mock_yandex = MagicMock()
        sd = MagicMock()
        sd.expiration = 600
        sd.duration = 200
        sd.audio_format = MagicMock()  # different format
        mock_yandex.get_stream_details = AsyncMock(return_value=sd)

        async def _fake_audio_stream(_details: object) -> Any:
            yield b"data"

        mock_yandex.get_audio_stream = _fake_audio_stream
        provider._yandex_provider = mock_yandex

        mock_ynison = MagicMock()
        mock_ynison.update_playing_status = AsyncMock()
        mock_ynison.state.is_paused = False
        provider._ynison = mock_ynison

        async def _fake_ffmpeg(**_kwargs: object) -> Any:
            yield b"pcm"

        original_format = provider._normalized_format

        with patch(
            "music_assistant.providers.yandex_ynison.provider.get_ffmpeg_stream",
            side_effect=_fake_ffmpeg,
        ):
            async for _ in provider._stream_track("track:123"):
                pass

        # AudioSource carries the format via its ProviderMapping in the new model.
        # We deliberately store a *fresh copy* per mapping (not `is original_format`)
        # so that MA's in-place ffmpeg mutations on `_normalized_format` cannot
        # leak into the mapping — but value equality must hold.
        mapping = next(iter(provider._audio_source.provider_mappings))
        assert mapping.audio_format == original_format
        assert mapping.audio_format is not original_format

    async def test_stream_track_api_error_returns_empty(self) -> None:
        """A typed stream-details failure ends the track without yielding audio."""
        provider = _make_provider()
        mock_yandex = MagicMock()
        mock_yandex.get_stream_details = AsyncMock(side_effect=MediaNotFoundError("API error"))
        provider._yandex_provider = mock_yandex

        collected: list[bytes] = []
        async for chunk in provider._stream_track("track:bad"):
            collected.append(chunk)

        assert collected == []

    async def test_stream_track_provider_unloaded_mid_stream_aborts_cleanly(
        self,
    ) -> None:
        """
        Unloaded linked provider mid-stream aborts cleanly (no AttributeError).

        Regression: the path between `await _get_stream_details_with_retry`
        and the ffmpeg stream builder used to dereference
        `self._yandex_provider` directly, racing with
        `_check_yandex_provider_match` which nulls the attribute on unload.
        """
        provider = _make_provider()
        mock_yandex = MagicMock()
        sd = MagicMock()
        sd.expiration = 600
        sd.audio_format = MagicMock()
        sd.to_dict.return_value = {"track_id": "t1"}
        sd.data = {"url": "https://cdn.example.com/audio.mp3"}
        _set_stream_owner(mock_yandex, sd)

        async def fetch_and_null(_track_id: str, _media_type: Any = None) -> Any:
            # Simulate the background unload task firing while we awaited.
            provider._yandex_provider = None
            return sd

        mock_yandex.get_stream_details = AsyncMock(side_effect=fetch_and_null)
        provider._yandex_provider = mock_yandex

        collected: list[bytes] = []
        async for chunk in provider._stream_track("t1"):
            collected.append(chunk)

        assert collected == []
        assert provider._stream_stop_event.is_set()

    async def test_stream_track_provider_switch_does_not_mix_owners(self) -> None:
        """Details from the old owner are not handed to a newly linked provider instance."""
        provider = _make_provider()
        owner_a = MagicMock()
        owner_b = MagicMock()
        streamdetails = MagicMock()
        streamdetails.expiration = 60
        streamdetails.audio_format = MagicMock()
        streamdetails.to_dict.return_value = {}
        streamdetails.data = None
        _set_stream_owner(owner_a, streamdetails, instance_id="yandex_music--a")
        _set_stream_owner(owner_b, instance_id="yandex_music--b")

        async def _fetch_and_switch(_track_id: str, _media_type: Any) -> MagicMock:
            provider._yandex_provider = owner_b
            return streamdetails

        owner_a.get_stream_details = AsyncMock(side_effect=_fetch_and_switch)
        owner_a.get_audio_stream = MagicMock()
        owner_b.get_audio_stream = MagicMock()
        provider._yandex_provider = owner_a

        output = [chunk async for chunk in provider._stream_track("track:1")]

        assert output == []
        assert provider._stream_stop_event.is_set()
        owner_a.get_audio_stream.assert_not_called()
        owner_b.get_audio_stream.assert_not_called()

    async def test_stream_track_provider_switch_during_metadata_aborts(self) -> None:
        """A linked-owner switch during metadata preparation cannot start the old source."""
        provider = _make_provider()
        owner_a = MagicMock()
        owner_b = MagicMock()
        streamdetails = MagicMock()
        streamdetails.audio_format = MagicMock()
        _set_stream_owner(owner_a, streamdetails, instance_id="yandex_music--a")
        _set_stream_owner(owner_b, instance_id="yandex_music--b")
        owner_a.get_audio_stream = MagicMock()
        provider._yandex_provider = owner_a
        _stub_attr(
            provider,
            "_get_stream_details_with_retry",
            AsyncMock(return_value=streamdetails),
        )

        async def _switch_owner(*_args: object) -> None:
            provider._yandex_provider = owner_b

        _stub_attr(provider, "_update_metadata_from_stream", AsyncMock(side_effect=_switch_owner))

        output = [chunk async for chunk in provider._stream_track("track:1")]

        assert output == []
        assert provider._stream_stop_event.is_set()
        owner_a.get_audio_stream.assert_not_called()


class TestRadioReplenishmentErrors:
    """Radio queue fallback owns typed MA failures only."""

    async def test_known_ma_error_returns_none(self) -> None:
        """A provider-reported media failure leaves the radio queue unchanged."""
        provider = _make_provider()
        provider._yandex_provider = MagicMock(available=True)
        provider._yandex_provider.get_rotor_station_tracks = AsyncMock(
            side_effect=MediaNotFoundError("missing")
        )

        result = await provider._replenish_radio_queue(
            "station1", "RADIO", [{"playable_id": "track1"}]
        )

        assert result is None

    async def test_unexpected_error_propagates(self) -> None:
        """An internal radio API error must not look like an empty station."""
        provider = _make_provider()
        provider._yandex_provider = MagicMock()
        provider._yandex_provider.get_rotor_station_tracks = AsyncMock(
            side_effect=RuntimeError("bug")
        )

        with pytest.raises(RuntimeError, match="bug"):
            await provider._replenish_radio_queue("station1", "RADIO", [{"playable_id": "track1"}])


def _make_ym_provider_stub(
    instance_id: str = "ym-inst",
    token: str | None = None,
    x_token: str | None = None,
) -> MagicMock:
    """Build a Yandex Music provider stub exposing setup-owned credentials."""
    values: dict[str, Any] = {"token": token, "x_token": x_token}
    ym_config = MagicMock()
    ym_config.get_value.side_effect = values.get
    ym = MagicMock()
    ym.instance_id = instance_id
    ym.available = True
    ym.domain = "yandex_music"
    ym.type = ProviderType.MUSIC
    ym.config = ym_config
    ym.get_setup_value.side_effect = values.get
    return ym


class TestPlayerRateSnap:
    """
    _update_normalized_format snaps the declared rate to player capability.

    Spec 0006 AC9-11: the auto rate is snapped down to the nearest sample rate
    the target player supports, so MA's AudioSource passthrough fast path is
    hit and no second resampling ffmpeg runs. Explicit overrides are never
    snapped; an unresolvable player leaves the rate as the hint/floor produced.
    """

    @staticmethod
    def _hint(sample_rate: int, bit_depth: int = 24) -> AudioFormat:
        return AudioFormat(
            content_type=ContentType.PCM_S24LE if bit_depth == 24 else ContentType.PCM_S16LE,
            sample_rate=sample_rate,
            bit_depth=bit_depth,
            channels=2,
        )

    @staticmethod
    def _link_player(provider: YandexYnisonProvider, rates: list[tuple[int, int]]) -> MagicMock:
        player = MagicMock()
        player.get_supported_sample_rates = MagicMock(return_value=rates)
        provider._active_player_id = "p1"
        provider.mass.players.get_player = MagicMock(return_value=player)  # type: ignore[attr-defined]
        return player

    async def test_hi_res_rate_snapped_down_to_supported(self) -> None:
        """A 96 kHz hint on a 48 kHz-max player declares 48 kHz (fast-path hit)."""
        provider = _make_provider()
        self._link_player(provider, [(44100, 16), (48000, 24)])
        provider._update_normalized_format(hint=self._hint(96000))
        assert provider._normalized_format.sample_rate == 48000
        # bit depth is outside the snap — it follows the hint, not the player.
        assert provider._normalized_format.bit_depth == 24

    async def test_rate_snapped_to_only_supported_value(self) -> None:
        """A 48 kHz hint on a 44.1 kHz-only player declares 44.1 kHz."""
        provider = _make_provider()
        self._link_player(provider, [(44100, 16)])
        provider._update_normalized_format(hint=self._hint(48000))
        assert provider._normalized_format.sample_rate == 44100

    async def test_supported_rate_left_untouched(self) -> None:
        """A 48 kHz hint on a player that supports 48 kHz is not snapped (AC10)."""
        provider = _make_provider()
        self._link_player(provider, [(44100, 16), (48000, 24)])
        provider._update_normalized_format(hint=self._hint(48000))
        assert provider._normalized_format.sample_rate == 48000

    async def test_no_resolvable_player_keeps_hint_rate(self) -> None:
        """With no target player the hint rate survives and nothing raises (AC10)."""
        provider = _make_provider()
        # default mock: get_player → None, all_players → [] → target player None
        provider._update_normalized_format(hint=self._hint(96000))
        assert provider._normalized_format.sample_rate == 96000

    async def test_explicit_override_beats_snap(self) -> None:
        """Explicit output_sample_rate wins over the player snap (AC11)."""
        provider = _make_provider()
        provider._cfg_sample_rate = "96000"
        self._link_player(provider, [(44100, 16), (48000, 24)])
        provider._update_normalized_format(hint=self._hint(48000))
        assert provider._normalized_format.sample_rate == 96000

    async def test_invalid_capability_data_keeps_hint_rate(self) -> None:
        """Malformed player capability data uses the selected source rate."""
        provider = _make_provider()
        player = self._link_player(provider, [])
        player.get_supported_sample_rates.side_effect = ValueError("invalid capabilities")

        provider._update_normalized_format(hint=self._hint(96000))

        assert provider._normalized_format.sample_rate == 96000

    async def test_unexpected_capability_error_propagates(self) -> None:
        """An internal capability error must not be hidden by rate fallback."""
        provider = _make_provider()
        player = self._link_player(provider, [])
        player.get_supported_sample_rates.side_effect = RuntimeError("bug")

        with pytest.raises(RuntimeError, match="bug"):
            provider._update_normalized_format(hint=self._hint(96000))


class TestResolveLinkedToken:
    """Resolve Ynison authentication from the selected Yandex Music provider."""

    async def test_uses_ym_token_when_available(self) -> None:
        """Returns the music token from the linked YM instance config."""
        provider = _make_provider()
        ym = _make_ym_provider_stub(token="ym-music-token")
        _stub_attr(provider.mass, "get_provider", MagicMock(return_value=ym))

        result = await provider._resolve_token()

        assert result.get_secret() == "ym-music-token"

    async def test_refreshes_in_memory_when_only_x_token(self) -> None:
        """Falls back to in-memory refresh via x_token; does not write config."""
        provider = _make_provider()
        ym = _make_ym_provider_stub(token=None, x_token="ym-x-token")
        _stub_attr(provider.mass, "get_provider", MagicMock(return_value=ym))

        with patch(
            "music_assistant.providers.yandex_ynison.provider.refresh_music_token",
            new_callable=AsyncMock,
            return_value=SecretStr("fresh-token"),
        ) as mock_refresh:
            result = await provider._resolve_token()

        assert result.get_secret() == "fresh-token"
        mock_refresh.assert_awaited_once()

    async def test_raises_when_ym_has_no_credentials(self) -> None:
        """Raises LoginFailed when YM instance config has neither token nor x_token."""
        provider = _make_provider()
        ym = _make_ym_provider_stub(token=None, x_token=None)
        _stub_attr(provider.mass, "get_provider", MagicMock(return_value=ym))

        with pytest.raises(LoginFailed, match="no usable token"):
            await provider._resolve_token()

    async def test_raises_when_ym_instance_unavailable(self) -> None:
        """A missing YM instance is a startup-ordering condition — transient error."""
        provider = _make_provider()
        _stub_attr(provider.mass, "get_provider", MagicMock(return_value=None))

        with pytest.raises(ResourceTemporarilyUnavailable, match="not loaded"):
            await provider._resolve_token()

    async def test_raises_when_linked_provider_is_not_yandex_music(self) -> None:
        """Stale/edited instance id pointing at a non-YM provider yields a clear error."""
        provider = _make_provider()
        provider._credential_source = YandexMusicCredentialSource(
            provider.mass,
            "some-other-id",
        )
        wrong = _make_ym_provider_stub()
        wrong.domain = "spotify"  # not yandex_music
        _stub_attr(provider.mass, "get_provider", MagicMock(return_value=wrong))

        with pytest.raises(LoginFailed, match="not a Yandex Music"):
            await provider._resolve_token()


class TestRefreshYnisonToken:
    """_refresh_ynison_token on YnisonClient auth-failure callback."""

    async def test_refreshes_from_linked_ym_x_token(self) -> None:
        """Reads x_token from linked YM and refreshes in-memory only."""
        provider = _make_provider()
        ym = _make_ym_provider_stub(token="stale", x_token="ym-x-token")
        _stub_attr(provider.mass, "get_provider", MagicMock(return_value=ym))
        # Ensure config writes are not invoked
        mock_update_config = MagicMock()
        _stub_attr(provider, "_update_config_value", mock_update_config)

        with patch(
            "music_assistant.providers.yandex_ynison.provider.refresh_music_token",
            new_callable=AsyncMock,
            return_value=SecretStr("fresh-token"),
        ) as mock_refresh:
            result = await provider._refresh_ynison_token()

        assert result.get_secret() == "fresh-token"
        mock_refresh.assert_awaited_once()
        mock_update_config.assert_not_called()

    async def test_raises_without_x_token(self) -> None:
        """Raises LoginFailed when YM has no x_token for refresh."""
        provider = _make_provider()
        ym = _make_ym_provider_stub(token="only-token", x_token=None)
        _stub_attr(provider.mass, "get_provider", MagicMock(return_value=ym))

        with pytest.raises(LoginFailed, match="Reconfigure Yandex Music authentication"):
            await provider._refresh_ynison_token()

    async def test_raises_when_ym_not_loaded(self) -> None:
        """A missing YM instance is transient on reactive refresh too."""
        provider = _make_provider()
        _stub_attr(provider.mass, "get_provider", MagicMock(return_value=None))

        with pytest.raises(ResourceTemporarilyUnavailable, match="not loaded"):
            await provider._refresh_ynison_token()


class TestYandexProviderMatch:
    """_check_yandex_provider_match obeys the linked Yandex Music instance id."""

    async def test_ignores_other_ym_instances(self) -> None:
        """Does not link to a YM instance with a different instance_id."""
        provider = _make_provider()
        provider._ym_instance_id = "wanted"
        other = _make_ym_provider_stub(instance_id="other")
        _stub_attr(provider.mass, "get_providers", MagicMock(return_value=[other]))

        await provider._check_yandex_provider_match()

        assert provider._yandex_provider is None

    async def test_matches_on_instance_id(self) -> None:
        """Links to the specific YM instance requested by config."""
        provider = _make_provider()
        provider._ym_instance_id = "wanted"
        wanted = _make_ym_provider_stub(instance_id="wanted")
        other = _make_ym_provider_stub(instance_id="other")
        _stub_attr(provider.mass, "get_providers", MagicMock(return_value=[other, wanted]))

        await provider._check_yandex_provider_match()

        assert provider._yandex_provider is wanted


# ------------------------------------------------------------------
# Yandex Music instance enumeration
# ------------------------------------------------------------------


class TestListYandexMusicInstances:
    """Tests for list_yandex_music_instances."""

    def test_returns_empty_when_none_configured(self) -> None:
        """Empty list when no yandex_music instances exist."""
        mass = _make_mock_mass()
        mass.config.get = MagicMock(return_value={})
        assert list_yandex_music_instances(mass) == []

    def test_lists_instances_with_display_name(self) -> None:
        """Returns (instance_id, display_name) pairs for yandex_music domains."""
        mass = _make_mock_mass()
        mass.config.get = MagicMock(
            return_value={
                "ym-a": {"domain": "yandex_music", "name": "Main Account"},
                "ym-b": {"domain": "yandex_music", "name": "Family"},
                "ynison-1": {"domain": "yandex_ynison", "name": "Ynison"},
            }
        )
        result = list_yandex_music_instances(mass)
        assert sorted(result) == [("ym-a", "Main Account"), ("ym-b", "Family")]

    def test_falls_back_to_instance_id_when_name_missing(self) -> None:
        """Uses instance_id as display name when 'name' is absent."""
        mass = _make_mock_mass()
        mass.config.get = MagicMock(return_value={"ym-a": {"domain": "yandex_music"}})
        result = list_yandex_music_instances(mass)
        assert result == [("ym-a", "ym-a")]


class TestPCMFrameAlignment:
    """Tests for PCM frame alignment padding in get_audio_stream."""

    async def test_frame_alignment_padding_s24le(self) -> None:
        """Verify padding math for s24le stereo (frame_size=6)."""
        provider = _make_provider()
        provider._normalized_format = make_pcm_format(PCM_LOSSLESS_PARAMS)
        fmt = provider._normalized_format
        frame_size = (fmt.bit_depth // 8) * fmt.channels
        assert frame_size == 6  # 3 bytes x 2 channels

        # 4096 bytes yielded: 4096 % 6 = 4, need 2 bytes padding
        bytes_yielded = 4096
        remainder = bytes_yielded % frame_size
        assert remainder == 4
        pad = frame_size - remainder
        assert pad == 2

    async def test_frame_alignment_padding_s16le(self) -> None:
        """Verify padding math for s16le stereo (frame_size=4)."""
        provider = _make_provider()
        provider._normalized_format = make_pcm_format(PCM_LOSSY_PARAMS)
        fmt = provider._normalized_format
        frame_size = (fmt.bit_depth // 8) * fmt.channels
        assert frame_size == 4  # 2 bytes x 2 channels

        # 4096 is already aligned to 4
        assert 4096 % frame_size == 0

        # 4097 needs 3 bytes padding
        assert 4097 % frame_size == 1
        assert frame_size - (4097 % frame_size) == 3

    async def test_no_padding_when_aligned(self) -> None:
        """No padding needed when bytes_yielded is already frame-aligned."""
        fmt = make_pcm_format(PCM_LOSSLESS_PARAMS)
        frame_size = (fmt.bit_depth // 8) * fmt.channels
        # 6000 bytes = 1000 frames of s24le stereo
        assert 6000 % frame_size == 0


# ------------------------------------------------------------------
# Playback controls
# ------------------------------------------------------------------


def _make_ynison_state(
    *,
    progress_ms: int = 5000,
    duration_ms: int = 120000,
    paused: bool = False,
    current_playable_index: int = 0,
    playable_list: list[dict[str, Any]] | None = None,
    device_id: str = "test-device-uuid",
) -> YnisonState:
    """Build a YnisonState for control-flow tests."""
    if playable_list is None:
        playable_list = [{"playable_id": "track1"}]
    return YnisonState(
        active_device_id=device_id,
        player_state={
            "status": {
                "paused": paused,
                "progress_ms": progress_ms,
                "duration_ms": duration_ms,
            },
            "player_queue": {
                "current_playable_index": current_playable_index,
                "playable_list": playable_list,
            },
        },
    )


def _mock_ynison(
    state: YnisonState | None = None,
    connected: bool = True,
    device_id: str = "test-device-uuid",
) -> MagicMock:
    """Create a mock YnisonClient with sensible defaults."""
    mock = MagicMock()
    mock.connected = connected
    mock.state = state or _make_ynison_state()
    mock.device_id = device_id
    mock.update_playing_status = AsyncMock()
    mock.update_player_state = AsyncMock()
    return mock


class TestPlaybackControls:
    """Tests for _on_play, _on_pause, _on_next, _on_previous, _on_seek."""

    @pytest.mark.parametrize("value", [True, False])
    async def test_source_control_does_not_treat_boolean_as_seek_position(
        self, value: bool
    ) -> None:
        """A shuffle-style boolean payload must not become a zero/one-second seek."""
        provider = _make_provider()
        provider._actual_duration_ms = 200000
        provider._seek_position_ms = 0
        provider._track_changed_event.clear()
        state = _make_ynison_state(progress_ms=5000, duration_ms=200000, paused=False)
        mock_ynison = _mock_ynison(state)
        provider._ynison = mock_ynison

        await provider.on_source_control(AUDIO_SOURCE_ID, SourceControl.SEEK, value)

        assert provider._seek_position_ms == 0
        assert not provider._track_changed_event.is_set()
        mock_ynison.update_playing_status.assert_not_awaited()

    async def test_source_control_normalizes_float_seek_position(self) -> None:
        """An internal fractional seek is truncated before reaching Ynison."""
        provider = _make_provider()
        provider._actual_duration_ms = 200000
        provider._seek_position_ms = 0
        state = _make_ynison_state(progress_ms=5000, duration_ms=200000, paused=False)
        mock_ynison = _mock_ynison(state)
        provider._ynison = mock_ynison

        await provider.on_source_control(
            AUDIO_SOURCE_ID,
            SourceControl.SEEK,
            cast("Any", 12.75),
        )

        assert provider._seek_position_ms == 12000
        mock_ynison.update_playing_status.assert_awaited_once_with(
            progress_ms=12000,
            duration_ms=200000,
            paused=False,
            strict=True,
        )

    async def test_source_control_sets_repeat_mode(self) -> None:
        """Music Assistant repeat control updates the Ynison queue option."""
        provider = _make_provider()
        mock_ynison = _mock_ynison()
        provider._ynison = mock_ynison
        provider._yandex_provider = MagicMock(available=True)

        await provider.on_source_control(AUDIO_SOURCE_ID, SourceControl.REPEAT, RepeatMode.ONE)

        sent = mock_ynison.update_player_state.call_args.kwargs["player_state"]
        assert sent["player_queue"]["options"]["repeat_mode"] == "ONE"

    async def test_source_control_sets_shuffle_mapping(self) -> None:
        """Music Assistant shuffle control publishes a complete index mapping."""
        provider = _make_provider()
        state = _make_ynison_state()
        state.player_state["player_queue"]["playable_list"] = [
            {"playable_id": "t1"},
            {"playable_id": "t2"},
            {"playable_id": "t3"},
        ]
        state.player_state["player_queue"]["current_playable_index"] = 1
        mock_ynison = _mock_ynison(state)
        provider._ynison = mock_ynison
        provider._yandex_provider = MagicMock(available=True)

        with patch(
            "music_assistant.providers.yandex_ynison.provider.random.sample", return_value=[2, 0]
        ):
            await provider.on_source_control(AUDIO_SOURCE_ID, SourceControl.SHUFFLE, True)

        sent = mock_ynison.update_player_state.call_args.kwargs["player_state"]
        assert sent["player_queue"]["shuffle_optional"]["playable_indices"] == [1, 2, 0]

    def test_linked_audio_source_advertises_repeat_and_shuffle(self) -> None:
        """Queue controls are exposed when the linked provider is available."""
        provider = _make_provider()
        provider._yandex_provider = MagicMock()

        source = provider._build_audio_source()

        assert source.can_repeat is True
        assert source.can_shuffle is True

        provider._yandex_provider.available = False
        source = provider._build_audio_source()
        assert source.can_repeat is False
        assert source.can_shuffle is False

    async def test_on_play_sends_progress_unpaused(self) -> None:
        """_on_play sends update_playing_status with paused=False."""
        provider = _make_provider()
        provider._actual_duration_ms = 120000
        state = _make_ynison_state(progress_ms=5000, duration_ms=120000, paused=True)
        mock_yn = _mock_ynison(state)
        provider._ynison = mock_yn

        await provider._on_play()

        mock_yn.update_playing_status.assert_awaited_once_with(
            progress_ms=5000, duration_ms=120000, paused=False, strict=True
        )

    async def test_on_play_no_ynison_raises(self) -> None:
        """_on_play raises when Ynison is not connected."""
        provider = _make_provider()
        provider._ynison = None

        with pytest.raises(UnsupportedFeaturedException):
            await provider._on_play()

    async def test_on_pause_sends_progress_paused(self) -> None:
        """_on_pause sends update_playing_status with paused=True."""
        provider = _make_provider()
        provider._actual_duration_ms = 120000
        state = _make_ynison_state(progress_ms=5000, duration_ms=120000, paused=False)
        mock_yn = _mock_ynison(state)
        provider._ynison = mock_yn

        await provider._on_pause()

        mock_yn.update_playing_status.assert_awaited_once_with(
            progress_ms=5000, duration_ms=120000, paused=True, strict=True
        )

    async def test_on_pause_no_ynison_raises(self) -> None:
        """_on_pause raises when Ynison is not connected."""
        provider = _make_provider()
        provider._ynison = None

        with pytest.raises(UnsupportedFeaturedException):
            await provider._on_pause()

    async def test_on_next_calls_signal_completion(self) -> None:
        """_on_next triggers _signal_track_completion."""
        provider = _make_provider()
        state = _make_ynison_state(
            progress_ms=180000,
            duration_ms=200000,
            playable_list=[{"playable_id": "t1"}, {"playable_id": "t2"}],
        )
        mock_yn = _mock_ynison(state)
        provider._ynison = mock_yn

        await provider._on_next()

        # Should have reported completion and advanced
        mock_yn.update_playing_status.assert_awaited_once()
        mock_yn.update_player_state.assert_awaited_once()

    async def test_on_next_no_ynison_raises(self) -> None:
        """_on_next raises when Ynison is not connected."""
        provider = _make_provider()
        provider._ynison = None

        with pytest.raises(UnsupportedFeaturedException):
            await provider._on_next()

    async def test_on_previous_decrements_index(self) -> None:
        """_on_previous decrements current_playable_index by 1."""
        provider = _make_provider()
        state = _make_ynison_state(
            current_playable_index=2,
            playable_list=[
                {"playable_id": "t1"},
                {"playable_id": "t2"},
                {"playable_id": "t3"},
            ],
        )
        mock_yn = _mock_ynison(state)
        provider._ynison = mock_yn

        await provider._on_previous()

        mock_yn.update_player_state.assert_awaited_once()
        sent = mock_yn.update_player_state.call_args.kwargs["player_state"]
        assert sent["player_queue"]["current_playable_index"] == 1

    async def test_on_previous_at_zero_no_op(self) -> None:
        """_on_previous at index 0 does nothing."""
        provider = _make_provider()
        state = _make_ynison_state(current_playable_index=0)
        mock_yn = _mock_ynison(state)
        provider._ynison = mock_yn

        await provider._on_previous()

        mock_yn.update_player_state.assert_not_called()

    async def test_on_previous_no_ynison_raises(self) -> None:
        """_on_previous raises when Ynison is not connected."""
        provider = _make_provider()
        provider._ynison = None

        with pytest.raises(UnsupportedFeaturedException):
            await provider._on_previous()

    async def test_on_seek_updates_position(self) -> None:
        """_on_seek sends progress and triggers local stream restart."""
        provider = _make_provider()
        provider._actual_duration_ms = 200000
        state = _make_ynison_state(progress_ms=5000, duration_ms=200000, paused=False)
        mock_yn = _mock_ynison(state)
        provider._ynison = mock_yn

        await provider._on_seek(30)  # 30 seconds

        assert provider._seek_position_ms == 30000
        assert provider._track_changed_event.is_set()
        mock_yn.update_playing_status.assert_awaited_once_with(
            progress_ms=30000, duration_ms=200000, paused=False, strict=True
        )

    async def test_on_seek_no_ynison_raises(self) -> None:
        """_on_seek raises when Ynison is not connected."""
        provider = _make_provider()
        provider._ynison = None

        with pytest.raises(UnsupportedFeaturedException):
            await provider._on_seek(10)


# ------------------------------------------------------------------
# _send_progress_to_ynison
# ------------------------------------------------------------------


class TestSendProgressToYnison:
    """Tests for _send_progress_to_ynison."""

    async def test_clamps_to_duration(self) -> None:
        """Progress is clamped to duration_ms."""
        provider = _make_provider()
        provider._ynison = _mock_ynison()

        await provider._send_progress_to_ynison(150000, 100000, False)

        provider._ynison.update_playing_status.assert_awaited_once_with(
            progress_ms=100000, duration_ms=100000, paused=False, strict=False
        )

    async def test_zero_duration_no_send(self) -> None:
        """Does not send when duration is 0."""
        provider = _make_provider()
        provider._ynison = _mock_ynison()

        await provider._send_progress_to_ynison(5000, 0, False)

        provider._ynison.update_playing_status.assert_not_called()

    async def test_not_connected_no_send(self) -> None:
        """Does not send when Ynison is disconnected."""
        provider = _make_provider()
        provider._ynison = _mock_ynison(connected=False)

        await provider._send_progress_to_ynison(5000, 10000, False)

        provider._ynison.update_playing_status.assert_not_called()

    async def test_no_ynison_no_send(self) -> None:
        """Does not crash when _ynison is None."""
        provider = _make_provider()
        provider._ynison = None

        await provider._send_progress_to_ynison(5000, 10000, False)
        # No assertion — just verify no crash.


# ------------------------------------------------------------------
# _pause_playback
# ------------------------------------------------------------------


class TestPausePlayback:
    """Tests for `_pause_playback` — external pause releases the player."""

    async def test_sets_stop_event_and_cmd_stops_queue(self) -> None:
        """External pause sets the stop event and calls cmd_stop on the queue id."""
        provider = _make_provider()
        provider._active_player_id = "spb_bridge1"
        provider._in_use_by_player = "player1"

        await provider._pause_playback()

        assert provider._stream_stop_event.is_set()
        provider.mass.players.cmd_stop.assert_awaited_once_with("player1")

    async def test_rewrites_active_player_id_after_successful_cmd_stop(self) -> None:
        """
        On a successful cmd_stop, `_active_player_id` demotes to the queue id.

        Queues live on the bare ALSA UUID; bridge wrappers (`spb_*`) do
        not own one. Resume's `play_media(_active_player_id, ...)`
        must target the queue id — otherwise MA raises
        `PlayerUnavailableError`. The demotion happens AFTER cmd_stop
        so a failure path leaves the bridge id intact for next attempt.
        """
        provider = _make_provider()
        provider._active_player_id = "spb_bridge1"
        provider._in_use_by_player = "player1"

        # capture _active_player_id at the moment cmd_stop is invoked —
        # must still be the bridge id (rewrite is post-success).
        captured: dict[str, str | None] = {}

        async def _capture_stop(_player_id: str) -> None:
            captured["active_player_id_at_call"] = provider._active_player_id

        provider.mass.players.cmd_stop = AsyncMock(side_effect=_capture_stop)

        await provider._pause_playback()

        assert captured["active_player_id_at_call"] == "spb_bridge1"
        assert provider._active_player_id == "player1"
        # _externally_paused flag set so the resume edge in _activate_playback can
        # detect us even if _stream_stop_event has been cleared by some
        # other code path.
        assert provider._externally_paused is True

    async def test_cmd_stop_failure_keeps_bridge_id_intact(self) -> None:
        """
        A cmd_stop failure must not demote `_active_player_id`.

        If we demoted to the queue id but cmd_stop never reached MA,
        the next `_activate_playback` would try `play_media(bare_uuid)`
        without MA ever having released the bridge — wedges the bridge
        in an inconsistent state. Better to keep the bridge id pinned
        and let the next pause attempt redo the cycle.
        """
        provider = _make_provider()
        provider._active_player_id = "spb_bridge1"
        provider._in_use_by_player = "player1"
        provider.mass.players.cmd_stop = AsyncMock(side_effect=PlayerCommandFailed("boom"))

        await provider._pause_playback()

        assert provider._active_player_id == "spb_bridge1"
        # Stream stop event still set — generator must exit even if MA
        # never confirmed; otherwise we'd serve real audio against a
        # player MA thinks is detached.
        assert provider._stream_stop_event.is_set()
        # _externally_paused stays False on the failure path — we're not in a
        # "successfully paused, expecting resume" state.
        assert provider._externally_paused is False

    async def test_unexpected_cmd_stop_error_propagates(self) -> None:
        """An unexpected player-controller error must escape the pause fallback."""
        provider = _make_provider()
        provider._active_player_id = "spb_bridge1"
        provider._in_use_by_player = "player1"
        provider.mass.players.cmd_stop = AsyncMock(side_effect=RuntimeError("bug"))

        with pytest.raises(RuntimeError, match="bug"):
            await provider._pause_playback()

    async def test_no_active_player_is_a_noop(self) -> None:
        """Pause with no active queue does not call cmd_stop or set the stop event."""
        provider = _make_provider()
        provider._active_player_id = None
        provider._in_use_by_player = None

        await provider._pause_playback()

        assert not provider._stream_stop_event.is_set()
        provider.mass.players.cmd_stop.assert_not_called()


class TestStreamTrackErrorHandling:
    """Expected MA failures end a track while unexpected failures propagate."""

    async def test_stream_details_ma_error_ends_track(self) -> None:
        """An exhausted operational lookup stops the stream without yielding audio."""
        provider = _make_provider()
        linked_provider = MagicMock()
        _set_stream_owner(linked_provider)
        provider._yandex_provider = linked_provider
        _stub_attr(
            provider,
            "_get_stream_details_with_retry",
            AsyncMock(side_effect=RetriesExhausted("unavailable")),
        )

        chunks = [chunk async for chunk in provider._stream_track("track1")]

        assert chunks == []
        assert provider._stream_stop_event.is_set()

    async def test_unexpected_stream_details_error_propagates(self) -> None:
        """An internal lookup bug must not be converted into an ordinary stream end."""
        provider = _make_provider()
        linked_provider = MagicMock()
        _set_stream_owner(linked_provider)
        provider._yandex_provider = linked_provider
        _stub_attr(
            provider,
            "_get_stream_details_with_retry",
            AsyncMock(side_effect=RuntimeError("bug")),
        )

        with pytest.raises(RuntimeError, match="bug"):
            await anext(provider._stream_track("track1"))


# ------------------------------------------------------------------
# Echo suppression via YnisonState.last_update_is_echo
# ------------------------------------------------------------------


class TestEchoSuppression:
    """Seek detection in _handle_ynison_state honours the state echo flag."""

    def _player(self, provider: YandexYnisonProvider) -> MagicMock:
        player = MagicMock()
        player.player_id = "player1"
        player.display_name = "Player 1"
        player.state.playback_state = PlaybackState.PLAYING
        provider.mass.players.all_players.return_value = [player]  # type: ignore[attr-defined]
        provider.mass.players.get_player.return_value = player  # type: ignore[attr-defined]
        return player

    async def _prime_same_track(self, provider: YandexYnisonProvider) -> None:
        """Set provider state so seek detection is the only active branch."""
        self._player(provider)
        provider._current_streaming_track_id = "track1"
        provider._active_player_id = "player1"
        provider._in_use_by_player = "player1"
        provider._streaming_progress_ms = 1000
        provider._seek_grace_until = 0.0  # grace expired

    async def test_echo_suppresses_seek_detection(self) -> None:
        """last_update_is_echo=True makes large drift ignored."""
        provider = _make_provider()
        await self._prime_same_track(provider)

        state = _make_ynison_state(progress_ms=10000)  # drift 9000ms vs 1000
        state.last_update_is_echo = True

        await provider._handle_ynison_state(state)

        assert not provider._track_changed_event.is_set()

    async def test_non_echo_triggers_seek(self) -> None:
        """last_update_is_echo=False with large drift triggers seek."""
        provider = _make_provider()
        await self._prime_same_track(provider)

        state = _make_ynison_state(progress_ms=10000)
        state.last_update_is_echo = False

        await provider._handle_ynison_state(state)

        assert provider._track_changed_event.is_set()
        assert provider._seek_position_ms == 10000

    async def test_default_echo_flag_false(self) -> None:
        """YnisonState default last_update_is_echo is False — seek still fires."""
        provider = _make_provider()
        await self._prime_same_track(provider)

        state = _make_ynison_state(progress_ms=10000)
        # No explicit override — the dataclass default is False.

        await provider._handle_ynison_state(state)

        assert provider._track_changed_event.is_set()


# ------------------------------------------------------------------
# _sync_progress
# ------------------------------------------------------------------


class TestSyncProgress:
    """Tests for _sync_progress."""

    async def test_updates_metadata_and_ynison(self) -> None:
        """Sync updates MA metadata and sends progress to Ynison."""
        provider = _make_provider()
        provider._actual_duration_ms = 200000
        provider._ynison = _mock_ynison()

        # 5 seconds of 44100Hz/16bit/2ch audio
        byte_rate = 44100 * 2 * 2
        bytes_yielded = byte_rate * 5

        await provider._sync_progress(0, bytes_yielded, "player1")

        # live progress lives on _stream_metadata, pushed through streamdetails
        assert provider._stream_metadata.elapsed_time == 5
        provider.mass.players.trigger_player_update.assert_called_with("player1")  # type: ignore[attr-defined]
        provider._ynison.update_playing_status.assert_awaited_once()

    async def test_with_seek_offset(self) -> None:
        """Seek offset is added to byte-based progress."""
        provider = _make_provider()
        provider._actual_duration_ms = 200000
        provider._ynison = _mock_ynison()

        byte_rate = 44100 * 2 * 2
        bytes_yielded = byte_rate * 2  # 2 seconds of audio
        seek_ms = 30000

        await provider._sync_progress(seek_ms, bytes_yielded, "player1")

        # 30000ms + 2000ms = 32000ms → 32s
        assert provider._stream_metadata.elapsed_time == 32
        assert provider._streaming_progress_ms == 32000

    async def test_no_player_id_skips_trigger(self) -> None:
        """When player_id is None, does not trigger player update."""
        provider = _make_provider()
        provider._actual_duration_ms = 200000
        provider._ynison = _mock_ynison()

        await provider._sync_progress(0, 0, None)

        provider.mass.players.trigger_player_update.assert_not_called()  # type: ignore[attr-defined]


# ------------------------------------------------------------------
# _bytes_to_ms
# ------------------------------------------------------------------


class TestBytesToMs:
    """Tests for _bytes_to_ms."""

    def test_16bit(self) -> None:
        """16-bit stereo 44100Hz: 176400 bytes = 1000ms."""
        provider = _make_provider()
        # Default format is 44100/16/2
        assert provider._bytes_to_ms(176400) == 1000

    def test_24bit(self) -> None:
        """24-bit stereo 48000Hz: 288000 bytes = 1000ms."""
        provider = _make_provider()
        # Explicit 48000/24 format (decoupled from the no-hint floor constant)
        # so this exercises the byte→ms math, not whatever rate the floor uses.
        provider._normalized_format = make_pcm_format(
            {
                "content_type": ContentType.PCM_S24LE,
                "sample_rate": 48000,
                "bit_depth": 24,
                "channels": 2,
            }
        )
        assert provider._bytes_to_ms(288000) == 1000

    def test_zero(self) -> None:
        """Zero bytes = zero milliseconds."""
        provider = _make_provider()
        assert provider._bytes_to_ms(0) == 0


# ------------------------------------------------------------------
# _get_stream_details_with_retry
# ------------------------------------------------------------------


@pytest.mark.asyncio
class TestGetStreamDetailsWithRetry:
    """Tests for _get_stream_details_with_retry."""

    async def test_success_first_attempt(self) -> None:
        """Returns stream details on first try and caches result."""
        provider = _make_provider()
        mock_yp = MagicMock()
        sd = MagicMock()
        sd.expiration = 600
        sd.to_dict.return_value = {"track_id": "t1"}
        sd.data = {"url": "https://cdn.example.com/audio.mp3", "decryption_key": "abc"}
        _set_stream_owner(mock_yp, sd)
        mock_yp.get_stream_details = AsyncMock(return_value=sd)
        provider._yandex_provider = mock_yp

        result = await provider._get_stream_details_with_retry("t1")
        assert result is sd
        mock_yp.get_stream_details.assert_awaited_once()
        provider.mass.cache.get.assert_awaited_once_with(  # type: ignore[attr-defined]
            "ynison_sd_yandex_music--test_t1",
            provider=provider.instance_id,
            base_class=StreamDetails,
        )
        # Verify cache.set was called with data field preserved
        provider.mass.cache.set.assert_awaited_once()  # type: ignore[attr-defined]
        assert (
            provider.mass.cache.set.call_args.args[0]  # type: ignore[attr-defined]
            == "ynison_sd_yandex_music--test_t1"
        )
        cached_value = provider.mass.cache.set.call_args[0][1]  # type: ignore[attr-defined]
        assert cached_value["data"] == sd.data

    async def test_cache_hit_skips_api(self) -> None:
        """Returns cached stream details without API call."""
        provider = _make_provider()
        cached_sd = MagicMock()
        cached_sd.expiration = 600
        provider.mass.cache.get = AsyncMock(return_value=cached_sd)  # type: ignore[method-assign]
        mock_yp = MagicMock()
        _set_stream_owner(mock_yp, cached_sd)
        mock_yp.get_stream_details = AsyncMock()
        provider._yandex_provider = mock_yp

        result = await provider._get_stream_details_with_retry("t1")
        assert result is cached_sd
        mock_yp.get_stream_details.assert_not_awaited()

    async def test_cache_owner_mismatch_is_discarded(self) -> None:
        """Cached details from another linked instance are never leased or streamed."""
        provider = _make_provider()
        cached_sd = MagicMock()
        cached_sd.provider = "yandex_music--other"
        provider.mass.cache.get = AsyncMock(return_value=cached_sd)  # type: ignore[method-assign]
        fresh_sd = MagicMock()
        fresh_sd.expiration = 60
        fresh_sd.to_dict.return_value = {}
        fresh_sd.data = None
        mock_yp = MagicMock()
        _set_stream_owner(mock_yp, fresh_sd)
        mock_yp.get_stream_details = AsyncMock(return_value=fresh_sd)
        provider._yandex_provider = mock_yp

        result = await provider._get_stream_details_with_retry("t1")

        assert result is fresh_sd
        provider.mass.cache.delete.assert_awaited_once_with(  # type: ignore[attr-defined]
            "ynison_sd_yandex_music--test_t1",
            provider=provider.instance_id,
        )
        mock_yp.get_stream_details.assert_awaited_once()

    async def test_fresh_owner_mismatch_is_not_retried(self) -> None:
        """A deterministic provider-owner violation remains actionable and immediate."""
        provider = _make_provider()
        streamdetails = MagicMock()
        streamdetails.provider = "yandex_music--other"
        mock_yp = MagicMock()
        _set_stream_owner(mock_yp)
        mock_yp.get_stream_details = AsyncMock(return_value=streamdetails)
        provider._yandex_provider = mock_yp

        with pytest.raises(InvalidDataError, match="expected yandex_music--test"):
            await provider._get_stream_details_with_retry("t1")

        mock_yp.get_stream_details.assert_awaited_once()

    async def test_retries_on_failure(self) -> None:
        """Retries on transient error, succeeds on second attempt."""
        provider = _make_provider()
        mock_yp = MagicMock()
        sd = MagicMock()
        sd.expiration = 600
        sd.to_dict.return_value = {"track_id": "t1"}
        _set_stream_owner(mock_yp, sd)
        mock_yp.get_stream_details = AsyncMock(
            side_effect=[ResourceTemporarilyUnavailable("transient"), sd]
        )
        provider._yandex_provider = mock_yp

        with patch(
            "music_assistant.providers.yandex_ynison.provider.asyncio.sleep", new_callable=AsyncMock
        ):
            result = await provider._get_stream_details_with_retry("t1")
        assert result is sd
        assert mock_yp.get_stream_details.await_count == 2

    async def test_raises_after_max_retries(self) -> None:
        """Raises RetriesExhausted after all transient retries are exhausted."""
        provider = _make_provider()
        mock_yp = MagicMock()
        _set_stream_owner(mock_yp)
        mock_yp.get_stream_details = AsyncMock(
            side_effect=ResourceTemporarilyUnavailable("always fails")
        )
        provider._yandex_provider = mock_yp

        with (
            patch(
                "music_assistant.providers.yandex_ynison.provider.asyncio.sleep",
                new_callable=AsyncMock,
            ),
            pytest.raises(RetriesExhausted, match="failed after"),
        ):
            await provider._get_stream_details_with_retry("t1")
        assert mock_yp.get_stream_details.await_count == _API_MAX_RETRIES

    async def test_permanent_error_is_not_retried(self) -> None:
        """A permanent MA error propagates without consuming the retry budget."""
        provider = _make_provider()
        mock_yp = MagicMock()
        mock_yp.get_stream_details = AsyncMock(side_effect=MediaNotFoundError("missing"))
        provider._yandex_provider = mock_yp

        with pytest.raises(MediaNotFoundError, match="missing"):
            await provider._get_stream_details_with_retry("t1")

        mock_yp.get_stream_details.assert_awaited_once()

    async def test_cancellation_not_retried(self) -> None:
        """CancelledError propagates immediately, no retry."""
        provider = _make_provider()
        mock_yp = MagicMock()
        _set_stream_owner(mock_yp)
        mock_yp.get_stream_details = AsyncMock(side_effect=asyncio.CancelledError())
        provider._yandex_provider = mock_yp

        with pytest.raises(asyncio.CancelledError):
            await provider._get_stream_details_with_retry("t1")
        mock_yp.get_stream_details.assert_awaited_once()

    async def test_unloaded_provider_raises_login_failed_not_attribute_error(
        self,
    ) -> None:
        """
        Linked yandex_music unloaded → LoginFailed, not AttributeError.

        Regression: _yandex_provider can be set to None by the background
        _check_yandex_provider_match task between awaits in this function.
        Prior code dereferenced `self._yandex_provider.get_stream_details`
        directly, raising AttributeError and hard-stopping the audio
        generator.  We now capture a local ref at entry and surface a
        clean LoginFailed instead.
        """
        provider = _make_provider()
        provider._yandex_provider = None

        with pytest.raises(LoginFailed, match="not loaded"):
            await provider._get_stream_details_with_retry("t1")


# ------------------------------------------------------------------
# _advance_queue_index
# ------------------------------------------------------------------


class TestAdvanceQueueIndex:
    """Tests for _advance_queue_index."""

    async def test_sends_state(self) -> None:
        """Advances queue index and sends new state."""
        provider = _make_provider()
        state = _make_ynison_state(
            current_playable_index=0,
            playable_list=[{"playable_id": "t1"}, {"playable_id": "t2"}],
        )
        mock_yn = _mock_ynison(state, device_id="own-device-id")
        provider._ynison = mock_yn

        await provider._advance_queue_index(3)

        mock_yn.update_player_state.assert_awaited_once()
        sent = mock_yn.update_player_state.call_args.kwargs["player_state"]
        assert sent["player_queue"]["current_playable_index"] == 3
        assert sent["status"]["progress_ms"] == "0"
        assert sent["status"]["duration_ms"] == "0"
        assert sent["status"]["paused"] is False
        # Outgoing state authored by our device_id, timestamps as strings.
        queue_version = sent["player_queue"]["version"]
        status_version = sent["status"]["version"]
        assert queue_version["device_id"] == "own-device-id"
        assert status_version["device_id"] == "own-device-id"
        assert isinstance(queue_version["version"], str)
        assert queue_version["timestamp_ms"] == "0"
        assert isinstance(status_version["version"], str)
        assert status_version["timestamp_ms"] == "0"

    async def test_with_expanded_list(self) -> None:
        """Expanded list replaces playable_list in sent state."""
        provider = _make_provider()
        state = _make_ynison_state(
            playable_list=[{"playable_id": "t1"}],
        )
        mock_yn = _mock_ynison(state, device_id="own-device-id")
        provider._ynison = mock_yn

        expanded = [{"playable_id": "t1"}, {"playable_id": "t2"}]
        await provider._advance_queue_index(1, expanded_list=expanded)

        sent = mock_yn.update_player_state.call_args.kwargs["player_state"]
        assert sent["player_queue"]["playable_list"] == expanded
        assert sent["player_queue"]["version"]["device_id"] == "own-device-id"

    async def test_not_connected_waits_then_sends(self) -> None:
        """Waits for reconnection before sending state."""
        provider = _make_provider()
        state = _make_ynison_state()
        mock_yn = _mock_ynison(state, connected=False)
        provider._ynison = mock_yn

        call_count = 0

        def _get_connected(_self: object) -> bool:
            nonlocal call_count
            call_count += 1
            # Reconnect after 2 checks
            return call_count > 2

        type(mock_yn).connected = property(_get_connected)

        await provider._advance_queue_index(1)

        mock_yn.update_player_state.assert_awaited_once()

    async def test_timeout_no_send(self) -> None:
        """Gives up after timeout when Ynison stays disconnected."""
        provider = _make_provider()
        state = _make_ynison_state()
        mock_yn = _mock_ynison(state, connected=False)
        provider._ynison = mock_yn

        # Patch asyncio.sleep to skip real waiting
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await provider._advance_queue_index(1)

        mock_yn.update_player_state.assert_not_called()

    async def test_no_ynison_returns(self) -> None:
        """Returns immediately when _ynison is None."""
        provider = _make_provider()
        provider._ynison = None

        await provider._advance_queue_index(1)
        # No crash, no calls


# ------------------------------------------------------------------
# _activate_playback
# ------------------------------------------------------------------


class TestActivatePlayback:
    """Tests for _activate_playback."""

    async def test_selects_source_on_new_player(self) -> None:
        """Selects source on target player when not yet active."""
        provider = _make_provider()
        provider._active_player_id = None

        player = MagicMock()
        player.player_id = "player1"
        player.display_name = "Player 1"
        player.state.playback_state = PlaybackState.IDLE
        provider.mass.players.all_players.return_value = [player]  # type: ignore[attr-defined]
        provider.mass.players.get_player.return_value = player  # type: ignore[attr-defined]

        state = _make_ynison_state(progress_ms=0, paused=False)

        await provider._activate_playback(state)

        assert provider._active_player_id == "player1"
        provider.mass.create_task.assert_called()  # type: ignore[unreachable]

    async def test_unpause_after_external_pause_fires_play_media(self) -> None:
        """
        Resume after pause schedules play_media for the (queue-id) player.

        Simulates the post-`_pause_playback` state: `_stream_stop_event`
        set, `_active_player_id` already demoted to the queue id (the
        pause path's post-cmd_stop rewrite), `_in_use_by_player` cleared
        by `on_source_unselected`. `_activate_playback` should fire
        `play_media(queue_id, audio_source.uri)` and clear the stop
        event so the next session is free to run.
        """
        provider = _make_provider()
        # Post-pause state: AriaCast path demoted _active_player_id to
        # the queue id (bare UUID) after cmd_stop succeeded.
        provider._stream_stop_event.set()
        provider._active_player_id = "player1"
        provider._in_use_by_player = None

        player = MagicMock()
        player.player_id = "player1"
        provider.mass.players.all_players.return_value = [player]
        provider.mass.players.get_player.return_value = player

        play_media_calls = _arm_play_media_recorder(provider)

        state = _make_ynison_state(progress_ms=10_000, paused=False)

        await provider._activate_playback(state)
        await asyncio.sleep(0)  # let the scheduled play_media coro run

        assert len(play_media_calls) == 1
        target_id, uri = play_media_calls[0]
        # Target must be the (queue id) bare player, NOT the bridge.
        # Bridge id has no queue → PlayerUnavailableError.
        assert target_id == "player1"
        assert uri == str(provider._audio_source.uri)
        assert not provider._stream_stop_event.is_set()

    async def test_resume_via_externally_paused_flag_alone(self) -> None:
        """Resume fires play_media even if `_stream_stop_event` was cleared."""
        provider = _make_provider()
        provider._stream_stop_event.clear()
        provider._externally_paused = True
        provider._active_player_id = "player1"
        provider._in_use_by_player = None

        player = MagicMock()
        player.player_id = "player1"
        provider.mass.players.all_players.return_value = [player]
        provider.mass.players.get_player.return_value = player

        play_media_calls = _arm_play_media_recorder(provider)

        await provider._activate_playback(_make_ynison_state(progress_ms=10_000, paused=False))
        await asyncio.sleep(0)

        assert play_media_calls, "play_media must fire even without stop event"
        assert provider._externally_paused is False  # cleared by _activate_playback

    async def test_detects_track_change(self) -> None:
        """Detects track change and updates streaming track id."""
        provider = _make_provider()
        provider._current_streaming_track_id = "track1"

        player = MagicMock()
        player.player_id = "player1"
        provider.mass.players.all_players.return_value = [player]  # type: ignore[attr-defined]
        provider.mass.players.get_player.return_value = player  # type: ignore[attr-defined]
        provider._active_player_id = "player1"

        state = _make_ynison_state(
            progress_ms=0,
            paused=False,
            playable_list=[{"playable_id": "track2"}],
        )

        await provider._activate_playback(state)

        assert provider._current_streaming_track_id == "track2"
        assert provider._track_changed_event.is_set()

    async def test_resume_after_pause(self) -> None:
        """Resume after pause triggers reselect and seek."""
        provider = _make_provider()
        provider._active_player_id = "player1"
        provider._current_streaming_track_id = "track1"
        provider._stream_stop_event.set()  # simulate paused

        player = MagicMock()
        player.player_id = "player1"
        provider.mass.players.get_player.return_value = player  # type: ignore[attr-defined]

        state = _make_ynison_state(
            progress_ms=50000,
            paused=False,
            playable_list=[{"playable_id": "track1"}],
        )

        await provider._activate_playback(state)

        assert provider._seek_position_ms == 50000
        assert provider._track_changed_event.is_set()

    async def test_no_target_player_returns(self) -> None:
        """Returns early when no target player is available."""
        provider = _make_provider()
        provider.mass.players.all_players.return_value = []  # type: ignore[attr-defined]
        provider.mass.players.get_player.return_value = None  # type: ignore[attr-defined]

        state = _make_ynison_state()

        await provider._activate_playback(state)

        assert provider._active_player_id is None


class TestInvalidateStreamCache:
    """Tests for _invalidate_stream_cache method."""

    async def test_deletes_cache_entry(self) -> None:
        """_invalidate_stream_cache calls mass.cache.delete."""
        provider = _make_provider()
        provider.mass.cache = MagicMock()
        provider.mass.cache.delete = AsyncMock()

        await provider._invalidate_stream_cache(
            "track:42", provider_instance_id="yandex_music--test"
        )

        provider.mass.cache.delete.assert_called_once_with(
            "ynison_sd_yandex_music--test_track:42",
            provider=provider.instance_id,
        )


# ------------------------------------------------------------------
# Protocol invariants ported from the pre-AudioSource branch
# ------------------------------------------------------------------


def _make_active_state(track_id: str | None = "track42", *, paused: bool = False) -> YnisonState:
    """Build a YnisonState that reports our device as active and optionally playing."""
    state = YnisonState()
    state.active_device_id = "dev1"  # ours
    queue: dict[str, Any] = {"player_queue": {}}
    if track_id is not None:
        queue["player_queue"] = {
            "playable_list": [{"playable_id": track_id}],
            "current_playable_index": 0,
        }
    queue["status"] = {"paused": paused, "progress_ms": 0, "duration_ms": 60_000}
    state.player_state = queue
    return state


class TestPostReconnectSettleWindow:
    """The 2 s settle window after a WS reconnect drops the first inbound state."""

    async def test_settle_window_skips_activate(self) -> None:
        """While in the settle window, _handle_ynison_state must not act."""
        provider = _make_provider()
        provider._device_id = "dev1"
        provider._ynison = MagicMock()
        provider._ynison.in_post_reconnect_settle = True
        activate_calls: list[Any] = []

        async def _spy_activate(state: YnisonState) -> None:
            activate_calls.append(state)

        _stub_attr(provider, "_activate_playback", _spy_activate)

        await provider._handle_ynison_state(_make_active_state())
        assert activate_calls == []

    async def test_no_settle_window_runs_activate(self) -> None:
        """Outside the settle window, normal handling resumes."""
        provider = _make_provider()
        provider._device_id = "dev1"
        provider._ynison = MagicMock()
        provider._ynison.in_post_reconnect_settle = False
        activate_calls: list[Any] = []

        async def _spy_activate(state: YnisonState) -> None:
            activate_calls.append(state)

        _stub_attr(provider, "_activate_playback", _spy_activate)
        # _maybe_prefetch is sync — patch with a no-op so we can spy on activate alone
        _stub_attr(provider, "_maybe_prefetch", MagicMock())

        await provider._handle_ynison_state(_make_active_state())
        assert len(activate_calls) == 1


class TestIdempotencyTTL:
    """`_idempotent(action, key)` collapses duplicates within TTL."""

    def test_second_call_within_ttl_returns_false(self) -> None:
        """Second call within `_COMMAND_IDEMPOTENCY_TTL` is suppressed."""
        provider = _make_provider()
        with patch(
            "music_assistant.providers.yandex_ynison.provider.time.monotonic",
            side_effect=[100.0, 100.5],
        ):
            assert provider._idempotent("on_pause", None) is True
            assert provider._idempotent("on_pause", None) is False

    def test_call_past_ttl_returns_true(self) -> None:
        """A call past the TTL window passes again."""
        provider = _make_provider()
        gap = _COMMAND_IDEMPOTENCY_TTL + 0.1
        with patch(
            "music_assistant.providers.yandex_ynison.provider.time.monotonic",
            side_effect=[100.0, 100.0 + gap, 100.0 + gap],
        ):
            assert provider._idempotent("on_pause", None) is True
            assert provider._idempotent("on_pause", None) is True

    def test_separate_keys_do_not_collide(self) -> None:
        """Different (action, key) tuples have independent debounce windows."""
        provider = _make_provider()
        with patch(
            "music_assistant.providers.yandex_ynison.provider.time.monotonic",
            side_effect=[100.0, 100.1],
        ):
            assert provider._idempotent("on_play", None) is True
            assert provider._idempotent("on_pause", None) is True

    async def test_on_pause_double_call_sends_one_update(self) -> None:
        """Two _on_pause() within 1 s → exactly one update_playing_status."""
        provider = _make_provider()
        provider._ynison = MagicMock()
        provider._ynison.connected = True
        provider._ynison.state.progress_ms = 1000
        provider._ynison.state.duration_ms = 60000
        provider._ynison.update_playing_status = AsyncMock()

        with patch(
            "music_assistant.providers.yandex_ynison.provider.time.monotonic",
            side_effect=[100.0, 100.5],
        ):
            await provider._on_pause()
            await provider._on_pause()

        assert provider._ynison.update_playing_status.await_count == 1


class TestProgressClamp:
    """`_send_progress_to_ynison` clamps progress_ms to duration_ms."""

    async def test_progress_clamped_to_duration(self) -> None:
        """progress_ms above duration_ms is clamped before WS send."""
        provider = _make_provider()
        provider._ynison = MagicMock()
        provider._ynison.connected = True
        provider._ynison.update_playing_status = AsyncMock()

        await provider._send_progress_to_ynison(progress_ms=5000, duration_ms=4000, paused=False)
        called_kwargs = provider._ynison.update_playing_status.await_args.kwargs
        assert called_kwargs["progress_ms"] == 4000
        assert called_kwargs["duration_ms"] == 4000

    async def test_progress_below_duration_passes_through(self) -> None:
        """Healthy progress values are forwarded unchanged."""
        provider = _make_provider()
        provider._ynison = MagicMock()
        provider._ynison.connected = True
        provider._ynison.update_playing_status = AsyncMock()

        await provider._send_progress_to_ynison(progress_ms=3000, duration_ms=4000, paused=False)
        called_kwargs = provider._ynison.update_playing_status.await_args.kwargs
        assert called_kwargs["progress_ms"] == 3000


class TestDriftClassifier:
    """`_classify_drift` returns ignore / queue_rebuild / seek per heuristic."""

    def test_drift_under_threshold_ignored(self) -> None:
        """Drift below the 3 s threshold is classified as `ignore`."""
        assert YandexYnisonProvider._classify_drift(60_500, 60_000) == "ignore"

    def test_queue_rebuild_pattern(self) -> None:
        """Ynison near zero while we're deep into the track → queue-rebuild echo."""
        assert YandexYnisonProvider._classify_drift(0, 120_000) == "queue_rebuild"
        assert YandexYnisonProvider._classify_drift(500, 30_000) == "queue_rebuild"

    def test_genuine_seek(self) -> None:
        """Large non-zero drift is treated as a genuine user seek."""
        assert YandexYnisonProvider._classify_drift(120_000, 60_000) == "seek"

    def test_custom_threshold(self) -> None:
        """Custom threshold raises the bar for `ignore`."""
        assert YandexYnisonProvider._classify_drift(5_000, 0, threshold_ms=10_000) == "ignore"


class TestOnSourceUnselectedStaleRejection:
    """`on_source_unselected` rejects stale stream_session_id callbacks."""

    async def test_stale_session_id_keeps_claim(self) -> None:
        """A teardown callback from a superseded session must not release the claim."""
        provider = _make_provider()
        provider._in_use_by_player = "queue1"
        provider._active_session_id = "live"

        await provider.on_source_unselected(AUDIO_SOURCE_ID, "queue1", "stale")

        assert provider._in_use_by_player == "queue1"
        assert provider._active_session_id == "live"

    async def test_matching_session_id_releases_claim(self) -> None:
        """The live session id matches → lock and session id clear."""
        provider = _make_provider()
        provider._in_use_by_player = "queue1"
        provider._active_session_id = "live"

        await provider.on_source_unselected(AUDIO_SOURCE_ID, "queue1", "live")

        assert provider._in_use_by_player is None
        assert provider._active_session_id is None


class TestUpdateSourceCapabilitiesRefresh:
    """`_update_source_capabilities` rebuilds the source and refreshes the session."""

    def _provider_in_use(self) -> YandexYnisonProvider:
        provider = _make_provider()
        # Linked yandex_music provider available → capabilities ON
        provider._yandex_provider = MagicMock()
        provider._in_use_by_player = "player1"
        provider.mass.players.refresh_source = MagicMock()
        return provider

    def test_a_capability_flip_refreshes_the_session(self) -> None:
        """
        The rebuilt source is handed to the session the player publishes from.

        The controls a client sees come from the object the session holds, so without
        this the new flags would not appear until the source was selected again.
        """
        provider = self._provider_in_use()

        provider._update_source_capabilities()

        provider.mass.players.refresh_source.assert_called_once()
        player_id, source = provider.mass.players.refresh_source.call_args.args
        assert player_id == "player1"
        assert isinstance(source, AudioSource)
        assert source.can_play_pause is True
        assert source.can_seek is True
        assert source.can_next_previous is True
        # and the provider keeps the same object it published
        assert provider._audio_source is source

    def test_nothing_is_refreshed_when_no_player_is_using_it(self) -> None:
        """With no player holding the source there is no session to refresh."""
        provider = self._provider_in_use()
        provider._in_use_by_player = None

        provider._update_source_capabilities()

        provider.mass.players.refresh_source.assert_not_called()

    def test_the_source_is_still_rebuilt_when_no_player_is_using_it(self) -> None:
        """The provider's own copy is updated regardless, ready for the next selection."""
        provider = self._provider_in_use()
        provider._in_use_by_player = None
        before = provider._audio_source

        provider._update_source_capabilities()

        assert provider._audio_source is not before
        assert provider._audio_source.can_play_pause is True


class TestPrefetchOrdering:
    """Format pre-fetch must complete BEFORE `play_media` is queued."""

    async def test_prefetch_called_before_play_media(self) -> None:
        """`_prefetch_format_for_track` is awaited before `play_media` is queued."""
        provider = _make_provider()
        provider._device_id = "dev1"
        # Force target_player resolution
        provider._default_player_id = "player1"

        order: list[str] = []

        async def _fake_prefetch(track_id: str) -> None:
            order.append(f"prefetch:{track_id}")

        async def _fake_play_media(player_id: str, uri: str) -> None:
            order.append(f"play_media:{player_id}:{uri}")

        _stub_attr(provider, "_prefetch_format_for_track", _fake_prefetch)
        provider.mass.player_queues.play_media = _fake_play_media
        # Replace create_task with sync await so ordering is observable
        provider.mass.create_task = lambda coro, *_a, **_kw: asyncio.get_event_loop().create_task(
            coro
        )
        # Player resolution returns our target
        provider.mass.players.all_players = MagicMock(return_value=[])
        _stub_attr(
            provider,
            "_get_target_player_id",
            MagicMock(return_value="player1"),
        )

        await provider._activate_playback(_make_active_state())
        # Let the create_task scheduled coro finish.
        await asyncio.sleep(0)

        assert order, "expected at least a prefetch call"
        assert order[0].startswith("prefetch:track42")
        assert any(c.startswith("play_media:player1") for c in order[1:])


class TestDynamicSessionCoordinator:
    """Dynamic transitions restart only when the effective PCM signature changes."""

    @staticmethod
    def _enable(provider: YandexYnisonProvider, rates: list[tuple[int, int]]) -> None:
        provider._requested_stream_mode = STREAM_MODE_MAX_QUALITY
        provider._effective_stream_mode = STREAM_MODE_MAX_QUALITY
        provider._dynamic_generation = 7
        provider._active_player_id = "player1"
        provider._in_use_by_player = "player1"
        player = MagicMock()
        player.get_supported_sample_rates.return_value = rates
        provider.mass.players.get_player.return_value = player

    @staticmethod
    def _details(rate: int, depth: int) -> MagicMock:
        details = MagicMock()
        details.audio_format = AudioFormat(
            content_type=ContentType.FLAC,
            sample_rate=rate,
            bit_depth=depth,
            channels=2,
        )
        return details

    async def test_mixed_format_transition_restarts_once_after_applying_format(self) -> None:
        """A changed signature must be installed before exactly one same-owner restart."""
        provider = _make_provider()
        self._enable(provider, [(44_100, 16), (96_000, 24)])
        provider._dynamic_session_signature = (ContentType.PCM_S16LE, 44_100, 16, 2)
        provider._current_streaming_track_id = "track1"
        provider._dynamic_session_ended_event.set()
        _stub_attr(
            provider,
            "_get_dynamic_stream_details",
            AsyncMock(return_value=self._details(96_000, 24)),
        )
        applied_at_play: list[tuple[ContentType, int, int, int]] = []

        async def _play_media(_queue_id: str, _uri: str) -> None:
            fmt = provider._normalized_format
            applied_at_play.append((fmt.content_type, fmt.sample_rate, fmt.bit_depth, fmt.channels))

        provider.mass.player_queues.play_media = _play_media

        await provider._handle_dynamic_track_transition("track2", 12_345, "player1", 7)
        await provider._handle_dynamic_track_transition("track2", 12_999, "player1", 7)

        assert applied_at_play == [(ContentType.PCM_S24LE, 96_000, 24, 2)]
        assert provider._pending_restart_track_id == "track2"
        assert provider._seek_position_ms == 12_345

    async def test_same_effective_format_continues_current_session(self) -> None:
        """Different source containers that resolve identically must not call play_media."""
        provider = _make_provider()
        self._enable(provider, [(44_100, 16), (96_000, 24)])
        provider._dynamic_session_signature = (ContentType.PCM_S24LE, 96_000, 24, 2)
        provider._ynison = _mock_ynison(
            _make_ynison_state(
                progress_ms=4_321,
                playable_list=[{"playable_id": "track2"}],
            )
        )
        _stub_attr(
            provider,
            "_get_dynamic_stream_details",
            AsyncMock(return_value=self._details(192_000, 24)),
        )

        await provider._handle_dynamic_track_transition("track2", 1_000, "player1", 7)

        provider.mass.player_queues.play_media.assert_not_awaited()
        assert provider._track_changed_event.is_set()
        assert provider._pending_restart_track_id is None
        assert provider._seek_position_ms == 4_321

    async def test_stale_generation_cannot_start_session(self) -> None:
        """A completed fetch from an obsolete track/device generation must be discarded."""
        provider = _make_provider()
        self._enable(provider, [(96_000, 24)])
        provider._dynamic_session_signature = (ContentType.PCM_S16LE, 44_100, 16, 2)
        _stub_attr(
            provider,
            "_get_dynamic_stream_details",
            AsyncMock(return_value=self._details(96_000, 24)),
        )

        await provider._handle_dynamic_track_transition("track2", 1_000, "player1", 6)

        provider.mass.player_queues.play_media.assert_not_awaited()
        assert provider._pending_restart_track_id is None

    async def test_invalid_real_format_is_retried_instead_of_guessed(self) -> None:
        """Out-of-range source metadata must not start dynamic PCM with a fallback guess."""
        provider = _make_provider()
        self._enable(provider, [(96_000, 24)])
        invalid = self._details(0, 24)
        valid = self._details(96_000, 24)
        fetch = AsyncMock(side_effect=[invalid, valid])
        _stub_attr(provider, "_get_stream_details_with_retry", fetch)
        invalidate = AsyncMock()
        _stub_attr(provider, "_invalidate_stream_cache", invalidate)

        with patch(
            "music_assistant.providers.yandex_ynison.provider.asyncio.sleep", new=AsyncMock()
        ):
            result = await provider._get_dynamic_stream_details("track2", 7)

        assert result is valid
        assert fetch.await_count == 2
        invalidate.assert_awaited_once_with("track2")

    async def test_exhausted_transient_batch_is_retried(self) -> None:
        """Dynamic startup starts a fresh retry batch after transient exhaustion."""
        provider = _make_provider()
        self._enable(provider, [(96_000, 24)])
        valid = self._details(96_000, 24)
        fetch = AsyncMock(side_effect=[RetriesExhausted("temporary"), valid])
        _stub_attr(provider, "_get_stream_details_with_retry", fetch)

        with patch(
            "music_assistant.providers.yandex_ynison.provider.asyncio.sleep", new=AsyncMock()
        ):
            result = await provider._get_dynamic_stream_details("track2", 7)

        assert result is valid
        assert fetch.await_count == 2

    @pytest.mark.parametrize(
        "error",
        [MediaNotFoundError("missing"), RuntimeError("bug")],
        ids=["permanent-ma-error", "unexpected-python-error"],
    )
    async def test_non_transient_dynamic_error_propagates(self, error: Exception) -> None:
        """Dynamic startup must not retry permanent failures or internal bugs."""
        provider = _make_provider()
        self._enable(provider, [(96_000, 24)])
        fetch = AsyncMock(side_effect=error)
        _stub_attr(provider, "_get_stream_details_with_retry", fetch)

        async def _end_generation(_delay: float) -> None:
            provider._dynamic_generation += 1

        sleep = AsyncMock(side_effect=_end_generation)
        with (
            patch("music_assistant.providers.yandex_ynison.provider.asyncio.sleep", new=sleep),
            pytest.raises(type(error), match=str(error)),
        ):
            await provider._get_dynamic_stream_details("track2", 7)

        fetch.assert_awaited_once()
        sleep.assert_not_awaited()

    def test_invalid_actual_player_capabilities_use_source_pcm(self) -> None:
        """Malformed actual-player capabilities preserve the source signature."""
        provider = _make_provider()
        player = MagicMock()
        player.get_supported_sample_rates.side_effect = ValueError("invalid capabilities")
        provider.mass.players.get_player.return_value = player

        signature = provider._effective_signature_for_player(self._details(96_000, 24), "player1")

        assert signature == (ContentType.PCM_S24LE, 96_000, 24, 2)

    def test_unexpected_actual_player_capability_error_propagates(self) -> None:
        """An internal actual-player lookup error must not select guessed PCM."""
        provider = _make_provider()
        player = MagicMock()
        player.get_supported_sample_rates.side_effect = RuntimeError("bug")
        provider.mass.players.get_player.return_value = player

        with pytest.raises(RuntimeError, match="bug"):
            provider._effective_signature_for_player(self._details(96_000, 24), "player1")

    async def test_on_source_selected_keeps_advertised_pcm_for_actual_consumer(self) -> None:
        """Selection must not change PCM after direct consumers fixed StreamDetails."""
        provider = _make_provider()
        self._enable(provider, [(44_100, 16), (96_000, 24)])
        provider._dynamic_session_signature = (ContentType.PCM_S24LE, 96_000, 24, 2)
        provider._apply_dynamic_signature(provider._dynamic_session_signature)
        provider._pending_restart_track_id = "track2"
        provider._prefetched_stream_details["track2"] = self._details(96_000, 24)
        actual = MagicMock()
        actual.get_supported_sample_rates.return_value = [(48_000, 24)]
        provider.mass.players.get_player.side_effect = lambda player_id: (
            actual if player_id == "bridge1" else None
        )

        await provider.on_source_selected("main", "bridge1", "player1", "session2")

        details = await provider.get_stream_details("main", MediaType.AUDIO_SOURCE)
        assert details.audio_format.sample_rate == 96_000
        assert details.audio_format.bit_depth == 24
        assert provider._pending_restart_track_id is None

    async def test_first_session_uses_real_format_and_latest_ynison_progress(self) -> None:
        """Initial dynamic playback must await real details and seek to the latest clock."""
        provider = _make_provider()
        self._enable(provider, [(44_100, 16), (96_000, 24)])
        provider._ynison = _mock_ynison(_make_ynison_state(progress_ms=9_876))
        _stub_attr(
            provider,
            "_get_dynamic_stream_details",
            AsyncMock(return_value=self._details(96_000, 24)),
        )
        observed: list[tuple[int, int]] = []

        async def _play_media(_queue_id: str, _uri: str) -> None:
            observed.append((provider._normalized_format.sample_rate, provider._seek_position_ms))

        provider.mass.player_queues.play_media = _play_media

        await provider._launch_dynamic_session("track1", 100, "player1", 7)

        assert observed == [(96_000, 9_876)]

    async def test_failed_initial_dynamic_launch_restores_retryable_state(self) -> None:
        """A failed launch must not suppress a retry for the same track."""
        provider = _make_provider()
        self._enable(provider, [(96_000, 24)])
        provider._dynamic_target_track_id = "track1"
        _stub_attr(
            provider,
            "_get_dynamic_stream_details",
            AsyncMock(side_effect=MediaNotFoundError("missing")),
        )

        with pytest.raises(MediaNotFoundError, match="missing"):
            await provider._launch_dynamic_session("track1", 100, "player1", 7)

        assert provider._dynamic_target_track_id is None
        assert provider._active_player_id is None

    async def test_failed_dynamic_play_media_restores_retryable_state(self) -> None:
        """A launch rejected by the player queue must leave playback stopped."""
        provider = _make_provider()
        self._enable(provider, [(96_000, 24)])
        provider._dynamic_target_track_id = "track1"
        _stub_attr(
            provider,
            "_get_dynamic_stream_details",
            AsyncMock(return_value=self._details(96_000, 24)),
        )
        provider.mass.player_queues.play_media.side_effect = PlayerCommandFailed("unavailable")

        with pytest.raises(PlayerCommandFailed, match="unavailable"):
            await provider._launch_dynamic_session("track1", 100, "player1", 7)

        assert provider._dynamic_target_track_id is None
        assert provider._active_player_id is None

    async def test_next_track_prefetch_uses_immediate_playable_successor(self) -> None:
        """Prefetch must use index + 1 and retain current plus next details only."""
        provider = _make_provider()
        self._enable(provider, [(96_000, 24)])
        next_details = self._details(96_000, 24)
        fetch = AsyncMock(return_value=next_details)
        _stub_attr(provider, "_get_dynamic_stream_details", fetch)

        await provider._prefetch_next_dynamic_track(
            1,
            [
                {"playable_id": "track0"},
                {"playable_id": "track1"},
                {"playable_id": "track2"},
                {"playable_id": "track3"},
            ],
            7,
        )

        fetch.assert_awaited_once_with("track2", 7)
        assert provider._prefetched_stream_details["track2"] is next_details

    async def test_next_track_prefetch_follows_shuffle_order(self) -> None:
        """Dynamic prefetch uses the same logical successor as playback."""
        provider = _make_provider()
        self._enable(provider, [(96_000, 24)])
        next_details = self._details(96_000, 24)
        fetch = AsyncMock(return_value=next_details)
        _stub_attr(provider, "_get_dynamic_stream_details", fetch)

        await provider._prefetch_next_dynamic_track(
            2,
            [
                {"playable_id": "track0"},
                {"playable_id": "track1"},
                {"playable_id": "track2"},
                {"playable_id": "track3"},
            ],
            7,
            (0, 2, 1, 3),
        )

        fetch.assert_awaited_once_with("track1", 7)

    def test_prefetch_cache_retains_current_and_next_when_completion_is_out_of_order(
        self,
    ) -> None:
        """Touching the new current track must evict the previous track, not the successor."""
        provider = _make_provider()
        track1 = self._details(44_100, 16)
        track2 = self._details(96_000, 24)
        track3 = self._details(48_000, 24)
        provider._remember_stream_details("track2", track2)
        provider._remember_stream_details("track1", track1)

        provider._remember_stream_details("track2", track2)
        provider._remember_stream_details("track3", track3)

        assert provider._prefetched_stream_details == {"track2": track2, "track3": track3}

    async def test_pause_cancels_launch_but_preserves_ready_prefetch(self) -> None:
        """Pause invalidates pending launch work without discarding fetched details."""
        provider = _make_provider()
        self._enable(provider, [(96_000, 24)])
        provider._in_use_by_player = None
        provider._prefetched_stream_details["track2"] = self._details(96_000, 24)
        provider._ynison = _mock_ynison(_make_ynison_state(paused=True))

        async def _pending() -> None:
            await asyncio.Event().wait()

        provider._dynamic_task = asyncio.create_task(_pending())
        old_generation = provider._dynamic_generation

        await provider._pause_playback()

        assert provider._dynamic_generation == old_generation + 1
        assert provider._dynamic_task is None
        assert "track2" in provider._prefetched_stream_details
        assert provider._active_player_id is None
        assert provider._externally_paused is True

    async def test_activate_schedules_only_one_initial_dynamic_launch(self) -> None:
        """Repeated Ynison state events must not queue duplicate play_media launches."""
        provider = _make_provider()
        self._enable(provider, [(96_000, 24)])
        provider._active_player_id = None
        _stub_attr(provider, "_get_target_player_id", MagicMock(return_value="player1"))
        launches: list[str] = []

        async def _launch(track_id: str, _progress: int, _queue: str, _generation: int) -> None:
            launches.append(track_id)

        _stub_attr(provider, "_launch_dynamic_session", _launch)
        provider.mass.create_task = lambda coro, *_a, **_kw: asyncio.create_task(coro)
        state = _make_active_state()

        await provider._activate_playback(state)
        await provider._activate_playback(state)
        await asyncio.sleep(0)

        assert launches == ["track42"]

    async def test_dynamic_launch_keeps_owner_when_consumer_is_a_bridge(self) -> None:
        """Dynamic work must target the stable owner rather than its physical bridge."""
        provider = _make_provider("base-player")
        self._enable(provider, [(96_000, 24)])
        provider._active_player_id = "spb_base-player"
        provider._in_use_by_player = None
        provider.mass.players.get_player.return_value = MagicMock()
        schedule = MagicMock()
        _stub_attr(provider, "_schedule_dynamic_launch", schedule)
        state = _make_active_state()

        await provider._activate_playback(state)

        schedule.assert_called_once_with("track42", state.progress_ms, "base-player")

    async def test_skip_replaces_pending_initial_dynamic_launch(self) -> None:
        """A skip before the first format resolves must launch the new current track."""
        provider = _make_provider()
        self._enable(provider, [(96_000, 24)])
        provider._active_player_id = None
        _stub_attr(provider, "_get_target_player_id", MagicMock(return_value="player1"))
        release = asyncio.Event()
        launches: list[str] = []

        async def _launch(track_id: str, _progress: int, _queue: str, _generation: int) -> None:
            launches.append(track_id)
            await release.wait()

        _stub_attr(provider, "_launch_dynamic_session", _launch)
        provider.mass.create_task = lambda coro, *_a, **_kw: asyncio.create_task(coro)
        first = _make_ynison_state(playable_list=[{"playable_id": "track1"}])
        second = _make_ynison_state(playable_list=[{"playable_id": "track2"}])

        try:
            await provider._activate_playback(first)
            await asyncio.sleep(0)
            await provider._activate_playback(second)
            await asyncio.sleep(0)

            assert launches == ["track1", "track2"]
            assert provider._dynamic_target_track_id == "track2"
        finally:
            release.set()
            await provider._cancel_dynamic_task(clear_prefetch=False)

    async def test_superseded_generator_cannot_end_current_dynamic_session(self) -> None:
        """An old same-owner generator must not release the current restart handshake."""
        provider = _make_provider()
        self._enable(provider, [(96_000, 24)])
        provider._active_session_id = "session1"
        provider._yandex_provider = MagicMock()
        provider._ynison = _mock_ynison(
            _make_ynison_state(playable_list=[{"playable_id": "track1"}])
        )

        async def _stream_track(*_args: object, **_kwargs: object) -> Any:
            yield b"\x00" * 4

        _stub_attr(provider, "_stream_track", _stream_track)
        stream_details = await provider.get_stream_details("main", MediaType.AUDIO_SOURCE)
        old_stream = provider.get_audio_stream(stream_details)
        current_stream = provider.get_audio_stream(stream_details)

        try:
            assert await anext(old_stream) == b"\x00" * 4
            provider._active_session_id = "session2"
            assert await anext(current_stream) == b"\x00" * 4
            assert not provider._dynamic_session_ended_event.is_set()

            with pytest.raises(StopAsyncIteration):
                await anext(old_stream)

            assert not provider._dynamic_session_ended_event.is_set()
        finally:
            await old_stream.aclose()
            await current_stream.aclose()

    async def test_stream_progress_refreshes_physical_bridge_not_owner(self) -> None:
        """Live progress must refresh the player that actually consumes the PCM."""
        provider = _make_provider("base-player")
        provider._in_use_by_player = "base-player"
        provider._active_player_id = "spb_base-player"
        provider._active_session_id = "session1"
        provider._yandex_provider = MagicMock()
        provider._ynison = _mock_ynison(
            _make_ynison_state(playable_list=[{"playable_id": "track1"}])
        )
        sync_progress = AsyncMock()
        _stub_attr(provider, "_sync_progress", sync_progress)

        async def _stream_track(*_args: object, **_kwargs: object) -> Any:
            provider._stream_stop_event.set()
            yield b"\x00" * 4

        _stub_attr(provider, "_stream_track", _stream_track)
        stream_details = await provider.get_stream_details("main", MediaType.AUDIO_SOURCE)
        stream = provider.get_audio_stream(stream_details)

        try:
            with patch(
                "music_assistant.providers.yandex_ynison.provider.time.monotonic",
                side_effect=[100.0, 106.0],
            ):
                assert await anext(stream) == b"\x00" * 4
                with pytest.raises(StopAsyncIteration):
                    await anext(stream)
        finally:
            await stream.aclose()

        sync_progress.assert_awaited_once()
        assert sync_progress.await_args is not None
        assert sync_progress.await_args.args[2] == "spb_base-player"

    async def test_reconnect_during_format_restart_exits_before_streaming_old_pcm(self) -> None:
        """A replacement generator must join a pending restart without emitting old PCM."""
        provider = _make_provider()
        self._enable(provider, [(44_100, 16), (96_000, 24)])
        provider._dynamic_session_signature = (ContentType.PCM_S16LE, 44_100, 16, 2)
        provider._active_session_id = "session1"
        provider._yandex_provider = MagicMock()
        provider._ynison = _mock_ynison(
            _make_ynison_state(playable_list=[{"playable_id": "track1"}])
        )
        _stub_attr(
            provider,
            "_get_dynamic_stream_details",
            AsyncMock(return_value=self._details(96_000, 24)),
        )

        async def _stream_track(*_args: object, **_kwargs: object) -> Any:
            yield b"\x00" * 4

        _stub_attr(provider, "_stream_track", _stream_track)
        stream_details = await provider.get_stream_details("main", MediaType.AUDIO_SOURCE)
        old_stream = provider.get_audio_stream(stream_details)
        replacement_stream = None
        transition: asyncio.Task[None] | None = None

        try:
            assert await anext(old_stream) == b"\x00" * 4
            assert provider._ynison is not None
            provider._ynison.state = _make_ynison_state(
                current_playable_index=1,
                playable_list=[{"playable_id": "track1"}, {"playable_id": "track2"}],
            )
            transition = asyncio.create_task(
                provider._handle_dynamic_track_transition("track2", 2_000, "player1", 7)
            )
            await asyncio.sleep(0)
            assert provider._format_restart_requested

            await provider.on_source_selected("main", "player1", "player1", "session2")
            replacement_details = await provider.get_stream_details("main", MediaType.AUDIO_SOURCE)
            replacement_stream = provider.get_audio_stream(replacement_details)
            with pytest.raises(StopAsyncIteration):
                await anext(replacement_stream)
            with pytest.raises(StopAsyncIteration):
                await anext(old_stream)
            await asyncio.wait_for(transition, timeout=1)

            provider.mass.player_queues.play_media.assert_awaited_once()
            assert provider._normalized_format.sample_rate == 96_000
        finally:
            await old_stream.aclose()
            if replacement_stream is not None:
                await replacement_stream.aclose()
            if transition is not None and not transition.done():
                transition.cancel()
                with suppress(asyncio.CancelledError):
                    await transition

    async def test_format_restart_exits_generator_without_signalling_completion(self) -> None:
        """A format boundary must not tell Ynison that the interrupted track ended."""
        provider = _make_provider()
        self._enable(provider, [(44_100, 16), (96_000, 24)])
        provider._active_session_id = "session1"
        provider._yandex_provider = MagicMock()
        provider._ynison = _mock_ynison(
            _make_ynison_state(playable_list=[{"playable_id": "track1"}])
        )
        completion = AsyncMock()
        _stub_attr(provider, "_signal_track_completion", completion)

        async def _stream_track(*_args: object, **_kwargs: object) -> Any:
            assert provider._ynison is not None
            provider._ynison.state = _make_ynison_state(
                progress_ms=2_000,
                playable_list=[{"playable_id": "track1"}, {"playable_id": "track2"}],
                current_playable_index=1,
            )
            provider._pending_restart_track_id = "track2"
            provider._format_restart_requested = True
            provider._track_changed_event.set()
            yield b"\x00" * 4

        _stub_attr(provider, "_stream_track", _stream_track)
        stream_details = await provider.get_stream_details("main", MediaType.AUDIO_SOURCE)

        assert [chunk async for chunk in provider.get_audio_stream(stream_details)] == [b"\x00" * 4]
        completion.assert_not_awaited()
        assert provider._dynamic_session_ended_event.is_set()

    async def test_generator_waits_for_format_decision_after_ynison_track_change(self) -> None:
        """A new track must not use the old session PCM while its real format is pending."""
        provider = _make_provider()
        self._enable(provider, [(44_100, 16), (96_000, 24)])
        provider._active_session_id = "session1"
        provider._yandex_provider = MagicMock()
        provider._ynison = _mock_ynison(
            _make_ynison_state(playable_list=[{"playable_id": "track1"}])
        )
        completion = AsyncMock()
        _stub_attr(provider, "_signal_track_completion", completion)
        decision_seen: list[bool] = []
        decision_released = False
        release_tasks: list[asyncio.Task[None]] = []

        async def _release_format_decision() -> None:
            nonlocal decision_released
            await asyncio.sleep(0)
            decision_released = True
            provider._track_changed_event.set()

        async def _stream_track(track_id: str, **_kwargs: object) -> Any:
            if track_id == "track1":
                assert provider._ynison is not None
                provider._ynison.state = _make_ynison_state(
                    current_playable_index=1,
                    playable_list=[{"playable_id": "track1"}, {"playable_id": "track2"}],
                )
                release_tasks.append(asyncio.create_task(_release_format_decision()))
                if False:
                    yield b""
                return
            decision_seen.append(decision_released)
            provider._stream_stop_event.set()
            yield b"\x00" * 4

        _stub_attr(provider, "_stream_track", _stream_track)
        stream_details = await provider.get_stream_details("main", MediaType.AUDIO_SOURCE)

        assert [chunk async for chunk in provider.get_audio_stream(stream_details)] == [b"\x00" * 4]
        await asyncio.gather(*release_tasks)
        assert decision_seen == [True]
        completion.assert_not_awaited()

    async def test_handoff_invalidates_tasks_and_drops_prefetch(self) -> None:
        """Clearing the active player must prevent old-device work from launching later."""
        provider = _make_provider()
        self._enable(provider, [(96_000, 24)])
        provider._prefetched_stream_details["track2"] = self._details(96_000, 24)

        async def _pending() -> None:
            await asyncio.Event().wait()

        task = asyncio.create_task(_pending())
        provider._dynamic_task = task
        old_generation = provider._dynamic_generation

        provider._clear_active_player()
        await asyncio.sleep(0)

        assert task.cancelled()
        assert provider._dynamic_generation == old_generation + 1
        assert provider._prefetched_stream_details == {}


class TestPrefetchErrorHandling:
    """Ordinary format prefetch owns typed MA failures only."""

    async def test_known_ma_error_keeps_current_format(self) -> None:
        """A provider-reported prefetch failure preserves the session format."""
        provider = _make_provider()
        provider._yandex_provider = MagicMock()
        before = dict(provider._normalized_params)
        _stub_attr(
            provider,
            "_get_stream_details_with_retry",
            AsyncMock(side_effect=MediaNotFoundError("missing")),
        )

        await provider._prefetch_format_for_track("track1")

        assert provider._normalized_params == before

    async def test_unexpected_error_propagates(self) -> None:
        """An internal prefetch error must not be converted into a format fallback."""
        provider = _make_provider()
        provider._yandex_provider = MagicMock()
        _stub_attr(
            provider,
            "_get_stream_details_with_retry",
            AsyncMock(side_effect=RuntimeError("bug")),
        )

        with pytest.raises(RuntimeError, match="bug"):
            await provider._prefetch_format_for_track("track1")


class TestPrefetchFlowsThroughToStreamDetails:
    """
    `get_stream_details` returns the *prefetched* AudioFormat.

    Pins the contract that MA's upstream passthrough path (#3969,
    `_select_audio_source_pcm_format`) honors: MA reads
    ``streamdetails.audio_format`` and only invokes ffmpeg when it
    cannot match the player's supported rates. A regression that
    decouples ``_prefetch_format_for_track`` from
    ``self._normalized_params`` (or that returns a stale snapshot in
    ``get_stream_details``) would silently downgrade hi-res passthrough
    to a forced ffmpeg resample with no functional indicator beyond
    log entropy.
    """

    async def test_prefetch_updates_streamdetails_audio_format(self) -> None:
        """Prefetched source rate/bit-depth must reach `get_stream_details`."""
        from music_assistant_models.enums import MediaType  # noqa: PLC0415

        provider = _make_provider()
        # Default before prefetch: lossy PCM (16-bit / 44.1 kHz auto base).
        default_rate = provider._normalized_params["sample_rate"]
        assert default_rate != 96_000  # sanity: ensure we'll see a change

        mock_yandex = MagicMock()
        _set_stream_owner(mock_yandex)

        async def _fake_get_stream_details(_track_id: str, _media_type: MediaType) -> Any:
            sd = MagicMock()
            sd.expiration = 60
            sd.duration = 200
            sd.data = None
            sd.audio_format = AudioFormat(
                content_type=ContentType.FLAC,
                sample_rate=96_000,
                bit_depth=24,
                channels=2,
            )
            sd.to_dict = MagicMock(return_value={})
            sd.provider = mock_yandex.instance_id
            return sd

        mock_yandex.get_stream_details = AsyncMock(side_effect=_fake_get_stream_details)
        provider._yandex_provider = mock_yandex
        # Set explicit AUTO so prefetch hint is allowed to promote both axes.
        provider._cfg_sample_rate = OUTPUT_AUTO
        provider._cfg_bit_depth = OUTPUT_AUTO

        await provider._prefetch_format_for_track("track42")

        # `_normalized_params` lifted to source rate/bit-depth.
        assert provider._normalized_params["sample_rate"] == 96_000
        assert provider._normalized_params["bit_depth"] == 24
        # And get_stream_details now reflects that — MA's
        # `_select_audio_source_pcm_format` consumes this.
        sd = await provider.get_stream_details("main", MediaType.AUDIO_SOURCE)
        assert sd.media_type == MediaType.AUDIO_SOURCE
        assert sd.audio_format.sample_rate == 96_000
        assert sd.audio_format.bit_depth == 24
        assert sd.audio_format.channels == 2

    async def test_streamdetails_audio_format_is_fresh_copy_per_call(self) -> None:
        """
        Each `get_stream_details` returns a fresh AudioFormat instance.

        `AudioFormat` is mutable (MA's outer ffmpeg sets `codec_type` in
        place). A shared instance across `get_stream_details` calls
        would let one consumer's mutation poison the next one's
        snapshot — which #3969's passthrough specifically depends on
        for the format-match comparison.
        """
        from music_assistant_models.enums import MediaType  # noqa: PLC0415

        provider = _make_provider()
        sd1 = await provider.get_stream_details("main", MediaType.AUDIO_SOURCE)
        sd2 = await provider.get_stream_details("main", MediaType.AUDIO_SOURCE)

        assert sd1.media_type == MediaType.AUDIO_SOURCE
        assert sd1.audio_format == sd2.audio_format  # value-equal
        assert sd1.audio_format is not sd2.audio_format  # not the same instance


class TestAudioStreamPausedReturn:
    """`get_audio_stream` exits immediately when Ynison reports paused."""

    async def test_paused_state_exits_generator_without_yielding(self) -> None:
        """A paused-at-entry session yields zero chunks and the generator ends."""
        provider = _make_provider()
        provider._yandex_provider = MagicMock()
        provider._in_use_by_player = "player1"
        provider._active_session_id = "session-1"

        ynison = MagicMock()
        ynison.state.is_paused = True
        ynison.state.current_track_id = "track42"
        provider._ynison = ynison

        streamdetails = MagicMock()
        gen = provider.get_audio_stream(streamdetails, seek_position=0)

        chunks: list[bytes] = []
        with suppress(StopAsyncIteration):
            async for chunk in gen:
                chunks.append(chunk)

        assert chunks == []


class TestNaturalEndDifferentiation:
    """
    `_signal_track_completion` fires only on clean iterator exhaustion.

    These tests exercise the post-inner-loop branch via
    ``_wait_for_track_change`` as the outer-loop gate. The previous
    iteration set ``_stream_stop_event`` between yields, which made the
    stop-event guard above ``natural_end`` short-circuit the very logic
    we wanted to verify — the tests passed for the wrong reason. The
    rewritten infra lets ``natural_end`` actually evaluate and uses the
    ``_wait_for_track_change`` stub to terminate the outer loop.
    """

    def _build(self) -> YandexYnisonProvider:
        provider = _make_provider()
        provider._yandex_provider = MagicMock()
        provider._in_use_by_player = "player1"
        provider._active_session_id = "session-1"
        ynison = MagicMock()
        ynison.connected = True
        ynison.state.is_paused = False
        ynison.state.current_track_id = "track42"
        ynison.state.player_state = {"status": {"paused": False}}
        ynison.update_playing_status = AsyncMock()
        provider._ynison = ynison
        return provider

    @staticmethod
    def _spy_signal(provider: YandexYnisonProvider) -> list[int]:
        calls: list[int] = []

        async def _spy() -> None:
            calls.append(1)

        _stub_attr(provider, "_signal_track_completion", _spy)
        return calls

    @staticmethod
    def _gate_outer_loop_after_signal(provider: YandexYnisonProvider) -> None:
        """
        `_wait_for_track_change` returns False → outer loop exits.

        ``natural_end`` calls `_wait_for_track_change`; we use its
        return as the gate so the test terminates AFTER natural_end
        has had a chance to evaluate. For non-natural-end paths
        (track-change / session-change), `_wait_for_track_change` is
        never called — we gate those via `_stream_stop_event.set()`
        inside the chunk-yield stub, AFTER one full outer iteration.
        """

        async def _wait_false(_old: str, timeout: float = 30.0) -> bool:  # noqa: ARG001
            provider._stream_stop_event.set()
            return False

        _stub_attr(provider, "_wait_for_track_change", _wait_false)

    @staticmethod
    async def _drive_to_exhaustion(provider: YandexYnisonProvider) -> None:
        streamdetails = MagicMock()
        gen = provider.get_audio_stream(streamdetails, seek_position=0)
        try:
            with suppress(StopAsyncIteration):
                async for _ in gen:
                    pass
        finally:
            with suppress(StopAsyncIteration, asyncio.CancelledError):
                await gen.aclose()

    async def test_clean_exhaustion_signals_completion(self) -> None:
        """Inner loop exhausts naturally → `_signal_track_completion` fires once."""
        provider = self._build()
        calls = self._spy_signal(provider)
        self._gate_outer_loop_after_signal(provider)

        async def _natural_end(
            _track_id: str, *, seek_ms: int = 0, session_params: dict[str, Any] | None = None
        ) -> Any:
            del seek_ms, session_params  # signature-compat with _stream_track
            yield b"\x00\x00\x00\x00"
            # generator exhausts cleanly — no break flag set

        _stub_attr(provider, "_stream_track", _natural_end)

        await self._drive_to_exhaustion(provider)

        assert calls == [1]

    async def test_track_change_during_chunk_loop_suppresses_signal(self) -> None:
        """
        `_track_changed_event` set mid-stream → natural_end False → no signal.

        Uses a two-invocation stub: first call arms `_track_changed_event`
        (the natural_end check we want to verify), second call sets
        `_stream_stop_event` to terminate the test. If we set the stop
        event in the first call, the stop-event guard above natural_end
        would short-circuit before the differentiation logic runs.
        """
        provider = self._build()
        calls = self._spy_signal(provider)

        invocation_count = 0

        async def _two_pass_stream(
            _track_id: str, *, seek_ms: int = 0, session_params: dict[str, Any] | None = None
        ) -> Any:
            del seek_ms, session_params  # signature-compat with _stream_track
            nonlocal invocation_count
            invocation_count += 1
            yield b"\x00\x00\x00\x00"
            if invocation_count == 1:
                # First pass: arm the break flag. natural_end will see it
                # and (correctly) suppress the signal. Outer loop re-iterates.
                provider._track_changed_event.set()
            else:
                # Second pass: stop the test.
                provider._stream_stop_event.set()

        _stub_attr(provider, "_stream_track", _two_pass_stream)

        await self._drive_to_exhaustion(provider)

        assert calls == []
        assert invocation_count == 2  # natural_end must have evaluated on pass 1

    async def test_session_change_during_chunk_loop_suppresses_signal(self) -> None:
        """
        Session-id rotation mid-stream → `broke_for_session_change` → no signal.

        The session-mismatch breaks both the inner chunk loop's break
        guard AND the outer-loop's session check, so the generator
        exits after one iteration without further help — `natural_end`
        runs once with `broke_for_session_change=True` and must not
        signal.
        """
        provider = self._build()
        calls = self._spy_signal(provider)

        async def _yield_then_rotate_session(
            _track_id: str, *, seek_ms: int = 0, session_params: dict[str, Any] | None = None
        ) -> Any:
            del seek_ms, session_params  # signature-compat with _stream_track
            yield b"\x00\x00\x00\x00"
            provider._active_session_id = "different-session"

        _stub_attr(provider, "_stream_track", _yield_then_rotate_session)

        await self._drive_to_exhaustion(provider)

        assert calls == []

    # NOTE: `broke_for_pause` inside natural_end is defensive: in
    # practice every external pause routes through `_pause_playback`,
    # which sets `_stream_stop_event` BEFORE the chunk loop can reach
    # the natural_end check (the stop-event guard at the top of
    # `get_audio_stream`'s outer loop short-circuits first). There is
    # no production code path that lands at natural_end with
    # `is_paused=True` and `_stream_stop_event=False`, so the clause
    # cannot be exercised by a black-box test. It survives as
    # belt-and-braces; intentionally untested.


class _TrackingSlot:
    """Stream-slot double that records acquire/release ordering."""

    def __init__(self, events: list[str], index: int) -> None:
        self._events = events
        self._index = index

    async def __aenter__(self) -> None:
        self._events.append(f"acquired-{self._index}")

    async def __aexit__(self, *_args: object) -> None:
        self._events.append(f"released-{self._index}")


class TestLinkedSlotRelease:
    """The linked provider's stream slot is released as soon as a track stops streaming."""

    @staticmethod
    def _build() -> YandexYnisonProvider:
        provider = _make_provider()
        provider._yandex_provider = MagicMock()
        provider._in_use_by_player = "player1"
        provider._active_session_id = "session-1"
        ynison = MagicMock()
        ynison.connected = True
        ynison.state.is_paused = False
        ynison.state.current_track_id = "track42"
        provider._ynison = ynison
        return provider

    async def test_consumer_close_releases_slot(self) -> None:
        """Closing the audio stream mid-track finalizes the generator holding the slot."""
        provider = self._build()
        events: list[str] = []

        async def _endless_stream(
            _track_id: str, *, seek_ms: int = 0, session_params: dict[str, Any] | None = None
        ) -> Any:
            del seek_ms, session_params  # signature-compat with _stream_track
            async with _TrackingSlot(events, 1):
                while True:
                    yield b"\x00\x00\x00\x00"

        _stub_attr(provider, "_stream_track", _endless_stream)

        gen = provider.get_audio_stream(MagicMock(), seek_position=0)
        assert await anext(gen) == b"\x00\x00\x00\x00"
        assert events == ["acquired-1"]

        await gen.aclose()

        assert events == ["acquired-1", "released-1"]

    async def test_track_change_releases_slot_before_next_track(self) -> None:
        """A track change never leaves the previous track's slot charged."""
        provider = self._build()
        events: list[str] = []
        invocations = 0

        async def _two_pass_stream(
            _track_id: str, *, seek_ms: int = 0, session_params: dict[str, Any] | None = None
        ) -> Any:
            del seek_ms, session_params  # signature-compat with _stream_track
            nonlocal invocations
            invocations += 1
            index = invocations
            async with _TrackingSlot(events, index):
                while True:
                    yield b"\x00\x00\x00\x00"
                    if index == 1:
                        provider._track_changed_event.set()
                    else:
                        provider._stream_stop_event.set()

        _stub_attr(provider, "_stream_track", _two_pass_stream)

        gen = provider.get_audio_stream(MagicMock(), seek_position=0)
        with suppress(StopAsyncIteration):
            async for _ in gen:
                pass

        assert events == ["acquired-1", "released-1", "acquired-2", "released-2"]


class TestBypassThrottlerScope:
    """`BYPASS_THROTTLER` is set inside `_stream_track`, NOT inside prefetch."""

    async def test_bypass_active_during_in_flight_stream_fetch(self) -> None:
        """The in-flight stream-details fetch runs with BYPASS_THROTTLER=True."""
        provider = _make_provider()
        observed: list[bool] = []
        mock_yandex = MagicMock()
        _set_stream_owner(mock_yandex)

        async def _fake_get_stream_details(_track_id: str, _media_type: Any) -> Any:
            observed.append(BYPASS_THROTTLER.get())
            sd = MagicMock()
            sd.expiration = 0
            sd.duration = 1
            sd.audio_format = MagicMock()
            sd.provider = mock_yandex.instance_id
            return sd

        mock_yandex.get_stream_details = AsyncMock(side_effect=_fake_get_stream_details)

        async def _fake_audio(_details: object) -> Any:
            yield b"x"

        mock_yandex.get_audio_stream = _fake_audio
        provider._yandex_provider = mock_yandex

        mock_ynison = MagicMock()
        mock_ynison.update_playing_status = AsyncMock()
        mock_ynison.state.is_paused = False
        provider._ynison = mock_ynison

        async def _fake_ffmpeg(**_kwargs: object) -> Any:
            yield b"pcm"

        with patch(
            "music_assistant.providers.yandex_ynison.provider.get_ffmpeg_stream",
            side_effect=_fake_ffmpeg,
        ):
            gen = provider._stream_track("track1")
            try:
                async for _ in gen:
                    break
            finally:
                await gen.aclose()

        assert observed == [True], (
            f"BYPASS_THROTTLER should be True inside _stream_track, got {observed}"
        )
        assert BYPASS_THROTTLER.get() is False

    async def test_bypass_not_active_during_prefetch(self) -> None:
        """The prefetch path is intentionally NOT bypassed — opportunistic only."""
        provider = _make_provider()
        observed: list[bool] = []
        mock_yandex = MagicMock()
        _set_stream_owner(mock_yandex)

        async def _fake_get_stream_details(_track_id: str, _media_type: Any) -> Any:
            observed.append(BYPASS_THROTTLER.get())
            sd = MagicMock()
            sd.expiration = 0
            sd.audio_format = MagicMock()
            sd.audio_format.sample_rate = 44100
            sd.audio_format.bit_depth = 16
            sd.provider = mock_yandex.instance_id
            return sd

        mock_yandex.get_stream_details = AsyncMock(side_effect=_fake_get_stream_details)
        provider._yandex_provider = mock_yandex

        await provider._prefetch_format_for_track("track1")

        assert observed == [False], (
            f"BYPASS_THROTTLER must NOT be set during prefetch, got {observed}"
        )

    async def test_bypass_resets_on_exception(self) -> None:
        """A raise inside the bypassed call must still reset the context-var."""
        provider = _make_provider()
        mock_yandex = MagicMock()
        mock_yandex.get_stream_details = AsyncMock(side_effect=RuntimeError("boom"))
        provider._yandex_provider = mock_yandex

        mock_ynison = MagicMock()
        mock_ynison.state.is_paused = False
        provider._ynison = mock_ynison

        with pytest.raises(RuntimeError, match="boom"):
            await anext(provider._stream_track("track1"))

        assert BYPASS_THROTTLER.get() is False


# ------------------------------------------------------------------
# Music-token cache (spec 0004)
# ------------------------------------------------------------------


class TestMusicTokenCache:
    """Tests for the in-memory cache around `refresh_music_token`."""

    @staticmethod
    def _linked_provider_with_x_token(
        x_token: str = "xtok-1",  # noqa: S107 — test fixture value
    ) -> tuple[YandexYnisonProvider, MagicMock]:
        """Construct a linked provider whose owner's x-token drives refresh."""
        provider = _make_provider()
        owner = _make_ym_provider_stub(token=None, x_token=x_token)
        _stub_attr(provider.mass, "get_provider", MagicMock(return_value=owner))
        return provider, owner

    async def test_resolve_token_caches_x_token_refresh(self) -> None:
        """Second `_resolve_token` call within TTL is a cache hit."""
        provider, _owner = self._linked_provider_with_x_token("xtok-1")

        with patch(
            "music_assistant.providers.yandex_ynison.provider.refresh_music_token",
            new_callable=AsyncMock,
            return_value=SecretStr("music-tok"),
        ) as mock_refresh:
            t1 = await provider._resolve_token()
            t2 = await provider._resolve_token()

        assert t1.get_secret() == "music-tok"
        assert t2.get_secret() == "music-tok"
        assert mock_refresh.await_count == 1

    async def test_resolve_token_refreshes_after_ttl_expires(self) -> None:
        """Time advancing past the TTL forces a fresh refresh."""
        provider, _owner = self._linked_provider_with_x_token("xtok-1")

        clock = {"now": 1000.0}
        provider._now = lambda: clock["now"]

        with patch(
            "music_assistant.providers.yandex_ynison.provider.refresh_music_token",
            new_callable=AsyncMock,
            return_value=SecretStr("music-tok"),
        ) as mock_refresh:
            await provider._resolve_token()  # cold miss
            clock["now"] += 60 * 60  # +60 min, past 50-min TTL
            await provider._resolve_token()  # must refresh

        assert mock_refresh.await_count == 2

    async def test_refresh_ynison_token_invalidates_cache(self) -> None:
        """A 401-driven refresh must bypass + drop the cached entry."""
        provider, _owner = self._linked_provider_with_x_token("xtok-1")

        with patch(
            "music_assistant.providers.yandex_ynison.provider.refresh_music_token",
            new_callable=AsyncMock,
            return_value=SecretStr("music-tok"),
        ) as mock_refresh:
            # Warm the cache.
            await provider._resolve_token()
            assert mock_refresh.await_count == 1

            # A 401 reconnect triggers the refresh path. The previously
            # cached token is provably stale and must be bypassed.
            mock_refresh.return_value = SecretStr("music-tok-2")
            result = await provider._refresh_ynison_token()

        assert result.get_secret() == "music-tok-2"
        assert mock_refresh.await_count == 2

        # The follow-up resolve uses the new cached value, not a third refresh.
        with patch(
            "music_assistant.providers.yandex_ynison.provider.refresh_music_token",
            new_callable=AsyncMock,
        ) as mock_refresh_after:
            after = await provider._resolve_token()
            assert after.get_secret() == "music-tok-2"
            mock_refresh_after.assert_not_called()

    async def test_concurrent_resolve_token_calls_refresh_once(self) -> None:
        """Two concurrent `_resolve_token` calls coalesce into one refresh."""
        provider, _owner = self._linked_provider_with_x_token("xtok-1")

        refresh_started = asyncio.Event()
        refresh_release = asyncio.Event()

        async def slow_refresh(_x_token: SecretStr) -> SecretStr:
            refresh_started.set()
            await refresh_release.wait()
            return SecretStr("music-tok")

        with patch(
            "music_assistant.providers.yandex_ynison.provider.refresh_music_token",
            side_effect=slow_refresh,
        ) as mock_refresh:
            task_a = asyncio.create_task(provider._resolve_token())
            task_b = asyncio.create_task(provider._resolve_token())
            await refresh_started.wait()
            refresh_release.set()
            r_a, r_b = await asyncio.gather(task_a, task_b)

        assert r_a.get_secret() == "music-tok"
        assert r_b.get_secret() == "music-tok"
        assert mock_refresh.await_count == 1

    async def test_cache_lru_evicts_oldest_after_four_x_tokens(self) -> None:
        """When a 5th distinct x_token arrives, the oldest entry is evicted."""
        provider, owner = self._linked_provider_with_x_token("xtok-1")

        async def fake_refresh(x_token: SecretStr) -> SecretStr:
            return SecretStr(f"music-for-{x_token.get_secret()}")

        with patch(
            "music_assistant.providers.yandex_ynison.provider.refresh_music_token",
            side_effect=fake_refresh,
        ):
            for i in range(1, 6):
                owner.get_setup_value.side_effect = {
                    "token": None,
                    "x_token": f"xtok-{i}",
                }.get
                await provider._resolve_token()

        import hashlib  # noqa: PLC0415 — test-local

        # 4-entry LRU after 5 distinct keys → oldest evicted, newest retained.
        assert len(provider._token_cache) == 4
        gone = hashlib.sha256(b"xtok-1").hexdigest()
        still_here = hashlib.sha256(b"xtok-5").hexdigest()
        assert gone not in provider._token_cache
        assert still_here in provider._token_cache

    async def test_cache_does_not_log_secrets_or_hashes(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """No log record may contain the x_token, the music token, or its hash."""
        provider, _owner = self._linked_provider_with_x_token("xtok-secret")

        with (
            caplog.at_level("DEBUG"),
            patch(
                "music_assistant.providers.yandex_ynison.provider.refresh_music_token",
                new_callable=AsyncMock,
                return_value=SecretStr("music-secret"),
            ),
        ):
            await provider._resolve_token()
            await provider._refresh_ynison_token()

        import hashlib  # noqa: PLC0415 — test-local

        forbidden = (
            "xtok-secret",
            "music-secret",
            hashlib.sha256(b"xtok-secret").hexdigest(),
        )
        for record in caplog.records:
            blob = record.getMessage()
            for needle in forbidden:
                assert needle not in blob, f"Credential leaked: {needle!r} in {blob!r}"


# ------------------------------------------------------------------
# Strict-mode delivery signalling (spec 0003)
# ------------------------------------------------------------------


class TestStrictModeDeliverySignal:
    """Tests for `_send`/`update_*` strict-mode propagation in `provider.py`."""

    async def test_on_play_raises_player_command_failed_when_send_fails(self) -> None:
        """`_on_play` translates `YnisonSendError` into `PlayerCommandFailed`."""
        provider = _make_provider()
        provider._actual_duration_ms = 120000
        state = _make_ynison_state(progress_ms=5000, duration_ms=120000, paused=True)
        mock_yn = _mock_ynison(state)
        mock_yn.update_playing_status = AsyncMock(side_effect=YnisonSendError("ws down"))
        provider._ynison = mock_yn

        with pytest.raises(PlayerCommandFailed):
            await provider._on_play()

    async def test_on_pause_raises_player_command_failed_when_send_fails(self) -> None:
        """`_on_pause` translates `YnisonSendError` into `PlayerCommandFailed`."""
        provider = _make_provider()
        provider._actual_duration_ms = 120000
        state = _make_ynison_state(progress_ms=5000, duration_ms=120000, paused=False)
        mock_yn = _mock_ynison(state)
        mock_yn.update_playing_status = AsyncMock(side_effect=YnisonSendError("ws down"))
        provider._ynison = mock_yn

        with pytest.raises(PlayerCommandFailed):
            await provider._on_pause()

    async def test_on_seek_raises_player_command_failed_when_send_fails(self) -> None:
        """`_on_seek` raises `PlayerCommandFailed` and leaves local seek state untouched."""
        provider = _make_provider()
        provider._actual_duration_ms = 200000
        provider._seek_position_ms = 0
        provider._track_changed_event.clear()
        state = _make_ynison_state(progress_ms=5000, duration_ms=200000, paused=False)
        mock_yn = _mock_ynison(state)
        mock_yn.update_playing_status = AsyncMock(side_effect=YnisonSendError("ws down"))
        provider._ynison = mock_yn

        with pytest.raises(PlayerCommandFailed):
            await provider._on_seek(30)  # 30 seconds

        # Local seek state must NOT be advanced past a send that never landed.
        assert provider._seek_position_ms == 0
        assert not provider._track_changed_event.is_set()

    async def test_signal_track_completion_logs_on_send_failure(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """`_signal_track_completion` swallows `YnisonSendError` and logs a warning."""
        provider = _make_provider()
        state = YnisonState(
            active_device_id=provider._device_id,
            player_state={
                "status": {"paused": False, "progress_ms": 180000, "duration_ms": 200000},
                "player_queue": {
                    "current_playable_index": 0,
                    "playable_list": [{"playable_id": "t1"}, {"playable_id": "t2"}],
                    "entity_type": "PLAYLIST",
                },
            },
        )
        mock_ynison = MagicMock()
        mock_ynison.state = state
        mock_ynison.connected = True
        mock_ynison.device_id = provider._device_id
        mock_ynison.update_playing_status = AsyncMock(side_effect=YnisonSendError("ws down"))
        mock_ynison.update_player_state = AsyncMock()
        provider._ynison = mock_ynison

        # Must not raise — end-of-track has no command to fail back to.
        with caplog.at_level("WARNING"):
            await provider._signal_track_completion()
        assert any("Track-completion signal dropped" in r.message for r in caplog.records)

    async def test_advance_queue_index_returns_on_send_failure(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """`_advance_queue_index` swallows `YnisonSendError` and logs a warning."""
        provider = _make_provider()
        state = _make_ynison_state(
            current_playable_index=0,
            playable_list=[{"playable_id": "t1"}, {"playable_id": "t2"}],
        )
        mock_yn = _mock_ynison(state)
        mock_yn.update_player_state = AsyncMock(side_effect=YnisonSendError("ws down"))
        provider._ynison = mock_yn

        with caplog.at_level("WARNING"):
            await provider._advance_queue_index(1)
        assert any("Queue-advance dropped" in r.message for r in caplog.records)

    async def test_sync_progress_uses_non_strict_send(self) -> None:
        """
        `_sync_progress` heartbeat must call into the non-strict send path.

        Regression guard: heartbeats stay fire-and-forget so a single bad
        send tick does not crash the streaming generator. We verify the
        forwarded `strict=False` kwarg rather than the bubble-up
        behaviour (which is the contract of the underlying `_send`
        already covered in `tests/test_ynison_client.py`).
        """
        provider = _make_provider()
        provider._actual_duration_ms = 200000
        provider._stream_metadata.duration = 200
        state = _make_ynison_state(progress_ms=5000, duration_ms=200000, paused=False)
        mock_yn = _mock_ynison(state)
        provider._ynison = mock_yn

        await provider._sync_progress(seek_ms=0, bytes_yielded=0, player_id=None)

        mock_yn.update_playing_status.assert_awaited_once()
        _args, kwargs = mock_yn.update_playing_status.call_args
        assert kwargs.get("strict", False) is False

    async def test_send_progress_strict_raises_when_not_connected(self) -> None:
        """`_send_progress_to_ynison(strict=True)` raises when Ynison is disconnected."""
        provider = _make_provider()
        provider._ynison = _mock_ynison(connected=False)

        with pytest.raises(YnisonSendError):
            await provider._send_progress_to_ynison(
                progress_ms=1000, duration_ms=2000, paused=False, strict=True
            )

    async def test_send_progress_non_strict_silent_when_not_connected(self) -> None:
        """`_send_progress_to_ynison` default behaviour stays silent when disconnected."""
        provider = _make_provider()
        provider._ynison = _mock_ynison(connected=False)

        # Must not raise
        await provider._send_progress_to_ynison(progress_ms=1000, duration_ms=2000, paused=False)


# ------------------------------------------------------------------
# Connected-Ynison guard helper (spec 0005)
# ------------------------------------------------------------------


class TestRequireConnectedYnison:
    """Tests for the extracted `_require_connected_ynison` helper."""

    def test_raises_unsupported_when_client_missing(self) -> None:
        """`_ynison is None` raises `UnsupportedFeaturedException`."""
        provider = _make_provider()
        provider._ynison = None

        with pytest.raises(UnsupportedFeaturedException, match="not initialized"):
            provider._require_connected_ynison()

    def test_raises_command_failed_when_disconnected(self) -> None:
        """`_ynison.connected is False` raises `PlayerCommandFailed`."""
        provider = _make_provider()
        provider._ynison = _mock_ynison(connected=False)

        with pytest.raises(PlayerCommandFailed, match="disconnected"):
            provider._require_connected_ynison()

    def test_returns_client_when_ready(self) -> None:
        """Happy path returns the live client unchanged."""
        provider = _make_provider()
        mock_yn = _mock_ynison(connected=True)
        provider._ynison = mock_yn

        result = provider._require_connected_ynison()
        assert result is mock_yn
