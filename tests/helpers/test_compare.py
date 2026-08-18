"""Tests for mediaitem compare helper functions."""

import pytest
from music_assistant_models import media_items
from music_assistant_models.enums import ExternalID

from music_assistant.helpers import compare


def _album(
    item_id: str = "1",
    provider: str = "test1",
    name: str = "Album A",
    version: str = "",
    year: int | None = None,
    external_ids: set[tuple[ExternalID, str]] | None = None,
) -> media_items.Album:
    """Build a minimal Album for evidence comparisons."""
    return media_items.Album(
        item_id=item_id,
        provider=provider,
        name=name,
        version=version,
        year=year,
        external_ids=external_ids or set(),
        artists=media_items.UniqueList(
            [
                media_items.Artist(
                    item_id="artist",
                    provider=provider,
                    name="Artist A",
                    provider_mappings={
                        media_items.ProviderMapping(
                            item_id="artist", provider_domain="test", provider_instance=provider
                        )
                    },
                )
            ]
        ),
        provider_mappings={
            media_items.ProviderMapping(
                item_id=item_id, provider_domain="test", provider_instance=provider
            )
        },
    )


def _track(
    item_id: str,
    disc_number: int = 1,
    track_number: int = 1,
    name: str = "Track",
    version: str = "",
    duration: int = 200,
    isrc: str | None = None,
) -> media_items.Track:
    """Build a minimal Track for album-track fingerprint comparisons."""
    return media_items.Track(
        item_id=item_id,
        provider="test1",
        name=name,
        version=version,
        duration=duration,
        disc_number=disc_number,
        track_number=track_number,
        external_ids={(ExternalID.ISRC, isrc)} if isrc else set(),
        provider_mappings={
            media_items.ProviderMapping(
                item_id=item_id, provider_domain="test", provider_instance="test1"
            )
        },
    )


def _tracklist(
    count: int, *, isrc_prefix: str = "USRC17607", duration: int = 200
) -> list[media_items.Track]:
    """Build an ordered list of distinct tracks sharing a common ISRC/duration scheme."""
    return [
        _track(
            item_id=str(number),
            track_number=number,
            name=f"Track {number}",
            duration=duration,
            isrc=f"{isrc_prefix}{number:03d}",
        )
        for number in range(1, count + 1)
    ]


def test_compare_version() -> None:
    """Test the version compare helper."""
    assert compare.compare_version("Remaster", "remaster") is True
    assert compare.compare_version("Remastered", "remaster") is True
    assert compare.compare_version("Remaster", "") is False
    assert compare.compare_version("Remaster", "Remix") is False
    assert compare.compare_version("", "Deluxe") is False
    assert compare.compare_version("", "Live") is False
    assert compare.compare_version("Live", "live") is True
    assert compare.compare_version("Live", "live version") is True
    assert compare.compare_version("Live version", "live") is True
    assert compare.compare_version("Deluxe Edition", "Deluxe") is True
    assert compare.compare_version("Deluxe Karaoke Edition", "Deluxe") is False
    assert compare.compare_version("Deluxe Karaoke Edition", "Karaoke") is False
    assert compare.compare_version("Deluxe Edition", "Edition Deluxe") is True
    assert compare.compare_version("", "Karaoke Version") is False
    assert compare.compare_version("Karaoke", "Karaoke Version") is True
    assert compare.compare_version("Remaster", "Remaster Edition Deluxe") is False
    assert compare.compare_version("Remastered Version", "Deluxe Version") is False
    assert compare.compare_version("2011 Remaster", "Remastered 2011") is True
    assert compare.compare_version("", "Album Version") is True
    assert compare.compare_version("Deluxe 2022 Remaster", "2022 Remaster") is False


def test_compare_version_deduplicates_repeated_tokens() -> None:
    """Repeated wording inside one version string does not block a match."""
    assert (
        compare.compare_version("Deluxe [2022 Remaster] 2022 Remaster", "Deluxe 2022 Remaster")
        is True
    )


def test_compare_version_ignores_hi_res_wording() -> None:
    """Quality-only wording (hi-res) is ignored wherever it appears in a version."""
    assert compare.compare_version("Remastered", "Remastered Hi-Res Version") is True
    assert compare.compare_version("Hi-Res Version", "") is True
    assert compare.compare_version("Hi-Res", "Remastered") is False


def test_compare_artist() -> None:
    """Test artist comparison."""
    artist_a = media_items.Artist(
        item_id="1",
        provider="test1",
        name="Artist A",
        provider_mappings={
            media_items.ProviderMapping(
                item_id="1", provider_domain="test", provider_instance="test1"
            )
        },
    )
    artist_b = media_items.Artist(
        item_id="1",
        provider="test2",
        name="Artist A",
        provider_mappings={
            media_items.ProviderMapping(
                item_id="2", provider_domain="test", provider_instance="test2"
            )
        },
    )
    # test match on name match
    assert compare.compare_artist(artist_a, artist_b) is True
    # test match on name mismatch
    artist_b.name = "Artist B"
    assert compare.compare_artist(artist_a, artist_b) is False
    # test on exact item_id match
    artist_b.item_id = artist_a.item_id
    artist_b.provider = artist_a.provider
    assert compare.compare_artist(artist_a, artist_b) is True
    # test on external id match
    artist_b.name = "Artist B"
    artist_b.item_id = "2"
    artist_b.provider = "test2"
    artist_a.external_ids = {(ExternalID.MB_ARTIST, "123")}
    artist_b.external_ids = artist_a.external_ids
    assert compare.compare_artist(artist_a, artist_b) is True
    # test on external id mismatch
    artist_b.name = artist_a.name
    artist_b.external_ids = {(ExternalID.MB_ARTIST, "1234")}
    assert compare.compare_artist(artist_a, artist_b) is False
    # test on external id mismatch while name matches
    artist_a = media_items.Artist(
        item_id="1",
        provider="test1",
        name="Artist A",
        external_ids={(ExternalID.MB_ARTIST, "123")},
        provider_mappings={
            media_items.ProviderMapping(
                item_id="1", provider_domain="test", provider_instance="test1"
            )
        },
    )
    artist_b = media_items.Artist(
        item_id="1",
        provider="test2",
        name="Artist A",
        external_ids={(ExternalID.MB_ARTIST, "abc")},
        provider_mappings={
            media_items.ProviderMapping(
                item_id="2", provider_domain="test", provider_instance="test2"
            )
        },
    )
    assert compare.compare_artist(artist_a, artist_b) is False


