"""
End-to-end tests for Rotation playback through the real queue controller.

Boots a hermetic MusicAssistant (fake `test` music provider + demo players), loads the
rotation_playlist plugin, fills the library with a few "Die drei ???" albums and plays a Rotation
that keeps the albums of one artist in random order, each album in track order and without its
intro tracks. Playback reports are sent the way the playback tracker sends them.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Any, cast

import pytest
from music_assistant_models.enums import (
    ContentType,
    EventType,
    MediaType,
    PlaybackState,
    QueueOption,
)
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    ProviderMapping,
    Track,
    UniqueList,
)
from music_assistant_models.playback_progress_report import MediaItemPlaybackProgressReport

from music_assistant.controllers.player_queues import managed_pool
from music_assistant.controllers.webserver.helpers.auth_middleware import set_current_user
from music_assistant.helpers.json import serialize_to_json
from music_assistant.providers.rotation_playlist import RotationPlaylistProvider
from music_assistant.providers.rotation_playlist.constants import HOUSEHOLD
from music_assistant.providers.rotation_playlist.rounds import derived_seed

from .conftest import demo_players, wait_for

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant_models.auth import User
    from music_assistant_models.media_items import Playlist
    from music_assistant_models.queue_item import QueueItem

    from music_assistant.mass import MusicAssistant

DIE_DREI = "Die drei ???"
GLOCKEN = "Böser die Glocken nie klingen"
# the chapters of each album; every album also starts with an intro and the title music
ALBUMS: dict[str, list[str]] = {
    GLOCKEN: [
        "Tag 01 - Unbekannter Herkunft (Teil 01)",
        "Tag 01 - Unbekannter Herkunft (Teil 02)",
        "Tag 02 - Die Spur im Schnee (Teil 01)",
        "Tag 02 - Die Spur im Schnee (Teil 02)",
    ],
    "Toteninsel": [
        "Toteninsel - Der Auftrag",
        "Toteninsel - Ankunft auf der Insel",
        "Toteninsel - Das Labor",
        "Toteninsel - Die Flucht",
    ],
    "Der Fluch des Rubins": [
        "Der Fluch des Rubins - Ein seltsamer Besucher",
        "Der Fluch des Rubins - Im Museum",
        "Der Fluch des Rubins - Die Verfolgung",
        "Der Fluch des Rubins - Das Versteck",
    ],
    "Das Erbe des Drachen": [
        "Das Erbe des Drachen - Ein Brief aus Wales",
        "Das Erbe des Drachen - Die Burg",
        "Das Erbe des Drachen - Der Drache erwacht",
        "Das Erbe des Drachen - Die Lösung",
    ],
}
OTHER_ARTIST = "TKKG"
OTHER_ALBUM = "Die Jagd nach den Millionendieben"
OTHER_TRACKS = ["Die Millionendiebe (Teil 1)", "Die Millionendiebe (Teil 2)"]
INTRO_WORDS = ("Inhaltsangabe", "Titelmusik")
POOL_TARGET = 6
TRACKS_PER_ALBUM = 4


def _album_tracks(album_name: str) -> list[str]:
    """Return all track titles of an album in track order, the intro tracks first."""
    return [
        f"{album_name} (Inhaltsangabe)",
        f"{album_name} (Titelmusik)",
        *ALBUMS.get(album_name, OTHER_TRACKS),
    ]


def _mapping(provider_instance: str, item_id: str) -> set[ProviderMapping]:
    return {
        ProviderMapping(
            item_id=item_id,
            provider_domain="test",
            provider_instance=provider_instance,
            in_library=True,
        )
    }


async def _add_artist_albums(
    mass: MusicAssistant, instance: str, number: int, artist_name: str, album_names: list[str]
) -> Artist:
    """
    Add an artist with its albums and tracks to the library and return the library artist.

    The tracks are added in reverse order, so the library order is not the track order.

    :param mass: The Music Assistant instance.
    :param instance: The instance id of the fake music provider.
    :param number: A number that keeps the provider item ids unique.
    :param artist_name: The name of the artist.
    :param album_names: The names of the albums of the artist.
    """
    artist = Artist(
        item_id=f"{number}",
        provider=instance,
        name=artist_name,
        provider_mappings=_mapping(instance, f"{number}"),
    )
    library_artist = await mass.music.artists.add_item_to_library(artist)
    for album_number, album_name in enumerate(album_names):
        album_id = f"{number}_{album_number}"
        album = Album(
            item_id=album_id,
            provider=instance,
            name=album_name,
            artists=UniqueList([artist]),
            provider_mappings=_mapping(instance, album_id),
        )
        await mass.music.albums.add_item_to_library(album)
        titles = _album_tracks(album_name)
        for track_number in reversed(range(1, len(titles) + 1)):
            track_id = f"{album_id}_{track_number}"
            track = Track(
                item_id=track_id,
                provider=instance,
                name=titles[track_number - 1],
                duration=600,
                artists=UniqueList([artist]),
                album=album,
                disc_number=1,
                track_number=track_number,
                provider_mappings=_mapping(instance, track_id),
            )
            await mass.music.tracks.add_item_to_library(track)
    return library_artist


def _steps(artist_uri: str) -> list[dict[str, Any]]:
    """Return the steps: the artist's albums in random order, each in track order, no intros."""
    return [
        {"type": "source", "media_type": "album"},
        {
            "type": "filter",
            "mode": "keep",
            "match": "all",
            "conditions": [{"field": "artist", "op": "is", "value": artist_uri}],
        },
        {"type": "sort", "keys": [{"field": "random"}]},
        {"type": "expand", "to": "track"},
        {
            "type": "filter",
            "mode": "remove",
            "match": "any",
            "conditions": [
                {"field": "title", "op": "contains", "value": word} for word in INTRO_WORDS
            ],
        },
        {"type": "sort", "keys": [{"field": "track_number"}]},
    ]


