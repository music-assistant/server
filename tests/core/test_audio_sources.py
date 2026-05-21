"""Tests for the AudioSource refactor — the new PluginProvider contract."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import (
    ContentType,
    MediaType,
    PlayerFeature,
    SourceControl,
    StreamType,
)
from music_assistant_models.errors import (
    MediaNotFoundError,
    ResourceBusyError,
    UnsupportedFeaturedException,
)
from music_assistant_models.media_items import AudioFormat, AudioSource, ProviderMapping
from music_assistant_models.streamdetails import StreamDetails, StreamMetadata

from music_assistant.controllers.players import PlayerController
from music_assistant.helpers.throttle_retry import Throttler
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
    mass.config.get_raw_player_config_value = MagicMock(return_value="auto")
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
    controller._player_throttlers = {"player_1": Throttler(1, 0.05)}
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
        self._in_use_by_queue: str | None = None
        self.control_calls: list[tuple[SourceControl, int | None]] = []
        self.selected_calls: list[tuple[str, str]] = []

    async def get_audio_sources(self) -> list[AudioSource]:
        return [self._audio_source]

    async def get_stream_details(self, source_id: str, queue_id: str) -> StreamDetails:
        if source_id != self._audio_source.item_id:
            raise MediaNotFoundError(f"Unknown AudioSource: {source_id}")
        if self._in_use_by_queue and self._in_use_by_queue != queue_id:
            raise ResourceBusyError(f"Source busy on queue {self._in_use_by_queue}")
        self._in_use_by_queue = queue_id
        return StreamDetails(
            provider=self.instance_id,
            item_id=source_id,
            audio_format=_audio_format(),
            media_type=MediaType.AUDIO_SOURCE,
            stream_type=StreamType.CUSTOM,
            stream_metadata=StreamMetadata(title="Fake"),
        )

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes, None]:
        del streamdetails, seek_position
        try:
            yield b"\x00" * 4096
        finally:
            self._in_use_by_queue = None

    async def on_source_control(
        self,
        source_id: str,
        action: SourceControl,
        value: int | None = None,
    ) -> None:
        assert source_id == self._audio_source.item_id
        self.control_calls.append((action, value))

    async def on_source_selected(self, source_id: str, player_id: str, queue_id: str) -> None:
        assert source_id == self._audio_source.item_id
        self.selected_calls.append((source_id, player_id))
        # Mirror the handoff pattern shared by every real plugin: release the
        # prior queue's exclusive claim so the upcoming get_stream_details for
        # the new queue is not rejected by the busy check.
        if self._in_use_by_queue and self._in_use_by_queue != queue_id:
            self._in_use_by_queue = None

    async def on_source_unselected(self, source_id: str, queue_id: str) -> None:
        assert source_id == self._audio_source.item_id
        # Idempotent / stale-callback-safe clear: only release if this queue
        # still owns the source.
        if self._in_use_by_queue == queue_id:
            self._in_use_by_queue = None


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
    async def test_get_stream_details_first_call_claims_lock(self) -> None:
        """First get_stream_details for a queue should claim the exclusive lock."""
        prov = _FakePluginProvider(_audio_source())
        sd = await prov.get_stream_details("main", "queue_a")
        assert sd.media_type == MediaType.AUDIO_SOURCE
        assert sd.stream_type == StreamType.CUSTOM
        assert sd.stream_metadata is not None
        assert prov._in_use_by_queue == "queue_a"

    @pytest.mark.asyncio
    async def test_get_stream_details_same_queue_does_not_raise(self) -> None:
        """A re-request from the same queue (e.g. reconnect) must not be rejected."""
        prov = _FakePluginProvider(_audio_source())
        await prov.get_stream_details("main", "queue_a")
        sd = await prov.get_stream_details("main", "queue_a")
        assert sd.media_type == MediaType.AUDIO_SOURCE

    @pytest.mark.asyncio
    async def test_get_stream_details_other_queue_raises_busy(self) -> None:
        """A second queue grabbing an exclusive source must hit ResourceBusyError."""
        prov = _FakePluginProvider(_audio_source())
        await prov.get_stream_details("main", "queue_a")
        with pytest.raises(ResourceBusyError):
            await prov.get_stream_details("main", "queue_b")

    @pytest.mark.asyncio
    async def test_get_stream_details_unknown_source_raises_not_found(self) -> None:
        """An unknown source_id should surface as MediaNotFoundError."""
        prov = _FakePluginProvider(_audio_source())
        with pytest.raises(MediaNotFoundError):
            await prov.get_stream_details("bogus", "queue_a")

    @pytest.mark.asyncio
    async def test_get_audio_stream_releases_lock_in_finally(self) -> None:
        """When the audio generator exits, the queue lock should be released."""
        prov = _FakePluginProvider(_audio_source())
        sd = await prov.get_stream_details("main", "queue_a")
        gen = prov.get_audio_stream(sd)
        await gen.__anext__()
        await gen.aclose()
        assert prov._in_use_by_queue is None

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
    async def test_handoff_releases_prior_queue_in_on_source_selected(self) -> None:
        """on_source_selected must clear the prior queue's claim so handoff works."""
        # The streams controller fires on_source_selected BEFORE get_stream_details.
        # Without releasing the prior claim, the second get_stream_details would
        # raise ResourceBusyError and the takeover path would be unreachable.
        prov = _FakePluginProvider(_audio_source())
        await prov.get_stream_details("main", "queue_a")
        assert prov._in_use_by_queue == "queue_a"
        # Different queue selects the source (cross-queue handoff).
        # The streams controller pairs this with get_stream_details for the
        # new queue, which must succeed without ResourceBusyError.
        await prov.on_source_selected("main", "player_b", "queue_b")
        sd = await prov.get_stream_details("main", "queue_b")
        assert sd.media_type == MediaType.AUDIO_SOURCE
        assert prov._in_use_by_queue == "queue_b"

    @pytest.mark.asyncio
    async def test_on_source_unselected_releases_own_queue(self) -> None:
        """on_source_unselected releases the claim if this queue still owns it."""
        prov = _FakePluginProvider(_audio_source())
        await prov.get_stream_details("main", "queue_a")
        await prov.on_source_unselected("main", "queue_a")
        assert prov._in_use_by_queue is None

    @pytest.mark.asyncio
    async def test_on_source_unselected_ignores_stale_queue(self) -> None:
        """A late on_source_unselected after a handoff must not clobber the new claim."""
        # Stale callback scenario: queue A's stream finally fires AFTER queue B
        # has already taken over. The guard inside on_source_unselected must
        # prevent it from clearing queue B's claim.
        prov = _FakePluginProvider(_audio_source())
        await prov.get_stream_details("main", "queue_a")
        # Simulate handoff: B selects the source, releasing A's claim
        await prov.on_source_selected("main", "player_b", "queue_b")
        await prov.get_stream_details("main", "queue_b")
        assert prov._in_use_by_queue == "queue_b"
        # Now queue A's late unselect fires — it must be a no-op
        await prov.on_source_unselected("main", "queue_a")
        assert prov._in_use_by_queue == "queue_b"