def test_compare_artist_name_normalization() -> None:
    """An accent difference in an artist name does not block a strict match."""
    artist_a = media_items.Artist(
        item_id="1",
        provider="test1",
        name="Bj\u00f6rk",
        provider_mappings={
            media_items.ProviderMapping(
                item_id="1", provider_domain="test", provider_instance="test1"
            )
        },
    )
    artist_b = media_items.Artist(
        item_id="2",
        provider="test2",
        name="Bjork",
        provider_mappings={
            media_items.ProviderMapping(
                item_id="2", provider_domain="test", provider_instance="test2"
            )
        },
    )

    assert compare.compare_artist(artist_a, artist_b) is True


def test_compare_album() -> None:
    """Test album comparison."""
    album_a = media_items.Album(
        item_id="1",
        provider="test1",
        name="Album A",
        provider_mappings={
            media_items.ProviderMapping(
                item_id="1", provider_domain="test", provider_instance="test1"
            )
        },
        artists=media_items.UniqueList(
            [
                media_items.Artist(
                    item_id="1",
                    provider="test1",
                    name="Artist A",
                    provider_mappings={
                        media_items.ProviderMapping(
                            item_id="1", provider_domain="test", provider_instance="test1"
                        )
                    },
                )
            ]
        ),
    )
    album_b = media_items.Album(
        item_id="1",
        provider="test2",
        name="Album A",
        provider_mappings={
            media_items.ProviderMapping(
                item_id="2", provider_domain="test", provider_instance="test2"
            )
        },
        artists=media_items.UniqueList(
            [
                media_items.Artist(
                    item_id="1",
                    provider="test1",
                    name="Artist A",
                    provider_mappings={
                        media_items.ProviderMapping(
                            item_id="1", provider_domain="test", provider_instance="test1"
                        )
                    },
                )
            ]
        ),
    )
    # test match on name match
    assert compare.compare_album(album_a, album_b) is True
    # test match on name mismatch
    album_b.name = "Album B"
    assert compare.compare_album(album_a, album_b) is False
    # test on version mismatch
    album_b.name = album_a.name
    album_b.version = "Deluxe"
    assert compare.compare_album(album_a, album_b) is False
    album_b.version = "Remix"
    assert compare.compare_album(album_a, album_b) is False
    # test on version match
    album_b.name = album_a.name
    album_a.version = "Deluxe"
    album_b.version = "Deluxe Edition"
    assert compare.compare_album(album_a, album_b) is True
    # test on exact item_id match
    album_b.item_id = album_a.item_id
    album_b.provider = album_a.provider
    assert compare.compare_album(album_a, album_b) is True
    # test on external id match
    album_b.name = "Album B"
    album_b.item_id = "2"
    album_b.provider = "test2"
    album_a.external_ids = {(ExternalID.MB_ALBUM, "123")}
    album_b.external_ids = album_a.external_ids
    assert compare.compare_album(album_a, album_b) is True
    # test on external id mismatch
    album_b.name = album_a.name
    album_b.external_ids = {(ExternalID.MB_ALBUM, "1234")}
    assert compare.compare_album(album_a, album_b) is False
    album_a.external_ids = set()
    album_b.external_ids = set()
    # fail on year mismatch
    album_b.external_ids = set()
    album_a.year = 2021
    album_b.year = 2020
    assert compare.compare_album(album_a, album_b) is False
    # pass on year match
    album_b.year = 2021
    assert compare.compare_album(album_a, album_b) is True
    # fail on artist mismatch
    album_a.artists = media_items.UniqueList(
        [media_items.ItemMapping(item_id="1", provider="test1", name="Artist A")]
    )
    album_b.artists = media_items.UniqueList(
        [media_items.ItemMapping(item_id="2", provider="test1", name="Artist B")]
    )
    assert compare.compare_album(album_a, album_b) is False
    # pass on partial artist match (if first artist matches)
    album_a.artists = media_items.UniqueList(
        [media_items.ItemMapping(item_id="1", provider="test1", name="Artist A")]
    )
    album_b.artists = media_items.UniqueList(
        [
            media_items.ItemMapping(item_id="1", provider="test1", name="Artist A"),
            media_items.ItemMapping(item_id="2", provider="test1", name="Artist B"),
        ]
    )
    assert compare.compare_album(album_a, album_b) is True
    # fail on partial artist match in strict mode
    album_b.artists = media_items.UniqueList(
        [
            media_items.ItemMapping(item_id="2", provider="test1", name="Artist B"),
            media_items.ItemMapping(item_id="1", provider="test1", name="Artist A"),
        ]
    )
    assert compare.compare_album(album_a, album_b) is False
    # partial artist match is allowed in non-strict mode
    assert compare.compare_album(album_a, album_b, False) is True


