"""Lazy engine that runs the steps of a Rotation playlist over the library."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import AsyncGenerator, Callable, Iterator
from contextlib import AbstractContextManager, aclosing, nullcontext
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

import aiohttp
from music_assistant_models.enums import AlbumType, MediaType, SortDirection, SortField
from music_assistant_models.errors import (
    InsufficientPermissions,
    InvalidDataError,
    MediaNotFoundError,
    ProviderPermissionDenied,
    UnsupportedFeaturedException,
)
from music_assistant_models.media_items import Album, Artist, Track

from music_assistant.constants import MASS_LOGGER_NAME
from music_assistant.controllers.music.sorting import MEDIA_TYPE_SORT_FIELDS
from music_assistant.helpers.compare import compare_album
from music_assistant.helpers.throttle_retry import RequestPriority, request_priority
from music_assistant.models.music_provider import PROVIDER_FETCH_ERRORS

from .constants import BLOCKING_SORT_MAX_ITEMS
from .fields import FIELDS, RANDOM, matches, sort_items
from .library_values import load_values
from .models import (
    ExpandStep,
    FilterStep,
    FlattenStep,
    ItemKind,
    LimitStep,
    SortKey,
    SortStep,
    SourceStep,
    Step,
    StepError,
    StepsError,
    item_kinds,
)

if TYPE_CHECKING:
    from music_assistant_models.media_items import MediaItemType

    from music_assistant.controllers.music.media.albums import AlbumsController
    from music_assistant.controllers.music.media.artists import ArtistsController
    from music_assistant.controllers.music.media.tracks import TracksController
    from music_assistant.mass import MusicAssistant

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.rotation_playlist")

PAGE_SIZE = 500
# the item cannot be listed for good: it is left out like an unavailable track
GONE_ERRORS: tuple[type[Exception], ...] = (
    MediaNotFoundError,
    InvalidDataError,
    InsufficientPermissions,
    ProviderPermissionDenied,
    UnsupportedFeaturedException,
)
# the errors of a listing; the ones that are not gone may pass, so they hold the round
FETCH_ERRORS: tuple[type[Exception], ...] = (*PROVIDER_FETCH_ERRORS, *GONE_ERRORS)
_SQL_SORT_FIELDS = {
    "name": SortField.NAME,
    "title": SortField.NAME,
    "date_added": SortField.TIMESTAMP_ADDED,
    "play_count": SortField.PLAY_COUNT,
    "last_played": SortField.LAST_PLAYED,
}
_ALBUM_PLAY_FIELDS = ("play_count", "last_played")

type _Stream = AsyncGenerator[Node]


@dataclass
class Node:
    """One item in the result, with its children when it was expanded into a group."""

    item: MediaItemType
    children: list[Node] | None = None
    removed: list[Node] = field(default_factory=list)
    # a unit that was not expanded because it already played; later steps pass it on as is
    skipped: bool = False
    # some of its children or tracks could not be listed, so it may hold less than it should
    fetch_failed: bool = False
    # the field values the media item does not carry, loaded from the library by field name
    loaded: dict[str, Any] = field(default_factory=dict)


@dataclass
class StepStats:
    """The number of items a step passed on; not exact when the run stopped early."""

    step: int
    count: int
    exact: bool


@dataclass
class RunContext:
    """Settings and results of one run of the steps."""

    mass: MusicAssistant
    seed: int
    collect_removed: bool = False
    count_cap: int | None = None
    # skip provider calls; the counts of the steps that would need them become "at least"
    count_only_library: bool = False
    # the uris of top-level units that need not be expanded, as they already played
    skip_units: frozenset[str] = frozenset()
    stats: list[StepStats] = field(default_factory=list)
    # the provider's logger, so its log level setting applies
    logger: logging.Logger = field(default=LOGGER, repr=False)
    skipped_provider: bool = field(default=False, init=False)
    # a listing failed (for example a provider is down), so the result may be incomplete
    fetch_failed: bool = field(default=False, init=False)
    warned_no_duration: bool = field(default=False, init=False, repr=False)
    # the loaded fields the steps use, per item kind
    loads: dict[ItemKind, frozenset[str]] = field(default_factory=dict, init=False, repr=False)


async def iter_units(steps: list[Step], ctx: RunContext) -> AsyncGenerator[Node]:
    """
    Run validated steps and yield the top-level units in play order, pulling lazily.

    The counts in ``ctx.stats`` grow while the units are pulled; close the iterator
    (for example with ``contextlib.aclosing``) when stopping early.

    :param steps: The steps of the Rotation, starting with the source.
    :param ctx: The run settings; its stats are reset.
    :raises StepsError: When the steps do not start with a source, or a sort needs too many items.
    """
    ctx.stats[:] = [StepStats(number, 0, True) for number in range(1, len(steps) + 1)]
    source = steps[0] if steps else None
    if not isinstance(source, SourceStep):
        raise StepsError(
            [
                StepError(
                    1, "type", "the steps must start with a source step", "steps_without_source"
                )
            ]
        )
    kinds = item_kinds(steps)
    if ctx.skip_units and not _can_skip_units(steps):
        ctx.skip_units = frozenset()
    query, pushed, counted = _plan(steps, ctx)
    ctx.loads = _loads(steps, pushed)
    if counted:
        # the library query filters already, so the steps before the filter count the library
        total = await _controller(ctx, source.media_type).library_count()
        for index in counted:
            ctx.stats[index].count = total
    stream = _source(source.media_type, query, ctx, counted)
    if 0 not in counted:
        stream = _count(stream, ctx, 0, 0)
    for index, step in enumerate(steps[1:], start=1):
        kind, depth = kinds[index - 1]
        if index not in pushed:
            stream = _run_step(step, index + 1, stream, kind, depth, ctx)
        # a blocking sort counts all it read itself, also when a later limit stops early
        blocking = isinstance(step, SortStep) and depth == 0 and index not in pushed
        if not blocking and index not in counted:
            stream = _count(stream, ctx, index, kinds[index][1])
    async with aclosing(stream) as units:
        async for unit in units:
            yield unit


def unit_tracks(node: Node) -> list[Track]:
    """
    Return the tracks of a unit in play order; album and artist leaves are left out.

    :param node: The unit.
    """
    return [leaf.item for leaf in _leaves(node) if isinstance(leaf.item, Track)]


async def playable_tracks(node: Node, ctx: RunContext) -> list[Track]:
    """
    Return the tracks a unit plays, with album and artist leaves played complete.

    :param node: The unit.
    :param ctx: The run settings.
    """
    music = ctx.mass.music
    tracks: list[Track] = []
    for leaf in _leaves(node):
        item = leaf.item
        try:
            if isinstance(item, Track):
                tracks.append(item)
            elif ctx.count_only_library and item.provider != "library":
                ctx.skipped_provider = True
            elif isinstance(item, Album):
                tracks.extend(await _album_tracks(item, True, ctx))
            elif isinstance(item, Artist):
                with _priority_for(item.provider != "library"):
                    tracks.extend(
                        await music.artists.tracks(item.item_id, item.provider, limit=None)
                    )
        except FETCH_ERRORS as err:
            if _fetch_failed(ctx, err, "the tracks of %s", item.uri):
                node.fetch_failed = True
    return tracks


def _plan(steps: list[Step], ctx: RunContext) -> tuple[dict[str, Any], set[int], set[int]]:
    """Return the library query arguments, the sort steps it covers and the steps it hides."""
    kind = steps[0].media_type if isinstance(steps[0], SourceStep) else ItemKind.ALBUM
    query: dict[str, Any] = {}
    pushed: set[int] = set()
    counted: set[int] = set()
    seen_filter = False
    # Only the first filter goes into the query, and it still runs in memory as well. The steps
    # before it see the filtered list, so their count comes from the library count instead. A
    # single-key sort may move into the query over filters, as filtering does not change the order.
    for index, step in enumerate(steps[1:], start=1):
        if isinstance(step, FilterStep):
            if not seen_filter and _push_filter(step, kind, query, ctx):
                counted = {0, *pushed}
            seen_filter = True
        elif isinstance(step, SortStep) and not pushed and (sort := _sql_sort(step, kind, ctx)):
            query.update(sort)
            pushed.add(index)
        else:
            break
    return query, pushed, counted


def _loads(steps: list[Step], pushed: set[int]) -> dict[ItemKind, frozenset[str]]:
    """Return the loaded fields that the filters and the in-memory sorts use, per item kind."""
    kinds = item_kinds(steps)
    result: dict[ItemKind, set[str]] = {}
    for index, step in enumerate(steps[1:], start=1):
        if isinstance(step, FilterStep):
            names = [condition.field for condition in step.conditions]
        elif isinstance(step, SortStep) and index not in pushed:
            names = [key.field for key in step.keys]
        else:
            continue
        kind = kinds[index - 1][0]
        catalog = FIELDS[kind]
        result.setdefault(kind, set()).update(
            name for name in names if (definition := catalog.get(name)) and definition.loaded
        )
    return {kind: frozenset(names) for kind, names in result.items() if names}


def _push_filter(step: FilterStep, kind: ItemKind, query: dict[str, Any], ctx: RunContext) -> bool:
    """Add the conditions of a filter that the library query supports to the query."""
    before = len(query)
    if step.mode != "keep" or (step.match != "all" and len(step.conditions) != 1):
        return False
    for condition in step.conditions:
        values = condition.value if condition.op == "one_of" else [condition.value]
        if condition.op not in ("is", "one_of") or not isinstance(values, list):
            continue
        if condition.field == "favorite" and condition.value is True:
            query["favorite"] = True
        elif condition.field == "provider" and "provider" not in query:
            wanted = {str(value).casefold() for value in values}
            instances = [
                provider.instance_id
                for provider in ctx.mass.music.providers
                if {provider.instance_id.casefold(), provider.domain.casefold()} & wanted
            ]
            if instances:
                query["provider"] = instances
        elif (
            condition.field == "album_type"
            and kind == ItemKind.ALBUM
            and "album_types" not in query
        ):
            try:
                query["album_types"] = [AlbumType(value) for value in values]
            except ValueError:
                continue
    return len(query) > before


def _sql_sort(step: SortStep, kind: ItemKind, ctx: RunContext) -> dict[str, Any] | None:
    """Return the library query arguments for a sort, or None when the query cannot do it."""
    if len(step.keys) != 1:
        return None
    key = step.keys[0]
    if key.field == RANDOM:
        return {"sort_field": SortField.RANDOM, "random_seed": ctx.seed}
    sort_field = _SQL_SORT_FIELDS.get(key.field)
    if sort_field is None or sort_field not in MEDIA_TYPE_SORT_FIELDS[MediaType(kind.value)]:
        return None
    if kind == ItemKind.ALBUM and key.field in _ALBUM_PLAY_FIELDS:
        # the plays of an album count its tracks, which the album columns do not
        return None
    direction = SortDirection.DESC if key.descending else SortDirection.ASC
    return {"sort_field": sort_field, "sort_direction": direction}


def _can_skip_units(steps: list[Step]) -> bool:
    """Return whether played top-level units can pass the steps without being expanded."""
    # a flatten would turn them into tracks, and a limit on the expanded units could pick
    # other units when some are not expanded
    kinds = item_kinds(steps)
    expanded = False
    for index, step in enumerate(steps):
        if isinstance(step, ExpandStep):
            expanded = True
        elif (expanded and isinstance(step, FlattenStep)) or (
            expanded and isinstance(step, LimitStep) and kinds[index - 1][1] == 0
        ):
            return False
    return True


def _controller(
    ctx: RunContext, kind: ItemKind
) -> AlbumsController | ArtistsController | TracksController:
    """Return the library controller of an item kind."""
    music = ctx.mass.music
    if kind == ItemKind.ARTIST:
        return music.artists
    return music.albums if kind == ItemKind.ALBUM else music.tracks


async def _source(
    kind: ItemKind, query: dict[str, Any], ctx: RunContext, counted: set[int]
) -> _Stream:
    """Yield the library items page by page, up to the count cap."""
    controller = _controller(ctx, kind)
    offset = read = 0
    while True:
        page = await controller.library_items(
            limit=PAGE_SIZE, offset=offset, summary=False, **query
        )
        nodes = [Node(item) for item in page]
        await _load(nodes, kind, ctx)
        for node in nodes:
            if ctx.count_cap is not None and read >= ctx.count_cap:
                for index, stats in enumerate(ctx.stats):
                    stats.exact = index in counted
                return
            read += 1
            yield node
        if len(page) < PAGE_SIZE:
            return
        offset += len(page)


async def _count(stream: _Stream, ctx: RunContext, index: int, depth: int) -> _Stream:
    """Pass the units on, counting the items at the step's depth."""
    exhausted = False
    try:
        async with aclosing(stream) as units:
            async for unit in units:
                ctx.stats[index].count += sum(1 for _ in _level(unit, depth))
                yield unit
        exhausted = True
    finally:
        if not exhausted:
            ctx.stats[index].exact = False


