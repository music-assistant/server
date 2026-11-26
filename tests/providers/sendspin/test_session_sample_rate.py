"""Tests for the Sendspin session sample rate following the first track."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from functools import partial
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiosendspin.models import AudioCodec
from aiosendspin.models.core import ClientStatePayload
from aiosendspin.models.player import PlayerStatePayload, SupportedAudioFormat
from aiosendspin.server.audio import AudioFormat as SendspinAudioFormat
from aiosendspin.server.roles.player.v1 import PlayerPersistentState, PlayerV1Role
from music_assistant_models.enums import (
    ContentType,
    CrossfadeMode,
    MediaType,
    VolumeNormalizationMode,
)
from music_assistant_models.errors import QueueEmpty
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.constants import (
    CONF_FLOW_MODE_SAMPLE_RATE,
    FLOW_MODE_SAMPLE_RATE_48000,
    FLOW_MODE_SAMPLE_RATE_96000,
    FLOW_MODE_SAMPLE_RATE_BIT_PERFECT,
    FLOW_MODE_SAMPLE_RATE_HIGHEST,
    FLOW_MODE_SAMPLE_RATE_SMART,
)
from music_assistant.controllers.streams import StreamsController
from music_assistant.controllers.streams.audio import StreamsAudio
from music_assistant.models.player import PlayerMedia
from music_assistant.providers.sendspin.playback import SendspinPlaybackSession
from music_assistant.providers.sendspin.player import (
    CONF_PREFERRED_SENDSPIN_FORMAT,
    SENDSPIN_FORMAT_AUTOMATIC,
    SendspinPlayer,
)
from music_assistant.providers.sendspin.provider import SendspinProvider
from music_assistant.providers.squeezelite.bridge_role import SlimprotoBridgeRole
from tests.common import MockProvider, attach_mock, mock_player_mass
from tests.providers.sendspin.test_short_session_elapsed_anchor import _short_session

_FLOW_MODES = (
    FLOW_MODE_SAMPLE_RATE_SMART,
    FLOW_MODE_SAMPLE_RATE_BIT_PERFECT,
    FLOW_MODE_SAMPLE_RATE_48000,
    FLOW_MODE_SAMPLE_RATE_96000,
    FLOW_MODE_SAMPLE_RATE_HIGHEST,
)
_FLOW_MODE_PAIRS = [(leader, member) for leader in _FLOW_MODES for member in _FLOW_MODES]
_HAND_OVER_MODES = (
    FLOW_MODE_SAMPLE_RATE_BIT_PERFECT,
    FLOW_MODE_SAMPLE_RATE_SMART,
    FLOW_MODE_SAMPLE_RATE_48000,
)
_HAND_OVER_MODE_PAIRS = [(old, new) for old in _HAND_OVER_MODES for new in _HAND_OVER_MODES]

if TYPE_CHECKING:
    from music_assistant.models.player_protocol import PlayerProtocol

PLAYER_ID = "client-1"
MEMBER_ID = "client-2"
FLAC_48K = SupportedAudioFormat(codec=AudioCodec.FLAC, channels=2, sample_rate=48000, bit_depth=16)
FLAC_44K = SupportedAudioFormat(codec=AudioCodec.FLAC, channels=2, sample_rate=44100, bit_depth=16)
FLAC_96K = SupportedAudioFormat(codec=AudioCodec.FLAC, channels=2, sample_rate=96000, bit_depth=24)
OPUS_48K = SupportedAudioFormat(codec=AudioCodec.OPUS, channels=2, sample_rate=48000, bit_depth=16)
# a member that stays in the group when its leader leaves
_OTHER_MEMBER: tuple[list[SupportedAudioFormat], str, SupportedAudioFormat | None] = (
    [FLAC_48K, FLAC_44K],
    SENDSPIN_FORMAT_AUTOMATIC,
    None,
)
# per member, leader first: its formats, its configured format and the format it asks for
_MEMBER_SET_UPS: dict[
    str, list[tuple[list[SupportedAudioFormat], str, SupportedAudioFormat | None]]
] = {
    "48k leader, 44.1/48k member": [
        ([FLAC_48K], SENDSPIN_FORMAT_AUTOMATIC, None),
        ([FLAC_48K, FLAC_44K], SENDSPIN_FORMAT_AUTOMATIC, None),
    ],
    "member asks 48k of 96/48k": [
        ([FLAC_48K], SENDSPIN_FORMAT_AUTOMATIC, None),
        ([FLAC_96K, FLAC_48K], SENDSPIN_FORMAT_AUTOMATIC, FLAC_48K),
    ],
    "member asks 44.1k of 48/44.1k": [
        ([FLAC_48K], SENDSPIN_FORMAT_AUTOMATIC, None),
        ([FLAC_48K, FLAC_44K], SENDSPIN_FORMAT_AUTOMATIC, FLAC_44K),
    ],
    "hi-res member": [
        ([FLAC_48K], SENDSPIN_FORMAT_AUTOMATIC, None),
        ([FLAC_48K, FLAC_96K, FLAC_44K], SENDSPIN_FORMAT_AUTOMATIC, None),
    ],
    "hand-picked 48k member of 96/48k": [
        ([FLAC_48K, FLAC_44K], SENDSPIN_FORMAT_AUTOMATIC, None),
        ([FLAC_96K, FLAC_48K], "flac:48000:16:2", None),
    ],
    "hand-picked 96k leader": [
        ([FLAC_48K, FLAC_96K], "flac:96000:24:2", None),
        ([FLAC_48K, FLAC_44K], SENDSPIN_FORMAT_AUTOMATIC, None),
    ],
    "lossy leader, hi-res member": [
        ([OPUS_48K, FLAC_44K], SENDSPIN_FORMAT_AUTOMATIC, None),
        ([FLAC_96K, FLAC_48K], SENDSPIN_FORMAT_AUTOMATIC, None),
    ],
}


@pytest.mark.parametrize(
    ("blends_items", "mixes_overlay", "bit_depth"),
    [(False, False, 16), (True, False, 24), (False, True, 24)],
)
async def test_session_that_mixes_audio_plays_in_all_the_bits(
    monkeypatch: pytest.MonkeyPatch, blends_items: bool, mixes_overlay: bool, bit_depth: int
) -> None:
    """A session that blends its items or mixes an overlay in has more bits than a 16-bit item."""
    flac_24 = SupportedAudioFormat(
        codec=AudioCodec.FLAC, channels=2, sample_rate=44100, bit_depth=24
    )
    player, _ = _player([flac_24, FLAC_44K])
    session = _session(player, sample_rate=44100)
    assert session._item_streamdetails is not None
    # nothing else works on the audio of the item
    session._item_streamdetails.volume_normalization_mode = VolumeNormalizationMode.DISABLED
    monkeypatch.setattr(
        player.mass.streams.audio,
        "_resolve_player_dsp_config",
        lambda _player: MagicMock(enabled=False),
    )
    cast("MagicMock", player.mass).streams.flow_stream_blends_items.return_value = blends_items
    monkeypatch.setattr(
        "music_assistant.providers.sendspin.playback.overlay_mixed_in",
        lambda *_args, **_kwargs: mixes_overlay,
    )
    media = PlayerMedia(uri="track-1", source_id="queue-1", queue_item_id="item-1")

    session._start_mixes = session._get_start_mixes(media)
    await session._follow_session_sample_rate([PLAYER_ID])

    assert player.session_bit_depth == bit_depth


async def test_session_follows_first_track_rate() -> None:
    """A 44.1 kHz first track plays at 44.1 kHz in the client's own top codec."""
    player, role = _player([FLAC_48K, FLAC_44K])
    session = _session(player, sample_rate=44100)

    await session._follow_session_sample_rate([PLAYER_ID])
    pcm_format, _ = session._select_session_pcm_formats()

    assert pcm_format.sample_rate == 44100
    assert role.preferred_codec == AudioCodec.FLAC
    assert role.preferred_format == SendspinAudioFormat(44100, 16, 2)
    # a config update during the session keeps the steered format
    await player._apply_preferred_format()
    assert role.preferred_format == SendspinAudioFormat(44100, 16, 2)