def test_compare_album_barcode_requires_corroboration() -> None:
    """A shared retail barcode only overrides year when album identity also agrees."""
    artist_a = media_items.Artist(
        item_id="1",
        provider="test1",
        name="Artist A",
        provider_mappings={
            media_items.ProviderMapping(
                item_id="1", provider_domain="test", provider_instance="test1"
            )
        },
    )
    artist_b = media_items.Artist(
        item_id="2",
        provider="test2",
        name="Artist A",
        provider_mappings={
            media_items.ProviderMapping(
                item_id="2", provider_domain="test", provider_instance="test2"
            )
        },
    )
    barcode_a = (ExternalID.BARCODE, "000724354283857")
    barcode_b = (ExternalID.BARCODE, "0724354283857")
    album_a = media_items.Album(
        item_id="1",
        provider="test1",
        name="#1",
        year=2001,
        artists=media_items.UniqueList([artist_a]),
        external_ids={barcode_a},
        provider_mappings={
            media_items.ProviderMapping(
                item_id="1", provider_domain="test", provider_instance="test1"
            )
        },
    )
    album_b = media_items.Album(
        item_id="2",
        provider="test2",
        name="#1",
        year=2002,
        artists=media_items.UniqueList([artist_b]),
        external_ids={barcode_b},
        provider_mappings={
            media_items.ProviderMapping(
                item_id="2", provider_domain="test", provider_instance="test2"
            )
        },
    )

    assert compare.compare_album(album_a, album_b) is True

    album_b.name = "Different Album"
    assert compare.compare_album(album_a, album_b) is False
    album_b.name = album_a.name
    album_b.artists[0].name = "Different Artist"
    assert compare.compare_album(album_a, album_b) is False


def test_compare_album_evidence_barcode_never_matches_unrelated_titles() -> None:
    """A shared canonical barcode alone must never match unrelated title/artist data."""
    barcode = {(ExternalID.BARCODE, "0724354283857")}
    album_a = _album(name="Album A", external_ids=barcode)
    album_b = _album(item_id="2", provider="test2", name="Completely Different Album", year=1999)
    album_b.external_ids = barcode

    assert compare.compare_album_evidence(album_a, album_b) == compare.AlbumMatchEvidence.NO_MATCH
    assert compare.compare_album(album_a, album_b) is False


def test_compare_album_evidence_name_hyphen_spacing_drift_matches() -> None:
    """Punctuation/spacing drift around a title's hyphenation does not block a match."""
    album_a = _album(name="All Change - - EP")
    album_b = _album(item_id="2", provider="test2", name="All Change - EP")

    assert compare.compare_album_evidence(album_a, album_b) == compare.AlbumMatchEvidence.MATCH
    assert compare.compare_album(album_a, album_b) is True


def test_compare_album_evidence_name_accent_and_apostrophe_variants_match() -> None:
    """Diacritic and apostrophe drift in an otherwise identical title does not block a match."""
    album_a = _album(name="Am\u00e9lie Soundtrack")
    album_b = _album(item_id="2", provider="test2", name="Amelie Soundtrack")
    assert compare.compare_album_evidence(album_a, album_b) == compare.AlbumMatchEvidence.MATCH

    album_a = _album(name="Guns N' Roses Live")
    album_b = _album(item_id="2", provider="test2", name="Guns N Roses Live")
    assert compare.compare_album_evidence(album_a, album_b) == compare.AlbumMatchEvidence.MATCH


def test_compare_album_evidence_name_hyphenation_vs_spacing_matches() -> None:
    """A hyphenated title matches its spaced or joined spelling across providers."""
    album_a = _album(name="Trans-Europe Express")
    album_b = _album(item_id="2", provider="test2", name="Trans Europe Express")
    assert compare.compare_album_evidence(album_a, album_b) == compare.AlbumMatchEvidence.MATCH

    album_a = _album(name="Hell - On")
    album_b = _album(item_id="2", provider="test2", name="Hell-On")
    assert compare.compare_album_evidence(album_a, album_b) == compare.AlbumMatchEvidence.MATCH


def test_compare_album_evidence_name_retail_suffix_stripped() -> None:
    """An Apple-style ' - EP'/' - Single' retail suffix does not block a match."""
    for suffix in (" - EP", " - Single", " \u2013 EP"):
        album_a = _album(name=f"Album A{suffix}")
        album_b = _album(item_id="2", provider="test2", name="Album A")
        assert (
            compare.compare_album_evidence(album_a, album_b) == compare.AlbumMatchEvidence.MATCH
        ), suffix


def test_compare_album_evidence_punctuation_only_title_whitespace_drift_matches() -> None:
    """Whitespace drift within a punctuation-only title does not block a match."""
    album_a = _album(name="( )")
    album_b = _album(item_id="2", provider="test2", name="()")
    assert compare.compare_album_evidence(album_a, album_b) == compare.AlbumMatchEvidence.MATCH

    # the retail suffix is also ignored when the remaining title is punctuation-only
    album_a = _album(name="... - EP")
    album_b = _album(item_id="2", provider="test2", name="...")
    assert compare.compare_album_evidence(album_a, album_b) == compare.AlbumMatchEvidence.MATCH

    # different punctuation-only titles are still different albums
    album_a = _album(name="\u00f7")
    album_b = _album(item_id="2", provider="test2", name="=")
    assert compare.compare_album_evidence(album_a, album_b) == compare.AlbumMatchEvidence.NO_MATCH


