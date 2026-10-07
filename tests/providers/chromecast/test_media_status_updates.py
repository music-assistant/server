"""
Tests for the player state ChromecastPlayer derives from a media status.

Every status the receiver sends is turned into the playback position, the metadata
of what is loaded, and - for a cast group holding a stereo pair - the state of its
members. These are what the user sees in the UI, so each is asserted on here.
"""

from __future__ import annotations

import time
from typing import Any, cast
from unittest.mock import MagicMock

from music_assistant_models.enums import MediaType, PlaybackState, PlayerType
from music_assistant_models.player import PlayerMedia

from music_assistant.providers.chromecast.constants import MASS_APP_ID
from music_assistant.providers.chromecast.player import ChromecastPlayer


def _fake_player(
    *,
    player_type: PlayerType = PlayerType.PLAYER,
    powered: bool = False,
    group_members: list[str] | None = None,
) -> Any:
    """
    Build a Cast player on mocked collaborators, so it handles media status for real.

    The playback state is seeded as if something was playing already, so the assertions
    prove the handler wrote the new state instead of matching an untouched default.

    :param player_type: Type of the player.
    :param powered: Whether the player is powered on.
    :param group_members: Player ids of the group's members, if it is a group.
    """
    # __init__ is skipped: it needs a provider, cast info and a live Chromecast connection.
    # Typed as Any because the collaborators below are read back as the mocks they are.
    fake = cast("Any", ChromecastPlayer.__new__(ChromecastPlayer))
    fake.mass = MagicMock()
    fake.logger = MagicMock()
    fake.cc = MagicMock(app_id=MASS_APP_ID)
    fake.update_state = MagicMock()
    fake._flow_stream_underrun = MagicMock(return_value=False)
    fake._media_error_reported = False
    fake._app_quit_task_id = "cast_quit_app_test"
    fake.active_cast_group = None
    # display_name is a cached property, normally built from the config in __init__
    fake._cache = {"display_name": "Test Cast"}
    # the state below is exposed through read-only properties on the base Player,
    # so it is seeded through the attributes backing them
    fake._attr_type = player_type
    fake._attr_powered = powered
    fake._attr_group_members = group_members or []
    fake._attr_playback_state = PlaybackState.PLAYING
    fake._attr_current_media = PlayerMedia(uri="http://mass/previous.flac")
    fake._attr_elapsed_time = 123.0
    fake._attr_elapsed_time_last_updated = 0.0
    return fake


def _fake_group_member(*, multichannel: bool) -> Any:
    """
    Build a member of a cast group on mocked collaborators.

    Its state is seeded as if left over from earlier playback, so the assertions
    prove whether the group wrote to the member or left it alone.

    :param multichannel: Whether the member is a multichannel group (a stereo pair).
    """
    # cast_info is set in __init__, so a mock specced on the class does not carry it
    child = MagicMock(spec=ChromecastPlayer)
    child.cast_info = MagicMock(is_multichannel_group=multichannel)
    child._attr_playback_state = PlaybackState.IDLE
    child._attr_current_media = None
    child._attr_elapsed_time = 0.0
    child._attr_elapsed_time_last_updated = 0.0
    child._attr_active_source = "previous_source"
    return child


def _fake_group_with_a_stereo_pair(*, powered: bool = True) -> tuple[Any, Any, Any]:
    """
    Build a cast group holding a regular speaker and a stereo pair, in that order.

    The regular speaker comes first because it is the one that has to be skipped: a group
    that gave up on the first member it skips would leave the stereo pair behind.

    :param powered: Whether the group is powered on.
    :return: The group, its regular speaker and its stereo pair.
    """
    group = _fake_player(
        player_type=PlayerType.GROUP,
        powered=powered,
        group_members=["speaker_1", "stereo_pair_1"],
    )
    speaker = _fake_group_member(multichannel=False)
    stereo_pair = _fake_group_member(multichannel=True)
    # by id, so the group is held to looking up the members it lists
    group.mass.players.get_player.side_effect = {
        "speaker_1": speaker,
        "stereo_pair_1": stereo_pair,
    }.get
    return group, speaker, stereo_pair


def _media_status(
    *,
    playing: bool = False,
    paused: bool = False,
    content_id: str = "http://mass/track.flac",
    current_time: float = 0.0,
    adjusted_current_time: float = 0.0,
    title: str | None = None,
    artist: str | None = None,
    album_name: str | None = None,
    image_url: str | None = None,
    duration: float | None = None,
) -> MagicMock:
    """
    Build a MediaStatus as the receiver reports it.

    :param playing: Whether the receiver reports playback.
    :param paused: Whether the receiver reports paused playback.
    :param content_id: Content id the receiver has loaded, if any.
    :param current_time: Position as of the moment the receiver sent the status.
    :param adjusted_current_time: Position extrapolated to now.
    :param title: Track title reported by the receiver.
    :param artist: Track artist reported by the receiver.
    :param album_name: Album name reported by the receiver.
    :param image_url: Cover art url reported by the receiver.
    :param duration: Track duration in seconds.
    """
    status = MagicMock()
    status.content_id = content_id
    status.player_state = "PLAYING" if playing else "PAUSED" if paused else "IDLE"
    status.player_is_playing = playing
    status.player_is_paused = paused
    status.player_is_idle = not playing and not paused
    status.idle_reason = None
    status.current_time = current_time
    status.adjusted_current_time = adjusted_current_time
    status.title = title
    status.artist = artist
    status.album_name = album_name
    status.images = [MagicMock(url=image_url)] if image_url else []
    status.duration = duration
    return status


