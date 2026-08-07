"""Tests for the filesystem provider's self-validating podcast episode list cache."""

from __future__ import annotations

import asyncio
import logging
import os
import pathlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.errors import InvalidDataError, MediaNotFoundError
from music_assistant_models.media_items import Podcast, PodcastEpisode

from music_assistant.helpers.tags import AudioTags
from music_assistant.mass import MusicAssistant
from music_assistant.providers.filesystem_local import LocalFileSystemProvider
from music_assistant.providers.filesystem_local.constants import (
    CACHE_CATEGORY_PODCAST_EPISODES,
    PARTIAL_LISTING_CACHE_EXPIRATION,
)

PODCAST_FOLDER = "My Podcast"
INSTANCE_ID = "filesystem_local--test"
PARSE_TAGS_TARGET = "music_assistant.providers.filesystem_local.base.async_parse_tags"


def _audio_tags(path: str, chapters: bool = False) -> AudioTags:
    """
    Build AudioTags as ffprobe would report them for one generated episode file.

    :param path: Absolute path of the file being parsed.
    :param chapters: Whether to report an embedded chapter.
    """
    filename = pathlib.Path(path).name
    # episode-03.mp3 -> track 3
    track = int(filename.split("-")[1].split(".")[0])
    raw: dict[str, object] = {}
    if chapters:
        raw["chapters"] = [
            {"id": 1, "start_time": "0.0", "end_time": "21.0", "tags": {"title": "Intro"}},
            {"id": 2, "start_time": "21.0", "end_time": "42.0", "tags": {"title": "Outro"}},
        ]
    return AudioTags(
        raw=raw,
        sample_rate=44100,
        channels=2,
        bits_per_sample=16,
        format="mp3",
        bit_rate=128,
        duration=42.0,
        tags={
            "album": PODCAST_FOLDER,
            "title": f"Episode {track}",
            "track": str(track),
            "publisher": "ACME Radio",
            "comment": f"description of episode {track}",
        },
        has_cover_image=False,
        filename=filename,
    )


def _parse_tags_spy(chapters: bool = False, slow_first: bool = False) -> AsyncMock:
    """
    Return an AsyncMock standing in for async_parse_tags, counting the files it parses.

    :param chapters: Whether the reported tags include embedded chapters.
    :param slow_first: Make the first episode finish last, so that a listing collected in
        task completion order comes out in a different order than the directory listing.
    """

    async def _parse(path: str, _size: int | None = None) -> AudioTags:
        if slow_first and path.endswith("episode-01.mp3"):
            await asyncio.sleep(0.05)
        return _audio_tags(path, chapters)

    return AsyncMock(side_effect=_parse)


def _write_episodes(folder: pathlib.Path, count: int) -> None:
    """Create `count` dummy episode files; their tags are supplied by the parse spy."""
    folder.mkdir(parents=True, exist_ok=True)
    for index in range(1, count + 1):
        (folder / f"episode-{index:02d}.mp3").write_bytes(b"dummy audio")


@pytest.fixture(name="provider")
async def provider_fixture(
    mass_minimal: MusicAssistant, tmp_path: pathlib.Path
) -> LocalFileSystemProvider:
    """Create a podcasts filesystem provider backed by a real cache database."""
    # the mass_minimal fixture constructs the cache controller but never sets up its database
    await mass_minimal.cache.setup(await mass_minimal.config.get_core_config("cache"))
    base_path = tmp_path / "media"
    base_path.mkdir()
    _write_episodes(base_path / PODCAST_FOLDER, 3)

    # __new__ skips __init__, which would resolve the log level and content type through
    # config plumbing a bare test provider cannot satisfy
    provider = LocalFileSystemProvider.__new__(LocalFileSystemProvider)
    provider.mass = mass_minimal
    provider.config = MagicMock()
    provider.config.instance_id = INSTANCE_ID
    provider.manifest = MagicMock()
    provider.manifest.domain = "filesystem_local"
    provider.cache = mass_minimal.cache
    provider.logger = logging.getLogger("test.filesystem_local")
    provider.base_path = str(base_path)
    provider.media_content_type = "podcasts"
    provider.write_access = False
    provider.sync_running = False
    return provider


