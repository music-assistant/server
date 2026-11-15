"""Tests for the derivation of the state a player publishes."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from music_assistant_models.audio_processing import ActiveSourceAudioDetails
from music_assistant_models.constants import (
    PLAYER_CONTROL_FAKE,
    PLAYER_CONTROL_NATIVE,
    PLAYER_CONTROL_NONE,
)
from music_assistant_models.enums import (
    MediaType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
    ProtocolRole,
    RepeatMode,
    SleepTimerMode,
)
from music_assistant_models.media_items import (
    AudioFormat,
    AudioSource,
    ItemMapping,
    PodcastEpisode,
    ProviderMapping,
    Track,
)
from music_assistant_models.player import (
    DeviceInfo,
    OutputProtocol,
    PlayerMedia,
    PlayerOption,
    PlayerOptionType,
    PlayerProtocolInfo,
    PlayerSoundMode,
    PlayerSource,
)
from music_assistant_models.player import Player as State
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.streamdetails import StreamDetails, StreamMetadata
from music_assistant_models.unique_list import UniqueList

from music_assistant.controllers.players.audio_sources import AudioSourceSession
from music_assistant.models.player_state import (
    GroupSnapshot,
    PeerSnapshot,
    ProtocolSnapshot,
    QueueSnapshot,
    StateEffects,
    derive_player_state,
    reconcile_position_anchor,
    resolve_media_queue,
)
from tests.common import (
    make_group_snapshot,
    make_peer_snapshot,
    make_player_state,
    make_protocol_snapshot,
    make_state_inputs,
)

PLAYER_ID = "player_1"
NOW = 1000.0
SOURCE_URI = "spotify_connect--test://audio_source/main"


def _derive(**values: Any) -> State:
    """Return the state derived from the inputs of an idle player with the given values."""
    state, _ = derive_player_state(make_state_inputs(**values))
    return state


def _effects(**values: Any) -> StateEffects:
    """Return the effects of deriving the state of an idle player with the given values."""
    _, effects = derive_player_state(make_state_inputs(**values))
    return effects


def _own(**values: Any) -> ProtocolSnapshot:
    """Return what the player's own provider object reports."""
    return make_protocol_snapshot(PLAYER_ID, **values)


def _peer(player_id: str, **values: Any) -> PeerSnapshot:
    """
    Return another player, with the given reported and published values.

    :param player_id: Id of the player.
    :param values: Values of the snapshot; any other value is one of its published state.
    """
    snapshot_keys = PeerSnapshot.__dataclass_fields__.keys()
    snapshot_values = {key: values.pop(key) for key in list(values) if key in snapshot_keys}
    return make_peer_snapshot(player_id, make_player_state(player_id, **values), **snapshot_values)


def _group(group_id: str = "group_1", **values: Any) -> GroupSnapshot:
    """
    Return a powered group player that holds the player.

    :param group_id: Id of the group player.
    :param values: Values of the snapshot; any other value is one of its published state.
    """
    values = {"powered": True, "group_members": [PLAYER_ID]} | values
    snapshot_keys = GroupSnapshot.__dataclass_fields__.keys()
    snapshot_values = {key: values.pop(key) for key in list(values) if key in snapshot_keys}
    return make_group_snapshot(group_id, make_player_state(group_id, **values), **snapshot_values)


def _group_peer(group: GroupSnapshot) -> PeerSnapshot:
    """Return a group player the way the players that refer to it depend on it."""
    return make_peer_snapshot(group.player_id, group.state, type=PlayerType.GROUP)


def _queue(queue_id: str = PLAYER_ID, **values: Any) -> QueueSnapshot:
    """
    Return a queue.

    :param queue_id: Id of the queue.
    :param values: Values of the snapshot; any other value is one of the queue itself.
    """
    item_image_url = values.pop("item_image_url", None)
    media_item_image_url = values.pop("media_item_image_url", None)
    queue_values: dict[str, Any] = {
        "active": True,
        "display_name": queue_id,
        "available": True,
        "items": 1,
        "elapsed_time": 0,
        "elapsed_time_last_updated": NOW,
    }
    return QueueSnapshot(
        queue=PlayerQueue(queue_id=queue_id, **(queue_values | values)),
        item_image_url=item_image_url,
        media_item_image_url=media_item_image_url,
    )


def _queue_item(**values: Any) -> QueueItem:
    """Return an item of the player's queue."""
    item_values: dict[str, Any] = {"queue_item_id": "item_1", "name": "Item", "duration": 200}
    return QueueItem(queue_id=PLAYER_ID, **(item_values | values))


def _audio_source(**values: Any) -> AudioSource:
    """Return a live source as a plugin declares it."""
    return AudioSource(
        item_id="main",
        provider="spotify_connect--test",
        name="Spotify Connect",
        provider_mappings={
            ProviderMapping(
                item_id="main",
                provider_domain="spotify_connect",
                provider_instance="spotify_connect--test",
            )
        },
        **values,
    )


def _session(source: AudioSource | None = None, **values: Any) -> AudioSourceSession:
    """Return a live source session on the player."""
    return AudioSourceSession(
        player_id=PLAYER_ID,
        source=source or _audio_source(),
        provider_instance_id="spotify_connect--test",
        **values,
    )


def _app_protocol(**values: Any) -> ProtocolSnapshot:
    """Return an audio protocol that plays an app Music Assistant did not start."""
    app_values: dict[str, Any] = {
        "playback_state": PlaybackState.PLAYING,
        "elapsed_time": 12.0,
        "elapsed_time_last_updated": NOW - 10,
        "active_source": "tunein",
        "source_list": [PlayerSource(id="tunein", name="TuneIn", passive=True, can_seek=True)],
        "current_media": PlayerMedia(uri="https://radio.example", title="Radio 538"),
        "supported_features": {PlayerFeature.PAUSE, PlayerFeature.SEEK, PlayerFeature.ENQUEUE},
    }
    return make_protocol_snapshot("cast_1", **(app_values | values))


def _protocol_infos() -> list[PlayerProtocolInfo]:
    """Return the protocols of a player with its own protocol, a Chromecast and an AirPlay one."""
    return [
        PlayerProtocolInfo(
            protocol_id=PLAYER_ID, domain="test", provider="test_prov", role=ProtocolRole.NATIVE
        ),
        PlayerProtocolInfo(
            protocol_id="cast_1",
            domain="chromecast",
            provider="chromecast",
            role=ProtocolRole.AUDIO,
        ),
        PlayerProtocolInfo(
            protocol_id="airplay_1", domain="airplay", provider="airplay", role=ProtocolRole.AUDIO
        ),
    ]


def _active_ids(state: State) -> list[str]:
    """Return the ids of the protocols the state marks as active."""
    return [protocol.protocol_id for protocol in state.protocols if protocol.active]


def _source(state: State, source_id: str) -> PlayerSource | None:
    """Return the entry with the given id from the published source list."""
    return next((x for x in state.source_list if x.id == source_id), None)


class TestPublishedValues:
    """Values that are published the way they come in."""

    def test_config_and_reported_values_are_published_as_they_are(self) -> None:
        """What needs no derivation reaches the state unchanged."""
        device_info = DeviceInfo(model="Model", manufacturer="Maker")
        sound_modes = UniqueList([PlayerSoundMode(id="movie", name="Movie")])
        options = UniqueList(
            [
                PlayerOption(
                    key="night_mode", name="Night mode", type=PlayerOptionType.BOOLEAN, value=True
                )
            ]
        )
        output_protocols = [
            OutputProtocol(output_protocol_id="native", name="Test", protocol_domain="test")
        ]

        state = _derive(
            provider="test_prov--1",
            player_type=PlayerType.STEREO_PAIR,
            name="Living room",
            enabled=True,
            hide_in_ui=True,
            expose_to_ha=False,
            icon="soundbar",
            legacy_ids=["old_player_1"],
            device_info=device_info,
            private=True,
            active_sound_mode="movie",
            sound_mode_list=sound_modes,
            options=options,
            static_group_members=["a", "b"],
            has_setup_flow=True,
            output_protocols=output_protocols,
            active_output_protocol="native",
            sleep_timer_expires_at=NOW + 60,
            sleep_timer_mode=SleepTimerMode.DURATION,
            extra_attributes={"key": "value"},
            own=_own(
                needs_setup=True, setup_reason="pairing_required", available_for_playback=False
            ),
        )

        assert state.player_id == PLAYER_ID
        assert state.provider == "test_prov--1"
        assert state.type == PlayerType.STEREO_PAIR
        assert state.name == "Living room"
        assert state.hide_in_ui is True
        assert state.expose_to_ha is False
        assert state.icon == "soundbar"
        assert state.legacy_ids == ["old_player_1"]
        assert state.device_info is device_info
        assert state.private is True
        assert state.active_sound_mode == "movie"
        assert state.sound_mode_list == sound_modes
        assert state.options == options
        assert state.static_group_members == ["a", "b"]
        assert state.has_setup_flow is True
        assert state.output_protocols == output_protocols
        assert state.active_output_protocol == "native"
        assert state.sleep_timer_expires_at == NOW + 60
        assert state.sleep_timer_mode == SleepTimerMode.DURATION
        assert state.extra_attributes == {"key": "value"}
        assert state.needs_setup is True
        assert state.setup_reason == "pairing_required"

    def test_control_states_are_published_as_they_are(self) -> None:
        """Power, volume and mute are what the controls they are resolved to report."""
        state = _derive(
            power_control=PLAYER_CONTROL_NATIVE,
            volume_control="volume_ctrl",
            mute_control=PLAYER_CONTROL_FAKE,
            powered=True,
            volume_level=35,
            volume_muted=False,
        )

        assert (state.power_control, state.powered) == (PLAYER_CONTROL_NATIVE, True)
        assert (state.volume_control, state.volume_level) == ("volume_ctrl", 35)
        assert (state.mute_control, state.volume_muted) == (PLAYER_CONTROL_FAKE, False)


