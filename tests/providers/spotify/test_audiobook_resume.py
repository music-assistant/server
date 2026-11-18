"""Tests for the Spotify audiobook resume position."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MediaNotFoundError, RetriesExhausted

from music_assistant.providers.spotify.constants import CONF_SYNC_AUDIOBOOK_PROGRESS
from music_assistant.providers.spotify.provider import SpotifyProvider
from tests.common import use_real_create_task

ENDPOINT = "audiobooks/book1/chapters"
# Spotify keeps the ETag while the first chapter and the chapter count stay the same
ETAG = "chapters-etag"
CHAPTER_MS = 600000


class _PageCache:
    """Dict-backed stand-in for the cache controller, keyed on key and checksum."""

    def __init__(self) -> None:
        """Initialize an empty cache."""
        self.entries: dict[tuple[str, str | None], Any] = {}
        self.get = AsyncMock(side_effect=self._get)
        self.set = AsyncMock(side_effect=self._set)
        # @use_cache never gets a hit, so every decorated method runs its body
        self.get_with_freshness = AsyncMock(return_value=(None, False, False))

    def _get(self, key: str, checksum: str | None = None, **_kwargs: Any) -> Any:
        return self.entries.get((key, checksum))

    def _set(self, key: str, data: Any, checksum: str | None = None, **_kwargs: Any) -> None:
        self.entries[(key, checksum)] = data


def _make_provider(
    chapters: list[dict[str, Any]],
) -> tuple[SpotifyProvider, AsyncMock, _PageCache]:
    """Return a Spotify provider whose mocked Spotify API serves the given chapters in pages."""
    provider = object.__new__(SpotifyProvider)
    provider.config = MagicMock(instance_id="spotify--test")
    provider.config.get_value.side_effect = {CONF_SYNC_AUDIOBOOK_PROGRESS: True}.get
    provider.manifest = MagicMock(domain="spotify")
    provider.logger = MagicMock()
    provider._audiobooks_supported = True
    cache = _PageCache()
    provider.mass = MagicMock(cache=cache)
    use_real_create_task(provider.mass)

    async def _get_page(_endpoint: str, limit: int, offset: int, **_kwargs: Any) -> dict[str, Any]:
        return {"etag": ETAG, "total": len(chapters), "items": chapters[offset : offset + limit]}

    get_data = AsyncMock(side_effect=_get_page)
    provider._get_data = get_data  # type: ignore[method-assign]
    return provider, get_data, cache


def _chapter(
    chapter_id: str | None, fully_played: bool = False, position_ms: int = 0
) -> dict[str, Any]:
    """Return a chapter entry as returned by the audiobook chapters endpoint."""
    return {
        "id": chapter_id,
        "duration_ms": CHAPTER_MS,
        "resume_point": {"fully_played": fully_played, "resume_position_ms": position_ms},
    }


def _page_request(offset: int) -> Any:
    """Return the expected live request for the chapters page at the given offset."""
    return call(ENDPOINT, limit=50, offset=offset, market="from_token")


async def test_resume_position_reads_live_chapter_pages() -> None:
    """Played chapters and the partial chapter add up, read without the page cache."""
    provider, get_data, cache = _make_provider(
        [
            _chapter("chapter1", fully_played=True),
            # an entry without an id is not in the chapter list, so it adds no duration
            _chapter(None, fully_played=True),
            _chapter("chapter2", position_ms=15000),
            _chapter("chapter3"),
        ]
    )

    result = await provider.get_resume_position("book1", MediaType.AUDIOBOOK)

    assert result == (False, CHAPTER_MS + 15000, None)
    assert get_data.await_args_list == [_page_request(0)]
    cache.get.assert_not_awaited()
    cache.set.assert_not_awaited()


async def test_resume_position_follows_progress_after_first_chapter() -> None:
    """Progress made elsewhere after the first chapter shows up at the next read."""
    chapters = [
        _chapter("chapter1", fully_played=True),
        _chapter("chapter2", position_ms=15000),
        _chapter("chapter3"),
    ]
    provider, _get_data, _cache = _make_provider(chapters)

    before = await provider.get_resume_position("book1", MediaType.AUDIOBOOK)
    chapters[1:] = [
        _chapter("chapter2", fully_played=True),
        _chapter("chapter3", position_ms=20000),
    ]
    after = await provider.get_resume_position("book1", MediaType.AUDIOBOOK)

    assert before == (False, CHAPTER_MS + 15000, None)
    assert after == (False, 2 * CHAPTER_MS + 20000, None)


async def test_resume_position_stops_at_first_unfinished_chapter() -> None:
    """Pages after the one holding the first unfinished chapter are never requested."""
    chapters = [_chapter(f"chapter{idx}", fully_played=True) for idx in range(60)]
    chapters.append(_chapter("chapter60", position_ms=30000))
    chapters.extend(_chapter(f"chapter{idx}") for idx in range(61, 120))
    provider, get_data, _cache = _make_provider(chapters)

    result = await provider.get_resume_position("book1", MediaType.AUDIOBOOK)

    assert result == (False, 60 * CHAPTER_MS + 30000, None)
    assert get_data.await_args_list == [_page_request(0), _page_request(50)]


async def test_resume_position_of_fully_played_book() -> None:
    """A fully played book reports its total duration without paging past the last chapter."""
    chapters = [_chapter(f"chapter{idx}", fully_played=True) for idx in range(100)]
    provider, get_data, _cache = _make_provider(chapters)

    result = await provider.get_resume_position("book1", MediaType.AUDIOBOOK)

    assert result == (True, 100 * CHAPTER_MS, None)
    assert get_data.await_args_list == [_page_request(0), _page_request(50)]


@pytest.mark.parametrize(
    "error",
    [None, MediaNotFoundError("Audiobook not found"), RetriesExhausted("Retries exhausted")],
    ids=["no_chapters", "not_found", "retries_exhausted"],
)
async def test_resume_position_unavailable(error: Exception | None) -> None:
    """A book without chapters or an unreachable book leaves the resume position to MA."""
    provider, get_data, _cache = _make_provider([])
    if error is not None:
        get_data.side_effect = error

    with pytest.raises(NotImplementedError):
        await provider.get_resume_position("book1", MediaType.AUDIOBOOK)
