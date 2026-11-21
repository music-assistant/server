"""
Tests for the list of live sources a user can start, and the inputs a player can switch to.

Each entry carries the plain name of the source and the player it belongs to. A player adds
the inputs it can switch to, and a source of a player the user may not use is left out.
"""

from __future__ import annotations

from unittest.mock import MagicMock, Mock

from music_assistant_models.enums import PlayerType, ProviderFeature
from music_assistant_models.media_items import AudioSource, BrowseFolder, ProviderMapping
from music_assistant_models.player import PlayerSource

from music_assistant.controllers.music import MusicController
from music_assistant.controllers.players import PlayerController
from music_assistant.models.plugin import PluginProvider
from tests.common import MockProvider, as_user, attach_mock, mock_player_mass


def _source(
    provider: str, item_id: str, name: str, owner: str | None, can_initiate: bool = True
) -> AudioSource:
    """Return a live source as its provider delivers it."""
    return AudioSource(
        item_id=item_id,
        provider=provider,
        name=name,
        provider_mappings={
            ProviderMapping(item_id=item_id, provider_domain=provider, provider_instance=provider)
        },
        can_initiate=can_initiate,
        owner_player_id=owner,
    )


# the line-in names its player by the id of the protocol of its device
LINE_IN = _source("sendspin", "lr", "Line-in", "proto_living_room")
RECORD_PLAYER = _source("sendspin", "rp", "Record player", "proto_record_player")
SPOTIFY = _source("spotify_connect", "kitchen", "Spotify Connect", "kitchen")
# a receiver that only the app on a phone can start
AIRPLAY = _source("airplay_receiver", "kitchen", "AirPlay Receiver", "kitchen", can_initiate=False)
VBAN = _source("vban", "main", "VBAN Receiver", None)

TV = PlayerSource(id="hdmi", name="TV", is_input=True)