def test_the_position_while_playing_runs_on_between_updates() -> None:
    """The receiver only sends a status now and then, so the position is extrapolated."""
    fake = _fake_player()

    fake._handle_media_status(
        _media_status(playing=True, current_time=40.0, adjusted_current_time=42.5)
    )

    assert fake._attr_elapsed_time == 42.5


def test_the_position_while_paused_is_the_reported_one() -> None:
    """A paused position stands still, so extrapolating it would run it off."""
    fake = _fake_player()

    fake._handle_media_status(
        _media_status(paused=True, current_time=12.5, adjusted_current_time=99.0)
    )

    assert fake._attr_elapsed_time == 12.5


def test_the_position_is_stamped_with_the_moment_it_was_read() -> None:
    """A position only means something together with when it was taken."""
    fake = _fake_player()
    before = time.time()

    fake._handle_media_status(_media_status(playing=True, adjusted_current_time=5.0))

    assert fake._attr_elapsed_time_last_updated >= before


def test_the_metadata_of_what_is_playing_is_published() -> None:
    """What the receiver reports about the track is what MA shows for it."""
    fake = _fake_player()

    fake._handle_media_status(
        _media_status(
            playing=True,
            content_id="http://mass/flow/track.flac",
            title="Sun Rays",
            artist="The Testers",
            album_name="Fixture Sessions",
            image_url="http://mass/cover.jpg",
            duration=241.7,
        )
    )

    media = fake._attr_current_media
    assert media.uri == "http://mass/flow/track.flac"
    assert media.title == "Sun Rays"
    assert media.artist == "The Testers"
    assert media.album == "Fixture Sessions"
    assert media.image_url == "http://mass/cover.jpg"
    assert media.duration == 241
    assert media.media_type == MediaType.TRACK


def test_a_paused_player_keeps_showing_what_is_loaded() -> None:
    """Pausing does not unload the track, so its metadata stays on screen."""
    fake = _fake_player()

    fake._handle_media_status(_media_status(paused=True, title="Sun Rays", artist="The Testers"))

    assert fake._attr_current_media.title == "Sun Rays"
    assert fake._attr_current_media.artist == "The Testers"


def test_the_previous_track_metadata_does_not_linger() -> None:
    """A track without an album must not keep showing the previous track's album."""
    fake = _fake_player()
    fake._handle_media_status(
        _media_status(
            playing=True,
            content_id="http://mass/one.flac",
            title="Sun Rays",
            album_name="Fixture Sessions",
        )
    )

    fake._handle_media_status(
        _media_status(playing=True, content_id="http://mass/two.flac", title="Moon Rays")
    )

    assert fake._attr_current_media.title == "Moon Rays"
    assert fake._attr_current_media.album is None


def test_an_idle_status_does_not_put_its_content_back_on_screen() -> None:
    """A receiver keeps naming the content it last had loaded, which is not playback."""
    fake = _fake_player()

    fake._handle_media_status(_media_status(content_id="http://mass/track.flac"))

    assert fake._attr_current_media is None


def test_a_multichannel_member_follows_its_group() -> None:
    """A stereo pair gets no status of its own, so the group hands it its state."""
    group, _, stereo_pair = _fake_group_with_a_stereo_pair()

    group._handle_media_status(
        _media_status(playing=True, title="Sun Rays", adjusted_current_time=30.0)
    )

    assert stereo_pair._attr_playback_state == PlaybackState.PLAYING
    assert stereo_pair._attr_current_media is group._attr_current_media
    assert stereo_pair._attr_elapsed_time == 30.0
    assert stereo_pair._attr_elapsed_time_last_updated == group._attr_elapsed_time_last_updated
    # the group plays an MA queue, which is not a source the member is on
    assert stereo_pair._attr_active_source is None
    stereo_pair.update_state.assert_called_once()


def test_a_regular_group_member_is_left_alone() -> None:
    """A regular speaker reports its own status, which the group must not overwrite."""
    group, speaker, _ = _fake_group_with_a_stereo_pair()

    group._handle_media_status(_media_status(playing=True, adjusted_current_time=30.0))

    assert speaker._attr_playback_state == PlaybackState.IDLE
    assert speaker._attr_current_media is None
    assert speaker._attr_elapsed_time == 0.0
    assert speaker._attr_active_source == "previous_source"
    speaker.update_state.assert_not_called()


def test_an_unpowered_group_does_not_push_state_to_its_members() -> None:
    """A group that is off has no playback to hand out."""
    group, _, stereo_pair = _fake_group_with_a_stereo_pair(powered=False)

    group._handle_media_status(_media_status(playing=True, adjusted_current_time=30.0))

    assert stereo_pair._attr_playback_state == PlaybackState.IDLE
    stereo_pair.update_state.assert_not_called()
