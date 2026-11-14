"""Tests for the Player.update_state change-detection path."""

from __future__ import annotations

import copy
import time
from statistics import median
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from music_assistant_models.audio_processing import (
    ActiveSourceAudioDetails,
    AudioFidelity,
    AudioQuality,
)
from music_assistant_models.enums import (
    ContentType,
    CrossfadeMode,
    ImageType,
    MediaType,
    PlaybackState,
    PlayerType,
    ProtocolRole,
    VolumeNormalizationMode,
)
from music_assistant_models.media_items import AudioFormat, AudioSource, MediaItemImage, Track
from music_assistant_models.media_items.provider_mapping import ProviderMapping
from music_assistant_models.player import Player as PlayerState
from music_assistant_models.player import PlayerOption, PlayerOptionType, PlayerSoundMode
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.streamdetails import StreamDetails, StreamMetadata
from music_assistant_models.unique_list import UniqueList

import music_assistant.models.player as player_module
from music_assistant.constants import CONF_LEGACY_IDS
from music_assistant.controllers.players.audio_sources import AudioSourceSession
from music_assistant.models.player import Player
from music_assistant.models.player_state import derive_player_state
from tests.common import MockProtocol, MockProvider, attach_mock, mock_player_mass


@pytest.fixture
def mock_mass() -> MagicMock:
    """Create a mock MusicAssistant instance."""
    # no queue registered: current_media resolves from the player's native media
    return mock_player_mass()


@pytest.fixture
def protocol(mock_mass: MagicMock) -> MockProtocol:
    """Create the protocol of a playing player whose state is calculated once."""
    provider = MockProvider("test_provider", mass=mock_mass)
    player, protocol = attach_mock(provider, "player_1", "Player 1")
    now = time.time()
    protocol._attr_playback_state = PlaybackState.PLAYING
    protocol._attr_volume_level = 20
    protocol._attr_elapsed_time = 10.0
    protocol._attr_elapsed_time_last_updated = now
    protocol.set_current_media(uri="http://test/stream", title="Test")
    player.update_state(signal_event=False)
    return protocol


@pytest.fixture
def player(protocol: MockProtocol) -> Player:
    """Return the playing player of the protocol fixture."""
    assert protocol.player is not None
    return protocol.player


class _SyncedProtocol(MockProtocol):
    """A protocol whose provider names its sync leader itself."""

    leader_id: str | None = None

    @property
    def synced_to(self) -> str | None:
        """Return the id of the sync leader as the provider names it."""
        return self.leader_id


def _image(url: str = "http://img/cover.jpg") -> MediaItemImage:
    """
    Return the image of a media item.

    :param url: Where the image is found.
    """
    return MediaItemImage(
        type=ImageType.THUMB,
        path=url,
        provider="url",
        remotely_accessible=True,
    )


class TestUpdateStateChangeDetection:
    """The no-change update_state path does no work; changed inputs recalculate."""

    def test_no_change_update_does_no_work(self, player: Player, protocol: MockProtocol) -> None:
        """An update_state call without any changed input builds nothing."""
        with (
            patch.object(player_module, "derive_player_state", wraps=derive_player_state) as derive,
            patch.object(copy, "deepcopy", wraps=copy.deepcopy) as deepcopy_mock,
        ):
            player.update_state()

        derive.assert_not_called()
        deepcopy_mock.assert_not_called()
        # nor the lists the properties of the protocol build on every read
        assert "sound_mode_list" not in protocol._cache
        assert "options" not in protocol._cache
        # nor the config the state takes its legacy ids from
        assert "_legacy_ids" not in player._cache

    def test_position_tick_does_not_rebuild(self, player: Player, protocol: MockProtocol) -> None:
        """A regular playback tick keeps the anchor and builds nothing."""
        assert protocol._attr_elapsed_time is not None
        assert protocol._attr_elapsed_time_last_updated is not None
        protocol._attr_elapsed_time += 1
        protocol._attr_elapsed_time_last_updated += 1

        with patch.object(
            player_module, "derive_player_state", wraps=derive_player_state
        ) as derive:
            player.update_state()

        derive.assert_not_called()
        assert player.state.elapsed_time == 10.0

    def test_position_jump_rebuilds_state(self, player: Player, protocol: MockProtocol) -> None:
        """A corrected-position jump (seek) adopts the new anchor."""
        protocol._attr_elapsed_time = 61.0
        protocol._attr_elapsed_time_last_updated = time.time()

        player.update_state(signal_event=False)

        assert player.state.elapsed_time == 61.0

    def test_changed_input_rebuilds_state(self, player: Player, protocol: MockProtocol) -> None:
        """A changed player attribute is picked up by the next update_state call."""
        protocol._attr_volume_level = 55

        with patch.object(
            player_module, "derive_player_state", wraps=derive_player_state
        ) as derive:
            player.update_state(signal_event=False)

        derive.assert_called_once()
        assert player.state.volume_level == 55

    def test_changed_sound_modes_and_options_rebuild_state(
        self, player: Player, protocol: MockProtocol
    ) -> None:
        """Sound modes and options the device reports anew reach the state."""
        protocol._attr_sound_mode_list = [PlayerSoundMode(id="movie", name="Movie")]
        player.update_state(signal_event=False)
        assert [x.id for x in player.state.sound_mode_list] == ["movie"]

        protocol._attr_options = [
            PlayerOption(key="bass", name="Bass", type=PlayerOptionType.INTEGER, value=3)
        ]
        player.update_state(signal_event=False)
        assert [(x.key, x.value) for x in player.state.options] == [("bass", 3)]

    def test_changed_privacy_rebuilds_state(self, player: Player, protocol: MockProtocol) -> None:
        """A player turning private must reach clients, which decide where to show it."""
        protocol._attr_private = True

        with patch.object(
            player_module, "derive_player_state", wraps=derive_player_state
        ) as derive:
            player.update_state(signal_event=False)

        derive.assert_called_once()
        assert player.state.private is True

    def test_mark_state_dirty_forces_recalculation(self, player: Player) -> None:
        """mark_state_dirty recalculates even when no own input changed."""
        with patch.object(
            player_module, "derive_player_state", wraps=derive_player_state
        ) as derive:
            player.mark_state_dirty()
            player.update_state(signal_event=False)

        derive.assert_called_once()

    def test_position_appearing_signals_a_jump(
        self, player: Player, protocol: MockProtocol, mock_mass: MagicMock
    ) -> None:
        """
        The first elapsed write after an anchor reset is signalled as a position jump.

        A playback session restart (seek) resets the anchor to None; the session's
        first elapsed write is its only propagation opportunity, so swallowing it
        strands wrapper players on the stale None anchor (a frozen queue position).
        """
        protocol._attr_elapsed_time = None
        protocol._attr_elapsed_time_last_updated = None
        player.update_state(signal_event=False)
        assert player.state.elapsed_time is None

        protocol._attr_elapsed_time = 0.5
        protocol._attr_elapsed_time_last_updated = time.time()
        player.update_state()

        mock_mass.players.on_player_position_jumped.assert_called_once_with(player)
        assert player.state.elapsed_time == 0.5


