"""Tests for LocalFileSystemProvider._resolve_artists_with_mbids."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from music_assistant.providers.filesystem_local import LocalFileSystemProvider


def _create_provider(mb_provider: object | None = None) -> LocalFileSystemProvider:
    """Create a bare LocalFileSystemProvider with a mocked MusicBrainz lookup.

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
