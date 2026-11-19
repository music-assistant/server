"""Tests for the Sendspin Sync plugin provider entry point."""

from __future__ import annotations

import asyncio
import pathlib
from contextlib import aclosing
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.auth import Scope, UserRole
from music_assistant_models.enums import (
    MediaType,
    PlaybackState,
    PlayerType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import (
    ActionUnavailable,
    InvalidDataError,
    MediaNotFoundError,
    PlayerUnavailableError,
    ResourceBusyError,
    UnsupportedFeaturedException,
)
from music_assistant_models.player import OutputProtocol
from music_assistant_models.provider import ProviderManifest

from music_assistant.controllers.streams.audio import AUDIO_SOURCE_CHUNK_SECONDS
from music_assistant.controllers.webserver.helpers.auth_middleware import ROLE_SCOPES
from music_assistant.helpers.shared_playback import SENDSPIN_DOMAIN
from music_assistant.providers import sendspin_sync
from music_assistant.providers.sendspin_sync import (
    AUDIO_SOURCE_ID,
    CONF_OWED_STATIC_DELAYS,
    SESSION_TIMEOUT_SECONDS,
    SUPPORTED_FEATURES,
    SendspinSyncProvider,
    setup,
)
from music_assistant.providers.sendspin_sync.chirp import (
    BIT_DEPTH,
    CHANNELS,
    SAMPLE_RATE,
    build_chirp_period,
)
from music_assistant.providers.sendspin_sync.session import CalibrationSession

MANIFEST_PATH = pathlib.Path(sendspin_sync.__file__).parent / "manifest.json"


async def test_setup_returns_the_plugin_provider() -> None:
    """setup() hands Music Assistant a SendspinSyncProvider instance."""
    manifest = MagicMock()
    config = MagicMock()
    # both are load-bearing: Provider.__init__ names its logger after the domain and
    # passes the log level through str(), which a bare MagicMock turns into a repr
    # that setLevel rejects
    manifest.domain = "sendspin_sync"
    config.get_value = MagicMock(return_value="GLOBAL")
    provider = await setup(MagicMock(), manifest, config)
    assert isinstance(provider, SendspinSyncProvider)


async def test_manifest_declares_its_sendspin_dependency() -> None:
    """The shipped manifest couples the plugin to the sendspin provider."""
    manifest = await ProviderManifest.parse(str(MANIFEST_PATH))
    assert manifest.domain == "sendspin_sync"
    assert manifest.depends_on == "sendspin"


def test_provider_declares_the_audio_source_feature() -> None:
    """The calibration track is offered through the AudioSource feature."""
    assert {ProviderFeature.AUDIO_SOURCE} == SUPPORTED_FEATURES


async def test_the_calibration_source_is_only_started_by_a_session() -> None:
    """
    The exposed AudioSource is offered nowhere a user could start it, and has no controls.

    Started on another queue while a session runs, it would take the stream away from
    the session and leave its speakers silent until the timeout.
    """
    provider = await _setup_provider()
    (source,) = await provider.get_audio_sources()
    assert source.item_id == AUDIO_SOURCE_ID
    assert not source.can_initiate
    assert source.exclusive
    assert not source.can_play_pause
    assert not source.can_seek
    assert not source.can_next_previous
    assert not source.allow_external_trigger


async def test_stream_details_describe_the_generated_pcm() -> None:
    """The declared format matches the bytes the generator actually emits."""
    provider = await _setup_provider()
    streamdetails = await provider.get_stream_details(AUDIO_SOURCE_ID, MediaType.AUDIO_SOURCE)
    assert streamdetails.stream_type == StreamType.CUSTOM
    assert streamdetails.audio_format.sample_rate == SAMPLE_RATE
    assert streamdetails.audio_format.bit_depth == BIT_DEPTH
    assert streamdetails.audio_format.channels == CHANNELS


async def test_stream_details_reject_an_unknown_source() -> None:
    """A request for an item this plugin does not own is a MediaNotFoundError."""
    provider = await _setup_provider()
    with pytest.raises(MediaNotFoundError):
        await provider.get_stream_details("nope", MediaType.AUDIO_SOURCE)


async def test_stream_details_claim_nothing() -> None:
    """Fetching stream details leaves ownership untouched, so queue preload is safe."""
    provider = await _setup_provider()
    await provider.get_stream_details(AUDIO_SOURCE_ID, MediaType.AUDIO_SOURCE)
    assert provider._in_use_by_queue is None
    assert provider._active_session_id is None


async def test_audio_stream_repeats_the_period_seamlessly() -> None:
    """Consecutive chunks concatenate back into whole periods, with no gap at the loop."""
    provider = await _setup_provider()
    await provider.on_source_selected(AUDIO_SOURCE_ID, "player", "queue", "session")
    period = build_chirp_period()
    streamed = await _take_bytes(provider, len(period) * 2)
    assert streamed == period * 2


async def test_the_period_slices_into_whole_chunks() -> None:
    """
    The period divides evenly by the chunk size the streams controller paces at.

    An indivisible period would leave a short chunk at every loop boundary, so the
    stream would stop handing out the uniform, ready-made chunks the up-front
    slicing exists to provide.
    """
    provider = await _setup_provider()
    chunk_size = int(SAMPLE_RATE * AUDIO_SOURCE_CHUNK_SECONDS) * CHANNELS * (BIT_DEPTH // 8)
    assert {len(chunk) for chunk in provider._period_chunks} == {chunk_size}


async def test_audio_stream_stops_when_another_session_supersedes_it() -> None:
    """A fresh claim on the same queue ends the previous session's generator."""
    provider = await _setup_provider()
    await provider.on_source_selected(AUDIO_SOURCE_ID, "player", "queue", "session")
    async with aclosing(provider.get_audio_stream(MagicMock())) as stream:
        assert await anext(stream)
        await provider.on_source_selected(AUDIO_SOURCE_ID, "player", "queue", "newer-session")
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
    # the superseded generator must not release the newer session's claim
    assert provider._in_use_by_queue == "queue"
    assert provider._active_session_id == "newer-session"


async def test_teardown_releases_the_source() -> None:
    """The matching unselect frees the source for another queue."""
    provider = await _setup_provider()
    await provider.on_source_selected(AUDIO_SOURCE_ID, "player", "queue", "session")
    await provider.on_source_unselected(AUDIO_SOURCE_ID, "queue", "session")
    assert provider._in_use_by_queue is None
    assert provider._active_session_id is None


async def test_a_stale_teardown_leaves_the_live_claim_alone() -> None:
    """An unselect from a superseded same-queue request is ignored."""
    provider = await _setup_provider()
    await provider.on_source_selected(AUDIO_SOURCE_ID, "player", "queue", "newer-session")
    await provider.on_source_unselected(AUDIO_SOURCE_ID, "queue", "older-session")
    assert provider._in_use_by_queue == "queue"
    assert provider._active_session_id == "newer-session"


async def test_source_hooks_ignore_another_plugins_source() -> None:
    """Lifecycle callbacks for a source this plugin does not own change nothing."""
    provider = await _setup_provider()
    await provider.on_source_selected("elsewhere", "player", "queue", "session")
    assert provider._in_use_by_queue is None

    await provider.on_source_selected(AUDIO_SOURCE_ID, "player", "queue", "session")
    await provider.on_source_unselected("elsewhere", "queue", "session")
    assert provider._in_use_by_queue == "queue"


async def test_api_commands_are_registered_and_released_on_unload() -> None:
    """Every session command is registered under the plugin namespace and given back."""
    provider = await _setup_provider()
    unregister_calls: list[str] = []

    def _register(command: str, _handler: object, **_kwargs: object) -> MagicMock:
        return MagicMock(side_effect=lambda: unregister_calls.append(command))

    mass = _mock_mass(provider)
    mass.register_api_command = MagicMock(side_effect=_register)
    await provider.loaded_in_mass()

    registered = [call.args[0] for call in mass.register_api_command.call_args_list]
    assert registered == [
        "sendspin_sync/eligible_players",
        "sendspin_sync/session",
        "sendspin_sync/start_session",
        "sendspin_sync/solo_player",
        "sendspin_sync/keep_alive",
        "sendspin_sync/apply_measurements",
        "sendspin_sync/stop_session",
    ]

    await provider.unload()
    assert unregister_calls == registered


async def test_session_commands_need_more_than_a_guest_scope() -> None:
    """
    Driving a session is guarded like any other player config write.

    Starting one zeroes the static delays, stopping one puts them back and applying a
    result persists one - every mutation config/players/save gates on
    CONFIG_PLAYERS_WRITE - and an in-process call into the Sendspin provider is never
    re-checked against the caller's scopes, so the registration is the only guard.
    Pinned per command: lowering one back to a guest scope is invisible in behaviour
    and only shows up as a security problem.
    """
    provider = await _setup_provider()
    mass = _mock_mass(provider)
    mass.register_api_command = MagicMock()
    await provider.loaded_in_mass()

    scope_by_command = {
        call.args[0]: call.kwargs["required_scope"]
        for call in mass.register_api_command.call_args_list
    }

    assert scope_by_command == {
        "sendspin_sync/eligible_players": Scope.PLAYERS_READ,
        "sendspin_sync/session": Scope.PLAYERS_READ,
        "sendspin_sync/start_session": Scope.CONFIG_PLAYERS_WRITE,
        "sendspin_sync/solo_player": Scope.CONFIG_PLAYERS_WRITE,
        "sendspin_sync/keep_alive": Scope.CONFIG_PLAYERS_WRITE,
        "sendspin_sync/apply_measurements": Scope.CONFIG_PLAYERS_WRITE,
        "sendspin_sync/stop_session": Scope.CONFIG_PLAYERS_WRITE,
    }
    assert Scope.CONFIG_PLAYERS_WRITE not in ROLE_SCOPES[UserRole.GUEST]


async def test_eligible_players_reports_which_speakers_can_be_written_to() -> None:
    """
    A speaker whose client refuses a static delay is offered, flagged rather than hidden.

    The flag lands while the user is picking speakers, so nobody walks the house before
    finding out which of them they will have to correct by hand.
    """
    provider = await _setup_provider()
    _stub_sendspin(provider, {"writable": 0, "fixed": 0}, non_adjustable={"fixed"})

    offered = await provider.get_eligible_players()

    assert [(p.player_id, p.adjustable) for p in offered] == [("writable", True), ("fixed", False)]


async def test_a_physical_speaker_is_offered_as_the_player_the_user_knows() -> None:
    """
    A wrapped Sendspin speaker is offered by its visible player, not its hidden client.

    That visible player is the one the user recognises and the only one grouping, volume
    and mute can be driven on; whether its delay can be written is asked of the client
    behind it, which is the object that carries one.
    """
    provider = await _setup_provider()
    sendspin = _stub_sendspin(
        provider,
        {"lr_client": 0, "browser": 0},
        non_adjustable={"lr_client"},
        session_members={"living_room": "lr_client", "browser": "browser"},
    )

    offered = await provider.get_eligible_players()

    assert [(p.player_id, p.name, p.adjustable) for p in offered] == [
        ("living_room", "living_room", False),
        ("browser", "browser", True),
    ]
    assert sendspin.supports_player_static_delay.call_args_list[0].args[0] == "lr_client"


async def test_a_speaker_that_does_not_play_over_sendspin_is_not_offered() -> None:
    """A player MA reaches over other protocols only can not carry a Sendspin delay."""
    provider = await _setup_provider()
    _stub_sendspin(provider, {"browser": 0})
    mass = _mock_mass(provider)
    airplay_only = MagicMock()
    airplay_only.player_id = "airplay_only"
    airplay_only.state.available = True
    airplay_only.state.type = PlayerType.PLAYER
    airplay_only.get_output_protocol_by_domain = MagicMock(return_value=None)
    mass.players.all_players = MagicMock(
        return_value=[airplay_only, mass.players.get_player("browser")]
    )

    offered = await provider.get_eligible_players()

    assert [p.player_id for p in offered] == ["browser"]


async def test_a_session_anchor_is_never_offered_as_a_speaker() -> None:
    """
    The hidden virtual player that leads a calibration group is not a speaker to pick.

    Eligibility no longer turns on the provider a player belongs to, and a virtual player
    is typed like a web player, so this is the check that keeps anchors - this plugin's
    or another's - out of the list.
    """
    provider = await _setup_provider()
    sendspin = _stub_sendspin(provider, {"browser": 0, "virtual_anchor": 0})
    sendspin.is_virtual_player = MagicMock(side_effect=lambda player_id: player_id != "browser")

    offered = await provider.get_eligible_players()

    assert [p.player_id for p in offered] == ["browser"]


async def test_nothing_is_offered_without_the_sendspin_provider() -> None:
    """With Sendspin gone there are no Sendspin speakers to calibrate."""
    provider = await _setup_provider()
    _stub_players(provider, {"a": "a"})
    _mock_mass(provider).get_provider = MagicMock(return_value=None)

    assert await provider.get_eligible_players() == []


async def test_unload_stops_a_running_session() -> None:
    """Unloading the plugin does not leave a user's speakers muted and regrouped."""
    provider = await _setup_provider()
    session = MagicMock()
    session.stop = AsyncMock()
    provider._session = session

    await provider.unload()

    session.stop.assert_awaited_once()
    assert await provider.get_session() is None
    _mock_mass(provider).cancel_timer.assert_called_once()


async def test_no_session_reports_no_state() -> None:
    """The session command answers with None while nothing is running."""
    provider = await _setup_provider()
    assert await provider.get_session() is None


async def test_a_second_session_is_refused() -> None:
    """Only one calibration session can hold the speakers at a time."""
    provider = await _setup_provider()
    provider._session = MagicMock()

    with pytest.raises(ResourceBusyError):
        await provider.start_session(["player"])


async def test_solo_without_a_session_is_refused() -> None:
    """Soloing a speaker needs a session to solo it within."""
    provider = await _setup_provider()

    with pytest.raises(ActionUnavailable):
        await provider.solo_player("player")


async def test_stopping_without_a_session_is_a_no_op() -> None:
    """Stopping when nothing is running is not an error."""
    provider = await _setup_provider()
    await provider.stop_session()
    assert provider._session is None


async def test_the_stream_ending_externally_stops_the_session() -> None:
    """A session can not outlive the stream that carries its phase reference."""
    provider = await _setup_provider()
    session = MagicMock()
    session.queue_id = "anchor"
    session.stopped = False
    provider._session = session
    await provider.on_source_selected(AUDIO_SOURCE_ID, "player", "anchor", "session")

    await provider.on_source_unselected(AUDIO_SOURCE_ID, "anchor", "session")

    create_task = _mock_mass(provider).create_task
    create_task.assert_called_once()
    # the stubbed create_task never runs the teardown it was handed
    create_task.call_args.args[0].close()


async def test_another_queues_teardown_leaves_the_session_alone() -> None:
    """An unselect from a queue that is not the session's anchor changes nothing."""
    provider = await _setup_provider()
    session = MagicMock()
    session.queue_id = "anchor"
    session.stopped = False
    provider._session = session
    await provider.on_source_selected(AUDIO_SOURCE_ID, "player", "elsewhere", "session")

    await provider.on_source_unselected(AUDIO_SOURCE_ID, "elsewhere", "session")

    _mock_mass(provider).create_task.assert_not_called()


async def test_a_stopped_session_is_not_torn_down_again() -> None:
    """The unselect fired by the session's own stop does not re-enter that stop."""
    provider = await _setup_provider()
    session = MagicMock()
    session.queue_id = "anchor"
    session.stopped = True
    provider._session = session
    await provider.on_source_selected(AUDIO_SOURCE_ID, "player", "anchor", "session")

    await provider.on_source_unselected(AUDIO_SOURCE_ID, "anchor", "session")

    _mock_mass(provider).create_task.assert_not_called()


async def test_starting_a_session_arms_the_inactivity_timeout() -> None:
    """A phone that walks away can not leave a house muted, so the session is on a timer."""
    provider = await _setup_provider()
    mass = _mock_mass(provider)
    session = _stub_session()

    with patch.object(CalibrationSession, "create", AsyncMock(return_value=session)):
        await provider.start_session(["player"])

    mass.call_later.assert_called_once()
    assert mass.call_later.call_args.args[0] == SESSION_TIMEOUT_SECONDS
    assert mass.call_later.call_args.kwargs["task_id"] == provider._session_timeout_id
    session.begin.assert_awaited_once()


async def test_the_session_is_visible_before_its_stream_starts() -> None:
    """
    The teardown hook can only save a session it can already see.

    Starting the stream fires the source lifecycle hooks, so a session registered
    only after begin() returns would miss a stream that died on the way up and sit
    holding the speakers until the timeout.
    """
    provider = await _setup_provider()
    session = _stub_session()
    visible_during_begin: list[bool] = []
    session.begin = AsyncMock(
        side_effect=lambda _source: visible_during_begin.append(provider._session is session)
    )

    with patch.object(CalibrationSession, "create", AsyncMock(return_value=session)):
        await provider.start_session(["player"])

    assert visible_during_begin == [True]


async def test_each_solo_rearms_the_inactivity_timeout() -> None:
    """Driving the session keeps it alive; the timeout only fires once it is forgotten."""
    provider = await _setup_provider()
    mass = _mock_mass(provider)
    session = _stub_session()

    with patch.object(CalibrationSession, "create", AsyncMock(return_value=session)):
        await provider.start_session(["player"])
    await provider.solo_player("player")

    armed_with = [call.kwargs["task_id"] for call in mass.call_later.call_args_list]
    assert armed_with == [provider._session_timeout_id, provider._session_timeout_id]
    assert mass.cancel_timer.call_args_list == []


async def test_the_timeout_cancels_the_session_it_was_armed_for() -> None:
    """When the timeout fires it restores the speakers and clears the session."""
    provider = await _setup_provider()
    session = _stub_session()

    with patch.object(CalibrationSession, "create", AsyncMock(return_value=session)):
        await provider.start_session(["player"])
    await provider._handle_session_timeout()

    session.stop.assert_awaited_once()
    assert await provider.get_session() is None


async def test_a_keep_alive_rearms_the_inactivity_timeout_and_nothing_else() -> None:
    """A client that is still there says so without touching a speaker."""
    provider = await _setup_provider()
    mass = _mock_mass(provider)
    session = _stub_session()

    with patch.object(CalibrationSession, "create", AsyncMock(return_value=session)):
        await provider.start_session(["player"])
    await provider.keep_alive()

    assert len(mass.call_later.call_args_list) == 2
    session.solo.assert_not_awaited()


async def test_a_keep_alive_without_a_session_is_refused() -> None:
    """A client learns its session is gone from the keep-alive it sends."""
    provider = await _setup_provider()

    with pytest.raises(ActionUnavailable):
        await provider.keep_alive()

    _mock_mass(provider).call_later.assert_not_called()


async def test_a_teardown_queued_for_an_earlier_session_leaves_the_next_one_alone() -> None:
    """A teardown waits for the lock, and the session it was meant for may be gone by then."""
    provider = await _setup_provider()
    earlier = await _run_session(provider, ["player"])
    await provider.stop_session()
    current = await _run_session(provider, ["player"])

    await provider._stop_session(earlier)

    current.stop.assert_not_awaited()
    assert provider._session is current


async def test_a_start_that_is_cancelled_leaves_its_session_for_the_unload_to_finish() -> None:
    """
    A server going down cancels a start, and the stop that follows it, in one go.

    The session stays registered until it is torn down, so the unload still finds it.
    """
    provider = await _setup_provider()
    session = _stub_session()
    session.begin = AsyncMock(side_effect=asyncio.CancelledError)
    session.stop = AsyncMock(side_effect=[asyncio.CancelledError, None])

    with (
        patch.object(CalibrationSession, "create", AsyncMock(return_value=session)),
        pytest.raises(asyncio.CancelledError),
    ):
        await provider.start_session(["player"])
    await provider.unload()

    assert session.stop.await_count == 2


async def test_the_delays_a_session_takes_off_are_stored_until_they_are_back() -> None:
    """
    A server that goes down mid-session may not come back with speakers at zero.

    The zero is stored with each speaker and the delay it replaced is only in memory, so
    the plugin stores what it owes at once and clears it when the session has put it back.
    """
    provider = await _setup_provider()
    _stub_sendspin(provider, {"a": 250, "b": 0})
    stored = _mock_mass(provider).config.set_raw_provider_config_value

    await _run_real_session(provider, ["a", "b"])
    assert stored.call_args.args == (
        provider.instance_id,
        CONF_OWED_STATIC_DELAYS,
        '[["a", 250, null]]',
    )
    assert stored.call_args.kwargs == {"immediate": True}

    await provider.stop_session()
    assert stored.call_args.args[2] == "[]"


async def test_delays_left_off_by_a_server_that_went_down_are_put_back_on_load() -> None:
    """A speaker that is already there gets its delay back, for the carrier it came off."""
    provider = await _setup_provider()
    delays_ms = {"a": 0}
    sendspin = _stub_sendspin(provider, delays_ms)
    config = _mock_mass(provider).config
    config.get_raw_provider_config_value = MagicMock(return_value='[["a", 250, "a_airplay"]]')

    await provider.loaded_in_mass()

    sendspin.set_player_static_delay.assert_awaited_once_with("a", 250, carrier_id="a_airplay")
    assert config.set_raw_provider_config_value.call_args.args[2] == "[]"


async def test_a_speaker_that_is_not_back_yet_gets_its_delay_in_what_is_stored_of_it() -> None:
    """After a restart the plugin loads before most speakers have connected."""
    provider = await _setup_provider()
    sendspin = _stub_sendspin(provider, {})
    sendspin.set_player_static_delay = AsyncMock(side_effect=PlayerUnavailableError("a"))
    _mock_mass(provider).config.get_raw_provider_config_value = MagicMock(
        return_value='[["a", 250, null]]'
    )

    await provider.loaded_in_mass()

    sendspin.store_player_static_delay.assert_called_once_with("a", 250, carrier_id=None)


async def test_eligible_players_reports_which_speakers_are_silent() -> None:
    """A muted speaker is one a session refuses, so a client says so before the start."""
    provider = await _setup_provider()
    _stub_sendspin(provider, {"loud": 0, "muted": 0})
    _mock_mass(provider).players.get_player("muted").state.volume_muted = True

    offered = await provider.get_eligible_players()

    assert [(p.player_id, p.silent) for p in offered] == [("loud", False), ("muted", True)]


async def test_a_stream_that_never_starts_releases_the_session() -> None:
    """A failed start leaves nothing behind for the timeout to trip over."""
    provider = await _setup_provider()
    mass = _mock_mass(provider)
    session = _stub_session()
    session.begin = AsyncMock(side_effect=RuntimeError("no stream"))

    with (
        patch.object(CalibrationSession, "create", AsyncMock(return_value=session)),
        pytest.raises(RuntimeError),
    ):
        await provider.start_session(["player"])

    session.stop.assert_awaited_once()
    assert await provider.get_session() is None
    mass.cancel_timer.assert_called_with(provider._session_timeout_id)


async def test_applying_measurements_reaches_the_devices_through_the_sendspin_provider() -> None:
    """
    The normalised delays are handed to the published Sendspin applier, per player.

    Going through the provider is what pushes the value to the client; writing the
    config directly would leave the speaker itself uncorrected.
    """
    provider = await _setup_provider()
    sendspin = _stub_sendspin(provider, {"a": 0, "b": 0, "c": 0})
    await _run_session(provider, ["a", "b", "c"])

    result = await provider.apply_measurements({"a": 10.0, "b": 35.0, "c": 12.0})

    assert result.applied == {"a": 0, "b": 25, "c": 2}
    assert result.manual == {}
    assert _applied_delays(sendspin) == {"a": 0, "b": 25, "c": 2}


async def test_applying_measurements_folds_in_the_delays_already_in_place() -> None:
    """Each member's current delay is read back and counted towards how late it is."""
    provider = await _setup_provider()
    sendspin = _stub_sendspin(provider, {"amp": 200, "speaker": 0})
    await _run_session(provider, ["amp", "speaker"])

    result = await provider.apply_measurements({"amp": 0.0, "speaker": 40.0})

    read_back = [call.args[0] for call in sendspin.get_player_static_delay.call_args_list]
    assert sorted(read_back) == ["amp", "speaker"]
    # the amp is 200 ms of advance plus 0 measured; the speaker 0 plus 40
    assert result.applied == {"amp": 160, "speaker": 0}


async def test_re_measuring_a_corrected_group_writes_the_same_delays_again() -> None:
    """
    Running the calibration twice converges rather than drifting.

    A corrected group arrives together, so the second pass measures equal offsets;
    folding in what each player now carries has to reproduce those same values.
    """
    provider = await _setup_provider()
    sendspin = _stub_sendspin(provider, {"a": 0, "b": 0, "c": 0})
    await _run_session(provider, ["a", "b", "c"])
    first = await provider.apply_measurements({"a": 0.0, "b": 120.0, "c": 35.0})

    second = await provider.apply_measurements(dict.fromkeys(("a", "b", "c"), 4.0))

    assert first.applied == {"a": 0, "b": 120, "c": 35}
    assert second.applied == first.applied
    assert _applied_delays(sendspin) == first.applied


async def test_measurements_are_refused_without_a_running_session() -> None:
    """Offsets are only meaningful against a live stream, so there must be a session."""
    provider = await _setup_provider()
    sendspin = _stub_sendspin(provider, {"a": 0})

    with pytest.raises(ActionUnavailable):
        await provider.apply_measurements({"a": 0.0})

    sendspin.set_player_static_delay.assert_not_awaited()


async def test_a_refused_measurement_writes_nothing_at_all() -> None:
    """
    Every value is computed and accepted before the first one is written.

    A rejection partway through the group would leave it half corrected, which is
    worse than either applying the lot or applying none of it.
    """
    provider = await _setup_provider()
    sendspin = _stub_sendspin(provider, {"a": 0, "b": 0})
    await _run_session(provider, ["a", "b"])

    with pytest.raises(InvalidDataError):
        await provider.apply_measurements({"a": 0.0, "b": 99_000.0})

    sendspin.set_player_static_delay.assert_not_awaited()


async def test_a_measurement_for_a_player_outside_the_session_is_refused() -> None:
    """A speaker that did not share the session's stream can not be normalised with it."""
    provider = await _setup_provider()
    sendspin = _stub_sendspin(provider, {"a": 0, "stranger": 0})
    await _run_session(provider, ["a"])

    with pytest.raises(InvalidDataError):
        await provider.apply_measurements({"a": 0.0, "stranger": 5.0})

    sendspin.set_player_static_delay.assert_not_awaited()


async def test_applying_a_result_leaves_the_session_running() -> None:
    """
    The session survives so the correction can be verified where it was measured.

    Ending it would ungroup every speaker, changing the sync path the numbers describe.
    """
    provider = await _setup_provider()
    _stub_sendspin(provider, {"a": 0})
    session = await _run_session(provider, ["a"])

    await provider.apply_measurements({"a": 0.0})

    session.stop.assert_not_awaited()
    assert provider._session is session


async def test_applying_a_result_rearms_the_inactivity_timeout() -> None:
    """Applying is activity, so it keeps a session alive like a solo does."""
    provider = await _setup_provider()
    _stub_sendspin(provider, {"a": 0})
    await _run_session(provider, ["a"])
    mass = _mock_mass(provider)
    mass.call_later.reset_mock()

    await provider.apply_measurements({"a": 0.0})

    assert mass.call_later.call_args.kwargs["task_id"] == provider._session_timeout_id


async def test_a_member_that_cannot_take_a_static_delay_comes_back_for_the_user() -> None:
    """
    Such a member is measured and normalised like any other, but never written to.

    Its delay is handed back under ``manual`` instead, which is the whole point: the
    user can set it on the device even though MA can not.
    """
    provider = await _setup_provider()
    sendspin = _stub_sendspin(provider, {"a": 0, "b": 0}, non_adjustable={"b"})
    await _run_session(provider, ["a", "b"])

    result = await provider.apply_measurements({"a": 0.0, "b": 10.0})

    assert result.applied == {"a": 0}
    assert result.manual == {"b": 10}
    assert _applied_delays(sendspin) == {"a": 0}


async def test_a_member_that_cannot_take_a_static_delay_is_never_read_either() -> None:
    """
    Its current delay is taken as 0 rather than read, because the read would raise.

    MA holds no static delay for such a speaker, and whatever its firmware applies is
    already inside the arrival that was just measured.
    """
    provider = await _setup_provider()
    sendspin = _stub_sendspin(provider, {"a": 0, "b": 0}, non_adjustable={"b"})
    await _run_session(provider, ["a", "b"])

    await provider.apply_measurements({"a": 0.0, "b": 10.0})

    read_back = [call.args[0] for call in sendspin.get_player_static_delay.call_args_list]
    assert read_back == ["a"]


async def test_a_member_that_vanished_is_an_error_rather_than_one_to_correct_by_hand() -> None:
    """
    A speaker that has gone raises instead of landing under ``manual``.

    Sendspin answers the same "no static delay" for a speaker that has left as for one
    that refuses one, so the two have to be told apart before the result is built -
    otherwise the user is sent to go adjust a speaker that is not there.
    """
    provider = await _setup_provider()
    sendspin = _stub_sendspin(provider, {"a": 0, "b": 0})
    await _run_session(provider, ["a", "b"])
    _mock_mass(provider).players.get_player = MagicMock(
        side_effect=lambda player_id, *_: None if player_id == "b" else MagicMock()
    )

    with pytest.raises(PlayerUnavailableError):
        await provider.apply_measurements({"a": 0.0, "b": 10.0})

    sendspin.set_player_static_delay.assert_not_awaited()


async def test_a_member_that_went_unavailable_is_an_error_too() -> None:
    """A speaker still registered but off the network is no more correctable than a gone one."""
    provider = await _setup_provider()
    sendspin = _stub_sendspin(provider, {"a": 0, "b": 0})
    await _run_session(provider, ["a", "b"])
    gone = MagicMock()
    gone.state.available = False
    _mock_mass(provider).players.get_player = MagicMock(
        side_effect=lambda player_id, *_: gone if player_id == "b" else MagicMock()
    )

    with pytest.raises(PlayerUnavailableError):
        await provider.apply_measurements({"a": 0.0, "b": 10.0})

    sendspin.set_player_static_delay.assert_not_awaited()


async def test_a_delay_already_in_place_moves_the_reference_a_manual_value_is_measured_from() -> (
    None
):
    """
    The fold-in and the split compose: a peer's existing delay shifts the whole group.

    Ignoring the amp's 200 ms of advance would leave it at 0 and hand the user 40 for
    the speaker MA can not write to. Counting it makes the amp the later arrival of the
    two, so the unwritable speaker becomes the reference and needs nothing done to it.
    """
    provider = await _setup_provider()
    sendspin = _stub_sendspin(provider, {"amp": 200, "fixed": 0}, non_adjustable={"fixed"})
    await _run_session(provider, ["amp", "fixed"])

    result = await provider.apply_measurements({"amp": 0.0, "fixed": 40.0})

    # the amp totals 0 + 200, the fixed speaker 40 + 0 and so defines the reference
    assert result.applied == {"amp": 160}
    assert result.manual == {"fixed": 0}
    assert _applied_delays(sendspin) == {"amp": 160}


async def test_a_group_of_speakers_that_take_no_delay_writes_nothing_and_does_not_error() -> None:
    """A session of entirely unwritable speakers still yields a usable result."""
    provider = await _setup_provider()
    sendspin = _stub_sendspin(provider, {"a": 0, "b": 0}, non_adjustable={"a", "b"})
    await _run_session(provider, ["a", "b"])

    result = await provider.apply_measurements({"a": 5.0, "b": 45.0})

    assert result.applied == {}
    assert result.manual == {"a": 0, "b": 40}
    sendspin.set_player_static_delay.assert_not_awaited()


async def test_a_speaker_that_takes_no_delay_can_still_set_the_reference() -> None:
    """
    The earliest arrival gets 0 whether or not MA can write to it.

    It belongs in the min() that picks the reference like any other speaker; excluding
    it would normalise the group against a baseline that is not the earliest arrival.
    """
    provider = await _setup_provider()
    _stub_sendspin(provider, {"early": 0, "late": 0}, non_adjustable={"early"})
    await _run_session(provider, ["early", "late"])

    result = await provider.apply_measurements({"early": 5.0, "late": 65.0})

    assert result.manual == {"early": 0}
    assert result.applied == {"late": 60}


async def test_a_measurement_out_of_range_is_refused_for_an_unwritable_speaker_too() -> None:
    """A bad measurement is a bad measurement whoever was going to apply it."""
    provider = await _setup_provider()
    sendspin = _stub_sendspin(provider, {"a": 0, "b": 0}, non_adjustable={"b"})
    await _run_session(provider, ["a", "b"])

    with pytest.raises(InvalidDataError):
        await provider.apply_measurements({"a": 0.0, "b": 99_000.0})

    sendspin.set_player_static_delay.assert_not_awaited()


async def test_a_player_lost_mid_apply_leaves_the_earlier_writes_in_place() -> None:
    """
    A write that fails partway through propagates rather than being swallowed.

    Validation is all-or-nothing, but the writes are not: a speaker that disappears
    between the read and its own write leaves the players before it corrected. The
    caller has to see that rather than get a clean return over a half-corrected group.
    """
    provider = await _setup_provider()
    sendspin = _stub_sendspin(provider, {"a": 0, "b": 0, "c": 0})
    await _run_session(provider, ["a", "b", "c"])
    applied: list[str] = []

    async def _apply(player_id: str, _delay_ms: int) -> None:
        if player_id == "c":
            raise PlayerUnavailableError(player_id)
        applied.append(player_id)

    sendspin.set_player_static_delay = AsyncMock(side_effect=_apply)

    with pytest.raises(PlayerUnavailableError):
        await provider.apply_measurements({"a": 0.0, "b": 10.0, "c": 20.0})

    assert applied == ["a", "b"]


async def test_a_physical_speakers_correction_is_written_to_its_sendspin_client() -> None:
    """
    A wrapped speaker is measured as the player the user picked and corrected underneath.

    The result stays keyed by the member the client asked about, while the delay itself
    goes to the Sendspin player behind it - the only object that carries one.
    """
    provider = await _setup_provider()
    sendspin = _stub_sendspin(
        provider,
        {"lr_client": 0, "kitchen_client": 0},
        session_members={"living_room": "lr_client", "kitchen": "kitchen_client"},
    )
    await _run_session(provider, ["living_room", "kitchen"])

    result = await provider.apply_measurements({"living_room": 0.0, "kitchen": 40.0})

    assert result.applied == {"living_room": 0, "kitchen": 40}
    assert result.manual == {}
    assert _applied_delays(sendspin) == {"lr_client": 0, "kitchen_client": 40}


async def test_a_physical_speaker_that_takes_no_delay_comes_back_by_its_visible_id() -> None:
    """What the user has to go and adjust is named by the speaker, not by its client."""
    provider = await _setup_provider()
    sendspin = _stub_sendspin(
        provider,
        {"lr_client": 0, "kitchen_client": 0},
        non_adjustable={"kitchen_client"},
        session_members={"living_room": "lr_client", "kitchen": "kitchen_client"},
    )
    await _run_session(provider, ["living_room", "kitchen"])

    result = await provider.apply_measurements({"living_room": 0.0, "kitchen": 40.0})

    assert result.manual == {"kitchen": 40}
    assert _applied_delays(sendspin) == {"lr_client": 0}


async def test_a_member_that_lost_its_sendspin_client_is_refused() -> None:
    """
    A speaker whose Sendspin side went away mid-session can not be resolved, so it raises.

    Reporting it as one to adjust by hand would be a lie, and folding its delay in as 0
    would move the reference every other speaker in the group is corrected against.
    """
    provider = await _setup_provider()
    sendspin = _stub_sendspin(
        provider,
        {"lr_client": 0, "kitchen_client": 0},
        session_members={"living_room": "lr_client", "kitchen": "kitchen_client"},
    )
    await _run_session(provider, ["living_room", "kitchen"])
    kitchen = _mock_mass(provider).players.get_player("kitchen")
    kitchen.get_output_protocol_by_domain = MagicMock(return_value=None)

    with pytest.raises(PlayerUnavailableError):
        await provider.apply_measurements({"living_room": 0.0, "kitchen": 40.0})

    sendspin.set_player_static_delay.assert_not_awaited()


async def test_a_member_whose_output_was_handed_over_is_refused() -> None:
    """
    A bridged speaker that another protocol took over mid-session has to be measured again.

    Its measurement was taken over the carrier before. Each carrier has a latency and a
    static delay of its own, so writing the result would correct the wrong one.
    """
    provider = await _setup_provider()
    sendspin = _stub_sendspin(
        provider,
        {"lr_client": 0, "kitchen_client": 0},
        session_members={"living_room": "lr_client", "kitchen": "kitchen_client"},
    )
    sendspin.get_player_static_delay_carrier = MagicMock(return_value="airplay")
    await _run_real_session(provider, ["living_room", "kitchen"])
    sendspin.get_player_static_delay_carrier = MagicMock(
        side_effect=lambda protocol_id: (
            "squeezelite" if protocol_id == "kitchen_client" else "airplay"
        )
    )

    with pytest.raises(ActionUnavailable):
        await provider.apply_measurements({"living_room": 0.0, "kitchen": 40.0})

    sendspin.set_player_static_delay.assert_not_awaited()


async def test_measurements_are_refused_when_sendspin_is_gone() -> None:
    """Without the Sendspin provider there is nothing to apply the correction through."""
    provider = await _setup_provider()
    await _run_session(provider, ["a"])
    _mock_mass(provider).get_provider = MagicMock(return_value=None)

    with pytest.raises(ActionUnavailable):
        await provider.apply_measurements({"a": 0.0})


async def test_a_full_cycle_leaves_the_measured_speakers_on_their_correction() -> None:
    """
    Start, measure, apply and stop: the speakers end on the delays the run computed.

    The applied correction supersedes the zero the session put in place, so the restore
    may not put the pre-session delay back over it - that value is exactly what the
    correction was computed to replace.
    """
    provider = await _setup_provider()
    delays_ms = {"a": 250, "b": 0}
    _stub_sendspin(provider, delays_ms)
    await _run_real_session(provider, ["a", "b"])
    assert delays_ms == {"a": 0, "b": 0}

    result = await provider.apply_measurements({"a": 10.0, "b": 55.0})
    await provider.stop_session()

    assert result.applied == {"a": 0, "b": 45}
    # what each speaker carried before the session, which is what undoes the correction
    assert result.previous == {"a": 250, "b": 0}
    assert delays_ms == {"a": 0, "b": 45}


async def test_a_session_that_ends_without_an_apply_gives_every_delay_back() -> None:
    """A run the user abandons leaves the speakers exactly as it found them."""
    provider = await _setup_provider()
    delays_ms = {"a": 250, "b": 30}
    _stub_sendspin(provider, delays_ms)
    await _run_real_session(provider, ["a", "b"])

    await provider.stop_session()

    assert delays_ms == {"a": 250, "b": 30}


async def test_a_refused_apply_leaves_every_delay_to_be_restored() -> None:
    """Nothing is written, so nothing supersedes the restore and every speaker goes back."""
    provider = await _setup_provider()
    delays_ms = {"a": 250, "b": 30}
    _stub_sendspin(provider, delays_ms)
    await _run_real_session(provider, ["a", "b"])

    with pytest.raises(InvalidDataError):
        await provider.apply_measurements({"a": 0.0, "b": 99_000.0})
    await provider.stop_session()

    assert delays_ms == {"a": 250, "b": 30}


async def test_a_speaker_the_correction_could_not_reach_goes_back_to_what_it_had() -> None:
    """
    A member reported for the user to adjust by hand keeps the delay it started with.

    Its result is an increment on whatever its firmware applies, not an absolute MA
    wrote, so there is nothing on the speaker for the restore to overwrite.
    """
    provider = await _setup_provider()
    delays_ms = {"a": 250, "fixed": 0}
    _stub_sendspin(provider, delays_ms, non_adjustable={"fixed"})
    await _run_real_session(provider, ["a", "fixed"])

    result = await provider.apply_measurements({"a": 10.0, "fixed": 55.0})
    await provider.stop_session()

    assert result.manual == {"fixed": 45}
    # 'fixed' was never written to, on the way in or out; 'a' keeps its correction
    assert delays_ms == {"a": 0, "fixed": 0}


async def test_a_speaker_that_stopped_accepting_a_delay_mid_session_gets_it_back() -> None:
    """
    A client whose support flapped across a reconnect is reported by hand and restored.

    Its correction is an increment on whatever the device now applies rather than an
    absolute MA wrote, so nothing superseded the delay the session took off it.
    """
    provider = await _setup_provider()
    delays_ms = {"a": 0, "b": 250}
    sendspin = _stub_sendspin(provider, delays_ms)
    await _run_real_session(provider, ["a", "b"])
    assert delays_ms == {"a": 0, "b": 0}
    sendspin.supports_player_static_delay = MagicMock(side_effect=lambda p: p != "b")

    result = await provider.apply_measurements({"a": 0.0, "b": 30.0})
    await provider.stop_session()

    assert result.manual == {"b": 30}
    assert delays_ms == {"a": 0, "b": 250}


async def test_a_timed_out_session_gives_the_delays_back() -> None:
    """A phone that walks away may not leave the speakers measuring from zero."""
    provider = await _setup_provider()
    delays_ms = {"a": 250}
    _stub_sendspin(provider, delays_ms)
    await _run_real_session(provider, ["a"])

    await provider._handle_session_timeout()

    assert delays_ms == {"a": 250}
    assert await provider.get_session() is None


async def test_unloading_the_plugin_gives_the_delays_back() -> None:
    """Nor may a reload of the plugin itself."""
    provider = await _setup_provider()
    delays_ms = {"a": 250}
    _stub_sendspin(provider, delays_ms)
    await _run_real_session(provider, ["a"])

    await provider.unload()

    assert delays_ms == {"a": 250}


def _stub_session(player_ids: list[str] | None = None) -> MagicMock:
    """Return a stub calibration session that reports itself live."""
    session = MagicMock()
    session.queue_id = "anchor"
    session.stopped = False
    session.player_ids = player_ids if player_ids is not None else ["player"]
    session.begin = AsyncMock()
    session.solo = AsyncMock()
    session.stop = AsyncMock()
    session.static_delay_before = MagicMock(return_value=None)
    return session


async def _run_session(provider: SendspinSyncProvider, player_ids: list[str]) -> MagicMock:
    """Put a stub session over the given players in place on the provider."""
    session = _stub_session(player_ids)
    with patch.object(CalibrationSession, "create", AsyncMock(return_value=session)):
        await provider.start_session(player_ids)
    return session


async def _run_real_session(provider: SendspinSyncProvider, player_ids: list[str]) -> None:
    """
    Start an actual calibration session over the given players, on a stubbed anchor.

    Unlike :func:`_run_session` this drives the real session, so the static delays it
    zeroes on the way in and puts back at teardown are exercised too.

    :param provider: The plugin provider to start the session on.
    :param player_ids: The players to calibrate. Register them first with
        :func:`_stub_sendspin`.
    """
    mass = _mock_mass(provider)
    for command in ("cmd_volume_mute", "cmd_volume_set", "cmd_set_members", "cmd_power"):
        setattr(mass.players, command, AsyncMock())
    mass.players.cmd_play = AsyncMock()
    mass.players.cmd_resume = AsyncMock()
    mass.player_queues.play_media = AsyncMock()
    mass.player_queues.stop = AsyncMock()
    mass.player_queues.get = MagicMock(return_value=None)
    with patch(
        "music_assistant.providers.sendspin_sync.session.SharedPlaybackSession"
    ) as shared_cls:
        shared_cls.create_remote = AsyncMock(return_value=_stub_anchor())
        await provider.start_session(player_ids)


def _stub_anchor() -> MagicMock:
    """Return a stub of the shared playback session a calibration group is anchored on."""
    anchor = MagicMock()
    anchor.player_id = "calibration_anchor"
    anchor.queue_id = "calibration_anchor"
    anchor.add_guest_listener = AsyncMock()
    anchor.remove_guest_listener = AsyncMock()
    anchor.close = AsyncMock()
    return anchor


def _stub_sendspin(
    provider: SendspinSyncProvider,
    current_delays_ms: dict[str, int],
    non_adjustable: set[str] | None = None,
    session_members: dict[str, str] | None = None,
) -> MagicMock:
    """
    Install a stub Sendspin provider that carries the given static delays.

    Reads and writes hit the same mapping, so a second ``apply_measurements`` sees what
    the first one applied - which is what makes the convergence test meaningful.

    Also registers the session's players, since an apply resolves each member to the
    Sendspin player its delay is written to.

    :param provider: The plugin provider to install the stub behind.
    :param current_delays_ms: The static delay each *Sendspin* player starts out carrying.
    :param non_adjustable: Sendspin players whose client refuses a static delay. Reading
        one raises, exactly as the real provider does.
    :param session_members: Session member id -> the Sendspin player behind it, for
        wrapped physical speakers. Defaults to a web player per delay entry, which is
        its own Sendspin endpoint.
    """
    refused = non_adjustable or set()
    _stub_players(
        provider,
        session_members or {player_id: player_id for player_id in current_delays_ms},
    )

    def _get(player_id: str) -> int:
        if player_id in refused:
            raise UnsupportedFeaturedException(player_id)
        return current_delays_ms[player_id]

    sendspin = MagicMock()
    sendspin.is_virtual_player = MagicMock(return_value=False)
    sendspin.supports_player_static_delay = MagicMock(side_effect=lambda p: p not in refused)
    sendspin.get_player_static_delay = MagicMock(side_effect=_get)
    sendspin.get_player_static_delay_carrier = MagicMock(return_value=None)
    sendspin.set_player_static_delay = AsyncMock(
        side_effect=lambda player_id, delay_ms, **_kwargs: current_delays_ms.__setitem__(
            player_id, delay_ms
        )
    )
    _mock_mass(provider).get_provider = MagicMock(return_value=sendspin)
    return sendspin


def _stub_players(provider: SendspinSyncProvider, session_members: dict[str, str]) -> None:
    """
    Register visible players that all_players offers and a session can resolve.

    Each is eligible and resolves to the Sendspin player its static delay is written to.

    :param provider: The plugin provider whose stub MusicAssistant should hold them.
    :param session_members: Session member id -> the Sendspin player behind it. A member
        mapped to itself is a web player; any other mapping is a physical speaker, whose
        visible player wraps a hidden Sendspin client of that id.
    """
    registry: dict[str, MagicMock] = {}
    for player_id, sendspin_id in session_members.items():
        player = MagicMock()
        player.player_id = player_id
        player.state.name = player_id
        player.state.available = True
        player.state.type = PlayerType.PLAYER
        player.state.playback_state = PlaybackState.IDLE
        player.state.mute_control = "mute_control"
        player.state.volume_control = "volume_control"
        # the rest of the state a real session snapshots and restores, so a test can
        # drive one end to end rather than against a stub session
        player.state.powered = True
        player.state.power_control = "power_control"
        player.state.volume_muted = False
        player.state.volume_level = 42
        player.state.synced_to = None
        player.state.active_group = None
        player.state.active_source = None
        player.state.current_media = None
        player.state.supported_features = set()
        player.extra_data = {}
        client = MagicMock()
        client.protocol_id = sendspin_id
        protocol = OutputProtocol(
            # a web player renders through its own protocol, a speaker through a linked one
            output_protocol_id="native" if sendspin_id == player_id else sendspin_id,
            name="Sendspin",
            protocol_domain=SENDSPIN_DOMAIN,
        )
        player.get_output_protocol_by_domain = MagicMock(
            side_effect=lambda domain, found=protocol: found if domain == SENDSPIN_DOMAIN else None
        )
        player.get_output_protocol = MagicMock(return_value=client)
        registry[player_id] = player
    mass = _mock_mass(provider)
    mass.players.get_player = MagicMock(side_effect=registry.get)
    mass.players.all_players = MagicMock(return_value=list(registry.values()))


def _applied_delays(sendspin: MagicMock) -> dict[str, int]:
    """Return the last static delay applied per player through the Sendspin provider."""
    return {call.args[0]: call.args[1] for call in sendspin.set_player_static_delay.await_args_list}


async def _setup_provider() -> SendspinSyncProvider:
    """Return a fully initialized provider backed by mocked MA plumbing."""
    manifest = MagicMock()
    manifest.domain = "sendspin_sync"
    config = MagicMock()
    config.get_value = MagicMock(return_value="GLOBAL")
    config.name = "Sendspin Sync"
    config.instance_id = "sendspin_sync--test"
    provider = await setup(MagicMock(), manifest, config)
    assert isinstance(provider, SendspinSyncProvider)
    await provider.handle_async_init()
    return provider


def _mock_mass(provider: SendspinSyncProvider) -> MagicMock:
    """Return the stub MusicAssistant the given provider was set up with."""
    return cast("MagicMock", provider.mass)


async def _take_bytes(provider: SendspinSyncProvider, count: int) -> bytes:
    """Return the first ``count`` bytes the provider's audio stream yields."""
    collected = bytearray()
    async with aclosing(provider.get_audio_stream(MagicMock())) as stream:
        async for chunk in stream:
            collected += chunk
            if len(collected) >= count:
                break
    return bytes(collected[:count])