def _run_step(
    step: Step, number: int, stream: _Stream, kind: ItemKind, depth: int, ctx: RunContext
) -> _Stream:
    """Return the stream after one step that works on items of a kind at a depth."""
    if isinstance(step, FilterStep):
        if depth == 0:
            return _filter_top(stream, step, kind)
        return _in_groups(stream, depth, lambda group: _filter_group(group, step, kind, ctx))
    if isinstance(step, SortStep):
        if depth == 0:
            return _sort_top(stream, step.keys, number, kind, ctx)
        return _in_groups(stream, depth, lambda group: _sort_group(group, step.keys, kind, ctx))
    if isinstance(step, LimitStep):
        if depth == 0:
            return _limit_top(stream, step, ctx)
        return _in_groups(stream, depth, lambda group: _limit_group(group, step, ctx))
    if isinstance(step, ExpandStep):
        return _expand(stream, step, number, kind, depth, ctx)
    if isinstance(step, FlattenStep):
        return _flatten(stream)
    return stream


async def _filter_top(stream: _Stream, step: FilterStep, kind: ItemKind) -> _Stream:
    async with aclosing(stream) as units:
        async for unit in units:
            if _keeps(unit, kind, step):
                yield unit


async def _sort_top(
    stream: _Stream, keys: list[SortKey], number: int, kind: ItemKind, ctx: RunContext
) -> _Stream:
    """Read the whole (bounded) input, then yield it in order."""
    units: list[Node] = []
    async with aclosing(stream) as source:
        async for unit in source:
            units.append(unit)
            if len(units) > BLOCKING_SORT_MAX_ITEMS:
                raise StepsError(
                    [
                        StepError(
                            number,
                            None,
                            f"this order needs all items and more than {BLOCKING_SORT_MAX_ITEMS} "
                            f"reach it; add an 'Only the first' step before step {number}",
                            "sort_too_many_items",
                            [BLOCKING_SORT_MAX_ITEMS, number],
                        )
                    ]
                )
    stats = ctx.stats[number - 1]
    stats.count = len(units)
    stats.exact = ctx.stats[number - 2].exact
    for unit in _sorted(units, keys, kind, ctx.seed):
        yield unit