# ---------------------------------------------------------- active source helper


class TestGetActiveAudioSource:
    """Verify _get_active_audio_source resolves the active queue item."""

    def test_returns_none_when_no_active_queue(
        self,
        controller: PlayerController,
        player: MockPlayer,
        mock_mass: MagicMock,
    ) -> None:
        """No active queue → no active AudioSource."""
        controller.get_active_queue = MagicMock(return_value=None)  # type: ignore[method-assign]
        assert controller._get_active_audio_source(player) is None

    def test_returns_none_when_queue_item_is_not_audiosource(
        self,
        controller: PlayerController,
        player: MockPlayer,
        mock_mass: MagicMock,
    ) -> None:
        """Active queue with a non-AudioSource item → returns None."""
        active_queue = MagicMock()
        current_item = MagicMock()
        media_item = MagicMock()
        media_item.media_type = MediaType.TRACK
        current_item.media_item = media_item
        active_queue.current_item = current_item
        controller.get_active_queue = MagicMock(return_value=active_queue)  # type: ignore[method-assign]

        assert controller._get_active_audio_source(player) is None

    def test_returns_tuple_when_audiosource_active(
        self,
        controller: PlayerController,
        player: MockPlayer,
        mock_mass: MagicMock,
    ) -> None:
        """Active AudioSource queue item → returns (AudioSource, PluginProvider)."""
        source = _audio_source()
        # Use a MagicMock with spec=PluginProvider so the helper's isinstance
        # check passes; _FakePluginProvider is duck-typed and would fail here.
        plugin_prov = MagicMock(spec=PluginProvider)

        active_queue = MagicMock()
        current_item = MagicMock()
        current_item.media_item = source
        active_queue.current_item = current_item
        controller.get_active_queue = MagicMock(return_value=active_queue)  # type: ignore[method-assign]
        mock_mass.get_provider = MagicMock(return_value=plugin_prov)

        result = controller._get_active_audio_source(player)
        assert result is not None
        returned_source, returned_prov = result
        assert returned_source is source
        assert returned_prov is plugin_prov


