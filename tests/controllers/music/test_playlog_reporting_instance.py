"""
Tests that a play reported by a provider instance is attributed to that instance's user.

When two accounts of one service (e.g. one Audiobookshelf instance per user) share a
library item, the item's first provider mapping must not decide whose progress it is.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from music_assistant_models.config_entries import ProviderAccess
from music_assistant_models.enums import MediaType, ProviderSharing
from music_assistant_models.media_items import Artist, AudioFormat, ProviderMapping, Track
from music_assistant_models.unique_list import UniqueList

from music_assistant.constants import DB_TABLE_PLAYLOG
from music_assistant.mass import MusicAssistant
from tests.common import set_music_source_access

if TYPE_CHECKING:
    from music_assistant_models.auth import User

INSTANCE_A = "audiobookshelf--a"
INSTANCE_B = "audiobookshelf--b"


def _mappings(item_id: str) -> set[ProviderMapping]:
    return {
        ProviderMapping(
            item_id=item_id,
            provider_domain="audiobookshelf",
            provider_instance=instance_id,
            audio_format=AudioFormat(),
        )
        for instance_id in (INSTANCE_A, INSTANCE_B)
    }


async def _setup(mass: MusicAssistant, name: str) -> tuple[User, User, Track]:
    """Create two users, each owning one instance, and a track merged from both instances."""
    user_a = await mass.webserver.auth.create_user(f"{name}a")
    user_b = await mass.webserver.auth.create_user(f"{name}b")
    set_music_source_access(
        mass,
        {
            INSTANCE_A: ProviderAccess(owner=user_a.user_id, sharing=ProviderSharing.PRIVATE),
            INSTANCE_B: ProviderAccess(owner=user_b.user_id, sharing=ProviderSharing.PRIVATE),
        },
    )
    added = await mass.music.tracks.add_item_to_library(
        Track(
            item_id=f"{name}-track",
            provider=INSTANCE_A,
            name=name,
            duration=180,
            provider_mappings=_mappings(f"{name}-track"),
            artists=UniqueList(
                [
                    Artist(
                        item_id=f"{name}-artist",
                        provider=INSTANCE_A,
                        name=f"{name} Artist",
                        provider_mappings=_mappings(f"{name}-artist"),
                    )
                ]
            ),
        )
    )
    track = await mass.music.tracks.get_library_item(added.item_id)
    assert {m.provider_instance for m in track.provider_mappings} == {INSTANCE_A, INSTANCE_B}
    return user_a, user_b, track


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


async def test_mark_played_uses_the_reporting_instance_user(mass: MusicAssistant) -> None:
    """Progress reported by each instance is stored for its owner only."""
    user_a, user_b, track = await _setup(mass, "reporter")

    # both directions, so the test fails whichever mapping a lookup would pick first
    await mass.music.mark_item_played(
        track,
        fully_played=False,
        seconds_played=60,
        user_initiated=False,
        provider_instance_id=INSTANCE_A,
    )
    await mass.music.mark_item_played(
        track,
        fully_played=False,
        seconds_played=120,
        user_initiated=False,
        provider_instance_id=INSTANCE_B,
    )

    row_a = await _playlog_row(mass, track, user_a.user_id)
    row_b = await _playlog_row(mass, track, user_b.user_id)
    assert row_a is not None
    assert row_b is not None
    assert row_a["seconds_played"] == 60
    assert row_b["seconds_played"] == 120


async def test_mark_unplayed_uses_the_reporting_instance_user(mass: MusicAssistant) -> None:
    """Progress discarded on one instance leaves the other owner's progress alone."""
    user_a, user_b, track = await _setup(mass, "discarder")
    for user in (user_a, user_b):
        await mass.music.mark_item_played(
            track, fully_played=False, seconds_played=60, userid=user.user_id
        )

    await mass.music.mark_item_unplayed(track, provider_instance_id=INSTANCE_B)
    assert await _playlog_row(mass, track, user_b.user_id) is None
    assert await _playlog_row(mass, track, user_a.user_id) is not None

    await mass.music.mark_item_unplayed(track, provider_instance_id=INSTANCE_A)
    assert await _playlog_row(mass, track, user_a.user_id) is None