class Scenario:
    """The loaded Rotation with its provider, playlist and steps."""

    def __init__(
        self,
        mass: MusicAssistant,
        provider: RotationPlaylistProvider,
        playlist: Playlist,
        steps: list[dict[str, Any]],
    ) -> None:
        """
        Initialize the scenario.

        :param mass: The Music Assistant instance.
        :param provider: The Rotation provider.
        :param playlist: The library playlist of the Rotation.
        :param steps: The steps document of the Rotation.
        """
        self.mass = mass
        self.provider = provider
        self.playlist = playlist
        self.steps = steps
        self.rotation_id = next(iter(provider._steps))

    def seed(self, key: str) -> int:
        """
        Return the seed of the current round of a user.

        :param key: The user id, or the household key.
        """
        return self.provider._progress.get(self.rotation_id, key).seed

    def played(self, key: str) -> set[str]:
        """
        Return the library uris a user already played in the current round, for any unit.

        The progress also keeps the provider uris of each track, which are left out here.

        :param key: The user id, or the household key.
        """
        state = self.provider._progress.get(self.rotation_id, key)
        played = state.played.union(
            *(uris for plays in state.unit_plays.values() for uris in plays.values())
        )
        return {uri for uri in played if uri.startswith("library://")}

    async def expected(self, seed: int) -> list[tuple[str, list[str]]]:
        """
        Return the albums and their tracks in play order for a seed.

        :param seed: The seed of the round.
        """
        preview = await self.provider.preview(self.steps, limit=200, seed=seed)
        return [
            (group["item"]["name"], [entry["track"]["name"] for entry in group["tracks"]])
            for group in preview["groups"]
        ]

    async def play(self, queue_id: str, user: User | None) -> None:
        """
        Play the Rotation on a queue as a user, like a client request does.

        :param queue_id: The queue to play on.
        :param user: The user who plays, or None for the household.
        """
        set_current_user(user)
        try:
            await self.mass.player_queues.play_media(queue_id, self.playlist)
        finally:
            set_current_user(None)
        assert await wait_for(lambda: bool(self.mass.player_queues.items(queue_id)))

    async def report_played(self, queue_id: str, items: list[QueueItem]) -> None:
        """
        Send the playback report of started items, as the playback tracker does.

        :param queue_id: The queue that plays the items.
        :param items: The queue items that started to play.
        """
        queue_data = self.mass.player_queues.queue_data(queue_id)
        key = queue_data.userid or HOUSEHOLD
        for item in items:
            media_item = item.media_item
            assert media_item is not None
            uri = str(media_item.uri)
            report = MediaItemPlaybackProgressReport(
                uri=uri,
                media_type=MediaType.TRACK,
                name=media_item.name,
                duration=600,
                seconds_played=30,
                fully_played=False,
                is_playing=True,
                userid=queue_data.userid,
                player_id=queue_id,
            )
            self.mass.player_queues._report_to_scrobblers(report)
            assert await wait_for(partial(self.is_played, key, uri), timeout=5)

    def is_played(self, key: str, uri: str) -> bool:
        """
        Return whether a user already played a track in the current round.

        :param key: The user id, or the household key.
        :param uri: The track uri.
        """
        return uri in self.played(key)

    async def refill(self, queue_id: str, current_index: int) -> None:
        """
        Move the queue to an index and top it up, as the playback tracker does when it runs low.

        :param queue_id: The queue to refill.
        :param current_index: The index of the item that plays now.
        """
        queue = self.mass.player_queues.get(queue_id)
        assert queue is not None
        queue.current_index = current_index
        queue.current_item = self.mass.player_queues.items(queue_id)[current_index]
        await self.mass.player_queues._fill_dynamic_tracks(queue_id)

    async def start(self, queue_id: str, index: int) -> None:
        """
        Make a queue item the playing one, as the queue announces it when a new item starts.

        No playback report follows, as for an item that is skipped within a few seconds.

        :param queue_id: The queue that plays the item.
        :param index: The index of the item that starts.
        """
        queue = self.mass.player_queues.get(queue_id)
        assert queue is not None
        item = self.mass.player_queues.items(queue_id)[index]
        assert item.media_item is not None
        queue.current_index = index
        queue.current_item = item
        queue.state = PlaybackState.PLAYING
        self.mass.signal_event(EventType.QUEUE_UPDATED, object_id=queue_id, data=queue)
        key = self.mass.player_queues.queue_data(queue_id).userid or HOUSEHOLD
        uri = str(item.media_item.uri)
        assert await wait_for(partial(self.is_played, key, uri), timeout=5)

    async def pin_seed(self, key: str, same_album_at_seam: bool) -> None:
        """
        Give a user a round 1 seed that does or does not repeat its last album first in round 2.

        :param key: The user id, or the household key.
        :param same_album_at_seam: True to start round 2 with the last album of round 1.
        """
        for seed in range(1000):
            round_one = await self.expected(seed)
            round_two = await self.expected(derived_seed(seed, 2))
            if (round_one[-1][0] == round_two[0][0]) == same_album_at_seam:
                self.provider._progress._stored(self.rotation_id, key).seed = seed
                return
        pytest.fail("no seed found")


