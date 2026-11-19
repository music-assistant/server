"""Tests for the home theater set of an Apple TV, which plays on its speakers."""

from __future__ import annotations

import socket
from typing import NamedTuple, cast
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from music_assistant_models.enums import ContentType, PlaybackState, PlayerFeature
from music_assistant_models.errors import PlayerCommandFailed
from music_assistant_models.media_items import AudioFormat
from zeroconf.asyncio import AsyncServiceInfo

from music_assistant.constants import CONF_PASSWORD, CONF_SYNC_ADJUST
from music_assistant.providers.airplay.constants import (
    AIRPLAY_DISCOVERY_TYPE,
    AIRPLAY_PCM_FORMAT,
    CONF_AIRPLAY_CREDENTIALS,
    CONF_BUFFER_DEPTH,
    CONF_ENABLE_HIRES,
    CONF_HOMEPODS_ONLY,
    CONF_IGNORE_VOLUME,
    CONF_PASSWORD_INVALID,
    CONF_STORED_VOLUME,
    CONF_STREAMING_MODE,
    STREAMING_MODE_AP2_COMPAT,
)
from music_assistant.providers.airplay.player import AirPlayPlayer
from music_assistant.providers.airplay.speaker import AirPlaySpeaker

SET_ID = "3F2A9C1E-7B44-4D0E-9A61-2C8D5E7F1A30"
AP2_FEATURES = "0x4A7FDFD5,0x3C177FDE"


class HomeTheater(NamedTuple):
    """An Apple TV that carries a set, its speaker and the provider that knows them."""

    apple_tv: AirPlayPlayer
    speaker: AirPlaySpeaker
    provider: MagicMock


def _record(position: int | None, device_id: str, flags: str) -> AsyncServiceInfo:
    """
    Return the _airplay._tcp record of a device, in the set unless position is None.

    :param position: Place in the set, 0 for the Apple TV.
    :param device_id: The device id the device advertises.
    :param flags: The status flags the device advertises.
    """
    properties = {"deviceid": device_id, "features": AP2_FEATURES, "flags": flags}
    if position is not None:
        properties |= {
            "psgid": SET_ID,
            "psgsz": "2",
            "psgtp": "1",
            "pgmid": str(position),
            "gpn": "Study",
        }
    return AsyncServiceInfo(
        AIRPLAY_DISCOVERY_TYPE,
        f"{device_id}.{AIRPLAY_DISCOVERY_TYPE}",
        addresses=[socket.inet_aton("192.168.1.60")],
        port=7000,
        properties=properties,
    )


def _config(values: dict[str, object], setup_data: dict[str, object] | None = None) -> MagicMock:
    """Return a protocol config that serves the given values."""
    config = MagicMock(setup_data=setup_data or {})
    config.get_value.side_effect = lambda key, default=None: values.get(key, default)
    return config


def _home_theater(
    *,
    apple_tv_position: int | None = 0,
    speaker_flags: str = "0xa0c04",
    homepods_only: bool = False,
) -> HomeTheater:
    """
    Build an Apple TV and the HomePod of its set on one mock provider.

    :param apple_tv_position: Place of the Apple TV in the set, None for a plain Apple TV.
    :param speaker_flags: The status flags the HomePod advertises.
    :param homepods_only: The stored value of the option to play on the HomePods only.
    """
    provider = MagicMock()
    stored: dict[tuple[str, str], object] = {}
    if homepods_only:
        stored["apaabbcc001101", CONF_HOMEPODS_ONLY] = True
    provider.mass.config.get_protocol_value.side_effect = lambda protocol_id, key, default=None: (
        stored.get((protocol_id, key), default)
    )
    provider.mass.config.set_protocol_value.side_effect = lambda protocol_id, key, value: (
        stored.__setitem__((protocol_id, key), value)
    )
    provider.mass.config.get_base_protocol_config.side_effect = lambda *_args: _config({})
    apple_tv = AirPlayPlayer(
        provider=provider,
        protocol_id="apaabbcc001101",
        raop_discovery_info=None,
        airplay_discovery_info=_record(apple_tv_position, "AA:BB:CC:00:11:01", "0x644"),
        address="192.168.1.60",
        display_name="Apple TV Study",
        manufacturer="Apple",
        model="Apple TV 4K Gen3",
    )
    speaker = AirPlaySpeaker(
        provider=provider,
        protocol_id="apaabbcc001102",
        raop_discovery_info=None,
        airplay_discovery_info=_record(1, "AA:BB:CC:00:11:02", speaker_flags),
        address="192.168.1.61",
        display_name="HomePod Study",
        manufacturer="Apple",
        model="HomePod",
    )
    provider.get_set_carrier.return_value = apple_tv
    provider.get_set_speakers.return_value = [speaker]
    provider.get_protocol.side_effect = {apple_tv.protocol_id: apple_tv}.get
    return HomeTheater(apple_tv, speaker, provider)