async def test_second_listing_is_served_from_cache(provider: LocalFileSystemProvider) -> None:
    """A second listing of an unchanged folder parses no files at all."""
    with patch(PARSE_TAGS_TARGET, new=_parse_tags_spy()) as parse_tags:
        first = [x async for x in provider.get_podcast_episodes(PODCAST_FOLDER)]
    assert parse_tags.await_count == 3

    with patch(PARSE_TAGS_TARGET, new=_parse_tags_spy()) as parse_tags:
        second = [x async for x in provider.get_podcast_episodes(PODCAST_FOLDER)]
    parse_tags.assert_not_awaited()

    assert [x.to_dict() for x in second] == [x.to_dict() for x in first]


async def test_listing_keeps_scandir_order(provider: LocalFileSystemProvider) -> None:
    """Episodes are yielded in (natural sorted) directory order, cold and cached alike."""
    expected = [f"{PODCAST_FOLDER}/episode-{index:02d}.mp3" for index in (1, 2, 3)]
    # the first episode parses slowest, so collecting in task completion order would
    # produce [02, 03, 01] here - the order this test exists to rule out
    with patch(PARSE_TAGS_TARGET, new=_parse_tags_spy(slow_first=True)):
        cold = [x async for x in provider.get_podcast_episodes(PODCAST_FOLDER)]
        cached = [x async for x in provider.get_podcast_episodes(PODCAST_FOLDER)]

    assert [x.item_id for x in cold] == expected
    assert [x.item_id for x in cached] == expected


async def test_parsing_stays_within_the_concurrency_limit(
    provider: LocalFileSystemProvider, tmp_path: pathlib.Path
) -> None:
    """
    A cold listing never runs more parse tasks at once than _SYNC_CONCURRENCY allows.

    Parsing shells out to ffprobe through the shared thread executor, so an unbounded fan-out
    over a large podcast starves every other thread pool user for the duration of the listing.
    TaskManager.create_task does not honour the limit it was constructed with - only
    create_task_with_limit acquires the semaphore - which makes this a one-word regression.
    """
    episode_count = provider._SYNC_CONCURRENCY * 2 + 8
    _write_episodes(tmp_path / "media" / "Big Podcast", episode_count)
    in_flight = 0
    peak = 0

    async def _parse(path: str, _size: int | None = None) -> AudioTags:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        # hold the slot open, so tasks started together overlap here rather than
        # running to completion one by one and never registering as concurrent
        await asyncio.sleep(0.01)
        in_flight -= 1
        return _audio_tags(path)

    with patch(PARSE_TAGS_TARGET, new=AsyncMock(side_effect=_parse)) as parse_tags:
        result = [x async for x in provider.get_podcast_episodes("Big Podcast")]

    assert parse_tags.await_count == episode_count
    assert len(result) == episode_count
    assert peak <= provider._SYNC_CONCURRENCY
    # without this the assertion above would also hold for a fully serialized listing,
    # which is not what the limit is for
    assert peak > 1


async def test_added_file_invalidates_cache(
    provider: LocalFileSystemProvider, tmp_path: pathlib.Path
) -> None:
    """A new episode file is picked up on the next listing, with no sync in between."""
    with patch(PARSE_TAGS_TARGET, new=_parse_tags_spy()):
        [x async for x in provider.get_podcast_episodes(PODCAST_FOLDER)]

    (tmp_path / "media" / PODCAST_FOLDER / "episode-04.mp3").write_bytes(b"dummy audio")

    with patch(PARSE_TAGS_TARGET, new=_parse_tags_spy()) as parse_tags:
        result = [x async for x in provider.get_podcast_episodes(PODCAST_FOLDER)]

    assert parse_tags.await_count == 4
    assert f"{PODCAST_FOLDER}/episode-04.mp3" in [x.item_id for x in result]