def _titles(mass: MusicAssistant, queue_id: str) -> list[str]:
    return [
        item.media_item.name if item.media_item else ""
        for item in mass.player_queues.items(queue_id)
    ]


def _uri_at(mass: MusicAssistant, queue_id: str, index: int) -> str:
    media_item = mass.player_queues.items(queue_id)[index].media_item
    assert media_item is not None
    return str(media_item.uri)


def _flat(expected: list[tuple[str, list[str]]]) -> list[str]:
    return [title for _album, titles in expected for title in titles]


@pytest.fixture
async def scenario(
    e2e_mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[Scenario]:
    """Load the Rotation provider, fill the library and create the Die drei ??? Rotation."""
    # a small pool, so playing through the Rotation takes several refills
    monkeypatch.setattr(managed_pool, "MANAGED_POOL_TARGET", POOL_TARGET)
    test_prov = e2e_mass.get_provider("test")
    assert test_prov is not None
    library_artist = await _add_artist_albums(
        e2e_mass, test_prov.instance_id, 900, DIE_DREI, list(ALBUMS)
    )
    await _add_artist_albums(e2e_mass, test_prov.instance_id, 901, OTHER_ARTIST, [OTHER_ALBUM])
    await e2e_mass.config._create_provider_instance("rotation_playlist", {})
    assert await wait_for(lambda: e2e_mass.get_provider("rotation_playlist") is not None)
    provider = cast("RotationPlaylistProvider", e2e_mass.get_provider("rotation_playlist"))
    steps = _steps(f"library://artist/{library_artist.item_id}")
    playlist = await provider.create(DIE_DREI, steps)
    yield Scenario(e2e_mass, provider, playlist, steps)
    set_current_user(None)


@pytest.mark.asyncio
async def test_rotation_fills_queue_with_whole_albums_in_order(scenario: Scenario) -> None:
    """Playing a Rotation fills the queue with whole albums in track order, without intros."""
    mass = scenario.mass
    queue_id = demo_players(mass)[0].player_id
    user = await mass.webserver.auth.create_user(username="justus")
    expected = await scenario.expected(scenario.seed(user.user_id))

    # the steps keep the four Die drei ??? albums, each with its chapters in track order
    assert sorted(name for name, _titles in expected) == sorted(ALBUMS)
    assert all(titles == ALBUMS[name] for name, titles in expected)

    await scenario.play(queue_id, user)

    titles = _titles(mass, queue_id)
    assert titles == _flat(expected)[:POOL_TARGET]
    assert not any(word in title for title in titles for word in INTRO_WORDS)
    queue = mass.player_queues.get(queue_id)
    assert queue is not None
    assert queue.is_dynamic
    assert [item.name for item in mass.player_queues.queue_data(queue_id).source_items] == [
        DIE_DREI
    ]


@pytest.mark.asyncio
async def test_rotation_refill_continues_in_order(scenario: Scenario) -> None:
    """After the queue advances, a refill continues with the next unplayed tracks in order."""
    mass = scenario.mass
    queue_id = demo_players(mass)[0].player_id
    user = await mass.webserver.auth.create_user(username="justus")
    order = _flat(await scenario.expected(scenario.seed(user.user_id)))
    await scenario.play(queue_id, user)

    # the first album played to its last chapter, which plays now
    await scenario.report_played(queue_id, mass.player_queues.items(queue_id)[:TRACKS_PER_ALBUM])
    await scenario.refill(queue_id, current_index=TRACKS_PER_ALBUM - 1)

    titles = _titles(mass, queue_id)
    assert len(titles) == len(set(titles))
    assert titles == order[: len(titles)]
    # the pool is topped up again: the current track plus a full pool of upcoming tracks
    assert len(titles) == TRACKS_PER_ALBUM + POOL_TARGET

    # once more, through the second album
    await scenario.report_played(
        queue_id, mass.player_queues.items(queue_id)[TRACKS_PER_ALBUM : 2 * TRACKS_PER_ALBUM]
    )
    await scenario.refill(queue_id, current_index=2 * TRACKS_PER_ALBUM - 1)

    titles = _titles(mass, queue_id)
    assert len(titles) == len(set(titles))
    assert titles == order[: len(titles)]
    assert len(titles) == 2 * TRACKS_PER_ALBUM + POOL_TARGET


@pytest.mark.asyncio
async def test_rotation_continues_after_clear_and_starts_fresh_for_others(
    scenario: Scenario,
) -> None:
    """Playing again after a clear continues the user's round; other users start their own."""
    mass = scenario.mass
    players = demo_players(mass)
    queue_id = players[0].player_id
    justus = await mass.webserver.auth.create_user(username="justus")
    peter = await mass.webserver.auth.create_user(username="peter")
    order = _flat(await scenario.expected(scenario.seed(justus.user_id)))
    await scenario.play(queue_id, justus)

    # the first album and the first chapter of the second album started
    await scenario.report_played(
        queue_id, mass.player_queues.items(queue_id)[: TRACKS_PER_ALBUM + 1]
    )
    mass.player_queues.clear(queue_id)
    assert not mass.player_queues.items(queue_id)

    await scenario.play(queue_id, justus)
    # the rest of the current album first, at the next unplayed chapter
    assert _titles(mass, queue_id) == order[TRACKS_PER_ALBUM + 1 :][:POOL_TARGET]

    # another user starts at the beginning of their own round, in their own order
    peter_order = _flat(await scenario.expected(scenario.seed(peter.user_id)))
    await scenario.play(players[1].player_id, peter)
    assert _titles(mass, players[1].player_id) == peter_order[:POOL_TARGET]
    # his first track starts to play, which counts for his own round only
    peter_first = {_uri_at(mass, players[1].player_id, 0)}
    assert await wait_for(lambda: scenario.played(peter.user_id) == peter_first, timeout=5)

    # without a user, the household has its own round as well
    household_order = _flat(await scenario.expected(scenario.seed(HOUSEHOLD)))
    await scenario.play(players[2].player_id, None)
    assert _titles(mass, players[2].player_id) == household_order[:POOL_TARGET]
    household_first = {_uri_at(mass, players[2].player_id, 0)}
    assert await wait_for(lambda: scenario.played(HOUSEHOLD) == household_first, timeout=5)
    # the plays of justus stay his own: the reported tracks and the one that started again
    justus_played = {_uri_at(mass, queue_id, 0), *scenario.played(justus.user_id)}
    assert len(justus_played) == TRACKS_PER_ALBUM + 2


@pytest.mark.parametrize("same_album_at_seam", [False, True])
@pytest.mark.asyncio
async def test_rotation_starts_round_two_after_every_album(
    scenario: Scenario, same_album_at_seam: bool
) -> None:
    """When every album played, the next refill starts round 2 with all albums again."""
    mass = scenario.mass
    queue_id = demo_players(mass)[0].player_id
    user = await mass.webserver.auth.create_user(username="justus")
    # round 2 may start with the album that ended round 1; it still plays complete and in order
    await scenario.pin_seed(user.user_id, same_album_at_seam)
    round_one = _flat(await scenario.expected(scenario.seed(user.user_id)))
    total = len(round_one)
    await scenario.play(queue_id, user)

    index = 0
    while index < total:
        items = mass.player_queues.items(queue_id)
        end = min(index + TRACKS_PER_ALBUM, total)
        await scenario.report_played(queue_id, items[index:end])
        await scenario.refill(queue_id, current_index=end - 1)
        index = end

    titles = _titles(mass, queue_id)
    # round 1 played completely, in order, each track once
    assert titles[:total] == round_one
    assert len(scenario.played(user.user_id)) == total
    # the refill after the last track already starts round 2, with a new album order
    round_two_seed = derived_seed(scenario.seed(user.user_id), 2)
    round_two = await scenario.expected(round_two_seed)
    assert sorted(name for name, _titles in round_two) == sorted(ALBUMS)
    assert titles[total:] == _flat(round_two)[:POOL_TARGET]

    # the round only moves on when a track of round 2 starts
    stored = scenario.provider._progress.get(scenario.rotation_id, user.user_id)
    assert stored.round == 1
    await scenario.report_played(queue_id, mass.player_queues.items(queue_id)[total : total + 1])
    stored = scenario.provider._progress.get(scenario.rotation_id, user.user_id)
    assert stored.round == 2
    first_of_round_two = mass.player_queues.items(queue_id)[total].media_item
    assert first_of_round_two is not None
    assert scenario.played(user.user_id) == {str(first_of_round_two.uri)}


@pytest.mark.asyncio
async def test_quickly_skipped_track_counts_as_played(scenario: Scenario) -> None:
    """A track skipped after a few seconds counts as played and is not queued again."""
    mass = scenario.mass
    queue_id = demo_players(mass)[0].player_id
    user = await mass.webserver.auth.create_user(username="justus")
    order = _flat(await scenario.expected(scenario.seed(user.user_id)))
    await scenario.play(queue_id, user)

    await scenario.start(queue_id, 0)
    await scenario.report_played(queue_id, mass.player_queues.items(queue_id)[:1])
    # the second chapter is skipped after 3 seconds, which sends no playback report
    await scenario.start(queue_id, 1)
    await scenario.start(queue_id, 2)
    await scenario.refill(queue_id, current_index=2)

    titles = _titles(mass, queue_id)
    assert len(titles) == len(set(titles))
    assert titles == order[: len(titles)]
    assert len(titles) == 3 + POOL_TARGET


@pytest.mark.parametrize("same_album_at_seam", [False, True])
@pytest.mark.asyncio
async def test_refill_before_the_last_track_is_reported_starts_round_two(
    scenario: Scenario, same_album_at_seam: bool
) -> None:
    """A refill while the last track of a round plays, before its report, starts round 2."""
    mass = scenario.mass
    queue_id = demo_players(mass)[0].player_id
    user = await mass.webserver.auth.create_user(username="justus")
    await scenario.pin_seed(user.user_id, same_album_at_seam)
    round_one = _flat(await scenario.expected(scenario.seed(user.user_id)))
    total = len(round_one)
    await scenario.play(queue_id, user)

    index = 0
    while index < total:
        items = mass.player_queues.items(queue_id)
        end = min(index + TRACKS_PER_ALBUM, total)
        # the last track of the round plays, but its first report is still to come
        await scenario.report_played(queue_id, items[index : min(end, total - 1)])
        await scenario.refill(queue_id, current_index=end - 1)
        index = end

    titles = _titles(mass, queue_id)
    assert titles[:total] == round_one
    round_two = _flat(await scenario.expected(derived_seed(scenario.seed(user.user_id), 2)))
    assert titles[total:] == round_two[:POOL_TARGET]

    # the first track of round 2 starts and is skipped at once: round 2 begins with it
    last_of_round_one = mass.player_queues.items(queue_id)[total - 1]
    await scenario.start(queue_id, total)
    stored = scenario.provider._progress.get(scenario.rotation_id, user.user_id)
    first_of_round_two = mass.player_queues.items(queue_id)[total].media_item
    assert first_of_round_two is not None
    assert stored.round == 2
    assert scenario.played(user.user_id) == {str(first_of_round_two.uri)}
    # the report the last track of round 1 sends when it ends does not count for round 2
    assert last_of_round_one.media_item is not None
    uri = str(last_of_round_one.media_item.uri)
    report = MediaItemPlaybackProgressReport(
        uri=uri,
        media_type=MediaType.TRACK,
        name=last_of_round_one.name,
        duration=600,
        seconds_played=40,
        fully_played=False,
        is_playing=False,
        userid=user.user_id,
        player_id=queue_id,
    )
    mass.player_queues._report_to_scrobblers(report)
    second = mass.player_queues.items(queue_id)[total + 1]
    await scenario.report_played(queue_id, [second])
    assert second.media_item is not None
    assert scenario.played(user.user_id) == {
        str(first_of_round_two.uri),
        str(second.media_item.uri),
    }


@pytest.mark.asyncio
async def test_play_next_track_keeps_rotation_tail(scenario: Scenario) -> None:
    """A track played next goes in after the current track; the Rotation's tail keeps its order."""
    mass = scenario.mass
    queue_id = demo_players(mass)[0].player_id
    user = await mass.webserver.auth.create_user(username="justus")
    await scenario.play(queue_id, user)
    before = _titles(mass, queue_id)

    extra = await _other_track(mass)
    set_current_user(user)
    try:
        await mass.player_queues.play_media(queue_id, extra, option=QueueOption.NEXT)
    finally:
        set_current_user(None)

    titles = _titles(mass, queue_id)
    assert extra.name in titles
    assert [title for title in titles if title != extra.name] == before


@pytest.mark.asyncio
async def test_add_track_goes_after_the_rotation_tail(scenario: Scenario) -> None:
    """
    A track added to the queue goes to the end; the Rotation's upcoming tracks keep their order.

    The queue of a Rotation alone is not shuffled, and the Rotation stays the only source, so
    later refills keep its order too.
    """
    mass = scenario.mass
    queue_id = demo_players(mass)[0].player_id
    user = await mass.webserver.auth.create_user(username="justus")
    await scenario.play(queue_id, user)
    before = _titles(mass, queue_id)
    assert mass.player_queues.queue_data(queue_id).queue.shuffle_enabled is False

    extra = await _other_track(mass)
    set_current_user(user)
    try:
        await mass.player_queues.play_media(queue_id, extra, option=QueueOption.ADD)
    finally:
        set_current_user(None)

    assert _titles(mass, queue_id) == [*before, extra.name]
    assert [item.name for item in mass.player_queues.queue_data(queue_id).source_items] == [
        DIE_DREI
    ]


@pytest.mark.asyncio
async def test_add_track_after_a_used_up_album_goes_after_the_rotation_tail(
    scenario: Scenario,
) -> None:
    """Once an added album is used up, a track added later still goes to the end as it is."""
    mass = scenario.mass
    queue_id = demo_players(mass)[0].player_id
    user = await mass.webserver.auth.create_user(username="justus")
    await scenario.play(queue_id, user)
    albums = await mass.music.albums.library_items(search=OTHER_ALBUM)
    assert albums
    set_current_user(user)
    try:
        await mass.player_queues.play_media(queue_id, albums[0], option=QueueOption.ADD)
    finally:
        set_current_user(None)
    queue_data = mass.player_queues.queue_data(queue_id)
    assert queue_data.queue.shuffle_enabled is True
    # the album plays out and the pool retires it, so the Rotation is the only source again
    mass.player_queues._managed_pool._retire_sources(queue_id, [str(albums[0].uri)])
    assert [item.name for item in queue_data.source_items] == [DIE_DREI]
    before = _titles(mass, queue_id)

    extra = await _other_track(mass)
    set_current_user(user)
    try:
        await mass.player_queues.play_media(queue_id, extra, option=QueueOption.ADD)
    finally:
        set_current_user(None)

    assert _titles(mass, queue_id) == [*before, extra.name]
    assert queue_data.queue.shuffle_enabled is False


async def _other_track(mass: MusicAssistant) -> Track:
    """Return the first chapter of the TKKG album from the library."""
    tracks = await mass.music.tracks.library_items(search=OTHER_TRACKS[0])
    assert tracks
    return tracks[0]


async def _album_names(scenario: Scenario, *steps: dict[str, Any]) -> list[str]:
    """Return the names of the albums that a preview of album steps lists, by name."""
    preview = await scenario.provider.preview(
        [{"type": "source", "media_type": "album"}, *steps, _BY_NAME], limit=200, seed=1
    )
    return [group["item"]["name"] for group in preview["groups"]]


def _keep(field: str, op: str, value: Any) -> dict[str, Any]:
    return {
        "type": "filter",
        "mode": "keep",
        "match": "all",
        "conditions": [{"field": field, "op": op, "value": value}],
    }


_BY_NAME = {"type": "sort", "keys": [{"field": "name"}]}


@pytest.mark.asyncio
async def test_filter_albums_by_values_from_the_library(scenario: Scenario) -> None:
    """Track count, play count and date favorited come from the library database."""
    mass = scenario.mass
    albums = {album.name: album for album in await mass.music.albums.library_items(limit=50)}
    assert await _album_names(scenario, _keep("track_count", "less_than", 5)) == [OTHER_ALBUM]
    assert await _album_names(scenario, _keep("track_count", "is", 6)) == sorted(ALBUMS)

    played = albums["Toteninsel"]
    await mass.music.database.execute(
        f"UPDATE albums SET play_count = 3, last_played = strftime('%s', 'now') "
        f"WHERE item_id = {int(played.item_id)}"
    )
    assert await _album_names(scenario, _keep("play_count", "more_than", 2)) == ["Toteninsel"]
    assert await _album_names(scenario, _keep("last_played", "in_last_days", 1)) == ["Toteninsel"]
    assert "Toteninsel" not in await _album_names(scenario, _keep("play_count", "is", 0))

    user = await mass.webserver.auth.create_user(username="justus")
    await mass.music.favorites.set(
        MediaType.ALBUM, int(albums[GLOCKEN].item_id), True, [user.user_id]
    )
    recent = _keep("date_favorited", "in_last_days", 1)
    assert await _album_names(scenario, recent) == []
    set_current_user(user)
    try:
        assert await _album_names(scenario, recent) == [GLOCKEN]
    finally:
        set_current_user(None)


async def _set_track_format(
    mass: MusicAssistant, track_id: str, audio_format: AudioFormat, available: bool = True
) -> None:
    """Give the mapping of a library track an audio format, as a music source reports it."""
    await mass.music.database.execute(
        "UPDATE provider_mappings SET audio_format = :audio_format, available = :available "
        "WHERE media_type = 'track' AND item_id = :item_id",
        {
            "audio_format": serialize_to_json(audio_format),
            "available": available,
            "item_id": int(track_id),
        },
    )


@pytest.mark.asyncio
async def test_album_plays_and_quality_come_from_its_tracks(scenario: Scenario) -> None:
    """An album counts the plays of its tracks and the best quality of the tracks it has."""
    mass = scenario.mass
    albums = {album.name: album for album in await mass.music.albums.library_items(limit=50)}
    tracks = {
        name: list(await mass.music.albums.tracks(album.item_id, "library", in_library_only=True))
        for name, album in albums.items()
    }
    await mass.music.database.execute(
        f"UPDATE albums SET play_count = 3 WHERE item_id = {int(albums['Toteninsel'].item_id)}"
    )
    rubin = tracks["Der Fluch des Rubins"]
    await mass.music.database.execute(
        "UPDATE tracks SET play_count = 2, last_played = strftime('%s', 'now') "
        f"WHERE item_id IN ({int(rubin[0].item_id)}, {int(rubin[1].item_id)})"
    )
    assert await _album_names(scenario, _keep("play_count", "more_than", 2)) == [
        "Der Fluch des Rubins",
        "Toteninsel",
    ]
    assert await _album_names(scenario, _keep("last_played", "in_last_days", 1)) == [
        "Der Fluch des Rubins"
    ]
    preview = await scenario.provider.preview(
        [
            {"type": "source", "media_type": "album"},
            {"type": "sort", "keys": [{"field": "play_count", "descending": True}]},
        ],
        limit=200,
        seed=1,
    )
    assert [group["item"]["name"] for group in preview["groups"]][:2] == [
        "Der Fluch des Rubins",
        "Toteninsel",
    ]

    hi_res = AudioFormat(content_type=ContentType.FLAC, bit_depth=24, sample_rate=96000)
    await _set_track_format(mass, tracks["Das Erbe des Drachen"][0].item_id, hi_res)
    await _set_track_format(mass, tracks[GLOCKEN][0].item_id, hi_res, available=False)
    mp3 = AudioFormat(content_type=ContentType.MP3)
    await _set_track_format(mass, tracks["Toteninsel"][0].item_id, mp3)
    assert await _album_names(scenario, _keep("audio_quality", "is", "hi_res")) == [
        "Das Erbe des Drachen"
    ]
    assert await _album_names(scenario, _keep("audio_quality", "is", "lossy")) == ["Toteninsel"]