def _playing_stream(session: MagicMock | None = None) -> MagicMock:
    """Return a connected, running stream of a session."""
    return MagicMock(
        running=True,
        connected=True,
        superseded=False,
        session=session or MagicMock(),
        send_cli_command=AsyncMock(return_value=True),
    )


def test_a_set_plays_in_the_session_of_its_speakers() -> None:
    """The set renders through its speaker, and the session names the set, not the HomePod."""
    apple_tv, speaker, _ = _home_theater()
    session = MagicMock()
    session.sync_clients = [speaker]
    speaker.stream = _playing_stream(session)
    apple_tv._attr_playback_state = PlaybackState.PLAYING

    assert apple_tv.carries_set is True
    assert apple_tv.endpoints == [speaker]
    assert speaker.owner is apple_tv
    assert apple_tv.session is session
    assert apple_tv.live_session_members == [apple_tv.protocol_id]
    assert apple_tv.has_live_audio is True
    # the Apple TV joins the session of its speakers, without being heard
    assert apple_tv.session_endpoints == [speaker, apple_tv]
    assert apple_tv.silent_member is apple_tv
    assert (apple_tv.renders_audio, speaker.renders_audio) == (False, True)
    session.sync_clients = [speaker, apple_tv]
    assert apple_tv.live_session_members == [apple_tv.protocol_id]


def test_a_set_that_plays_on_its_homepods_only_leaves_its_apple_tv_out() -> None:
    """With the option on the set has no member that is not heard; it is off by default."""
    apple_tv, _, _ = _home_theater()
    homepods_only, _, _ = _home_theater(homepods_only=True)

    assert (apple_tv.homepods_only, homepods_only.homepods_only) == (False, True)
    assert homepods_only.silent_member is None
    for room in (apple_tv, homepods_only):
        assert PlayerFeature.OPTIONS in room.supported_features
        (option,) = room.options
        assert (option.key, option.value, option.read_only) == (
            CONF_HOMEPODS_ONLY,
            room.homepods_only,
            False,
        )


def test_a_plain_apple_tv_plays_on_itself() -> None:
    """An Apple TV without a set renders its own audio."""
    apple_tv, _, _ = _home_theater(apple_tv_position=None)

    assert apple_tv.carries_set is False
    assert apple_tv.endpoints == [apple_tv]
    assert apple_tv.speakers == []
    assert apple_tv.silent_member is None
    assert apple_tv.renders_audio is True
    assert PlayerFeature.OPTIONS not in apple_tv.supported_features
    assert list(apple_tv.options) == []