# ------------------------------------------------------------- update_stream_metadata


class TestUpdateStreamMetadata:
    """Verify the streams controller helper mutates streamdetails and signals."""

    def test_no_op_when_queue_missing(self) -> None:
        """If queue doesn't exist, the helper is a silent no-op."""
        from music_assistant.controllers.streams import StreamsController  # noqa: PLC0415

        mass = MagicMock()
        mass.player_queues.get = MagicMock(return_value=None)
        mass.player_queues.signal_update = MagicMock()

        # bypass __init__ to avoid wiring the full controller
        controller = StreamsController.__new__(StreamsController)
        controller.mass = mass

        controller.update_stream_metadata("missing", "main", "x", StreamMetadata(title="t"))
        mass.player_queues.signal_update.assert_not_called()

    def test_no_op_when_no_streamdetails(self) -> None:
        """If the current queue item lacks streamdetails, the helper is a no-op."""
        from music_assistant.controllers.streams import StreamsController  # noqa: PLC0415

        mass = MagicMock()
        queue = MagicMock()
        queue.current_item = MagicMock()
        queue.current_item.streamdetails = None
        mass.player_queues.get = MagicMock(return_value=queue)
        mass.player_queues.signal_update = MagicMock()

        controller = StreamsController.__new__(StreamsController)
        controller.mass = mass

        controller.update_stream_metadata("q1", "main", "x", StreamMetadata(title="t"))
        mass.player_queues.signal_update.assert_not_called()

    def test_writes_metadata_and_signals_queue(self) -> None:
        """When streamdetails matches, the helper mutates and signals the queue update."""
        from music_assistant.controllers.streams import StreamsController  # noqa: PLC0415

        mass = MagicMock()
        sd = StreamDetails(
            provider="x",
            item_id="y",
            audio_format=_audio_format(),
            media_type=MediaType.AUDIO_SOURCE,
            stream_type=StreamType.CUSTOM,
        )
        queue = MagicMock()
        queue.current_item = MagicMock()
        queue.current_item.streamdetails = sd
        mass.player_queues.get = MagicMock(return_value=queue)
        mass.player_queues.signal_update = MagicMock()

        controller = StreamsController.__new__(StreamsController)
        controller.mass = mass

        new_meta = StreamMetadata(title="Now Playing")
        controller.update_stream_metadata("q1", "y", "x", new_meta)

        assert sd.stream_metadata is new_meta
        assert sd.stream_metadata_last_updated is not None
        mass.player_queues.signal_update.assert_called_once_with("q1")

    def test_rejects_when_current_item_is_not_audio_source(self) -> None:
        """A late metadata callback must not stamp over a track/radio item."""
        from music_assistant.controllers.streams import StreamsController  # noqa: PLC0415

        mass = MagicMock()
        sd = StreamDetails(
            provider="x",
            item_id="y",
            audio_format=_audio_format(),
            media_type=MediaType.TRACK,
            stream_type=StreamType.HTTP,
        )
        queue = MagicMock()
        queue.current_item = MagicMock()
        queue.current_item.streamdetails = sd
        mass.player_queues.get = MagicMock(return_value=queue)
        mass.player_queues.signal_update = MagicMock()

        controller = StreamsController.__new__(StreamsController)
        controller.mass = mass

        controller.update_stream_metadata("q1", "y", "x", StreamMetadata(title="stale"))
        assert sd.stream_metadata is None
        mass.player_queues.signal_update.assert_not_called()

    def test_rejects_when_source_id_or_provider_mismatches(self) -> None:
        """A stale callback from a different source/provider must not overwrite."""
        from music_assistant.controllers.streams import StreamsController  # noqa: PLC0415

        mass = MagicMock()
        sd = StreamDetails(
            provider="provider_a",
            item_id="source_a",
            audio_format=_audio_format(),
            media_type=MediaType.AUDIO_SOURCE,
            stream_type=StreamType.CUSTOM,
        )
        queue = MagicMock()
        queue.current_item = MagicMock()
        queue.current_item.streamdetails = sd
        mass.player_queues.get = MagicMock(return_value=queue)
        mass.player_queues.signal_update = MagicMock()

        controller = StreamsController.__new__(StreamsController)
        controller.mass = mass

        # Wrong provider
        controller.update_stream_metadata(
            "q1", "source_a", "provider_b", StreamMetadata(title="stale")
        )
        assert sd.stream_metadata is None

        # Wrong source_id
        controller.update_stream_metadata(
            "q1", "source_b", "provider_a", StreamMetadata(title="stale")
        )
        assert sd.stream_metadata is None

        mass.player_queues.signal_update.assert_not_called()


