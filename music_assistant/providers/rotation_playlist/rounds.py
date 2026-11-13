"""Rounds and per-user progress of the Rotation playlists."""

from __future__ import annotations

import hashlib
import secrets
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import MediaType
from music_assistant_models.helpers import create_uri
from music_assistant_models.media_items import MediaItemType, Track

from .constants import BATCH_MAX_TRACKS, PREVIEW_COUNT_CAP, SEED_BITS, STORAGE_VERSION
from .engine import iter_units, playable_tracks
from .storage import read_json, write_json

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

    from .engine import RunContext
    from .models import Step

# keeps the tracks of a unit that can play for the user; the others are not part of the round
type TrackFilter = Callable[[list[Track]], Awaitable[list[Track]]]


@dataclass(frozen=True)
class HandedUnit:
    """A unit a batch took tracks from, with all its kept tracks in play order."""

    # each track by the uris it is known by
    tracks: list[frozenset[str]]
    # all tracks of the unit could be listed, so it counts as played once they all played
    complete: bool


# the units the batches take tracks from, by unit key, in the order they are handed out
type UnitTracks = dict[str, HandedUnit]


def track_uris(track: MediaItemType) -> set[str]:
    """
    Return the uris a track is known by: its own and those of its provider mappings.

    The queue may play another copy of a handed out track (for example the library copy of
    a provider track), which shares a provider mapping with it.

    :param track: The track.
    """
    uris = {str(track.uri)}
    if isinstance(track, Track):
        uris.update(
            create_uri(MediaType.TRACK, mapping.provider_instance, mapping.item_id)
            for mapping in track.provider_mappings
        )
    return uris


def unit_key(unit_uri: str, seen: dict[str, int]) -> str:
    """
    Return the key of the next occurrence of a unit in the play order of a round.

    A unit can occur more than once (a track on two albums, after the albums are mixed),
    so a later occurrence gets its number: the uri, then ``uri#2``, ``uri#3`` and so on.

    :param unit_uri: The uri of the unit.
    :param seen: How often each unit uri occurred so far; updated.
    """
    count = seen.get(unit_uri, 0) + 1
    seen[unit_uri] = count
    return unit_uri if count == 1 else f"{unit_uri}#{count}"


def played_for(state: RoundState, units: UnitTracks, uris: set[str]) -> str | None:
    """
    Return the key of the handed out unit a started track plays for, or None when none holds it.

    That is the first unit in the handed out order that holds the track more often than it
    played as part of that unit.

    :param state: The progress of the user.
    :param units: The units handed out in this round, in their order.
    :param uris: The uris the track is known by.
    """
    for key, unit in units.items():
        if key in state.played_units:
            continue
        holds = sum(1 for track in unit.tracks if track & uris)
        if holds and holds > sum(1 for play in state.plays_of(key) if play & uris):
            return key
    return None


def new_seed() -> int:
    """Return a new random seed for the random orders of a round."""
    return secrets.randbits(SEED_BITS)


def derived_seed(*parts: object) -> int:
    """Return a seed that always follows from the same parts."""
    text = ":".join(str(part) for part in parts)
    digest = hashlib.sha1(text.encode(), usedforsecurity=False).digest()
    return int.from_bytes(digest[:4]) & ((1 << SEED_BITS) - 1)