async def test_explicit_format_is_not_overridden() -> None:
    """An explicitly configured Sendspin format wins over the session sample rate."""
    player, role = _player([FLAC_44K, FLAC_48K], preferred_format="flac:48000:16:2")
    session = _session(player, sample_rate=44100)

    await session._follow_session_sample_rate([PLAYER_ID])
    pcm_format, _ = session._select_session_pcm_formats()

    assert pcm_format.sample_rate == 48000
    assert role.preferred_format == SendspinAudioFormat(48000, 16, 2)


async def test_rate_missing_in_top_codec_keeps_client_default() -> None:
    """Without the session rate in its top codec, the client keeps its own default format."""
    player, role = _player([OPUS_48K, FLAC_44K])
    session = _session(player, sample_rate=44100)

    await session._follow_session_sample_rate([PLAYER_ID])
    pcm_format, _ = session._select_session_pcm_formats()

    assert pcm_format.sample_rate == 48000
    assert role.preferred_codec == AudioCodec.OPUS
    assert role._state().preferred_format_override is None


def test_supported_rates_follow_the_codec_in_use() -> None:
    """Only rates of the client's top codec, or of an explicit format, are reported."""
    automatic, _ = _player([OPUS_48K, FLAC_44K])
    explicit, _ = _player([OPUS_48K, FLAC_44K], preferred_format="flac:44100:16:2")

    assert automatic.supported_sample_rates == [(48000, 16)]
    assert explicit.supported_sample_rates == [(44100, 16)]


async def test_session_without_queue_item_returns_to_client_default() -> None:
    """A session without a first queue item (e.g. an audio source) uses the client's default."""
    player, role = _player([FLAC_48K, FLAC_44K])
    session = _session(player, sample_rate=44100)
    await session._follow_session_sample_rate([PLAYER_ID])

    session._item_streamdetails = None
    await session._follow_session_sample_rate([PLAYER_ID])
    pcm_format, _ = session._select_session_pcm_formats()

    assert pcm_format.sample_rate == 48000
    assert role._state().preferred_format_override is None


async def test_unlisted_explicit_format_reports_client_rates() -> None:
    """An explicit format the client no longer lists does not limit the supported rates."""
    player, _ = _player([FLAC_48K, FLAC_44K], preferred_format="flac:96000:24:2")

    assert player.supported_sample_rates == [(44100, 16), (48000, 16)]