def test_a_speaker_plays_with_the_settings_of_its_set() -> None:
    """Sync offset, password, output settings and volume come from the set, never the speaker."""
    apple_tv, speaker, provider = _home_theater()
    apple_tv._config = _config(
        {
            CONF_SYNC_ADJUST: 80,
            CONF_PASSWORD: "s3cret",
            CONF_STREAMING_MODE: STREAMING_MODE_AP2_COMPAT,
            CONF_BUFFER_DEPTH: 1500,
        }
    )
    # what the player the HomePod had before still stores
    speaker._config = _config(
        {CONF_SYNC_ADJUST: 500, CONF_PASSWORD: "stale", CONF_IGNORE_VOLUME: True},
        setup_data={CONF_AIRPLAY_CREDENTIALS: "a" * 192},
    )
    provider.mass.config.set_protocol_value(speaker.protocol_id, CONF_ENABLE_HIRES, True)
    provider.mass.config.set_protocol_value(speaker.protocol_id, CONF_PASSWORD_INVALID, True)
    apple_tv._attr_volume_level = 35
    apple_tv._attr_volume_muted = True

    assert speaker.sync_offset_ms == -80
    assert speaker.device_password == "s3cret"
    assert speaker.output_plan_protocol_id == apple_tv.protocol_id
    # the streaming settings of the set apply to its speakers, as far as the speaker offers them
    assert speaker.settings is apple_tv.config
    assert speaker.streaming_mode == STREAMING_MODE_AP2_COMPAT
    assert speaker.settings.get_value(CONF_BUFFER_DEPTH) == 1500
    assert speaker.password_invalid is False
    assert speaker.get_setup_value(CONF_AIRPLAY_CREDENTIALS) is None
    assert speaker.hires_playback_enabled is False
    assert (speaker.volume_level, speaker.volume_muted) == (35, True)


def test_a_speaker_reports_to_its_set_and_stores_nothing_itself() -> None:
    """State and a rejected password reach the set; the speaker writes no config of its own."""
    apple_tv, speaker, provider = _home_theater()
    stream = MagicMock()
    speaker.stream = stream

    speaker.set_state_from_stream(PlaybackState.PLAYING, 12.0, stream=stream)
    # a stream the speaker no longer plays on says nothing about it
    speaker.set_state_from_stream(PlaybackState.IDLE, stream=MagicMock())
    speaker.set_password_invalid(True)

    assert apple_tv.playback_state == PlaybackState.PLAYING
    assert apple_tv.elapsed_time == 12.0
    assert apple_tv.password_invalid is True
    set_value = provider.mass.config.set_protocol_value
    set_value.assert_called_once_with(apple_tv.protocol_id, CONF_PASSWORD_INVALID, True)


async def test_the_volume_of_a_set_is_sent_to_its_speakers() -> None:
    """The set's volume and mute reach the speaker's stream and are stored on the set."""
    apple_tv, speaker, provider = _home_theater()
    speaker.stream = _playing_stream()
    apple_tv.stream = _playing_stream(speaker.stream.session)

    await apple_tv.volume_set(25)
    await apple_tv.volume_mute(True)

    speaker.stream.send_cli_command.assert_any_await("VOLUME=25")
    speaker.stream.send_cli_command.assert_awaited_with("VOLUME=0")
    # an AirPlay volume writes the level of the Apple TV, which is not heard
    apple_tv.stream.send_cli_command.assert_not_awaited()
    assert provider.mass.config.get_protocol_value(apple_tv.protocol_id, CONF_STORED_VOLUME) == 25
    assert {call.args[0] for call in provider.mass.config.set_protocol_value.call_args_list} == {
        apple_tv.protocol_id
    }


async def test_play_media_on_a_set_starts_a_session_of_its_speakers() -> None:
    """The session of a set holds its speakers, so the Apple TV gets no session of its own."""
    apple_tv, speaker, _ = _home_theater()
    session = MagicMock(start=AsyncMock())
    with (
        patch(
            "music_assistant.providers.airplay.player.AirPlayStreamSession",
            return_value=session,
        ) as session_factory,
        patch.object(
            apple_tv, "_get_session_pcm_format", AsyncMock(return_value=AIRPLAY_PCM_FORMAT)
        ) as get_format,
    ):
        await apple_tv.play_media(MagicMock())

    assert session_factory.call_args.args[1] == [speaker]
    # the format counts the output settings of the set as well as its speakers
    get_format.assert_awaited_once()
    assert get_format.call_args.args[0] == [apple_tv, speaker]
    session.start.assert_awaited_once()


