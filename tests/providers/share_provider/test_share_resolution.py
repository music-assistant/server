"""Test share resolution tests."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Literal, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import ExternalID, MediaType, ProviderType
from music_assistant_models.errors import (
    ProviderUnavailableError,
)
from music_assistant_models.media_items import (
    Album,
    Artist,
    ProviderMapping,
    Track,
    UniqueList,
)

from music_assistant.helpers.compare import (
    TrackMatchConfidence,
)
from music_assistant.providers.share_provider import (
    SharePayload,
)

from .helpers import BARCODE, ISRC, _core_match, _mass, _provider, _track


@pytest.mark.parametrize(
    ("media_type", "external_ids", "expected_type", "expected_id"),
    [
        (
            MediaType.TRACK,
            {"isrc": ISRC, "mbid": "recording-mbid"},
            ExternalID.ISRC,
            ISRC,
        ),
        (
            MediaType.TRACK,
            {"mb_track": "release-track-mbid"},
            ExternalID.MB_TRACK,
            "release-track-mbid",
        ),
        (
            MediaType.ALBUM,
            {"barcode": BARCODE, "mbid": "album-mbid"},
            ExternalID.BARCODE,
            BARCODE,
        ),
        (
            MediaType.ARTIST,
            {"mbid": "artist-mbid"},
            ExternalID.MB_ARTIST,
            "artist-mbid",
        ),
    ],
)
async def test_resolve_uses_controller_external_id_before_search(
    media_type: MediaType,
    external_ids: dict[str, str],
    expected_type: ExternalID,
    expected_id: str,
) -> None:
    """Resolve matching media types through the shared controller contract."""
    provider = _provider()
    controller = _mass(provider).music.get_controller.return_value
    item: Album | Artist | Track | MagicMock
    if media_type == MediaType.ALBUM:
        item = Album(
            item_id="item",
            provider="library",
            name="Item",
            provider_mappings=set(),
            external_ids={(expected_type, expected_id)},
        )
    elif media_type == MediaType.TRACK:
        item = _track(
            item_id="item",
            name="Item",
            external_ids={(expected_type, expected_id)},
        )
    else:
        item = Artist(
            item_id="item",
            provider="library",
            name="Item",
            provider_mappings=set(),
            external_ids={(expected_type, expected_id)},
        )
    controller.get_item_by_external_id = AsyncMock(return_value=item)
    _mass(provider).music.tracks.get = AsyncMock(return_value=item)
    encoded = SharePayload(
        type=cast("Literal['track', 'album', 'artist', 'playlist']", media_type.value),
        title="Item",
        isrc=external_ids.get("isrc"),
        mbid=external_ids.get("mbid"),
        mb_track=external_ids.get("mb_track"),
        barcode=external_ids.get("barcode"),
        artist="Artist" if media_type == MediaType.TRACK else None,
        duration=218 if media_type == MediaType.TRACK else None,
    ).to_base64()

    result = await provider.resolve_share_link(encoded, allow_search=False)

    assert result is item
    controller.get_item_by_external_id.assert_awaited_once_with(expected_id, expected_type)
    _mass(provider).music.search.assert_not_called()


@pytest.mark.parametrize("lookup_failure", [False, True])
async def test_resolve_without_search_returns_none_after_id_miss(lookup_failure: bool) -> None:
    """ID-only resolution never falls back to a text-search candidate."""
    provider = _provider()
    mass = _mass(provider)
    controller = mass.music.get_controller.return_value
    controller.get_item_by_external_id = AsyncMock(
        return_value=None,
        side_effect=ProviderUnavailableError("provider offline") if lookup_failure else None,
    )
    encoded = SharePayload(
        type="track", title="Track", artist="Artist", isrc=ISRC, duration=218
    ).to_base64()

    assert await provider.resolve_share_link(encoded, allow_search=False) is None
    mass.music.search.assert_not_called()


async def test_resolve_skips_unrelated_barcode_album_and_tries_mbid() -> None:
    """A barcode collision does not mask an album matching the shared metadata."""
    provider = _provider()
    mass = _mass(provider)
    unrelated = Album(
        item_id="wrong-album",
        provider="library",
        name="Different Album",
        provider_mappings=set(),
    )
    matching = Album(
        item_id="right-album",
        provider="library",
        name="Shared Album",
        provider_mappings=set(),
    )
    controller = mass.music.get_controller.return_value
    controller.get_item_by_external_id = AsyncMock(side_effect=[unrelated, matching])
    payload = SharePayload(type="album", title="Shared Album", barcode=BARCODE, mbid="album-mbid")

    result = await provider._resolve_media_item(payload, allow_search=False)

    assert result is matching
    assert controller.get_item_by_external_id.await_count == 2
    mass.music.search.assert_not_called()


@pytest.mark.parametrize(
    ("candidate_title", "candidate_artist"),
    [("Different song", "Artist"), ("Shared song", "Other artist")],
)
async def test_resolve_rejects_unrelated_isrc_match(
    candidate_title: str, candidate_artist: str
) -> None:
    """A matching ISRC and duration still require title and artist corroboration."""
    provider = _provider()
    mass = _mass(provider)
    unrelated = _track(
        item_id="wrong-track",
        name=candidate_title,
        artist=candidate_artist,
        duration=218,
        external_ids={(ExternalID.ISRC, ISRC)},
    )
    mass.music.get_controller.return_value.get_item_by_external_id = AsyncMock(
        return_value=unrelated
    )
    mass.music.tracks.get = AsyncMock()
    payload = SharePayload(
        type="track", title="Shared song", artist="Artist", isrc=ISRC, duration=218
    )

    result = await provider._resolve_media_item(payload, allow_search=False)

    assert result is None
    mass.music.tracks.get.assert_not_called()


async def test_legacy_isrc_share_without_duration_is_not_resolved() -> None:
    """A legacy ISRC without duration is insufficient identity evidence."""
    provider = _provider()
    mass = _mass(provider)
    candidate = _track(external_ids={(ExternalID.ISRC, ISRC)})
    mass.music.get_controller.return_value.get_item_by_external_id = AsyncMock(
        return_value=candidate
    )
    result = await provider._resolve_media_item(
        SharePayload(type="track", title="Track", artist="Artist", isrc=ISRC),
        allow_search=False,
    )

    assert result is None
    mass.music.tracks.get.assert_not_called()


async def test_resolve_skips_conflicting_isrc_recording_id_and_tries_mbid() -> None:
    """A conflicting recording ID on an ISRC hit does not hide a valid MBID match."""
    provider = _provider()
    mass = _mass(provider)
    conflicting = _track(
        item_id="conflicting-track",
        name="Shared song",
        external_ids={(ExternalID.ISRC, ISRC), (ExternalID.MB_RECORDING, "recording-other")},
    )
    matching = _track(
        item_id="matching-track",
        name="Shared song",
        external_ids={(ExternalID.MB_RECORDING, "recording-shared")},
    )
    controller = mass.music.get_controller.return_value
    controller.get_item_by_external_id = AsyncMock(side_effect=[conflicting, matching])
    mass.music.tracks.get = AsyncMock(return_value=matching)
    payload = SharePayload(
        type="track",
        title="Shared song",
        artist="Artist",
        isrc=ISRC,
        mbid="recording-shared",
        duration=218,
    )

    result = await provider._resolve_media_item(payload, allow_search=False)

    assert result is matching
    assert controller.get_item_by_external_id.await_count == 2
    mass.music.tracks.get.assert_awaited_once_with(
        "matching-track", "catalog--1", allow_update_metadata=False
    )


async def test_resolve_without_search_returns_id_match() -> None:
    """ID matches include full track and album details without text search."""
    provider = _provider()
    mass = _mass(provider)
    item = _track(item_id="source-id")
    full_item = MagicMock()
    mass.music.tracks.get = AsyncMock(return_value=full_item)
    mass.music.get_controller.return_value.get_item_by_external_id = AsyncMock(return_value=item)
    encoded = SharePayload(
        type="track", title="Track", artist="Artist", isrc=ISRC, duration=218
    ).to_base64()

    assert await provider.resolve_share_link(encoded, allow_search=False) is full_item
    mass.music.tracks.get.assert_awaited_once_with(
        item.item_id, item.provider, allow_update_metadata=False
    )
    mass.music.search.assert_not_called()


async def test_resolve_keeps_release_track_id_match_despite_version_label_drift() -> None:
    """A release-specific MusicBrainz ID is stronger than provider version labels."""
    provider = _provider()
    mass = _mass(provider)
    id_match = _track(
        item_id="release-track",
        version="Studio",
        external_ids={(ExternalID.MB_TRACK, "release-track-mbid")},
    )
    full_track = _track(item_id="release-track", version="Studio")
    mass.music.get_controller.return_value.get_item_by_external_id = AsyncMock(
        return_value=id_match
    )
    mass.music.tracks.get = AsyncMock(return_value=full_track)
    payload = SharePayload(
        type="track",
        title="Track",
        artist="Artist",
        version="Live",
        mb_track="release-track-mbid",
    )

    assert await provider._resolve_media_item(payload, allow_search=False) is full_track
    mass.music.tracks.get.assert_awaited_once_with(
        "release-track", "catalog--1", allow_update_metadata=False
    )


async def test_resolve_rejects_version_mismatch_for_recording_id() -> None:
    """A recording ID must not resolve to a different explicit version."""
    provider = _provider()
    mass = _mass(provider)
    mass.music.get_controller.return_value.get_item_by_external_id = AsyncMock(
        return_value=_track(
            item_id="recording",
            version="Studio",
            external_ids={(ExternalID.MB_RECORDING, "recording-mbid")},
        )
    )
    payload = SharePayload(
        type="track",
        title="Track",
        artist="Artist",
        version="Live",
        mbid="recording-mbid",
    )

    assert await provider._resolve_media_item(payload, allow_search=False) is None
    mass.music.tracks.get.assert_not_called()


async def test_resolve_unversioned_track_rejects_versioned_recording_id() -> None:
    """An unversioned share must not resolve to a version-specific recording ID."""
    provider = _provider()
    mass = _mass(provider)
    mass.music.get_controller.return_value.get_item_by_external_id = AsyncMock(
        return_value=_track(
            item_id="live-recording",
            version="Live",
            external_ids={(ExternalID.ISRC, ISRC)},
        )
    )

    assert (
        await provider._resolve_media_item(
            SharePayload(type="track", title="Track", artist="Artist", isrc=ISRC),
            allow_search=False,
        )
        is None
    )
    mass.music.tracks.get.assert_not_called()


async def test_resolve_skips_id_match_when_full_track_details_are_unavailable() -> None:
    """An ID hit whose detail fetch fails remains a miss for preview fallback."""
    provider = _provider()
    mass = _mass(provider)
    mass.music.get_controller.return_value.get_item_by_external_id = AsyncMock(
        return_value=_track(item_id="source-id", external_ids={(ExternalID.ISRC, ISRC)})
    )
    mass.music.tracks.get = AsyncMock(side_effect=ProviderUnavailableError("offline"))
    encoded = SharePayload(
        type="track", title="Track", artist="Artist", isrc=ISRC, duration=218
    ).to_base64()

    assert await provider.resolve_share_link(encoded, allow_search=False) is None
    mass.music.tracks.get.assert_awaited_once()
    mass.music.search.assert_not_called()


async def test_track_resolution_uses_core_matcher_and_full_share_metadata() -> None:
    """Core matching receives the complete metadata available in the share payload."""
    provider = _provider()
    mass = _mass(provider)
    matched = _track(item_id="matched", version="Live")
    mass.music.get_controller.return_value.get_item_by_external_id = AsyncMock(return_value=None)
    mass.music.tracks.enrich_provider_mappings = AsyncMock(
        return_value=SimpleNamespace(matches=(_core_match(matched, TrackMatchConfidence.LIKELY),))
    )
    payload = SharePayload(
        type="track",
        title="Track",
        artist="Artist",
        version="Live",
        album="Album",
        isrc=ISRC,
        duration=218,
    )

    assert await provider._resolve_media_item(payload) is matched
    core_track = mass.music.tracks.enrich_provider_mappings.await_args.args[0]
    assert core_track.name == "Track"
    assert core_track.version == "Live"
    assert core_track.duration == 218
    assert core_track.album is None
    assert core_track.external_ids == {(ExternalID.ISRC, ISRC)}
    assert core_track.artists[0].name == "Artist"
    mass.music.search.assert_not_called()
    enrichment_call = mass.music.tracks.enrich_provider_mappings.await_args
    assert enrichment_call.kwargs["minimum_confidence"] == TrackMatchConfidence.LIKELY
    assert enrichment_call.kwargs["provider_instance_ids"] == set()
    assert enrichment_call.kwargs["search_non_streaming_without_mapping"] is True


async def test_core_search_accepts_matching_artist_with_featured_credit_drift() -> None:
    """Core ISRC matches tolerate services that omit a featured artist credit."""
    provider = _provider()
    mass = _mass(provider)
    candidate = _track(
        item_id="reused-isrc",
        artist="Artist",
        duration=218,
        external_ids={(ExternalID.ISRC, ISRC)},
    )
    mass.music.get_controller.return_value.get_item_by_external_id = AsyncMock(return_value=None)
    mass.music.tracks.enrich_provider_mappings = AsyncMock(
        return_value=SimpleNamespace(matches=(_core_match(candidate, TrackMatchConfidence.LIKELY),))
    )

    result = await provider._resolve_media_item(
        SharePayload(
            type="track",
            title="Track",
            artist="Artist, Featured Artist",
            artists=["Artist", "Featured Artist"],
            isrc=ISRC,
            duration=218,
        )
    )

    assert result is candidate


async def test_core_search_rejects_reused_isrc_with_different_lead_artist() -> None:
    """A reused ISRC cannot override a different primary artist in Core search."""
    provider = _provider()
    mass = _mass(provider)
    candidate = _track(
        item_id="reused-isrc",
        artist="Other Artist",
        duration=218,
        external_ids={(ExternalID.ISRC, ISRC)},
    )
    mass.music.get_controller.return_value.get_item_by_external_id = AsyncMock(return_value=None)
    mass.music.tracks.enrich_provider_mappings = AsyncMock(
        return_value=SimpleNamespace(matches=(_core_match(candidate, TrackMatchConfidence.LOOSE),))
    )

    result = await provider._resolve_media_item(
        SharePayload(
            type="track",
            title="Track",
            artist="Artist",
            isrc=ISRC,
            duration=218,
        )
    )

    assert result is None


async def test_core_search_accepts_core_match_with_different_featured_artist_credits() -> None:
    """A confident Core match is not rejected by a stricter share-local artist check."""
    provider = _provider()
    mass = _mass(provider)
    candidate = Track(
        item_id="reused-isrc",
        provider="catalog--1",
        name="Song",
        duration=218,
        external_ids={(ExternalID.ISRC, ISRC)},
        provider_mappings={
            ProviderMapping(
                item_id="reused-isrc",
                provider_domain="catalog",
                provider_instance="catalog--1",
                available=True,
            )
        },
        artists=UniqueList(
            [
                Artist(
                    item_id="first", provider="catalog--1", name="First", provider_mappings=set()
                ),
                Artist(
                    item_id="other", provider="catalog--1", name="Other", provider_mappings=set()
                ),
            ]
        ),
    )
    mass.music.get_controller.return_value.get_item_by_external_id = AsyncMock(return_value=None)
    mass.music.tracks.enrich_provider_mappings = AsyncMock(
        return_value=SimpleNamespace(matches=(_core_match(candidate, TrackMatchConfidence.LIKELY),))
    )

    result = await provider._resolve_media_item(
        SharePayload(
            type="track",
            title="Song",
            artist="First, Second",
            artists=["First", "Second"],
            isrc=ISRC,
            duration=218,
        )
    )

    assert result is candidate


@pytest.mark.parametrize(("second_artist", "expected_match"), [("Second", True), ("Other", False)])
async def test_isrc_match_requires_all_artist_credits(
    second_artist: str, expected_match: bool
) -> None:
    """An ISRC hit is accepted only when every credited artist matches."""
    provider = _provider()
    mass = _mass(provider)
    candidate = Track(
        item_id="candidate",
        provider="catalog--1",
        name="Song",
        duration=218,
        external_ids={(ExternalID.ISRC, ISRC)},
        provider_mappings={
            ProviderMapping(
                item_id="candidate",
                provider_domain="catalog",
                provider_instance="catalog--1",
                available=True,
            )
        },
        artists=UniqueList(
            [
                Artist(item_id="one", provider="catalog--1", name="First", provider_mappings=set()),
                Artist(
                    item_id="two",
                    provider="catalog--1",
                    name=second_artist,
                    provider_mappings=set(),
                ),
            ]
        ),
    )
    mass.music.get_controller.return_value.get_item_by_external_id = AsyncMock(
        return_value=candidate
    )
    mass.music.tracks.get = AsyncMock(return_value=candidate)
    payload = SharePayload(
        type="track",
        title="Song",
        artist="First, Second",
        artists=["First", "Second"],
        duration=218,
        isrc=ISRC,
    )

    result = await provider._resolve_media_item(payload, allow_search=False)

    assert (result is candidate) is expected_match
    assert mass.music.tracks.get.await_count == int(expected_match)


async def test_track_resolution_returns_none_without_core_match() -> None:
    """A failed or ambiguous core enrichment never opens an arbitrary candidate."""
    provider = _provider()
    mass = _mass(provider)
    payload = SharePayload(type="track", title="Track", artist="Artist")

    assert await provider._resolve_media_item(payload) is None
    mass.music.tracks.enrich_provider_mappings.assert_awaited_once()


async def test_track_resolution_rejects_core_loose_match() -> None:
    """Core loose title matches are insufficient to open a shared track."""
    provider = _provider()
    mass = _mass(provider)
    candidate = _track(item_id="loose")
    mass.music.get_controller.return_value.get_item_by_external_id = AsyncMock(return_value=None)
    mass.music.tracks.enrich_provider_mappings = AsyncMock(
        return_value=SimpleNamespace(matches=(_core_match(candidate, TrackMatchConfidence.LOOSE),))
    )

    assert (
        await provider._resolve_media_item(
            SharePayload(type="track", title="Track", artist="Artist")
        )
        is None
    )
    assert (
        mass.music.tracks.enrich_provider_mappings.await_args.kwargs["minimum_confidence"]
        == TrackMatchConfidence.LIKELY
    )


async def test_track_resolution_selects_highest_core_confidence() -> None:
    """The best confidence tier wins, with deterministic mapping selection."""
    provider = _provider()
    mass = _mass(provider)
    lower = _track(item_id="lower", artist="Artist")
    higher = _track(item_id="higher", artist="Artist")
    mass.music.tracks.enrich_provider_mappings = AsyncMock(
        return_value=SimpleNamespace(
            matches=(
                _core_match(lower, TrackMatchConfidence.LIKELY),
                _core_match(higher, TrackMatchConfidence.EXACT),
            )
        )
    )

    assert (
        await provider._resolve_media_item(
            SharePayload(type="track", title="Track", artist="Artist")
        )
        is higher
    )


async def test_search_provider_scope_filters_disabled_builtin_and_hidden_sources() -> None:
    """Only enabled music providers visible to the caller enter Core search."""
    provider = _provider()
    mass = _mass(provider)
    user = object()
    mass.config.get_provider_configs = AsyncMock(
        return_value=[
            SimpleNamespace(instance_id="tidal--visible", domain="tidal", enabled=True),
            SimpleNamespace(instance_id="jellyfin--visible", domain="jellyfin", enabled=True),
            SimpleNamespace(instance_id="tidal--disabled", domain="tidal", enabled=False),
            SimpleNamespace(instance_id="builtin", domain="builtin", enabled=True),
        ]
    )
    with (
        patch(
            "music_assistant.providers.share_provider.resolver.get_current_user",
            return_value=user,
        ),
        patch(
            "music_assistant.providers.share_provider.resolver.visible_music_sources",
            return_value=["tidal--visible", "jellyfin--visible"],
        ),
    ):
        provider_instances = await provider._searchable_music_provider_instances()

    assert provider_instances == {"tidal--visible", "jellyfin--visible"}
    mass.config.get_provider_configs.assert_awaited_once_with(provider_type=ProviderType.MUSIC)


async def test_album_search_requires_matching_edition() -> None:
    """Album search uses core comparison and rejects unknown or different editions."""
    provider = _provider()
    standard = Album(
        item_id="standard",
        provider="catalog--1",
        name="Album",
        version="Standard",
        provider_mappings=set(),
        artists=UniqueList(
            [
                Artist(
                    item_id="artist", provider="catalog--1", name="Artist", provider_mappings=set()
                )
            ]
        ),
    )
    deluxe = replace(standard, item_id="deluxe", version="Deluxe")
    results = MagicMock(tracks=[], albums=[standard, deluxe], artists=[])
    _mass(provider).music.search = AsyncMock(return_value=results)
    payload = SharePayload(type="album", title="Album", artist="Artist", version="Deluxe")

    assert await provider._resolve_media_item(payload) is deluxe

    results.albums = [replace(deluxe, version="")]
    assert await provider._resolve_media_item(payload) is None


async def test_album_search_does_not_choose_between_duplicate_core_matches() -> None:
    """Duplicate albums with equal core evidence remain unresolved."""
    provider = _provider()
    album = Album(
        item_id="album-one",
        provider="catalog--1",
        name="Album",
        provider_mappings=set(),
        artists=UniqueList(
            [
                Artist(
                    item_id="artist", provider="catalog--1", name="Artist", provider_mappings=set()
                )
            ]
        ),
    )
    duplicate = replace(album, item_id="album-two")
    _mass(provider).music.search = AsyncMock(
        return_value=MagicMock(albums=[album, duplicate], tracks=[], artists=[])
    )

    assert (
        await provider._resolve_media_item(
            SharePayload(type="album", title="Album", artist="Artist")
        )
        is None
    )


async def test_artist_search_does_not_choose_between_homonyms() -> None:
    """Artists with the same name but no matching ID remain unresolved."""
    provider = _provider()
    artist = Artist(
        item_id="artist-one",
        provider="catalog--1",
        name="Artist",
        provider_mappings=set(),
    )
    homonym = replace(artist, item_id="artist-two")
    _mass(provider).music.search = AsyncMock(
        return_value=MagicMock(artists=[artist, homonym], albums=[], tracks=[])
    )

    assert await provider._resolve_media_item(SharePayload(type="artist", title="Artist")) is None


async def test_album_search_collapses_matches_with_the_same_mbid() -> None:
    """Cross-provider album results with the same MusicBrainz ID are one match."""
    provider = _provider()
    album = Album(
        item_id="album-one",
        provider="catalog-one",
        name="Album",
        external_ids={(ExternalID.MB_ALBUM, "shared-album-mbid")},
        provider_mappings=set(),
        artists=UniqueList(
            [
                Artist(
                    item_id="artist-one",
                    provider="catalog-one",
                    name="Artist",
                    provider_mappings=set(),
                )
            ]
        ),
    )
    duplicate = replace(album, item_id="album-two", provider="catalog-two")
    _mass(provider).music.search = AsyncMock(
        return_value=MagicMock(albums=[album, duplicate], tracks=[], artists=[])
    )

    result = await provider._resolve_media_item(SharePayload(type="album", title="Album"))

    assert result is album


async def test_artist_search_collapses_matches_with_the_same_mbid() -> None:
    """Cross-provider artist results with the same MusicBrainz ID are one match."""
    provider = _provider()
    artist = Artist(
        item_id="artist-one",
        provider="catalog-one",
        name="Artist",
        external_ids={(ExternalID.MB_ARTIST, "shared-artist-mbid")},
        provider_mappings=set(),
    )
    duplicate = replace(artist, item_id="artist-two", provider="catalog-two")
    _mass(provider).music.search = AsyncMock(
        return_value=MagicMock(artists=[artist, duplicate], albums=[], tracks=[])
    )

    result = await provider._resolve_media_item(SharePayload(type="artist", title="Artist"))

    assert result is artist


@pytest.mark.parametrize("use_barcode", [False, True])
async def test_album_resolution_accepts_ep_title_variant_across_providers(
    use_barcode: bool,
) -> None:
    """Album titles with an EP suffix match across providers in search and barcode paths."""
    provider = _provider()
    mass = _mass(provider)
    candidate = Album(
        item_id="album",
        provider="qobuz--1",
        name="Album A - EP",
        artists=UniqueList(
            [
                Artist(
                    item_id="artist",
                    provider="qobuz--1",
                    name="Artist",
                    provider_mappings=set(),
                )
            ]
        ),
        provider_mappings=set(),
    )
    controller = mass.music.get_controller.return_value
    controller.get_item_by_external_id = AsyncMock(return_value=candidate)
    mass.music.search = AsyncMock(return_value=MagicMock(albums=[candidate], tracks=[], artists=[]))
    payload = SharePayload(
        type="album",
        title="Album A",
        artist="Artist",
        barcode=BARCODE if use_barcode else None,
    )

    result = await provider._resolve_media_item(payload)

    assert result is candidate
    if use_barcode:
        mass.music.search.assert_not_called()
    else:
        mass.music.search.assert_awaited_once()


async def test_external_id_resolution_skips_unavailable_track_for_available_fallback() -> None:
    """An unavailable library ID hit does not block a playable MBID result."""
    provider = _provider()
    mass = _mass(provider)
    artist = Artist(
        item_id="artist",
        provider="catalog--1",
        name="Artist",
        provider_mappings=set(),
    )
    unavailable = Track(
        item_id="unavailable-track",
        provider="library",
        name="Song",
        duration=218,
        external_ids={(ExternalID.ISRC, ISRC)},
        provider_mappings={
            ProviderMapping(
                item_id="unavailable-track",
                provider_domain="catalog",
                provider_instance="catalog--unavailable",
                available=False,
            )
        },
        artists=UniqueList([artist]),
    )
    available = Track(
        item_id="available-track",
        provider="catalog--1",
        name="Song",
        duration=218,
        external_ids={
            (ExternalID.ISRC, ISRC),
            (ExternalID.MB_RECORDING, "recording-mbid"),
        },
        provider_mappings={
            ProviderMapping(
                item_id="available-track",
                provider_domain="catalog",
                provider_instance="catalog--1",
                available=True,
            )
        },
        artists=UniqueList([artist]),
    )
    controller = mass.music.get_controller.return_value
    controller.get_item_by_external_id = AsyncMock(side_effect=[unavailable, available])
    mass.music.tracks.get = AsyncMock(return_value=available)
    payload = SharePayload(
        type="track",
        title="Song",
        artist="Artist",
        artists=["Artist"],
        isrc=ISRC,
        mbid="recording-mbid",
        duration=218,
    )

    result = await provider.resolve_share_link(payload.to_base64())

    assert result is available
    assert controller.get_item_by_external_id.await_count == 2
    mass.music.tracks.get.assert_awaited_once_with(
        available.item_id, available.provider, allow_update_metadata=False
    )