class TestAvailability:
    """The availability clients see."""

    @pytest.mark.parametrize(
        ("enabled", "reachable", "switched_off", "needs_setup", "available"),
        [
            (True, True, False, False, True),
            (False, True, False, False, False),
            (True, False, False, False, False),
            (True, False, True, False, True),
            (True, True, False, True, False),
            (True, False, True, True, False),
        ],
        ids=[
            "reachable",
            "disabled",
            "unreachable",
            "switched_off_at_its_power_control",
            "needs_setup",
            "switched_off_and_needs_setup",
        ],
    )
    def test_player_is_available_when_enabled_usable_and_set_up(
        self, enabled: bool, reachable: bool, switched_off: bool, needs_setup: bool, available: bool
    ) -> None:
        """An enabled, set up player is available while reachable or merely switched off."""
        state = _derive(
            enabled=enabled,
            offline_by_power_control=switched_off,
            own=_own(
                available=reachable,
                needs_setup=needs_setup,
                available_for_playback=reachable and not needs_setup,
            ),
        )

        assert state.available is available
        assert state.enabled is enabled

    @pytest.mark.parametrize("enabled", [True, False])
    @pytest.mark.parametrize("reachable", [True, False])
    @pytest.mark.parametrize("needs_setup", [True, False])
    def test_player_without_a_power_control_is_available_exactly_while_it_can_be_played_to(
        self, enabled: bool, reachable: bool, needs_setup: bool
    ) -> None:
        """Without a power control, available and available for playback say the same."""
        available_for_playback = reachable and not needs_setup
        state = _derive(
            enabled=enabled,
            offline_by_power_control=False,
            own=_own(
                available=reachable,
                needs_setup=needs_setup,
                available_for_playback=available_for_playback,
            ),
        )

        assert state.available is (enabled and available_for_playback)


class TestSyncedTo:
    """The player a player is synced to."""

    def test_player_names_the_leader_it_reports(self) -> None:
        """A player that reports its sync leader is synced to that player."""
        state = _derive(own=_own(synced_to="leader"), peers={"leader": _peer("leader")})

        assert state.synced_to == "leader"

    def test_leader_named_by_its_protocol_is_the_player_of_that_protocol(self) -> None:
        """Clients only know players, so a leading protocol shows as the player it belongs to."""
        state = _derive(
            own=_own(synced_to="airplay_leader"), peers={"airplay_leader": _peer("leader")}
        )

        assert state.synced_to == "leader"

    def test_unknown_leader_is_named_as_reported(self) -> None:
        """A leader that is not registered is still what the player is synced to."""
        state = _derive(own=_own(synced_to="gone"))

        assert state.synced_to == "gone"

    def test_linked_protocol_synced_to_a_player_names_that_player(self) -> None:
        """A sync of an audio protocol of the player is a sync of the player."""
        state = _derive(
            linked=(make_protocol_snapshot("airplay_1", synced_to="leader"),),
            peers={"leader": _peer("leader")},
        )

        assert state.synced_to == "leader"

    def test_linked_protocol_synced_to_a_protocol_names_its_player(self) -> None:
        """A protocol leading the sync shows as the player it belongs to."""
        state = _derive(
            linked=(make_protocol_snapshot("airplay_1", synced_to="airplay_leader"),),
            peers={"airplay_leader": _peer("leader")},
        )

        assert state.synced_to == "leader"

    def test_linked_protocol_synced_to_a_protocol_without_a_player_is_not_synced(self) -> None:
        """A leading protocol that belongs to no player gives nothing to sync to."""
        state = _derive(linked=(make_protocol_snapshot("airplay_1", synced_to="airplay_leader"),))

        assert state.synced_to is None


class TestActiveGroup:
    """The group player that holds a player."""

    @pytest.mark.parametrize(
        ("powered", "is_active_session", "members", "active"),
        [
            (True, False, [PLAYER_ID], True),
            (None, True, [PLAYER_ID], True),
            (None, False, [PLAYER_ID], False),
            (False, True, [PLAYER_ID], False),
            (True, True, ["other"], False),
        ],
        ids=["powered", "active_session", "dormant", "powered_off", "not_a_member"],
    )
    def test_group_holds_its_members_while_powered_or_in_session(
        self, powered: bool | None, is_active_session: bool, members: list[str], active: bool
    ) -> None:
        """A member belongs to a group that is switched on or holds a session."""
        group = _group(powered=powered, is_active_session=is_active_session, group_members=members)

        state = _derive(groups=(group,), peers={"group_1": _group_peer(group)})

        assert state.active_group == ("group_1" if active else None)

    def test_first_holding_group_wins(self) -> None:
        """A player in several active groups belongs to the first one."""
        first, second = _group("group_1"), _group("group_2")

        state = _derive(
            groups=(first, second),
            peers={"group_1": _group_peer(first), "group_2": _group_peer(second)},
        )

        assert state.active_group == "group_1"

    def test_group_does_not_hold_itself(self) -> None:
        """A group player listing its own id is not its own active group."""
        group = _group(PLAYER_ID)

        state = _derive(
            player_type=PlayerType.GROUP, groups=(group,), peers={PLAYER_ID: _group_peer(group)}
        )

        assert state.active_group is None


class TestForeignApp:
    """An app on an audio protocol that Music Assistant did not start."""

    def test_app_shows_on_an_idle_player(self) -> None:
        """Source, state, position, media and controls come from the protocol that plays the app."""
        app = _app_protocol()

        state, effects = derive_player_state(make_state_inputs(linked=(app,), foreign_app=app))

        assert state.active_source == "tunein"
        assert state.playback_state == PlaybackState.PLAYING
        assert state.elapsed_time == 12.0
        assert state.elapsed_time_last_updated == NOW - 10
        assert state.current_media is app.current_media
        source = _source(state, "tunein")
        assert source is not None
        assert source.can_seek
        assert PlayerFeature.SEEK in state.supported_features
        # what follows the audio MA plays is not taken over
        assert PlayerFeature.ENQUEUE not in state.supported_features
        assert effects.foreign_protocol_id == "cast_1"

    def test_app_replaces_the_same_service_among_own_sources(self) -> None:
        """The player's own entry for the service does not offer what the app does."""
        app = _app_protocol()

        state = _derive(
            own=_own(source_list=[PlayerSource(id="tunein", name="Own TuneIn", passive=True)]),
            linked=(app,),
            foreign_app=app,
        )

        source = _source(state, "tunein")
        assert source is not None
        assert source.name == "TuneIn"
        assert [x.id for x in state.source_list].count("tunein") == 1

    @pytest.mark.parametrize(
        ("own_state", "app_state", "shown"),
        [
            (PlaybackState.PLAYING, PlaybackState.PLAYING, False),
            (PlaybackState.PAUSED, PlaybackState.PAUSED, False),
            (PlaybackState.PAUSED, PlaybackState.PLAYING, True),
            (PlaybackState.IDLE, PlaybackState.PAUSED, True),
        ],
        ids=["player_plays", "both_paused", "app_plays_over_paused", "paused_app"],
    )
    def test_app_shows_only_when_the_player_plays_nothing_itself(
        self, own_state: PlaybackState, app_state: PlaybackState, shown: bool
    ) -> None:
        """The player's own playback outranks an app, unless an app plays over a paused player."""
        app = _app_protocol(playback_state=app_state)

        effects = _effects(own=_own(playback_state=own_state), linked=(app,), foreign_app=app)

        assert effects.foreign_protocol_id == ("cast_1" if shown else None)

    @pytest.mark.parametrize(
        "values",
        [
            {"active_output_protocol": "cast_1"},
            {"own": _own(synced_to="leader"), "peers": {"leader": _peer("leader")}},
            {"groups": (_group(),), "peers": {"group_1": _group_peer(_group())}},
            {"session": _session()},
        ],
        ids=["output_protocol_active", "synced", "grouped", "live_source"],
    )
    def test_app_does_not_show_while_the_player_follows_something_else(
        self, values: dict[str, Any]
    ) -> None:
        """A player that plays through a protocol, a group, a leader or a source keeps doing so."""
        app = _app_protocol()

        state, effects = derive_player_state(
            make_state_inputs(linked=(app,), foreign_app=app, **values)
        )

        assert effects.foreign_protocol_id is None
        assert state.active_source != "tunein"

    def test_native_output_does_not_hide_the_app(self) -> None:
        """An explicitly native output is still the player itself playing nothing."""
        app = _app_protocol()

        effects = _effects(active_output_protocol="native", linked=(app,), foreign_app=app)

        assert effects.foreign_protocol_id == "cast_1"


class TestActiveProtocol:
    """The protocol that is marked as the current playback path of the player."""

    def test_shown_app_marks_its_protocol(self) -> None:
        """The protocol whose app the player shows is its only active protocol."""
        app = _app_protocol()

        state = _derive(protocols=_protocol_infos(), linked=(app,), foreign_app=app)

        assert _active_ids(state) == ["cast_1"]

    def test_shown_app_is_marked_over_an_idle_native_output(self) -> None:
        """An app shown while the native output plays nothing is marked, not the own protocol."""
        app = _app_protocol()

        state = _derive(
            protocols=_protocol_infos(),
            active_output_protocol="native",
            linked=(app,),
            foreign_app=app,
        )

        assert _active_ids(state) == ["cast_1"]
        assert state.active_output_protocol == "native"

    @pytest.mark.parametrize(
        ("values", "active"),
        [
            ({"active_output_protocol": "airplay_1"}, ["airplay_1"]),
            ({"own": _own(playback_state=PlaybackState.PLAYING)}, [PLAYER_ID]),
            (
                {
                    "own": _own(playback_state=PlaybackState.PAUSED),
                    "foreign_app": _app_protocol(playback_state=PlaybackState.PAUSED),
                },
                [PLAYER_ID],
            ),
            ({"own": _own(synced_to="leader"), "peers": {"leader": _peer("leader")}}, [PLAYER_ID]),
            ({"groups": (_group(),), "peers": {"group_1": _group_peer(_group())}}, [PLAYER_ID]),
            ({"session": _session()}, [PLAYER_ID]),
        ],
        ids=[
            "other_output_active",
            "player_plays",
            "both_paused",
            "synced",
            "grouped",
            "live_source",
        ],
    )
    def test_app_that_is_not_shown_changes_no_marking(
        self, values: dict[str, Any], active: list[str]
    ) -> None:
        """An app the player does not show leaves the protocol of the active output marked."""
        app = _app_protocol()

        state = _derive(
            **(
                {
                    "protocols": _protocol_infos(),
                    "active_output_protocol": "native",
                    "linked": (app,),
                    "foreign_app": app,
                }
                | values
            )
        )

        assert _active_ids(state) == active

    @pytest.mark.parametrize(
        ("active_output_protocol", "active"),
        [(None, []), ("native", [PLAYER_ID]), ("airplay_1", ["airplay_1"])],
        ids=["no_output", "native_output", "protocol_output"],
    )
    def test_without_an_app_the_active_output_is_marked(
        self, active_output_protocol: str | None, active: list[str]
    ) -> None:
        """The protocol of the active output is marked, the own one for a native output."""
        state = _derive(protocols=_protocol_infos(), active_output_protocol=active_output_protocol)

        assert _active_ids(state) == active