async def test_a_set_without_a_speaker_on_the_network_cannot_play() -> None:
    """With none of its speakers seen the set refuses to play, instead of playing on nothing."""
    apple_tv, _, provider = _home_theater()
    provider.get_set_speakers.return_value = []

    with pytest.raises(PlayerCommandFailed, match="None of the speakers of Study"):
        await apple_tv.play_media(MagicMock())


async def test_a_set_joins_and_leaves_a_group_with_its_speakers() -> None:
    """A set that joins or leaves the group of another player brings its speakers along."""
    apple_tv, speaker, provider = _home_theater()
    leader = AirPlayPlayer(
        provider=provider,
        protocol_id="apleader",
        raop_discovery_info=None,
        airplay_discovery_info=None,
        address="192.168.1.70",
        display_name="Kitchen",
        manufacturer="Acme",
        model="Receiver",
    )
    provider.get_protocol.side_effect = {
        apple_tv.protocol_id: apple_tv,
        leader.protocol_id: leader,
    }.get
    session = MagicMock(add_client=AsyncMock(), remove_client=AsyncMock())
    session.sync_clients = [leader]
    leader.stream = _playing_stream(session)

    await leader.set_members(player_ids_to_add=[apple_tv.protocol_id])
    # the session adds the Apple TV after its speakers
    session.add_client.assert_awaited_once_with(speaker)
    session.sync_clients = [leader, speaker, apple_tv]
    await leader.set_members(player_ids_to_remove=[apple_tv.protocol_id])

    assert session.remove_client.await_args_list == [
        call(speaker, reason="child removed from group"),
        call(apple_tv, reason="child removed from group"),
    ]


async def test_pausing_a_set_parks_the_session_of_its_speakers() -> None:
    """A set pauses as a group does: its session is parked, to resume in step."""
    apple_tv, speaker, _ = _home_theater()
    session = MagicMock(standby=AsyncMock(return_value=True))
    speaker.stream = _playing_stream(session)

    await apple_tv.pause()

    session.standby.assert_awaited_once()
    speaker.stream.send_cli_command.assert_not_awaited()


async def test_a_set_needs_the_pairing_of_its_apple_tv_unless_it_leaves_it_out() -> None:
    """
    A set takes part with its Apple TV, which needs its pairing as a plain Apple TV does.

    Playing on the HomePods only, it needs no pairing, only the password a HomePod asks for.
    """
    plain_apple_tv, _, _ = _home_theater(apple_tv_position=None)
    assert plain_apple_tv.setup_reason == "pairing_required"
    apple_tv, _, _ = _home_theater()
    assert apple_tv.setup_reason == "pairing_required"
    apple_tv, _, _ = _home_theater(homepods_only=True)
    assert apple_tv.needs_setup is False
    # a HomePod that asks for a password asks it for its whole set
    apple_tv, _, _ = _home_theater(speaker_flags="0xa0c84")
    assert apple_tv.setup_reason == "password_required"

    apple_tv, _, _ = _home_theater(speaker_flags="0xa0c84", homepods_only=True)
    assert apple_tv.setup_reason == "password_required"
    flow = MagicMock()
    with (
        patch.object(apple_tv, "_ask_device_password", AsyncMock()) as ask_password,
        patch.object(apple_tv, "_run_protocol_pairing", AsyncMock()) as pair,
    ):
        await apple_tv._run_streaming_pairing(flow, {})
    ask_password.assert_awaited_once_with(flow)
    pair.assert_not_awaited()

    apple_tv._config = _config({CONF_PASSWORD: "s3cret"})
    assert apple_tv.needs_setup is False


def test_a_set_takes_the_name_of_its_room() -> None:
    """The set is named after the room it is in; a plain Apple TV keeps its own name."""
    apple_tv, _, _ = _home_theater()
    plain_apple_tv, _, _ = _home_theater(apple_tv_position=None)

    assert apple_tv.name == "Study"
    assert plain_apple_tv.name == "Apple TV Study"