async def test_late_joining_member_follows_session_rate() -> None:
    """A member joining a running session is steered to the session's first track rate."""
    leader, _ = _player([FLAC_48K, FLAC_44K])
    member, member_role = _player([FLAC_48K, FLAC_44K], player_id=MEMBER_ID)
    session = _session(leader, sample_rate=44100)
    players = {PLAYER_ID: leader, MEMBER_ID: member}
    cast("MagicMock", leader.mass).players.get_player.side_effect = players.get

    await session.add_member(MEMBER_ID)

    assert member_role.preferred_format == SendspinAudioFormat(44100, 16, 2)


@pytest.mark.parametrize(
    ("flow_mode", "expected_rate"),
    [(FLOW_MODE_SAMPLE_RATE_SMART, 48000), (FLOW_MODE_SAMPLE_RATE_BIT_PERFECT, 44100)],
)
async def test_only_bit_perfect_switches_down_to_a_later_lower_rate_item(
    flow_mode: str, expected_rate: int
) -> None:
    """Smart keeps a session's higher rate for a lower-rate later item, bit-perfect follows it."""
    player, role = _player([FLAC_48K, FLAC_44K], flow_mode=flow_mode)
    session = await _start_session(player, 48000)

    session._item_streamdetails = _flow_item("later", 44100).streamdetails
    sample_rate = await session._later_item_sample_rate(session._item_streamdetails)
    await session._follow_session_sample_rate([PLAYER_ID], later_item=True)

    assert sample_rate == expected_rate
    assert role.preferred_format == SendspinAudioFormat(expected_rate, 16, 2)


async def test_a_bridge_keeps_the_session_format_for_a_later_item() -> None:
    """A bridge, which restarts its stream to change formats, keeps it for a later item."""
    player, _ = _player([FLAC_48K, FLAC_44K])
    bridge_role = SlimprotoBridgeRole(client=player.api)
    set_preferred_format = bridge_role.set_preferred_format = MagicMock()  # type: ignore[method-assign]
    cast("MagicMock", player.api).roles_by_family.return_value = [bridge_role]

    await player.follow_session_sample_rate(
        _flow_item("later", 48000).streamdetails, later_item=True
    )

    set_preferred_format.assert_not_called()


async def test_group_session_takes_the_source_rate_a_member_plays() -> None:
    """A 96 kHz track in a group led by a 48 kHz player runs at 96 kHz for its 96 kHz member."""
    leader, leader_role = _player([FLAC_48K])
    member, member_role = _player([FLAC_48K, FLAC_96K], player_id=MEMBER_ID)
    _group(leader, member)
    session = _session(leader, sample_rate=96000)

    await session._follow_session_sample_rate([PLAYER_ID, MEMBER_ID])
    pcm_format, sendspin_format = session._select_session_pcm_formats()

    assert pcm_format.sample_rate == 96000
    assert sendspin_format.sample_rate == 96000
    assert member_role.preferred_format == SendspinAudioFormat(96000, 24, 2)
    assert leader_role.preferred_format == SendspinAudioFormat(48000, 16, 2)
    assert session._get_member_sample_rate(MEMBER_ID) == 96000
    assert session._get_member_sample_rate(PLAYER_ID) == 48000


async def test_group_session_follows_a_44k_track_a_member_plays() -> None:
    """A 44.1 kHz track in a group led by a 48 kHz player runs at 44.1 kHz for its 44.1 kHz member."""
    leader, _ = _player([FLAC_48K])
    member, member_role = _player([FLAC_48K, FLAC_44K], player_id=MEMBER_ID)
    _group(leader, member)
    session = _session(leader, sample_rate=44100)

    await session._follow_session_sample_rate([PLAYER_ID, MEMBER_ID])
    pcm_format, _ = session._select_session_pcm_formats()

    assert pcm_format.sample_rate == 44100
    assert member_role.preferred_format == SendspinAudioFormat(44100, 16, 2)


async def test_group_of_48k_members_stays_at_48k() -> None:
    """A group whose members only play 48 kHz runs at 48 kHz for 44.1 and 96 kHz tracks."""
    for track_rate in (44100, 96000):
        leader, _ = _player([FLAC_48K])
        member, _ = _player([OPUS_48K], player_id=MEMBER_ID)
        _group(leader, member)
        session = _session(leader, sample_rate=track_rate)

        await session._follow_session_sample_rate([PLAYER_ID, MEMBER_ID])
        pcm_format, _ = session._select_session_pcm_formats()

        assert pcm_format.sample_rate == 48000


async def test_group_session_does_not_go_above_the_track_rate() -> None:
    """A member with a configured 96 kHz format does not lift a 44.1 kHz track above 48 kHz."""
    leader, _ = _player([FLAC_48K])
    member, member_role = _player(
        [FLAC_48K, FLAC_96K], player_id=MEMBER_ID, preferred_format="flac:96000:24:2"
    )
    _group(leader, member)
    session = _session(leader, sample_rate=44100)

    await session._follow_session_sample_rate([PLAYER_ID, MEMBER_ID])
    pcm_format, _ = session._select_session_pcm_formats()

    assert member_role.preferred_format == SendspinAudioFormat(96000, 24, 2)
    assert pcm_format.sample_rate == 48000


