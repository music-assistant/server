"""Tests for playlist import track matching and scoring logic."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.enums import ExternalID, ImageType, MediaType
from music_assistant_models.media_items import (
    Artist,
    ItemMapping,
    MediaItemImage,
    MediaItemMetadata,
    Playlist,
    ProviderMapping,
    Track,
    UniqueList,
)

from music_assistant.helpers.playlists import (
    PlaylistItem,
    ProviderMappingInfo,
    generate_m3u,
)
from music_assistant.providers.builtin import BuiltinProvider


def _make_provider() -> BuiltinProvider:
    """Create a minimal BuiltinProvider with mocked mass."""
    mass = MagicMock()
    prov = object.__new__(BuiltinProvider)
    prov.mass = mass
    prov.logger = MagicMock()
    return prov


def _make_track(
    name: str,
    artists: list[str] | None = None,
    duration: int = 0,
    album_name: str | None = None,
    version: str = "",
    isrc: str | None = None,
    mbid: str | None = None,
    media_type: MediaType = MediaType.TRACK,
) -> Track:
    """Build a Track for matching tests."""
    artist_list: UniqueList[Artist | ItemMapping] = UniqueList()
    for a in artists or []:
        artist_list.append(
            ItemMapping(item_id=a, provider="test", name=a, media_type=MediaType.ARTIST)
        )
    external_ids: set[tuple[ExternalID, str]] = set()
    if isrc:
        external_ids.add((ExternalID.ISRC, isrc))
    if mbid:
        external_ids.add((ExternalID.MB_RECORDING, mbid))
    album_mapping = None
    if album_name:
        album_mapping = ItemMapping(
            item_id=album_name, provider="test", name=album_name, media_type=MediaType.ALBUM
        )
    track = Track(
        item_id="test123",
        provider="test",
        name=name,
        version=version,
        duration=duration,
        artists=artist_list,
        album=album_mapping,
        external_ids=external_ids,
        provider_mappings={
            ProviderMapping(
                item_id="test123",
                provider_domain="test",
                provider_instance="test",
            )
        },
    )
    track.media_type = media_type
    return track


def _make_playlist_item(
    title: str | None = None,
    length: str | None = None,
    metadata: dict[str, str] | None = None,
    providers: list[ProviderMappingInfo] | None = None,
) -> PlaylistItem:
    """Build a PlaylistItem for matching tests."""
    return PlaylistItem(
        path="spotify://track/original",
        title=title,
        length=length,
        metadata=metadata,
        providers=providers or [],
    )


def _make_playlist(name: str, image_url: str | None = None) -> Playlist:
    """Build a Playlist for builtin provider tests."""
    metadata = MediaItemMetadata()
    if image_url:
        metadata.images = UniqueList(
            [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=image_url,
                    provider="builtin",
                    remotely_accessible=True,
                )
            ]
        )
    return Playlist(
        item_id="playlist_1",
        provider="builtin",
        name=name,
        metadata=metadata,
        provider_mappings={
            ProviderMapping(
                item_id="playlist_1",
                provider_domain="builtin",
                provider_instance="builtin",
            )
        },
    )


# --------------------------------------------------------------------------- #
#  _score_track_match                                                          #
# --------------------------------------------------------------------------- #


class TestScoreTrackMatch:
    """Tests for the _score_track_match scoring method."""

    def setup_method(self) -> None:
        """Set up test fixtures."""
        self.prov = _make_provider()

    def test_isrc_match_returns_max_score(self) -> None:
        """ISRC match should return 10 (maximum score)."""
        candidate = _make_track("Song", artists=["Artist"], isrc="USRC17607839")
        item = _make_playlist_item(
            title="Artist - Song",
            metadata={"isrc": "USRC17607839"},
        )
        assert self.prov._score_track_match(candidate, item) == 10

    def test_isrc_match_case_insensitive(self) -> None:
        """ISRC matching should be case-insensitive."""
        candidate = _make_track("Song", artists=["Artist"], isrc="usrc17607839")
        item = _make_playlist_item(
            title="Artist - Song",
            metadata={"isrc": "USRC17607839"},
        )
        assert self.prov._score_track_match(candidate, item) == 10

    def test_mbid_match_returns_max_score(self) -> None:
        """MusicBrainz recording ID match should return 10."""
        candidate = _make_track(
            "Song", artists=["Artist"], mbid="a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        )
        item = _make_playlist_item(
            title="Artist - Song",
            metadata={"mbid": "A1B2C3D4-E5F6-7890-ABCD-EF1234567890"},
        )
        assert self.prov._score_track_match(candidate, item) == 10

    def test_title_match_only(self) -> None:
        """Title-only match (no artist) should score 1."""
        candidate = _make_track("Song Title")
        item = _make_playlist_item(title="Song Title")
        assert self.prov._score_track_match(candidate, item) == 1

    def test_title_mismatch_returns_zero(self) -> None:
        """Non-matching title should return 0."""
        candidate = _make_track("Completely Different")
        item = _make_playlist_item(title="Song Title")
        assert self.prov._score_track_match(candidate, item) == 0

    def test_title_and_artist_match(self) -> None:
        """Title + artist match should score 3 (1 title + 2 artist)."""
        candidate = _make_track("Song", artists=["Radiohead"])
        item = _make_playlist_item(title="Radiohead - Song")
        assert self.prov._score_track_match(candidate, item) == 3

    def test_artist_mismatch_returns_zero(self) -> None:
        """When artist is provided but doesn't match, return 0."""
        candidate = _make_track("Song", artists=["Coldplay"])
        item = _make_playlist_item(title="Radiohead - Song")
        assert self.prov._score_track_match(candidate, item) == 0

    def test_album_bonus(self) -> None:
        """Matching album adds 1 to the score."""
        candidate = _make_track("Song", artists=["Artist"], album_name="OK Computer")
        item = _make_playlist_item(
            title="Artist - Song",
            metadata={"album": "OK Computer"},
        )
        # 1 (title) + 2 (artist) + 1 (album) = 4
        assert self.prov._score_track_match(candidate, item) == 4

    def test_duration_near_exact_bonus(self) -> None:
        """Duration within 2 seconds adds 2 to the score."""
        candidate = _make_track("Song", artists=["Artist"], duration=241)
        item = _make_playlist_item(title="Artist - Song", length="240")
        # 1 (title) + 2 (artist) + 2 (duration) = 5
        assert self.prov._score_track_match(candidate, item) == 5

    def test_duration_close_bonus(self) -> None:
        """Duration within 5 seconds adds 1 to the score."""
        candidate = _make_track("Song", artists=["Artist"], duration=245)
        item = _make_playlist_item(title="Artist - Song", length="240")
        # 1 (title) + 2 (artist) + 1 (duration close) = 4
        assert self.prov._score_track_match(candidate, item) == 4

    def test_duration_too_far_no_bonus(self) -> None:
        """Duration beyond 5 seconds gets no bonus."""
        candidate = _make_track("Song", artists=["Artist"], duration=260)
        item = _make_playlist_item(title="Artist - Song", length="240")
        # 1 (title) + 2 (artist) = 3
        assert self.prov._score_track_match(candidate, item) == 3

    def test_version_match_bonus(self) -> None:
        """Matching version adds 1."""
        candidate = _make_track("Song", artists=["Artist"], version="Remastered")
        item = _make_playlist_item(
            title="Artist - Song",
            metadata={"version": "Remastered"},
        )
        # 1 (title) + 2 (artist) + 1 (version) = 4
        assert self.prov._score_track_match(candidate, item) == 4

    def test_version_mismatch_penalty(self) -> None:
        """Mismatched version subtracts 1."""
        candidate = _make_track("Song", artists=["Artist"], version="Live")
        item = _make_playlist_item(
            title="Artist - Song",
            metadata={"version": "Remastered"},
        )
        # 1 (title) + 2 (artist) - 1 (version mismatch) = 2
        assert self.prov._score_track_match(candidate, item) == 2

    def test_media_type_gate(self) -> None:
        """Mismatched media type should return 0."""
        candidate = _make_track("Song", artists=["Artist"])
        item = _make_playlist_item(
            title="Artist - Song",
            metadata={"media_type": "podcast_episode"},
        )
        assert self.prov._score_track_match(candidate, item) == 0

    def test_none_title_returns_zero(self) -> None:
        """PlaylistItem with no title should score 0."""
        candidate = _make_track("Song")
        item = _make_playlist_item(title=None)
        assert self.prov._score_track_match(candidate, item) == 0

    def test_full_metadata_high_score(self) -> None:
        """Track with all metadata matching should get a high score."""
        candidate = _make_track(
            "Everything In Its Right Place",
            artists=["Radiohead"],
            duration=240,
            album_name="Kid A",
            version="Remastered",
        )
        item = _make_playlist_item(
            title="Radiohead - Everything In Its Right Place",
            length="240",
            metadata={
                "media_type": "track",
                "album": "Kid A",
                "version": "Remastered",
            },
        )
        # 1 (title) + 2 (artist) + 1 (album) + 1 (version) + 2 (duration exact) = 7
        assert self.prov._score_track_match(candidate, item) == 7