class TestNativeCurrentMediaPosition:
    """The published position of native current_media and the timestamp it is paired with."""

    def _playing_player(
        self,
        mock_mass: MagicMock,
        *,
        media_elapsed_time: int | None,
        media_last_updated: float | None,
        player_elapsed_time: float | None,
        player_last_updated: float | None,
    ) -> Player:
        """Build a playing player with the given media and player level position anchors."""
        provider = MockProvider("test_provider", mass=mock_mass)
        player, protocol = attach_mock(provider, "player_1", "Player 1")
        protocol._attr_playback_state = PlaybackState.PLAYING
        protocol._attr_elapsed_time = player_elapsed_time
        protocol._attr_elapsed_time_last_updated = player_last_updated
        protocol.set_current_media(uri="http://test/stream", title="Test")
        assert protocol._attr_current_media is not None
        protocol._attr_current_media.elapsed_time = media_elapsed_time
        protocol._attr_current_media.elapsed_time_last_updated = media_last_updated
        player.update_state(signal_event=False)
        return player

    def test_zero_position_is_reported(self, mock_mass: MagicMock) -> None:
        """A position of zero (start of a track) is reported instead of being dropped."""
        anchor = time.time()
        player = self._playing_player(
            mock_mass,
            media_elapsed_time=0,
            media_last_updated=anchor,
            player_elapsed_time=0,
            player_last_updated=anchor,
        )

        assert player.state.current_media is not None
        assert player.state.current_media.elapsed_time == 0
        assert player.state.current_media.elapsed_time_last_updated == anchor

    def test_media_position_wins_without_player_position(self, mock_mass: MagicMock) -> None:
        """A media position is reported even when the player reports none of its own."""
        anchor = time.time()
        player = self._playing_player(
            mock_mass,
            media_elapsed_time=42,
            media_last_updated=anchor,
            player_elapsed_time=None,
            player_last_updated=None,
        )

        assert player.state.current_media is not None
        assert player.state.current_media.elapsed_time == 42
        assert player.state.current_media.elapsed_time_last_updated == anchor

    def test_media_position_is_never_paired_with_the_player_timestamp(
        self, mock_mass: MagicMock
    ) -> None:
        """A media position without its own timestamp does not borrow the player's."""
        player = self._playing_player(
            mock_mass,
            media_elapsed_time=42,
            media_last_updated=None,
            player_elapsed_time=10.0,
            player_last_updated=time.time(),
        )

        assert player.state.current_media is not None
        assert player.state.current_media.elapsed_time == 42
        assert player.state.current_media.elapsed_time_last_updated is None

    def test_late_arriving_position_is_adopted(self, mock_mass: MagicMock) -> None:
        """A position reported after the media was published still reaches the state."""
        player = self._playing_player(
            mock_mass,
            media_elapsed_time=None,
            media_last_updated=None,
            player_elapsed_time=None,
            player_last_updated=None,
        )
        published = player.state.current_media
        assert published is not None
        assert published.elapsed_time is None

        anchor = time.time()
        protocol = player.own_protocol
        assert protocol is not None
        protocol._attr_elapsed_time = 5.0
        protocol._attr_elapsed_time_last_updated = anchor
        player.update_state(signal_event=False)

        published = player.state.current_media
        assert published is not None
        assert published.elapsed_time == 5
        assert published.elapsed_time_last_updated == anchor

    def test_steady_playback_keeps_the_previous_anchor(self, mock_mass: MagicMock) -> None:
        """A regular playback tick keeps the anchor it already published."""
        anchor = time.time()
        player = self._playing_player(
            mock_mass,
            media_elapsed_time=None,
            media_last_updated=None,
            player_elapsed_time=10.0,
            player_last_updated=anchor,
        )

        protocol = player.own_protocol
        assert protocol is not None
        protocol._attr_elapsed_time = 11.0
        protocol._attr_elapsed_time_last_updated = anchor + 1
        player.update_state(signal_event=False)

        assert player.state.current_media is not None
        assert player.state.current_media.elapsed_time == 10
        assert player.state.current_media.elapsed_time_last_updated == anchor

    def test_player_position_is_paired_with_its_own_timestamp(self, mock_mass: MagicMock) -> None:
        """Falling back to the player position also takes the player level timestamp."""
        player_anchor = time.time()
        player = self._playing_player(
            mock_mass,
            media_elapsed_time=None,
            media_last_updated=player_anchor - 30,
            player_elapsed_time=42.7,
            player_last_updated=player_anchor,
        )

        assert player.state.current_media is not None
        assert player.state.current_media.elapsed_time == 42
        assert player.state.current_media.elapsed_time_last_updated == player_anchor

    def test_no_position_reports_no_timestamp(self, mock_mass: MagicMock) -> None:
        """Without any position, the timestamp is dropped along with it."""
        player = self._playing_player(
            mock_mass,
            media_elapsed_time=None,
            media_last_updated=time.time(),
            player_elapsed_time=None,
            player_last_updated=time.time(),
        )

        assert player.state.current_media is not None
        assert player.state.current_media.elapsed_time is None
        assert player.state.current_media.elapsed_time_last_updated is None