def test_compare_album_evidence_unrelated_punctuation_only_titles_stay_distinct() -> None:
    """Two different titles that both normalize to nothing must not be treated as equal."""
    album_a = _album(name="...")
    album_b = _album(item_id="2", provider="test2", name="!!!")

    assert compare.compare_album_evidence(album_a, album_b) == compare.AlbumMatchEvidence.NO_MATCH
    assert compare.compare_album(album_a, album_b) is False


def test_compare_album_evidence_mb_releasegroup_not_sufficient_alone() -> None:
    """A shared MusicBrainz release-group is family evidence only, never sufficient identity."""
    releasegroup = {(ExternalID.MB_RELEASEGROUP, "11111111-1111-1111-1111-111111111111")}
    original = _album(name="Album A", version="Original Mix", external_ids=releasegroup)
    remaster = _album(item_id="2", provider="test2", name="Album A", version="Live")
    remaster.external_ids = releasegroup

    # a genuine edition conflict (studio mix vs. live) must not auto-merge on releasegroup alone
    assert compare.compare_album_evidence(original, remaster) == compare.AlbumMatchEvidence.NO_MATCH
    assert compare.compare_album(original, remaster) is False


def test_compare_album_evidence_ambiguous_version_without_tracks_is_insufficient() -> None:
    """An ambiguous subset/superset edition wording without tracklists stays undecided."""
    base_item = _album(version="2022 Remaster")
    compare_item = _album(item_id="2", provider="test2", version="Deluxe 2022 Remaster")

    assert (
        compare.compare_album_evidence(base_item, compare_item)
        == compare.AlbumMatchEvidence.INSUFFICIENT
    )
    # the compatibility wrapper stays conservative and does not merge on insufficient evidence
    assert compare.compare_album(base_item, compare_item) is False


def test_compare_album_evidence_barcode_resolves_edition_ambiguity() -> None:
    """A shared retail barcode resolves ambiguous edition wording into a match."""
    barcode = {(ExternalID.BARCODE, "0724354283857")}
    base_item = _album(version="2022 Remaster", external_ids=barcode)
    compare_item = _album(item_id="2", provider="test2", version="Deluxe 2022 Remaster")
    compare_item.external_ids = barcode

    assert (
        compare.compare_album_evidence(base_item, compare_item) == compare.AlbumMatchEvidence.MATCH
    )

    # the same corroboration applies when comparing against a minimized ItemMapping
    mapping = media_items.ItemMapping(
        item_id="3",
        provider="test3",
        name="Album A",
        version="Deluxe 2022 Remaster",
        external_ids=barcode,
    )
    assert (
        compare.compare_album_evidence(base_item, mapping, strict=False)
        == compare.AlbumMatchEvidence.MATCH
    )

    # a conflicting tracklist fingerprint still overrides the barcode corroboration
    assert (
        compare.compare_album_evidence(
            base_item, compare_item, base_tracks=_tracklist(8), compare_tracks=_tracklist(14)
        )
        == compare.AlbumMatchEvidence.NO_MATCH
    )


def test_compare_album_evidence_subset_wording_with_recording_conflict_is_no_match() -> None:
    """A subset edition wording that adds a recording-altering qualifier must not merge."""
    base_item = _album(version="Deluxe")
    compare_item = _album(item_id="2", provider="test2", version="Deluxe Karaoke Edition")

    assert (
        compare.compare_album_evidence(base_item, compare_item)
        == compare.AlbumMatchEvidence.NO_MATCH
    )
    assert compare.compare_album(base_item, compare_item) is False


def test_compare_album_evidence_recording_conflict_word_anywhere_is_no_match() -> None:
    """A recording-changing qualifier stays NO_MATCH even when it's shared, not differing."""
    # "live" is shared by both sides here (not the token that differs), but a bare
    # "Live" tag next to a specific "Live at <venue>" tag is still not safe to merge
    base_item = _album(version="Live")
    compare_item = _album(item_id="2", provider="test2", version="Live at Wembley")

    assert (
        compare.compare_album_evidence(base_item, compare_item)
        == compare.AlbumMatchEvidence.NO_MATCH
    )
    assert compare.compare_album(base_item, compare_item) is False


def test_compare_album_evidence_equivalent_location_wording_matches() -> None:
    """Reordered wording that differs only by a harmless connector word ('at') still matches."""
    base_item = _album(version="Live at Wembley")
    compare_item = _album(item_id="2", provider="test2", version="Wembley Live")

    assert (
        compare.compare_album_evidence(base_item, compare_item) == compare.AlbumMatchEvidence.MATCH
    )
    assert compare.compare_album(base_item, compare_item) is True


def test_compare_album_evidence_blank_vs_remaster_version_is_insufficient() -> None:
    """A blank version next to a tagged remaster is undecided, not a proven conflict."""
    base_item = _album(version="")
    compare_item = _album(item_id="2", provider="test2", version="Remaster")

    assert (
        compare.compare_album_evidence(base_item, compare_item)
        == compare.AlbumMatchEvidence.INSUFFICIENT
    )
    # the compatibility wrapper stays conservative and does not merge on insufficient evidence
    assert compare.compare_album(base_item, compare_item) is False


