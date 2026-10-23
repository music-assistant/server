"""Tests for provider-agnostic share links."""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import replace
from typing import Literal, cast
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from music_assistant_models.auth import Scope
from music_assistant_models.enums import ExternalID, MediaType, PlaylistMatchPolicy
from music_assistant_models.errors import (
    InvalidDataError,
    ProviderUnavailableError,
    RetriesExhausted,
)
from music_assistant_models.media_items import Artist, ItemMapping, Track, UniqueList

from music_assistant.helpers.compare import compare_track
from music_assistant.helpers.playlists import (
    AlbumInfo,
    ArtistInfo,
    PlaylistItem,
    construct_media_item_from_playlist_item,
    parse_m3u,
)
from music_assistant.providers.share_provider import (
    SharePayload,
    ShareProvider,
    TrackStub,
)
from music_assistant.providers.share_provider.enrichment import (
    ShareLinkEnricher,
    _ShareHTTPClient,
)
from music_assistant.providers.share_provider.links import (
    build_provider_links,
    build_provider_refs,
    compact_artwork_ref,
    expand_artwork_ref,
    expand_provider_ref,
)

ISRC = "USRC17607839"
BARCODE = "00724354283857"


def _provider() -> ShareProvider:
    """Build a share provider with mocked server services."""
    mass = MagicMock()
    manifest = MagicMock()
    manifest.domain = "share_provider"
    config = MagicMock()
    config.instance_id = "share_provider--test"
    config.name = "Share Links"
    config.get_value.return_value = "GLOBAL"
    provider = ShareProvider(mass, manifest, config)
    provider._link_enricher = ShareLinkEnricher(mass, config.instance_id)
    return provider


def _mass(provider: ShareProvider) -> MagicMock:
    return cast("MagicMock", provider.mass)


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
    item = MagicMock()
    controller.get_item_by_external_id = AsyncMock(return_value=item)
    _mass(provider).music.tracks.get = AsyncMock(return_value=item)
    encoded = SharePayload(
        type=cast("Literal['track', 'album', 'artist', 'playlist']", media_type.value),
        title="Item",
        isrc=external_ids.get("isrc"),
        mbid=external_ids.get("mbid"),
        mb_track=external_ids.get("mb_track"),
        barcode=external_ids.get("barcode"),
    ).to_base64()

    result = await provider.resolve_share_link(encoded)

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
    encoded = SharePayload(type="track", title="Track", isrc=ISRC).to_base64()

    assert await provider.resolve_share_link(encoded, allow_search=False) is None
    mass.music.search.assert_not_called()


async def test_resolve_without_search_returns_id_match() -> None:
    """ID matches include full track and album details without text search."""
    provider = _provider()
    mass = _mass(provider)
    item = MagicMock()
    full_item = MagicMock()
    mass.music.tracks.get = AsyncMock(return_value=full_item)
    mass.music.get_controller.return_value.get_item_by_external_id = AsyncMock(return_value=item)
    encoded = SharePayload(type="track", title="Track", isrc=ISRC).to_base64()

    assert await provider.resolve_share_link(encoded, allow_search=False) is full_item
    mass.music.tracks.get.assert_awaited_once_with(
        item.item_id, item.provider, allow_update_metadata=False
    )
    mass.music.search.assert_not_called()


async def test_resolve_skips_id_match_when_full_track_details_are_unavailable() -> None:
    """An ID hit whose detail fetch fails remains a miss for preview fallback."""
    provider = _provider()
    mass = _mass(provider)
    mass.music.get_controller.return_value.get_item_by_external_id = AsyncMock(
        return_value=MagicMock(item_id="source-id", provider="source--1")
    )
    mass.music.tracks.get = AsyncMock(side_effect=ProviderUnavailableError("offline"))
    encoded = SharePayload(type="track", title="Track", isrc=ISRC).to_base64()

    assert await provider.resolve_share_link(encoded, allow_search=False) is None
    mass.music.tracks.get.assert_awaited_once()
    mass.music.search.assert_not_called()


