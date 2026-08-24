"""Tests for the AudioSource refactor — the new PluginProvider contract."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import (
    ContentType,
    MediaType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
    SourceControl,
    StreamType,
    VolumeNormalizationMode,
)
from music_assistant_models.errors import (
    MediaNotFoundError,
    UnsupportedFeaturedException,
)
from music_assistant_models.media_items import (
    AudioFormat,
    AudioSource,
    ProviderMapping,
    SoundEffect,
)
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.streamdetails import StreamDetails, StreamMetadata

from music_assistant.constants import CONF_VOLUME_NORMALIZATION_TARGET
from music_assistant.controllers.players import PlayerController
from music_assistant.controllers.streams.audio import StreamsAudio
from music_assistant.mass import MusicAssistant
from music_assistant.models.plugin import PluginProvider
from tests.common import MockPlayer, MockProvider

# -------------------------------------------------------------------- fixtures


@pytest.fixture
def mock_mass() -> MagicMock:
    """Create a mock MusicAssistant instance."""
    mass = MagicMock()
    mass.closing = False
    mass.loop = None
    mass.config = MagicMock()
    mass.config.get = MagicMock(return_value=[])
    mass.config.get_raw_player_config_value = MagicMock(
        side_effect=lambda _player_id, _key, default=None: default
    )
    mass.config.get_raw_core_config_value = MagicMock(return_value="GLOBAL")
    mass.signal_event = MagicMock()
    mass.get_providers = MagicMock(return_value=[])
    mass.streams = MagicMock()
    mass.streams.update_stream_metadata = MagicMock()
    mass.player_queues = MagicMock()
    mass.player_queues.get = MagicMock(return_value=None)
    mass.player_queues.play_media = AsyncMock()
    return mass


@pytest.fixture
def controller(mock_mass: MagicMock) -> PlayerController:
    """Create a PlayerController instance."""
    ctrl = PlayerController(mock_mass)
    mock_mass.players = ctrl
    return ctrl


@pytest.fixture
def provider(mock_mass: MagicMock) -> MockProvider:
    """Create a mock provider."""
    return MockProvider("test_provider", instance_id="test_prov", mass=mock_mass)


@pytest.fixture
def player(provider: MockProvider, controller: PlayerController) -> MockPlayer:
    """Create and register a mock player."""
    p = MockPlayer(provider, "player_1", "Test Player")
    p._attr_supported_features = {PlayerFeature.VOLUME_SET}
    controller._players = {"player_1": p}
    return p


def _audio_format() -> AudioFormat:
    """PCM 16/44.1/2 — what most plugin sources publish."""
    return AudioFormat(
        content_type=ContentType.PCM_S16LE,
        sample_rate=44100,
        bit_depth=16,
        channels=2,
    )


def _audio_source(
    *,
    item_id: str = "main",
    provider_id: str = "fake_plugin",
    can_play_pause: bool = False,
    can_seek: bool = False,
    can_next_previous: bool = False,
    exclusive: bool = True,
) -> AudioSource:
    """Build an AudioSource with given capability flags."""
    audio_format = _audio_format()
    return AudioSource(
        item_id=item_id,
        provider=provider_id,
        name="Fake Plugin",
        provider_mappings={
            ProviderMapping(
                item_id=item_id,
                provider_domain="fake_plugin",
                provider_instance=provider_id,
                audio_format=audio_format,
            )
        },
        can_play_pause=can_play_pause,
        can_seek=can_seek,
        can_next_previous=can_next_previous,
        exclusive=exclusive,
        allow_external_trigger=False,
    )


class _FakePluginProvider:
    """
    Duck-typed stand-in for a PluginProvider used to verify the new contract.

    Does not inherit from PluginProvider because that would require setting up
    a real mass instance + ProviderConfig + ProviderManifest just to run a
    handful of contract assertions. The tests below only call the new contract
    methods (get_audio_sources, get_stream_details, etc.), so duck-typing is
    sufficient.
    """

    domain = "fake_plugin"
    instance_id = "fake_plugin"

    def __init__(self, audio_source: AudioSource) -> None:
        self._audio_source = audio_source
        self._in_use_by_player: str | None = None
        self._active_session_id: str | None = None
        self.control_calls: list[tuple[SourceControl, int | None]] = []
        self.selected_calls: list[tuple[str, str]] = []

    async def get_audio_sources(self) -> list[AudioSource]:
        return [self._audio_source]

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        # Side-effect-free: ownership is claimed in on_source_selected. This
        # mirrors the contract every real plugin implements so preload paths
        # can fetch streamdetails without blocking a later handoff.
        del media_type
        if item_id != self._audio_source.item_id:
            raise MediaNotFoundError(f"Unknown AudioSource: {item_id}")
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=_audio_format(),
            media_type=MediaType.AUDIO_SOURCE,
            stream_type=StreamType.CUSTOM,
            stream_metadata=StreamMetadata(title="Fake"),
        )

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes]:
        del streamdetails, seek_position
        # Snapshot BOTH the queue id and the session id at stream start. The
        # queue-id-only guard is unsafe for same-queue reconnects: a fresh
        # on_source_selected refreshes _active_session_id without changing
        # _in_use_by_player, so the prior generator's teardown would otherwise
        # clobber the new session's claim.
        consumer_queue = self._in_use_by_player
        captured_session_id = self._active_session_id
        try:
            yield b"\x00" * 4096
        finally:
            if (
                self._in_use_by_player == consumer_queue
                and self._active_session_id == captured_session_id
            ):
                self._in_use_by_player = None

    async def on_source_control(
        self,
        source_id: str,
        action: SourceControl,
        value: int | None = None,
    ) -> None:
        assert source_id == self._audio_source.item_id
        self.control_calls.append((action, value))

    async def on_source_selected(
        self, source_id: str, player_id: str, queue_id: str, stream_session_id: str
    ) -> None:
        assert source_id == self._audio_source.item_id
        self.selected_calls.append((source_id, player_id))
        # Claim ownership for this queue. Overwriting any prior claim is
        # intentional — that is exactly how a cross-queue handoff works.
        self._in_use_by_player = queue_id
        # Record this request's session id so a later on_source_unselected can
        # reject stale callbacks from superseded same-queue requests.
        self._active_session_id = stream_session_id

    async def on_source_unselected(
        self, source_id: str, queue_id: str, stream_session_id: str
    ) -> None:
        assert source_id == self._audio_source.item_id
        # Reject stale callbacks: only release if this is still the active
        # session. The queue_id-only check would let an old request's late
        # teardown clobber the live claim of a same-queue reconnect.
        if self._active_session_id != stream_session_id:
            return
        self._active_session_id = None
        if self._in_use_by_player == queue_id:
            self._in_use_by_player = None


# -------------------------------------------------------------------- contract


class TestAudioSourceContract:
    """Verify the new PluginProvider contract surfaces correctly."""

    @pytest.mark.asyncio
    async def test_get_audio_sources_returns_audiosource(self) -> None:
        """The provider should expose its AudioSource for browse."""
        source = _audio_source()
        prov = _FakePluginProvider(source)
        result = await prov.get_audio_sources()
        assert result == [source]
        assert result[0].media_type == MediaType.AUDIO_SOURCE

    @pytest.mark.asyncio
    async def test_get_stream_details_is_side_effect_free(self) -> None:
        """get_stream_details must not claim the lock — that lives in on_source_selected."""
        # This is the core invariant that lets queue preload fetch streamdetails
        # without blocking a later cross-queue handoff at the actual stream
        # request. See PluginProvider.get_stream_details docstring.
        prov = _FakePluginProvider(_audio_source())
        sd = await prov.get_stream_details("main", MediaType.AUDIO_SOURCE)
        assert sd.media_type == MediaType.AUDIO_SOURCE
        assert sd.stream_type == StreamType.CUSTOM
        assert sd.stream_metadata is not None
        assert prov._in_use_by_player is None
        # Calling it again from a different queue must also be side-effect-free
        # — no busy raise, no claim — so preload from any queue is safe.
        sd2 = await prov.get_stream_details("main", MediaType.AUDIO_SOURCE)
        assert sd2.media_type == MediaType.AUDIO_SOURCE
        assert prov._in_use_by_player is None

    @pytest.mark.asyncio
    async def test_on_source_selected_claims_lock(self) -> None:
        """on_source_selected is the single point where exclusive ownership is claimed."""
        prov = _FakePluginProvider(_audio_source())
        await prov.on_source_selected("main", "player_a", "queue_a", "session_1")
        assert prov._in_use_by_player == "queue_a"
        assert prov._active_session_id == "session_1"

    @pytest.mark.asyncio
    async def test_get_stream_details_unknown_source_raises_not_found(self) -> None:
        """An unknown item_id should surface as MediaNotFoundError."""
        prov = _FakePluginProvider(_audio_source())
        with pytest.raises(MediaNotFoundError):
            await prov.get_stream_details("bogus", MediaType.AUDIO_SOURCE)

    @pytest.mark.asyncio
    async def test_get_audio_stream_releases_lock_in_finally(self) -> None:
        """When the audio generator exits, the queue lock should be released."""
        prov = _FakePluginProvider(_audio_source())
        await prov.on_source_selected("main", "player_a", "queue_a", "session_1")
        sd = await prov.get_stream_details("main", MediaType.AUDIO_SOURCE)
        gen = prov.get_audio_stream(sd)
        await gen.__anext__()
        await gen.aclose()
        assert prov._in_use_by_player is None

    @pytest.mark.asyncio
    async def test_on_source_control_records_action_and_value(self) -> None:
        """on_source_control is the proxy for play/pause/next/prev/seek/volume."""
        prov = _FakePluginProvider(_audio_source())
        await prov.on_source_control("main", SourceControl.SEEK, 42)
        await prov.on_source_control("main", SourceControl.NEXT)
        assert prov.control_calls == [
            (SourceControl.SEEK, 42),
            (SourceControl.NEXT, None),
        ]

    @pytest.mark.asyncio
    async def test_handoff_overwrites_prior_claim_in_on_source_selected(self) -> None:
        """A second on_source_selected from a different queue overwrites the claim."""
        # Handoff happens entirely inside on_source_selected: the new queue's
        # call replaces _in_use_by_player. get_stream_details is unaffected by
        # the previous queue's state because it does not claim or check.
        prov = _FakePluginProvider(_audio_source())
        await prov.on_source_selected("main", "player_a", "queue_a", "session_1")
        await prov.get_stream_details("main", MediaType.AUDIO_SOURCE)
        assert prov._in_use_by_player == "queue_a"
        # Different queue selects the source (cross-queue handoff).
        await prov.on_source_selected("main", "player_b", "queue_b", "session_2")
        assert prov._in_use_by_player == "queue_b"
        # get_stream_details for the new queue still succeeds — no busy raise.
        sd = await prov.get_stream_details("main", MediaType.AUDIO_SOURCE)
        assert sd.media_type == MediaType.AUDIO_SOURCE

    @pytest.mark.asyncio
    async def test_on_source_unselected_releases_own_session(self) -> None:
        """on_source_unselected releases the claim when the session id matches."""
        prov = _FakePluginProvider(_audio_source())
        await prov.on_source_selected("main", "player_a", "queue_a", "session_1")
        await prov.get_stream_details("main", MediaType.AUDIO_SOURCE)
        await prov.on_source_unselected("main", "queue_a", "session_1")
        assert prov._in_use_by_player is None
        assert prov._active_session_id is None

    @pytest.mark.asyncio
    async def test_on_source_unselected_ignores_stale_callback_after_handoff(self) -> None:
        """A late on_source_unselected after a handoff must not clobber the new claim."""
        # Stale callback scenario: queue A's stream finally fires AFTER queue B
        # has already taken over. The session-id guard must reject it.
        prov = _FakePluginProvider(_audio_source())
        await prov.on_source_selected("main", "player_a", "queue_a", "session_1")
        await prov.get_stream_details("main", MediaType.AUDIO_SOURCE)
        # Handoff: B selects the source with a fresh session id, releasing A's claim
        await prov.on_source_selected("main", "player_b", "queue_b", "session_2")
        await prov.get_stream_details("main", MediaType.AUDIO_SOURCE)
        assert prov._in_use_by_player == "queue_b"
        # Now queue A's late unselect (with the OLD session id) fires — must no-op
        await prov.on_source_unselected("main", "queue_a", "session_1")
        assert prov._in_use_by_player == "queue_b"
        assert prov._active_session_id == "session_2"

    @pytest.mark.asyncio
    async def test_get_audio_stream_finally_does_not_clobber_reconnected_claim(self) -> None:
        """The generator's finally must not release a same-queue reconnect's live claim."""
        # Reviewer-flagged scenario: stream 1's generator close fires AFTER
        # stream 2 (a same-queue reconnect) has already called
        # on_source_selected with a fresh session id. Stream 1's queue id
        # snapshot still matches _in_use_by_player (same queue), so a
        # queue-id-only guard would clear the lock that now belongs to
        # stream 2. The session-id guard prevents this.
        prov = _FakePluginProvider(_audio_source())
        # Stream 1 starts
        await prov.on_source_selected("main", "player_a", "queue_a", "session_1")
        sd = await prov.get_stream_details("main", MediaType.AUDIO_SOURCE)
        gen1 = prov.get_audio_stream(sd)
        await gen1.__anext__()
        # Same-queue reconnect arrives before stream 1's generator finishes
        await prov.on_source_selected("main", "player_a", "queue_a", "session_2")
        # Lock is now claimed for session_2 (same queue id, refreshed session)
        assert prov._in_use_by_player == "queue_a"
        assert prov._active_session_id == "session_2"
        # Stream 1's generator now closes — its finally MUST NOT clear the lock
        await gen1.aclose()
        post_release_queue: str | None = prov._in_use_by_player
        post_release_session: str | None = prov._active_session_id
        assert post_release_queue == "queue_a"
        assert post_release_session == "session_2"

    @pytest.mark.asyncio
    async def test_reconnect_with_cached_streamdetails_reclaims_lock(self) -> None:
        """A follow-up GET that reuses cached streamdetails must re-fire the claim."""
        # The streams controller fires on_source_selected for every AudioSource
        # GET, regardless of whether streamdetails are cached from a previous
        # request. So a disconnect/reconnect for the same queue item re-claims
        # the lock with a fresh session id, even though get_stream_details is
        # skipped via the cache. Verify the provider-level invariant: a new
        # on_source_selected fully re-establishes ownership.
        prov = _FakePluginProvider(_audio_source())
        # Request 1 — full lifecycle
        await prov.on_source_selected("main", "player_a", "queue_a", "session_1")
        await prov.get_stream_details("main", MediaType.AUDIO_SOURCE)
        await prov.on_source_unselected("main", "queue_a", "session_1")
        # Read into a locally-typed var so mypy's literal narrowing doesn't
        # chain across the next await (which mutates the attribute it can't see).
        post_release: str | None = prov._in_use_by_player
        assert post_release is None
        # Request 2 — same queue/player, no fresh get_stream_details (caller
        # has cached streamdetails). on_source_selected MUST re-claim.
        await prov.on_source_selected("main", "player_a", "queue_a", "session_2")
        post_reclaim_queue: str | None = prov._in_use_by_player
        post_reclaim_session: str | None = prov._active_session_id
        assert post_reclaim_queue == "queue_a"
        assert post_reclaim_session == "session_2"

    @pytest.mark.asyncio
    async def test_preload_get_stream_details_does_not_block_handoff(self) -> None:
        """Queue preload must not claim the source and block a later handoff."""
        # Reviewer-flagged scenario: player_queues._load_item calls
        # audio.get_stream_details during queue preparation, BEFORE the HTTP
        # stream request reaches serve_queue_item_stream. If get_stream_details
        # claimed the lock, a cross-queue takeover would fail here with
        # ResourceBusyError before the new queue's on_source_selected ever
        # gets a chance to do the handoff. Pushing the claim into
        # on_source_selected keeps preload side-effect-free.
        prov = _FakePluginProvider(_audio_source())
        # Queue A is actively streaming
        await prov.on_source_selected("main", "player_a", "queue_a", "session_1")
        await prov.get_stream_details("main", MediaType.AUDIO_SOURCE)
        assert prov._in_use_by_player == "queue_a"
        # Queue B's preload fetches streamdetails — must NOT raise busy, must
        # NOT alter ownership.
        sd = await prov.get_stream_details("main", MediaType.AUDIO_SOURCE)
        assert sd.media_type == MediaType.AUDIO_SOURCE
        assert prov._in_use_by_player == "queue_a"
        # Queue B's actual stream request fires on_source_selected, which
        # takes over cleanly.
        await prov.on_source_selected("main", "player_b", "queue_b", "session_2")
        assert prov._in_use_by_player == "queue_b"

    @pytest.mark.asyncio
    async def test_on_source_unselected_ignores_stale_callback_same_queue_reconnect(
        self,
    ) -> None:
        """A same-queue reconnect's late teardown must not clobber the live stream."""
        # The reviewer-flagged scenario: player drops, reopens a NEW GET for the
        # SAME queue before the first request's finally fires. Queue id matches
        # in both, so a queue-id-only guard would let the stale callback clear
        # the live claim. The session-id guard rejects it.
        prov = _FakePluginProvider(_audio_source())
        # Request 1 starts streaming
        await prov.on_source_selected("main", "player_a", "queue_a", "session_1")
        await prov.get_stream_details("main", MediaType.AUDIO_SOURCE)
        # Request 2 (reconnect) arrives BEFORE request 1's finally runs.
        # Same queue + same player → no handoff branch, just a fresh session id.
        await prov.on_source_selected("main", "player_a", "queue_a", "session_2")
        # Lock stays held (same queue, same player); only session id rolled forward
        assert prov._in_use_by_player == "queue_a"
        assert prov._active_session_id == "session_2"
        # Now request 1's late finally fires with the stale session id — no-op
        await prov.on_source_unselected("main", "queue_a", "session_1")
        # Read into locally-typed vars so the previous literal narrowing
        # ("session_2") doesn't make mypy treat the post-release assertions
        # below as unreachable.
        post_stale_queue: str | None = prov._in_use_by_player
        post_stale_session: str | None = prov._active_session_id
        assert post_stale_queue == "queue_a"
        assert post_stale_session == "session_2"
        # Request 2's finally then fires with the current session id — releases
        await prov.on_source_unselected("main", "queue_a", "session_2")
        post_release_queue: str | None = prov._in_use_by_player
        post_release_session: str | None = prov._active_session_id
        assert post_release_queue is None
        assert post_release_session is None