class TestPlaybackPosition:
    """The playback state and position of a player."""

    def test_player_reports_its_own_playback(self) -> None:
        """A player on its own publishes what its device reports."""
        state = _derive(
            own=_own(
                playback_state=PlaybackState.PLAYING,
                elapsed_time=5.0,
                elapsed_time_last_updated=NOW - 1,
            )
        )

        assert state.playback_state == PlaybackState.PLAYING
        assert state.elapsed_time == 5.0
        assert state.elapsed_time_last_updated == NOW - 1

    def test_active_output_protocol_is_the_source_of_truth(self) -> None:
        """While an audio protocol renders the audio, the playback it reports is the player's."""
        protocol = make_protocol_snapshot(
            "airplay_1",
            playback_state=PlaybackState.PLAYING,
            elapsed_time=30.0,
            elapsed_time_last_updated=NOW - 5,
        )

        state = _derive(
            active_output_protocol="airplay_1", active_protocol=protocol, linked=(protocol,)
        )

        assert state.playback_state == PlaybackState.PLAYING
        assert state.elapsed_time == 30.0
        assert state.elapsed_time_last_updated == NOW - 5

    def test_unregistered_output_protocol_leaves_the_own_playback(self) -> None:
        """An active output that is gone has nothing to report."""
        state = _derive(
            active_output_protocol="airplay_1", own=_own(playback_state=PlaybackState.PAUSED)
        )

        assert state.playback_state == PlaybackState.PAUSED

    def test_synced_player_mirrors_its_leader(self) -> None:
        """A synced player reports the same playback as its leader."""
        state = _derive(
            own=_own(synced_to="leader", playback_state=PlaybackState.IDLE),
            peers={
                "leader": _peer(
                    "leader",
                    playback_state=PlaybackState.PLAYING,
                    elapsed_time=8.0,
                    elapsed_time_last_updated=NOW - 2,
                )
            },
        )

        assert state.playback_state == PlaybackState.PLAYING
        assert state.elapsed_time == 8.0
        assert state.elapsed_time_last_updated == NOW - 2

    def test_live_source_reports_its_own_position(self) -> None:
        """The position a live source reports replaces the position of the device."""
        metadata = StreamMetadata(title="Song", elapsed_time=42, elapsed_time_last_updated=NOW - 3)

        state = _derive(
            own=_own(elapsed_time=600.0, elapsed_time_last_updated=NOW - 30),
            session=_session(stream_metadata=metadata),
        )

        assert state.elapsed_time == 42
        assert state.elapsed_time_last_updated == NOW - 3

    def test_live_source_position_without_a_timestamp_is_taken_now(self) -> None:
        """A position that comes without its timestamp counts from the moment it is read."""
        state = _derive(
            session=_session(stream_metadata=StreamMetadata(title="Song", elapsed_time=42))
        )

        assert state.elapsed_time == 42
        assert state.elapsed_time_last_updated == NOW

    def test_live_source_without_a_position_leaves_the_player_position(self) -> None:
        """A source that reports no position does not take the device's position away."""
        state = _derive(
            own=_own(elapsed_time=7.0, elapsed_time_last_updated=NOW - 1),
            session=_session(stream_metadata=StreamMetadata(title="Song")),
        )

        assert state.elapsed_time == 7.0
        assert state.elapsed_time_last_updated == NOW - 1

    @pytest.mark.parametrize(
        "values",
        [
            {
                "own": _own(synced_to="leader"),
                "peers": {"leader": _peer("leader", elapsed_time=8.0)},
            },
            {
                "groups": (_group(),),
                "peers": {"group_1": _group_peer(_group())},
                "own": _own(elapsed_time=8.0),
            },
        ],
        ids=["synced", "grouped"],
    )
    def test_live_source_position_is_only_for_the_player_playing_the_source(
        self, values: dict[str, Any]
    ) -> None:
        """A player hearing another player's audio keeps the position it took from there."""
        metadata = StreamMetadata(title="Song", elapsed_time=42, elapsed_time_last_updated=NOW)

        state = _derive(session=_session(stream_metadata=metadata), **values)

        assert state.elapsed_time == 8.0

    def test_player_switched_off_at_its_power_control_plays_nothing(self) -> None:
        """A device cut off mid-song may still report the state it had."""
        state = _derive(
            offline_by_power_control=True, own=_own(playback_state=PlaybackState.PLAYING)
        )

        assert state.playback_state == PlaybackState.IDLE


class TestActiveSource:
    """The active source of a player."""

    def test_idle_player_has_its_own_queue_as_source(self) -> None:
        """Without anything else going on, the player's own queue is its source."""
        assert _derive().active_source == PLAYER_ID

    def test_grouped_player_takes_the_source_of_its_group(self) -> None:
        """A group member plays what its group plays."""
        group = _group(active_source="group_1")

        state = _derive(
            groups=(group,),
            peers={"group_1": _group_peer(group)},
            own=_own(active_source="spotify"),
        )

        assert state.active_source == "group_1"

    def test_synced_player_takes_the_source_of_its_leader(self) -> None:
        """A synced player plays what its leader plays."""
        state = _derive(
            own=_own(synced_to="leader"),
            peers={"leader": _peer("leader", active_source="leader")},
        )

        assert state.active_source == "leader"

    def test_sync_leader_outranks_the_group(self) -> None:
        """A synced member of an active group reports the source of its sync leader."""
        group = _group(active_source="group_1")

        state = _derive(
            own=_own(synced_to="leader"),
            groups=(group,),
            peers={
                "group_1": _group_peer(group),
                "leader": _peer("leader", active_source="leader"),
            },
        )

        assert state.active_source == "leader"

    def test_player_synced_to_an_unknown_leader_has_no_source(self) -> None:
        """Without the leader there is no source to take over."""
        assert _derive(own=_own(synced_to="gone")).active_source is None

    def test_live_source_outranks_what_the_device_reports(self) -> None:
        """A live source Music Assistant put on the player is what it plays."""
        state = _derive(
            session=_session(),
            active_mass_source=PLAYER_ID,
            own=_own(active_source="tv", playback_state=PlaybackState.PLAYING),
        )

        assert state.active_source == SOURCE_URI

    def test_remembered_ma_source_outranks_an_unlisted_reported_source(self) -> None:
        """Many devices report their source inaccurately, so the MA source stays in place."""
        state = _derive(
            active_mass_source="queue_x",
            own=_own(active_source="network", playback_state=PlaybackState.PLAYING),
        )

        assert state.active_source == "queue_x"

    def test_known_external_source_takes_the_player_over(self) -> None:
        """A source that is external for certain replaces the remembered MA source."""
        state = _derive(
            active_mass_source="queue_x",
            own=_own(active_source="Spotify", playback_state=PlaybackState.PLAYING),
        )

        assert state.active_source == "Spotify"

    @pytest.mark.parametrize(
        ("trusted", "listed", "source"),
        [(True, True, "qplay"), (False, True, "queue_x"), (True, False, "queue_x")],
        ids=["trusted_and_listed", "not_trusted", "not_listed"],
    )
    def test_listed_source_takes_over_only_on_a_player_that_reports_accurately(
        self, trusted: bool, listed: bool, source: str
    ) -> None:
        """A source on the player's own list counts for a provider that vouches for it."""
        state = _derive(
            active_mass_source="queue_x",
            trusts_reported_source=trusted,
            own=_own(
                active_source="qplay",
                playback_state=PlaybackState.PLAYING,
                source_list=[PlayerSource(id="qplay", name="QPlay")] if listed else [],
            ),
        )

        assert state.active_source == source

    def test_reported_source_counts_while_the_player_plays_it(self) -> None:
        """Without a remembered MA source, the source the device reports is the active one."""
        state = _derive(own=_own(active_source="network", playback_state=PlaybackState.PAUSED))

        assert state.active_source == "network"

    @pytest.mark.parametrize(
        "values",
        [
            {"own": _own(active_source="network", playback_state=PlaybackState.IDLE)},
            {
                "own": _own(active_source="network", playback_state=PlaybackState.PLAYING),
                "active_output_protocol": "airplay_1",
            },
        ],
        ids=["idle", "output_protocol_active"],
    )
    def test_reported_source_is_overruled(self, values: dict[str, Any]) -> None:
        """An idle player, or one playing through a protocol, is not on the source it reports."""
        assert _derive(**values).active_source == PLAYER_ID

    def test_last_ma_source_is_kept_when_nothing_else_is_active(self) -> None:
        """The remembered MA source stays the active one on an idle player."""
        assert _derive(active_mass_source="queue_x").active_source == "queue_x"

    @pytest.mark.parametrize(
        "own",
        [
            _own(active_source="spotify", playback_state=PlaybackState.IDLE),
            _own(
                active_source=PLAYER_ID,
                playback_state=PlaybackState.PLAYING,
                source_list=[PlayerSource(id=PLAYER_ID, name="Native")],
            ),
        ],
        ids=["idle_on_an_external_source", "playing_its_own_listed_queue_source"],
    )
    def test_last_ma_source_comes_back_when_a_takeover_plays_nothing(
        self, own: ProtocolSnapshot
    ) -> None:
        """A takeover that is not something the player plays leaves the remembered MA source."""
        state = _derive(active_mass_source="queue_x", trusts_reported_source=True, own=own)

        assert state.active_source == "queue_x"


