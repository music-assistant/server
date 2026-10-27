"""Test share playlists tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from music_assistant_models.auth import Scope
from music_assistant_models.enums import ExternalID, MediaType, PlaylistMatchPolicy, ProviderType
from music_assistant_models.errors import (
    InvalidDataError,
    ProviderUnavailableError,
)
from music_assistant_models.media_items import (
    Artist,
    ItemMapping,
    Playlist,
    ProviderMapping,
    Radio,
    Track,
    UniqueList,
)

from music_assistant.helpers.compare import (
    TrackMatchConfidence,
    compare_track,
    compare_track_evidence,
)
from music_assistant.helpers.playlists import (
    AlbumInfo,
    ArtistInfo,
    PlaylistItem,
    construct_media_item_from_playlist_item,
    generate_m3u,
    parse_m3u,
)
from music_assistant.providers.share_provider import (
    SharePayload,
    ShareProvider,
    TrackStub,
)

from .helpers import ISRC, _core_match, _mass, _provider, _track


async def test_playlist_preview_uses_core_track_matcher() -> None:
    """Preview matching uses the same Core evidence path as share resolution."""
    provider = _provider()
    mass = _mass(provider)
    matched = _track(item_id="matched", external_ids={(ExternalID.ISRC, ISRC)})
    mass.music.get_controller.return_value.get_item_by_external_id = AsyncMock(return_value=None)
    mass.music.tracks.enrich_provider_mappings = AsyncMock(
        return_value=SimpleNamespace(matches=(_core_match(matched, TrackMatchConfidence.LIKELY),))
    )
    encoded = SharePayload(
        type="playlist",
        title="Playlist",
        tracks=[TrackStub(title="Track", artist="Artist", isrc=ISRC, duration=218)],
    ).to_base64()

    preview = await provider.preview_shared_playlist(encoded)

    assert len(preview) == 1
    assert preview[0].item_id == "matched"
    assert preview[0].position == 1
    core_track = mass.music.tracks.enrich_provider_mappings.await_args.args[0]
    assert core_track.duration == 218
    assert core_track.get_external_id(ExternalID.ISRC) == ISRC
    mass.config.get_provider_configs.assert_awaited_once_with(provider_type=ProviderType.MUSIC)
    enrichment_call = mass.music.tracks.enrich_provider_mappings.await_args
    assert enrichment_call.kwargs["provider_instance_ids"] == set()
    assert enrichment_call.kwargs["search_non_streaming_without_mapping"] is True


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

    async def iter_playlist_tracks(
        item_id: str, provider_instance_id_or_domain: str
    ) -> AsyncGenerator[Track]:
        assert item_id == "playlist"
        assert provider_instance_id_or_domain == "test"
        for track in tracks:
            yield track

    _mass(provider).music.playlists.iter_tracks = MagicMock(side_effect=iter_playlist_tracks)
    playlist = MagicMock(item_id="playlist", provider="test")

    exported = await provider._build_playlist_tracks(playlist)

    assert len(exported) == 502
    assert exported[-1].title == exported[0].title
    assert exported[500].title == "Track 500"
    assert all(track.album == "Album" for track in exported)
    _mass(provider).music.playlists.iter_tracks.assert_called_once_with("playlist", "test")


async def test_playlist_export_fails_when_a_later_page_fails() -> None:
    """A failed later playlist page cannot produce a truncated share."""
    provider = _provider()
    first_track = _track(item_id="first-track")

    async def iter_playlist_tracks(
        item_id: str, provider_instance_id_or_domain: str
    ) -> AsyncGenerator[Track]:
        assert item_id == "playlist"
        assert provider_instance_id_or_domain == "catalog--1"
        yield first_track
        raise ProviderUnavailableError("later page failed")

    _mass(provider).music.playlists.iter_tracks = MagicMock(side_effect=iter_playlist_tracks)
    playlist = MagicMock(item_id="playlist", provider="catalog--1")

    with pytest.raises(ProviderUnavailableError, match="later page failed"):
        await provider._build_playlist_tracks(playlist)


@pytest.mark.parametrize("include_track", [False, True])
async def test_playlist_export_rejects_unsupported_media_entries(include_track: bool) -> None:
    """A playlist with unsupported entries is rejected rather than truncated."""
    provider = _provider()
    playlist_tracks: list[Track | Radio] = [_track(item_id="track")] if include_track else []
    playlist_tracks.append(
        Radio(item_id="radio", provider="builtin", name="Radio", provider_mappings=set())
    )

    async def iter_playlist_tracks(
        item_id: str, provider_instance_id_or_domain: str
    ) -> AsyncGenerator[Track | Radio]:
        assert item_id == "playlist"
        assert provider_instance_id_or_domain == "builtin"
        for item in playlist_tracks:
            yield item

    _mass(provider).music.playlists.iter_tracks = MagicMock(side_effect=iter_playlist_tracks)
    playlist = Playlist(
        item_id="playlist", provider="builtin", name="Playlist", provider_mappings=set()
    )

    with pytest.raises(InvalidDataError, match="radio entries"):
        await provider._build_payload(playlist)


async def test_playlist_resolution_is_read_only() -> None:
    """Resolving a playlist preview never creates or edits a library playlist."""
    provider = _provider()
    encoded = SharePayload(
        type="playlist",
        title="Playlist",
        tracks=[TrackStub(title="Track", artist="Artist", isrc=ISRC)],
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
                version="Live",
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
    assert imported_track.version == "Live"
    assert imported_track.album == "Album"
    assert imported_track.isrc == ISRC
    assert imported_track.mbid == "recording-mbid"
    assert imported_track.mb_track == "release-track-mbid"
    assert imported_items[0].length == "218"
    decoded_playlist = SharePayload.from_base64(encoded)
    assert decoded_playlist.tracks is not None
    assert decoded_playlist.tracks[0].duration == 218
    assert imported_track.isrc == ISRC
    assert imported_items[0].metadata == {
        "media_type": "track",
        "name": "Track",
        "version": "Live",
        "mbid": "recording-mbid",
        "mb_track": "release-track-mbid",
    }
    assert imported_items[0].artists[0].name == "Artist"
    assert imported_items[0].album is None
    assert imported_track.album == "Album"
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
        version="Live",
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


async def test_import_shared_playlist_preserves_separate_artist_credits() -> None:
    """M3U import preserves the separate artist list in share metadata and items."""
    provider = _provider()
    mass = _mass(provider)
    mass.music.playlists.import_playlist = AsyncMock(return_value=MagicMock())
    payload = SharePayload(
        type="playlist",
        title="Shared Mix",
        tracks=[
            TrackStub(
                title="Song",
                artist="First, Second",
                artists=["First", "Second"],
                duration=218,
            )
        ],
    )

    await provider.import_shared_playlist(payload.to_base64())

    m3u_data = mass.music.playlists.import_playlist.await_args.args[0]
    imported_item = parse_m3u(m3u_data)[0]
    shared_payload = SharePayload.from_base64(imported_item.path.removeprefix("share://track/"))
    assert shared_payload.artists == ["First", "Second"]
    assert [artist.name for artist in imported_item.artists] == ["First", "Second"]


async def test_reshare_imported_unresolved_track_preserves_original_payload() -> None:
    """An unresolved imported track retains IDs, album data, and links when re-shared."""
    provider = _provider()
    mass = _mass(provider)
    mass.music.playlists.import_playlist = AsyncMock(return_value=MagicMock())
    payload = SharePayload(
        type="playlist",
        title="Shared Mix",
        tracks=[
            TrackStub(
                title="Track",
                artist="Artist",
                artists=["Artist"],
                version="Live",
                isrc=ISRC,
                mbid="recording-mbid",
                album="Album",
                duration=218,
                provider_refs={"spotify": "/track/original-track"},
            )
        ],
    )

    await provider.import_shared_playlist(payload.to_base64())

    imported_m3u = mass.music.playlists.import_playlist.await_args.args[0]
    imported_item = parse_m3u(imported_m3u)[0]
    unresolved_track = construct_media_item_from_playlist_item(imported_item, mass)
    assert isinstance(unresolved_track, Track)
    assert unresolved_track.provider == "builtin"
    assert unresolved_track.item_id == imported_item.path
    assert (ExternalID.ISRC, ISRC) not in unresolved_track.external_ids

    async def iter_playlist_tracks(
        item_id: str, provider_instance_id_or_domain: str
    ) -> AsyncGenerator[Track]:
        assert item_id == "playlist"
        assert provider_instance_id_or_domain == "builtin"
        yield unresolved_track

    mass.music.playlists.iter_tracks = MagicMock(side_effect=iter_playlist_tracks)
    stubs = await provider._build_playlist_tracks(MagicMock(item_id="playlist", provider="builtin"))

    assert payload.tracks is not None
    assert stubs == payload.tracks


@pytest.mark.parametrize("title", ["Song||isrc=USRC17607839", "Song|", "Song| "])
async def test_import_shared_playlist_rejects_m3u_extension_delimiter(title: str) -> None:
    """A share title cannot corrupt structured metadata through the M3U separator."""
    provider = _provider()
    mass = _mass(provider)
    encoded = SharePayload(
        type="playlist",
        title="Shared Mix",
        tracks=[
            TrackStub(
                title=title,
                artist="Artist",
                mbid="recording-mbid",
                mb_track="release-track-mbid",
            )
        ],
    ).to_base64()

    with pytest.raises(InvalidDataError, match="unsupported M3U delimiter"):
        await provider.import_shared_playlist(encoded)

    mass.music.playlists.import_playlist.assert_not_called()


def test_trailing_pipe_corrupts_m3u_metadata_on_builtin_rewrite() -> None:
    """The M3U rewrite loses the following metadata key after a trailing pipe."""
    original = PlaylistItem(
        path="share://track/payload",
        title="Artist - Song| ",
        metadata={"media_type": "track", "name": "Song| ", "mbid": "recording-mbid"},
    )

    first_parse = parse_m3u(generate_m3u("Mix", [original]))[0]
    second_parse = parse_m3u(generate_m3u("Mix", [first_parse]))[0]

    assert first_parse.metadata == {
        "media_type": "track",
        "name": "Song|",
        "mbid": "recording-mbid",
    }
    assert second_parse.metadata == {
        "media_type": "track",
        "name": "Song",
        "|mbid": "recording-mbid",
    }


async def test_playlist_payload_preserves_track_version_and_artist_credits() -> None:
    """Playlist stubs retain version metadata and the track's artist credit string."""
    provider = _provider()
    track = Track(
        item_id="track",
        provider="catalog--1",
        name="Song",
        version="Acoustic",
        duration=180,
        provider_mappings=set(),
        artists=UniqueList(
            [
                Artist(
                    item_id="artist-one",
                    provider="catalog--1",
                    name="First Artist",
                    provider_mappings=set(),
                ),
                Artist(
                    item_id="artist-two",
                    provider="catalog--1",
                    name="Second Artist",
                    provider_mappings=set(),
                ),
            ]
        ),
    )
    mass = _mass(provider)

    async def iter_playlist_tracks(
        item_id: str, provider_instance_id_or_domain: str
    ) -> AsyncGenerator[Track]:
        assert item_id == "playlist"
        assert provider_instance_id_or_domain == "catalog--1"
        yield track

    mass.music.playlists.iter_tracks = MagicMock(side_effect=iter_playlist_tracks)
    playlist = MagicMock(item_id="playlist", provider="catalog--1")

    stubs = await provider._build_playlist_tracks(playlist)

    assert len(stubs) == 1
    assert stubs[0].artist == "First Artist, Second Artist"
    assert stubs[0].artists == ["First Artist", "Second Artist"]
    assert stubs[0].version == "Acoustic"