@dataclass
class RoundState:
    """The progress of one user in one Rotation: the round, its seed and what played."""

    round: int = 1
    seed: int = field(default_factory=new_seed)
    # the uris of tracks that played for no known unit (also all played tracks of the old
    # format); each counts for the first unit in play order that holds it
    played: set[str] = field(default_factory=set)
    # the tracks that played as part of each unit, by unit key and then by queue item id
    unit_plays: dict[str, dict[str, frozenset[str]]] = field(default_factory=dict)
    # the queue items whose play went to the played tracks of no unit
    pooled_items: set[str] = field(default_factory=set)
    # the top-level units (for example albums) whose tracks all played in this round
    played_units: set[str] = field(default_factory=set)

    def copy(self) -> RoundState:
        """Return a copy that can change without changing this state."""
        return RoundState(
            round=self.round,
            seed=self.seed,
            played=set(self.played),
            unit_plays={key: dict(plays) for key, plays in self.unit_plays.items()},
            pooled_items=set(self.pooled_items),
            played_units=set(self.played_units),
        )

    def advance(self) -> None:
        """Move to the next round; its seed follows from the old seed and the round."""
        self.round += 1
        self.seed = derived_seed(self.seed, self.round)
        self.played = set()
        self.unit_plays = {}
        self.pooled_items = set()
        self.played_units = set()

    def plays_of(self, key: str) -> list[frozenset[str]]:
        """
        Return the tracks that played as part of a unit, each by its uris.

        :param key: The unit key.
        """
        return list(self.unit_plays.get(key, {}).values())

    def has_play(self, item_id: str) -> bool:
        """
        Return whether the play of a queue item is already counted.

        :param item_id: The queue item id.
        """
        return item_id in self.pooled_items or any(
            item_id in plays for plays in self.unit_plays.values()
        )

    def add_play(self, uris: set[str], item_id: str | None = None, unit: str | None = None) -> bool:
        """
        Count a started track; return True when this changed the state.

        :param uris: The uris the track is known by.
        :param item_id: The queue item that plays it; without one the play counts for no unit.
        :param unit: The key of the unit it plays for, or None when it is not known.
        """
        if unit is None or item_id is None:
            if item_id is not None:
                self.pooled_items.add(item_id)
            if uris <= self.played:
                return False
            self.played |= uris
            return True
        plays = self.unit_plays.setdefault(unit, {})
        if item_id in plays:
            return False
        plays[item_id] = frozenset(uris)
        return True

    def to_dict(self) -> dict[str, Any]:
        """Return the state as a JSON-serializable dict."""
        return {
            "round": self.round,
            "seed": self.seed,
            "played": sorted(self.played),
            "pooled_items": sorted(self.pooled_items),
            "unit_plays": {
                key: {item_id: sorted(uris) for item_id, uris in plays.items()}
                for key, plays in self.unit_plays.items()
            },
            "played_units": sorted(self.played_units),
        }

    @classmethod
    def from_dict(cls, data: Any) -> RoundState:
        """
        Build a state from stored data; wrong types fall back to the defaults.

        :param data: The stored dict.
        """
        state = cls()
        if not isinstance(data, dict):
            return state
        round_number = data.get("round")
        if isinstance(round_number, int) and not isinstance(round_number, bool):
            state.round = max(1, round_number)
        seed = data.get("seed")
        if isinstance(seed, int) and not isinstance(seed, bool) and seed >= 0:
            state.seed = seed
        played = data.get("played")
        if isinstance(played, list):
            state.played = {uri for uri in played if isinstance(uri, str)}
        played_units = data.get("played_units")
        if isinstance(played_units, list):
            state.played_units = {uri for uri in played_units if isinstance(uri, str)}
        pooled_items = data.get("pooled_items")
        if isinstance(pooled_items, list):
            state.pooled_items = {item_id for item_id in pooled_items if isinstance(item_id, str)}
        unit_plays = data.get("unit_plays")
        if isinstance(unit_plays, dict):
            for key, plays in unit_plays.items():
                if not isinstance(key, str) or not isinstance(plays, dict):
                    continue
                kept = {
                    item_id: frozenset(uri for uri in uris if isinstance(uri, str))
                    for item_id, uris in plays.items()
                    if isinstance(item_id, str) and isinstance(uris, list)
                }
                if kept := {item_id: uris for item_id, uris in kept.items() if uris}:
                    state.unit_plays[key] = kept
        return state