class TestStreamMetadataPosition:
    """The published position of a queue item carrying live stream metadata."""

    def _playing_player(
        self,
        mock_mass: MagicMock,
        *,
        metadata_elapsed_time: int | None,
        metadata_last_updated: float | None,
        queue_elapsed_time: float,
        queue_last_updated: float,
        media_type: MediaType = MediaType.RADIO,
        metadata_duration: int | None = None,
    ) -> Player:
        """Build a playing player whose active queue item carries live stream metadata."""
        queue = PlayerQueue(
            queue_id="player_1",
            active=True,
            display_name="Player 1",
            available=True,
            items=1,
            elapsed_time=queue_elapsed_time,
            elapsed_time_last_updated=queue_last_updated,
            current_item=QueueItem(
                queue_id="player_1",
                queue_item_id="item_1",
                name="Live Radio",
                duration=None,
                streamdetails=StreamDetails(
                    provider="test_provider",
                    item_id="item_1",
                    audio_format=AudioFormat(),
                    media_type=media_type,
                    stream_metadata=StreamMetadata(
                        title="Live Track",
                        duration=metadata_duration,
                        elapsed_time=metadata_elapsed_time,
                        elapsed_time_last_updated=metadata_last_updated,
                    ),
                ),
            ),
        )
        mock_mass.player_queues.get = MagicMock(return_value=queue)
        provider = MockProvider("test_provider", mass=mock_mass)
        player, protocol = attach_mock(provider, "player_1", "Player 1")
        protocol._attr_playback_state = PlaybackState.PLAYING
        player.update_state(signal_event=False)
        return player

    def test_zero_metadata_position_is_reported(self, mock_mass: MagicMock) -> None:
        """A live track that just started reports zero, not the elapsed stream time."""
        metadata_anchor = time.time()
        player = self._playing_player(
            mock_mass,
            metadata_elapsed_time=0,
            metadata_last_updated=metadata_anchor,
            queue_elapsed_time=300.0,
            queue_last_updated=metadata_anchor - 30,
        )

        assert player.state.current_media is not None
        assert player.state.current_media.title == "Live Track"
        assert player.state.current_media.elapsed_time == 0
        assert player.state.current_media.elapsed_time_last_updated == metadata_anchor

    def test_queue_position_is_paired_with_its_own_timestamp(self, mock_mass: MagicMock) -> None:
        """Without a metadata position, both values come from the queue."""
        queue_anchor = time.time()
        player = self._playing_player(
            mock_mass,
            metadata_elapsed_time=None,
            metadata_last_updated=queue_anchor - 30,
            queue_elapsed_time=300.5,
            queue_last_updated=queue_anchor,
        )

        assert player.state.current_media is not None
        assert player.state.current_media.title == "Live Track"
        assert player.state.current_media.elapsed_time == 300
        assert player.state.current_media.elapsed_time_last_updated == queue_anchor

    def test_a_song_duration_without_its_position_reports_no_position(
        self, mock_mass: MagicMock
    ) -> None:
        """A stream's song duration is not paired with how long the stream has played."""
        queue_anchor = time.time()
        player = self._playing_player(
            mock_mass,
            metadata_elapsed_time=None,
            metadata_last_updated=None,
            queue_elapsed_time=1289.0,
            queue_last_updated=queue_anchor,
            metadata_duration=430,
        )

        assert player.state.current_media is not None
        assert player.state.current_media.duration == 430
        assert player.state.current_media.elapsed_time is None
        assert player.state.current_media.elapsed_time_last_updated is None

    def test_a_radio_station_keeps_its_own_position(self, mock_mass: MagicMock) -> None:
        """
        A radio station's position is untouched by the live-source override.

        Radio is a queue item and always was: the override this replaced was gated on
        the media type being an audio source, so radio never went through it. Pinned
        here because the override lost that gate, and a station reporting the byte
        clock instead of its stream position would be a regression with nothing to do
        with external sources.
        """
        metadata_anchor = time.time()
        player = self._playing_player(
            mock_mass,
            metadata_elapsed_time=42,
            metadata_last_updated=metadata_anchor,
            queue_elapsed_time=999.0,
            queue_last_updated=metadata_anchor - 30,
            media_type=MediaType.RADIO,
        )

        assert player.state.current_media is not None
        assert player.state.current_media.elapsed_time == 42
        assert player.state.current_media.elapsed_time_last_updated == metadata_anchor

    def test_a_live_source_elsewhere_does_not_move_this_players_clock(
        self, mock_mass: MagicMock
    ) -> None:
        """
        A source playing on another player has no say in this player's position.

        Asserts the player-level clock, which is what the override writes - the
        media-level position for a radio item comes from its stream metadata by a
        different route, so it would not notice a leak.
        """
        anchor = time.time()
        player = self._playing_player(
            mock_mass,
            metadata_elapsed_time=None,
            metadata_last_updated=None,
            queue_elapsed_time=0.0,
            queue_last_updated=anchor,
            media_type=MediaType.RADIO,
        )
        protocol = player.own_protocol
        assert protocol is not None
        protocol._attr_elapsed_time = 123.0
        protocol._attr_elapsed_time_last_updated = anchor
        other = MagicMock()
        other.stream_metadata.elapsed_time = 7
        other.stream_metadata.elapsed_time_last_updated = anchor
        mock_mass.players.get_audio_source_session = MagicMock(
            side_effect=lambda pid: other if pid == "other_player" else None
        )
        player.update_state(signal_event=False)

        assert player.state.elapsed_time == 123.0