def test_the_media_of_a_set_reaches_its_speakers_and_its_apple_tv() -> None:
    """A media update of the set is pushed by its speaker and by the Apple TV that shows it."""
    apple_tv, speaker, provider = _home_theater()
    speaker.stream = _playing_stream()
    speaker.stream.send_metadata = MagicMock(return_value="push")
    apple_tv.stream = _playing_stream()
    apple_tv.stream.send_metadata = MagicMock(return_value="show")
    media = MagicMock(corrected_elapsed_time=7.0)
    apple_tv._player = MagicMock(current_media=media)

    apple_tv.on_player_media_updated()

    speaker.stream.send_metadata.assert_called_once_with(7, media)
    apple_tv.stream.send_metadata.assert_called_once_with(7, media)
    assert provider.mass.create_task.call_args_list == [call("push"), call("show")]


def test_a_speaker_sends_no_volume_report_of_its_own() -> None:
    """A volume the HomePod reports is handed to the set, which owns the volume."""
    apple_tv, speaker, _ = _home_theater()

    with patch.object(apple_tv, "update_volume_from_device") as update:
        speaker.update_volume_from_device(40)

    update.assert_called_once_with(40)
    assert cast("MagicMock", speaker.provider).mass.config.set_protocol_value.call_count == 0


def test_a_speaker_keeps_the_state_of_its_own_stream() -> None:
    """The session a set plays in reads its speakers as playing, as it does any member."""
    apple_tv, speaker, _ = _home_theater()
    stream = MagicMock()
    speaker.stream = stream

    speaker.set_state_from_stream(PlaybackState.PLAYING, 3.0, stream=stream)

    assert speaker.playback_state == PlaybackState.PLAYING
    assert apple_tv.playback_state == PlaybackState.PLAYING


def test_a_set_is_idle_only_once_none_of_its_speakers_plays() -> None:
    """One speaker of a pair that stops does not make the set idle while the other plays on."""
    apple_tv, speaker, provider = _home_theater()
    other = AirPlaySpeaker(
        provider=provider,
        protocol_id="apaabbcc001103",
        raop_discovery_info=None,
        airplay_discovery_info=_record(2, "AA:BB:CC:00:11:03", "0xa0c04"),
        address="192.168.1.62",
        display_name="HomePod Study 2",
        manufacturer="Apple",
        model="HomePod",
    )
    provider.get_set_speakers.return_value = [speaker, other]
    speaker.stream, other.stream = MagicMock(running=False), MagicMock(running=True)
    apple_tv._attr_playback_state = PlaybackState.PLAYING

    speaker.set_state_from_stream(PlaybackState.IDLE, 0, stream=speaker.stream)
    while_the_other_plays = apple_tv.playback_state
    other.stream.running = False
    other.set_state_from_stream(PlaybackState.IDLE, 0, stream=other.stream)

    assert (while_the_other_plays, apple_tv.playback_state) == (
        PlaybackState.PLAYING,
        PlaybackState.IDLE,
    )


def test_a_set_plays_24_bit_only_when_its_speakers_do() -> None:
    """
    The formats of a set are those of its speakers, not of its Apple TV.

    The Apple TV is fed the base format either way, as nothing of it is heard.
    """
    apple_tv, speaker, provider = _home_theater()
    apple_tv.advertised_audio_formats = 1 << 19 | 1 << 21
    provider.mass.config.set_protocol_value(apple_tv.protocol_id, CONF_ENABLE_HIRES, True)
    session_format = AudioFormat(
        content_type=ContentType.PCM_S32LE, sample_rate=48000, bit_depth=24
    )

    assert apple_tv.hires_playback_enabled is False
    assert apple_tv.supported_sample_rates == [(44100, 16)]

    # a speaker that plays 24-bit makes the set a 24-bit player
    speaker.advertised_audio_formats = 1 << 21
    with patch("music_assistant.providers.airplay.player.default_hires_enabled", return_value=True):
        assert (speaker.hires_playback_enabled, apple_tv.hires_playback_enabled) == (True, True)
        assert (48000, 24) in apple_tv.supported_sample_rates
        assert speaker.get_stream_pcm_format(session_format).bit_depth == 24
        assert apple_tv.get_stream_pcm_format(session_format) == AIRPLAY_PCM_FORMAT