# ---------------------------------------------------------- active source helper


class TestTransportCommandsRedirectToQueue:
    """
    players/cmd next/previous/seek hand off to the queue controller.

    With no live source playing, the queue is what the player is playing, so the
    command is its business. A live source takes the command first — see
    tests/controllers/players/test_live_source_dispatch.py.
    """

    def _wire_active_queue(self, mock_mass: MagicMock) -> None:
        """Point the mocked queue registry at the player's own (active) queue."""
        mock_mass.player_queues.get = MagicMock(return_value=SimpleNamespace(queue_id="player_1"))

    @pytest.mark.asyncio
    async def test_cmd_next_redirects_to_the_queue_controller(
        self,
        controller: PlayerController,
        player: MockPlayer,
        mock_mass: MagicMock,
    ) -> None:
        """players/cmd/next reaches player_queues.next for the active queue."""
        self._wire_active_queue(mock_mass)
        mock_mass.player_queues.next = AsyncMock()

        await controller.cmd_next_track("player_1")

        mock_mass.player_queues.next.assert_awaited_once_with("player_1")

    @pytest.mark.asyncio
    async def test_cmd_previous_redirects_to_the_queue_controller(
        self,
        controller: PlayerController,
        player: MockPlayer,
        mock_mass: MagicMock,
    ) -> None:
        """players/cmd/previous reaches player_queues.previous for the active queue."""
        self._wire_active_queue(mock_mass)
        mock_mass.player_queues.previous = AsyncMock()

        await controller.cmd_previous_track("player_1")

        mock_mass.player_queues.previous.assert_awaited_once_with("player_1")

    @pytest.mark.asyncio
    async def test_cmd_seek_redirects_to_the_queue_controller(
        self,
        controller: PlayerController,
        player: MockPlayer,
        mock_mass: MagicMock,
    ) -> None:
        """players/cmd/seek reaches player_queues.seek for the active queue."""
        self._wire_active_queue(mock_mass)
        mock_mass.player_queues.seek = AsyncMock()

        await controller.cmd_seek("player_1", 42)

        mock_mass.player_queues.seek.assert_awaited_once_with("player_1", 42)


