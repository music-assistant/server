"""
Tests for the per-player AudioSource session record.

The session holds what an external source is, who owns it and what it reports,
without any of it living in a queue item. Nothing produces a session yet, so
these tests drive the mixin directly.
"""

from typing import cast
from unittest.mock import MagicMock

from music_assistant_models.enums import ContentType, MediaType, ProviderFeature, StreamType
from music_assistant_models.media_items import AudioFormat, AudioSource
from music_assistant_models.media_items.provider_mapping import ProviderMapping
from music_assistant_models.streamdetails import StreamDetails, StreamMetadata

from music_assistant.controllers.players import PlayerController
from music_assistant.controllers.players.audio_sources import (
    AudioSourceMixin,
    AudioSourceSession,
)
from music_assistant.models.music_provider import MusicProvider
from music_assistant.models.plugin import PluginProvider

PLAYER_ID = "player-1"
PROVIDER_INSTANCE = "spotify_connect--abc"
SOURCE_ID = "main"


def _audio_source(item_id: str = SOURCE_ID, *, can_seek: bool = False) -> AudioSource:
    return AudioSource(
        item_id=item_id,
        provider=PROVIDER_INSTANCE,
        name="Spotify Connect",
        provider_mappings={
            ProviderMapping(
                item_id=item_id,
                provider_domain="spotify_connect",
                provider_instance=PROVIDER_INSTANCE,
            )
        },
        can_play_pause=True,
        can_seek=can_seek,
    )


class _Controller(AudioSourceMixin):
    """Minimal stand-in for the parts of PlayerController the mixin relies on."""

    def __init__(self, provider: object | None) -> None:
        self._source_sessions: dict[str, AudioSourceSession] = {}
        self.mass = MagicMock()
        self.mass.get_provider.return_value = provider
        # kept under its own name so assertions can read the mock's call record
        self.log_mock = MagicMock()
        self.logger = self.log_mock
        self.updated_players: list[str] = []

    def trigger_player_update(self, player_id: str) -> None:
        """Record that a player update was signalled."""
        self.updated_players.append(player_id)


def _streamdetails(metadata: StreamMetadata) -> StreamDetails:
    return StreamDetails(
        provider=PROVIDER_INSTANCE,
        item_id=SOURCE_ID,
        audio_format=AudioFormat(content_type=ContentType.PCM_S16LE),
        media_type=MediaType.AUDIO_SOURCE,
        stream_type=StreamType.CUSTOM,
        stream_metadata=metadata,
    )


def _plugin_provider(*, with_feature: bool = True) -> MagicMock:
    provider = MagicMock(spec=PluginProvider)
    provider.instance_id = PROVIDER_INSTANCE
    provider.supported_features = {ProviderFeature.AUDIO_SOURCE} if with_feature else set()
    return provider


def test_no_session_by_default() -> None:
    """A player with nothing playing on it has no session and no source."""
    ctrl = _Controller(_plugin_provider())
    assert ctrl.get_audio_source_session(PLAYER_ID) is None
    assert ctrl.get_player_audio_source(PLAYER_ID) is None


def test_started_session_resolves_source_and_provider() -> None:
    """A started session resolves to its AudioSource and owning plugin."""
    provider = _plugin_provider()
    ctrl = _Controller(provider)
    source = _audio_source()
    session = ctrl._start_audio_source_session(PLAYER_ID, source, PROVIDER_INSTANCE)

    assert session.player_id == PLAYER_ID
    assert session.source_id == SOURCE_ID
    assert session.streamdetails is None
    assert session.stream_metadata is None
    assert session.stream_session_id is None
    assert session.started_at > 0

    assert ctrl.get_audio_source_session(PLAYER_ID) is session
    assert ctrl.get_player_audio_source(PLAYER_ID) == (source, provider)