async def test_a_set_pairs_its_apple_tv_in_the_setup_flow() -> None:
    """The setup of a set pairs its Apple TV, which takes part in the sessions of the set."""
    apple_tv, _, _ = _home_theater()
    # the room first offers to play on its HomePods only, which the user declines
    flow = MagicMock(form=AsyncMock(return_value={CONF_HOMEPODS_ONLY: False}))
    with patch.object(apple_tv, "_run_protocol_pairing", AsyncMock(return_value=False)) as pair:
        await apple_tv._run_streaming_pairing(flow, {})

    assert flow.form.await_args.kwargs["step_id"] == "homepods_only_offer"
    pair.assert_awaited_once()
    assert apple_tv.homepods_only is False


def test_the_state_of_a_set_is_not_the_one_of_its_apple_tv() -> None:
    """What the stream of the Apple TV reports is not heard; a plain Apple TV plays on it."""
    apple_tv, _, _ = _home_theater()
    plain_apple_tv, _, _ = _home_theater(apple_tv_position=None)
    for player in (apple_tv, plain_apple_tv):
        player.stream = MagicMock()
        player.set_state_from_stream(PlaybackState.PLAYING, 5.0, stream=player.stream)

    assert apple_tv.playback_state == PlaybackState.IDLE
    assert plain_apple_tv.playback_state == PlaybackState.PLAYING


@pytest.mark.parametrize("homepods_only", [True, False])
async def test_the_option_of_a_set_takes_its_apple_tv_out_of_the_music_or_back_in(
    homepods_only: bool,
) -> None:
    """The option is stored on the set and applies to the session that plays at once."""
    apple_tv, speaker, provider = _home_theater(homepods_only=not homepods_only)
    session = MagicMock(remove_silent_member=AsyncMock(), add_silent_member=MagicMock())
    speaker.stream = _playing_stream(session)

    await apple_tv.set_option(CONF_HOMEPODS_ONLY, homepods_only)

    assert provider.mass.config.get_protocol_value(apple_tv.protocol_id, CONF_HOMEPODS_ONLY) is (
        homepods_only
    )
    assert apple_tv.homepods_only is homepods_only
    if homepods_only:
        session.remove_silent_member.assert_awaited_once_with(
            apple_tv, rejoin=False, reason="set plays on its speakers only"
        )
        session.add_silent_member.assert_not_called()
    else:
        session.add_silent_member.assert_called_once_with(apple_tv)
        session.remove_silent_member.assert_not_awaited()


async def test_only_a_set_has_the_option_to_play_on_its_homepods_only() -> None:
    """A plain Apple TV refuses the option, as a set refuses an option it does not have."""
    plain_apple_tv, _, _ = _home_theater(apple_tv_position=None)
    apple_tv, _, _ = _home_theater()

    with pytest.raises(PlayerCommandFailed):
        await plain_apple_tv.set_option(CONF_HOMEPODS_ONLY, True)
    with pytest.raises(PlayerCommandFailed):
        await apple_tv.set_option("other", True)


async def test_a_stop_of_a_set_reaches_the_session_of_its_speakers() -> None:
    """A stop ends the session of a set, also once the streams of its speakers ended."""
    apple_tv, speaker, _ = _home_theater()
    session = MagicMock(stop=AsyncMock())
    speaker.stream = MagicMock(running=False, session=session)

    await apple_tv.stop()

    session.stop.assert_awaited_once()


