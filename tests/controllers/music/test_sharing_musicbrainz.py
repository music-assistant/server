"""Sharing resolves MusicBrainz release groups through authorized music sources."""

import pytest
from music_assistant_models.enums import ProviderType
from music_assistant_models.errors import MediaNotFoundError

from music_assistant.controllers.music import MusicController
from tests.common import set_music_source_access
from tests.controllers.music.test_musicbrainz_release_group import (
    RELEASE_GROUP_ID,
    SPOTIFY_ALBUM_ID,
    SPOTIFY_ALBUM_URL,
    _album,
    _edition,
    _harness,
    _release,
)


@pytest.mark.parametrize("hidden_mapping", [False, True])
async def test_share_release_group_through_real_resolution(hidden_mapping: bool) -> None:
    """Discography identities use the release resolver and authorized returned mappings."""
    album = _album("hidden" if hidden_mapping else "spotify_account", SPOTIFY_ALBUM_ID)
    with _harness(
        editions=[_edition("edition")],
        releases=[_release("edition", urls=[SPOTIFY_ALBUM_URL])],
        loaded={"spotify": ["spotify_account"]},
        albums={("spotify_account", SPOTIFY_ALBUM_ID): album},
    ) as harness:
        controller = MusicController.__new__(MusicController)
        controller.mass = harness.ctrl.mass
        controller.albums = harness.ctrl
        set_music_source_access(controller.mass, {"spotify_account": None})
        controller.mass.config.get("providers")["spotify_account"].update(
            domain="spotify", type=ProviderType.MUSIC.value
        )
        if hidden_mapping:
            with pytest.raises(MediaNotFoundError):
                await controller.get_item_share_info(f"musicbrainz://album/{RELEASE_GROUP_ID}")
        else:
            result = await controller.get_item_share_info(f"musicbrainz://album/{RELEASE_GROUP_ID}")
            assert result["links"][0]["url"] == SPOTIFY_ALBUM_URL
        harness.musicbrainz.browse_releases_by_release_group.assert_awaited_once_with(
            RELEASE_GROUP_ID, complete=False
        )