class TestLiveSourcePosition:
    """The published position of a live external source playing on a player."""

    def _player_with_source(
        self,
        mock_mass: MagicMock,
        *,
        metadata_elapsed_time: int | None,
        metadata_last_updated: float | None,
    ) -> Player:
        """Build a playing player with a live source reporting its own position."""
        source = AudioSource(
            item_id="main",
            provider="test_instance",
            name="Spotify Connect",
            provider_mappings={
                ProviderMapping(
                    item_id="main",
                    provider_domain="spotify_connect",
                    provider_instance="test_instance",
                )
            },
        )
        session = AudioSourceSession(
            player_id="player_1",
            source=source,
            provider_instance_id="test_instance",
            stream_metadata=StreamMetadata(
                title="Live Track",
                elapsed_time=metadata_elapsed_time,
                elapsed_time_last_updated=metadata_last_updated,
            ),
        )
        mock_mass.players.get_audio_source_session = MagicMock(return_value=session)
        provider = MockProvider("test_provider", mass=mock_mass)
        player, protocol = attach_mock(provider, "player_1", "Player 1")
        protocol._attr_playback_state = PlaybackState.PLAYING
        protocol._attr_elapsed_time = 300.0
        protocol._attr_elapsed_time_last_updated = (metadata_last_updated or time.time()) - 30
        player.update_state(signal_event=False)
        return player

    def test_source_position_matches_player_position(self, mock_mass: MagicMock) -> None:
        """An upstream source position is reported identically on player and media level."""
        metadata_anchor = time.time()
        player = self._player_with_source(
            mock_mass,
            metadata_elapsed_time=0,
            metadata_last_updated=metadata_anchor,
        )

        assert player.state.current_media is not None
        assert player.state.elapsed_time == 0
        assert player.state.current_media.elapsed_time == 0
        assert player.state.elapsed_time_last_updated == metadata_anchor
        assert player.state.current_media.elapsed_time_last_updated == metadata_anchor

    def test_source_without_a_position_falls_back_to_the_player(self, mock_mass: MagicMock) -> None:
        """A source that reports no position leaves the player's own clock in charge."""
        anchor = time.time()
        player = self._player_with_source(
            mock_mass,
            metadata_elapsed_time=None,
            metadata_last_updated=anchor,
        )

        assert player.state.elapsed_time == 300.0
        assert player.state.current_media is not None
        assert player.state.current_media.elapsed_time == 300

    def test_the_source_names_what_is_playing(self, mock_mass: MagicMock) -> None:
        """The media on screen is the track the source reports, on the source's uri."""
        player = self._player_with_source(
            mock_mass, metadata_elapsed_time=12, metadata_last_updated=time.time()
        )

        media = player.state.current_media
        assert media is not None
        assert media.title == "Live Track"
        assert media.media_type is MediaType.AUDIO_SOURCE
        assert media.uri == "test_instance://audio_source/main"
        # the owner of the session, which its stream url is keyed on
        assert media.source_id == "player_1"
        # no queue item: this is not playing out of a queue
        assert media.queue_item_id is None


