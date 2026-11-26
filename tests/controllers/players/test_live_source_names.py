"""
Tests for the name of a live source, as shown on a player and outside any player.

A source carries its own name and the player it belongs to. On that player it shows by its
name alone, elsewhere with the name of that player after it, and a source of no player or of
a device that is only an input shows by its name everywhere.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from music_assistant_models.enums import (
    MediaType,
    PlaybackState,
    PlayerType,
    ProtocolRole,
    ProviderFeature,
)
from music_assistant_models.errors import InsufficientPermissions
from music_assistant_models.media_items import AudioSource, ProviderMapping
from music_assistant_models.player import PlayerSource
from music_assistant_models.streamdetails import StreamMetadata

from music_assistant.controllers.music import MusicController
from music_assistant.controllers.players import PlayerController
from music_assistant.models.player import Player
from music_assistant.models.plugin import PluginProvider
from tests.common import MockProvider, as_user, attach_mock, mock_player_mass


def _source(provider: str, item_id: str, name: str, owner: str | None) -> AudioSource:
    """Return a live source as its provider delivers it."""
    return AudioSource(
        item_id=item_id,
        provider=provider,
        name=name,
        provider_mappings={
            ProviderMapping(item_id=item_id, provider_domain=provider, provider_instance=provider)
        },
        can_initiate=True,
        owner_player_id=owner,
    )


# the line-in names its player by the id of the protocol of its device
LINE_IN = _source("sendspin", "lr", "Line-in", "proto_living_room")
SPOTIFY = _source("spotify_connect", "kitchen", "Spotify Connect", "kitchen")
RECORD_PLAYER = _source("sendspin", "rp", "Record player", "proto_record_player")
VBAN = _source("vban", "main", "VBAN Receiver", None)


class _SourcePlugin(PluginProvider):
    """A provider of live sources, bound to players or listed as inputs of their devices."""

    def __init__(
        self,
        instance_id: str,
        sources: list[AudioSource],
        bound: dict[str, list[AudioSource]] | None = None,
        inputs: dict[str, list[AudioSource]] | None = None,
    ) -> None:
        self.manifest = Mock()
        self.manifest.domain = instance_id
        self.config = Mock(instance_id=instance_id)
        self.config.name = instance_id
        self._supported_features = {ProviderFeature.AUDIO_SOURCE}
        self._sources = sources
        self._bound = bound
        self._inputs = inputs or {}

    async def get_audio_sources(self) -> list[AudioSource]:
        """Return all sources of the provider."""
        return self._sources

    def get_player_audio_sources(self, player_id: str) -> list[AudioSource] | None:
        """Return the sources bound to the player, None for a provider that binds none."""
        return None if self._bound is None else self._bound.get(player_id, [])

    def get_player_input_sources(self, player_id: str) -> list[AudioSource]:
        """Return the sources that are inputs of the device of the player."""
        return self._inputs.get(player_id, [])


class _Home:
    """
    A players controller with Living Room, Kitchen and a record player.

    Downstairs groups Living Room and Kitchen, and Everywhere groups Downstairs.
    """

    def __init__(self) -> None:
        """Create the players, the providers of the sources and the group Downstairs."""
        self.mass = mock_player_mass()
        self.mass.config.get_raw_core_config_value = MagicMock(return_value="GLOBAL")
        self.controller = PlayerController(self.mass)
        self.mass.players = self.controller
        self.controller._handle_play_media = AsyncMock()  # type: ignore[method-assign]
        self.music = MusicController.__new__(MusicController)
        self.music.mass = self.mass
        self.mass.music = self.music
        providers = {
            "sendspin": _SourcePlugin(
                "sendspin",
                [LINE_IN, RECORD_PLAYER],
                inputs={"proto_living_room": [LINE_IN], "proto_record_player": [RECORD_PLAYER]},
            ),
            "spotify_connect": _SourcePlugin(
                "spotify_connect", [SPOTIFY], bound={"kitchen": [SPOTIFY]}
            ),
            "vban": _SourcePlugin("vban", [VBAN]),
        }
        self.mass.get_provider = MagicMock(side_effect=providers.get)
        self.mass.get_providers_supporting_feature = MagicMock(
            side_effect=lambda feature, **_: (
                list(providers.values()) if feature == ProviderFeature.AUDIO_SOURCE else []
            )
        )
        provider = MockProvider("test", instance_id="test", mass=self.mass)
        self.living_room, _ = attach_mock(
            provider, "proto_living_room", "Living Room", player_id="living_room"
        )
        self.kitchen, _ = attach_mock(provider, "proto_kitchen", "Kitchen", player_id="kitchen")
        self.record_player, _ = attach_mock(
            provider,
            "proto_record_player",
            "Record player",
            player_id="record_player",
            player_type=PlayerType.SOURCE,
        )
        self.group, self.group_protocol = attach_mock(
            provider,
            "proto_downstairs",
            "Downstairs",
            role=ProtocolRole.VIRTUAL,
            player_id="downstairs",
            player_type=PlayerType.GROUP,
        )
        self.group_protocol._attr_group_members = ["living_room", "kitchen"]
        self.everywhere, self.everywhere_protocol = attach_mock(
            provider,
            "proto_everywhere",
            "Everywhere",
            role=ProtocolRole.VIRTUAL,
            player_id="everywhere",
            player_type=PlayerType.GROUP,
        )
        self.everywhere_protocol._attr_group_members = ["downstairs"]
        # a group holds its members only while it plays
        self.group_protocol._attr_powered = False
        self.everywhere_protocol._attr_powered = False
        self.refresh()

    def play(self, player: Player, source: AudioSource, title: str | None = None) -> None:
        """
        Let a source play on a player, reporting the given title or none.

        :param player: The player the source plays on.
        :param source: The source.
        :param title: The title the source reports.
        """
        if player is self.everywhere:
            self.everywhere_protocol._attr_powered = True
        if player in (self.group, self.everywhere):
            self.group_protocol._attr_powered = True
        session = self.controller._start_audio_source_session(
            player.player_id, source, source.provider
        )
        if title is not None:
            session.stream_metadata = StreamMetadata(title=title)
        self.refresh()

    def refresh(self) -> None:
        """Derive the state of every player, a group before its members."""
        for player in (
            self.record_player,
            self.everywhere,
            self.group,
            self.living_room,
            self.kitchen,
        ):
            player.refresh_state(signal_event=False)


def _entry_name(player: Player, source: AudioSource) -> str | None:
    """Return the name of the entry of a source in the source list of a player."""
    entries = [x.name for x in player.source_list if x.id == source.uri]
    assert len(entries) <= 1
    return entries[0] if entries else None


def _title(player: Player) -> str | None:
    """Return the title of what a player plays."""
    return player.current_media.title if player.current_media else None


@pytest.mark.parametrize(
    ("source", "shown_on", "expected"),
    [
        (SPOTIFY, "kitchen", "Spotify Connect"),
        (SPOTIFY, "living_room", "Spotify Connect (Kitchen)"),
        (SPOTIFY, None, "Spotify Connect (Kitchen)"),
        (LINE_IN, "living_room", "Line-in"),
        (LINE_IN, "kitchen", "Line-in (Living Room)"),
        (LINE_IN, None, "Line-in (Living Room)"),
        (RECORD_PLAYER, "living_room", "Record player"),
        (RECORD_PLAYER, None, "Record player"),
        (VBAN, "kitchen", "VBAN Receiver"),
        (VBAN, None, "VBAN Receiver"),
    ],
    ids=[
        "bound-own_player",
        "bound-other_player",
        "bound-outside",
        "input-own_player",
        "input-other_player",
        "input-outside",
        "input_only_device-other_player",
        "input_only_device-outside",
        "no_player-player",
        "no_player-outside",
    ],
)
def test_the_name_depends_on_where_the_source_shows(
    source: AudioSource, shown_on: str | None, expected: str
) -> None:
    """A source shows by its name on its own player, and with that player elsewhere."""
    home = _Home()

    assert home.controller.get_audio_source_name(source, shown_on) == expected


def test_a_player_merged_into_another_is_found_by_its_old_id() -> None:
    """A provider that stored the id of a merged player names the player it is now."""
    home = _Home()
    home.controller._legacy_ids["old_kitchen"] = "kitchen"
    source = replace(SPOTIFY, owner_player_id="old_kitchen")

    assert home.controller.get_audio_source_owner(source) is home.kitchen
    assert home.controller.get_audio_source_name(source, "kitchen") == "Spotify Connect"


def test_an_old_player_id_wins_over_a_protocol_of_another_player_with_that_id() -> None:
    """
    A stored player id names that player, also when a protocol of another player has it.

    The id was Living Room's before a merge, and an audio protocol of Kitchen has the same id.
    """
    home = _Home()
    home.controller._legacy_ids["old_living_room"] = "living_room"
    attach_mock(
        MockProvider("airplay", instance_id="airplay", mass=home.mass),
        "old_living_room",
        "Kitchen",
        role=ProtocolRole.AUDIO,
        player=home.kitchen,
    )
    source = replace(SPOTIFY, owner_player_id="old_living_room")

    assert home.controller.get_audio_source_owner(source) is home.living_room
    assert home.controller.get_audio_source_name(source, "living_room") == "Spotify Connect"
    assert (
        home.controller.get_audio_source_name(source, "kitchen") == "Spotify Connect (Living Room)"
    )


def test_the_own_player_lists_its_sources_by_their_names() -> None:
    """The source list of a player names the sources that belong to it without the player."""
    home = _Home()

    assert _entry_name(home.living_room, LINE_IN) == "Line-in"
    assert _entry_name(home.kitchen, SPOTIFY) == "Spotify Connect"


def test_the_input_of_a_device_is_listed_by_its_name_as_an_input() -> None:
    """The line-in of Living Room is listed there under its name, as an input."""
    home = _Home()

    entries = [x for x in home.living_room.source_list if x.id == LINE_IN.uri]

    assert [(x.name, x.is_input) for x in entries] == [("Line-in", True)]


def test_the_entry_of_an_input_is_named_by_the_naming_rule() -> None:
    """An input of the device is named for the player like every other source."""
    home = _Home()
    home.controller.get_audio_source_name = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda source, shown_on=None: f"{source.name} on {shown_on}"
    )

    home.refresh()

    assert _entry_name(home.living_room, LINE_IN) == "Line-in on living_room"


@pytest.mark.parametrize("own_session", [True, False], ids=["playing", "not_playing"])
def test_a_source_on_its_own_player_shows_by_its_name(own_session: bool) -> None:
    """The entry of the session and the title name the source alone on its own player."""
    home = _Home()
    if own_session:
        home.play(home.living_room, LINE_IN)

    assert _entry_name(home.living_room, LINE_IN) == "Line-in"
    assert _title(home.living_room) == ("Line-in" if own_session else None)


def test_a_source_on_another_player_shows_with_its_player() -> None:
    """Kitchen lists and titles the line-in of Living Room with Living Room after it."""
    home = _Home()

    home.play(home.kitchen, LINE_IN)

    assert _entry_name(home.kitchen, LINE_IN) == "Line-in (Living Room)"
    assert _title(home.kitchen) == "Line-in (Living Room)"
    # its own player keeps listing it by its name alone
    assert _entry_name(home.living_room, LINE_IN) == "Line-in"


def test_each_member_of_a_group_names_the_source_for_itself() -> None:
    """
    The group and its members show the source by the name that holds where they are.

    Downstairs plays the line-in of Living Room: the group and Kitchen show it with Living
    Room after it, Living Room itself by its name alone.
    """
    home = _Home()

    home.play(home.group, LINE_IN)

    assert _entry_name(home.group, LINE_IN) == "Line-in (Living Room)"
    assert _title(home.group) == "Line-in (Living Room)"
    assert home.living_room.active_group == "downstairs"
    assert _title(home.living_room) == "Line-in"
    assert _title(home.kitchen) == "Line-in (Living Room)"


def test_a_member_of_a_group_in_a_group_names_the_source_for_itself() -> None:
    """
    A member finds the source of the group that plays it, also through a group in between.

    Everywhere plays the line-in of Living Room, and holds Downstairs, which holds Living Room
    and Kitchen.
    """
    home = _Home()

    home.play(home.everywhere, LINE_IN)

    assert home.living_room.active_group == "downstairs"
    assert home.group.active_group == "everywhere"
    assert [_title(x) for x in (home.everywhere, home.group, home.living_room, home.kitchen)] == [
        "Line-in (Living Room)",
        "Line-in (Living Room)",
        "Line-in",
        "Line-in (Living Room)",
    ]


def _upstairs_holding(home: _Home, source: AudioSource, title: str | None = None) -> None:
    """
    Let the group Upstairs, which holds neither Living Room nor Kitchen, hold a source.

    :param home: The players.
    :param source: The source it still holds, as while it moves to another group.
    :param title: The title it reported last.
    """
    upstairs, _ = attach_mock(
        MockProvider("upstairs", instance_id="upstairs", mass=home.mass),
        "proto_upstairs",
        "Upstairs",
        role=ProtocolRole.VIRTUAL,
        player_id="upstairs",
        player_type=PlayerType.GROUP,
    )
    session = home.controller._start_audio_source_session("upstairs", source, source.provider)
    if title is not None:
        session.stream_metadata = StreamMetadata(title=title)
    upstairs.refresh_state(signal_event=False)


def test_a_member_follows_the_source_of_its_own_group_during_a_handoff() -> None:
    """
    A member shows the source as its own group plays it, not as another group still does.

    While the line-in moves from Upstairs to Downstairs, Upstairs still holds it with the
    title it reported last.
    """
    home = _Home()
    _upstairs_holding(home, LINE_IN, title="Blue in Green")

    home.play(home.group, LINE_IN)

    assert _title(home.living_room) == "Line-in"
    assert _title(home.kitchen) == "Line-in (Living Room)"


def test_a_member_names_the_source_as_its_own_group_holds_it_during_a_handoff() -> None:
    """A source renamed while it moves shows by the name the session of its own group has."""
    home = _Home()
    _upstairs_holding(home, LINE_IN)

    home.play(home.group, replace(LINE_IN, name="Record player"))

    assert _title(home.living_room) == "Record player"
    assert _title(home.kitchen) == "Record player (Living Room)"


def test_a_title_the_source_reports_shows_on_every_player() -> None:
    """What the source reports is the title on the group and on each of its members."""
    home = _Home()

    home.play(home.group, LINE_IN, title="Blue in Green")

    assert [_title(x) for x in (home.group, home.living_room, home.kitchen)] == [
        "Blue in Green"
    ] * 3


def test_a_renamed_input_shows_its_new_name_while_it_plays() -> None:
    """A provider that hands the session the renamed source is followed at once, everywhere."""
    home = _Home()
    home.play(home.group, LINE_IN)

    home.controller.refresh_source("downstairs", replace(LINE_IN, name="Record player"))
    home.refresh()

    assert _title(home.group) == "Record player (Living Room)"
    assert _title(home.living_room) == "Record player"
    assert _title(home.kitchen) == "Record player (Living Room)"
    assert _entry_name(home.group, LINE_IN) == "Record player (Living Room)"


async def test_a_source_starts_with_its_name_on_that_player() -> None:
    """The media a source starts with, which a speaker may show, names it for that player."""
    home = _Home()

    await home.controller._start_audio_source(
        home.kitchen, LINE_IN, home.mass.get_provider("sendspin")
    )

    media = home.controller._handle_play_media.await_args.args[1]  # type: ignore[attr-defined]
    assert media.title == "Line-in (Living Room)"


async def test_a_client_gets_the_source_named_for_outside_a_player() -> None:
    """The item command names the source with its player, which it gives by its player id."""
    home = _Home()

    item: Any = await home.music.get_item(MediaType.AUDIO_SOURCE, "lr", "sendspin")

    assert (item.name, item.owner_player_id) == ("Line-in (Living Room)", "living_room")
    # what the provider delivers is left as it is
    assert (LINE_IN.name, LINE_IN.owner_player_id) == ("Line-in", "proto_living_room")


async def test_a_selected_source_plays_with_its_own_name() -> None:
    """A session gets the source as its provider delivers it, so its name is composed once."""
    home = _Home()

    resolved = await home.controller._resolve_audio_source_uri(str(LINE_IN.uri))

    assert resolved is not None
    assert resolved[0].name == "Line-in"


# the line-in of Living Room's speaker as Kitchen's speaker lists it, by the protocol id
LINE_IN_OF_LIVING_ROOM = PlayerSource(
    id="line_in@lr", name="Line-in", is_input=True, owner_player_id="proto_living_room"
)


@pytest.mark.parametrize(
    ("shown_on", "expected"),
    [("living_room", "Line-in"), ("kitchen", "Line-in (Living Room)")],
    ids=["own_player", "other_player"],
)
def test_an_input_of_another_player_is_named_by_the_same_rule(shown_on: str, expected: str) -> None:
    """An entry of a source list that names its player is named like a live source."""
    home = _Home()

    assert home.controller.get_audio_source_owner(LINE_IN_OF_LIVING_ROOM) is home.living_room
    assert home.controller.get_audio_source_name(LINE_IN_OF_LIVING_ROOM, shown_on) == expected


def test_a_player_lists_an_input_of_another_player_with_that_player() -> None:
    """
    Kitchen lists the line-in of Living Room's speaker with Living Room after it, as an input.

    The id stays the one the speaker selects it by, and the entry names Living Room by its
    player id. Its own line-in keeps its name.
    """
    home = _Home()
    protocol = home.kitchen.own_protocol
    assert protocol is not None
    own_line_in = PlayerSource(id="line_in", name="Line-in", is_input=True)
    protocol._attr_source_list = [own_line_in, LINE_IN_OF_LIVING_ROOM]

    home.refresh()

    entries = [x for x in home.kitchen.source_list if x.id.startswith("line_in")]
    assert [(x.id, x.name, x.is_input, x.owner_player_id) for x in entries] == [
        ("line_in", "Line-in", True, None),
        ("line_in@lr", "Line-in (Living Room)", True, "living_room"),
    ]
    # what the speaker lists is left as it is
    assert protocol._attr_source_list == [own_line_in, LINE_IN_OF_LIVING_ROOM]
    assert LINE_IN_OF_LIVING_ROOM.name == "Line-in"


def test_an_input_of_a_player_music_assistant_does_not_have_is_left_out() -> None:
    """An input that names no player Music Assistant has, such as a disabled one, is not listed."""
    home = _Home()
    protocol = home.kitchen.own_protocol
    assert protocol is not None
    protocol._attr_source_list = [replace(LINE_IN_OF_LIVING_ROOM, owner_player_id="proto_gone")]

    home.refresh()

    assert not [x for x in home.kitchen.source_list if x.id == LINE_IN_OF_LIVING_ROOM.id]


@pytest.mark.parametrize(
    ("player_filter", "allowed"),
    [(["kitchen"], False), (["kitchen", "living_room"], True), ([], True)],
    ids=["kitchen_only", "both_players", "unrestricted"],
)
async def test_an_input_of_another_player_needs_access_to_that_player(
    player_filter: list[str], allowed: bool
) -> None:
    """
    Selecting Living Room's line-in on Kitchen needs access to Living Room as well.

    A user who may only use Kitchen is refused before anything happens to Kitchen.
    """
    home = _Home()
    protocol = home.kitchen.own_protocol
    assert protocol is not None
    protocol._attr_source_list = [LINE_IN_OF_LIVING_ROOM]
    home.refresh()
    home.controller._handle_select_source = AsyncMock()  # type: ignore[method-assign]
    drop_pending_step = home.mass.player_queues._drop_pending_step

    with as_user(player_filter):
        if allowed:
            await home.controller.select_source("kitchen", LINE_IN_OF_LIVING_ROOM.id)
        else:
            with pytest.raises(InsufficientPermissions):
                await home.controller.select_source("kitchen", LINE_IN_OF_LIVING_ROOM.id)

    if allowed:
        home.controller._handle_select_source.assert_awaited_once_with(
            "kitchen", LINE_IN_OF_LIVING_ROOM.id
        )
    else:
        home.controller._handle_select_source.assert_not_awaited()
        drop_pending_step.assert_not_called()


def test_a_member_finds_the_input_its_leader_plays_by_the_same_id() -> None:
    """
    Living Room follows Kitchen, which plays Living Room's line-in, so both rooms hear it.

    Living Room takes the source of Kitchen and lists it by the same id, by its name alone;
    Kitchen lists it with Living Room after it.
    """
    home = _Home()
    living_room = home.living_room.own_protocol
    kitchen = home.kitchen.own_protocol
    assert living_room is not None
    assert kitchen is not None
    living_room._attr_source_list = [replace(LINE_IN_OF_LIVING_ROOM, owner_player_id=None)]
    kitchen._attr_source_list = [LINE_IN_OF_LIVING_ROOM]
    kitchen._attr_group_members = ["proto_kitchen", "proto_living_room"]
    kitchen._attr_active_source = LINE_IN_OF_LIVING_ROOM.id
    kitchen._attr_playback_state = PlaybackState.PLAYING

    home.kitchen.refresh_state(signal_event=False)
    home.living_room.refresh_state(signal_event=False)

    assert home.living_room.synced_to == "kitchen"
    for player, name in ((home.kitchen, "Line-in (Living Room)"), (home.living_room, "Line-in")):
        assert player.active_source == LINE_IN_OF_LIVING_ROOM.id
        assert [x.name for x in player.source_list if x.id == player.active_source] == [name]