class ProgressStore:
    """The progress of all users, kept in one JSON file."""

    def __init__(self, path: str) -> None:
        """
        Initialize the store.

        :param path: The progress file.
        """
        self._path = path
        self._entries: dict[str, dict[str, RoundState]] = {}

    def load(self) -> None:
        """Load the progress file; a missing or broken file gives an empty store."""
        self._entries = {}
        entries = read_json(self._path).get("entries")
        if not isinstance(entries, dict):
            return
        for rotation_id, users in entries.items():
            if not isinstance(users, dict):
                continue
            self._entries[str(rotation_id)] = {
                str(user_key): RoundState.from_dict(raw) for user_key, raw in users.items()
            }

    def get(self, rotation_id: str, user_key: str) -> RoundState:
        """
        Return the state of a user in a Rotation; an unknown one is a fresh state, not stored.

        The fresh state always has the same seed for the same Rotation and user, so a batch
        handed out before the first play keeps its order once the play is stored.

        :param rotation_id: The Rotation.
        :param user_key: The user, or the household key.
        """
        state = self._entries.get(rotation_id, {}).get(user_key)
        return state if state is not None else RoundState(seed=derived_seed(rotation_id, user_key))

    def mark_started(
        self,
        rotation_id: str,
        user_key: str,
        uri: str,
        aliases: Iterable[str] = (),
        item_id: str | None = None,
        unit: str | None = None,
    ) -> bool:
        """
        Mark a track as played; return True when this changed the state.

        :param rotation_id: The Rotation.
        :param user_key: The user, or the household key.
        :param uri: The track uri.
        :param aliases: Other uris the same track is known by.
        :param item_id: The queue item that plays it.
        :param unit: The key of the unit it plays for, or None when it is not known.
        """
        return self._stored(rotation_id, user_key).add_play({uri, *aliases}, item_id, unit)

    def mark_unit_played(self, rotation_id: str, user_key: str, unit_uri: str) -> bool:
        """
        Mark a unit (for example an album) as fully played; return True when this changed it.

        :param rotation_id: The Rotation.
        :param user_key: The user, or the household key.
        :param unit_uri: The uri of the unit.
        """
        state = self._stored(rotation_id, user_key)
        if unit_uri in state.played_units:
            return False
        state.played_units.add(unit_uri)
        return True

    def clear_played_units(self, rotation_id: str) -> bool:
        """
        Forget the fully played units of all users in a Rotation; the played tracks stay.

        :param rotation_id: The Rotation.
        """
        changed = False
        for state in self._entries.get(rotation_id, {}).values():
            changed = changed or bool(state.played_units)
            state.played_units = set()
        return changed

    def next_round(self, rotation_id: str, user_key: str) -> None:
        """
        Start the next round of a user, as next_batch does when a round is used up.

        :param rotation_id: The Rotation.
        :param user_key: The user, or the household key.
        """
        self._stored(rotation_id, user_key).advance()

    def reset(self, rotation_id: str, user_key: str) -> None:
        """
        Start the user over with a fresh round 1.

        :param rotation_id: The Rotation.
        :param user_key: The user, or the household key.
        """
        self._entries.setdefault(rotation_id, {})[user_key] = RoundState()

    def drop_rotation(self, rotation_id: str) -> None:
        """
        Forget the progress of all users in a Rotation.

        :param rotation_id: The Rotation.
        """
        self._entries.pop(rotation_id, None)

    async def save(self) -> None:
        """Write the progress file."""
        entries = {
            rotation_id: {user_key: state.to_dict() for user_key, state in users.items()}
            for rotation_id, users in self._entries.items()
        }
        await write_json(self._path, {"version": STORAGE_VERSION, "entries": entries})

    def _stored(self, rotation_id: str, user_key: str) -> RoundState:
        """Return the stored state of a user, storing the fresh state first when needed."""
        users = self._entries.setdefault(rotation_id, {})
        if user_key not in users:
            users[user_key] = self.get(rotation_id, user_key)
        return users[user_key]


async def next_batch(
    steps: list[Step],
    state: RoundState,
    ctx_factory: Callable[[int], RunContext],
    max_tracks: int = BATCH_MAX_TRACKS,
    keep: TrackFilter | None = None,
    units: UnitTracks | None = None,
) -> list[Track]:
    """
    Return the next unplayed tracks of the round, starting a new round when it is done.

    A round is only done when the steps gave playable tracks and all of them were played;
    when they give nothing at all, the state stays as it is.

    :param steps: The steps of the Rotation.
    :param state: The progress of the user; a new round changes it.
    :param ctx_factory: Builds a fresh run context for a seed.
    :param max_tracks: The maximum number of tracks to return.
    :param keep: Keeps the tracks that can play for the user; the others do not count.
    :param units: Receives the kept tracks of each unit the batch takes tracks from.
    """
    batch, saw_tracks, failed = await _walk(steps, state, ctx_factory, max_tracks, keep, units)
    if batch or not saw_tracks or failed:
        # a listing that failed may hide unplayed tracks, so the round is not done yet
        return batch
    state.advance()
    if units is not None:
        units.clear()
    batch, _saw_tracks, _failed = await _walk(steps, state, ctx_factory, max_tracks, keep, units)
    return batch


