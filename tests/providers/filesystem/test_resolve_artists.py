"""Tests for LocalFileSystemProvider._resolve_artists_with_mbids."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.media_items import Album, Artist

from music_assistant.providers.filesystem_local import LocalFileSystemProvider

MBID_A = "11111111-1111-1111-1111-111111111111"
MBID_B = "22222222-2222-2222-2222-222222222222"


def _make_artist(name: str, mbid: str | None = None) -> Artist:
    """Create a minimal Artist, optionally carrying a MusicBrainz ID."""
    artist = Artist(item_id=name, provider="test", name=name, provider_mappings=set())
    if mbid:
        artist.mbid = mbid
    return artist


def _make_album(*artists: Artist) -> Album:
    """Create a minimal Album with the given artists."""
    album = Album(item_id="album", provider="test", name="Album", provider_mappings=set())
    for artist in artists:
        album.artists.append(artist)
    return album


def _create_provider(mb_provider: object | None = None) -> LocalFileSystemProvider:
    """
    Create a bare LocalFileSystemProvider with a mocked MusicBrainz lookup.

    :param mb_provider: Object returned by ``mass.get_provider("musicbrainz")``;
        ``None`` simulates the provider not being loaded.
    """
    with patch.object(LocalFileSystemProvider, "__init__", lambda *_a, **_kw: None):
        provider = LocalFileSystemProvider.__new__(LocalFileSystemProvider)

    provider.mass = MagicMock()
    provider.mass.get_provider = MagicMock(return_value=mb_provider)
    provider.logger = MagicMock()
    return provider


def _make_mb_provider(result: list[tuple[str, str, str] | None]) -> MagicMock:
    """Create a fake MusicBrainz provider whose lookup returns ``result``."""
    mb_provider = MagicMock()
    mb_provider.resolve_artists_from_mbids = AsyncMock(return_value=result)
    return mb_provider


class TestResolveArtistsWithMbids:
    """Test the parsed-name vs MBID reconciliation logic."""

    @pytest.mark.asyncio
    async def test_counts_match_uses_tags_without_lookup(self) -> None:
        """When name and MBID counts agree, tag data is used and MB is not queried."""
        provider = _create_provider()
        result = await provider._resolve_artists_with_mbids(
            parsed_names=("Artist A", "Artist B"),
            mbids=("mbid-a", "mbid-b"),
            sort_names=("A, Artist", "B, Artist"),
            log_label="ARTISTS tag",
        )
        assert result == [
            ("Artist A", "mbid-a", "A, Artist"),
            ("Artist B", "mbid-b", "B, Artist"),
        ]
        provider.mass.get_provider.assert_not_called()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_no_mbids_uses_tags_without_lookup(self) -> None:
        """With no MBIDs at all, tag-parsed names are returned with None MBIDs."""
        provider = _create_provider()
        result = await provider._resolve_artists_with_mbids(
            parsed_names=("Artist A", "Artist B"),
            mbids=(),
            sort_names=("A, Artist", "B, Artist"),
            log_label="ARTISTS tag",
        )
        assert result == [
            ("Artist A", None, "A, Artist"),
            ("Artist B", None, "B, Artist"),
        ]
        provider.mass.get_provider.assert_not_called()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_short_sort_names_pad_with_none(self) -> None:
        """A sort_names tuple shorter than parsed_names yields None for the tail."""
        provider = _create_provider()
        result = await provider._resolve_artists_with_mbids(
            parsed_names=("Artist A", "Artist B", "Artist C"),
            mbids=("mbid-a", "mbid-b", "mbid-c"),
            sort_names=("A, Artist",),
            log_label="ARTISTS tag",
        )
        assert result == [
            ("Artist A", "mbid-a", "A, Artist"),
            ("Artist B", "mbid-b", None),
            ("Artist C", "mbid-c", None),
        ]

    @pytest.mark.asyncio
    async def test_mismatch_without_provider_falls_back_to_tags(self) -> None:
        """A count mismatch with no MusicBrainz provider falls back to tag names."""
        provider = _create_provider(mb_provider=None)
        result = await provider._resolve_artists_with_mbids(
            parsed_names=("Artist A & Artist B",),
            mbids=("mbid-a", "mbid-b"),
            sort_names=(),
            log_label="ARTISTS tag",
        )
        assert result == [("Artist A & Artist B", "mbid-a", None)]
        provider.logger.warning.assert_called_once()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_mismatch_resolves_canonical_names(self) -> None:
        """A count mismatch queries MusicBrainz and returns its canonical triples."""
        mb_provider = _make_mb_provider(
            [
                ("Artist A", "mbid-a", "A, Artist"),
                ("Artist B", "mbid-b", "B, Artist"),
            ]
        )
        provider = _create_provider(mb_provider=mb_provider)
        result = await provider._resolve_artists_with_mbids(
            parsed_names=("Artist A & Artist B",),
            mbids=("mbid-a", "mbid-b"),
            sort_names=(),
            log_label="ARTISTS tag",
        )
        assert result == [
            ("Artist A", "mbid-a", "A, Artist"),
            ("Artist B", "mbid-b", "B, Artist"),
        ]
        mb_provider.resolve_artists_from_mbids.assert_awaited_once_with(("mbid-a", "mbid-b"))

    @pytest.mark.asyncio
    async def test_mismatch_drops_failed_lookups(self) -> None:
        """MBIDs whose lookup returned None are dropped from the resolved list."""
        mb_provider = _make_mb_provider(
            [
                ("Artist A", "mbid-a", "A, Artist"),
                None,
                ("Artist C", "mbid-c", "C, Artist"),
            ]
        )
        provider = _create_provider(mb_provider=mb_provider)
        result = await provider._resolve_artists_with_mbids(
            parsed_names=("Artist A & Artist C",),
            mbids=("mbid-a", "mbid-b", "mbid-c"),
            sort_names=(),
            log_label="ARTISTS tag",
        )
        assert result == [
            ("Artist A", "mbid-a", "A, Artist"),
            ("Artist C", "mbid-c", "C, Artist"),
        ]

    @pytest.mark.asyncio
    async def test_all_lookups_fail_falls_back_to_tags(self) -> None:
        """When every MusicBrainz lookup fails, tag-parsed names are used instead."""
        mb_provider = _make_mb_provider([None, None])
        provider = _create_provider(mb_provider=mb_provider)
        result = await provider._resolve_artists_with_mbids(
            parsed_names=("Artist A & Artist B",),
            mbids=("mbid-a", "mbid-b"),
            sort_names=(),
            log_label="ARTISTS tag",
        )
        assert result == [("Artist A & Artist B", "mbid-a", None)]
        provider.logger.warning.assert_called_once()  # type: ignore[attr-defined]


class TestMatchAlbumArtist:
    """Test reuse of an existing album artist when parsing track artists."""

    def test_no_album_returns_none(self) -> None:
        """With no album there is nothing to match against."""
        provider = _create_provider()
        assert provider._match_album_artist(None, "Artist A", MBID_A) is None

    def test_matches_on_mbid_despite_different_name(self) -> None:
        """
        The album artist is reused when MBIDs agree even if names differ.

        This is the mixed case: the album-artist tag count matched its MBID
        count (so it kept the tag-parsed name), while the track-artist count
        mismatched and was resolved to a canonical MusicBrainz name.
        """
        album_artist = _make_artist("Above & Beyond", mbid=MBID_A)
        album = _make_album(album_artist)
        provider = _create_provider()
        assert provider._match_album_artist(album, "Above and Beyond", MBID_A) is album_artist

    def test_matches_on_name_when_no_mbid(self) -> None:
        """Without a track-artist MBID, fall back to an exact name match."""
        album_artist = _make_artist("Artist A")
        album = _make_album(album_artist)
        provider = _create_provider()
        assert provider._match_album_artist(album, "Artist A", None) is album_artist

    def test_no_match_creates_nothing(self) -> None:
        """A different artist (no shared MBID, no shared name) does not match."""
        album = _make_album(_make_artist("Artist A", mbid=MBID_A))
        provider = _create_provider()
        assert provider._match_album_artist(album, "Artist B", MBID_B) is None

    def test_name_match_still_applies_with_unset_album_mbid(self) -> None:
        """
        A track MBID does not prevent the legacy name match when album has none.

        Matching is additive (MBID or name), so an album artist with no MBID is
        still reused by name even when the track artist carries one.
        """
        album_artist = _make_artist("Artist A")
        album = _make_album(album_artist)
        provider = _create_provider()
        assert provider._match_album_artist(album, "Artist A", MBID_A) is album_artist
