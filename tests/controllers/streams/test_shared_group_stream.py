"""Tests for a member of a group fetching the one stream its group renders."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from unittest.mock import MagicMock, patch

from music_assistant_models.enums import ContentType, PlayerType, ProtocolRole
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.player import PlayerMedia

from music_assistant.controllers.streams import StreamsController
from tests.common import MockProtocol, MockProvider, attach_mock, mock_player_mass

PCM_FORMAT = AudioFormat(content_type=ContentType.PCM_S16LE, sample_rate=44100, bit_depth=16)
SHARED_URI = "http://ma.local/shared/group.flac"


async def _shared_audio() -> AsyncGenerator[bytes]:
    """Stand in for the audio of the stream a group renders."""
    yield b"shared"


class _SharingGroup(MockProtocol):
    """A group that hands its members the media of one stream it renders itself."""

    def get_shared_stream(
        self, media: PlayerMedia, pcm_format: AudioFormat
    ) -> AsyncGenerator[bytes] | None:
        """Return the shared audio for the media this group hands out."""
        return _shared_audio() if media.uri == SHARED_URI else None


def _streams(mass: MagicMock) -> StreamsController:
    """Return a streams controller on a mocked server, for the choice of the audio source."""
    streams = StreamsController.__new__(StreamsController)
    streams.mass = mass
    return streams


async def test_member_gets_the_stream_of_a_group_that_has_an_id_of_its_own() -> None:
    """The shared stream is found through the group player, whatever its id looks like."""
    mass = mock_player_mass()
    provider = MockProvider("universal_group", instance_id="universal_group", mass=mass)
    group, _ = attach_mock(
        provider,
        "ugp_abc",
        "Group",
        role=ProtocolRole.VIRTUAL,
        player_type=PlayerType.GROUP,
        protocol_class=_SharingGroup,
    )
    assert group.player_id != "ugp_abc"
    media = PlayerMedia(uri=SHARED_URI, source_id=group.player_id)

    stream = _streams(mass).get_stream(media, PCM_FORMAT, player_id="member_protocol")

    assert [chunk async for chunk in stream] == [b"shared"]


async def test_other_media_of_the_group_is_no_shared_stream() -> None:
    """Media the group does not share is fetched like any other, also when it names the group."""
    mass = mock_player_mass()
    provider = MockProvider("universal_group", instance_id="universal_group", mass=mass)
    group, _ = attach_mock(
        provider,
        "ugp_abc",
        "Group",
        role=ProtocolRole.VIRTUAL,
        player_type=PlayerType.GROUP,
        protocol_class=_SharingGroup,
    )
    media = PlayerMedia(uri="http://radio.local/stream.mp3", source_id=group.player_id)
    other_stream = MagicMock()

    with patch(
        "music_assistant.controllers.streams.controller.get_ffmpeg_stream",
        return_value=other_stream,
    ):
        stream = _streams(mass).get_stream(media, PCM_FORMAT)

    assert stream is other_stream