@pytest.mark.parametrize("media_type", [MediaType.TRACK, MediaType.ALBUM, MediaType.ARTIST])
@pytest.mark.parametrize("include_match", [False, True])
async def test_text_search_ignores_unrelated_first_result(
    media_type: MediaType, include_match: bool
) -> None:
    """Search fallback returns a metadata match, not merely the first result."""
    provider = _provider()
    mass = _mass(provider)
    first = MagicMock()
    first.name = "Unrelated result"
    first.artist_str = "Artist"
    matching = MagicMock()
    matching.name = "Shared title"
    matching.artist_str = "Artist"
    field = {MediaType.TRACK: "tracks", MediaType.ALBUM: "albums", MediaType.ARTIST: "artists"}[
        media_type
    ]
    results = MagicMock(tracks=[], albums=[], artists=[])
    setattr(results, field, [first, *([matching] if include_match else [])])
    mass.music.search = AsyncMock(return_value=results)
    payload = SharePayload(
        type=cast("Literal['track', 'album', 'artist']", media_type.value),
        title="Shared title",
        artist="Artist" if media_type is not MediaType.ARTIST else None,
    )

    result = await provider._resolve_media_item(payload)

    assert result is (matching if include_match else None)


async def test_resolve_falls_back_to_search_after_external_id_miss() -> None:
    """A provider-agnostic title search remains the fallback when IDs do not match."""
    provider = _provider()
    mass = _mass(provider)
    controller = mass.music.get_controller.return_value
    controller.get_item_by_external_id = AsyncMock(return_value=None)
    item = MagicMock()
    item.name = "Track"
    item.artist_str = "Artist"
    mass.music.search = AsyncMock(return_value=MagicMock(tracks=[item], albums=[], artists=[]))
    encoded = SharePayload(type="track", title="Track", artist="Artist", isrc=ISRC).to_base64()

    result = await provider.resolve_share_link(encoded)

    assert result is item
    mass.music.search.assert_awaited_once_with(
        "Artist Track", media_types=[MediaType.TRACK], limit=5
    )


async def test_resolve_falls_back_to_search_after_external_id_provider_failure() -> None:
    """A provider lookup failure does not block the global text-search fallback."""
    provider = _provider()
    mass = _mass(provider)
    controller = mass.music.get_controller.return_value
    controller.get_item_by_external_id = AsyncMock(
        side_effect=ProviderUnavailableError("provider offline")
    )
    item = MagicMock()
    item.name = "Track"
    item.artist_str = "Artist"
    mass.music.search = AsyncMock(return_value=MagicMock(tracks=[item], albums=[], artists=[]))
    encoded = SharePayload(type="track", title="Track", artist="Artist", isrc=ISRC).to_base64()

    result = await provider.resolve_share_link(encoded)

    assert result is item
    mass.music.search.assert_awaited_once_with(
        "Artist Track", media_types=[MediaType.TRACK], limit=5
    )


async def test_build_album_payload_includes_barcode_mbid_and_provider_link() -> None:
    """Album shares carry the identifiers and provider links available to the sender."""
    provider = _provider()
    album = MagicMock()
    album.media_type = MediaType.ALBUM
    album.name = "Album"
    album.artist_str = "Artist"
    album.image = None
    album.provider_mappings = [MagicMock(provider_domain="spotify", item_id="album/123")]
    album.get_external_id.side_effect = {
        ExternalID.MB_ALBUM: "album-mbid",
        ExternalID.BARCODE: BARCODE,
    }.__getitem__

    payload = await provider._build_payload(album)

    assert payload.type == "album"
    assert payload.mbid == "album-mbid"
    assert payload.barcode == BARCODE
    assert payload.provider_refs == {"spotify": "/album/album%2F123"}


async def test_playlist_export_includes_all_tracks_duplicates_and_mapped_album_names() -> None:
    """Large playlist shares preserve every entry and compact album metadata."""
    provider = _provider()
    album = ItemMapping(item_id="album", provider="test", name="Album", media_type=MediaType.ALBUM)
    tracks = [
        Track(
            item_id=str(index),
            provider="test",
            name=f"Track {index}",
            album=album,
            provider_mappings=set(),
        )
        for index in range(501)
    ]
    tracks.append(tracks[0])
    _mass(provider).music.playlists.tracks = AsyncMock(return_value=tracks)
    playlist = MagicMock(item_id="playlist", provider="test")

    exported = await provider._build_playlist_tracks(playlist)

    assert len(exported) == 502
    assert exported[-1].title == exported[0].title
    assert exported[500].title == "Track 500"
    assert all(track.album == "Album" for track in exported)
    _mass(provider).music.playlists.tracks.assert_awaited_once_with("playlist", "test", limit=0)