def test_compare_album_evidence_blank_vs_remaster_resolves_with_fingerprint() -> None:
    """A blank-vs-remaster version gap is resolved once track fingerprints are supplied."""
    base_item = _album(version="")
    compare_item = _album(item_id="2", provider="test2", version="Remaster")
    matching_tracks = _tracklist(10)

    assert (
        compare.compare_album_evidence(
            base_item, compare_item, base_tracks=matching_tracks, compare_tracks=matching_tracks
        )
        == compare.AlbumMatchEvidence.MATCH
    )

    conflicting_tracks = _tracklist(10, isrc_prefix="USRC28718")
    assert (
        compare.compare_album_evidence(
            base_item,
            compare_item,
            base_tracks=matching_tracks,
            compare_tracks=conflicting_tracks,
        )
        == compare.AlbumMatchEvidence.NO_MATCH
    )


@pytest.mark.parametrize(
    "conflict_version",
    ["Live", "Remix", "Karaoke Edition", "Instrumental", "Acoustic Version", "Demo", "Cover"],
)
def test_compare_album_evidence_blank_vs_recording_conflict_is_no_match(
    conflict_version: str,
) -> None:
    """
    A blank version next to a recording-changing qualifier is a proven conflict.

    Unlike a blank version next to plain packaging wording (remaster, deluxe, ...), a
    missing version tag can never explain away a recording-altering qualifier on the
    other side, so this must not be left for a tracklist to resolve.
    """
    base_item = _album(version="")
    compare_item = _album(item_id="2", provider="test2", version=conflict_version)

    assert (
        compare.compare_album_evidence(base_item, compare_item)
        == compare.AlbumMatchEvidence.NO_MATCH
    )
    assert compare.compare_album(base_item, compare_item) is False


@pytest.mark.parametrize("packaging_version", ["Remaster", "Deluxe Edition", "Anniversary Edition"])
def test_compare_album_evidence_blank_vs_packaging_edition_is_insufficient(
    packaging_version: str,
) -> None:
    """A blank version next to plain packaging wording stays undecided, not a proven conflict."""
    base_item = _album(version="")
    compare_item = _album(item_id="2", provider="test2", version=packaging_version)

    assert (
        compare.compare_album_evidence(base_item, compare_item)
        == compare.AlbumMatchEvidence.INSUFFICIENT
    )
    # the compatibility wrapper stays conservative and does not merge on insufficient evidence
    assert compare.compare_album(base_item, compare_item) is False


def test_compare_album_evidence_resolves_with_matching_fingerprint() -> None:
    """Ambiguous edition wording resolves to MATCH once track fingerprints agree."""
    base_item = _album(version="2022 Remaster")
    compare_item = _album(item_id="2", provider="test2", version="Deluxe 2022 Remaster")
    base_tracks = _tracklist(14)
    compare_tracks = _tracklist(14)

    assert (
        compare.compare_album_evidence(
            base_item, compare_item, base_tracks=base_tracks, compare_tracks=compare_tracks
        )
        == compare.AlbumMatchEvidence.MATCH
    )


def test_compare_album_evidence_resolves_with_conflicting_fingerprint() -> None:
    """Ambiguous edition wording resolves to NO_MATCH once track fingerprints disagree."""
    base_item = _album(version="2022 Remaster")
    compare_item = _album(item_id="2", provider="test2", version="Deluxe 2022 Remaster")
    base_tracks = _tracklist(14, isrc_prefix="USRC17607")
    compare_tracks = _tracklist(14, isrc_prefix="USRC28718")

    assert (
        compare.compare_album_evidence(
            base_item, compare_item, base_tracks=base_tracks, compare_tracks=compare_tracks
        )
        == compare.AlbumMatchEvidence.NO_MATCH
    )


def test_compare_album_evidence_ordinary_and_deluxe_track_counts_stay_distinct() -> None:
    """An ordinary 8-track album and a 14-track edition must not be merged via fingerprints."""
    base_item = _album(version="2022 Remaster")
    compare_item = _album(item_id="2", provider="test2", version="Deluxe 2022 Remaster")
    base_tracks = _tracklist(8)
    compare_tracks = _tracklist(14)

    assert (
        compare.compare_album_evidence(
            base_item, compare_item, base_tracks=base_tracks, compare_tracks=compare_tracks
        )
        == compare.AlbumMatchEvidence.NO_MATCH
    )


def test_compare_album_evidence_fingerprint_overrides_matching_metadata() -> None:
    """A conflicting tracklist overrides an otherwise nominally-matching album."""
    base_item = _album(version="", year=2020)
    compare_item = _album(item_id="2", provider="test2", version="", year=2020)
    base_tracks = _tracklist(8)
    compare_tracks = _tracklist(14)

    assert (
        compare.compare_album_evidence(
            base_item, compare_item, base_tracks=base_tracks, compare_tracks=compare_tracks
        )
        == compare.AlbumMatchEvidence.NO_MATCH
    )


def test_compare_album_track_fingerprint_matching_isrc_and_duration() -> None:
    """Equal ISRC, title and duration at every position is a confident match."""
    base_tracks = _tracklist(3)
    compare_tracks = _tracklist(3)

    assert (
        compare.compare_album_track_fingerprint(base_tracks, compare_tracks)
        == compare.AlbumMatchEvidence.MATCH
    )


def test_compare_album_track_fingerprint_shared_isrc_without_duration_is_insufficient() -> None:
    """A shared ISRC alone cannot confirm a match if neither side has a duration."""
    base_tracks = [_track("1", track_number=1, isrc="USRC17607839", duration=0)]
    compare_tracks = [_track("2", track_number=1, isrc="USRC17607839", duration=0)]

    assert (
        compare.compare_album_track_fingerprint(base_tracks, compare_tracks)
        == compare.AlbumMatchEvidence.INSUFFICIENT
    )