# ------------------------------------------------------------- update_stream_metadata


def _make_audio_source_session(
    elapsed_time: int | None,
    elapsed_time_last_updated: float | None = None,
    player_id: str = "player_1",
) -> MagicMock:
    """Build a session mock whose live source reports the given position."""
    sd = StreamDetails(
        provider="fake_plugin",
        item_id="main",
        audio_format=_audio_format(),
        media_type=MediaType.AUDIO_SOURCE,
        stream_type=StreamType.CUSTOM,
    )
    session = MagicMock()
    session.player_id = player_id
    session.streamdetails = sd
    session.source_uri = "fake_plugin://audio_source/main"
    session.source.image = None
    session.stream_metadata = StreamMetadata(title="Live Track")
    session.stream_metadata.elapsed_time = elapsed_time
    session.stream_metadata.elapsed_time_last_updated = elapsed_time_last_updated
    return session


class TestAudioSourceElapsedTimeOverride:
    """
    Verify PlayerState.elapsed_time prefers what the live source reports.

    The override is load-bearing — player.state.corrected_elapsed_time is
    consumed by the queue controller's resume logic and several player
    providers; without it those flows would run against the byte-consumed
    clock and lose upstream seeks / pause-resume on a live source.
    """

    def test_audio_source_elapsed_time_preferred_over_player(
        self,
        provider: MockProvider,
        controller: PlayerController,
        mock_mass: MagicMock,
    ) -> None:
        """AudioSource stream_metadata.elapsed_time overrides the player clock."""
        player = MockPlayer(provider, "player_1", "Test Player")
        player._attr_playback_state = PlaybackState.PLAYING
        player._attr_elapsed_time = 10.0  # physical player reports 10s
        player._attr_elapsed_time_last_updated = time.time() - 5
        player._attr_active_source = "player_1"

        controller._players = {"player_1": player}

        session = _make_audio_source_session(elapsed_time=42)
        mock_mass.players.get_audio_source_session = MagicMock(return_value=session)

        player.update_state(signal_event=False)

        # Override wins over the player's own elapsed_time
        assert player.state.elapsed_time == 42

    def test_audio_source_no_elapsed_time_falls_through(
        self,
        provider: MockProvider,
        controller: PlayerController,
        mock_mass: MagicMock,
    ) -> None:
        """Without stream_metadata.elapsed_time, use the player's own clock."""
        player = MockPlayer(provider, "player_1", "Test Player")
        player._attr_playback_state = PlaybackState.PLAYING
        player._attr_elapsed_time = 10.0
        player._attr_elapsed_time_last_updated = time.time()
        player._attr_active_source = "player_1"

        controller._players = {"player_1": player}

        session = _make_audio_source_session(elapsed_time=None)
        mock_mass.players.get_audio_source_session = MagicMock(return_value=session)

        player.update_state(signal_event=False)

        assert player.state.elapsed_time == 10.0

    def test_audio_source_elapsed_time_with_output_protocol(
        self,
        provider: MockProvider,
        controller: PlayerController,
        mock_mass: MagicMock,
    ) -> None:
        """Override wins even when an output protocol is active."""
        # Protocol player (e.g. AirPlay) reporting bytes-consumed
        protocol_player = MockPlayer(
            provider, "airplay_1", "AirPlay", player_type=PlayerType.PROTOCOL
        )
        protocol_player._attr_playback_state = PlaybackState.PLAYING
        protocol_player._attr_elapsed_time = 5.0
        protocol_player._attr_elapsed_time_last_updated = time.time()
        # Main player with active output protocol
        player = MockPlayer(provider, "player_1", "Test Player")
        player._attr_playback_state = PlaybackState.PLAYING
        player._attr_elapsed_time = 8.0
        player._attr_elapsed_time_last_updated = time.time()
        player._attr_active_source = "player_1"
        player.set_active_output_protocol("airplay_1")

        controller._players = {"player_1": player, "airplay_1": protocol_player}

        session = _make_audio_source_session(elapsed_time=42)
        mock_mass.players.get_audio_source_session = MagicMock(return_value=session)

        protocol_player.update_state(signal_event=False)
        # refresh: the mocked queue/protocol player are cross-source state and the
        # production fan-out (which marks the player dirty) is suppressed here
        player.refresh_state(signal_event=False)

        # The override is layered AFTER protocol/sync resolution, so it wins
        assert player.state.elapsed_time == 42

    def test_audio_source_elapsed_time_last_updated_fallback(
        self,
        provider: MockProvider,
        controller: PlayerController,
        mock_mass: MagicMock,
    ) -> None:
        """When stream_metadata.elapsed_time_last_updated is None, use time.time()."""
        player = MockPlayer(provider, "player_1", "Test Player")
        player._attr_playback_state = PlaybackState.PLAYING
        player._attr_elapsed_time = 10.0
        old_timestamp = time.time() - 100
        player._attr_elapsed_time_last_updated = old_timestamp
        player._attr_active_source = "player_1"

        controller._players = {"player_1": player}

        session = _make_audio_source_session(elapsed_time=42, elapsed_time_last_updated=None)
        mock_mass.players.get_audio_source_session = MagicMock(return_value=session)

        before = time.time()
        player.update_state(signal_event=False)
        after = time.time()

        assert player.state.elapsed_time == 42
        # Should snap to time.time() — not inherit the stale player timestamp
        assert player.state.elapsed_time_last_updated is not None
        assert player.state.elapsed_time_last_updated >= before
        assert player.state.elapsed_time_last_updated <= after