async def test_share_base_url_is_an_instance_config_option() -> None:
    """The config controller can discover the share URL option on the provider."""
    entries = await _provider().get_config_entries()
    assert len(entries) == 1
    assert entries[0].key == "share_base_url"
    assert entries[0].default_value == "https://share.music-assistant.io"


@pytest.mark.parametrize(("status", "retry_after"), [(429, "37"), (503, None)])
async def test_external_http_service_throttles_transient_statuses(
    status: int, retry_after: str | None
) -> None:
    """Transient service failures are retried and rate-limit cooldown is shared."""
    mass = MagicMock()
    response = MagicMock(status=status, headers={"Retry-After": retry_after} if retry_after else {})
    response.json = AsyncMock(return_value={})
    request_context = MagicMock()
    request_context.__aenter__ = AsyncMock(return_value=response)
    request_context.__aexit__ = AsyncMock(return_value=False)
    client = _ShareHTTPClient(mass, "Deezer")
    client.throttler.retry_attempts = 1
    with (
        patch.object(
            mass.http_session,
            "get",
            MagicMock(return_value=request_context),
        ) as get_request,
        patch.object(
            client.throttler.throttler,
            "acquire",
            AsyncMock(return_value=0),
        ),
        pytest.raises(RetriesExhausted),
    ):
        await client.get_json("https://api.deezer.com/test")

    get_request.assert_called_once()
    if status == 429:
        assert client.throttler.cooldown_remaining >= 36


@pytest.mark.parametrize(
    ("method_name", "arguments"),
    [
        ("_get_deezer_track_url", (ISRC,)),
        ("_search_deezer_track_url", ('track:"Song"', "Song", "Artist")),
        ("_get_apple_music_track_url", ("Artist Song", "Song", "Artist")),
        ("_get_apple_music_track_url_by_id", ("12345",)),
    ],
)
async def test_transient_http_failures_are_not_cached(
    method_name: str, arguments: tuple[str, ...]
) -> None:
    """A temporary service outage is retried on the next cached lookup."""
    provider = _provider()
    mass = _mass(provider)
    mass.cache.get_with_freshness = AsyncMock(return_value=(None, False, False))
    mass.create_task = lambda coro, **_kwargs: asyncio.create_task(coro)
    response = MagicMock(status=503, headers={})
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=False)
    provider._link_enricher._deezer_client = _ShareHTTPClient(mass, "Deezer")
    provider._link_enricher._apple_music_client = _ShareHTTPClient(mass, "Apple Music")
    provider._link_enricher._deezer_client.throttler.retry_attempts = 1
    provider._link_enricher._apple_music_client.throttler.retry_attempts = 1
    method = getattr(provider._link_enricher, method_name)

    with (
        patch.object(mass.http_session, "get", MagicMock(return_value=context)) as http_get,
        patch.object(
            provider._link_enricher._deezer_client.throttler.throttler,
            "acquire",
            AsyncMock(return_value=0),
        ),
        patch.object(
            provider._link_enricher._apple_music_client.throttler.throttler,
            "acquire",
            AsyncMock(return_value=0),
        ),
    ):
        for _ in range(2):
            with pytest.raises(RetriesExhausted):
                await method(*arguments)

    assert http_get.call_count == 2


@pytest.mark.parametrize("include_match", [False, True])
async def test_ytmusic_enrichment_rejects_unrelated_search_results(include_match: bool) -> None:
    """YouTube Music links are only included after matching title and artist."""
    provider = _provider()
    wrong_title = MagicMock(name="wrong")
    wrong_title.name = "Different song"
    wrong_title.artist_str = "Artist"
    wrong_title.provider_mappings = [MagicMock(provider_domain="ytmusic", item_id="wrong")]
    wrong_artist = MagicMock()
    wrong_artist.name = "Song"
    wrong_artist.artist_str = "Other performer"
    wrong_artist.provider_mappings = [MagicMock(provider_domain="ytmusic", item_id="wrong-artist")]
    match = MagicMock()
    match.name = "Song"
    match.artist_str = "Artist"
    match.provider_mappings = [MagicMock(provider_domain="ytmusic", item_id="correct")]
    results = [wrong_title, wrong_artist, *([match] if include_match else [])]
    _mass(provider).music.search = AsyncMock(return_value=MagicMock(tracks=results))
    refs = {"deezer": "/track/1", "apple_music": "/album/1?i=2"}

    await provider._link_enricher.enrich(refs, None, "Song", "Artist")

    if include_match:
        assert refs["ytmusic"] == "/watch?v=correct"
    else:
        assert "ytmusic" not in refs