async def test_lossy_leader_does_not_cap_the_group() -> None:
    """A leader that plays a lossy codec does not hold a 96 kHz member at 48 kHz."""
    leader, leader_role = _player([OPUS_48K])
    member, _ = _player([FLAC_48K, FLAC_96K], player_id=MEMBER_ID)
    _group(leader, member)
    session = _session(leader, sample_rate=96000)

    await session._follow_session_sample_rate([PLAYER_ID, MEMBER_ID])
    pcm_format, _ = session._select_session_pcm_formats()

    assert leader_role.preferred_codec == AudioCodec.OPUS
    assert pcm_format.sample_rate == 96000


async def test_lossy_member_counts_with_at_most_48k(monkeypatch: pytest.MonkeyPatch) -> None:
    """A member with a lossy codec counts with 48 kHz, whatever rate it plays."""
    leader, _ = _player([FLAC_48K])
    member, _ = _player([FLAC_48K], player_id=MEMBER_ID)
    _group(leader, member)
    session = _session(leader, sample_rate=96000)
    formats = {
        PLAYER_ID: AudioFormat(content_type=ContentType.PCM_S16LE, sample_rate=44100),
        MEMBER_ID: AudioFormat(content_type=ContentType.OPUS, sample_rate=96000),
    }
    monkeypatch.setattr(session, "_get_member_preferred_format", formats.get)

    pcm_format, _ = session._select_session_pcm_formats()

    assert pcm_format.sample_rate == 48000


async def test_hand_picked_leader_format_does_not_cap_the_group() -> None:
    """A leader with a configured 48 kHz format keeps it, while the group runs at 96 kHz."""
    leader, leader_role = _player([FLAC_48K, FLAC_96K], preferred_format="flac:48000:16:2")
    member, _ = _player([FLAC_48K, FLAC_96K], player_id=MEMBER_ID)
    _group(leader, member)
    session = _session(leader, sample_rate=96000)

    await session._follow_session_sample_rate([PLAYER_ID, MEMBER_ID])
    pcm_format, _ = session._select_session_pcm_formats()

    assert leader_role.preferred_format == SendspinAudioFormat(48000, 16, 2)
    assert pcm_format.sample_rate == 96000


async def test_group_session_without_first_item_keeps_leader_rate() -> None:
    """Without a first item to follow, the group runs at the leader's rate."""
    leader, _ = _player([FLAC_96K, FLAC_48K])
    member, _ = _player([FLAC_48K], player_id=MEMBER_ID)
    _group(leader, member)
    session = _session(leader, sample_rate=44100)
    session._item_streamdetails = None

    await session._follow_session_sample_rate([PLAYER_ID, MEMBER_ID])
    pcm_format, _ = session._select_session_pcm_formats()

    assert pcm_format.sample_rate == 96000


async def test_group_client_without_player_role_does_not_count() -> None:
    """A group client that plays no audio leaves the session rate to the members that do."""
    leader, _ = _player([FLAC_48K])
    controller, _ = _player([], player_id=MEMBER_ID)
    cast("MagicMock", controller.api).roles_by_family.return_value = []
    _group(leader, controller)
    session = _session(leader, sample_rate=96000)
    session._pcm_format = AudioFormat(content_type=ContentType.PCM_F32LE, sample_rate=96000)

    await session._follow_session_sample_rate([PLAYER_ID])
    pcm_format, _ = session._select_session_pcm_formats()

    assert pcm_format.sample_rate == 48000


async def test_a_client_that_renders_nothing_does_not_count() -> None:
    """The silent client of a virtual or multi-channel player leaves the rate to its members."""
    leader, _ = _player([FLAC_96K], player_id=PLAYER_ID)
    member, _ = _player([FLAC_48K], player_id=MEMBER_ID)
    _group(leader, member)
    cast("MagicMock", leader.provider).is_silent_client.side_effect = lambda client_id: (
        client_id == PLAYER_ID
    )
    session = _session(leader, sample_rate=96000)

    pcm_format, _ = session._select_session_pcm_formats()

    assert pcm_format.sample_rate == 48000
    assert await session.get_session_sample_rate(session._item_streamdetails) == 48000  # type: ignore[arg-type]


async def test_consecutive_44k_tracks_flow_without_restart() -> None:
    """A group of a 48 kHz leader and a 44.1 kHz member flows from one 44.1 kHz track to the next."""
    leader, _ = _player([FLAC_48K])
    member, _ = _player([FLAC_48K, FLAC_44K], player_id=MEMBER_ID)
    _group(leader, member)

    assert await _flow_items_played(leader, [44100, 44100, 44100]) == ["0", "1", "2"]


async def test_higher_track_rate_a_member_plays_restarts_the_flow() -> None:
    """A 96 kHz track restarts the flow when a member plays 96 kHz."""
    leader, _ = _player([FLAC_48K])
    member, _ = _player([FLAC_48K, FLAC_44K], player_id=MEMBER_ID)
    hires_member, _ = _player([FLAC_48K, FLAC_96K], player_id="client-3")
    _group(leader, member, hires_member)

    assert await _flow_items_played(leader, [44100, 44100, 96000]) == ["0", "1"]
    assert await _flow_items_played(leader, [48000, 48000, 96000]) == ["0", "1"]


async def test_higher_track_rate_no_member_plays_keeps_the_flow() -> None:
    """A 96 kHz track keeps a 48 kHz flow going when the members play 48 and 44.1 kHz."""
    leader, _ = _player([FLAC_48K])
    member, _ = _player([FLAC_48K, FLAC_44K], player_id=MEMBER_ID)
    _group(leader, member)

    assert await _flow_items_played(leader, [48000, 96000, 44100]) == ["0", "1", "2"]