async def test_added_cover_art_reaches_the_listing(
    provider: LocalFileSystemProvider, tmp_path: pathlib.Path
) -> None:
    """
    Cover art added to the folder shows up on the next listing.

    Local images are read while parsing and embedded in every cached episode (as the parent
    podcast's image, and as the episode thumbnail for files without embedded art), so art
    that leaves the cache valid would stay invisible for the life of the entry. Note this
    covers *adding* a file rather than overwriting one: the parsed image is a bare path with
    no version suffix, so replacing cover.jpg in place is served fresh from disk regardless.
    """
    with patch(PARSE_TAGS_TARGET, new=_parse_tags_spy()):
        first = [x async for x in provider.get_podcast_episodes(PODCAST_FOLDER)]
    assert isinstance(first[0].podcast, Podcast)
    assert first[0].podcast.image is None

    (tmp_path / "media" / PODCAST_FOLDER / "cover.jpg").write_bytes(b"art")

    with patch(PARSE_TAGS_TARGET, new=_parse_tags_spy()) as parse_tags:
        second = [x async for x in provider.get_podcast_episodes(PODCAST_FOLDER)]

    assert parse_tags.await_count == 3, "the folder signature should have invalidated"
    assert isinstance(second[0].podcast, Podcast)
    # not just re-parsed: the new art has to survive the folder-images cache too
    assert second[0].podcast.image is not None
    assert second[0].podcast.image.path == f"{PODCAST_FOLDER}/cover.jpg"


async def test_edited_metadata_json_is_not_refrozen_from_the_inner_cache(
    provider: LocalFileSystemProvider, tmp_path: pathlib.Path
) -> None:
    """
    An edited metadata.json reaches the re-parsed listing instead of a stale copy of itself.

    The folder's metadata.json and artwork are read through their own per-folder caches, and
    neither is checksum validated. A re-parse triggered by the folder signature must not be
    filled from those, or the very edit that invalidated the listing is frozen back into it -
    permanently, because the new entry's signature then matches the folder.
    """
    metadata_file = tmp_path / "media" / PODCAST_FOLDER / "metadata.json"
    metadata_file.write_text('{"title": "Old Name"}')
    with patch(PARSE_TAGS_TARGET, new=_parse_tags_spy()):
        first = [x async for x in provider.get_podcast_episodes(PODCAST_FOLDER)]
    assert isinstance(first[0].podcast, Podcast)
    assert first[0].podcast.name == "Old Name"

    # a different length as well as different content: the signature's mtime is whole
    # seconds, so within one second only the size distinguishes the two files
    metadata_file.write_text('{"title": "A Brand New Name"}')

    with patch(PARSE_TAGS_TARGET, new=_parse_tags_spy()) as parse_tags:
        second = [x async for x in provider.get_podcast_episodes(PODCAST_FOLDER)]

    assert parse_tags.await_count == 3, "the folder signature should have invalidated"
    assert isinstance(second[0].podcast, Podcast)
    assert second[0].podcast.name == "A Brand New Name"


async def test_cold_parse_reads_the_folder_artwork_once(
    provider: LocalFileSystemProvider, tmp_path: pathlib.Path
) -> None:
    """
    Invalidating the artwork cache does not make every parse task go and rebuild it.

    Each episode embeds the folder's artwork, read through a per-folder cache that this
    listing has just had to drop. Left empty, all _SYNC_CONCURRENCY parse tasks miss it at
    once and each repeats the same scandir, which on the high latency transports that lower
    that limit is the expensive part.
    """
    (tmp_path / "media" / PODCAST_FOLDER / "cover.jpg").write_bytes(b"cover")
    scandir_calls: list[str] = []
    original_scandir = provider._scandir

    async def _spy_scandir(path: str) -> Any:
        scandir_calls.append(path)
        return await original_scandir(path)

    with (
        patch.object(provider, "_scandir", _spy_scandir),
        patch(PARSE_TAGS_TARGET, new=_parse_tags_spy()) as parse_tags,
    ):
        episodes = [x async for x in provider.get_podcast_episodes(PODCAST_FOLDER)]

    assert parse_tags.await_count == 3, "this has to be the cold path to prove anything"
    assert len(episodes) == 3
    # the listing's own scandir, plus one to fill the artwork cache for all three episodes
    assert len(scandir_calls) == 2, scandir_calls