class TestLiveSourceAudioDetails:
    """Audio details published for a live external source."""

    @staticmethod
    def _details(quality: AudioQuality = AudioQuality.LOSSLESS) -> ActiveSourceAudioDetails:
        """Return source audio details with a known lossless input."""
        return ActiveSourceAudioDetails(
            input_format=AudioFormat(
                content_type=ContentType.FLAC,
                sample_rate=44100,
                bit_depth=16,
                channels=2,
            ),
            input_fidelity=AudioFidelity(quality=quality),
            crossfade_mode=CrossfadeMode.SOURCE,
            volume_normalization_mode=VolumeNormalizationMode.SOURCE,
        )

    def test_source_audio_details_reach_player_state(self, mock_mass: MagicMock) -> None:
        """The source session's audio snapshot is part of the published player state."""
        details = self._details()
        source = AudioSource(
            item_id="main",
            provider="test_instance",
            name="Spotify Connect",
            provider_mappings=set(),
        )
        session = AudioSourceSession(
            player_id="player_1",
            source=source,
            provider_instance_id="test_instance",
            active_source_audio=details,
        )
        mock_mass.players.get_audio_source_session.return_value = session
        player, _ = attach_mock(MockProvider("test_provider", mass=mock_mass), "player_1", "Player")

        player.update_state(signal_event=False)

        assert player.state.active_source_audio == details

    def test_replacing_source_audio_details_signals_player_update(
        self, mock_mass: MagicMock
    ) -> None:
        """Replacing the snapshot is detected even when no player-owned input changed."""
        first = self._details()
        source = AudioSource(
            item_id="main",
            provider="test_instance",
            name="Spotify Connect",
            provider_mappings=set(),
        )
        session = AudioSourceSession(
            player_id="player_1",
            source=source,
            provider_instance_id="test_instance",
            active_source_audio=first,
        )
        mock_mass.players.get_audio_source_session.return_value = session
        player, _ = attach_mock(MockProvider("test_provider", mass=mock_mass), "player_1", "Player")
        player.update_state(signal_event=False)
        mock_mass.players.signal_player_state_update.reset_mock()
        session.active_source_audio = self._details(AudioQuality.STANDARD)

        player.mark_state_dirty()
        player.update_state()

        assert player.state.active_source_audio is not None
        assert player.state.active_source_audio.input_fidelity.quality is AudioQuality.STANDARD
        mock_mass.players.signal_player_state_update.assert_called_once()

    def test_ending_source_session_clears_player_audio_details(self, mock_mass: MagicMock) -> None:
        """A player no longer owning the source publishes no source audio snapshot."""
        source = AudioSource(
            item_id="main",
            provider="test_instance",
            name="Spotify Connect",
            provider_mappings=set(),
        )
        session = AudioSourceSession(
            player_id="player_1",
            source=source,
            provider_instance_id="test_instance",
            active_source_audio=self._details(),
        )
        mock_mass.players.get_audio_source_session.return_value = session
        player, _ = attach_mock(MockProvider("test_provider", mass=mock_mass), "player_1", "Player")
        player.update_state(signal_event=False)
        mock_mass.players.get_audio_source_session.return_value = None

        player.mark_state_dirty()
        player.update_state(signal_event=False)

        assert player.state.active_source_audio is None


class TestMediaUpdatedCallback:
    """The (debounced) media-updated callback fires on media identity changes."""

    def test_palette_resolution_fires_media_updated(
        self, mock_mass: MagicMock, player: Player, protocol: MockProtocol
    ) -> None:
        """A late palette resolution re-fires the media-updated callback."""
        protocol.set_current_media(uri="http://test/stream", title="Test", image_url="http://img")
        player.update_state(signal_event=False)
        mock_mass.call_later.reset_mock()

        player.set_resolved_palette("http://img", MagicMock())
        player.update_state(force_update=True, signal_event=False)

        assert any(
            call.kwargs.get("task_id") == f"player_media_updated_{player.player_id}"
            for call in mock_mass.call_later.call_args_list
        )


class TestCacheInvalidationClasses:
    """Config-derived cached properties survive state updates, all others refresh."""

    def test_config_cached_props_survive_state_updates(self, player: Player) -> None:
        """Config-derived cached properties are not recomputed on state updates."""
        assert "_icon" in player._cache
        # nothing changed, so this only drops what every update calculates again
        player.update_state(signal_event=False)
        assert set(player._cache) == {"_hide_in_ui", "_expose_to_ha"}

    def test_set_config_invalidates_all_cached_props(self, player: Player) -> None:
        """set_config invalidates every cached property, including config-derived ones."""
        assert "_icon" in player._cache
        player.set_config(player.config)
        assert len(player._cache) == 0

    def test_player_implementation_cached_props_cleared_each_update(
        self, protocol: MockProtocol
    ) -> None:
        """Cached properties defined by player implementations refresh on every update."""
        protocol._cache["some_provider_prop"] = object()
        protocol.update_state()
        assert "some_provider_prop" not in protocol._cache