async def test_import_playlist_preserves_playlist_image() -> None:
    """Test that importing an M3U keeps the playlist-level image."""
    prov = _make_provider()
    prov_any = cast("Any", prov)
    prov_any.create_playlist = AsyncMock(return_value=_make_playlist("Imported Playlist"))
    prov_any.get_playlist = AsyncMock(
        return_value=_make_playlist("Imported Playlist", "https://img.example.com/cover.jpg")
    )
    prov_any._write_m3u_file = AsyncMock()

    m3u_data = generate_m3u(
        "Imported Playlist",
        [PlaylistItem(path="spotify://track/abc123", title="Test", length="120")],
        "https://img.example.com/cover.jpg",
    )

    result = await prov.import_playlist(m3u_data)

    assert prov_any._write_m3u_file.await_args is not None
    args = prov_any._write_m3u_file.await_args.args
    assert args[0] == "playlist_1"
    assert args[1] == "Imported Playlist"
    assert args[3] == "https://img.example.com/cover.jpg"
    assert result.image is not None
    assert result.image.path == "https://img.example.com/cover.jpg"


async def test_remove_playlist_tracks_preserves_playlist_image() -> None:
    """Test that rewriting a playlist after track removal keeps the playlist image."""
    prov = _make_provider()
    prov._playlist_locks = {}
    prov_any = cast("Any", prov)
    prov_any._read_m3u_file = AsyncMock(
        return_value=generate_m3u(
            "My Playlist",
            [
                PlaylistItem(path="spotify://track/one", title="One", length="120"),
                PlaylistItem(path="spotify://track/two", title="Two", length="180"),
            ],
            "https://img.example.com/cover.jpg",
        )
    )
    prov_any.get_playlist = AsyncMock(
        return_value=_make_playlist("My Playlist", "https://img.example.com/cover.jpg")
    )
    prov_any._write_m3u_file = AsyncMock()

    await prov.remove_playlist_tracks("playlist_1", (1,))

    assert prov_any._write_m3u_file.await_args is not None
    args = prov_any._write_m3u_file.await_args.args
    assert args[0] == "playlist_1"
    assert args[1] == "My Playlist"
    assert len(args[2]) == 1
    assert args[3] == "https://img.example.com/cover.jpg"