# ----------------------------------------------- silence-keepalive wrapper


class TestAudioSourceSilenceKeepalive:
    """Verify the wrapper relays inner bytes and inserts silence during idle gaps."""

    @pytest.mark.asyncio
    async def test_relays_inner_bytes_unchanged(self) -> None:
        """When the inner generator yields fast enough, no silence is inserted."""
        from music_assistant.helpers.audio import (  # noqa: PLC0415
            audio_source_silence_keepalive,
        )

        async def _inner() -> AsyncGenerator[bytes]:
            yield b"chunk1"
            yield b"chunk2"

        out = [chunk async for chunk in audio_source_silence_keepalive(_inner(), _audio_format())]
        assert out == [b"chunk1", b"chunk2"]

    @pytest.mark.asyncio
    async def test_inserts_silence_during_idle_gap(self) -> None:
        """A producer that stalls longer than the threshold should see silence inserted."""
        from music_assistant.helpers.audio import (  # noqa: PLC0415
            audio_source_silence_keepalive,
        )

        async def _inner() -> AsyncGenerator[bytes]:
            yield b"hello"
            # stall longer than the idle threshold
            await asyncio.sleep(0.25)
            yield b"world"

        pcm_format = _audio_format()
        out: list[bytes] = []
        async for chunk in audio_source_silence_keepalive(
            _inner(), pcm_format, idle_threshold_s=0.05, silence_chunk_ms=10
        ):
            out.append(chunk)
        # the real bytes are passed through
        assert b"hello" in out
        assert b"world" in out
        # at least one silence chunk landed between them
        bytes_per_second = (
            pcm_format.sample_rate * pcm_format.channels * (pcm_format.bit_depth // 8)
        )
        silence_chunk = b"\x00" * (bytes_per_second * 10 // 1000)
        assert silence_chunk in out

    @pytest.mark.asyncio
    async def test_completes_when_inner_finishes(self) -> None:
        """The wrapper completes once the inner generator is exhausted."""
        from music_assistant.helpers.audio import (  # noqa: PLC0415
            audio_source_silence_keepalive,
        )

        async def _inner() -> AsyncGenerator[bytes]:
            yield b"one"

        chunks = [
            chunk
            async for chunk in audio_source_silence_keepalive(
                _inner(), _audio_format(), idle_threshold_s=1.0
            )
        ]
        assert chunks == [b"one"]

    @pytest.mark.asyncio
    async def test_propagates_inner_error(self) -> None:
        """An error from the source generator reaches the stream consumer."""
        from music_assistant.helpers.audio import (  # noqa: PLC0415
            audio_source_silence_keepalive,
        )

        async def _inner() -> AsyncGenerator[bytes]:
            yield b"one"
            raise RuntimeError("source failed")

        stream = audio_source_silence_keepalive(_inner(), _audio_format())
        assert await anext(stream) == b"one"
        with pytest.raises(RuntimeError, match="source failed"):
            await anext(stream)

    @pytest.mark.asyncio
    async def test_cancellation_with_full_queue_completes(self) -> None:
        """Cancelling the wrapper also closes a source blocked on a full queue."""
        from music_assistant.helpers.audio import (  # noqa: PLC0415
            audio_source_silence_keepalive,
        )

        queue_full = asyncio.Event()
        source_closed = asyncio.Event()

        async def _inner() -> AsyncGenerator[bytes]:
            try:
                for index in range(10):
                    if index == 9:
                        queue_full.set()
                    yield b"audio"
            finally:
                source_closed.set()

        stream = audio_source_silence_keepalive(_inner(), _audio_format(), idle_threshold_s=1)
        assert await anext(stream) == b"audio"
        await asyncio.wait_for(queue_full.wait(), timeout=1)

        await asyncio.wait_for(stream.aclose(), timeout=1)

        assert source_closed.is_set()

    @pytest.mark.asyncio
    async def test_cancellation_with_full_queue_propagates_cleanup_error(self) -> None:
        """Source cleanup errors propagate without blocking wrapper cancellation."""
        from music_assistant.helpers.audio import (  # noqa: PLC0415
            audio_source_silence_keepalive,
        )

        queue_full = asyncio.Event()

        async def _inner() -> AsyncGenerator[bytes]:
            try:
                for index in range(10):
                    if index == 9:
                        queue_full.set()
                    yield b"audio"
            finally:
                raise RuntimeError("cleanup failed")

        stream = audio_source_silence_keepalive(_inner(), _audio_format(), idle_threshold_s=1)
        assert await anext(stream) == b"audio"
        await asyncio.wait_for(queue_full.wait(), timeout=1)

        with pytest.raises(RuntimeError, match="cleanup failed"):
            await asyncio.wait_for(stream.aclose(), timeout=1)

    @pytest.mark.asyncio
    async def test_propagates_source_cancelled_error(self) -> None:
        """A source-raised cancellation reaches the stream consumer."""
        from music_assistant.helpers.audio import (  # noqa: PLC0415
            audio_source_silence_keepalive,
        )

        async def _inner() -> AsyncGenerator[bytes]:
            yield b"one"
            raise asyncio.CancelledError

        stream = audio_source_silence_keepalive(_inner(), _audio_format())
        assert await anext(stream) == b"one"
        with pytest.raises(asyncio.CancelledError):
            await anext(stream)

    @pytest.mark.asyncio
    async def test_custom_audio_source_path_applies_wrapper(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CUSTOM AudioSources are wrapped using the format their bytes arrive in."""
        decoded_format = _audio_format()
        wrapped_formats: list[AudioFormat] = []

        async def _inner() -> AsyncGenerator[bytes]:
            yield b"audio"

        async def _keepalive(
            inner: AsyncGenerator[bytes], pcm_format: AudioFormat
        ) -> AsyncGenerator[bytes]:
            wrapped_formats.append(pcm_format)
            async for chunk in inner:
                yield chunk

        monkeypatch.setattr(
            "music_assistant.controllers.streams.audio.audio_source_silence_keepalive",
            _keepalive,
        )
        provider = MagicMock()
        provider.available = True
        provider.get_audio_stream.return_value = _inner()
        mass = MagicMock()
        mass.get_provider.return_value = provider
        controller = StreamsAudio(mass)
        streamdetails = StreamDetails(
            provider="fake_plugin",
            item_id="main",
            audio_format=AudioFormat(content_type=ContentType.FLAC),
            decoded_audio_format=decoded_format,
            media_type=MediaType.AUDIO_SOURCE,
            stream_type=StreamType.CUSTOM,
        )

        source, seek_position, extra_input_args = await controller._resolve_media_stream_source(
            streamdetails, seek_position=0, extra_input_args=[]
        )

        assert not isinstance(source, str)
        assert [chunk async for chunk in source] == [b"audio"]
        assert wrapped_formats == [decoded_format]
        assert seek_position == 0
        assert extra_input_args == []


class TestAudioSourceLibraryRejection:
    """AudioSources are dynamic plugin surfaces — favorites/library must reject them."""

    @pytest.mark.asyncio
    async def test_add_to_favorites_rejects_audio_source(self) -> None:
        """add_item_to_favorites raises UnsupportedFeaturedException for AUDIO_SOURCE."""
        from music_assistant.controllers.music import MusicController  # noqa: PLC0415

        controller = MusicController.__new__(MusicController)
        controller.mass = MagicMock()
        source = _audio_source()
        with pytest.raises(UnsupportedFeaturedException, match="can not be favorites"):
            await controller.add_item_to_favorites(source)

    @pytest.mark.asyncio
    async def test_add_to_library_rejects_audio_source(self) -> None:
        """add_item_to_library raises UnsupportedFeaturedException for AUDIO_SOURCE."""
        from music_assistant.controllers.music import MusicController  # noqa: PLC0415

        controller = MusicController.__new__(MusicController)
        controller.mass = MagicMock()
        source = _audio_source()
        controller.get_item = AsyncMock(return_value=source)  # type: ignore[method-assign]
        with pytest.raises(UnsupportedFeaturedException, match="can not be library items"):
            await controller.add_item_to_library(source)

    @pytest.mark.asyncio
    async def test_add_to_favorites_rejects_stale_audio_source_uri(self) -> None:
        """A stale audio-source URI (plugin unloaded) raises the honest error."""
        # Without the parse_uri-first guard, get_item_by_uri would bubble
        # MediaNotFoundError because the owning plugin is gone, masking the
        # real reason: AudioSources can't be favorited regardless.
        from music_assistant.controllers.music import MusicController  # noqa: PLC0415

        controller = MusicController.__new__(MusicController)
        controller.mass = MagicMock()
        # The URI shape parse_uri returns AUDIO_SOURCE for.
        with pytest.raises(UnsupportedFeaturedException, match="can not be favorites"):
            await controller.add_item_to_favorites("stale_plugin://audio_source/main")

    @pytest.mark.asyncio
    async def test_add_to_library_rejects_stale_audio_source_uri(self) -> None:
        """A stale audio-source URI (plugin unloaded) raises the honest error."""
        from music_assistant.controllers.music import MusicController  # noqa: PLC0415

        controller = MusicController.__new__(MusicController)
        controller.mass = MagicMock()
        with pytest.raises(UnsupportedFeaturedException, match="can not be library items"):
            await controller.add_item_to_library("stale_plugin://audio_source/main")


# ------------------------------------------------ streamdetails dispatch for plugin-owned items


def _register_stub_plugin(
    mass: MusicAssistant, provider_cls: type[PluginProvider], *, instance_id: str
) -> PluginProvider:
    """Instantiate a real PluginProvider subclass with a stub manifest/config and register it."""
    manifest = MagicMock()
    manifest.domain = instance_id
    config = MagicMock()
    config.instance_id = instance_id
    config.get_value = MagicMock(return_value="GLOBAL")
    provider = provider_cls(mass, manifest, config)
    # get_provider() only returns providers marked available; the real load pipeline sets
    # this after handle_async_init, which we skip here.
    provider.available = True
    mass._providers[instance_id] = provider
    return provider


def _ready_streams_audio(mass: MusicAssistant) -> StreamsAudio:
    """
    Attach a minimal streams/player_queues stand-in to `mass` and return its StreamsAudio.

    mass_minimal only wires config/cache/discovery; get_stream_details also reads
    mass.streams (volume-normalization config) and mass.player_queues (preferred-provider
    lookup), so dispatch tests stub those in directly rather than booting the full server.
    """
    mass.player_queues = MagicMock()
    mass.player_queues.queue_data_or_none = MagicMock(return_value=None)
    mass.streams = MagicMock()
    mass.streams.get_config_value = MagicMock(
        side_effect=lambda key, *_a, **_k: (
            -14
            if key == CONF_VOLUME_NORMALIZATION_TARGET
            else VolumeNormalizationMode.DISABLED.value
        )
    )
    mass.streams.audio = StreamsAudio(mass)
    return mass.streams.audio  # type: ignore[no-any-return]


def _queue_item_for_sound_effect(
    *, queue_id: str, provider_instance: str, item_id: str
) -> QueueItem:
    """Build a QueueItem wrapping a SoundEffect owned by `provider_instance`."""
    media_item = SoundEffect(
        item_id=item_id,
        provider=provider_instance,
        name="Clip",
        provider_mappings={
            ProviderMapping(
                item_id=item_id,
                provider_domain=provider_instance,
                provider_instance=provider_instance,
            )
        },
    )
    return QueueItem(
        queue_id=queue_id, queue_item_id=item_id, name="Clip", duration=None, media_item=media_item
    )


async def test_plugin_owned_sound_effect_uses_plugin_stream_details(
    mass_minimal: MusicAssistant,
) -> None:
    """A SOUND_EFFECT owned by a plugin provider is served by the plugin hook."""
    received: list[tuple[str, MediaType]] = []

    class ClipPlugin(PluginProvider):
        async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
            received.append((item_id, media_type))
            return StreamDetails(
                provider=self.instance_id,
                item_id=item_id,
                audio_format=AudioFormat(content_type=ContentType.MP3),
                media_type=media_type,
                stream_type=StreamType.HTTP,
                path="http://example.invalid/clip.mp3",
                duration=12,
            )

    audio = _ready_streams_audio(mass_minimal)
    plugin = _register_stub_plugin(mass_minimal, ClipPlugin, instance_id="clipper")
    queue_item = _queue_item_for_sound_effect(
        queue_id="player_a", provider_instance=plugin.instance_id, item_id="clip_007"
    )

    streamdetails = await audio.get_stream_details(queue_item)

    assert received == [("clip_007", MediaType.SOUND_EFFECT)]
    assert streamdetails.media_type == MediaType.SOUND_EFFECT