type Entry = tuple[str | None, str, str | None, bool]


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

    Living Room has a line-in that plays live, and switches to its own TV, Optical and
    TuneIn. Kitchen runs Spotify Connect and an AirPlay receiver, and switches to its own
    line-in. The record player is a device that is only an input.
    """

    def __init__(self) -> None:
        """Create the players and the providers of the sources."""
        self.mass = mock_player_mass()
        self.mass.config.get_raw_core_config_value = MagicMock(return_value="GLOBAL")
        self.players = PlayerController(self.mass)
        self.mass.players = self.players
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
            "airplay_receiver": _SourcePlugin(
                "airplay_receiver", [AIRPLAY], bound={"kitchen": [AIRPLAY]}
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
        _, self.living_room_protocol = attach_mock(
            provider, "proto_living_room", "Living Room", player_id="living_room"
        )
        self.living_room_protocol._attr_source_list = [
            TV,
            PlayerSource(id="optical", name="Optical", is_input=True, passive=True),
            PlayerSource(id="tunein", name="TuneIn"),
        ]
        _, kitchen_protocol = attach_mock(provider, "proto_kitchen", "Kitchen", player_id="kitchen")
        kitchen_protocol._attr_source_list = [
            PlayerSource(id="line_in", name="Line-in", is_input=True)
        ]
        attach_mock(
            provider,
            "proto_record_player",
            "Record player",
            player_id="record_player",
            player_type=PlayerType.SOURCE,
        )
        self.refresh()

    async def live_sources(self, player_id: str | None = None) -> list[Entry]:
        """
        Return the entries of the list of live sources, sorted.

        :param player_id: The player to list them for, None for all players.
        """
        entries = await self.music.live_sources(player_id)
        return sorted(
            ((x.id, x.name, x.owner_player_id, x.is_input) for x in entries),
            key=str,
        )

    def sync_kitchen_to_living_room(self) -> None:
        """Sync Kitchen to Living Room, as the protocol of Living Room reports it."""
        self.living_room_protocol._attr_group_members = ["proto_living_room", "proto_kitchen"]
        self.refresh()

    def refresh(self) -> None:
        """Derive the state of every player."""
        for player in list(self.players._players.values()):
            player.refresh_state(signal_event=False)


def _entries(*entries: Entry) -> list[Entry]:
    """Return the given entries, sorted as the list of the home is."""
    return sorted(entries, key=str)


LINE_IN_ENTRY: Entry = (LINE_IN.uri, "Line-in", "living_room", True)
RECORD_PLAYER_ENTRY: Entry = (RECORD_PLAYER.uri, "Record player", "record_player", True)
SPOTIFY_ENTRY: Entry = (SPOTIFY.uri, "Spotify Connect", "kitchen", False)
VBAN_ENTRY: Entry = (VBAN.uri, "VBAN Receiver", None, False)
TV_ENTRY: Entry = ("hdmi", "TV", "living_room", True)
KITCHEN_LINE_IN_ENTRY: Entry = ("line_in", "Line-in", "kitchen", True)


async def test_without_a_player_every_live_source_is_listed() -> None:
    """The live sources of all players are listed by their own names, without any inputs."""
    home = _Home()

    assert await home.live_sources() == _entries(
        LINE_IN_ENTRY, RECORD_PLAYER_ENTRY, SPOTIFY_ENTRY, VBAN_ENTRY
    )


async def test_a_player_adds_the_inputs_it_can_switch_to() -> None:
    """
    A player lists the live sources it can start and its inputs that can be selected.

    A source bound to another player, an input that can not be selected, an entry that is
    not an input and a source that can not be started from here are left out; the line-in
    of the player itself is listed once.
    """
    home = _Home()

    assert await home.live_sources("living_room") == _entries(
        LINE_IN_ENTRY, RECORD_PLAYER_ENTRY, VBAN_ENTRY, TV_ENTRY
    )
    assert await home.live_sources("kitchen") == _entries(
        LINE_IN_ENTRY, RECORD_PLAYER_ENTRY, SPOTIFY_ENTRY, VBAN_ENTRY, KITCHEN_LINE_IN_ENTRY
    )


async def test_a_player_synced_to_another_lists_no_inputs() -> None:
    """A player that plays what another player plays lists the live sources only."""
    home = _Home()
    home.sync_kitchen_to_living_room()

    assert home.players.get_player("kitchen").synced_to == "living_room"  # type: ignore[union-attr]
    assert await home.live_sources("kitchen") == _entries(
        LINE_IN_ENTRY, RECORD_PLAYER_ENTRY, SPOTIFY_ENTRY, VBAN_ENTRY
    )


async def test_a_restricted_user_gets_the_sources_of_their_players_only() -> None:
    """A source or input of a player the user may not use is left out, one of no player stays."""
    home = _Home()

    with as_user(["kitchen"]):
        unscoped = await home.live_sources()
        living_room = await home.live_sources("living_room")
        kitchen = await home.live_sources("kitchen")

    assert unscoped == _entries(SPOTIFY_ENTRY, VBAN_ENTRY)
    assert living_room == _entries(VBAN_ENTRY)
    assert kitchen == _entries(SPOTIFY_ENTRY, VBAN_ENTRY, KITCHEN_LINE_IN_ENTRY)


async def test_a_restricted_user_keeps_the_sources_of_their_own_client_player() -> None:
    """The private client player the user connected on counts as one of their players."""
    home = _Home()
    home.living_room_protocol._attr_private = True
    home.refresh()

    with as_user(["kitchen"], client_player_id="proto_living_room"):
        unscoped = await home.live_sources()
        living_room = await home.live_sources("living_room")

    assert unscoped == _entries(LINE_IN_ENTRY, SPOTIFY_ENTRY, VBAN_ENTRY)
    assert living_room == _entries(LINE_IN_ENTRY, VBAN_ENTRY, TV_ENTRY)


async def test_the_browse_folder_names_each_source_for_outside_a_player() -> None:
    """The folder holds the live sources of the list, each named with the player it belongs to."""
    home = _Home()

    root = await home.music.browse(path=None)
    listing = await home.music.browse(path="live_sources://")

    assert [(x.path, x.name) for x in root if isinstance(x, BrowseFolder)] == [
        ("live_sources://", "Live sources")
    ]
    assert sorted(x.name for x in listing[1:]) == [
        "Line-in (Living Room)",
        "Record player",
        "Spotify Connect (Kitchen)",
        "VBAN Receiver",
    ]