async def _limit_top(stream: _Stream, step: LimitStep, ctx: RunContext) -> _Stream:
    """Yield units until the limit is reached, then stop pulling."""
    taken = 0
    async with aclosing(stream) as units:
        async for unit in units:
            yield unit
            taken += 1 if step.count is not None else _duration(unit, ctx)
            if taken >= _limit_amount(step):
                return


async def _in_groups(stream: _Stream, depth: int, apply: Callable[[Node], None]) -> _Stream:
    """Apply a change to every innermost group; units left empty are dropped."""
    async with aclosing(stream) as units:
        async for unit in units:
            if unit.skipped:
                yield unit
                continue
            for group in list(_level(unit, depth - 1)):
                apply(group)
            if _prune(unit):
                yield unit


async def _expand(
    stream: _Stream, step: ExpandStep, number: int, kind: ItemKind, depth: int, ctx: RunContext
) -> _Stream:
    """Turn every item at the depth into a group of its children."""
    async with aclosing(stream) as units:
        async for unit in units:
            if unit.skipped or (depth == 0 and str(unit.item.uri) in ctx.skip_units):
                unit.skipped = True
                yield unit
                continue
            added: list[Node] = []
            for leaf in list(_level(unit, depth)):
                children, complete = await _children(leaf.item, kind, step, number, ctx)
                leaf.children = [Node(child) for child in children]
                added.extend(leaf.children)
                unit.fetch_failed = unit.fetch_failed or not complete
            await _load(added, step.to, ctx)
            if _prune(unit):
                yield unit


