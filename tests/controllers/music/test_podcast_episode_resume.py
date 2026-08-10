"""
Tests for applying per-user resume info to a provider's podcast episode listing.

Resume info for a listing is read from the playlog table in one batched query, no matter
how many episodes the podcast has. The integration tests use the ``mass`` fixture from
``tests/conftest.py``, which creates a full MusicAssistant instance with a real SQLite
database in a temporary directory.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import Podcast, PodcastEpisode, ProviderMapping

from music_assistant.constants import DB_TABLE_PLAYLOG
from music_assistant.mass import MusicAssistant
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant_models.auth import User

PROVIDER_ID = "test_podcast_prov"
PODCAST_ID = "show-001"


class _StubPodcastProvider(MusicProvider):
    """Minimal music provider yielding a prepared list of podcast episodes."""

    def __init__(self, episodes: list[PodcastEpisode]) -> None:
        """
        Initialize the stub provider.

        :param episodes: The episodes to yield from get_podcast_episodes.
        """
        self.episodes = episodes
        self.config = MagicMock()
        self.config.instance_id = PROVIDER_ID
        self.manifest = MagicMock()
        self.manifest.domain = PROVIDER_ID
        self.logger = MagicMock()

    async def get_podcast_episodes(self, prov_podcast_id: str) -> AsyncGenerator[PodcastEpisode]:
        """Yield the prepared episodes for the given podcast id."""
        for episode in self.episodes:
            yield episode


def _episode(index: int, **kwargs: Any) -> PodcastEpisode:
    """
    Build a provider podcast episode.

    :param index: Episode number, also used to derive its item id.
    :param kwargs: Extra PodcastEpisode attributes (e.g. resume info).
    """
    item_id = f"ep-{index:03d}"
    return PodcastEpisode(
        item_id=item_id,
        provider=PROVIDER_ID,
        name=f"Episode {index}",
        provider_mappings={
            ProviderMapping(
                item_id=item_id,
                provider_domain=PROVIDER_ID,
                provider_instance=PROVIDER_ID,
            )
        },
        position=index,
        podcast=Podcast(
            item_id=PODCAST_ID,
            provider=PROVIDER_ID,
            name="My Podcast",
            provider_mappings={
                ProviderMapping(
                    item_id=PODCAST_ID,
                    provider_domain=PROVIDER_ID,
                    provider_instance=PROVIDER_ID,
                )
            },
        ),
        **kwargs,
    )


async def _add_playlog_row(
    mass: MusicAssistant,
    item_id: str,
    userid: str,
    seconds_played: int,
    fully_played: bool,
    timestamp: int = 1000,
) -> None:
    """Seed one playlog row for a podcast episode."""
    await mass.music.database.insert(
        DB_TABLE_PLAYLOG,
        {
            "item_id": item_id,
            "provider": PROVIDER_ID,
            "media_type": MediaType.PODCAST_EPISODE.value,
            "name": item_id,
            "userid": userid,
            "seconds_played": seconds_played,
            "fully_played": fully_played,
            "timestamp": timestamp,
        },
        allow_replace=True,
    )


@pytest.fixture(name="count_playlog_queries")
def count_playlog_queries_fixture(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> Callable[[], int]:
    """Count every database read of the playlog table, single row or batched."""
    calls = 0

    def _counted(name: str) -> Callable[..., Any]:
        original = getattr(mass.music.database, name)

        async def _wrapper(table: str, *args: Any, **kwargs: Any) -> Any:
            nonlocal calls
            if table == DB_TABLE_PLAYLOG:
                calls += 1
            return await original(table, *args, **kwargs)

        return _wrapper

    for name in ("get_row", "get_rows"):
        monkeypatch.setattr(mass.music.database, name, _counted(name))
    return lambda: calls


async def _list_episodes(
    mass: MusicAssistant,
    episodes: list[PodcastEpisode],
    user: User | None = None,
) -> list[PodcastEpisode]:
    """
    Run the controller's provider listing for a stub provider.

    :param mass: The MusicAssistant instance to run against.
    :param episodes: The episodes the stub provider yields.
    :param user: The session user the request is made for, if any.
    """
    provider = _StubPodcastProvider(episodes)
    with (
        patch.object(mass, "get_provider", return_value=provider),
        patch(
            "music_assistant.controllers.music.media.podcasts.get_current_user",
            return_value=user,
        ),
    ):
        return [
            x
            async for x in mass.music.podcasts._get_provider_podcast_episodes(
                PODCAST_ID, PROVIDER_ID
            )
        ]


async def _explain_resume_query(mass: MusicAssistant, user: User | None) -> list[str]:
    """
    Return the query plan rows for the batched resume query as the controller emits it.

    :param mass: The MusicAssistant instance to run against.
    :param user: The session user the request is made for, if any.
    """
    database = mass.music.database
    # drop the planner statistics so SQLite assumes its default (large) table size instead of
    # the handful of rows seeded here, where scanning the table really is the cheaper plan
    await database.execute("ANALYZE")
    await database.execute("DELETE FROM sqlite_stat1")
    await database.execute("ANALYZE sqlite_master")

    captured: dict[str, Any] = {}
    original = database._db.execute_fetchall

    async def _spy(sql: str, params: Any = None) -> Any:
        if DB_TABLE_PLAYLOG in sql and sql.startswith("SELECT"):
            captured["sql"], captured["params"] = sql, params
        return await original(sql, params)

    with patch.object(database._db, "execute_fetchall", _spy):
        await _list_episodes(mass, [_episode(1)], user=user)

    # the emitted query is explained rather than a copy of it, so the plan cannot drift
    # away from the query this test claims to cover
    assert "sql" in captured, "no playlog select was emitted"
    plan = await database.get_rows_from_query(
        f"EXPLAIN QUERY PLAN {captured['sql']}", captured["params"], limit=0
    )
    return [row["detail"] for row in plan]


async def test_resume_info_is_scoped_to_the_requesting_user(mass: MusicAssistant) -> None:
    """The requesting user's progress is applied; another user's progress is ignored."""
    user = await mass.webserver.auth.create_user("podcastresume")
    other_user = await mass.webserver.auth.create_user("podcastresumeother")
    await _add_playlog_row(mass, "ep-001", user.user_id, seconds_played=90, fully_played=False)
    await _add_playlog_row(mass, "ep-002", other_user.user_id, seconds_played=30, fully_played=True)

    result = await _list_episodes(mass, [_episode(1), _episode(2)], user=user)

    assert result[0].resume_position_ms == 90000
    assert result[0].fully_played is False
    assert result[1].resume_position_ms is None
    assert result[1].fully_played is None


async def test_native_resume_info_is_not_overwritten(mass: MusicAssistant) -> None:
    """An episode that arrives with resume info from its provider is left untouched."""
    user = await mass.webserver.auth.create_user("podcastnative")
    await _add_playlog_row(mass, "ep-001", user.user_id, seconds_played=90, fully_played=True)

    episode = _episode(1, fully_played=False, resume_position_ms=5000)
    result = await _list_episodes(mass, [episode], user=user)

    assert result[0].fully_played is False
    assert result[0].resume_position_ms == 5000


async def test_listing_uses_a_single_playlog_query(
    mass: MusicAssistant, count_playlog_queries: Callable[[], int]
) -> None:
    """Resume info for an entire listing costs one query, not one query per episode."""
    user = await mass.webserver.auth.create_user("podcastbatched")
    await _add_playlog_row(mass, "ep-005", user.user_id, seconds_played=120, fully_played=False)

    episodes = [_episode(index) for index in range(1, 26)]
    result = await _list_episodes(mass, episodes, user=user)

    assert count_playlog_queries() == 1
    assert len(result) == 25
    assert result[4].resume_position_ms == 120000


async def test_no_playlog_query_when_provider_supplies_resume_info(
    mass: MusicAssistant, count_playlog_queries: Callable[[], int]
) -> None:
    """A provider that reports resume info natively triggers no playlog query at all."""
    user = await mass.webserver.auth.create_user("podcastnativeonly")
    episodes = [_episode(index, fully_played=False, resume_position_ms=1000) for index in (1, 2)]

    result = await _list_episodes(mass, episodes, user=user)

    assert count_playlog_queries() == 0
    assert all(x.resume_position_ms == 1000 for x in result)


async def test_resume_info_is_not_capped_by_the_default_row_limit(mass: MusicAssistant) -> None:
    """
    Resume info survives a playlog holding more rows than the default query limit.

    get_rows caps at 500 rows unless limit=0 is passed, and the batched query sorts by
    ascending timestamp - so the cap keeps the *oldest* rows and drops the newest, which are
    exactly the episodes a listener is part way through. The playlog keeps 90 days of history
    across every podcast of a provider, so busy households cross 500 rows.
    """
    user = await mass.webserver.auth.create_user("podcastrowlimit")
    row_count = 501
    for index in range(1, row_count + 1):
        # ascending timestamps: the newest row sorts last and is the first one a cap drops
        await _add_playlog_row(
            mass,
            f"ep-{index:03d}",
            user.user_id,
            seconds_played=index,
            fully_played=False,
            timestamp=1000 + index,
        )

    # the oldest row doubles as a control: it survives either way, so a failure below
    # points at the cap rather than at resume lookup being broken altogether
    result = await _list_episodes(mass, [_episode(1), _episode(row_count)], user=user)

    assert result[0].resume_position_ms == 1000
    assert result[1].resume_position_ms == row_count * 1000


async def test_resume_query_uses_the_provider_media_type_index(mass: MusicAssistant) -> None:
    """
    The batched resume query is served by an index instead of scanning the playlog.

    It filters on provider/media_type/userid, which the item_id-first unique index cannot
    serve, so without a dedicated index SQLite falls back to the userid index and reads every
    row that user ever played, of every media type. The playlog holds 90 days of history for
    the whole household, so that cost grows with listening activity rather than podcast size.
    """
    user = await mass.webserver.auth.create_user("podcastindexplan")
    await _add_playlog_row(mass, "ep-001", user.user_id, seconds_played=10, fully_played=False)

    details = await _explain_resume_query(mass, user)

    assert any(f"USING INDEX {DB_TABLE_PLAYLOG}_provider_media_type_idx" in x for x in details), (
        details
    )
    # with userid in the filter the equality prefix reaches timestamp, so the index satisfies
    # the ORDER BY on its own. That part does not survive userid dropping out of the filter -
    # the test below covers what still holds there
    assert not any("TEMP B-TREE" in x for x in details), details


async def test_resume_query_uses_the_index_without_a_session_user(mass: MusicAssistant) -> None:
    """
    The batched resume query still avoids a playlog scan when no user can be resolved.

    Without a userid to filter on, the usable equality prefix stops at provider/media_type and
    SQLite sorts the matched rows rather than reading them already ordered. The lookup itself
    still has to go through the index: that sort covers one provider's episodes, where a table
    scan would cover the whole household's 90 days of history.
    """
    await _add_playlog_row(mass, "ep-001", "user-a", seconds_played=10, fully_played=False)

    with patch.object(
        mass.music, "_get_user_for_provider", new_callable=AsyncMock, return_value=None
    ):
        details = await _explain_resume_query(mass, None)

    assert any(f"USING INDEX {DB_TABLE_PLAYLOG}_provider_media_type_idx" in x for x in details), (
        details
    )


async def test_without_a_session_user_the_newest_progress_wins(mass: MusicAssistant) -> None:
    """With no user to scope to, the most recently played row is applied."""
    # the newest row is deliberately both inserted first and owned by the alphabetically
    # first userid, so neither insertion order nor the index's (userid, timestamp) key order
    # can leave it last. The map the controller builds keeps whichever row it sees last, so
    # only an explicit sort on timestamp lands on the 240s one
    await _add_playlog_row(mass, "ep-001", "user-a", 240, fully_played=False, timestamp=2000)
    await _add_playlog_row(mass, "ep-001", "user-b", 30, fully_played=False, timestamp=1000)

    with patch.object(
        mass.music, "_get_user_for_provider", new_callable=AsyncMock, return_value=None
    ):
        result = await _list_episodes(mass, [_episode(1)], user=None)

    assert result[0].resume_position_ms == 240000