def test_compare_album_track_fingerprint_conflicting_isrc() -> None:
    """Conflicting ISRCs at the same position indicate a different recording/remaster."""
    base_tracks = [_track("1", track_number=1, isrc="USRC17607839")]
    compare_tracks = [_track("2", track_number=1, isrc="USRC28718001")]

    assert (
        compare.compare_album_track_fingerprint(base_tracks, compare_tracks)
        == compare.AlbumMatchEvidence.NO_MATCH
    )


def test_compare_album_track_fingerprint_invalid_isrc_falls_back_to_title_duration() -> None:
    """A structurally invalid ISRC is ignored, not treated as identity evidence."""
    base_tracks = [_track("1", track_number=1, name="Track One", duration=200, isrc="NOTANISRC")]
    matching_compare_tracks = [
        _track("2", track_number=1, name="Track One", duration=200, isrc="ALSOINVALID")
    ]

    # both sides tag an (invalid) ISRC, but neither is structurally valid, so the
    # comparison falls back to title/duration and still finds a match
    assert (
        compare.compare_album_track_fingerprint(base_tracks, matching_compare_tracks)
        == compare.AlbumMatchEvidence.MATCH
    )

    # a genuine title conflict is still caught once the invalid ISRCs are ignored
    conflicting_compare_tracks = [
        _track("2", track_number=1, name="Different Track", duration=200, isrc="ALSOINVALID")
    ]
    assert (
        compare.compare_album_track_fingerprint(base_tracks, conflicting_compare_tracks)
        == compare.AlbumMatchEvidence.NO_MATCH
    )


def test_compare_album_track_fingerprint_title_duration_fallback() -> None:
    """Without ISRCs, matching normalized title/version and a tight duration match."""
    base_tracks = [_track("1", track_number=1, name="Track One", duration=200)]
    compare_tracks = [_track("2", track_number=1, name="Track One", duration=201)]

    assert (
        compare.compare_album_track_fingerprint(base_tracks, compare_tracks)
        == compare.AlbumMatchEvidence.MATCH
    )
    # a genuine duration conflict without ISRCs cannot be treated as the same recording
    compare_tracks = [_track("2", track_number=1, name="Track One", duration=260)]
    assert (
        compare.compare_album_track_fingerprint(base_tracks, compare_tracks)
        == compare.AlbumMatchEvidence.NO_MATCH
    )


def test_compare_album_track_fingerprint_sparse_tracks_are_insufficient() -> None:
    """Tracks with no ISRC, title, or duration cannot support a confident decision."""
    base_tracks = [_track("1", track_number=1, name="", duration=0)]
    compare_tracks = [_track("2", track_number=1, name="", duration=0)]

    assert (
        compare.compare_album_track_fingerprint(base_tracks, compare_tracks)
        == compare.AlbumMatchEvidence.INSUFFICIENT
    )
    # no tracklists supplied at all is equally undecided
    assert (
        compare.compare_album_track_fingerprint(None, None)
        == compare.AlbumMatchEvidence.INSUFFICIENT
    )
    assert (
        compare.compare_album_track_fingerprint([], []) == compare.AlbumMatchEvidence.INSUFFICIENT
    )


def test_compare_album_track_fingerprint_unknown_disc_layout_vs_multi_disc_is_insufficient() -> (
    None
):
    """An unknown (no disc numbers reported at all) layout can't be shape-compared."""
    # a provider that never reports disc numbers: assumed single disc by omission
    base_tracks = [_track(str(n), disc_number=0, track_number=n) for n in range(1, 15)]
    # the other side is a genuine 2-disc release (8 + 6 tracks)
    compare_tracks = [_track(f"d1-{n}", disc_number=1, track_number=n) for n in range(1, 9)] + [
        _track(f"d2-{n}", disc_number=2, track_number=n) for n in range(1, 7)
    ]

    # assuming disc 1 for the unknown side would falsely look like a shape conflict
    # (or, worse, a false match): neither is safe, so this must stay undecided
    assert (
        compare.compare_album_track_fingerprint(base_tracks, compare_tracks)
        == compare.AlbumMatchEvidence.INSUFFICIENT
    )


def test_album_tracks_have_positions() -> None:
    """A trustworthy disc/track layout is recognized, an ambiguous one is not."""
    assert compare.album_tracks_have_positions(_tracklist(10)) is True
    assert compare.album_tracks_have_positions(None) is False
    assert compare.album_tracks_have_positions([]) is False
    # a missing track number makes the layout untrustworthy
    assert compare.album_tracks_have_positions([_track("1", track_number=0)]) is False
    # a missing disc number is treated as unknown, not silently assumed to be disc 1
    assert compare.album_tracks_have_positions([_track("1", disc_number=0)]) is False
    # a duplicate position cannot be trusted either
    duplicate = [_track("1", track_number=1), _track("2", track_number=1)]
    assert compare.album_tracks_have_positions(duplicate) is False


def test_compare_external_ids_checks_all_unique_values() -> None:
    """One mismatching unique identifier does not hide another matching value."""
    base_ids = {
        (ExternalID.MB_ALBUM, "11111111-1111-1111-1111-111111111111"),
        (ExternalID.MB_ALBUM, "22222222-2222-2222-2222-222222222222"),
    }
    compare_ids = {(ExternalID.MB_ALBUM, "22222222-2222-2222-2222-222222222222")}

    assert compare.compare_external_ids(base_ids, compare_ids, ExternalID.MB_ALBUM) is True


def test_compare_external_ids_truncated_barcode_matches_full_value() -> None:
    """A truncated 13-digit GTIN-14 (Qobuz) matches another provider's full barcode."""
    base_ids = {(ExternalID.BARCODE, "0060252758365")}
    compare_ids = {(ExternalID.BARCODE, "00602527583655")}

    assert compare.compare_external_ids(base_ids, compare_ids, ExternalID.BARCODE) is True