class TestActiveSourceAudio:
    """The audio details of the active external source."""

    @staticmethod
    def _details() -> ActiveSourceAudioDetails:
        """Return the audio details of a source."""
        return ActiveSourceAudioDetails(input_format=AudioFormat())

    def test_live_source_details_are_published(self) -> None:
        """The audio details of the live source on the player are part of its state."""
        details = self._details()

        assert _derive(session=_session(active_source_audio=details)).active_source_audio is details

    def test_player_without_a_live_source_has_no_details(self) -> None:
        """Music Assistant's own queue publishes no source audio details."""
        assert _derive().active_source_audio is None

    def test_grouped_player_takes_the_details_of_its_group(self) -> None:
        """A group member publishes the details of what its group plays."""
        details = self._details()
        group = _group(active_source_audio=details)

        state = _derive(groups=(group,), peers={"group_1": _group_peer(group)}, session=_session())

        assert state.active_source_audio is details


class TestSourceList:
    """The sources of a player."""

    def test_filled_queue_advertises_transport_controls(self) -> None:
        """A queue holding items can be played, seeked and skipped through."""
        source = _source(_derive(queues={PLAYER_ID: _queue(items=5)}), PLAYER_ID)

        assert source is not None
        assert source.name == "Music Assistant Queue"
        assert source.passive is False
        assert source.can_play_pause is True
        assert source.can_seek is True
        assert source.can_next_previous is True

    def test_empty_queue_reports_transport_controls_unavailable(self) -> None:
        """An empty queue has nothing to play, so clients can grey the controls out."""
        source = _source(_derive(queues={PLAYER_ID: _queue(items=0)}), PLAYER_ID)

        assert source is not None
        assert source.can_play_pause is False
        assert source.can_seek is False
        assert source.can_next_previous is False

    def test_finished_queue_only_advertises_play(self) -> None:
        """A queue that played to its end can be started over, but has nothing left to seek or skip."""
        source = _source(_derive(queues={PLAYER_ID: _queue(items=5, ended=True)}), PLAYER_ID)

        assert source is not None
        assert source.can_play_pause is True
        assert source.can_seek is False
        assert source.can_next_previous is False

    def test_missing_queue_reports_transport_controls_unavailable(self) -> None:
        """A player without a registered queue behaves like an empty one."""
        source = _source(_derive(), PLAYER_ID)

        assert source is not None
        assert source.can_play_pause is False

    def test_provider_supplied_queue_source_is_left_alone(self) -> None:
        """A provider publishing its own source under the player id keeps its own flags."""
        own_source = PlayerSource(id=PLAYER_ID, name="Native", passive=False, can_play_pause=True)

        state = _derive(own=_own(source_list=[own_source]), queues={PLAYER_ID: _queue(items=0)})

        assert [x for x in state.source_list if x.id == PLAYER_ID] == [own_source]
        assert _source(state, PLAYER_ID) is own_source

    def test_ordering_source_advertises_its_controls(self) -> None:
        """A source that orders its own session says so, so clients can offer the controls."""
        session = _session(_audio_source(can_shuffle=True, can_repeat=True))

        source = _source(_derive(session=session), SOURCE_URI)

        assert source is not None
        assert source.name == "Spotify Connect"
        assert source.can_shuffle is True
        assert source.can_repeat is True

    def test_transport_only_source_advertises_no_ordering(self) -> None:
        """A source that cannot be reordered says so, so clients grey the controls out."""
        session = _session(_audio_source(can_play_pause=True, can_seek=True))

        source = _source(_derive(session=session), SOURCE_URI)

        assert source is not None
        # pins that this source really was published, so the flags below are a
        # read of it rather than of an absent entry
        assert source.can_play_pause is True
        assert source.can_seek is True
        assert source.can_next_previous is False
        assert source.can_shuffle is False
        assert source.can_repeat is False

    @pytest.mark.parametrize("can_initiate", [True, False])
    def test_live_source_ma_can_not_start_is_passive(self, can_initiate: bool) -> None:
        """A playing source MA cannot start on demand is not offered for selection."""
        session = _session(_audio_source(can_initiate=can_initiate))

        source = _source(_derive(session=session), SOURCE_URI)

        assert source is not None
        assert source.passive is not can_initiate

    @pytest.mark.parametrize(
        ("shuffle_enabled", "repeat_mode"),
        [(True, RepeatMode.ALL), (False, RepeatMode.OFF), (None, None)],
        ids=["reported_on", "reported_off", "unreported"],
    )
    def test_reported_ordering_reaches_the_source_list(
        self, shuffle_enabled: bool | None, repeat_mode: RepeatMode | None
    ) -> None:
        """What the session reports is what clients render, unreported staying unknown."""
        session = _session(shuffle_enabled=shuffle_enabled, repeat_mode=repeat_mode)

        source = _source(_derive(session=session), SOURCE_URI)

        assert source is not None
        assert source.shuffle_enabled is shuffle_enabled
        assert source.repeat_mode is repeat_mode

    def test_standing_source_gets_an_entry_without_a_session(self) -> None:
        """A source bound to the player is selectable while nothing plays it."""
        standing = _audio_source(can_initiate=True, can_play_pause=True)

        source = _source(_derive(standing_sources=(standing,)), SOURCE_URI)

        assert source is not None
        assert source.passive is False
        assert source.can_play_pause is True

    def test_standing_entry_passive_follows_can_initiate(self) -> None:
        """A source MA cannot start on demand is listed as passive."""
        source = _source(_derive(standing_sources=(_audio_source(can_initiate=False),)), SOURCE_URI)

        assert source is not None
        assert source.passive is True

    def test_live_session_entry_wins_over_the_standing_entry(self) -> None:
        """While the source is live, the entry with its live ordering state is the one listed."""
        source = _audio_source(can_initiate=True, can_shuffle=True, can_repeat=True)
        session = _session(source, shuffle_enabled=True, repeat_mode=RepeatMode.ALL)

        state = _derive(session=session, standing_sources=(source,))

        entries = [x for x in state.source_list if x.id == SOURCE_URI]
        assert len(entries) == 1
        assert entries[0].shuffle_enabled is True
        assert entries[0].repeat_mode is RepeatMode.ALL

    def test_standing_source_is_listed_once(self) -> None:
        """A source reported both as bound and as a device input gets a single entry."""
        source = _audio_source(can_initiate=True)

        state = _derive(standing_sources=(source, source))

        assert [x.id for x in state.source_list].count(SOURCE_URI) == 1


class TestSupportedFeatures:
    """The features of a player."""

    def test_own_features_are_kept(self) -> None:
        """What the provider object supports itself is supported."""
        state = _derive(own=_own(supported_features={PlayerFeature.PAUSE, PlayerFeature.SEEK}))

        assert state.supported_features == {PlayerFeature.PAUSE, PlayerFeature.SEEK}

    def test_own_features_are_not_changed_in_place(self) -> None:
        """The derived set is a new one, the reported features stay what they are."""
        reported: set[PlayerFeature] = set()

        _derive(own=_own(supported_features=reported), power_control=PLAYER_CONTROL_NATIVE)

        assert reported == set()

    def test_active_output_protocol_adds_its_playback_features(self) -> None:
        """The protocol rendering the audio decides what playback can do."""
        protocol = make_protocol_snapshot(
            "airplay_1",
            supported_features={
                PlayerFeature.PAUSE,
                PlayerFeature.ENQUEUE,
                PlayerFeature.SET_MEMBERS,
                PlayerFeature.SEEK,
            },
        )

        state = _derive(
            linked=(protocol,), active_protocol=protocol, active_output_protocol="airplay_1"
        )

        assert state.supported_features == {
            PlayerFeature.PAUSE,
            PlayerFeature.ENQUEUE,
            PlayerFeature.SET_MEMBERS,
        }

    def test_idle_linked_protocol_only_adds_what_works_without_playing(self) -> None:
        """An audio protocol that renders nothing adds announcements and grouping."""
        protocol = make_protocol_snapshot(
            "airplay_1",
            supported_features={
                PlayerFeature.PAUSE,
                PlayerFeature.PLAY_ANNOUNCEMENT,
                PlayerFeature.SET_MEMBERS,
            },
        )

        state = _derive(linked=(protocol,))

        assert state.supported_features == {
            PlayerFeature.PLAY_ANNOUNCEMENT,
            PlayerFeature.SET_MEMBERS,
        }

    @pytest.mark.parametrize(
        ("control", "feature"),
        [
            ("power_control", PlayerFeature.POWER),
            ("volume_control", PlayerFeature.VOLUME_SET),
            ("mute_control", PlayerFeature.VOLUME_MUTE),
        ],
    )
    def test_control_features_follow_the_resolved_controls(
        self, control: str, feature: PlayerFeature
    ) -> None:
        """A feature is there with a control to back it, whatever the provider object says."""
        with_control = _derive(**{control: PLAYER_CONTROL_FAKE})
        without_control = _derive(
            **{control: PLAYER_CONTROL_NONE}, own=_own(supported_features={feature})
        )

        assert feature in with_control.supported_features
        assert feature not in without_control.supported_features

    def test_group_player_keeps_its_own_volume_features(self) -> None:
        """A group derives volume and mute from its members, it has no control to resolve."""
        state = _derive(
            player_type=PlayerType.GROUP,
            own=_own(supported_features={PlayerFeature.VOLUME_SET, PlayerFeature.VOLUME_MUTE}),
        )

        assert PlayerFeature.VOLUME_SET in state.supported_features
        assert PlayerFeature.VOLUME_MUTE in state.supported_features

    def test_native_source_plus_queue_grants_bit(self) -> None:
        """MA Queue + one native source (e.g. line-in) → SELECT_SOURCE."""
        state = _derive(
            own=_own(source_list=[PlayerSource(id="line_in", name="Line-in", passive=False)])
        )

        assert PlayerFeature.SELECT_SOURCE in state.supported_features

    @pytest.mark.parametrize(
        "source_list",
        [[], [PlayerSource(id="spotify", name="Spotify", passive=True)]],
        ids=["no_sources", "passive_source"],
    )
    def test_no_extra_sources_does_not_grant_bit(self, source_list: list[PlayerSource]) -> None:
        """MA Queue as the only source that can be started → SELECT_SOURCE not set."""
        state = _derive(own=_own(source_list=source_list))

        assert PlayerFeature.SELECT_SOURCE not in state.supported_features

    def test_locked_grouping_withdraws_set_members(self) -> None:
        """A read-only group offers no grouping, also not through a linked protocol."""
        protocol = make_protocol_snapshot(
            "airplay_1", supported_features={PlayerFeature.SET_MEMBERS}
        )

        state = _derive(
            grouping_locked=True,
            own=_own(supported_features={PlayerFeature.SET_MEMBERS}),
            linked=(protocol,),
        )

        assert PlayerFeature.SET_MEMBERS not in state.supported_features


