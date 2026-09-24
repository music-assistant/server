"""
Tests that a play reported by a provider instance is attributed to that instance's user.

When two accounts of one service (e.g. one Audiobookshelf instance per user) share a
library item, the item's first provider mapping must not decide whose progress it is.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import Artist, ProviderMapping, Track
from music_assistant_models.unique_list import UniqueList

from music_assistant.constants import DB_TABLE_PLAYLOG
from music_assistant.mass import MusicAssistant

if TYPE_CHECKING:
    from collections.abc import Iterable

    from music_assistant_models.auth import User

INSTANCE_A = "audiobookshelf--a"
INSTANCE_B = "audiobookshelf--b"


async def _add_track(mass: MusicAssistant, name: str) -> Track:
    artist = await mass.music.artists.add_item_to_library(
        Artist(
            item_id="0",
            provider="library",
            name=f"{name} Artist",
            provider_mappings={
                ProviderMapping(
                    item_id=f"{name}-artist",
                    provider_domain="library",
                    provider_instance="library",
                    in_library=True,
                )
            },
        )
    )
    added = await mass.music.tracks.add_item_to_library(
        Track(
            item_id="0",
            provider="library",
            name=name,
            duration=180,
            provider_mappings={
                ProviderMapping(
                    item_id=f"{name}-track",
                    provider_domain="library",
                    provider_instance="library",
                    in_library=True,
                )
            },
            artists=UniqueList([artist]),
        )
    )
    return await mass.music.tracks.get_library_item(added.item_id)


async def _playlog_row(mass: MusicAssistant, track: Track, userid: str) -> dict[str, Any] | None:
    row = await mass.music.database.get_row(
        DB_TABLE_PLAYLOG,
        {
            "item_id": track.item_id,
            "provider": "library",
            "media_type": MediaType.TRACK.value,
            "userid": userid,
        },
    )
    return dict(row) if row else None


def _user_lookup(user_a: User, user_b: User) -> Any:
    async def _lookup(provider_mappings_or_instance_id: Iterable[ProviderMapping] | str) -> User:
        if isinstance(provider_mappings_or_instance_id, str):
            return {INSTANCE_A: user_a, INSTANCE_B: user_b}[provider_mappings_or_instance_id]
        # guessing from the item's mappings lands on the first account
        return user_a

    return _lookup


async def test_mark_played_uses_the_reporting_instance_user(mass: MusicAssistant) -> None:
    """Progress reported by B's instance is stored for B, not for the first mapping's user."""
    user_a = await mass.webserver.auth.create_user("reportera")
    user_b = await mass.webserver.auth.create_user("reporterb")
    track = await _add_track(mass, "Reported")

    with patch.object(mass.music, "_get_user_for_provider", _user_lookup(user_a, user_b)):
        await mass.music.mark_item_played(
            track,
            fully_played=False,
            seconds_played=120,
            user_initiated=False,
            provider_instance_id=INSTANCE_B,
        )

    row_b = await _playlog_row(mass, track, user_b.user_id)
    assert row_b is not None
    assert row_b["seconds_played"] == 120
    assert await _playlog_row(mass, track, user_a.user_id) is None


async def test_mark_unplayed_uses_the_reporting_instance_user(mass: MusicAssistant) -> None:
    """A progress discarded on B's instance leaves A's progress alone."""
    user_a = await mass.webserver.auth.create_user("discardera")
    user_b = await mass.webserver.auth.create_user("discarderb")
    track = await _add_track(mass, "Discarded")
    for user in (user_a, user_b):
        await mass.music.mark_item_played(
            track, fully_played=False, seconds_played=60, userid=user.user_id
        )

    with patch.object(mass.music, "_get_user_for_provider", _user_lookup(user_a, user_b)):
        await mass.music.mark_item_unplayed(track, provider_instance_id=INSTANCE_B)

    assert await _playlog_row(mass, track, user_b.user_id) is None
    assert await _playlog_row(mass, track, user_a.user_id) is not None