async def test_96k_track_lifts_a_44k_flow_to_48k_as_for_a_lone_player() -> None:
    """A 96 kHz track restarts a 44.1 kHz flow at 48 kHz, as for a lone 44.1/48 kHz player."""
    leader, _ = _player([FLAC_48K])
    member, _ = _player([FLAC_48K, FLAC_44K], player_id=MEMBER_ID)
    _group(leader, member)
    lone_player, _ = _player([FLAC_48K, FLAC_44K], player_id="client-3")

    assert await _flow_items_played(leader, [44100, 96000]) == ["0"]
    assert await _flow_items_played(lone_player, [44100, 96000]) == ["0"]
    assert (await _start_session(leader, 96000))._pcm_format.sample_rate == 48000
    assert (await _start_session(lone_player, 96000))._pcm_format.sample_rate == 48000


async def test_predicted_rate_is_the_rate_a_session_start_picks() -> None:
    """For every member set-up and rate setting, the predicted rate is what a session start picks."""
    for name, members in _MEMBER_SET_UPS.items():
        for leader_mode, member_mode in _FLOW_MODE_PAIRS:
            modes = [leader_mode] + [member_mode] * (len(members) - 1)
            leader = _group_of(members, modes)[0]
            for previous_rate in (44100, 96000):
                for item_rate in (32000, 44100, 48000, 88200, 96000, 192000):
                    running = await _start_session(leader, previous_rate)
                    current = running._pcm_format.sample_rate
                    item = _flow_item("next", item_rate)

                    predicted = await running.get_session_sample_rate(item.streamdetails)
                    restarts = await _flow_restart(running)(item.streamdetails)
                    started = (await _start_session(leader, item_rate))._pcm_format.sample_rate

                    case = (name, leader_mode, member_mode, previous_rate, item_rate)
                    assert predicted == started, case
                    assert restarts == _restart_due(started, current, leader_mode), case


async def test_restart_decision_follows_the_group_after_a_hand_over() -> None:
    """After a hand-over, a flow restarts as the group that plays on would decide."""
    for name, members in _MEMBER_SET_UPS.items():
        for old_mode, new_mode in _HAND_OVER_MODE_PAIRS:
            for previous_rate in (44100, 96000):
                old_leader, new_leader, other = _group_of(
                    [*members, _OTHER_MEMBER], [old_mode, new_mode, FLOW_MODE_SAMPLE_RATE_SMART]
                )
                running = await _start_session(old_leader, previous_rate)
                current = running._pcm_format.sample_rate
                flow_restart = _flow_restart(running)
                await _hand_over(running, old_leader, new_leader, other)
                for item_rate in (32000, 44100, 48000, 88200, 96000, 192000):
                    item = _flow_item("next", item_rate)

                    restarts = await flow_restart(item.streamdetails)
                    started = (await _start_session(new_leader, item_rate))._pcm_format

                    case = (name, old_mode, new_mode, previous_rate, item_rate)
                    assert restarts == _restart_due(started.sample_rate, current, new_mode), case


async def test_flow_restarts_by_the_mode_of_the_leader_that_plays_on() -> None:
    """A 48 kHz flow handed from a bit-perfect leader to a smart one plays on into 44.1 kHz."""
    leader, _ = _player([FLAC_48K], flow_mode=FLOW_MODE_SAMPLE_RATE_BIT_PERFECT)
    member, _ = _player([FLAC_48K, FLAC_44K], player_id=MEMBER_ID)
    _group(leader, member)
    played_with_bit_perfect_leader = await _flow_items_played(leader, [48000, 44100])
    session = await _start_session(leader, 48000)
    flow_restart = _flow_restart(session)
    await _hand_over(session, leader, member)

    played = await _flow_items_played(
        leader, [48000, 44100], flow_rate=48000, flow_restart=flow_restart
    )

    assert played_with_bit_perfect_leader == ["0"]
    assert played == ["0", "1"]


async def test_fixed_rate_leader_restarts_for_a_rate_a_member_plays() -> None:
    """A leader fixed at 48 kHz restarts a 48 kHz flow for a 96 kHz track a member plays."""
    leader, _ = _player([FLAC_96K, FLAC_48K], flow_mode=FLOW_MODE_SAMPLE_RATE_48000)
    member, _ = _player([FLAC_48K, FLAC_96K], player_id=MEMBER_ID)
    _group(leader, member)

    assert await _flow_items_played(leader, [48000, 96000]) == ["0"]
    assert (await _start_session(leader, 96000))._pcm_format.sample_rate == 96000
    assert await _flow_items_played(leader, [48000, 48000, 44100]) == ["0", "1", "2"]


async def test_session_follows_the_rate_the_audio_arrives_in() -> None:
    """A 96 kHz track that arrives decoded at 48 kHz runs at 48 kHz, and is predicted so."""
    leader, _ = _player([FLAC_48K])
    member, member_role = _player([FLAC_48K, FLAC_96K], player_id=MEMBER_ID)
    _group(leader, member)
    session = await _start_session(leader, 44100)
    item = _flow_item("next", 96000)
    item.streamdetails.decoded_audio_format = AudioFormat(
        content_type=ContentType.PCM_S16LE, sample_rate=48000, bit_depth=16
    )

    predicted = await session.get_session_sample_rate(item.streamdetails)
    session._item_streamdetails = item.streamdetails
    await session._follow_session_sample_rate([PLAYER_ID, MEMBER_ID])
    pcm_format, _ = session._select_session_pcm_formats()

    assert member_role.preferred_format == SendspinAudioFormat(96000, 24, 2)
    assert pcm_format.sample_rate == 48000
    assert predicted == 48000