async def _flatten(stream: _Stream) -> _Stream:
    async with aclosing(stream) as units:
        async for unit in units:
            for leaf in _leaves(unit):
                yield Node(leaf.item, loaded=leaf.loaded)


def _filter_group(group: Node, step: FilterStep, kind: ItemKind, ctx: RunContext) -> None:
    kept: list[Node] = []
    for child in group.children or ():
        if _keeps(child, kind, step):
            kept.append(child)
        elif ctx.collect_removed:
            group.removed.append(child)
    group.children = kept


def _sort_group(group: Node, keys: list[SortKey], kind: ItemKind, ctx: RunContext) -> None:
    group.children = _sorted(group.children or [], keys, kind, ctx.seed)


def _limit_group(group: Node, step: LimitStep, ctx: RunContext) -> None:
    kept: list[Node] = []
    taken = 0
    for child in group.children or ():
        if taken >= _limit_amount(step):
            break
        kept.append(child)
        taken += 1 if step.count is not None else _duration(child, ctx)
    group.children = kept


async def _children(
    item: MediaItemType, kind: ItemKind, step: ExpandStep, number: int, ctx: RunContext
) -> tuple[list[MediaItemType], bool]:
    """Return the children of an item and whether all of them could be listed."""
    music = ctx.mass.music
    if ctx.count_only_library and (item.provider != "library" or step.scope == "providers"):
        ctx.skipped_provider = True
        for stats in ctx.stats[number - 1 :]:
            stats.exact = False
        if item.provider != "library":
            return [], True
        step = replace(step, scope="library")
    try:
        if kind == ItemKind.ALBUM and isinstance(item, Album):
            return list(await _album_tracks(item, step.scope == "library", ctx)), True
        if step.to == ItemKind.TRACK:
            with _priority_for(item.provider != "library"):
                tracks = await music.artists.tracks(item.item_id, item.provider, limit=None)
            return list(tracks), True
        albums, complete = await _artist_albums(item, step.scope, ctx)
        return list(albums), complete
    except FETCH_ERRORS as err:
        return [], not _fetch_failed(ctx, err, f"the {step.to.value}s of %s", item.uri)