def test_build_provider_links_keeps_apple_music_canonical_url() -> None:
    """Apple mappings carry a canonical storefront URL that must not be rebuilt."""
    item = MagicMock()
    item.provider_mappings = [
        MagicMock(
            provider_domain="apple_music",
            item_id="123456789",
            url="https://music.apple.com/us/album/album-name/987654321?i=123456789",
        )
    ]

    assert build_provider_links(item, "track") == {
        "apple_music": "https://music.apple.com/us/album/album-name/987654321?i=123456789"
    }


def test_build_provider_refs_omits_provider_hosts() -> None:
    """Share payload references keep localized paths without full URLs."""
    item = MagicMock()
    item.provider_mappings = [
        MagicMock(
            provider_domain="apple_music",
            item_id="123456789",
            url="https://music.apple.com/at/album/a-little-bit-off/1487244856?i=1487244862",
        ),
        MagicMock(provider_domain="spotify", item_id="track-id", url=None),
    ]

    assert build_provider_refs(item, "track") == {
        "apple_music": "/at/album/1487244856?i=1487244862",
        "spotify": "/track/track-id",
    }


def test_build_provider_links_skips_qobuz_artist_name_mapping() -> None:
    """Qobuz artist links require numeric catalog IDs, not performer names."""
    item = MagicMock()
    item.provider_mappings = [
        MagicMock(provider_domain="qobuz", item_id="Example Performer", url=None),
        MagicMock(provider_domain="qobuz", item_id="12345", url=None),
    ]
    assert build_provider_links(item, "artist") == {"qobuz": "https://open.qobuz.com/artist/12345"}

    item.provider_mappings = [
        MagicMock(
            provider_domain="qobuz",
            item_id="Artist Name",
            url="https://open.qobuz.com/artist/54321",
        ),
    ]
    assert build_provider_links(item, "artist") == {"qobuz": "https://open.qobuz.com/artist/54321"}


def test_payload_uses_compact_provider_and_artwork_refs_without_version_field() -> None:
    """Share metadata is encoded without a redundant protocol-version field."""
    apple_url = "https://music.apple.com/at/album/123?i=456"
    art_url = "https://is1-ssl.mzstatic.com/image/thumb/cover/1000x1000bb.jpg"
    art_ref = compact_artwork_ref(art_url)
    assert art_ref is not None
    payload = SharePayload(
        type="track",
        title="Track",
        art_ref=art_ref,
        provider_refs={
            "apple_music": build_provider_refs(
                MagicMock(
                    provider_mappings=[
                        MagicMock(
                            provider_domain="apple_music",
                            item_id="456",
                            url=apple_url,
                        )
                    ]
                ),
                "track",
            )["apple_music"]
        },
    )

    encoded = payload.to_base64()
    decoded = SharePayload.from_base64(encoded)
    decoded_json = base64.urlsafe_b64decode(encoded).decode()

    assert decoded.art_ref == art_ref
    assert decoded.provider_refs == {
        "apple_music": "/at/album/123?i=456",
    }
    assert expand_artwork_ref(art_ref) == art_url
    assert expand_provider_ref("apple_music", decoded.provider_refs["apple_music"]) == apple_url
    assert "https://" not in decoded_json
    assert '"v"' not in decoded_json


def test_build_provider_links_ignores_non_apple_mapping_urls() -> None:
    """Only canonical Apple Music URLs are accepted from mapping.url."""
    item = MagicMock()
    item.provider_mappings = [
        MagicMock(
            provider_domain="apple_music",
            item_id="123456789",
            url="javascript:alert(1)",
        )
    ]

    assert build_provider_links(item, "track") == {}