def test_starting_a_second_session_replaces_the_first() -> None:
    """A player outputs one source at a time, so a new session replaces the old."""
    ctrl = _Controller(_plugin_provider())
    first = ctrl._start_audio_source_session(PLAYER_ID, _audio_source("first"), PROVIDER_INSTANCE)
    second = ctrl._start_audio_source_session(PLAYER_ID, _audio_source("second"), PROVIDER_INSTANCE)

    current = ctrl.get_audio_source_session(PLAYER_ID)
    assert current is second
    assert current.source_id == "second"
    cast("MagicMock", ctrl.mass.streams.audio_processing.clear_source).assert_called_once_with(
        PLAYER_ID, first.playback_session_id
    )


def test_ending_a_session_drops_and_returns_it() -> None:
    """Ending a session removes it from the player and hands it back."""
    ctrl = _Controller(_plugin_provider())
    session = ctrl._start_audio_source_session(PLAYER_ID, _audio_source(), PROVIDER_INSTANCE)

    assert ctrl._end_audio_source_session(PLAYER_ID) is session
    cast("MagicMock", ctrl.mass.streams.audio_processing.clear_source).assert_called_once_with(
        PLAYER_ID, session.playback_session_id
    )
    assert ctrl.get_audio_source_session(PLAYER_ID) is None
    # ending twice is harmless
    assert ctrl._end_audio_source_session(PLAYER_ID) is None


def test_sessions_are_isolated_per_player() -> None:
    """A session on one player is invisible to another."""
    ctrl = _Controller(_plugin_provider())
    ctrl._start_audio_source_session(PLAYER_ID, _audio_source(), PROVIDER_INSTANCE)

    assert ctrl.get_audio_source_session("player-2") is None
    assert ctrl.get_player_audio_source("player-2") is None


def test_source_unresolvable_when_provider_is_gone() -> None:
    """A session whose plugin has unloaded resolves to nothing."""
    ctrl = _Controller(None)
    ctrl._start_audio_source_session(PLAYER_ID, _audio_source(), PROVIDER_INSTANCE)

    assert ctrl.get_audio_source_session(PLAYER_ID) is not None
    assert ctrl.get_player_audio_source(PLAYER_ID) is None


def test_source_unresolvable_when_the_feature_was_turned_off() -> None:
    """A provider that dropped the AUDIO_SOURCE feature at runtime is skipped."""
    ctrl = _Controller(_plugin_provider(with_feature=False))
    ctrl._start_audio_source_session(PLAYER_ID, _audio_source(), PROVIDER_INSTANCE)

    assert ctrl.get_player_audio_source(PLAYER_ID) is None


def test_source_unresolvable_when_the_provider_is_not_a_plugin() -> None:
    """
    A non-PluginProvider behind the instance id is skipped.

    It declares the AUDIO_SOURCE feature so the feature guard cannot be what
    rejects it — only the isinstance check can.
    """
    not_a_plugin = MagicMock(spec=MusicProvider)
    not_a_plugin.supported_features = {ProviderFeature.AUDIO_SOURCE}
    ctrl = _Controller(not_a_plugin)
    ctrl._start_audio_source_session(PLAYER_ID, _audio_source(), PROVIDER_INSTANCE)

    assert ctrl.get_player_audio_source(PLAYER_ID) is None


def test_metadata_update_is_accepted_before_streamdetails_exist() -> None:
    """The owning plugin can replay what it knows the moment it claims the player."""
    ctrl = _Controller(_plugin_provider())
    session = ctrl._start_audio_source_session(PLAYER_ID, _audio_source(), PROVIDER_INSTANCE)
    metadata = StreamMetadata(title="Take Five", artist="Dave Brubeck")

    ctrl.update_source_metadata(PLAYER_ID, SOURCE_ID, PROVIDER_INSTANCE, metadata)

    assert session.streamdetails is None
    assert session.stream_metadata is metadata
    assert session.stream_metadata_last_updated is not None
    assert ctrl.updated_players == [PLAYER_ID]