class TestGroupMembers:
    """The players grouped to a player."""

    def test_leader_lists_itself_first(self) -> None:
        """A sync leader lists its own id, then the players synced to it."""
        state = _derive(
            own=_own(group_members=["member", PLAYER_ID]),
            peers={PLAYER_ID: _peer(PLAYER_ID), "member": _peer("member")},
        )

        assert state.group_members == [PLAYER_ID, "member"]

    def test_player_that_is_its_only_member_has_no_members(self) -> None:
        """A solo player reporting itself is not a group."""
        state = _derive(own=_own(group_members=[PLAYER_ID]), peers={PLAYER_ID: _peer(PLAYER_ID)})

        assert state.group_members == []

    def test_synced_player_has_no_members(self) -> None:
        """Members belong to the leader, not to a player synced to it."""
        state = _derive(
            own=_own(synced_to="leader", group_members=["leader", PLAYER_ID]),
            peers={"leader": _peer("leader"), PLAYER_ID: _peer(PLAYER_ID)},
        )

        assert state.group_members == []

    def test_protocol_members_show_as_their_players(self) -> None:
        """Members a device reports as protocols are the players those belong to."""
        state = _derive(
            # the last one is a protocol that is attached to no player
            own=_own(group_members=[PLAYER_ID, "airplay_2", "airplay_3"]),
            peers={PLAYER_ID: _peer(PLAYER_ID), "airplay_2": _peer("player_2")},
        )

        assert state.group_members == [PLAYER_ID, "player_2"]

    def test_active_output_protocol_adds_its_members(self) -> None:
        """A group formed on the protocol that renders the audio is the player's group."""
        protocol = make_protocol_snapshot("airplay_1", group_members=["airplay_1", "airplay_2"])

        state = _derive(
            linked=(protocol,),
            active_protocol=protocol,
            active_output_protocol="airplay_1",
            peers={"airplay_1": _peer(PLAYER_ID), "airplay_2": _peer("player_2")},
        )

        assert state.group_members == [PLAYER_ID, "player_2"]

    def test_idle_linked_protocol_does_not_add_its_members(self) -> None:
        """Only the protocol of the active output contributes members."""
        protocol = make_protocol_snapshot("airplay_1", group_members=["airplay_1", "airplay_2"])

        state = _derive(
            linked=(protocol,),
            peers={"airplay_1": _peer(PLAYER_ID), "airplay_2": _peer("player_2")},
        )

        assert state.group_members == []

    def test_group_player_does_not_list_itself(self) -> None:
        """A group player lists its members, of which it is not one."""
        state = _derive(
            player_type=PlayerType.GROUP,
            own=_own(group_members=["member"]),
            peers={"member": _peer("member")},
        )

        assert state.group_members == ["member"]


class TestCanGroupWith:
    """The players a player can be grouped with."""

    def test_available_candidates_are_offered(self) -> None:
        """A player can group with the available players it reports."""
        state = _derive(
            can_group_with={"player_2", "player_3"},
            peers={"player_2": _peer("player_2"), "player_3": _peer("player_3")},
        )

        assert state.can_group_with == {"player_2", "player_3"}

    @pytest.mark.parametrize(
        "candidate",
        [
            _peer("player_2", available=False),
            _peer("player_2", grouping_locked=True),
            _peer(
                "player_2", playback_state=PlaybackState.PLAYING, group_members=["player_2", "x"]
            ),
            _peer("player_2", playback_state=PlaybackState.PAUSED, group_members=["player_2", "x"]),
            _peer("player_2", type=PlayerType.SOURCE),
            _peer("player_2", type=PlayerType.UNKNOWN),
        ],
        ids=[
            "unavailable",
            "grouping_locked",
            "playing_leader",
            "paused_leader",
            "source",
            "unknown",
        ],
    )
    def test_candidate_that_can_not_join_is_left_out(self, candidate: PeerSnapshot) -> None:
        """A player that is gone, read-only or leading a group of its own is no target."""
        state = _derive(can_group_with={"player_2"}, peers={"player_2": candidate})

        assert state.can_group_with == set()

    @pytest.mark.parametrize(
        "candidate",
        [
            _peer("player_2", group_members=["player_2", "x"]),
            _peer("player_2", playback_state=PlaybackState.PLAYING),
        ],
        ids=["idle_leader", "playing_alone"],
    )
    def test_candidate_that_leads_nothing_playing_is_offered(self, candidate: PeerSnapshot) -> None:
        """Only a player that plays to members of its own is taken."""
        state = _derive(can_group_with={"player_2"}, peers={"player_2": candidate})

        assert state.can_group_with == {"player_2"}

    def test_player_does_not_group_with_itself(self) -> None:
        """A provider naming all its players includes the player itself, which is no target."""
        state = _derive(
            can_group_with={PLAYER_ID, "player_2"},
            peers={PLAYER_ID: _peer(PLAYER_ID), "player_2": _peer("player_2")},
        )

        assert state.can_group_with == {"player_2"}

    def test_protocol_candidates_show_as_their_players(self) -> None:
        """A protocol a device can group with stands for the player it belongs to."""
        state = _derive(
            # the second one is a protocol that is attached to no player
            can_group_with={"airplay_2", "airplay_3", "airplay_4"},
            peers={
                "airplay_2": _peer("player_2"),
                # its player can not join
                "airplay_4": _peer("player_4", available=False),
            },
        )

        assert state.can_group_with == {"player_2"}

    def test_synced_player_can_not_group(self) -> None:
        """A player that is synced already has nothing to group with."""
        state = _derive(
            own=_own(synced_to="leader"),
            can_group_with={"player_2"},
            peers={"leader": _peer("leader"), "player_2": _peer("player_2")},
        )

        assert state.can_group_with == set()

    def test_locked_grouping_offers_no_targets(self) -> None:
        """A read-only group offers no targets, also not those of an audio protocol."""
        state = _derive(
            grouping_locked=True,
            can_group_with={"player_2"},
            protocol_can_group_with={"player_2"},
            linked=(make_protocol_snapshot("airplay_1"),),
            peers={"player_2": _peer("player_2")},
        )

        assert state.can_group_with == set()

    def test_linked_protocols_add_their_candidates(self) -> None:
        """What an audio protocol of the player can group with, the player can group with."""
        state = _derive(
            linked=(make_protocol_snapshot("airplay_1"),),
            protocol_can_group_with={"airplay_2", "airplay_3", "player_4"},
            peers={
                "airplay_2": _peer("player_2"),
                "airplay_3": _peer("player_3", available=False),
                "player_4": _peer("player_4"),
            },
        )

        assert state.can_group_with == {"player_2", "player_4"}

    def test_external_source_leaves_only_the_own_candidates(self) -> None:
        """A device playing something MA does not produce can not group through a protocol."""
        state = _derive(
            own=_own(active_source="tv", playback_state=PlaybackState.PLAYING),
            can_group_with={"player_2"},
            protocol_can_group_with={"player_3"},
            linked=(make_protocol_snapshot("airplay_1"),),
            peers={"player_2": _peer("player_2"), "player_3": _peer("player_3")},
        )

        assert state.active_source == "tv"
        assert state.can_group_with == {"player_2"}

    def test_source_ma_plays_itself_keeps_the_protocol_candidates(self) -> None:
        """Another queue or a live audio source is audio MA can send to a protocol group."""
        state = _derive(
            active_mass_source="queue_x",
            mass_sources=frozenset({"queue_x"}),
            linked=(make_protocol_snapshot("airplay_1"),),
            protocol_can_group_with={"player_3"},
            peers={"player_3": _peer("player_3")},
        )

        assert state.active_source == "queue_x"
        assert state.can_group_with == {"player_3"}