def test_compare_track() -> None:  # noqa: PLR0915
    """Test track comparison."""
    track_a = media_items.Track(
        item_id="1",
        provider="test1",
        name="Track A",
        provider_mappings={
            media_items.ProviderMapping(
                item_id="1", provider_domain="test", provider_instance="test1"
            )
        },
        artists=media_items.UniqueList(
            [
                media_items.Artist(
                    item_id="1",
                    provider="test1",
                    name="Artist A",
                    provider_mappings={
                        media_items.ProviderMapping(
                            item_id="1", provider_domain="test", provider_instance="test1"
                        )
                    },
                )
            ]
        ),
    )
    track_b = media_items.Track(
        item_id="1",
        provider="test2",
        name="Track A",
        provider_mappings={
            media_items.ProviderMapping(
                item_id="2", provider_domain="test", provider_instance="test2"
            )
        },
        artists=media_items.UniqueList(
            [
                media_items.Artist(
                    item_id="1",
                    provider="test1",
                    name="Artist A",
                    provider_mappings={
                        media_items.ProviderMapping(
                            item_id="1", provider_domain="test", provider_instance="test1"
                        )
                    },
                )
            ]
        ),
    )
    # test match on name match
    assert compare.compare_track(track_a, track_b) is True
    # test match on name mismatch
    track_b.name = "Track B"
    assert compare.compare_track(track_a, track_b) is False
    # test on version mismatch
    track_b.name = track_a.name
    track_b.version = "Deluxe"
    assert compare.compare_track(track_a, track_b) is False
    track_b.version = "Remix"
    assert compare.compare_track(track_a, track_b) is False
    # test on version mismatch
    track_b.name = track_a.name
    track_a.version = ""
    track_b.version = "Remaster"
    assert compare.compare_track(track_a, track_b) is False
    track_b.version = "Remix"
    assert compare.compare_track(track_a, track_b) is False
    # test on version match
    track_b.name = track_a.name
    track_a.version = "Deluxe"
    track_b.version = "Deluxe Edition"
    assert compare.compare_track(track_a, track_b) is True
    # test on exact item_id match
    track_b.item_id = track_a.item_id
    track_b.provider = track_a.provider
    assert compare.compare_track(track_a, track_b) is True
    # test on external id match
    track_b.name = "Track B"
    track_b.item_id = "2"
    track_b.provider = "test2"
    track_a.external_ids = {(ExternalID.MB_RECORDING, "123")}
    track_b.external_ids = track_a.external_ids
    assert compare.compare_track(track_a, track_b) is True
    # test on external id mismatch
    track_b.name = track_a.name
    track_b.external_ids = {(ExternalID.MB_RECORDING, "1234")}
    assert compare.compare_track(track_a, track_b) is False
    track_a.external_ids = set()
    track_b.external_ids = set()
    # fail on artist mismatch
    track_a.artists = media_items.UniqueList(
        [media_items.ItemMapping(item_id="1", provider="test1", name="Artist A")]
    )
    track_b.artists = media_items.UniqueList(
        [media_items.ItemMapping(item_id="2", provider="test1", name="Artist B")]
    )
    assert compare.compare_track(track_a, track_b) is False
    # pass on partial artist match (if first artist matches)
    track_a.artists = media_items.UniqueList(
        [media_items.ItemMapping(item_id="1", provider="test1", name="Artist A")]
    )
    track_b.artists = media_items.UniqueList(
        [
            media_items.ItemMapping(item_id="1", provider="test1", name="Artist A"),
            media_items.ItemMapping(item_id="2", provider="test1", name="Artist B"),
        ]
    )
    assert compare.compare_track(track_a, track_b) is True
    # fail on partial artist match in strict mode
    track_b.artists = media_items.UniqueList(
        [
            media_items.ItemMapping(item_id="2", provider="test1", name="Artist B"),
            media_items.ItemMapping(item_id="1", provider="test1", name="Artist A"),
        ]
    )
    assert compare.compare_track(track_a, track_b) is False
    # partial artist match is allowed in non-strict mode
    assert compare.compare_track(track_a, track_b, False) is True
    track_b.artists = track_a.artists
    # fail on album mismatch
    track_a.album = media_items.ItemMapping(item_id="1", provider="test1", name="Album A")
    track_b.album = media_items.ItemMapping(item_id="2", provider="test1", name="Album B")
    assert compare.compare_track(track_a, track_b) is False
    # pass on exact album(track) match (regardless duration)
    track_b.album = track_a.album
    track_a.disc_number = 1
    track_a.track_number = 1
    track_b.disc_number = track_a.disc_number
    track_b.track_number = track_a.track_number
    track_a.duration = 300
    track_b.duration = 310
    assert compare.compare_track(track_a, track_b) is True
    # pass on album(track) mismatch
    track_b.album = track_a.album
    track_a.disc_number = 1
    track_a.track_number = 1
    track_b.disc_number = track_a.disc_number
    track_b.track_number = 2
    track_b.duration = track_a.duration
    assert compare.compare_track(track_a, track_b) is False
    # test special case - ISRC match but MusicBrainz ID mismatch
    # this can happen for some classical music albums
    track_a.external_ids = {
        (ExternalID.ISRC, "123"),
        (ExternalID.MB_RECORDING, "abc"),
    }
    track_b.external_ids = {
        (ExternalID.ISRC, "123"),
        (ExternalID.MB_RECORDING, "abcd"),
    }
    assert compare.compare_track(track_a, track_b) is False
    # test multi-disc: same album, same external IDs, different disc numbers should NOT match
    track_a.external_ids = {(ExternalID.MB_RECORDING, "same-recording-id")}
    track_b.external_ids = {(ExternalID.MB_RECORDING, "same-recording-id")}
    track_a.album = media_items.ItemMapping(item_id="1", provider="test1", name="Album A")
    track_b.album = media_items.ItemMapping(item_id="1", provider="test1", name="Album A")
    track_a.disc_number = 1
    track_b.disc_number = 2
    track_a.track_number = 3
    track_b.track_number = 3
    assert compare.compare_track(track_a, track_b) is False
    # same disc number should still match via external ID
    track_b.disc_number = 1
    assert compare.compare_track(track_a, track_b) is True
    # different disc but different albums should still match via external ID
    track_b.disc_number = 2
    track_b.album = media_items.ItemMapping(item_id="2", provider="test1", name="Album B")
    assert compare.compare_track(track_a, track_b) is True