async def test_import_isrc_only_track_requires_title_and_artist_corroboration() -> None:
    """Share import keeps the ISRC in its URI but not the lossy matcher metadata."""
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
    assert not shared_track.external_ids
    assert compare_track_evidence(catalog_track, shared_track) >= TrackMatchConfidence.LIKELY
    assert (
        compare_track_evidence(catalog_track, shared_item_without_duration)
        < TrackMatchConfidence.LIKELY
    )

    wrong_artist_track = replace(
        catalog_track,
        artists=UniqueList(
            [
                Artist(
                    item_id="other-artist",
                    provider="catalog--1",
                    name="Different Artist",
                    provider_mappings=set(),
                )
            ]
        ),
    )
    wrong_artist_track.external_ids.add((ExternalID.ISRC, ISRC))
    assert compare_track_evidence(wrong_artist_track, shared_track) == TrackMatchConfidence.NO_MATCH


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
    assert all(
        kwargs == {"hydrate_track": False, "search_provider_instance_ids": set()}
        for _, kwargs in resolver.call_args_list
    )
    assert resolver.await_count == 2
    _mass(provider).music.playlists.import_playlist.assert_not_called()


async def test_preview_skips_full_track_hydration_for_matched_tracks() -> None:
    """A preview returns the matched Track without per-track audio-analysis hydration."""
    provider = _provider()
    mass = _mass(provider)
    matched = Track(
        item_id="42",
        provider="library",
        name="Known",
        duration=218,
        external_ids={(ExternalID.ISRC, ISRC)},
        provider_mappings={
            ProviderMapping(
                item_id="42",
                provider_domain="catalog",
                provider_instance="catalog--1",
                available=True,
            )
        },
        artists=UniqueList(
            [
                Artist(
                    item_id="artist",
                    provider="catalog--1",
                    name="Artist",
                    provider_mappings=set(),
                )
            ]
        ),
    )
    controller = mass.music.get_controller.return_value
    controller.get_item_by_external_id = AsyncMock(return_value=matched)
    mass.music.tracks.get = AsyncMock()
    encoded = SharePayload(
        type="playlist",
        title="Playlist",
        tracks=[
            TrackStub(title="Known", artist="Artist", artists=["Artist"], isrc=ISRC, duration=218)
        ],
    ).to_base64()

    tracks = await provider.preview_shared_playlist(encoded)

    assert len(tracks) == 1
    assert tracks[0].item_id == matched.item_id
    assert tracks[0].name == "Known"
    assert tracks[0].position == 1
    mass.music.tracks.get.assert_not_called()


async def test_preview_cancels_pending_resolutions_after_unexpected_error() -> None:
    """An unexpected resolver error cancels sibling and payload-resolution tasks."""
    provider = _provider()
    encoded = SharePayload(
        type="playlist",
        title="Playlist",
        tracks=[TrackStub(title="Fail"), TrackStub(title="Wait")],
    ).to_base64()
    pending_started = asyncio.Event()
    sibling_cancelled = asyncio.Event()

    async def resolve_media_item(payload: SharePayload, **_kwargs: object) -> Track | None:
        if payload.title == "Wait":
            pending_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                sibling_cancelled.set()
                raise
            return None
        await pending_started.wait()
        raise RuntimeError("unexpected resolver failure")

    with (
        patch.object(ShareProvider, "_resolve_media_item", side_effect=resolve_media_item),
        pytest.raises(RuntimeError, match="unexpected resolver failure"),
    ):
        await provider.preview_shared_playlist(encoded)

    assert sibling_cancelled.is_set()


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