class TestCurrentMedia:
    """The media a player publishes as playing."""

    def test_idle_player_without_a_queue_has_no_media(self) -> None:
        """Nothing loaded anywhere gives no current media."""
        assert _derive().current_media is None

    def test_grouped_player_shares_the_media_of_its_group(self) -> None:
        """A group member publishes the very media object of its group."""
        media = PlayerMedia(uri="group://media", title="Group song")
        group = _group(current_media=media)

        state = _derive(
            groups=(group,),
            peers={"group_1": _group_peer(group)},
            own=_own(current_media=PlayerMedia(uri="own://media")),
        )

        assert state.current_media is media

    def test_synced_player_shares_the_media_of_its_leader(self) -> None:
        """A synced player publishes the very media object of its leader."""
        media = PlayerMedia(uri="leader://media", title="Leader song")

        state = _derive(
            own=_own(synced_to="leader"), peers={"leader": _peer("leader", current_media=media)}
        )

        assert state.current_media is media

    @pytest.mark.parametrize("synced_to", ["gone", PLAYER_ID], ids=["unknown", "itself"])
    def test_player_following_no_real_leader_has_no_media(self, synced_to: str) -> None:
        """A leader that is gone, or the player itself, has no media to mirror."""
        state = _derive(
            own=_own(synced_to=synced_to, current_media=PlayerMedia(uri="own://media")),
            peers={PLAYER_ID: _peer(PLAYER_ID, current_media=PlayerMedia(uri="old://media"))},
        )

        assert state.current_media is None

    def test_live_source_names_what_it_plays(self) -> None:
        """The metadata a live source reports is the current media."""
        metadata = StreamMetadata(
            title="Song",
            artist="Artist",
            album="Album",
            image_url="http://img/song.jpg",
            duration=180,
            elapsed_time=42,
            elapsed_time_last_updated=NOW - 3,
        )
        session = _session(stream_metadata=metadata)

        media = _derive(session=session, session_image_url="http://img/source.jpg").current_media

        assert media is not None
        assert media.uri == SOURCE_URI
        assert media.media_type == MediaType.AUDIO_SOURCE
        assert media.title == "Song"
        assert media.artist == "Artist"
        assert media.album == "Album"
        assert media.image_url == "http://img/song.jpg"
        assert media.duration == 180
        # the published position of the player, so both stay in agreement
        assert media.elapsed_time == 42
        assert media.elapsed_time_last_updated == NOW - 3

    def test_silent_live_source_shows_as_itself(self) -> None:
        """A source that reports nothing shows its own name and artwork."""
        media = _derive(session=_session(), session_image_url="http://img/source.jpg").current_media

        assert media is not None
        assert media.title == "Spotify Connect"
        assert media.artist is None
        assert media.image_url == "http://img/source.jpg"
        assert media.elapsed_time is None

    def test_live_source_media_can_be_handed_back_to_the_player(self) -> None:
        """The media carries the owner and the token its stream url is resolved with."""
        session = _session()

        media = _derive(session=session).current_media

        assert media is not None
        assert media.source_id == PLAYER_ID
        assert media.queue_session_id == session.playback_session_id

    def test_foreign_app_media_is_published_as_reported(self) -> None:
        """The media of an app on an audio protocol is the object that protocol reports."""
        app = _app_protocol()

        state = _derive(
            linked=(app,), foreign_app=app, queues={PLAYER_ID: _queue(current_item=_queue_item())}
        )

        assert state.current_media is app.current_media

    def test_queue_item_with_a_media_item_is_described_by_it(self) -> None:
        """A track from the queue shows its own details and the position of the queue."""
        track = Track(
            item_id="t1",
            provider="test",
            name="Song",
            version="Live",
            duration=200,
            provider_mappings={
                ProviderMapping(item_id="t1", provider_domain="test", provider_instance="test")
            },
            album=ItemMapping(
                media_type=MediaType.ALBUM, item_id="a1", provider="test", name="Album"
            ),
            artists=UniqueList(
                [
                    ItemMapping(
                        media_type=MediaType.ARTIST, item_id="ar1", provider="test", name="Artist"
                    )
                ]
            ),
        )
        queue = _queue(
            current_item=_queue_item(media_item=track),
            elapsed_time=12.7,
            elapsed_time_last_updated=NOW - 4,
            item_image_url="http://img/item.jpg",
            media_item_image_url="http://img/track.jpg",
        )

        media = _derive(queues={PLAYER_ID: queue}).current_media

        assert media is not None
        assert media.uri == "test://track/t1"
        assert media.media_type == MediaType.TRACK
        assert media.title == "Song (Live)"
        assert media.artist == "Artist"
        assert media.album == "Album"
        assert media.image_url == "http://img/track.jpg"
        assert media.duration == 200
        assert media.source_id == PLAYER_ID
        assert media.queue_item_id == "item_1"
        assert media.elapsed_time == 12
        assert media.elapsed_time_last_updated == NOW - 4

    def test_media_item_without_an_image_shows_the_image_of_the_queue_item(self) -> None:
        """The artwork of the queue item stands in for a media item that has none."""
        track = Track(
            item_id="t1",
            provider="test",
            name="Song",
            provider_mappings={
                ProviderMapping(item_id="t1", provider_domain="test", provider_instance="test")
            },
        )
        queue = _queue(
            current_item=_queue_item(media_item=track), item_image_url="http://img/item.jpg"
        )

        media = _derive(queues={PLAYER_ID: queue}).current_media

        assert media is not None
        assert media.title == "Song"
        assert media.image_url == "http://img/item.jpg"

    def test_podcast_episode_reports_the_podcast_as_artist_only(self) -> None:
        """The podcast name fills the artist and is not repeated as the album."""
        episode = PodcastEpisode(
            item_id="ep1",
            provider="test",
            name="Episode 1",
            position=1,
            duration=300,
            podcast=ItemMapping(
                media_type=MediaType.PODCAST, item_id="pod1", provider="test", name="My Podcast"
            ),
            provider_mappings={
                ProviderMapping(item_id="ep1", provider_domain="test", provider_instance="test")
            },
        )
        episode.metadata.description = "<p>About <b>this</b> episode</p>"
        queue = _queue(current_item=_queue_item(media_item=episode))

        media = _derive(queues={PLAYER_ID: queue}).current_media

        assert media is not None
        assert media.title == "Episode 1"
        assert media.artist == "My Podcast"
        assert media.album is None

    def test_description_stands_in_for_the_album_as_plain_text(self) -> None:
        """An item without album or podcast shows its description, without the markup."""
        track = Track(
            item_id="t1",
            provider="test",
            name="Song",
            provider_mappings={
                ProviderMapping(item_id="t1", provider_domain="test", provider_instance="test")
            },
        )
        track.metadata.description = "<p>Recorded <b>live</b></p>"
        queue = _queue(current_item=_queue_item(media_item=track))

        media = _derive(queues={PLAYER_ID: queue}).current_media

        assert media is not None
        assert media.album == "Recorded **live**"

    def test_queue_item_with_stream_metadata_is_described_by_the_stream(self) -> None:
        """A radio station shows the song its stream reports."""
        metadata = StreamMetadata(
            title="Live Track",
            artist="Live Artist",
            image_url="http://img/live.jpg",
            elapsed_time=5,
            elapsed_time_last_updated=NOW - 2,
        )
        item = _queue_item(
            name="Live Radio",
            duration=None,
            streamdetails=StreamDetails(
                provider="test",
                item_id="radio_1",
                audio_format=AudioFormat(),
                media_type=MediaType.RADIO,
                stream_metadata=metadata,
            ),
        )
        queue = _queue(
            current_item=item,
            elapsed_time=600,
            elapsed_time_last_updated=NOW - 9,
            item_image_url="http://img/item.jpg",
        )

        media = _derive(queues={PLAYER_ID: queue}).current_media

        assert media is not None
        assert media.title == "Live Track"
        assert media.artist == "Live Artist"
        assert media.album == "Live Radio"
        assert media.image_url == "http://img/live.jpg"
        assert media.source_id == PLAYER_ID
        assert media.queue_item_id == "item_1"
        # the position of the song, with the timestamp that belongs to it
        assert media.elapsed_time == 5
        assert media.elapsed_time_last_updated == NOW - 2

    @pytest.mark.parametrize(
        ("duration", "elapsed_time", "elapsed_time_last_updated"),
        [(None, 600, NOW - 9), (180, None, None)],
        ids=["station", "song_with_duration"],
    )
    def test_stream_without_a_position_falls_back_to_the_queue_only_for_a_station(
        self,
        duration: int | None,
        elapsed_time: int | None,
        elapsed_time_last_updated: float | None,
    ) -> None:
        """How long the stream has played is no position in a song that reports its duration."""
        item = _queue_item(
            streamdetails=StreamDetails(
                provider="test",
                item_id="radio_1",
                audio_format=AudioFormat(),
                media_type=MediaType.RADIO,
                stream_metadata=StreamMetadata(title="Live Track", duration=duration),
            ),
        )
        queue = _queue(current_item=item, elapsed_time=600, elapsed_time_last_updated=NOW - 9)

        media = _derive(queues={PLAYER_ID: queue}).current_media

        assert media is not None
        assert media.elapsed_time == elapsed_time
        assert media.elapsed_time_last_updated == elapsed_time_last_updated

    def test_bare_queue_item_is_described_by_itself(self) -> None:
        """An item without media item or stream metadata shows its own name."""
        queue = _queue(
            current_item=_queue_item(name="Some url"),
            elapsed_time=3.9,
            item_image_url="http://img/item.jpg",
        )

        media = _derive(queues={PLAYER_ID: queue}).current_media

        assert media is not None
        assert media.title == "Some url"
        assert media.image_url == "http://img/item.jpg"
        assert media.duration == 200
        assert media.elapsed_time == 3

    def test_active_queue_without_an_item_hides_the_media_of_the_device(self) -> None:
        """While an MA queue is the source, the device's own media is not what plays."""
        state = _derive(
            queues={PLAYER_ID: _queue(items=0)}, own=_own(current_media=PlayerMedia(uri="x://y"))
        )

        assert state.current_media is None

    def test_queue_of_the_active_source_is_the_one_shown(self) -> None:
        """A player playing another queue shows the item of that queue."""
        state = _derive(
            active_mass_source="queue_x",
            queues={
                "queue_x": _queue("queue_x", current_item=_queue_item(name="Theirs")),
                PLAYER_ID: _queue(current_item=_queue_item(name="Mine")),
            },
        )

        assert state.current_media is not None
        assert state.current_media.title == "Theirs"
        assert state.current_media.source_id == "queue_x"

    def test_own_queue_stands_in_for_a_source_that_is_no_queue(self) -> None:
        """A remembered source without a queue behind it leaves the player's own queue."""
        state = _derive(
            active_mass_source="no_queue",
            queues={PLAYER_ID: _queue(current_item=_queue_item(name="Mine"))},
        )

        assert state.current_media is not None
        assert state.current_media.title == "Mine"

    def test_own_queue_is_not_shown_while_the_device_reports_another_source(self) -> None:
        """A device on a source of its own shows its own media, not the idle MA queue."""
        own_media = PlayerMedia(uri="spotify:track:1", title="Shout")

        state = _derive(
            own=_own(
                active_source="spotify",
                playback_state=PlaybackState.PLAYING,
                current_media=own_media,
            ),
            queues={PLAYER_ID: _queue(current_item=_queue_item(name="Mine"))},
        )

        assert state.current_media is not None
        assert state.current_media.title == "Shout"
        assert state.current_media.source_id == "spotify"

    def test_media_of_the_device_is_published_as_a_copy(self) -> None:
        """What the device reports is copied, with the position of the media itself preferred."""
        own_media = PlayerMedia(
            uri="http://stream",
            media_type=MediaType.RADIO,
            title="Title",
            artist="Artist",
            album="Album",
            image_url="http://img/own.jpg",
            duration=100,
            source_id="own_source",
            queue_item_id="own_item",
            elapsed_time=20,
            elapsed_time_last_updated=NOW - 6,
        )

        state = _derive(
            own=_own(current_media=own_media, elapsed_time=99.0, elapsed_time_last_updated=NOW)
        )

        media = state.current_media
        assert media is not None
        assert media is not own_media
        assert media.uri == "http://stream"
        assert media.media_type == MediaType.RADIO
        assert (media.title, media.artist, media.album) == ("Title", "Artist", "Album")
        assert media.image_url == "http://img/own.jpg"
        assert media.duration == 100
        assert media.source_id == "own_source"
        assert media.queue_item_id == "own_item"
        assert media.elapsed_time == 20
        assert media.elapsed_time_last_updated == NOW - 6

    def test_media_without_a_position_takes_the_position_of_the_player(self) -> None:
        """Position and timestamp both come from the player, never a mix of the two."""
        state = _derive(
            own=_own(
                current_media=PlayerMedia(uri="http://stream", elapsed_time_last_updated=NOW - 60),
                elapsed_time=33.8,
                elapsed_time_last_updated=NOW - 1,
            )
        )

        media = state.current_media
        assert media is not None
        assert media.elapsed_time == 33
        assert media.elapsed_time_last_updated == NOW - 1
        # the media names no source of its own, so it plays on the active source
        assert media.source_id == PLAYER_ID

    def test_resolved_palette_is_attached_to_the_image_it_belongs_to(self) -> None:
        """The palette travels with the media while its image is the one shown."""
        palette = MagicMock()

        def media(image_url: str) -> PlayerMedia | None:
            return _derive(
                own=_own(current_media=PlayerMedia(uri="http://stream", image_url=image_url)),
                palette_url="http://img/own.jpg",
                palette=palette,
            ).current_media

        shown, other = media("http://img/own.jpg"), media("http://img/other.jpg")
        assert shown is not None
        assert other is not None
        assert shown.palette is palette
        assert other.palette is None


