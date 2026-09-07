"""Tests for mediaitem compare helper functions."""

import sqlite3

import pytest
from music_assistant_models import media_items
from music_assistant_models.enums import ExternalID, MediaType

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


def _provider_track(
    item_id: str,
    provider: str,
    *,
    name: str = "Track",
    version: str = "",
    duration: int = 200,
    album_name: str = "Album",
    artist_names: tuple[str, ...] = ("Artist A",),
    external_ids: set[tuple[ExternalID, str]] | None = None,
    album_external_ids: set[tuple[ExternalID, str]] | None = None,
) -> media_items.Track:
    """Build a provider track for confidence comparisons."""
    album = _album(
        item_id=f"album-{item_id}",
        provider=provider,
        name=album_name,
        external_ids=album_external_ids,
    )
    return media_items.Track(
        item_id=item_id,
        provider=provider,
        name=name,
        version=version,
        duration=duration,
        disc_number=1,
        track_number=1,
        artists=media_items.UniqueList(
            [
                media_items.ItemMapping(
                    item_id=f"artist-{index}",
                    provider=provider,
                    name=artist_name,
                    media_type=MediaType.ARTIST,
                )
                for index, artist_name in enumerate(artist_names)
            ]
        ),
        album=album,
        external_ids=external_ids or set(),
        provider_mappings={
            media_items.ProviderMapping(
                item_id=item_id,
                provider_domain=provider,
                provider_instance=provider,
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
    """A retail suffix, however a provider sets it off, does not block a match."""
    for suffix in (
        " - EP",
        " -EP",
        " \u2013 EP",
        " (EP)",
        " [EP]",
        " - Single",
        " (Single)",
    ):
        album_a = _album(name=f"Album A{suffix}")
        album_b = _album(item_id="2", provider="test2", name="Album A")
        assert (
            compare.compare_album_evidence(album_a, album_b) == compare.AlbumMatchEvidence.MATCH
        ), suffix


def test_compare_album_evidence_retail_suffix_spelled_differently_matches() -> None:
    """Two providers setting off the same retail suffix differently name the same album."""
    for spelling in (" -EP", " \u2013 EP", " (EP)", " [EP]"):
        album_a = _album(name="Album A - EP")
        album_b = _album(item_id="2", provider="test2", name=f"Album A{spelling}")
        assert (
            compare.compare_album_evidence(album_a, album_b) == compare.AlbumMatchEvidence.MATCH
        ), spelling


def test_album_retail_suffix_sql_match_needs_the_suffix_set_off() -> None:
    """The pre-filter condition sees every separator style, but not an ordinary title."""
    connection = sqlite3.connect(":memory:")

    def relates(name: str, suffix_key: str) -> bool:
        condition = compare.album_retail_suffix_sql_match("?", suffix_key)
        return bool(connection.execute(f"SELECT {condition}", (name,)).fetchone()[0])

    for name in (
        "Album A - EP",
        "Album A -EP",
        "Album A \u2013 EP",
        "Album A \u2014EP",
        "Album A (EP)",
        "Album A [EP]",
        "Album A - ep",
        # a bare trailing word is related here on purpose and refused by the comparison
        "The SL2 EP",
    ):
        assert relates(name, "ep"), name
    for name in ("Step", "Sleep", "EP"):
        assert not relates(name, "ep"), name

    assert relates("Think of You - Single", "single")
    assert relates("Brazen 'Weep' (CD1) (single)", "single")
    for name in ("Singles", "Every Single Day"):
        assert not relates(name, "single"), name


def test_compare_album_evidence_bare_suffix_word_stays_part_of_the_title() -> None:
    """A trailing suffix word that is not set off belongs to the title itself."""
    for name in ("The SL2 EP", "Saturday Night Single"):
        album_a = _album(name=name)
        album_b = _album(item_id="2", provider="test2", name=name.rsplit(" ", 1)[0])
        assert (
            compare.compare_album_evidence(album_a, album_b) == compare.AlbumMatchEvidence.NO_MATCH
        ), name

    # two providers spelling that same title identically still match
    album_a = _album(name="The SL2 EP")
    album_b = _album(item_id="2", provider="test2", name="The SL2 EP")
    assert compare.compare_album_evidence(album_a, album_b) == compare.AlbumMatchEvidence.MATCH


def test_compare_album_evidence_bordering_symbol_marks_a_different_release() -> None:
    """A bonus edition marked by a symbol at either end of the title is a release of its own."""
    for name in ("MOTOMAMI +", "MOTOMAMI+", "+ MOTOMAMI", "+MOTOMAMI"):
        album_a = _album(name="MOTOMAMI", year=2022)
        album_b = _album(item_id="2", provider="test2", name=name, year=2022)

        assert (
            compare.compare_album_evidence(album_a, album_b) == compare.AlbumMatchEvidence.NO_MATCH
        ), name
        assert compare.compare_album(album_a, album_b) is False, name

    # how the symbol is spaced is still formatting the two providers may differ on
    album_a = _album(name="MOTOMAMI +", year=2022)
    album_b = _album(item_id="2", provider="test2", name="MOTOMAMI+", year=2022)
    assert compare.compare_album_evidence(album_a, album_b) == compare.AlbumMatchEvidence.MATCH


def test_compare_album_evidence_symbol_standing_in_for_letters_is_still_drift() -> None:
    """A symbol used to spell a letter is folded away, so both spellings still match."""
    for stylized, plain in (("bbno$", "bbno"), ("\u0060Round Midnight", "'Round Midnight")):
        album_a = _album(name=stylized)
        album_b = _album(item_id="2", provider="test2", name=plain)
        assert (
            compare.compare_album_evidence(album_a, album_b) == compare.AlbumMatchEvidence.MATCH
        ), stylized

    # a mathematical symbol anyascii spells out is counted once, not twice
    album_a = _album(name="Partial \u2202")
    album_b = _album(item_id="2", provider="test2", name="Partial d")
    assert compare.compare_album_evidence(album_a, album_b) == compare.AlbumMatchEvidence.MATCH


def test_compare_album_evidence_symbol_only_titles_identify_their_own_album() -> None:
    """A symbol-titled album (Ed Sheeran's '+', '=', '÷') matches only its own spelling."""
    for name in ("+", "=", "÷", "\u00d7"):
        assert (
            compare.compare_album_evidence(
                _album(name=name), _album(item_id="2", provider="test2", name=f"{name} ")
            )
            == compare.AlbumMatchEvidence.MATCH
        ), name
    # a symbol-only title is decided on its complete raw spelling, punctuation included
    for base_name, compare_name in (
        ("+", "="),
        ("+", "÷"),
        ("=", "÷"),
        ("\u00d7", "÷"),
        ("+", "+!"),
    ):
        assert (
            compare.compare_album_evidence(
                _album(name=base_name), _album(item_id="2", provider="test2", name=compare_name)
            )
            == compare.AlbumMatchEvidence.NO_MATCH
        ), (base_name, compare_name)


def test_compare_album_evidence_standalone_separator_between_words_is_drift() -> None:
    """A symbol standing between words is a separator, so it never blocks a match."""
    album_a = _album(name="HIStory - PAST, PRESENT AND FUTURE - BOOK I", year=1995)
    album_b = _album(
        item_id="2",
        provider="test2",
        name="HIStory: Past, Present and Future, Book I",
        year=1995,
    )

    assert compare.compare_album_evidence(album_a, album_b) == compare.AlbumMatchEvidence.MATCH


def test_compare_album_evidence_ep_and_single_of_the_same_name_stay_distinct() -> None:
    """Two titles naming a different format are separate releases, whatever base they share."""
    album_a = _album(name="Stargazing - EP", year=2023)
    album_b = _album(item_id="2", provider="test2", name="Stargazing - Single", year=2023)

    assert compare.compare_album_evidence(album_a, album_b) == compare.AlbumMatchEvidence.NO_MATCH

    # either spelling still matches the plain title, which names no format at all
    for name in ("Stargazing - EP", "Stargazing - Single"):
        album_a = _album(name=name, year=2023)
        album_b = _album(item_id="2", provider="test2", name="Stargazing", year=2023)
        assert (
            compare.compare_album_evidence(album_a, album_b) == compare.AlbumMatchEvidence.MATCH
        ), name


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
    """A blank version never cancels a recording-changing qualifier."""
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


def test_compare_track_evidence_ranks_release_and_recording_matches() -> None:
    """Exact-release evidence outranks the same recording on another album."""
    mb_album_id = {(ExternalID.MB_ALBUM, "11111111-1111-1111-1111-111111111111")}
    base = _provider_track("base", "provider_a", album_external_ids=mb_album_id)
    exact = _provider_track("exact", "provider_b", album_external_ids=mb_album_id)
    alternate_release = _provider_track(
        "alternate",
        "provider_b",
        album_name="Compilation",
    )

    assert compare.compare_track_evidence(base, exact) == compare.TrackMatchConfidence.EXACT
    assert (
        compare.compare_track_evidence(base, alternate_release)
        == compare.TrackMatchConfidence.LIKELY
    )


def test_compare_track_evidence_full_metadata_agreement_without_release_evidence_is_likely() -> (
    None
):
    """Title/artist/version/position agreement alone, with no release-level id, caps at LIKELY."""
    base = _provider_track("base", "provider_a")
    candidate = _provider_track("candidate", "provider_b")

    # Matching metadata alone is not release-level proof.
    assert compare.compare_track_evidence(base, candidate) == compare.TrackMatchConfidence.LIKELY


def test_compare_track_evidence_conflicting_release_track_ids_are_not_exact() -> None:
    """Different MusicBrainz release-track IDs can still identify the same recording."""
    base = _provider_track(
        "base",
        "provider_a",
        external_ids={
            (
                ExternalID.MB_TRACK,
                "11111111-1111-1111-1111-111111111111",
            )
        },
    )
    candidate = _provider_track(
        "candidate",
        "provider_b",
        external_ids={
            (
                ExternalID.MB_TRACK,
                "22222222-2222-2222-2222-222222222222",
            )
        },
    )

    assert compare.compare_track_evidence(base, candidate) == compare.TrackMatchConfidence.LIKELY


def test_compare_track_evidence_cross_instance_item_id_collision_is_not_exact() -> None:
    """A coincidental item id shared by two different provider instances is not proof."""
    base = media_items.Track(
        item_id="Artist/Album/01.flac",
        provider="filesystem_1",
        name="Track One",
        version="",
        duration=200,
        disc_number=1,
        track_number=1,
        artists=media_items.UniqueList(
            [
                media_items.ItemMapping(
                    item_id="artist-a",
                    provider="filesystem_1",
                    name="Artist A",
                    media_type=MediaType.ARTIST,
                )
            ]
        ),
        provider_mappings={
            media_items.ProviderMapping(
                item_id="Artist/Album/01.flac",
                provider_domain="filesystem",
                provider_instance="filesystem_1",
            )
        },
    )
    # Non-streaming sibling instances do not share item IDs.
    unrelated = media_items.Track(
        item_id="Artist/Album/01.flac",
        provider="filesystem_2",
        name="Completely Different Song",
        version="",
        duration=321,
        disc_number=1,
        track_number=7,
        artists=media_items.UniqueList(
            [
                media_items.ItemMapping(
                    item_id="artist-b",
                    provider="filesystem_2",
                    name="Someone Else",
                    media_type=MediaType.ARTIST,
                )
            ]
        ),
        provider_mappings={
            media_items.ProviderMapping(
                item_id="Artist/Album/01.flac",
                provider_domain="filesystem",
                provider_instance="filesystem_2",
            )
        },
    )

    assert compare.compare_track_evidence(base, unrelated) == compare.TrackMatchConfidence.NO_MATCH

    # the same instance sharing the same item id is unambiguous identity and stays EXACT
    same_instance = media_items.Track(
        item_id="Artist/Album/01.flac",
        provider="filesystem_1",
        name="Completely Different Song",
        version="",
        duration=321,
        disc_number=1,
        track_number=7,
        artists=base.artists,
        provider_mappings={
            media_items.ProviderMapping(
                item_id="Artist/Album/01.flac",
                provider_domain="filesystem",
                provider_instance="filesystem_1",
            )
        },
    )
    assert compare.compare_track_evidence(base, same_instance) == compare.TrackMatchConfidence.EXACT


def test_compare_track_evidence_cross_instance_album_id_collision_is_not_same_album() -> None:
    """A coincidental album item id shared across non-streaming instances is not proof."""

    def _colliding_album(provider_instance: str, name: str) -> media_items.Album:
        return media_items.Album(
            item_id="Various/Comp/album.dir",
            provider=provider_instance,
            name=name,
            artists=media_items.UniqueList(
                [
                    media_items.Artist(
                        item_id="artist",
                        provider=provider_instance,
                        name="Various Artists",
                        provider_mappings={
                            media_items.ProviderMapping(
                                item_id="artist",
                                provider_domain="filesystem",
                                provider_instance=provider_instance,
                            )
                        },
                    )
                ]
            ),
            provider_mappings={
                media_items.ProviderMapping(
                    item_id="Various/Comp/album.dir",
                    provider_domain="filesystem",
                    provider_instance=provider_instance,
                )
            },
        )

    def _album_track(
        provider_instance: str, album: media_items.Album, track_number: int
    ) -> media_items.Track:
        return media_items.Track(
            item_id=f"{provider_instance}-track",
            provider=provider_instance,
            name="Some Song Title",
            duration=200,
            disc_number=1,
            track_number=track_number,
            artists=media_items.UniqueList(
                [
                    media_items.ItemMapping(
                        item_id="artist-a",
                        provider=provider_instance,
                        name="Some Artist",
                        media_type=MediaType.ARTIST,
                    )
                ]
            ),
            album=album,
            provider_mappings={
                media_items.ProviderMapping(
                    item_id=f"{provider_instance}-track",
                    provider_domain="filesystem",
                    provider_instance=provider_instance,
                )
            },
        )

    # Non-streaming sibling instances do not share album IDs either.
    album_a = _colliding_album("filesystem_1", "Compilation Volume 1")
    album_b = _colliding_album("filesystem_2", "A Totally Different Compilation")
    assert compare._same_album(album_a, album_b) is False

    # The false album match must not force a NO_MATCH here.
    base = _album_track("filesystem_1", album_a, 3)
    candidate = _album_track("filesystem_2", album_b, 9)
    assert compare.compare_track_evidence(base, candidate) == compare.TrackMatchConfidence.LIKELY


def test_compare_track_evidence_authoritative_id_overrides_position_drift() -> None:
    """Provider track-number drift does not override an authoritative release-track ID."""
    mb_track = (
        ExternalID.MB_TRACK,
        "11111111-1111-1111-1111-111111111111",
    )
    base = _provider_track("base", "provider_a", external_ids={mb_track})
    candidate = _provider_track(
        "candidate",
        "provider_b",
        external_ids={mb_track},
    )
    candidate.track_number = 2

    assert compare.compare_track_evidence(base, candidate) == compare.TrackMatchConfidence.EXACT

    base.external_ids.clear()
    candidate.external_ids.clear()
    assert compare.compare_track_evidence(base, candidate) == compare.TrackMatchConfidence.NO_MATCH


def test_compare_track_evidence_simplified_albums_do_not_prove_exact_release() -> None:
    """Album mappings without artist and year evidence only support a recording match."""
    base = _provider_track("base", "provider_a")
    candidate = _provider_track("candidate", "provider_b")
    base.album = media_items.ItemMapping(
        item_id="album-a",
        provider="provider_a",
        name="Album",
        media_type=MediaType.ALBUM,
    )
    candidate.album = media_items.ItemMapping(
        item_id="album-b",
        provider="provider_b",
        name="Album",
        media_type=MediaType.ALBUM,
    )

    assert compare.compare_track_evidence(base, candidate) == compare.TrackMatchConfidence.LIKELY


def test_compare_track_evidence_accepts_missing_remaster_by_album_year() -> None:
    """Matching album years resolve version metadata omitted by one provider."""
    base = _provider_track("base", "provider_a")
    remaster = _provider_track(
        "remaster",
        "provider_b",
        version="2022 Remaster",
    )
    assert isinstance(base.album, media_items.Album)
    base.album.year = 2022
    assert isinstance(remaster.album, media_items.Album)
    remaster.album.year = 2022

    assert compare.compare_track_evidence(base, remaster) == compare.TrackMatchConfidence.LIKELY

    remaster.album.year = 2021
    assert compare.compare_track_evidence(base, remaster) == compare.TrackMatchConfidence.LOOSE


def test_compare_track_evidence_rejects_recording_version_conflicts() -> None:
    """A recording-changing version never matches the original recording."""
    recording_id = (
        ExternalID.MB_RECORDING,
        "12345678-1234-1234-1234-123456789abc",
    )
    base = _provider_track("base", "provider_a", external_ids={recording_id})
    remix = _provider_track(
        "remix",
        "provider_b",
        version="Club Remix",
        external_ids={recording_id},
    )

    assert compare.compare_track_evidence(base, remix) == compare.TrackMatchConfidence.NO_MATCH


def test_compare_track_evidence_release_track_id_overrides_version_drift() -> None:
    """A shared release-track ID overrides conflicting provider version metadata."""
    mb_track = (
        ExternalID.MB_TRACK,
        "12345678-1234-1234-1234-123456789abc",
    )
    base = _provider_track("base", "provider_a", external_ids={mb_track})
    mislabeled = _provider_track(
        "candidate",
        "provider_b",
        version="Club Remix",
        external_ids={mb_track},
    )

    assert compare.compare_track_evidence(base, mislabeled) == compare.TrackMatchConfidence.EXACT


def test_compare_track_evidence_handles_featured_artist_title_drift() -> None:
    """Featured credits may move between the title and structured artist list."""
    title_credit = _provider_track(
        "base",
        "provider_a",
        name="Track (feat. Guest)",
    )
    structured_credit = _provider_track(
        "candidate",
        "provider_b",
        album_name="Compilation",
        artist_names=("Artist A", "Guest"),
    )

    assert (
        compare.compare_track_evidence(title_credit, structured_credit)
        == compare.TrackMatchConfidence.LIKELY
    )


def test_compare_track_evidence_handles_with_artist_title_drift() -> None:
    """A with-credit may move between the title and structured artist list."""
    title_credit = _provider_track(
        "base",
        "provider_a",
        name="Track (with Guest)",
    )
    structured_credit = _provider_track(
        "candidate",
        "provider_b",
        album_name="Compilation",
        artist_names=("Artist A", "Guest"),
    )

    assert (
        compare.compare_track_evidence(title_credit, structured_credit)
        == compare.TrackMatchConfidence.LIKELY
    )


def test_compare_track_evidence_stops_feature_credit_before_version() -> None:
    """Version brackets after a bare featured credit are not part of the artist name."""
    title_credit = _provider_track(
        "base",
        "provider_a",
        name="Track feat. Guest (Radio Edit)",
        album_name="Original",
    )
    structured_credit = _provider_track(
        "candidate",
        "provider_b",
        version="Radio Edit",
        album_name="Compilation",
        artist_names=("Artist A", "Guest"),
    )

    assert (
        compare.compare_track_evidence(title_credit, structured_credit)
        == compare.TrackMatchConfidence.LIKELY
    )


def test_compare_track_evidence_strips_bare_colon_feature_credit() -> None:
    """A bare colon-form featured credit is stripped like the dot/space forms during search."""
    title_credit = _provider_track(
        "base",
        "provider_a",
        name="Track feat:Guest",
        album_name="Original",
    )
    structured_credit = _provider_track(
        "candidate",
        "provider_b",
        album_name="Compilation",
        artist_names=("Artist A", "Guest"),
    )

    assert (
        compare.compare_track_evidence(title_credit, structured_credit)
        == compare.TrackMatchConfidence.LIKELY
    )


def test_compare_track_evidence_allows_omitted_featured_artist() -> None:
    """A provider may omit a featured credit carried by the other provider."""
    credited = _provider_track(
        "base",
        "provider_a",
        name="Track (feat. Guest)",
        album_name="Original",
    )
    omitted = _provider_track(
        "candidate",
        "provider_b",
        album_name="Compilation",
    )

    assert compare.compare_track_evidence(credited, omitted) == compare.TrackMatchConfidence.LIKELY


def test_compare_track_evidence_rejects_conflicting_featured_artists() -> None:
    """Different explicit featured credits identify different collaborations."""
    alice = _provider_track(
        "alice",
        "provider_a",
        name="Track (feat. Alice)",
        album_name="Original",
    )
    bob = _provider_track(
        "bob",
        "provider_b",
        name="Track (feat. Bob)",
        album_name="Compilation",
    )

    assert compare.compare_track_evidence(alice, bob) == compare.TrackMatchConfidence.NO_MATCH


def test_compare_track_evidence_rejects_conflicting_colon_featured_artists() -> None:
    """Colon-form featured credits remain part of track identity."""
    alice = _provider_track(
        "alice",
        "provider_a",
        name="Track (feat:Alice)",
        album_name="Original",
    )
    bob = _provider_track(
        "bob",
        "provider_b",
        name="Track (feat:Bob)",
        album_name="Compilation",
    )

    assert compare.compare_track_evidence(alice, bob) == compare.TrackMatchConfidence.NO_MATCH


def test_compare_track_evidence_keeps_composite_artist_identity() -> None:
    """A partial overlap does not match a complete composite artist credit."""
    partial_credit = _provider_track(
        "partial",
        "provider_a",
        name="Track (feat. Tyler)",
        album_name="Original",
    )
    composite_credit = _provider_track(
        "composite",
        "provider_b",
        album_name="Compilation",
        artist_names=("Artist A", "Tyler, The Creator"),
    )

    assert (
        compare.compare_track_evidence(partial_credit, composite_credit)
        == compare.TrackMatchConfidence.NO_MATCH
    )


def test_compare_track_evidence_rejects_candidate_missing_primary_artist() -> None:
    """A candidate crediting only the source's featured guest is not the same track."""
    original = _provider_track(
        "original",
        "provider_a",
        artist_names=("Alice", "Bob"),
        album_name="Original",
    )
    guest_only = _provider_track(
        "guest_only",
        "provider_b",
        artist_names=("Bob",),
        album_name="Bob Solo Album",
    )

    assert (
        compare.compare_track_evidence(original, guest_only)
        == compare.TrackMatchConfidence.NO_MATCH
    )


def test_compare_track_evidence_keeps_complete_featured_artist_name() -> None:
    """A separator inside one featured artist name is not treated as conflicting credits."""
    title_credit = _provider_track(
        "base",
        "provider_a",
        name="Track (feat. Simon and Garfunkel)",
        album_name="Original",
    )
    structured_credit = _provider_track(
        "candidate",
        "provider_b",
        album_name="Compilation",
        artist_names=("Artist A", "Simon & Garfunkel"),
    )

    assert (
        compare.compare_track_evidence(title_credit, structured_credit)
        == compare.TrackMatchConfidence.LIKELY
    )


def test_compare_track_evidence_matches_composite_primary_artist() -> None:
    """A composite band name credited as the sole primary artist still matches itself."""
    base = _provider_track(
        "base",
        "provider_a",
        album_name="Original",
        artist_names=("Simon & Garfunkel",),
    )
    candidate = _provider_track(
        "candidate",
        "provider_b",
        album_name="Compilation",
        artist_names=("Simon & Garfunkel",),
    )

    assert compare.compare_track_evidence(base, candidate) != compare.TrackMatchConfidence.NO_MATCH


def test_compare_track_evidence_matches_asymmetric_composite_credit() -> None:
    """A single composite credit reconstructed from an M3U still matches two split artists."""
    # a third-party M3U exporter can collapse a multi-artist credit into one combined
    # name; the provider candidate credits the same two artists as separate entries
    m3u_credit = _provider_track(
        "m3u",
        "provider_a",
        album_name="Original",
        artist_names=("Artist A, Artist B",),
    )
    provider_credit = _provider_track(
        "provider",
        "provider_b",
        album_name="Compilation",
        artist_names=("Artist A", "Artist B"),
    )

    assert (
        compare.compare_track_evidence(m3u_credit, provider_credit)
        != compare.TrackMatchConfidence.NO_MATCH
    )


def test_compare_track_evidence_rejects_unrelated_separate_artists_matching_band_name() -> None:
    """A structured act name is not shredded into unrelated solo artists sharing its words."""
    # "Earth, Wind & Fire" is one official act's own name, not a list of three artists -
    # a track crediting three separate solo artists literally named Earth/Wind/Fire is
    # an unrelated recording that must not be treated as the same one
    band_credit = _provider_track(
        "band",
        "provider_a",
        album_name="Original",
        artist_names=("Earth, Wind & Fire",),
    )
    solo_artists_credit = _provider_track(
        "solo",
        "provider_b",
        album_name="Compilation",
        artist_names=("Earth", "Wind", "Fire"),
    )

    assert (
        compare.compare_track_evidence(band_credit, solo_artists_credit)
        == compare.TrackMatchConfidence.NO_MATCH
    )


def test_compare_track_evidence_ranks_recording_identifiers() -> None:
    """Release-track IDs are exact while recording IDs identify alternate releases."""
    mb_track = (
        ExternalID.MB_TRACK,
        "12345678-1234-1234-1234-123456789abc",
    )
    mb_recording = (
        ExternalID.MB_RECORDING,
        "abcdefab-abcd-abcd-abcd-abcdefabcdef",
    )
    base = _provider_track(
        "base",
        "provider_a",
        album_name="Original",
        external_ids={mb_track, mb_recording},
    )
    exact = _provider_track(
        "exact",
        "provider_b",
        album_name="Reissue",
        external_ids={mb_track, mb_recording},
    )
    recording = _provider_track(
        "recording",
        "provider_b",
        album_name="Compilation",
        external_ids={
            (
                ExternalID.MB_TRACK,
                "aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb",
            ),
            mb_recording,
        },
    )
    conflicting_recording = _provider_track(
        "conflict",
        "provider_b",
        external_ids={
            mb_track,
            (
                ExternalID.MB_RECORDING,
                "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            ),
        },
    )

    assert compare.compare_track_evidence(base, exact) == compare.TrackMatchConfidence.EXACT
    assert compare.compare_track_evidence(base, recording) == compare.TrackMatchConfidence.LIKELY
    assert (
        compare.compare_track_evidence(base, conflicting_recording)
        == compare.TrackMatchConfidence.NO_MATCH
    )


def test_compare_track_evidence_uses_isrc_duration_tolerance() -> None:
    """A valid shared ISRC tolerates provider duration drift up to eight seconds."""
    isrc = (ExternalID.ISRC, "USRC17607839")
    base = _provider_track("base", "provider_a", duration=200, external_ids={isrc})
    within_tolerance = _provider_track(
        "within",
        "provider_b",
        duration=208,
        album_name="Compilation",
        external_ids={isrc},
    )
    outside_tolerance = _provider_track(
        "outside",
        "provider_b",
        duration=209,
        album_name="Compilation",
        external_ids={isrc},
    )

    assert (
        compare.compare_track_evidence(base, within_tolerance)
        == compare.TrackMatchConfidence.LIKELY
    )
    assert (
        compare.compare_track_evidence(base, outside_tolerance)
        == compare.TrackMatchConfidence.NO_MATCH
    )


def test_compare_track_evidence_treats_unknown_duration_as_loose_not_reject() -> None:
    """A title/artist match isn't rejected just because one side's duration is unknown."""
    base = _provider_track("base", "provider_a", duration=200)
    # 0 is the unset default; -1 is the M3U convention for an unadvertised duration
    missing_duration = _provider_track(
        "missing", "provider_b", duration=0, album_name="Compilation"
    )
    unknown_duration = _provider_track(
        "unknown", "provider_b", duration=-1, album_name="Compilation"
    )

    assert (
        compare.compare_track_evidence(base, missing_duration) == compare.TrackMatchConfidence.LOOSE
    )
    assert (
        compare.compare_track_evidence(base, unknown_duration) == compare.TrackMatchConfidence.LOOSE
    )


def test_compare_track_evidence_rejects_explicitness_conflicts() -> None:
    """Explicit and clean recordings are not interchangeable migration matches."""
    explicit = _provider_track("explicit", "provider_a")
    clean = _provider_track("clean", "provider_b")
    explicit.metadata.explicit = True
    clean.metadata.explicit = False

    assert compare.compare_track_evidence(explicit, clean) == compare.TrackMatchConfidence.NO_MATCH


def test_compare_track_evidence_uses_hydrated_album_explicitness() -> None:
    """Hydrated album metadata prevents clean and explicit substitutions."""
    base = _provider_track("base", "provider_a")
    candidate = _provider_track("candidate", "provider_b")
    assert isinstance(base.album, media_items.Album)
    assert isinstance(candidate.album, media_items.Album)
    base_album = base.album
    candidate_album = candidate.album
    base_album.metadata.explicit = True
    candidate_album.metadata.explicit = False
    base.album = media_items.ItemMapping(
        item_id=base_album.item_id,
        provider=base_album.provider,
        name=base_album.name,
        media_type=MediaType.ALBUM,
    )
    candidate.album = media_items.ItemMapping(
        item_id=candidate_album.item_id,
        provider=candidate_album.provider,
        name=candidate_album.name,
        media_type=MediaType.ALBUM,
    )

    assert (
        compare.compare_track_evidence(
            base,
            candidate,
            base_album=base_album,
            compare_album_item=candidate_album,
        )
        == compare.TrackMatchConfidence.NO_MATCH
    )


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