class TestInputCollection:
    """What update_state looks up to calculate the state."""

    @pytest.mark.parametrize("synced", [True, False])
    def test_grouping_candidates_are_only_looked_up_for_a_player_that_can_group(
        self, mock_mass: MagicMock, synced: bool
    ) -> None:
        """A synced player has nothing to group with, so its candidates are left alone."""
        provider = MockProvider("test_provider", mass=mock_mass)
        player, protocol = attach_mock(provider, "player_1", "Player 1")
        protocol._attr_can_group_with = {"test_instance"}
        leader, leader_protocol = attach_mock(provider, "leader", "Leader")
        leader_protocol._attr_group_members = ["leader", "player_1"] if synced else []

        player.update_state(signal_event=False)

        assert player.state.synced_to == (leader.player_id if synced else None)
        assert mock_mass.get_provider.called is not synced

    @pytest.mark.parametrize("grouped_id", ["member", "member_protocol", "old_member"])
    @pytest.mark.parametrize("can_play", [True, False])
    def test_group_volume_counts_the_members_that_can_play(
        self, mock_mass: MagicMock, grouped_id: str, can_play: bool
    ) -> None:
        """A member decides the group volume while it can play, however the group names it."""
        provider = MockProvider("test_provider", mass=mock_mass)
        leader, leader_protocol = attach_mock(provider, "leader", "Leader")
        leader_protocol._attr_volume_level = 20
        leader_protocol._attr_group_members = ["leader", grouped_id]
        member, member_protocol = attach_mock(provider, "member", "Member")
        member_protocol._attr_volume_level = 80
        member_protocol._attr_available = can_play
        _, member_audio = attach_mock(
            provider, "member_protocol", "Member", role=ProtocolRole.AUDIO, player=member
        )
        member_audio._attr_available = can_play
        # the member also answers to a legacy id
        mock_mass.players._legacy_ids["old_member"] = member.player_id
        member.update_state(signal_event=False)

        leader.update_state(signal_event=False)

        assert leader.state.group_members == [leader.player_id, member.player_id]
        assert leader.state.group_volume == (80 if can_play else 20)

    def test_sync_leader_named_by_a_legacy_id_is_followed(self, mock_mass: MagicMock) -> None:
        """A player synced to a legacy id of its leader mirrors the player that answers to it."""
        provider = MockProvider("test_provider", mass=mock_mass)
        leader, leader_protocol = attach_mock(provider, "leader", "Leader")
        leader_protocol._attr_playback_state = PlaybackState.PLAYING
        leader_protocol.set_current_media(uri="http://test/stream", title="Song")
        follower, follower_protocol = attach_mock(
            provider, "follower", "Follower", protocol_class=_SyncedProtocol
        )
        assert isinstance(follower_protocol, _SyncedProtocol)
        follower_protocol.leader_id = "old_leader"
        mock_mass.players._legacy_ids["old_leader"] = leader.player_id
        leader.update_state(signal_event=False)

        follower.update_state(signal_event=False)

        assert follower.state.synced_to == leader.player_id
        assert follower.state.playback_state == PlaybackState.PLAYING
        assert follower.state.active_source == leader.state.active_source
        assert follower.state.current_media is leader.state.current_media

    def test_members_of_the_active_output_protocol_count_for_the_group_volume(
        self, mock_mass: MagicMock
    ) -> None:
        """A player grouped through the protocol that plays decides the group volume as well."""
        provider = MockProvider("test_provider", mass=mock_mass)
        leader, leader_protocol = attach_mock(provider, "leader", "Leader")
        leader_protocol._attr_volume_level = 20
        _, leader_audio = attach_mock(
            provider, "leader_protocol", "Leader", role=ProtocolRole.AUDIO, player=leader
        )
        leader_audio._attr_group_members = ["leader_protocol", "member"]
        member, member_protocol = attach_mock(provider, "member", "Member")
        member_protocol._attr_volume_level = 80
        member.update_state(signal_event=False)

        leader.set_active_output_protocol("leader_protocol")

        assert leader.state.group_members == [leader.player_id, member.player_id]
        assert leader.state.group_volume == 80

    def test_member_of_an_active_group_player_shows_what_the_group_plays(
        self, mock_mass: MagicMock
    ) -> None:
        """A group player holding the player is where the player takes its source and media."""
        provider = MockProvider("test_provider", mass=mock_mass)
        group, group_protocol = attach_mock(
            provider, "group_1", "Group", role=ProtocolRole.VIRTUAL, player_type=PlayerType.GROUP
        )
        player, _ = attach_mock(provider, "player_1", "Player 1")
        # a group names its members by the id of their player
        group_protocol._attr_group_members = [player.player_id]
        group_protocol._attr_active_source = "group_1"
        group_protocol._attr_playback_state = PlaybackState.PLAYING
        group_protocol.set_current_media(uri="http://test/group", title="Group Song")
        group.update_state(signal_event=False)

        player.update_state(signal_event=False)

        assert player.state.active_group == group.player_id
        assert player.state.active_source == group.state.active_source
        assert player.state.current_media is group.state.current_media

    @pytest.mark.parametrize(
        ("kind", "looked_up"),
        [("own_queue", True), ("synced", False), ("external_source", False)],
    )
    def test_artwork_is_only_looked_up_for_the_queue_the_media_is_taken_from(
        self, mock_mass: MagicMock, kind: str, looked_up: bool
    ) -> None:
        """A player that shows other media than the item of its queue needs no artwork of it."""
        queue = PlayerQueue(
            queue_id="player_1",
            active=True,
            display_name="Player 1",
            available=True,
            items=1,
            current_item=QueueItem(
                queue_id="player_1",
                queue_item_id="item_1",
                name="Song",
                duration=200,
                image=_image(),
            ),
        )
        mock_mass.player_queues.get = MagicMock(side_effect={"player_1": queue}.get)
        mock_mass.metadata.get_image_url = MagicMock(return_value="http://ma/cover.jpg")
        provider = MockProvider("test_provider", mass=mock_mass)
        player, protocol = attach_mock(provider, "protocol_1", "Player 1", player_id="player_1")
        protocol._attr_playback_state = PlaybackState.PLAYING
        _, leader_protocol = attach_mock(provider, "leader", "Leader")
        if kind == "synced":
            leader_protocol._attr_group_members = ["leader", "protocol_1"]
        if kind == "external_source":
            protocol._attr_active_source = "line_in"

        player.update_state(signal_event=False)

        assert mock_mass.metadata.get_image_url.called is looked_up
        media = player.state.current_media
        assert (media.image_url if media else None) == (
            "http://ma/cover.jpg" if looked_up else None
        )

    def test_image_of_the_media_item_is_shown_for_a_queue_item_without_one(
        self, mock_mass: MagicMock
    ) -> None:
        """A queue item without an image of its own shows the image of the track it holds."""
        player = _queue_item_player(mock_mass, _queue_item(image=None))

        player.update_state(signal_event=False)

        assert mock_mass.metadata.get_image_url.call_count == 1
        assert player.state.current_media is not None
        assert player.state.current_media.image_url == "http://ma/track.jpg"

    @pytest.mark.parametrize("stream_image_url", [None, "http://stream/now.jpg"])
    def test_artwork_of_a_stream_with_metadata_is_only_looked_up_where_it_is_shown(
        self, mock_mass: MagicMock, stream_image_url: str | None
    ) -> None:
        """A stream that reports metadata shows its own image, or else that of the queue item."""
        streamdetails = StreamDetails(
            provider="test_provider",
            item_id="item_1",
            audio_format=AudioFormat(),
            media_type=MediaType.RADIO,
            stream_metadata=StreamMetadata(title="Live Track", image_url=stream_image_url),
        )
        player = _queue_item_player(mock_mass, _queue_item(streamdetails=streamdetails))

        player.update_state(signal_event=False)

        assert mock_mass.metadata.get_image_url.call_count == (1 if stream_image_url is None else 0)
        assert player.state.current_media is not None
        assert player.state.current_media.image_url == (stream_image_url or "http://ma/item.jpg")

    @pytest.mark.parametrize("stream_image_url", [None, "http://stream/now.jpg"])
    def test_image_of_a_live_source_is_only_looked_up_when_what_it_plays_has_none(
        self, mock_mass: MagicMock, stream_image_url: str | None
    ) -> None:
        """What a live source is playing may come with an image, which is the one shown."""
        source = AudioSource(
            item_id="main",
            provider="test_instance",
            name="Spotify Connect",
            provider_mappings={
                ProviderMapping(
                    item_id="main",
                    provider_domain="spotify_connect",
                    provider_instance="test_instance",
                )
            },
        )
        source.metadata.images = UniqueList([_image()])
        session = AudioSourceSession(
            player_id="player_1",
            source=source,
            provider_instance_id="test_instance",
            stream_metadata=StreamMetadata(title="Live Track", image_url=stream_image_url),
        )
        mock_mass.players.get_audio_source_session = MagicMock(return_value=session)
        mock_mass.metadata.get_image_url = MagicMock(return_value="http://ma/source.jpg")
        provider = MockProvider("test_provider", mass=mock_mass)
        player, _ = attach_mock(provider, "player_1", "Player 1")

        player.update_state(signal_event=False)

        assert mock_mass.metadata.get_image_url.called is (stream_image_url is None)
        assert player.state.current_media is not None
        assert player.state.current_media.image_url == (stream_image_url or "http://ma/source.jpg")