class TestMediaQueue:
    """The queue a player takes its current media from."""

    @pytest.mark.parametrize(
        ("values", "queue_id"),
        [
            ({}, PLAYER_ID),
            ({"active_mass_source": "queue_x"}, "queue_x"),
            ({"active_mass_source": "no_queue"}, PLAYER_ID),
            (
                {"own": _own(active_source="queue_x", playback_state=PlaybackState.PLAYING)},
                "queue_x",
            ),
            (
                {"own": _own(active_source="spotify", playback_state=PlaybackState.PLAYING)},
                None,
            ),
            ({"own": _own(synced_to="leader"), "peers": {"leader": _peer("leader")}}, None),
            ({"groups": (_group(),), "peers": {"group_1": _group_peer(_group())}}, None),
            ({"session": _session()}, None),
            ({"linked": (_app_protocol(),), "foreign_app": _app_protocol()}, None),
        ],
        ids=[
            "own_queue",
            "queue_of_the_active_source",
            "source_that_is_no_queue",
            "reported_queue",
            "external_source",
            "synced",
            "grouped",
            "live_source",
            "foreign_app",
        ],
    )
    def test_media_queue_is_the_queue_the_media_is_taken_from(
        self, values: dict[str, Any], queue_id: str | None
    ) -> None:
        """Only a player that shows the item of a queue takes its media from that queue."""
        queues = {
            queue_id: _queue(
                queue_id,
                current_item=_queue_item(),
                item_image_url=f"http://img/{queue_id}.jpg",
            )
            for queue_id in (PLAYER_ID, "queue_x")
        }
        inputs = make_state_inputs(queues=queues, **values)

        media_queue = resolve_media_queue(inputs)

        assert media_queue is (queues[queue_id] if queue_id else None)
        # which makes it the only queue whose artwork the state shows
        media = derive_player_state(inputs)[0].current_media
        assert (media.image_url if media else None) == (
            media_queue.item_image_url if media_queue else None
        )


class TestMediaPositionAnchor:
    """The position anchor of the current media across calculations."""

    @staticmethod
    def _playing(
        elapsed_time: int, elapsed_time_last_updated: float, previous: State | None = None
    ) -> tuple[State, StateEffects]:
        """Derive the state of a player playing its own media at the given position."""
        return derive_player_state(
            make_state_inputs(
                previous_state=previous or make_player_state(PLAYER_ID),
                own=_own(
                    playback_state=PlaybackState.PLAYING,
                    current_media=PlayerMedia(
                        uri="http://stream",
                        elapsed_time=elapsed_time,
                        elapsed_time_last_updated=elapsed_time_last_updated,
                    ),
                ),
            )
        )

    def test_steady_playback_keeps_the_previous_anchor(self) -> None:
        """A tick that extrapolates to the same position changes nothing."""
        previous, _ = self._playing(10, NOW - 5)

        state, effects = self._playing(15, NOW, previous)

        assert state.current_media is not None
        assert state.current_media.elapsed_time == 10
        assert state.current_media.elapsed_time_last_updated == NOW - 5
        assert effects.media_position_jumped is False

    def test_seek_adopts_the_new_anchor(self) -> None:
        """A position that jumped is published and reported as a jump."""
        previous, _ = self._playing(10, NOW - 5)

        state, effects = self._playing(90, NOW, previous)

        assert state.current_media is not None
        assert state.current_media.elapsed_time == 90
        assert state.current_media.elapsed_time_last_updated == NOW
        assert effects.media_position_jumped is True

    def test_another_item_adopts_its_anchor_without_a_jump(self) -> None:
        """A new item starts from its own position, which is no seek."""
        previous = make_player_state(
            PLAYER_ID,
            playback_state=PlaybackState.PLAYING,
            current_media=PlayerMedia(
                uri="http://other", elapsed_time=10, elapsed_time_last_updated=NOW - 5
            ),
        )

        state, effects = self._playing(90, NOW, previous)

        assert state.current_media is not None
        assert state.current_media.elapsed_time == 90
        assert effects.media_position_jumped is False

    @staticmethod
    def _mirroring(media: PlayerMedia, kind: str) -> dict[str, Any]:
        """Return the inputs of a player that publishes the given media of another player."""
        owner: dict[str, Any] = {"playback_state": PlaybackState.PLAYING, "current_media": media}
        if kind == "synced":
            return {"own": _own(synced_to="leader"), "peers": {"leader": _peer("leader", **owner)}}
        if kind == "grouped":
            group = _group(**owner)
            return {
                "own": _own(playback_state=PlaybackState.PLAYING),
                "groups": (group,),
                "peers": {"group_1": _group_peer(group)},
            }
        app = _app_protocol(current_media=media)
        return {"linked": (app,), "foreign_app": app}

    @pytest.mark.parametrize("kind", ["synced", "grouped", "foreign_app"])
    def test_mirrored_media_keeps_the_anchor_of_its_owner(self, kind: str) -> None:
        """The media object of another player is shared, so its anchor is never rewritten."""
        media = PlayerMedia(uri="http://stream", elapsed_time=15, elapsed_time_last_updated=NOW)
        previous = make_player_state(
            PLAYER_ID,
            playback_state=PlaybackState.PLAYING,
            current_media=PlayerMedia(
                uri="http://stream", elapsed_time=10, elapsed_time_last_updated=NOW - 5
            ),
        )

        state, effects = derive_player_state(
            make_state_inputs(previous_state=previous, **self._mirroring(media, kind))
        )

        assert state.current_media is media
        # steady playback, which would keep the previous anchor on media of the player itself
        assert (media.elapsed_time, media.elapsed_time_last_updated) == (15, NOW)
        assert effects.media_position_jumped is False

    def test_jump_of_mirrored_media_is_reported(self) -> None:
        """A member reports the seek its leader made."""
        media = PlayerMedia(uri="http://stream", elapsed_time=90, elapsed_time_last_updated=NOW)
        previous = make_player_state(
            PLAYER_ID,
            playback_state=PlaybackState.PLAYING,
            current_media=PlayerMedia(
                uri="http://stream", elapsed_time=10, elapsed_time_last_updated=NOW - 5
            ),
        )

        _, effects = derive_player_state(
            make_state_inputs(previous_state=previous, **self._mirroring(media, "synced"))
        )

        assert effects.media_position_jumped is True