async def test_enrich_prefers_apple_music_catalog_id_lookup() -> None:
    """The Apple source track ID reaches the payload's canonical URL lookup."""
    provider = _provider()
    apple_url = "https://music.apple.com/us/album/example/987654321?i=123456789"
    track = MagicMock()
    track.media_type = MediaType.TRACK
    track.name = "A Little Bit Off"
    track.artist_str = "Five Finger Death Punch"
    track.album = None
    track.image = None
    track.provider_mappings = [
        MagicMock(provider_domain="apple_music", item_id="123456789", url=None)
    ]
    track.get_external_id.return_value = None
    mass = _mass(provider)
    mass.music.search = AsyncMock(return_value=MagicMock(tracks=[]))

    with (
        patch.object(
            provider._link_enricher,
            "_get_deezer_track_url",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch.object(
            provider._link_enricher,
            "_search_deezer_track_url",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch.object(
            provider._link_enricher,
            "_get_apple_music_track_url_by_id",
            new_callable=AsyncMock,
            return_value=apple_url,
        ) as apple_id_lookup,
        patch.object(
            provider._link_enricher,
            "_get_apple_music_track_url",
            new_callable=AsyncMock,
            return_value=None,
        ) as apple_search,
    ):
        payload = await provider._build_payload(track)

        assert payload.provider_refs == {"apple_music": "/us/album/987654321?i=123456789"}
        apple_id_lookup.assert_awaited_once_with("123456789")
        apple_search.assert_not_awaited()


async def test_playlist_resolution_is_read_only() -> None:
    """Resolving a playlist preview never creates or edits a library playlist."""
    provider = _provider()
    encoded = SharePayload(
        type="playlist",
        title="Playlist",
        tracks=[TrackStub("Track", "Artist", ISRC)],
    ).to_base64()

    assert await provider.resolve_share_link(encoded) is None
    _mass(provider).music.playlists.create_playlist.assert_not_called()
    _mass(provider).music.playlists.add_playlist_tracks.assert_not_called()


async def test_import_shared_playlist_uses_existing_matching_importer() -> None:
    """A confirmed playlist import preserves metadata for the built-in matcher."""
    provider = _provider()
    playlist = MagicMock()
    mass = _mass(provider)
    mass.music.playlists.import_playlist = AsyncMock(return_value=playlist)
    encoded = SharePayload(
        type="playlist",
        title="Shared Mix",
        tracks=[
            TrackStub(
                title="Track",
                artist="Artist",
                isrc=ISRC,
                mbid="recording-mbid",
                mb_track="release-track-mbid",
                album="Album",
                duration=218,
            )
        ],
    ).to_base64()

    result = await provider.import_shared_playlist(encoded)

    assert result is playlist
    mass.music.playlists.import_playlist.assert_awaited_once()
    await_args = mass.music.playlists.import_playlist.await_args
    assert await_args is not None
    m3u_data = await_args.args[0]
    imported_items = parse_m3u(m3u_data)
    assert len(imported_items) == 1
    imported_track = SharePayload.from_base64(imported_items[0].path.removeprefix("share://track/"))
    assert imported_track.type == "track"
    assert imported_track.title == "Track"
    assert imported_track.artist == "Artist"
    assert imported_track.album == "Album"
    assert imported_track.isrc == ISRC
    assert imported_track.mbid == "recording-mbid"
    assert imported_track.mb_track == "release-track-mbid"
    assert imported_items[0].length == "218"
    decoded_playlist = SharePayload.from_base64(encoded)
    assert decoded_playlist.tracks is not None
    assert decoded_playlist.tracks[0].duration == 218
    assert imported_items[0].metadata == {
        "media_type": "track",
        "name": "Track",
        "isrc": ISRC,
        "mbid": "recording-mbid",
        "mb_track": "release-track-mbid",
    }
    assert imported_items[0].artists[0].name == "Artist"
    assert imported_items[0].album is not None
    assert imported_items[0].album.name == "Album"
    shared_track = construct_media_item_from_playlist_item(imported_items[0], mass)
    shared_track_without_duration = construct_media_item_from_playlist_item(
        PlaylistItem(
            path="share://track/no-duration",
            title="Track",
            metadata={"media_type": "track", "name": "Track", "isrc": ISRC},
            artists=[ArtistInfo("Artist", "", "", "")],
            album=AlbumInfo("Album", "", "", ""),
        ),
        mass,
    )
    catalog_track = Track(
        item_id="catalog-track",
        provider="catalog--1",
        name="Track",
        duration=218,
        external_ids={(ExternalID.ISRC, ISRC)},
        provider_mappings=set(),
        artists=UniqueList(
            [
                Artist(
                    item_id="artist", provider="catalog--1", name="Artist", provider_mappings=set()
                )
            ]
        ),
    )
    assert isinstance(shared_track, Track)
    assert compare_track(catalog_track, shared_track)
    assert isinstance(shared_track_without_duration, Track)
    assert not compare_track(catalog_track, shared_track_without_duration)
    mass.music.playlists.import_playlist.assert_awaited_once_with(
        m3u_data,
        library_matching=True,
        match_policy=PlaylistMatchPolicy.SAME_RECORDING,
    )


async def test_import_isrc_only_track_matches_when_duration_is_preserved() -> None:
    """ISRC-only SAME_RECORDING matching needs the original track duration."""
    provider = _provider()
    mass = _mass(provider)
    mass.music.playlists.import_playlist = AsyncMock(return_value=MagicMock())
    encoded = SharePayload(
        type="playlist",
        title="Shared Mix",
        tracks=[TrackStub(title="Track", artist="Artist", isrc=ISRC, duration=218)],
    ).to_base64()

    await provider.import_shared_playlist(encoded)
    imported_m3u = mass.music.playlists.import_playlist.await_args.args[0]
    imported_item = parse_m3u(imported_m3u)[0]
    shared_track = construct_media_item_from_playlist_item(imported_item, mass)
    shared_item_without_duration = construct_media_item_from_playlist_item(
        replace(imported_item, length=None), mass
    )
    catalog_track = Track(
        item_id="catalog-track",
        provider="catalog--1",
        name="Track",
        duration=218,
        external_ids={(ExternalID.ISRC, ISRC)},
        provider_mappings=set(),
        artists=UniqueList(
            [
                Artist(
                    item_id="artist", provider="catalog--1", name="Artist", provider_mappings=set()
                )
            ]
        ),
    )

    assert isinstance(shared_track, Track)
    assert isinstance(shared_item_without_duration, Track)
    assert not shared_track.external_ids.intersection(
        {(ExternalID.MB_RECORDING, "recording-mbid"), (ExternalID.MB_TRACK, "release-track-mbid")}
    )
    assert compare_track(catalog_track, shared_track)
    assert not compare_track(catalog_track, shared_item_without_duration)


async def test_import_shared_playlist_rejects_non_playlist_payload() -> None:
    """Only an explicit playlist payload can reach the write-capable importer."""
    provider = _provider()
    encoded = SharePayload(type="track", title="Track").to_base64()

    with pytest.raises(InvalidDataError):
        await provider.import_shared_playlist(encoded)

    _mass(provider).music.playlists.import_playlist.assert_not_called()


async def test_api_commands_require_library_read_and_unregister_on_unload() -> None:
    """Share commands are authenticated read operations and unregister cleanly."""
    provider = _provider()
    unregister_generate = MagicMock()
    unregister_resolve = MagicMock()
    unregister_import = MagicMock()
    unregister_preview = MagicMock()
    mass = _mass(provider)
    mass.register_api_command.side_effect = [
        unregister_generate,
        unregister_resolve,
        unregister_import,
        unregister_preview,
    ]

    await provider.handle_async_init()
    await provider.unload()

    assert mass.register_api_command.call_args_list == [
        call("share/generate", provider.generate_share_link, required_scope=Scope.LIBRARY_READ),
        call("share/resolve", provider.resolve_share_link, required_scope=Scope.LIBRARY_READ),
        call(
            "share/import_playlist",
            provider.import_shared_playlist,
            required_scope=Scope.LIBRARY_WRITE,
        ),
        call(
            "share/preview_playlist",
            provider.preview_shared_playlist,
            required_scope=Scope.LIBRARY_READ,
        ),
    ]
    unregister_generate.assert_called_once_with()
    unregister_resolve.assert_called_once_with()
    unregister_import.assert_called_once_with()
    unregister_preview.assert_called_once_with()
    mass.webserver.register_dynamic_route.assert_not_called()


async def test_preview_keeps_unmatched_and_duplicate_tracks_in_original_order() -> None:
    """Preview entries preserve identity and position without library writes."""
    provider = _provider()
    known = Track(item_id="42", provider="library", name="Known", provider_mappings=set())
    encoded = SharePayload(
        type="playlist",
        title="Playlist",
        tracks=[
            TrackStub(title="Known"),
            TrackStub(title="Missing", artist="Artist", album="Album"),
            TrackStub(title="Known"),
        ],
    ).to_base64()
    resolver = AsyncMock(side_effect=[known, None, known])
    with patch.object(ShareProvider, "_resolve_media_item", resolver):
        tracks = await provider.preview_shared_playlist(encoded)

    assert [track.name for track in tracks] == ["Known", "Missing", "Known"]
    assert [track.position for track in tracks] == [1, 2, 3]
    assert tracks[0] is not tracks[2]
    assert known.position is None
    assert not tracks[1].available
    assert tracks[1].provider_mappings == set()
    assert tracks[1].item_id.startswith("share-preview://track/")
    assert tracks[1].artists[0].name == "Artist"
    assert tracks[1].album is not None
    assert tracks[1].album.name == "Album"
    assert all(kwargs == {"allow_search": False} for _, kwargs in resolver.call_args_list)
    _mass(provider).music.playlists.import_playlist.assert_not_called()


async def test_preview_only_resolves_the_requested_block() -> None:
    """Scrolling resolves only the requested portion of a shared playlist."""
    provider = _provider()
    encoded = SharePayload(
        type="playlist",
        title="Playlist",
        tracks=[TrackStub(title=f"Track {index}") for index in range(55)],
    ).to_base64()
    resolver = AsyncMock(return_value=None)
    with patch.object(ShareProvider, "_resolve_media_item", resolver):
        tracks = await provider.preview_shared_playlist(encoded, offset=50, limit=50)
    assert [track.position for track in tracks] == [51, 52, 53, 54, 55]
    assert resolver.await_count == 5


@pytest.mark.parametrize(("offset", "limit"), [(-1, 50), (0, 0)])
async def test_preview_rejects_invalid_ranges(offset: int, limit: int) -> None:
    """Invalid preview ranges are rejected before resolving any tracks."""
    provider = _provider()
    with pytest.raises(InvalidDataError):
        await provider.preview_shared_playlist("", offset=offset, limit=limit)


async def test_api_registration_failure_unregisters_prior_commands() -> None:
    """A failed later registration removes commands already registered."""
    provider = _provider()
    unregister_generate = MagicMock()
    _mass(provider).register_api_command.side_effect = [
        unregister_generate,
        RuntimeError("command already registered"),
    ]

    with pytest.raises(RuntimeError, match="command already registered"):
        await provider.handle_async_init()

    unregister_generate.assert_called_once_with()


async def test_payload_rejects_unsupported_media_types() -> None:
    """Reject unknown media types before dispatching a share payload."""
    encoded = base64.urlsafe_b64encode(
        json.dumps({"type": "podcast", "title": "Podcast"}).encode()
    ).decode()

    with pytest.raises(ValueError, match="Unsupported share payload"):
        SharePayload.from_base64(encoded)


def test_payload_does_not_require_a_version_field() -> None:
    """The share payload contains no protocol-version marker."""
    encoded = base64.urlsafe_b64encode(
        json.dumps({"type": "track", "title": "Track"}).encode()
    ).decode()

    assert SharePayload.from_base64(encoded).title == "Track"


@pytest.mark.parametrize("duration", [True, -1, "218", 1.5])
def test_payload_rejects_invalid_playlist_track_duration(duration: object) -> None:
    """Playlist durations are nonnegative integer seconds when present."""
    encoded = base64.urlsafe_b64encode(
        json.dumps(
            {
                "type": "playlist",
                "title": "Mix",
                "tracks": [{"title": "Track", "duration": duration}],
            }
        ).encode()
    ).decode()

    with pytest.raises(ValueError, match="Invalid share playlist track duration"):
        SharePayload.from_base64(encoded)


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        (
            "provider_refs",
            {"spotify": "https://evil.example/track/1"},
            "Invalid share provider references",
        ),
        ("art_ref", "x:evil.example:/art.jpg", "Invalid share artwork reference"),
    ],
)
def test_payload_rejects_unsafe_urls(field: str, value: object, error: str) -> None:
    """Share payloads cannot impersonate providers or load unsafe artwork."""
    encoded = base64.urlsafe_b64encode(
        json.dumps({"type": "track", "title": "Track", field: value}).encode()
    ).decode()

    with pytest.raises(ValueError, match=error):
        SharePayload.from_base64(encoded)