async def _artist_albums(
    artist: MediaItemType, scope: str, ctx: RunContext
) -> tuple[list[Album], bool]:
    """Return the albums of an artist and whether all of its providers could list them."""
    music = ctx.mass.music
    albums = list(await music.artists.albums(artist.item_id, artist.provider, limit=None))
    if scope != "providers":
        return albums, True
    # the scope "providers" adds the albums on the artist's providers; an album found again
    # there counts once, matched like the albums of an artist that is played
    complete = True
    known = {
        (mapping.provider_instance, mapping.item_id)
        for a in albums
        for mapping in a.provider_mappings
    }
    unique_providers = set(music.get_unique_providers())
    for mapping in sorted(
        artist.provider_mappings, key=lambda mapping: (mapping.provider_instance, mapping.item_id)
    ):
        if mapping.provider_instance not in unique_providers:
            continue
        try:
            with request_priority(RequestPriority.LOW):
                listed = await music.artists.albums(
                    mapping.item_id, mapping.provider_instance, limit=None
                )
        except FETCH_ERRORS as err:
            if _fetch_failed(
                ctx, err, "the albums of %s on %s", artist.name, mapping.provider_instance
            ):
                complete = False
            continue
        for album in listed:
            if any(
                (album_mapping.provider_instance, album_mapping.item_id) in known
                for album_mapping in album.provider_mappings
            ) or any(compare_album(album, other) for other in albums):
                continue
            albums.append(album)
    return albums, complete


async def _album_tracks(album: Album, library_only: bool, ctx: RunContext) -> list[Track]:
    """Return the tracks of an album; a provider album always lists all its tracks."""
    library_only = library_only and album.provider == "library"
    with _priority_for(not library_only):
        return list(
            await ctx.mass.music.albums.tracks(
                album.item_id, album.provider, in_library_only=library_only, limit=None
            )
        )