class TestGroupVolume:
    """The volume and mute of a player together with the players grouped to it."""

    def test_solo_player_reports_its_own_volume(self) -> None:
        """Without members, the group volume and mute are those of the player."""
        state = _derive(
            volume_control=PLAYER_CONTROL_NATIVE,
            mute_control=PLAYER_CONTROL_NATIVE,
            volume_level=55,
            volume_muted=True,
        )

        assert state.group_volume == 55
        assert state.group_volume_muted is True

    def test_solo_player_without_controls_has_no_group_volume(self) -> None:
        """A level that nothing controls is not reported."""
        state = _derive(volume_level=55, volume_muted=True)

        assert state.group_volume is None
        assert state.group_volume_muted is None

    def test_leader_counts_its_own_new_volume(self) -> None:
        """The loudest member decides, and the leader counts with the level it has now."""
        state = _derive(
            previous_state=make_player_state(
                PLAYER_ID, volume_control=PLAYER_CONTROL_NATIVE, volume_level=20
            ),
            volume_control=PLAYER_CONTROL_NATIVE,
            volume_level=70,
            own=_own(group_members=[PLAYER_ID, "member"]),
            peers={
                PLAYER_ID: _peer(PLAYER_ID, volume_control=PLAYER_CONTROL_NATIVE, volume_level=20),
                "member": _peer("member", volume_control=PLAYER_CONTROL_NATIVE, volume_level=40),
            },
        )

        assert state.group_volume == 70

    def test_group_player_does_not_count_itself(self) -> None:
        """A group player has no volume of its own, only its members count."""
        state = _derive(
            player_type=PlayerType.GROUP,
            volume_control=PLAYER_CONTROL_NATIVE,
            volume_level=90,
            own=_own(group_members=[PLAYER_ID, "member"]),
            peers={
                PLAYER_ID: _peer(PLAYER_ID, type=PlayerType.GROUP),
                "member": _peer("member", volume_control=PLAYER_CONTROL_NATIVE, volume_level=40),
            },
        )

        assert state.group_volume == 40

    def test_player_named_by_a_legacy_id_does_not_count_itself(self) -> None:
        """A group player that lists itself by a legacy id is still no member of its own."""
        state = _derive(
            player_type=PlayerType.GROUP,
            volume_control=PLAYER_CONTROL_NATIVE,
            volume_level=90,
            own=_own(group_members=["legacy_id", "member"]),
            peers={
                "legacy_id": _peer(
                    PLAYER_ID,
                    type=PlayerType.GROUP,
                    volume_control=PLAYER_CONTROL_NATIVE,
                    volume_level=90,
                ),
                "member": _peer("member", volume_control=PLAYER_CONTROL_NATIVE, volume_level=40),
            },
        )

        assert state.group_volume == 40

    @pytest.mark.parametrize(
        "member",
        [
            make_peer_snapshot(
                "member",
                make_player_state(
                    "member", volume_control=PLAYER_CONTROL_NATIVE, volume_level=90, powered=False
                ),
            ),
            _peer("member", volume_control=PLAYER_CONTROL_NATIVE, volume_level=90, enabled=False),
            _peer(
                "member",
                volume_control=PLAYER_CONTROL_NATIVE,
                volume_level=90,
                available_for_playback=False,
            ),
            _peer("member", volume_control=PLAYER_CONTROL_NONE, volume_level=90),
            _peer("member", volume_control=PLAYER_CONTROL_NATIVE, volume_level=None),
        ],
        ids=["powered_off", "disabled", "unusable", "no_volume_control", "unknown_level"],
    )
    def test_member_that_does_not_play_along_is_left_out(self, member: PeerSnapshot) -> None:
        """A member that is off, gone or without a level does not decide the group volume."""
        state = _derive(
            volume_control=PLAYER_CONTROL_NATIVE,
            volume_level=30,
            own=_own(group_members=[PLAYER_ID, "member"]),
            peers={PLAYER_ID: _peer(PLAYER_ID), "member": member},
        )

        assert state.group_volume == 30

    def test_group_without_a_member_that_counts_has_no_volume(self) -> None:
        """A group whose members have no volume control reports no group volume."""
        state = _derive(
            player_type=PlayerType.GROUP,
            own=_own(group_members=["member"]),
            peers={"member": _peer("member", volume_level=40)},
        )

        assert state.group_volume is None

    @pytest.mark.parametrize(
        ("leader_muted", "member_muted", "group_muted"),
        [(True, True, True), (False, False, False), (True, False, None), (None, True, True)],
        ids=["all_muted", "none_muted", "mixed", "unknown_is_left_out"],
    )
    def test_group_is_muted_when_all_its_members_are(
        self, leader_muted: bool | None, member_muted: bool, group_muted: bool | None
    ) -> None:
        """The group mute is only known when the members that report one agree."""
        state = _derive(
            mute_control=PLAYER_CONTROL_NATIVE,
            volume_muted=leader_muted,
            own=_own(group_members=[PLAYER_ID, "member"]),
            peers={
                PLAYER_ID: _peer(PLAYER_ID),
                "member": _peer(
                    "member", mute_control=PLAYER_CONTROL_NATIVE, volume_muted=member_muted
                ),
            },
        )

        assert state.group_volume_muted is group_muted

    def test_member_without_a_mute_control_does_not_decide_the_group_mute(self) -> None:
        """A member that can not be muted is left out of the group mute."""
        state = _derive(
            mute_control=PLAYER_CONTROL_NATIVE,
            volume_muted=True,
            own=_own(group_members=[PLAYER_ID, "member"]),
            peers={PLAYER_ID: _peer(PLAYER_ID), "member": _peer("member", volume_muted=False)},
        )

        assert state.group_volume_muted is True


class TestEffects:
    """What has to happen besides publishing the derived state."""

    @staticmethod
    def _playing(elapsed_time: float | None, elapsed_time_last_updated: float | None) -> State:
        """Return the previous state of a player playing at the given position."""
        return make_player_state(
            PLAYER_ID,
            playback_state=PlaybackState.PLAYING,
            elapsed_time=elapsed_time,
            elapsed_time_last_updated=elapsed_time_last_updated,
        )

    def test_seek_is_reported_as_a_position_jump(self) -> None:
        """A position that moved outside regular playback has to re-base the queue timing."""
        effects = _effects(
            previous_state=self._playing(10.0, NOW - 5),
            own=_own(
                playback_state=PlaybackState.PLAYING,
                elapsed_time=61.0,
                elapsed_time_last_updated=NOW,
            ),
        )

        assert effects.position_jumped is True

    def test_steady_playback_is_no_position_jump(self) -> None:
        """A tick that extrapolates to the same position is regular playback."""
        effects = _effects(
            previous_state=self._playing(10.0, NOW - 5),
            own=_own(
                playback_state=PlaybackState.PLAYING,
                elapsed_time=15.2,
                elapsed_time_last_updated=NOW,
            ),
        )

        assert effects.position_jumped is False

    def test_position_appearing_is_a_position_jump(self) -> None:
        """The first position after a reset is the only chance to propagate it."""
        effects = _effects(
            previous_state=self._playing(None, None),
            own=_own(
                playback_state=PlaybackState.PLAYING,
                elapsed_time=0.5,
                elapsed_time_last_updated=NOW,
            ),
        )

        assert effects.position_jumped is True

    @pytest.mark.parametrize(
        ("stored", "reported", "to_store"),
        [("Old name", "New name", "New name"), ("Name", "Name", None), ("Name", None, None)],
        ids=["changed", "unchanged", "unreported"],
    )
    def test_changed_device_name_is_stored_as_the_default_name(
        self, stored: str, reported: str | None, to_store: str | None
    ) -> None:
        """The name the device reports is kept in the config for when it is gone."""
        effects = _effects(default_name=stored, device_name=reported)

        assert effects.default_name == to_store

    @pytest.mark.parametrize(
        ("previous", "current", "left_idle", "went_idle"),
        [
            (PlaybackState.IDLE, PlaybackState.PLAYING, True, False),
            (PlaybackState.IDLE, PlaybackState.PAUSED, True, False),
            (PlaybackState.PLAYING, PlaybackState.IDLE, False, True),
            (PlaybackState.PLAYING, PlaybackState.PAUSED, False, False),
            (PlaybackState.IDLE, PlaybackState.IDLE, False, False),
        ],
    )
    def test_playback_starting_and_ending_is_reported(
        self, previous: PlaybackState, current: PlaybackState, left_idle: bool, went_idle: bool
    ) -> None:
        """Only the moves from and to idle start and end playback."""
        effects = _effects(
            previous_state=make_player_state(PLAYER_ID, playback_state=previous),
            own=_own(playback_state=current),
        )

        assert effects.left_idle is left_idle
        assert effects.went_idle is went_idle

    @pytest.mark.parametrize(
        ("role", "synced_to", "active_output", "clears"),
        [
            (ProtocolRole.CONTROL, "leader_1", "dlna_1", True),
            # it plays through its own protocol, which follows the leader with it
            (ProtocolRole.CONTROL, "leader_1", "native", False),
            (ProtocolRole.CONTROL, None, "dlna_1", False),
            # a native protocol that follows a leader plays what the leader sends it
            (ProtocolRole.NATIVE, "leader_1", "dlna_1", False),
        ],
    )
    def test_device_that_follows_a_native_leader_gives_up_its_output(
        self, role: ProtocolRole, synced_to: str | None, active_output: str, clears: bool
    ) -> None:
        """A control protocol that follows a native leader is no output's player anymore."""
        state, effects = derive_player_state(
            make_state_inputs(
                own=_own(role=role, synced_to=synced_to),
                active_output_protocol=active_output,
                peers={"leader_1": _peer("leader_1")},
            )
        )

        assert effects.clear_active_output is clears
        assert state.active_output_protocol == (None if clears else active_output)

    def test_nothing_to_do_for_an_idle_player(self) -> None:
        """Deriving the state of a player nothing happens on asks for nothing."""
        assert _effects() == StateEffects(
            position_jumped=False,
            media_position_jumped=False,
            default_name=None,
            left_idle=False,
            went_idle=False,
            foreign_protocol_id=None,
            clear_active_output=False,
        )


class TestReconcilePositionAnchor:
    """The anchor reconcile helper's handling of anchors."""

    def test_incomplete_to_complete_reports_a_jump(self) -> None:
        """A position appearing on a None anchor is a discontinuity, not a quiet adopt."""
        position, _, jumped = reconcile_position_anchor(
            None, None, 0.5, NOW, prev_playing=True, new_playing=True, now=NOW
        )
        assert position == 0.5
        assert jumped

    def test_complete_to_incomplete_adopts_silently(self) -> None:
        """A position disappearing (stop/reset) is adopted without a jump signal."""
        position, _, jumped = reconcile_position_anchor(
            10.0, NOW, None, None, prev_playing=True, new_playing=False, now=NOW
        )
        assert position is None
        assert not jumped

    def test_incomplete_to_incomplete_stays_silent(self) -> None:
        """No position on either side has nothing to signal."""
        _, _, jumped = reconcile_position_anchor(
            None, None, None, None, prev_playing=False, new_playing=False, now=NOW
        )
        assert not jumped

    def test_steady_playback_still_keeps_the_previous_anchor(self) -> None:
        """A tick extrapolating to the same corrected position keeps the anchor unchanged."""
        position, timestamp, jumped = reconcile_position_anchor(
            10.0, NOW - 5, 15.2, NOW, prev_playing=True, new_playing=True, now=NOW
        )
        assert position == 10.0
        assert timestamp == NOW - 5
        assert not jumped

    def test_resume_adopts_the_fresh_anchor(self) -> None:
        """Resuming must not extrapolate from a timestamp taken before the stop."""
        position, timestamp, jumped = reconcile_position_anchor(
            59.0, NOW - 3600, 59.0, NOW - 0.5, prev_playing=False, new_playing=True, now=NOW
        )
        assert position == 59.0
        assert timestamp == NOW - 0.5
        assert not jumped

    def test_pause_adopts_the_paused_anchor(self) -> None:
        """A paused anchor is read without extrapolation, so it must hold the paused position."""
        position, timestamp, jumped = reconcile_position_anchor(
            0.0, NOW - 90, 90.0, NOW, prev_playing=True, new_playing=False, now=NOW
        )
        assert position == 90.0
        assert timestamp == NOW
        assert not jumped

    def test_forced_adopt_takes_the_new_anchor_without_a_jump(self) -> None:
        """A forced adopt of steady playback takes the fresh anchor and reports no jump."""
        position, timestamp, jumped = reconcile_position_anchor(
            10.0, NOW - 5, 15.2, NOW, prev_playing=True, new_playing=True, now=NOW, force_adopt=True
        )
        assert position == 15.2
        assert timestamp == NOW
        assert not jumped