def test_metadata_update_rejected_for_another_source() -> None:
    """Metadata for a source that is not the one playing is dropped."""
    ctrl = _Controller(_plugin_provider())
    session = ctrl._start_audio_source_session(PLAYER_ID, _audio_source(), PROVIDER_INSTANCE)

    ctrl.update_source_metadata(
        PLAYER_ID, "some-other-source", PROVIDER_INSTANCE, StreamMetadata(title="Nope")
    )

    assert session.stream_metadata is None
    assert ctrl.updated_players == []
    assert ctrl.log_mock.debug.called


def test_metadata_update_rejected_for_another_provider() -> None:
    """Metadata from a provider that does not own the session is dropped."""
    ctrl = _Controller(_plugin_provider())
    session = ctrl._start_audio_source_session(PLAYER_ID, _audio_source(), PROVIDER_INSTANCE)

    ctrl.update_source_metadata(
        PLAYER_ID, SOURCE_ID, "airplay_receiver--xyz", StreamMetadata(title="Nope")
    )

    assert session.stream_metadata is None
    assert ctrl.updated_players == []
    assert ctrl.log_mock.debug.called


def test_metadata_update_rejected_when_nothing_is_playing() -> None:
    """Metadata for a player with no session is dropped without raising."""
    ctrl = _Controller(_plugin_provider())

    ctrl.update_source_metadata(
        PLAYER_ID, SOURCE_ID, PROVIDER_INSTANCE, StreamMetadata(title="Nope")
    )

    assert ctrl.get_audio_source_session(PLAYER_ID) is None
    assert ctrl.updated_players == []
    assert ctrl.log_mock.debug.called


def test_reconnecting_the_same_source_keeps_its_metadata() -> None:
    """
    A drop and reconnect re-stamps the stream token without losing what was known.

    The queue-borne path reuses the cached streamdetails on a reconnect, so the
    reported track survives one; a fresh session per stream request would blank it
    until the plugin happened to push again.
    """
    ctrl = _Controller(_plugin_provider())
    source = _audio_source()
    first = ctrl._start_audio_source_session(PLAYER_ID, source, PROVIDER_INSTANCE)
    audio_details = MagicMock()
    first.active_source_audio = audio_details
    first_session_id = first.playback_session_id
    ctrl.update_source_metadata(
        PLAYER_ID, SOURCE_ID, PROVIDER_INSTANCE, StreamMetadata(title="Take Five")
    )

    again = ctrl._start_audio_source_session(PLAYER_ID, source, PROVIDER_INSTANCE)

    assert again is first
    assert again.stream_metadata is not None
    assert again.stream_metadata.title == "Take Five"
    assert again.started_at == first.started_at
    assert again.active_source_audio is audio_details
    cast("MagicMock", ctrl.mass.streams.audio_processing.clear_source).assert_called_once_with(
        PLAYER_ID,
        first_session_id,
        preserve_details=True,
    )


def test_selecting_a_different_source_does_not_keep_the_old_metadata() -> None:
    """A different source is a new session, so nothing carries over."""
    ctrl = _Controller(_plugin_provider())
    ctrl._start_audio_source_session(PLAYER_ID, _audio_source("first"), PROVIDER_INSTANCE)
    ctrl.update_source_metadata(
        PLAYER_ID, "first", PROVIDER_INSTANCE, StreamMetadata(title="Take Five")
    )

    second = ctrl._start_audio_source_session(PLAYER_ID, _audio_source("second"), PROVIDER_INSTANCE)

    assert second.source_id == "second"
    assert second.stream_metadata is None


def test_attaching_streamdetails_adopts_their_metadata() -> None:
    """The placeholder a plugin sets in get_stream_details is what the session reports."""
    ctrl = _Controller(_plugin_provider())
    session = ctrl._start_audio_source_session(PLAYER_ID, _audio_source(), PROVIDER_INSTANCE)

    session.attach_streamdetails(_streamdetails(StreamMetadata(title="VBAN | Studio")))

    assert session.streamdetails is not None
    assert session.stream_metadata is not None
    assert session.stream_metadata.title == "VBAN | Studio"
    assert session.stream_metadata_last_updated is not None