class TestLegacyIds:
    """The legacy ids stored with the config of a player reach its state."""

    @pytest.mark.parametrize(
        ("stored", "expected"),
        [
            pytest.param(["old_1", "old_2"], ["old_1", "old_2"], id="list"),
            pytest.param(None, [], id="none_stored"),
            pytest.param("old_1", [], id="not_a_list"),
            pytest.param([None, 7, "old_1", ["old_2"]], ["old_1"], id="not_strings"),
        ],
    )
    def test_stored_legacy_ids_reach_the_state(
        self, mock_mass: MagicMock, stored: Any, expected: list[str]
    ) -> None:
        """The stored list is published as it is, entries of an unexpected shape left out."""
        _store_legacy_ids(mock_mass, stored)
        provider = MockProvider("test_provider", mass=mock_mass)
        player, _ = attach_mock(provider, "player_1", "Player 1")

        player.update_state(signal_event=False)

        assert player.state.legacy_ids == expected

    def test_changed_legacy_ids_publish_an_update(
        self, mock_mass: MagicMock, player: Player
    ) -> None:
        """Clients hear of legacy ids stored for a player once its state is recalculated."""
        _store_legacy_ids(mock_mass, ["old_1"])

        player.mark_state_dirty()
        player.update_state()

        mock_mass.players.signal_player_state_update.assert_called_once()
        changed_values = mock_mass.players.signal_player_state_update.call_args.args[1]
        assert changed_values["legacy_ids"] == ((), ("old_1",))
        assert player.state.legacy_ids == ["old_1"]

    def test_wire_form_of_the_state_lists_the_legacy_ids(self, mock_mass: MagicMock) -> None:
        """The serialized state carries the legacy ids, and a client reads them back."""
        _store_legacy_ids(mock_mass, ["old_1"])
        provider = MockProvider("test_provider", mass=mock_mass)
        player, _ = attach_mock(provider, "player_1", "Player 1")

        player.update_state(signal_event=False)
        wire_form = player.to_dict()

        assert wire_form["legacy_ids"] == ["old_1"]
        assert PlayerState.from_dict(wire_form).legacy_ids == ["old_1"]