async def test_case_variant_metadata_json_is_covered_by_the_signature(
    provider: LocalFileSystemProvider, tmp_path: pathlib.Path
) -> None:
    """
    A folder holding `Metadata.json` still invalidates its listing when that file is edited.

    _get_podcast_metadata locates the file through exists(), which resolves
    case-insensitively on APFS, SMB and NTFS - so the parse embeds a `Metadata.json` that a
    case-sensitive signature check leaves uncovered, freezing the listing for the lifetime of
    the entry. This asserts on invalidation rather than on the embedded name, so it holds on
    case-sensitive filesystems too, where the parse never reads the file in the first place.
    """
    metadata_file = tmp_path / "media" / PODCAST_FOLDER / "Metadata.json"
    metadata_file.write_text('{"title": "Old Name"}')
    with patch(PARSE_TAGS_TARGET, new=_parse_tags_spy()):
        [x async for x in provider.get_podcast_episodes(PODCAST_FOLDER)]

    # a different length as well as different content: the signature's mtime is whole
    # seconds, so within one second only the size distinguishes the two files
    metadata_file.write_text('{"title": "A Brand New Name"}')

    with patch(PARSE_TAGS_TARGET, new=_parse_tags_spy()) as parse_tags:
        [x async for x in provider.get_podcast_episodes(PODCAST_FOLDER)]

    assert parse_tags.await_count == 3, "the folder signature should have invalidated"


async def test_removed_file_invalidates_cache(
    provider: LocalFileSystemProvider, tmp_path: pathlib.Path
) -> None:
    """A deleted episode file is dropped on the next listing, with no sync in between."""
    with patch(PARSE_TAGS_TARGET, new=_parse_tags_spy()):
        [x async for x in provider.get_podcast_episodes(PODCAST_FOLDER)]

    (tmp_path / "media" / PODCAST_FOLDER / "episode-02.mp3").unlink()

    with patch(PARSE_TAGS_TARGET, new=_parse_tags_spy()) as parse_tags:
        result = [x async for x in provider.get_podcast_episodes(PODCAST_FOLDER)]

    assert parse_tags.await_count == 2
    assert [x.item_id for x in result] == [
        f"{PODCAST_FOLDER}/episode-01.mp3",
        f"{PODCAST_FOLDER}/episode-03.mp3",
    ]


async def test_touched_file_invalidates_cache(
    provider: LocalFileSystemProvider, tmp_path: pathlib.Path
) -> None:
    """A file whose modification time changed is re-parsed on the next listing."""
    with patch(PARSE_TAGS_TARGET, new=_parse_tags_spy()):
        [x async for x in provider.get_podcast_episodes(PODCAST_FOLDER)]

    episode_file = tmp_path / "media" / PODCAST_FOLDER / "episode-02.mp3"
    # the signature uses whole-second mtimes, so move it well clear of the original
    stat = episode_file.stat()
    os.utime(episode_file, (stat.st_atime, stat.st_mtime + 120))

    with patch(PARSE_TAGS_TARGET, new=_parse_tags_spy()) as parse_tags:
        [x async for x in provider.get_podcast_episodes(PODCAST_FOLDER)]

    assert parse_tags.await_count == 3


async def test_resized_file_invalidates_cache(
    provider: LocalFileSystemProvider, tmp_path: pathlib.Path
) -> None:
    """A file whose size changed is re-parsed even if the mtime is restored."""
    with patch(PARSE_TAGS_TARGET, new=_parse_tags_spy()):
        [x async for x in provider.get_podcast_episodes(PODCAST_FOLDER)]

    episode_file = tmp_path / "media" / PODCAST_FOLDER / "episode-02.mp3"
    stat = episode_file.stat()
    episode_file.write_bytes(b"dummy audio, but longer")
    os.utime(episode_file, (stat.st_atime, stat.st_mtime))

    with patch(PARSE_TAGS_TARGET, new=_parse_tags_spy()) as parse_tags:
        [x async for x in provider.get_podcast_episodes(PODCAST_FOLDER)]

    assert parse_tags.await_count == 3


