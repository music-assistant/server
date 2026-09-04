"""Test the episode ordering of the ARD Audiothek provider."""

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

from music_assistant.providers.ard_audiothek import SUPPORTED_FEATURES, ARDAudiothek
from music_assistant.providers.ard_audiothek.database_queries import show_length_query


def _episode(core_id: str, publish_date: str) -> dict[str, Any]:
    """Return a minimal API episode node with a playable audio entry."""
    return {
        "coreId": core_id,
        "title": f"Episode {core_id}",
        "status": "PUBLISHED",
        "publishDate": publish_date,
        "duration": 60,
        "summary": "",
        "audioList": [{"href": f"https://example.com/{core_id}.mp3", "audioBitrate": 128}],
        "imagesList": [],
    }


def _provider(episodes: list[dict[str, Any]]) -> ARDAudiothek:
    """Return a provider serving the given episodes as a single API page."""
    config = Mock(instance_id="ard_audiothek--test", enabled=True)
    config.get_value.side_effect = lambda key, default=None: (
        "INFO" if key == "log_level" else default
    )
    provider = ARDAudiothek(Mock(), Mock(domain="ard_audiothek"), config, SUPPORTED_FEATURES)

    async def _execute(query: Any) -> dict[str, Any]:
        if query is show_length_query:
            return {"show": {"items": {"totalCount": len(episodes)}}}
        offset = query.variable_values["offset"]
        page = episodes[offset : offset + query.variable_values["first"]]
        return {"show": {"title": "My Show", "items": {"nodes": page}}}

    session = Mock(execute=AsyncMock(side_effect=_execute))
    client = Mock(__aenter__=AsyncMock(return_value=session), __aexit__=AsyncMock())
    provider.get_client = AsyncMock(return_value=client)  # type: ignore[method-assign]
    provider._update_progress = AsyncMock()  # type: ignore[method-assign]
    provider._get_progress = Mock(return_value=(False, 0))  # type: ignore[method-assign]
    return provider


async def test_episode_positions_run_oldest_to_newest() -> None:
    """The API lists episodes newest first, so the first one listed gets the highest position."""
    episodes = [
        _episode("c", "2026-08-31T18:00:00+02:00"),
        _episode("b", "2026-06-29T17:30:00+02:00"),
        _episode("a", "2026-06-22T17:30:00+02:00"),
    ]
    provider = _provider(episodes)

    with patch("music_assistant.providers.ard_audiothek._parse_podcast_episode") as parse:
        parse.side_effect = lambda *args: Mock(item_id=args[3], position=args[5])
        parsed = [ep async for ep in provider.get_podcast_episodes("show-1")]

    assert {ep.item_id: ep.position for ep in parsed} == {"a": 1, "b": 2, "c": 3}


async def test_depublished_and_audioless_episodes_are_skipped() -> None:
    """Episodes that cannot be played are left out of the listing and the numbering."""
    episodes = [
        _episode("c", "2026-08-31T18:00:00+02:00"),
        {**_episode("gone", "2026-07-01T00:00:00+02:00"), "status": "DEPUBLISHED"},
        {**_episode("silent", "2026-06-30T00:00:00+02:00"), "audioList": []},
        _episode("a", "2026-06-22T17:30:00+02:00"),
    ]
    provider = _provider(episodes)

    with patch("music_assistant.providers.ard_audiothek._parse_podcast_episode") as parse:
        parse.side_effect = lambda *args: Mock(item_id=args[3], position=args[5])
        parsed = [ep async for ep in provider.get_podcast_episodes("show-1")]

    assert {ep.item_id: ep.position for ep in parsed} == {"a": 1, "c": 2}


async def test_episodes_are_ranked_across_every_page() -> None:
    """A show long enough to be paged is numbered as one list, not one page at a time."""
    newest = datetime(2026, 9, 1, tzinfo=UTC)
    # the API lists newest first, and 600 episodes span more than one page
    episodes = [
        _episode(f"e{index:04d}", (newest - timedelta(days=index)).isoformat())
        for index in range(600)
    ]
    provider = _provider(episodes)

    with patch("music_assistant.providers.ard_audiothek._parse_podcast_episode") as parse:
        parse.side_effect = lambda *args: Mock(item_id=args[3], position=args[5])
        parsed = [ep async for ep in provider.get_podcast_episodes("show-1")]

    assert len(parsed) == 600
    # the oldest episode listed last is numbered 1, the newest listed first is numbered 600
    assert {ep.item_id: ep.position for ep in parsed if ep.position in (1, 600)} == {
        "e0599": 1,
        "e0000": 600,
    }