def test_attaching_streamdetails_does_not_overwrite_a_reported_track() -> None:
    """A source that already reported something keeps it: the placeholder is only a fallback."""
    ctrl = _Controller(_plugin_provider())
    session = ctrl._start_audio_source_session(PLAYER_ID, _audio_source(), PROVIDER_INSTANCE)
    ctrl.update_source_metadata(
        PLAYER_ID, SOURCE_ID, PROVIDER_INSTANCE, StreamMetadata(title="Take Five")
    )

    session.attach_streamdetails(_streamdetails(StreamMetadata(title="Spotify Connect | Kitchen")))

    assert session.stream_metadata is not None
    assert session.stream_metadata.title == "Take Five"


def test_source_id_is_provider_scoped_and_uri_is_unique() -> None:
    """Every shipped plugin names its only source "main", so the uri is the unique handle."""
    ctrl = _Controller(_plugin_provider())
    session = ctrl._start_audio_source_session(PLAYER_ID, _audio_source(), PROVIDER_INSTANCE)

    assert session.source_id == "main"
    assert session.source_uri != session.source_id
    assert session.source_uri is not None
    assert PROVIDER_INSTANCE in session.source_uri


def test_mixin_is_attached_to_the_real_player_controller() -> None:
    """The carrier is wired into PlayerController and its store is initialised."""
    mass = MagicMock()
    mass.config.get_raw_core_config_value.return_value = "INFO"
    controller = PlayerController(mass)

    assert isinstance(controller, AudioSourceMixin)
    assert controller._source_sessions == {}
    assert controller.get_audio_source_session("no-such-player") is None


def test_reselecting_adopts_a_rebuilt_source() -> None:
    """
    A plugin that rebuilds its source with new capabilities gets those reported.

    spotify_connect and yandex_ynison rebuild the AudioSource whenever a
    capability flag changes; ynison's _update_source_capabilities even overwrites
    the queue item's snapshot so the new flags reach the UI without waiting for
    the next play. A session that kept the first object would report stale flags.
    """
    ctrl = _Controller(_plugin_provider())
    session = ctrl._start_audio_source_session(
        PLAYER_ID, _audio_source(can_seek=False), PROVIDER_INSTANCE
    )
    ctrl.update_source_metadata(
        PLAYER_ID, SOURCE_ID, PROVIDER_INSTANCE, StreamMetadata(title="Take Five")
    )

    rebuilt = _audio_source(can_seek=True)
    again = ctrl._start_audio_source_session(PLAYER_ID, rebuilt, PROVIDER_INSTANCE)

    assert again is session
    assert again.source is rebuilt
    assert again.source.can_seek is True
    # the reported track still survives the reselect
    assert again.stream_metadata is not None
    assert again.stream_metadata.title == "Take Five"


def test_a_fallback_only_source_follows_a_changed_placeholder() -> None:
    """
    A source that never reports keeps taking its placeholder from the stream details.

    vban_receiver and sendspin_source never call update_stream_metadata, so the
    placeholder is all they have. Adopting one must not stop a later one landing:
    a VBAN reconnect from a different sender describes something else.
    """
    ctrl = _Controller(_plugin_provider())
    session = ctrl._start_audio_source_session(PLAYER_ID, _audio_source(), PROVIDER_INSTANCE)

    session.attach_streamdetails(_streamdetails(StreamMetadata(title="VBAN | Studio")))
    assert session.stream_metadata is not None
    assert session.stream_metadata.title == "VBAN | Studio"

    session.attach_streamdetails(_streamdetails(StreamMetadata(title="VBAN | Booth")))
    assert session.stream_metadata.title == "VBAN | Booth"
    assert session.stream_metadata_reported is False