async def test_playback_hands_its_flow_its_restart_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A playback hands its flow stream its restart decision, which is no once it ended."""
    session = _short_session(monkeypatch)
    session.playback_task = None

    await session._run_playback(MagicMock())

    get_stream = cast("MagicMock", session.player.mass.streams.get_stream)
    flow_restart = get_stream.call_args.kwargs["flow_restart"]
    assert await flow_restart(_flow_item("next", 96000).streamdetails) is False


def test_raw_stream_hands_its_flow_what_the_consumer_asks() -> None:
    """The raw PCM stream of a queue hands its flow what the consumer asks for a next item."""
    controller: Any = StreamsController.__new__(StreamsController)
    controller.mass = MagicMock()
    source_protocol = controller.mass.players.get_player.return_value.own_protocol
    source_protocol.get_shared_stream.return_value = None
    controller.audio = MagicMock()
    controller._get_live_audio_source_session = MagicMock(return_value=None)
    controller._get_flow_start_item = MagicMock()
    controller._update_audio_processing_context = MagicMock()
    controller._count_as_output_stream = lambda stream: stream
    flow_restart = AsyncMock(return_value=False)
    media = PlayerMedia(uri="track-1", source_id="queue-1", queue_item_id="item-1")

    controller.get_stream(
        media,
        AudioFormat(content_type=ContentType.PCM_F32LE, sample_rate=48000),
        player_id=PLAYER_ID,
        force_flow_mode=True,
        flow_restart=flow_restart,
    )

    flow_kwargs = controller.audio.get_queue_flow_stream.call_args.kwargs
    assert flow_kwargs["flow_restart"] is flow_restart


async def test_flow_of_an_ended_session_does_not_restart() -> None:
    """A flow whose session has ended plays on at its rate instead of restarting."""
    leader, _ = _player([FLAC_48K])
    member, _ = _player([FLAC_48K, FLAC_44K, FLAC_96K], player_id=MEMBER_ID)
    _group(leader, member)
    session = await _start_session(leader, 44100)
    flow_restart = _flow_restart(session)
    assert await flow_restart(_flow_item("next", 96000).streamdetails) is True

    session.playback_task = None
    items = await _flow_items_played(
        leader, [44100, 96000, 96000], flow_rate=44100, flow_restart=flow_restart
    )

    assert items == ["0", "1", "2"]


def test_player_role_keeps_the_requested_format() -> None:
    """The player role of the library still keeps the format a client asks for where it is read."""
    _, role = _player([FLAC_96K, FLAC_48K])
    role.on_initial_client_state(ClientStatePayload(player=PlayerStatePayload(format=FLAC_48K)))

    assert role._client_format == FLAC_48K


async def test_unreadable_requested_format_falls_back_to_first_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the format a client asks for, its first choice counts, with one warning."""
    monkeypatch.setattr(SendspinPlayer, "_requested_format_unreadable", False)
    player, role = _player([FLAC_96K, FLAC_48K])
    del role._client_format

    assert player._default_format(role) == FLAC_96K
    assert player._default_format(role) == FLAC_96K
    cast("MagicMock", player.logger).warning.assert_called_once()


async def test_member_asking_48k_keeps_the_flow_at_96k_tracks() -> None:
    """A member that asks for 48 kHz of its 96/48 kHz formats does not restart 96 kHz tracks."""
    leader, _ = _player([FLAC_48K])
    member, member_role = _player([FLAC_96K, FLAC_48K], player_id=MEMBER_ID)
    member_role.on_initial_client_state(
        ClientStatePayload(player=PlayerStatePayload(format=FLAC_48K))
    )
    _group(leader, member)

    assert await _flow_items_played(leader, [48000, 96000, 96000]) == ["0", "1", "2"]
    assert await _flow_items_played(leader, [96000, 96000]) == ["0", "1"]


async def test_flow_follows_the_session_handed_to_a_new_leader() -> None:
    """After its leader leaves, a flow restarts by the rates of the group that plays on."""
    leader, _ = _player([FLAC_48K])
    member, _ = _player([FLAC_48K, FLAC_44K], player_id=MEMBER_ID)
    _group(leader, member)
    session = await _start_session(leader, 44100)
    flow_restart = _flow_restart(session)
    await _hand_over(session, leader, member)

    played = await _flow_items_played(
        leader, [44100, 44100, 44100], flow_rate=44100, flow_restart=flow_restart
    )
    # the departed leader plays something on its own while the group plays on
    solo = await _start_session(leader, 48000)
    _flow_restart(solo)
    played_after_solo = await _flow_items_played(
        leader, [44100, 44100, 44100], flow_rate=44100, flow_restart=flow_restart
    )

    assert played == ["0", "1", "2"]
    assert played_after_solo == ["0", "1", "2"]