async def _load(nodes: list[Node], kind: ItemKind, ctx: RunContext) -> None:
    """Load the fields the steps use for the library items of nodes, in one batch."""
    names = ctx.loads.get(kind)
    if not names:
        return
    by_id: dict[int, list[Node]] = {}
    for node in nodes:
        library_id = str(node.item.item_id)
        if node.item.provider == "library" and library_id.isdigit():
            by_id.setdefault(int(library_id), []).append(node)
    if not by_id:
        return
    values = await load_values(ctx.mass, kind, list(by_id), names)
    for item_id, found in values.items():
        for node in by_id.get(item_id, []):
            node.loaded.update(found)


def _priority_for(low: bool) -> AbstractContextManager[None]:
    """Return a context that lowers the request priority when asked to."""
    return request_priority(RequestPriority.LOW) if low else nullcontext()


def _keeps(node: Node, kind: ItemKind, step: FilterStep) -> bool:
    """Return whether a filter step keeps the item of a node."""
    if not step.conditions:
        return True
    hits = (matches(node.item, kind, condition, node.loaded) for condition in step.conditions)
    hit = all(hits) if step.match == "all" else any(hits)
    return hit if step.mode == "keep" else not hit


def _sorted(nodes: list[Node], keys: list[SortKey], kind: ItemKind, seed: int) -> list[Node]:
    """Return nodes ordered by sort keys; a random key orders by the seed."""
    fields = [key.field for key in keys]
    if RANDOM in fields:
        # a hash of the seed and the uri; the keys after it cannot change the order
        keys = keys[: fields.index(RANDOM)]
        nodes = sorted(nodes, key=lambda node: _random_key(seed, node.item))
    by_item: dict[int, list[Node]] = {}
    for node in nodes:
        by_item.setdefault(id(node.item), []).append(node)
    ordered = sort_items(
        [node.item for node in nodes],
        kind,
        keys,
        lambda item: by_item[id(item)][0].loaded,
    )
    return [by_item[id(item)].pop(0) for item in ordered]


def _random_key(seed: int, item: MediaItemType) -> str:
    return hashlib.sha1(f"{seed}:{item.uri}".encode(), usedforsecurity=False).hexdigest()


def _limit_amount(step: LimitStep) -> int:
    """Return the limit as a count, or in seconds for a duration limit."""
    return step.count if step.count is not None else (step.minutes or 0) * 60


def _duration(node: Node, ctx: RunContext) -> int:
    """Return the duration of a node in seconds; items without one count as 0."""
    if node.children is not None:
        return sum(_duration(child, ctx) for child in node.children)
    duration = getattr(node.item, "duration", 0) or 0
    if not duration and not isinstance(node.item, Track) and not ctx.warned_no_duration:
        ctx.warned_no_duration = True
        ctx.logger.info(
            "A duration limit counts %s items as 0 minutes; expand them to tracks first",
            node.item.media_type.value,
        )
    return int(duration)


def _level(node: Node, depth: int) -> Iterator[Node]:
    """Yield the nodes at a depth below a node (the node itself at depth 0)."""
    if depth == 0:
        yield node
        return
    for child in node.children or ():
        yield from _level(child, depth - 1)


def _leaves(node: Node) -> Iterator[Node]:
    if node.children is None:
        yield node
        return
    for child in node.children:
        yield from _leaves(child)


def _prune(node: Node) -> bool:
    """Drop empty groups below a node and return whether the node still holds items."""
    if node.children is None:
        return True
    node.children = [child for child in node.children if _prune(child)]
    return bool(node.children)


def _fetch_failed(ctx: RunContext, err: Exception, what: str, *args: object) -> bool:
    """Log a failed listing; return True when it may pass, which marks the run as failed."""
    if isinstance(err, GONE_ERRORS):
        ctx.logger.info("Leaving out %s: %s", what % args, _error_summary(err))
        return False
    ctx.logger.warning("Unable to list %s: %s", what % args, _error_summary(err))
    ctx.fetch_failed = True
    return True


def _error_summary(err: Exception) -> str:
    """Return the type and the HTTP status of an error; its text may name a url with a token."""
    if isinstance(err, aiohttp.ClientResponseError):
        return f"{type(err).__name__} (status {err.status})"
    return type(err).__name__