def test_claiming_a_session_stamps_the_stream_token() -> None:
    """A stream request claiming the live session becomes the one serving it."""
    ctrl = _Controller(_plugin_provider())
    session = ctrl._start_audio_source_session(PLAYER_ID, _audio_source(), PROVIDER_INSTANCE)

    assert ctrl.claim_audio_source_session(session, session.playback_session_id, "tok-1") is True
    assert session.stream_session_id == "tok-1"


def test_a_claimed_token_outlives_the_stream_that_stamped_it() -> None:
    """
    The stream token records that a selection was streamed, not that it still is.

    A paused source keeps the player while its stream is torn down, and what
    separates it from one that was never streamed at all is this token.
    """
    ctrl = _Controller(_plugin_provider())
    session = ctrl._start_audio_source_session(PLAYER_ID, _audio_source(), PROVIDER_INSTANCE)
    ctrl.claim_audio_source_session(session, session.playback_session_id, "tok-1")

    session.attach_streamdetails(_streamdetails(StreamMetadata(title="Take Five")))

    assert session.stream_session_id == "tok-1"


def test_claiming_a_superseded_session_is_refused() -> None:
    """A stream request set up for a session the player has left behind is turned away."""
    ctrl = _Controller(_plugin_provider())
    first = ctrl._start_audio_source_session(PLAYER_ID, _audio_source("first"), PROVIDER_INSTANCE)
    second = ctrl._start_audio_source_session(PLAYER_ID, _audio_source("second"), PROVIDER_INSTANCE)

    assert ctrl.claim_audio_source_session(first, first.playback_session_id, "tok-1") is False
    assert first.stream_session_id is None
    # the live session is not claimable with a stale playback token either
    assert ctrl.claim_audio_source_session(second, "a-token-from-before", "tok-2") is False
    assert second.stream_session_id is None


def test_a_handover_evicts_the_old_player_only_on_claim() -> None:
    """
    A source moving between players leaves the old one only when the stream claims it.

    Starting the new session is not the commit: a takeover that never produces a
    stream request must leave the source untouched on the player that has it.
    """
    ctrl = _Controller(_plugin_provider())
    source = _audio_source()
    session_a = ctrl._start_audio_source_session("player-a", source, PROVIDER_INSTANCE)
    session_b = ctrl._start_audio_source_session("player-b", source, PROVIDER_INSTANCE)

    clear_source = cast("MagicMock", ctrl.mass.streams.audio_processing.clear_source)
    assert ctrl.get_audio_source_session("player-a") is session_a
    assert ctrl.get_audio_source_session("player-b") is session_b
    assert "player-a" not in ctrl.updated_players
    clear_source.assert_not_called()

    assert ctrl.claim_audio_source_session(session_b, session_b.playback_session_id, "tok-1")

    assert ctrl.get_audio_source_session("player-a") is None
    assert ctrl.updated_players == ["player-a"]
    clear_source.assert_called_once_with("player-a", session_a.playback_session_id)

    # a renderer reconnect re-claims the same session without another eviction
    assert ctrl.claim_audio_source_session(session_b, session_b.playback_session_id, "tok-2")

    assert ctrl.updated_players == ["player-a"]
    clear_source.assert_called_once()


def test_a_reported_track_is_not_replaced_by_a_later_placeholder() -> None:
    """Once the source has reported, stream details stop overriding it."""
    ctrl = _Controller(_plugin_provider())
    session = ctrl._start_audio_source_session(PLAYER_ID, _audio_source(), PROVIDER_INSTANCE)
    session.attach_streamdetails(_streamdetails(StreamMetadata(title="Spotify Connect | Kitchen")))

    ctrl.update_source_metadata(
        PLAYER_ID, SOURCE_ID, PROVIDER_INSTANCE, StreamMetadata(title="Take Five")
    )
    assert session.stream_metadata_reported is True

    # a reconnect brings fresh stream details carrying only the placeholder again
    session.attach_streamdetails(_streamdetails(StreamMetadata(title="Spotify Connect | Kitchen")))

    assert session.stream_metadata is not None
    assert session.stream_metadata.title == "Take Five"