async def test_group_of_48k_members_restarts_as_its_leader_alone() -> None:
    """A group of 48 kHz members takes the same restart decisions as its leader's rates."""
    leader, _ = _player([FLAC_48K])
    member, _ = _player([OPUS_48K], player_id=MEMBER_ID)
    _group(leader, member)
    session = _session(leader, sample_rate=44100)
    await session._follow_session_sample_rate([PLAYER_ID, MEMBER_ID])
    pcm_format, _ = session._select_session_pcm_formats()
    flow_restart = _flow_restart(session)
    audio = leader.mass.streams.audio
    supported = sorted({rate for rate, _ in leader.get_supported_sample_rates()})

    for next_rate in (22050, 44100, 48000, 88200, 96000, 192000):
        item = _flow_item("next", next_rate)
        decisions = [
            await flow_restart(item.streamdetails),
            audio._flow_stream_needs_restart(
                cast("Any", item),
                pcm_format,
                supported,
                FLOW_MODE_SAMPLE_RATE_SMART,
                is_first_track=False,
            ),
        ]
        assert decisions == [False, False], next_rate


async def test_flow_of_other_players_follows_their_supported_rates() -> None:
    """A player outside a Sendspin group restarts its flow by its own supported rates."""
    mass = mock_player_mass()
    _, protocol = attach_mock(MockProvider("test", mass=mass), "renderer-1")
    protocol._attr_supported_sample_rates = [(48000, 16), (96000, 24)]

    assert await _flow_items_played(protocol, [44100, 48000, 44100], 48000) == ["0", "1", "2"]
    assert await _flow_items_played(protocol, [44100, 48000, 96000], 48000) == ["0", "1"]


def _player(
    formats: list[SupportedAudioFormat],
    *,
    player_id: str = PLAYER_ID,
    preferred_format: str = SENDSPIN_FORMAT_AUTOMATIC,
    flow_mode: str = FLOW_MODE_SAMPLE_RATE_SMART,
) -> tuple[SendspinPlayer, PlayerV1Role]:
    """Build an un-initialized SendspinPlayer around a real player role."""
    client = MagicMock(client_id=player_id)
    client.info.player_support.supported_formats = formats
    client.group.has_active_stream = False
    client.get_or_create_role_state.return_value = PlayerPersistentState()
    role = PlayerV1Role(client)
    role._ensure_preferred_format()
    client.roles_by_family.return_value = [role]
    client.group.clients = [client]

    audio = StreamsAudio(MagicMock())
    audio.mass.config.get_player_dsp_config = MagicMock(  # type: ignore[method-assign]
        return_value=MagicMock(enabled=False)
    )
    config_values = {
        CONF_PREFERRED_SENDSPIN_FORMAT: preferred_format,
        CONF_FLOW_MODE_SAMPLE_RATE: flow_mode,
    }
    player = SendspinPlayer.__new__(SendspinPlayer)
    player._protocol_id = player_id
    player._player = None
    player.api = client
    player.logger = MagicMock()
    player._config = MagicMock()
    player._config.get_value.side_effect = lambda key, default=None: config_values.get(key, default)
    player._provider = MagicMock()
    player._provider.server_api.get_client.return_value = client
    player._provider.is_silent_client.return_value = False
    player._provider.multi_channel_player_of.return_value = None
    player._provider.get_client_session_format = partial(
        SendspinProvider.get_client_session_format, player._provider
    )
    player._provider.get_protocol = lambda pid: player.mass.players.get_player(pid)
    player.mass = MagicMock()
    player.mass.streams.audio = audio
    player.mass.players.get_player.return_value = player
    return player, role


def _group_of(
    members: list[tuple[list[SupportedAudioFormat], str, SupportedAudioFormat | None]],
    modes: list[str],
) -> list[SendspinPlayer]:
    """
    Return the players of a new group set up as described, leader first.

    :param members: Per player its formats, its configured format and the format it asks for.
    :param modes: Per player its sample rate setting.
    """
    players = []
    for index, ((formats, preferred_format, requested), mode) in enumerate(
        zip(members, modes, strict=True)
    ):
        player, role = _player(
            formats,
            player_id=f"client-{index}",
            preferred_format=preferred_format,
            flow_mode=mode,
        )
        if requested is not None:
            role.on_initial_client_state(
                ClientStatePayload(player=PlayerStatePayload(format=requested))
            )
        players.append(player)
    _group(*players)
    return players


def _group(*players: SendspinPlayer) -> None:
    """Put the given players in one Sendspin group, led by the first."""
    clients = {player.protocol_id: player.api for player in players}
    players_by_id = {player.protocol_id: player for player in players}
    for player in players:
        cast("MagicMock", player.api).group.clients = list(clients.values())
        cast("MagicMock", player.provider).server_api.get_client.side_effect = clients.get
        cast("MagicMock", player.mass).players.get_player.side_effect = players_by_id.get