async def test_empty_folder_yields_nothing_and_is_cached(
    provider: LocalFileSystemProvider,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty podcast folder lists nothing, is a cache hit next time, and stays not-found."""
    (tmp_path / "media" / "Empty Podcast").mkdir()
    writes = 0
    original_set = provider.mass.cache.set

    async def _counting_set(*args: Any, **kwargs: Any) -> None:
        nonlocal writes
        if kwargs.get("category") == CACHE_CATEGORY_PODCAST_EPISODES:
            writes += 1
        await original_set(*args, **kwargs)

    monkeypatch.setattr(provider.mass.cache, "set", _counting_set)

    with patch(PARSE_TAGS_TARGET, new=_parse_tags_spy()) as parse_tags:
        assert [x async for x in provider.get_podcast_episodes("Empty Podcast")] == []
        assert [x async for x in provider.get_podcast_episodes("Empty Podcast")] == []
    parse_tags.assert_not_awaited()
    # an empty listing is still a real cache entry, so a hit has to be told from a miss by
    # presence and not by truthiness: only the first call may write. Counting parses cannot
    # see this - an empty folder has nothing to parse either way
    assert writes == 1

    with pytest.raises(MediaNotFoundError):
        await provider.get_podcast("Empty Podcast")


async def test_missing_folder_raises(provider: LocalFileSystemProvider) -> None:
    """A podcast folder that does not exist fails rather than caching an empty listing."""
    with pytest.raises(FileNotFoundError):
        [x async for x in provider.get_podcast_episodes("No Such Podcast")]


async def test_refresh_bypasses_cache(
    provider: LocalFileSystemProvider, mass_minimal: MusicAssistant
) -> None:
    """A forced refresh (music/refresh_item) re-reads the folder instead of using the cache."""
    with patch(PARSE_TAGS_TARGET, new=_parse_tags_spy()):
        [x async for x in provider.get_podcast_episodes(PODCAST_FOLDER)]

    with patch(PARSE_TAGS_TARGET, new=_parse_tags_spy()) as parse_tags:
        async with mass_minimal.cache.handle_refresh(True):
            [x async for x in provider.get_podcast_episodes(PODCAST_FOLDER)]

    assert parse_tags.await_count == 3


async def test_partial_listing_is_cached_only_briefly(
    provider: LocalFileSystemProvider, tmp_path: pathlib.Path
) -> None:
    """
    One unreadable file keeps the rest of the listing, cached under a short expiration.

    Storing nothing at all would mean a permanently corrupt file re-parses every other file
    in the folder on every single request, for as long as it sits there - the cost this cache
    exists to remove, silently withheld from the one user who most needs it. The entry is kept
    instead, short lived because the dropped episode cannot reappear before it expires.
    """
    expirations: list[int | None] = []
    original_set = provider.mass.cache.set

    async def _spy_set(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("category") == CACHE_CATEGORY_PODCAST_EPISODES:
            expirations.append(kwargs.get("expiration"))
        return await original_set(*args, **kwargs)

    def _side_effect(path: str, _size: int | None = None) -> AudioTags:
        if path.endswith("episode-02.mp3"):
            raise InvalidDataError("Unable to parse file")
        return _audio_tags(path)

    with (
        patch.object(provider.mass.cache, "set", _spy_set),
        patch(PARSE_TAGS_TARGET, new=AsyncMock(side_effect=_side_effect)) as parse_tags,
    ):
        partial = [x async for x in provider.get_podcast_episodes(PODCAST_FOLDER)]
    assert parse_tags.await_count == 3
    assert [x.item_id for x in partial] == [
        f"{PODCAST_FOLDER}/episode-01.mp3",
        f"{PODCAST_FOLDER}/episode-03.mp3",
    ]
    # not the year a complete listing gets, or the missing episode would be hidden for it
    assert expirations == [PARTIAL_LISTING_CACHE_EXPIRATION]

    # the readable files are served from the cache rather than parsed again
    with patch(PARSE_TAGS_TARGET, new=_parse_tags_spy()) as parse_tags:
        cached = [x async for x in provider.get_podcast_episodes(PODCAST_FOLDER)]
    parse_tags.assert_not_awaited()
    assert len(cached) == 2

    # the folder signature still recovers the listing ahead of that expiration: a file that
    # becomes readable by being rewritten moves the signature and re-parses immediately
    (tmp_path / "media" / PODCAST_FOLDER / "episode-02.mp3").write_bytes(b"dummy audio fixed")

    with patch(PARSE_TAGS_TARGET, new=_parse_tags_spy()) as parse_tags:
        recovered = [x async for x in provider.get_podcast_episodes(PODCAST_FOLDER)]
    assert parse_tags.await_count == 3
    assert len(recovered) == 3


async def test_cached_episode_survives_serialization(provider: LocalFileSystemProvider) -> None:
    """Everything the players and UI need survives the cache round trip."""
    with patch(PARSE_TAGS_TARGET, new=_parse_tags_spy(chapters=True)):
        [fresh, *_] = [x async for x in provider.get_podcast_episodes(PODCAST_FOLDER)]

    with patch(PARSE_TAGS_TARGET, new=_parse_tags_spy(chapters=True)) as parse_tags:
        [cached, *_] = [x async for x in provider.get_podcast_episodes(PODCAST_FOLDER)]
    # the second listing has to come out of the cache, otherwise everything below would
    # compare two freshly parsed episodes and prove nothing about serialization
    parse_tags.assert_not_awaited()

    # the scalar fields are covered by the to_dict() comparison in the cache hit test;
    # what needs its own assertions is what a wrong deserialization would silently produce
    assert isinstance(cached, PodcastEpisode)
    assert [x.name for x in cached.metadata.chapters or []] == ["Intro", "Outro"]
    # the podcast attribute is a Podcast | ItemMapping union: it must resolve to Podcast,
    # because get_podcast() reads the parent podcast straight off a listed episode
    assert isinstance(cached.podcast, Podcast)
    assert isinstance(fresh.podcast, Podcast)
    assert cached.podcast.publisher == "ACME Radio"
    assert (
        next(iter(cached.provider_mappings)).audio_format
        == next(iter(fresh.provider_mappings)).audio_format
    )


async def test_get_podcast_uses_cached_listing(provider: LocalFileSystemProvider) -> None:
    """get_podcast reads the parent podcast off the cached listing without parsing files."""
    with patch(PARSE_TAGS_TARGET, new=_parse_tags_spy()):
        [x async for x in provider.get_podcast_episodes(PODCAST_FOLDER)]

    with patch(PARSE_TAGS_TARGET, new=_parse_tags_spy()) as parse_tags:
        podcast = await provider.get_podcast(PODCAST_FOLDER)

    parse_tags.assert_not_awaited()
    assert isinstance(podcast, Podcast)
    assert podcast.name == PODCAST_FOLDER


async def test_no_resume_state_is_cached(provider: LocalFileSystemProvider) -> None:
    """Resume info applied to a listed episode never ends up in the provider cache."""
    with patch(PARSE_TAGS_TARGET, new=_parse_tags_spy()):
        episodes = [x async for x in provider.get_podcast_episodes(PODCAST_FOLDER)]
        # the controller applies per-user resume info to the objects the provider yields
        for episode in episodes:
            episode.fully_played = True
            episode.resume_position_ms = 120000

        cached = [x async for x in provider.get_podcast_episodes(PODCAST_FOLDER)]

    assert len(cached) == 3
    assert all(x.fully_played is None for x in cached)
    assert all(not x.resume_position_ms for x in cached)
