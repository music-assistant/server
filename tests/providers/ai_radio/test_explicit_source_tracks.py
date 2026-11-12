"""Tests that an AI Radio station leaves explicit tracks out for a restricted member."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.media_items import (
    MediaItemMetadata,
    Playlist,
    ProviderMapping,
    Track,
)

from music_assistant.providers.ai_radio.runtime import AIRadioRuntimeMixin

KID = User(user_id="kid", username="kid", role=UserRole.USER, allow_explicit=False)
PARENT = User(user_id="parent", username="parent", role=UserRole.USER)


def _track(item_id: str, explicit: bool) -> Track:
    """Build a library track with the given explicit flag."""
    return Track(
        item_id=item_id,
        provider="library",
        name=item_id,
        metadata=MediaItemMetadata(explicit=explicit),
        provider_mappings={
            ProviderMapping(
                item_id=item_id,
                provider_domain="filesystem_local",
                provider_instance="filesystem_local",
            )
        },
    )


async def _iterate(tracks: list[Track]) -> AsyncGenerator[Track]:
    """Yield the given tracks as the playlists controller does."""
    for track in tracks:
        yield track


class _Runtime(AIRadioRuntimeMixin):
    """Bare runtime harness over a source playlist of a clean and an explicit track."""

    def __init__(self) -> None:
        """Stamp the attrs the source fetch reads, skipping real provider init."""
        # the mixin declares `mass: MusicAssistant`; a mock stands in for tests
        self.mass: Any = MagicMock()
        self.mass.music.playlists.get = AsyncMock(
            return_value=Playlist(
                item_id="list", provider="library", name="List", provider_mappings=set()
            )
        )
        self.mass.music.playlists.iter_tracks = lambda *_args: _iterate(
            [_track("clean", False), _track("explicit", True)]
        )
        self.logger = MagicMock()

    async def _track_to_uri(self, track: Any) -> str:
        """Return the uri of a source track."""
        return f"library://track/{track.item_id}"


@pytest.mark.parametrize(("user", "expected"), [(KID, ["clean"]), (PARENT, ["clean", "explicit"])])
async def test_the_source_playlist_leaves_explicit_tracks_out(
    user: User, expected: list[str]
) -> None:
    """The run plays as the member who started it, so their setting narrows its tracks."""
    runtime = _Runtime()

    with patch("music_assistant.providers.ai_radio.runtime.get_current_user", return_value=user):
        tracks, _ = await runtime._fetch_source_tracks(
            {"source_playlist_id": "list", "source_playlist_provider": "library"}
        )

    assert [track["item_id"] for track in tracks] == expected