def _store_legacy_ids(mock_mass: MagicMock, stored: Any) -> None:
    """
    Store the given value as the legacy ids of every player.

    :param mock_mass: The mocked Music Assistant instance.
    :param stored: The value to store.
    """
    mock_mass.config.get_player_value = MagicMock(
        side_effect=lambda _player_id, key, default=None: (
            stored if key == CONF_LEGACY_IDS else default
        )
    )


def _queue_item(**values: Any) -> QueueItem:
    """
    Return a queue item with an image of its own that holds a track with an image.

    :param values: The values that differ from that queue item.
    """
    track = Track(
        item_id="t1",
        provider="test",
        name="Song",
        duration=200,
        provider_mappings={
            ProviderMapping(item_id="t1", provider_domain="test", provider_instance="test")
        },
    )
    track.metadata.images = UniqueList([_image("http://img/track.jpg")])
    defaults: dict[str, Any] = {
        "queue_id": "player_1",
        "queue_item_id": "item_1",
        "name": "Song",
        "duration": 200,
        "image": _image("http://img/item.jpg"),
        "media_item": track,
    }
    return QueueItem(**(defaults | values))


def _queue_item_player(mock_mass: MagicMock, current_item: QueueItem) -> Player:
    """
    Return a playing player whose own queue plays the given item.

    The artwork lookup returns the url of the image it is given, in the Music Assistant
    host: ``http://img/x.jpg`` becomes ``http://ma/x.jpg``.

    :param mock_mass: The mocked Music Assistant instance.
    :param current_item: The current item of the queue.
    """
    queue = PlayerQueue(
        queue_id="player_1",
        active=True,
        display_name="Player 1",
        available=True,
        items=1,
        current_item=current_item,
    )
    mock_mass.player_queues.get = MagicMock(side_effect={"player_1": queue}.get)
    mock_mass.metadata.get_image_url = MagicMock(
        side_effect=lambda image, **_: image.path.replace("http://img/", "http://ma/")
    )
    provider = MockProvider("test_provider", mass=mock_mass)
    player, protocol = attach_mock(provider, "protocol_1", "Player 1", player_id="player_1")
    protocol._attr_playback_state = PlaybackState.PLAYING
    return player


class TestUpdateStateTiming:
    """Micro-benchmarks guarding the cost of the update_state paths."""

    def test_no_change_update_is_fast(self, player: Player) -> None:
        """The no-change path completes well within the microsecond budget."""
        for _ in range(50):  # warmup
            player.update_state()
        timings = []
        for _ in range(200):
            start = time.perf_counter()
            player.update_state()
            timings.append(time.perf_counter() - start)
        duration = median(timings)
        # target is <50us on a dev machine; assert with generous CI headroom
        assert duration < 0.001, f"no-change update_state took {duration * 1e6:.0f}us (median)"

    def test_full_update_is_fast(self, player: Player, protocol: MockProtocol) -> None:
        """A full recalculation completes well within the microsecond budget."""
        for volume in range(50):  # warmup
            protocol._attr_volume_level = volume
            player.update_state(signal_event=False)
        timings = []
        for volume in range(200):
            protocol._attr_volume_level = volume % 100
            start = time.perf_counter()
            player.update_state(signal_event=False)
            timings.append(time.perf_counter() - start)
        duration = median(timings)
        # target is <500us on a dev machine; assert with generous CI headroom
        assert duration < 0.005, f"full update_state took {duration * 1e6:.0f}us (median)"
