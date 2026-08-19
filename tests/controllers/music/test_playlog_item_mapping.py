"""
Tests that the playlog commands accept the minimized items the Discover rows hand out.

Recommendation rows such as 'In progress' and 'Recently played' return ItemMapping objects
rather than full media items, and the frontend posts one of those straight back when the
user picks 'Mark as played'. See music-assistant/support#6131.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from unittest.mock import MagicMock, patch

from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import (
    ItemMapping,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
)

from music_assistant.constants import DB_TABLE_PLAYLOG
from music_assistant.helpers.api import parse_arguments
from music_assistant.mass import MusicAssistant
from music_assistant.models.music_provider import MusicProvider

PROVIDER_ID = "podcastfeed--LxfKPLhS"
PODCAST_ID = "stay-forever"
EPISODE_ID = "49d77a72faa63de8145f1d8d7b3a17d9"
EPISODE_NAME = "Forbidden Forest (SF 130)"


class _StubPodcastProvider(MusicProvider):
    """Minimal music provider that resolves a single podcast episode."""

    def __init__(self) -> None:
        """Initialize the stub provider."""
        self.config = MagicMock()
        self.config.instance_id = PROVIDER_ID
        self.manifest = MagicMock()
        self.manifest.domain = PROVIDER_ID
        self.logger = MagicMock()

    async def get_podcast_episode(self, prov_episode_id: str) -> PodcastEpisode:
        """Return the full episode for the given provider episode id."""
        return PodcastEpisode(
            item_id=prov_episode_id,
            provider=PROVIDER_ID,
            name=EPISODE_NAME,
            position=130,
            duration=7200,
            provider_mappings={
                ProviderMapping(
                    item_id=prov_episode_id,
                    provider_domain=PROVIDER_ID,
                    provider_instance=PROVIDER_ID,
                )
            },
            podcast=Podcast(
                item_id=PODCAST_ID,
                provider=PROVIDER_ID,
                name="Stay Forever",
                provider_mappings={
                    ProviderMapping(
                        item_id=PODCAST_ID,
                        provider_domain=PROVIDER_ID,
                        provider_instance=PROVIDER_ID,
                    )
                },
            ),
        )


def _minimized_payload(*, fully_played: bool) -> dict[str, Any]:
    """
    Build the media_item payload the frontend posts for an 'In progress' row item.

    :param fully_played: The played state the frontend stamps onto the item before sending.
    """
    return {
        "item_id": EPISODE_ID,
        "provider": PROVIDER_ID,
        "name": EPISODE_NAME,
        "version": "",
        "sort_name": "forbidden forest (sf 130)",
        "uri": f"{PROVIDER_ID}://podcast_episode/{EPISODE_ID}",
        "external_ids": [],
        "is_playable": True,
        "media_type": "podcast_episode",
        "available": True,
        "image": None,
        "year": None,
        "fully_played": fully_played,
        "resume_position_ms": 0,
    }


def _minimized_episode() -> ItemMapping:
    """Return the episode as in_progress_items hands it to the frontend."""
    return ItemMapping(
        item_id=EPISODE_ID,
        provider=PROVIDER_ID,
        name=EPISODE_NAME,
        media_type=MediaType.PODCAST_EPISODE,
    )


async def _playlog_rows(mass: MusicAssistant) -> list[Mapping[str, Any]]:
    """Return every playlog row for the episode under test."""
    return await mass.music.database.get_rows(
        DB_TABLE_PLAYLOG,
        {"media_type": MediaType.PODCAST_EPISODE.value, "item_id": EPISODE_ID},
    )


async def test_mark_played_command_accepts_a_minimized_media_item(
    mass: MusicAssistant,
) -> None:
    """The raw payload posted for an 'In progress' row parses into the command's arguments."""
    handler = mass.command_handlers["music/mark_played"]

    args = parse_arguments(
        handler.signature,
        handler.type_hints,
        {"media_item": _minimized_payload(fully_played=True), "fully_played": True},
    )

    assert isinstance(args["media_item"], ItemMapping)
    assert args["media_item"].uri == f"{PROVIDER_ID}://podcast_episode/{EPISODE_ID}"


async def test_mark_unplayed_command_accepts_a_minimized_media_item(
    mass: MusicAssistant,
) -> None:
    """The same payload parses for the mark_unplayed command."""
    handler = mass.command_handlers["music/mark_unplayed"]

    args = parse_arguments(
        handler.signature,
        handler.type_hints,
        {"media_item": _minimized_payload(fully_played=False)},
    )

    assert isinstance(args["media_item"], ItemMapping)


async def test_mark_played_records_a_minimized_podcast_episode(mass: MusicAssistant) -> None:
    """Marking an 'In progress' row item as played writes its playlog row."""
    user = await mass.webserver.auth.create_user("markplayedmapping")

    with patch.object(mass, "get_provider", return_value=_StubPodcastProvider()):
        await mass.music.mark_item_played(
            _minimized_episode(), fully_played=True, userid=user.user_id
        )

    rows = await _playlog_rows(mass)
    assert len(rows) == 1
    assert rows[0]["fully_played"]


async def test_mark_unplayed_clears_a_minimized_podcast_episode(mass: MusicAssistant) -> None:
    """Marking an 'In progress' row item as unplayed removes its playlog row again."""
    user = await mass.webserver.auth.create_user("markunplayedmapping")

    with patch.object(mass, "get_provider", return_value=_StubPodcastProvider()):
        await mass.music.mark_item_played(
            _minimized_episode(), fully_played=True, userid=user.user_id
        )
        assert await _playlog_rows(mass)

        await mass.music.mark_item_unplayed(_minimized_episode(), userid=user.user_id)

    assert not await _playlog_rows(mass)