async def round_status(
    steps: list[Step],
    state: RoundState,
    ctx_factory: Callable[[int], RunContext],
    keep: TrackFilter | None = None,
) -> dict[str, Any]:
    """
    Return the round number and how many units are played, without provider calls.

    Units whose tracks would need a provider call do not count as played, and the counts
    are then not exact; they are also not exact beyond the preview cap. Units without a
    track that can play for the user are not counted.

    :param steps: The steps of the Rotation.
    :param state: The progress of the user.
    :param ctx_factory: Builds a fresh run context for a seed.
    :param keep: Keeps the tracks that can play for the user; the others do not count.
    """
    ctx = ctx_factory(state.seed)
    ctx.count_only_library = True
    ctx.skip_units = frozenset(state.played_units)
    total = 0
    played = 0
    capped = False
    progress = _Progress(state)
    async with aclosing(iter_units(steps, ctx)) as units:
        async for unit in units:
            if total >= PREVIEW_COUNT_CAP:
                capped = True
                break
            key = progress.key(str(unit.item.uri))
            if unit.skipped or key in state.played_units:
                total += 1
                played += 1
                continue
            tracks = await playable_tracks(unit, ctx)
            kept = await keep(tracks) if keep else tracks
            if tracks and not kept:
                continue
            total += 1
            if kept and not progress.unplayed(key, kept):
                played += 1
    exact = not capped and not ctx.skipped_provider and all(stats.exact for stats in ctx.stats)
    return {
        "round": state.round,
        "units_total": total,
        "units_played": played,
        "exact": exact,
    }


async def _walk(
    steps: list[Step],
    state: RoundState,
    ctx_factory: Callable[[int], RunContext],
    max_tracks: int,
    keep: TrackFilter | None,
    units: UnitTracks | None,
) -> tuple[list[Track], bool, bool]:
    """Return the unplayed tracks, whether there were playable tracks and whether a fetch failed."""
    ctx = ctx_factory(state.seed)
    ctx.skip_units = frozenset(state.played_units)
    batch: list[Track] = []
    saw_tracks = False
    progress = _Progress(state)
    async with aclosing(iter_units(steps, ctx)) as stream:
        async for unit in stream:
            key = progress.key(str(unit.item.uri))
            if unit.skipped or key in state.played_units:
                saw_tracks = True
                continue
            tracks = await playable_tracks(unit, ctx)
            if keep:
                tracks = await keep(tracks)
            unplayed = progress.unplayed(key, tracks)
            saw_tracks = saw_tracks or bool(tracks)
            if units is not None and unplayed:
                units[key] = HandedUnit(
                    [frozenset(track_uris(track)) for track in tracks],
                    complete=not unit.fetch_failed and not isinstance(unit.item, Track),
                )
            for track in unplayed:
                batch.append(track)
                if len(batch) >= max_tracks:
                    return batch, saw_tracks, ctx.fetch_failed
    return batch, saw_tracks, ctx.fetch_failed


class _Progress:
    """What already played in a round, taken up unit by unit in play order."""

    def __init__(self, state: RoundState) -> None:
        """
        Initialize the progress.

        :param state: The progress of the user.
        """
        self._state = state
        # the plays without a unit are taken up by the first units that hold them
        self._unknown = set(state.played)
        self._seen: dict[str, int] = {}

    def key(self, unit_uri: str) -> str:
        """Return the key of the next unit in play order."""
        return unit_key(unit_uri, self._seen)

    def unplayed(self, key: str, tracks: list[Track]) -> list[Track]:
        """Return the tracks of a unit that did not play as part of it, in their order."""
        plays = self._state.plays_of(key)
        result: list[Track] = []
        for track in tracks:
            uris = track_uris(track)
            if (play := next((play for play in plays if play & uris), None)) is not None:
                plays.remove(play)
            elif uris & self._unknown:
                self._unknown -= uris
            else:
                result.append(track)
        return result