def test_compare_track_missing_disc_number_assumes_disc_one() -> None:
    """A track without a disc number tag still makes the exact albumtrack match on disc 1."""

    def _albumtrack(item_id: str, provider: str, disc_number: int) -> media_items.Track:
        return media_items.Track(
            item_id=item_id,
            provider=provider,
            name="Track A",
            duration=300,
            disc_number=disc_number,
            track_number=5,
            artists=media_items.UniqueList(
                [media_items.ItemMapping(item_id="1", provider=provider, name="Artist A")]
            ),
            album=media_items.ItemMapping(item_id="1", provider=provider, name="Album A"),
            provider_mappings={
                media_items.ProviderMapping(
                    item_id=item_id, provider_domain=provider, provider_instance=provider
                )
            },
        )

    untagged = _albumtrack("1", "test1", disc_number=0)
    disc_one = _albumtrack("2", "test2", disc_number=1)
    # durations differ beyond every fallback tolerance: only the albumtrack path can match
    disc_one.duration = 320
    assert compare.compare_track(untagged, disc_one) is True

    # an unknown disc number only ever assumes disc 1, never a higher disc
    disc_two = _albumtrack("3", "test2", disc_number=2)
    disc_two.duration = 320
    assert compare.compare_track(untagged, disc_two) is False


def test_compare_strings_accent_drift_matches() -> None:
    """Diacritics do not distinguish two otherwise identical names."""
    assert compare.compare_strings("Sigur R\u00f3s", "Sigur Ros") is True
    assert compare.compare_strings("C\u00e9line Dion", "Celine Dion") is True
    assert compare.compare_strings("Bj\u00f6rk", "Bjork") is True


def test_compare_strings_punctuation_and_spacing_drift_matches() -> None:
    """Apostrophe, punctuation and spacing drift does not distinguish two names."""
    assert compare.compare_strings("Jane's Addiction", "Jane\u2019s Addiction") is True
    assert compare.compare_strings("A.C. Newman", "AC Newman") is True
    assert compare.compare_strings("All-4-One", "All4One") is True


def test_compare_strings_symbol_only_names_stay_distinct() -> None:
    """Names that normalize to nothing are compared raw, so only real duplicates match."""
    assert compare.compare_strings("!!!", "...") is False
    # spacing drift within such a name is still the same name
    assert compare.compare_strings("( )", "()") is True
    # a name that normalizes to nothing is never the same as one that doesn't
    assert compare.compare_strings("!!!", "Chk Chk Chk") is False


def test_compare_strings_normalization_does_not_leak_into_fuzzy() -> None:
    """The non-strict path keeps its own (more lenient) verdicts."""
    assert compare.compare_strings("!!!", "...", strict=False) is True


def test_compare_strings_case_insensitive_fuzzy() -> None:
    """Test that non-strict fuzzy matching is fully case-insensitive."""
    # These differ slightly ("Feat." vs "FT.") so create_safe_string won't match,
    # falling through to SequenceMatcher which must compare both strings lowered.
    assert compare.compare_strings("Track Feat. John", "TRACK FT. JOHN", strict=False) is True


def test_loose_compare_strings_containment_both_directions() -> None:
    """Partial containment matches regardless of which side has the extra wording."""
    assert compare.loose_compare_strings("Some Track", "Some Track (Acoustic)") is True
    assert compare.loose_compare_strings("Some Track (Acoustic)", "Some Track") is True


def test_compare_radio() -> None:
    """Test the radio compare helper."""

    def _radio(
        item_id: str, provider: str, name: str, *, is_dynamic: bool = False
    ) -> media_items.Radio:
        return media_items.Radio(
            item_id=item_id,
            provider=provider,
            name=name,
            is_dynamic=is_dynamic,
            provider_mappings={
                media_items.ProviderMapping(
                    item_id=item_id, provider_domain=provider, provider_instance=provider
                )
            },
        )

    live_a = _radio("a", "tunein", "Chill Vibes")
    live_b = _radio("b", "radiobrowser", "Chill Vibes")
    # a live station is matched across providers on its name
    assert compare.compare_radio(live_a, live_b) is True

    station = _radio("c", "pandora", "Chill Vibes", is_dynamic=True)
    # a dynamic station only exists on its own provider, so the name is not enough
    assert compare.compare_radio(station, live_a) is False
    assert compare.compare_radio(live_a, station) is False
    # ... but it is still recognised as itself
    same = _radio("c", "pandora", "Chill Vibes", is_dynamic=True)
    assert compare.compare_radio(station, same) is True