def _leader_of_group(provider: MagicMock, apple_tv: AirPlayPlayer) -> AirPlayPlayer:
    """Return another AirPlay player that leads a group the set plays in."""
    leader = AirPlayPlayer(
        provider=provider,
        protocol_id="apleader",
        raop_discovery_info=None,
        airplay_discovery_info=None,
        address="192.168.1.70",
        display_name="Kitchen",
        manufacturer="Acme",
        model="Receiver",
    )
    provider.get_protocol.side_effect = {
        apple_tv.protocol_id: apple_tv,
        leader.protocol_id: leader,
    }.get
    leader._attr_group_members = [leader.protocol_id, apple_tv.protocol_id]
    return leader


async def test_a_set_plays_on_when_the_leader_of_its_group_leaves() -> None:
    """The leader leaving takes only its own stream out: the set plays on with its Apple TV."""
    apple_tv, speaker, provider = _home_theater()
    leader = _leader_of_group(provider, apple_tv)
    session = MagicMock(remove_client=AsyncMock())
    session.sync_clients = [leader, speaker, apple_tv]
    leader.stream = _playing_stream(session)

    await leader.set_members(player_ids_to_remove=[leader.protocol_id])

    session.remove_client.assert_awaited_once_with(leader, reason="leader removed from group")


async def test_a_set_detached_in_the_group_of_another_player_takes_its_apple_tv_along() -> None:
    """A set whose protocol detaches leaves the group session with its speakers and Apple TV."""
    apple_tv, speaker, _ = _home_theater()
    session = MagicMock(remove_client=AsyncMock())
    speaker.stream = _playing_stream(session)
    apple_tv.stream = _playing_stream(session)

    with patch.object(AirPlayPlayer, "update_state"):
        await apple_tv.on_detach()

    assert session.remove_client.await_args_list == [
        call(speaker, reason="player unloaded"),
        call(apple_tv, reason="player unloaded"),
    ]


async def test_a_set_heals_the_group_session_it_fell_out_of_with_its_speakers() -> None:
    """
    The re-join of a set adds its speakers to the session of the group again.

    Its Apple TV follows them, which the session takes care of.
    """
    apple_tv, speaker, provider = _home_theater()
    leader = _leader_of_group(provider, apple_tv)
    session = MagicMock()
    session.sync_clients = [leader]

    async def add_client(player: AirPlayPlayer) -> None:
        session.sync_clients.append(player)
        player.stream = _playing_stream(session)

    session.add_client = AsyncMock(side_effect=add_client)
    leader.stream = _playing_stream(session)
    leader._attr_playback_state = PlaybackState.PLAYING
    apple_tv._attr_playback_state = PlaybackState.IDLE

    with patch("music_assistant.providers.airplay.player.AIRPLAY_REJOIN_ATTEMPT_DELAYS", (0,)):
        await apple_tv._group_rejoin_attempts([leader.protocol_id])

    session.add_client.assert_awaited_once_with(speaker)


async def test_a_stereo_pair_plays_on_both_of_its_speakers() -> None:
    """A set with a pair of HomePods starts a session of both, the first one first."""
    apple_tv, speaker, provider = _home_theater()
    other = AirPlaySpeaker(
        provider=provider,
        protocol_id="apaabbcc001103",
        raop_discovery_info=None,
        airplay_discovery_info=_record(2, "AA:BB:CC:00:11:03", "0xa0c04"),
        address="192.168.1.62",
        display_name="HomePod Study 2",
        manufacturer="Apple",
        model="HomePod",
    )
    provider.get_set_speakers.return_value = [speaker, other]
    session = MagicMock(start=AsyncMock())
    with (
        patch(
            "music_assistant.providers.airplay.player.AirPlayStreamSession",
            return_value=session,
        ) as session_factory,
        patch.object(
            apple_tv, "_get_session_pcm_format", AsyncMock(return_value=AIRPLAY_PCM_FORMAT)
        ),
    ):
        await apple_tv.play_media(MagicMock())

    assert session_factory.call_args.args[1] == [speaker, other]
    assert apple_tv.session_endpoints == [speaker, other, apple_tv]