async def _flow_items_played(
    protocol: PlayerProtocol,
    item_rates: list[int],
    flow_rate: int | None = None,
    flow_restart: Callable[[StreamDetails], Awaitable[bool]] | None = None,
) -> list[str]:
    """
    Return the items a flow stream for a protocol plays before it ends or restarts.

    :param protocol: The protocol that consumes the flow stream.
    :param item_rates: The sample rates of the queue items, from the first on.
    :param flow_rate: The rate of the flow stream; by default the rate of the Sendspin
        session of the protocol for the first item.
    :param flow_restart: What decides whether the flow restarts for a next item; by default
        that of the Sendspin session started for the first item.
    """
    items = [_flow_item(str(index), rate) for index, rate in enumerate(item_rates)]
    if flow_rate is None:
        session = await _start_session(cast("SendspinPlayer", protocol), item_rates[0])
        flow_rate = session._pcm_format.sample_rate
        flow_restart = _flow_restart(session)

    async def _load_next(_queue_id: str, current_id: str) -> Any:
        if (index := int(current_id) + 1) < len(items):
            return items[index]
        raise QueueEmpty

    async def _one_chunk(*_args: object, **_kwargs: object) -> AsyncGenerator[bytes]:
        yield b"\x00" * 8

    audio = StreamsAudio(MagicMock())
    queue_data = SimpleNamespace(session_id="session-1", flow_mode_stream_log=[])
    mass = cast("MagicMock", audio.mass)
    mass.player_queues.queue_data.return_value = queue_data
    mass.player_queues.load_next_queue_item = _load_next
    mass.player_queues.get_active_queue.return_value = None
    mass.streams.get_crossfade_mode.return_value = CrossfadeMode.DISABLED
    mass.config.get_raw_core_config_value.return_value = 0
    audio.get_queue_item_stream = _one_chunk  # type: ignore[method-assign]
    queue = SimpleNamespace(
        queue_id="queue-1",
        display_name="Queue",
        flow_mode=False,
        overlay_enabled=False,
        overlay_source=None,
    )
    pcm_format = AudioFormat(content_type=ContentType.PCM_F32LE, sample_rate=flow_rate)
    async for _chunk in audio.get_queue_flow_stream(
        cast("Any", queue),
        cast("Any", items[0]),
        pcm_format,
        session_id="session-1",
        protocol_player=protocol,
        flow_restart=flow_restart,
    ):
        pass
    return [entry.queue_item_id for entry in queue_data.flow_mode_stream_log]


async def _start_session(leader: SendspinPlayer, sample_rate: int) -> SendspinPlaybackSession:
    """
    Start the Sendspin session of a leader for a first item of a sample rate, as playback does.

    :param leader: The player that leads the group.
    :param sample_rate: The sample rate of the first item.
    """
    session = SendspinPlaybackSession(leader)
    session._item_streamdetails = _flow_item("first", sample_rate).streamdetails
    await session._follow_session_sample_rate(
        [client.client_id for client in leader.api.group.clients]
    )
    session._pcm_format, session._sendspin_pcm_format = session._select_session_pcm_formats()
    leader.playback_session = session
    return session


def _flow_restart(
    session: SendspinPlaybackSession,
) -> Callable[[StreamDetails], Awaitable[bool]]:
    """
    Return what the flow stream of a session asks, with the session playing in this task.

    :param session: The session that opens the flow stream.
    """
    session.playback_task = cast("asyncio.Task[None]", asyncio.current_task())
    return session._flow_restart_of_this_playback()


async def _hand_over(
    session: SendspinPlaybackSession,
    old_leader: SendspinPlayer,
    new_leader: SendspinPlayer,
    *others: SendspinPlayer,
) -> None:
    """
    Hand a session over as a leader leaving its group does.

    :param session: The session of the group.
    :param old_leader: The leader that leaves the group.
    :param new_leader: The member that leads the group from now on.
    :param others: The other members that stay in the group.
    """
    _group(old_leader)
    _group(new_leader, *others)
    await session.transfer_to(new_leader)
    new_leader.playback_session = session
    old_leader.playback_session = SendspinPlaybackSession(old_leader)


def _restart_due(started_rate: int, stream_rate: int, leader_mode: str) -> bool:
    """
    Return whether a flow is due to restart when a new session would start at a rate.

    :param started_rate: The rate a session starting with the next item runs at.
    :param stream_rate: The rate the flow runs at.
    :param leader_mode: The sample rate setting of the leader of the group.
    """
    if leader_mode == FLOW_MODE_SAMPLE_RATE_BIT_PERFECT:
        return started_rate != stream_rate
    return started_rate > stream_rate


def _flow_item(item_id: str, sample_rate: int) -> SimpleNamespace:
    """Return a queue item of a track with the given sample rate."""
    return SimpleNamespace(
        queue_item_id=item_id,
        name=item_id,
        media_type=MediaType.TRACK,
        duration=300,
        extra_attributes={},
        streamdetails=StreamDetails(
            provider="test",
            item_id=item_id,
            audio_format=AudioFormat(
                content_type=ContentType.FLAC, sample_rate=sample_rate, bit_depth=16
            ),
            media_type=MediaType.TRACK,
            duration=300,
        ),
    )


def _session(player: SendspinPlayer, *, sample_rate: int) -> SendspinPlaybackSession:
    """Return a playback session whose first queue item has the given sample rate."""
    streamdetails = StreamDetails(
        provider="test",
        item_id="1",
        audio_format=AudioFormat(
            content_type=ContentType.FLAC, sample_rate=sample_rate, bit_depth=16
        ),
        media_type=MediaType.TRACK,
    )
    cast("MagicMock", player.mass).player_queues.get_item.return_value = MagicMock(
        streamdetails=streamdetails
    )
    session = SendspinPlaybackSession(player)
    media = PlayerMedia(uri="track-1", source_id="queue-1", queue_item_id="item-1")
    session._item_streamdetails = session._get_item_streamdetails(media)
    return session