# ----------------------------------------------- silence-keepalive wrapper


class TestAudioSourceSilenceKeepalive:
    """Verify the wrapper relays inner bytes and inserts silence during idle gaps."""

    @pytest.mark.asyncio
    async def test_relays_inner_bytes_unchanged(self) -> None:
        """When the inner generator yields fast enough, no silence is inserted."""
        from music_assistant.helpers.audio import (  # noqa: PLC0415
            audio_source_silence_keepalive,
        )

        async def _inner() -> AsyncGenerator[bytes, None]:
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

        async def _inner() -> AsyncGenerator[bytes, None]:
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

        async def _inner() -> AsyncGenerator[bytes, None]:
            yield b"one"

        chunks = [
            chunk
            async for chunk in audio_source_silence_keepalive(
                _inner(), _audio_format(), idle_threshold_s=1.0
            )
        ]
        assert chunks == [b"one"]


class TestAudioSourceLibraryRejection:
    """AudioSources are dynamic plugin surfaces — favorites/library must reject them."""

    @pytest.mark.asyncio
    async def test_add_to_favorites_rejects_audio_source(self) -> None:
        """add_item_to_favorites raises UnsupportedFeaturedException for AUDIO_SOURCE."""
        from music_assistant.controllers.music import MusicController  # noqa: PLC0415

        controller = MusicController.__new__(MusicController)
        controller.mass = MagicMock()
        source = _audio_source()
        with pytest.raises(UnsupportedFeaturedException, match="Live Inputs"):
            await controller.add_item_to_favorites(source)

    @pytest.mark.asyncio
    async def test_add_to_library_rejects_audio_source(self) -> None:
        """add_item_to_library raises UnsupportedFeaturedException for AUDIO_SOURCE."""
        from music_assistant.controllers.music import MusicController  # noqa: PLC0415

        controller = MusicController.__new__(MusicController)
        controller.mass = MagicMock()
        source = _audio_source()
        controller.get_item = AsyncMock(return_value=source)  # type: ignore[method-assign]
        with pytest.raises(UnsupportedFeaturedException, match="Live Inputs"):
            await controller.add_item_to_library(source)
