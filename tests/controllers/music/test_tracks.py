"""Tests for the tracks controller."""

from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from music_assistant_models.enums import ExternalID, MediaType, ProviderFeature
from music_assistant_models.errors import (
    MediaNotFoundError,
    ProviderUnavailableError,
    ResourceTemporarilyUnavailable,
)
from music_assistant_models.media_items import (
    Artist,
    ItemMapping,
    ProviderMapping,
    SearchResults,
    Track,
    UniqueList,
)

from music_assistant.controllers.music import MusicController
from music_assistant.controllers.music.media.tracks import (
    TrackProviderMatch,
    TrackProviderMatchResult,
    TracksController,
)
from music_assistant.helpers.compare import TrackMatchConfidence
from music_assistant.mass import MusicAssistant
from music_assistant.models.music_provider import MusicProvider

from .helpers import create_album, create_track


@pytest.fixture
async def music(mass_minimal: MusicAssistant) -> AsyncGenerator[MusicController]:
    """Return a music controller attached to the minimal mass instance."""
    controller = MusicController(mass_minimal)
    mass_minimal.music = controller
    yield controller
    if controller._database:
        await controller._database.close()


@pytest.mark.asyncio
async def test_explicit_filter_true_generates_sql(music: MusicController) -> None:
    """Test that explicit=True generates correct SQL filter."""
    captured_parts: list[str] = []

    async def mock_query(*_args: Any, **kwargs: Any) -> list[Any]:
        captured_parts.extend(kwargs.get("extra_query_parts", []))
        return []

    with patch.object(music.tracks, "get_library_items_by_query", mock_query):
        await music.tracks.library_items(explicit=True, limit=10)
        assert any(
            "json_extract(tracks.metadata, '$.explicit') = 1" in part for part in captured_parts
        )


@pytest.mark.asyncio
async def test_explicit_filter_false_generates_sql(music: MusicController) -> None:
    """Test that explicit=False generates correct SQL filter."""
    captured_parts: list[str] = []

    async def mock_query(*_args: Any, **kwargs: Any) -> list[Any]:
        captured_parts.extend(kwargs.get("extra_query_parts", []))
        return []

    with patch.object(music.tracks, "get_library_items_by_query", mock_query):
        await music.tracks.library_items(explicit=False, limit=10)
        assert any("IS NULL" in part and "= 0" in part for part in captured_parts)


@pytest.mark.asyncio
async def test_explicit_filter_none_generates_no_sql(music: MusicController) -> None:
    """Test that explicit=None generates no explicit filter."""
    captured_parts: list[str] = []

    async def mock_query(*_args: Any, **kwargs: Any) -> list[Any]:
        captured_parts.extend(kwargs.get("extra_query_parts", []))
        return []

    with patch.object(music.tracks, "get_library_items_by_query", mock_query):
        await music.tracks.library_items(explicit=None, limit=10)
        assert not any("explicit" in part.lower() for part in captured_parts)


@pytest.mark.asyncio
async def test_explicit_filter_default_is_none(music: MusicController) -> None:
    """Test that omitting explicit parameter behaves like explicit=None."""
    captured_parts: list[str] = []

    async def mock_query(*_args: Any, **kwargs: Any) -> list[Any]:
        captured_parts.extend(kwargs.get("extra_query_parts", []))
        return []

    with patch.object(music.tracks, "get_library_items_by_query", mock_query):
        await music.tracks.library_items(limit=10)
        assert not any("explicit" in part.lower() for part in captured_parts)


@pytest.mark.asyncio
async def test_by_prov_id_batches_item_ids_into_in_clause(music: MusicController) -> None:
    """provider_item_ids builds a single parameterized IN (...) subquery."""
    captured_parts: list[str] = []
    captured_params: dict[str, Any] = {}

    async def mock_query(*_args: Any, **kwargs: Any) -> list[Any]:
        captured_parts.extend(kwargs.get("extra_query_parts", []))
        captured_params.update(kwargs.get("extra_query_params", {}))
        return []

    with patch.object(music.tracks, "get_library_items_by_query", mock_query):
        await music.tracks.get_library_items_by_prov_id(
            provider_instance_id_or_domain="spotify",
            provider_item_ids=["x", "y", "z"],
        )

    subquery = " ".join(captured_parts)
    assert "provider_mappings.provider_item_id IN (:item_id_0, :item_id_1, :item_id_2)" in subquery
    assert captured_params["prov_id"] == "spotify"
    assert [captured_params[f"item_id_{i}"] for i in range(3)] == ["x", "y", "z"]


@pytest.mark.asyncio
async def test_by_prov_id_empty_item_ids_matches_nothing(music: MusicController) -> None:
    """An explicit empty provider_item_ids returns [] (not the whole provider library)."""
    ran = False

    async def mock_query(*_args: Any, **_kwargs: Any) -> list[Any]:
        nonlocal ran
        ran = True
        return []

    with patch.object(music.tracks, "get_library_items_by_query", mock_query):
        result = await music.tracks.get_library_items_by_prov_id(
            provider_instance_id_or_domain="spotify", provider_item_ids=[]
        )

    assert result == []
    assert ran is False  # short-circuits before it can build an unconstrained query


async def test_match_provider_uses_full_track_mapping(music: MusicController) -> None:
    """Provider matching stores mapping details from the fetched track."""
    base_track = create_track("spotify_1", "base")
    search_track = create_track("qobuz_1", "candidate")
    full_track = create_track("qobuz_1", "candidate")
    full_track.provider_mappings = {
        ProviderMapping(
            item_id="candidate",
            provider_domain="qobuz",
            provider_instance="qobuz_1",
            url="https://provider.example/full",
        )
    }
    provider = MagicMock()
    provider.name = "Qobuz"
    provider.domain = "qobuz"

    with (
        patch.object(music.tracks, "search", AsyncMock(return_value=[search_track])),
        patch.object(
            music.tracks,
            "get_provider_item",
            AsyncMock(return_value=full_track),
        ),
        patch(
            "music_assistant.controllers.music.media.tracks.compare_media_item",
            return_value=True,
        ),
        patch(
            "music_assistant.controllers.music.media.tracks.compare_track",
            return_value=True,
        ),
    ):
        mappings = await music.tracks.match_provider(base_track, provider, ref_albums=[])

    assert mappings == list(full_track.provider_mappings)


async def test_get_provider_item_can_disable_library_fallback(
    music: MusicController,
) -> None:
    """Authoritative hydration surfaces a missing provider ID instead of stored details."""
    provider = MagicMock(spec=MusicProvider)
    provider.instance_id = "qobuz_1"
    provider.domain = "qobuz"
    provider.get_track = AsyncMock(side_effect=MediaNotFoundError("gone"))
    library_fallback = create_track("qobuz_1", "stale")

    with (
        patch.object(music.mass, "get_provider", return_value=provider),
        patch.object(
            music.tracks,
            "get_library_item_by_prov_id",
            AsyncMock(return_value=library_fallback),
        ),
    ):
        with pytest.raises(MediaNotFoundError, match="not found"):
            await music.tracks.get_provider_item(
                "stale",
                provider.instance_id,
                allow_fallback=False,
            )
        result = await music.tracks.get_provider_item(
            "stale",
            provider.instance_id,
        )

    assert result is library_fallback


async def test_get_provider_item_strict_instance_rejects_fallback(
    music: MusicController,
) -> None:
    """Authoritative hydration can not switch to another provider account."""
    unavailable_provider = MagicMock(spec=MusicProvider)
    unavailable_provider.instance_id = "qobuz_1"
    unavailable_provider.available = False
    outside_scope_provider = MagicMock(spec=MusicProvider)
    outside_scope_provider.instance_id = "qobuz_2"
    outside_scope_provider.available = True
    outside_scope_provider.get_track = AsyncMock()

    with (
        patch.object(
            music.mass,
            "get_provider",
            side_effect=lambda _provider_instance_id, return_unavailable=False: (
                unavailable_provider if return_unavailable else outside_scope_provider
            ),
        ),
        pytest.raises(ProviderUnavailableError, match="qobuz_1"),
    ):
        await music.tracks.get_provider_item(
            "track",
            "qobuz_1",
            allow_fallback=False,
            strict_provider_instance=True,
        )

    outside_scope_provider.get_track.assert_not_awaited()


async def test_find_provider_match_reuses_domain_mapping_for_target_instance(
    music: MusicController,
) -> None:
    """A catalog mapping is reused across instances without searching."""
    track = create_track("qobuz_1", "track")
    provider = MagicMock()
    provider.instance_id = "qobuz_2"
    provider.domain = "qobuz"

    with patch.object(music, "search_provider", AsyncMock()) as search:
        result = await music.tracks.find_provider_match(track, provider)

    assert result.match is not None
    assert result.match.mapping.item_id == "track"
    assert result.match.mapping.provider_instance == "qobuz_2"
    assert result.match.confidence == TrackMatchConfidence.EXACT
    search.assert_not_awaited()


async def test_find_provider_match_revalidates_untrusted_source_mapping(
    music: MusicController,
) -> None:
    """A mapping restored from a Music Assistant playlist is reclassified from metadata."""
    source = create_track("qobuz_1", "source")
    candidate = create_track("qobuz_1", "source")
    provider = MagicMock()
    provider.instance_id = "qobuz_1"
    provider.domain = "qobuz"
    provider.supported_features = set()
    provider.supported_media_types = {MediaType.TRACK}

    with (
        patch.object(
            music.tracks,
            "get_provider_item",
            AsyncMock(return_value=candidate),
        ),
        patch.object(
            music.tracks,
            "_get_full_track_album",
            AsyncMock(return_value=None),
        ),
    ):
        exact_result = await music.tracks.find_provider_match(
            source,
            provider,
            minimum_confidence=TrackMatchConfidence.EXACT,
            trust_base_mapping=False,
        )
        likely_result = await music.tracks.find_provider_match(
            source,
            provider,
            minimum_confidence=TrackMatchConfidence.LIKELY,
            trust_base_mapping=False,
        )

    assert exact_result.match is None
    assert likely_result.match is not None
    assert likely_result.match.confidence == TrackMatchConfidence.LIKELY


async def test_match_confidence_hydrates_album_after_initial_no_match(
    music: MusicController,
) -> None:
    """Full release evidence can resolve a duration-based first-pass rejection."""
    base = create_track("spotify_1", "base", duration=200)
    candidate = create_track("qobuz_1", "candidate", duration=210)
    base.disc_number = candidate.disc_number = 1
    base.track_number = candidate.track_number = 1
    base_album = create_album("spotify_1", "base-album", name="Album")
    candidate_album = create_album("qobuz_1", "candidate-album", name="Album")
    base.album = ItemMapping(
        item_id=base_album.item_id,
        provider=base_album.provider,
        name=base_album.name,
        media_type=MediaType.ALBUM,
    )
    candidate.album = ItemMapping(
        item_id=candidate_album.item_id,
        provider=candidate_album.provider,
        name=candidate_album.name,
        media_type=MediaType.ALBUM,
    )

    with patch.object(
        music.tracks,
        "_get_full_track_album",
        AsyncMock(side_effect=(base_album, candidate_album)),
    ):
        confidence, _ = await music.tracks._get_match_confidence(
            base,
            candidate,
            None,
        )

    assert confidence == TrackMatchConfidence.EXACT


@pytest.mark.parametrize(
    "error",
    [TimeoutError(), ResourceTemporarilyUnavailable("Provider temporarily unavailable")],
)
async def test_full_track_album_falls_back_after_transient_failure(
    music: MusicController,
    error: Exception,
) -> None:
    """Optional album evidence falls back to the mapping after a transient provider failure."""
    track = create_track("spotify_1", "track")
    track.album = ItemMapping(
        item_id="album",
        provider="spotify_1",
        name="Album",
        media_type=MediaType.ALBUM,
    )

    with patch.object(
        music.albums,
        "get",
        AsyncMock(side_effect=error),
    ):
        result = await music.tracks._get_full_track_album(track)

    assert result is track.album


async def test_find_provider_match_classifies_library_mapping_against_source(
    music: MusicController,
) -> None:
    """A library mapping is a candidate, not proof of an exact source release."""
    source = create_track("spotify_1", "source")
    library_track = create_track("spotify_1", "library")
    target = create_track("qobuz_1", "target")
    library_track.provider_mappings.add(next(iter(target.provider_mappings)))
    provider = MagicMock()
    provider.instance_id = "qobuz_1"
    provider.domain = "qobuz"
    provider.supported_features = set()
    provider.supported_media_types = {MediaType.TRACK}

    with (
        patch.object(
            music.tracks,
            "get_provider_item",
            AsyncMock(return_value=target),
        ),
        patch.object(
            music.tracks,
            "_get_full_track_album",
            AsyncMock(return_value=None),
        ),
    ):
        exact_result = await music.tracks.find_provider_match(
            source,
            provider,
            minimum_confidence=TrackMatchConfidence.EXACT,
            mapping_source=library_track,
            allowed_provider_instances={"qobuz_1"},
        )
        likely_result = await music.tracks.find_provider_match(
            source,
            provider,
            minimum_confidence=TrackMatchConfidence.LIKELY,
            mapping_source=library_track,
            allowed_provider_instances={"qobuz_1"},
        )

    assert exact_result.match is None
    assert likely_result.match is not None
    assert likely_result.match.confidence == TrackMatchConfidence.LIKELY


async def test_library_mapping_does_not_preempt_exact_provider_search(
    music: MusicController,
) -> None:
    """An alternate library mapping remains a fallback while exact search continues."""
    mb_track = (
        ExternalID.MB_TRACK,
        "12345678-1234-1234-1234-123456789abc",
    )
    source = create_track("spotify_1", "source")
    source.external_ids.add(mb_track)
    source.artists.append(
        ItemMapping(
            item_id="other-artist",
            provider="spotify_1",
            name="Other Artist",
            media_type=MediaType.ARTIST,
        )
    )
    library_track = create_track("spotify_1", "library")
    mapped_candidate = create_track("qobuz_1", "mapped")
    exact_candidate = create_track("qobuz_1", "exact")
    exact_candidate.external_ids.add(mb_track)
    library_track.provider_mappings.add(next(iter(mapped_candidate.provider_mappings)))
    provider = MagicMock()
    provider.instance_id = "qobuz_1"
    provider.domain = "qobuz"
    provider.supported_features = {ProviderFeature.SEARCH}
    provider.supported_media_types = {MediaType.TRACK}
    candidates = {
        "mapped": mapped_candidate,
        "exact": exact_candidate,
    }

    with (
        patch.object(
            music.tracks,
            "get_provider_item",
            AsyncMock(side_effect=lambda item_id, *_args, **_kwargs: candidates[item_id]),
        ),
        patch.object(
            music,
            "search_provider",
            AsyncMock(
                side_effect=(
                    SearchResults(tracks=[exact_candidate]),
                    ResourceTemporarilyUnavailable("Later search timed out"),
                )
            ),
        ) as search_provider,
        patch.object(
            music.tracks,
            "_get_full_track_album",
            AsyncMock(return_value=None),
        ),
    ):
        result = await music.tracks.find_provider_match(
            source,
            provider,
            minimum_confidence=TrackMatchConfidence.LIKELY,
            mapping_source=library_track,
            allowed_provider_instances={"qobuz_1"},
        )

    assert result.match is not None
    assert result.match.track.item_id == "exact"
    assert result.match.confidence == TrackMatchConfidence.EXACT
    assert search_provider.await_count == 1


async def test_find_provider_match_prefers_exact_candidate(
    music: MusicController,
) -> None:
    """An exact candidate outranks a loose result returned earlier."""
    mb_track = (
        ExternalID.MB_TRACK,
        "12345678-1234-1234-1234-123456789abc",
    )
    base = create_track("spotify_1", "base", isrc="USRC17607839")
    base.external_ids.add(mb_track)
    loose = create_track("qobuz_1", "loose", isrc="OTHER")
    loose.version = "Deluxe"
    stale = create_track("qobuz_1", "stale", isrc="STALE")
    exact = create_track("qobuz_1", "exact", isrc="USRC17607839")
    exact.provider = "qobuz"
    exact.external_ids.add(mb_track)
    provider = MagicMock()
    provider.instance_id = "qobuz_1"
    provider.domain = "qobuz"
    provider.supported_features = {ProviderFeature.SEARCH}
    provider.supported_media_types = {MediaType.TRACK}

    async def get_provider_item(item_id: str, *_args: object, **_kwargs: object) -> Track:
        if item_id == "stale":
            raise MediaNotFoundError("Stale search result")
        return {"loose": loose, "exact": exact}[item_id]

    get_provider_item_mock = AsyncMock(side_effect=get_provider_item)
    with (
        patch.object(
            music,
            "search_provider",
            AsyncMock(return_value=SearchResults(tracks=[stale, loose, exact])),
        ),
        patch.object(
            music.tracks,
            "get_provider_item",
            get_provider_item_mock,
        ),
        patch.object(music.tracks, "_get_full_track_album", AsyncMock(return_value=None)),
    ):
        result = await music.tracks.find_provider_match(
            base,
            provider,
            minimum_confidence=TrackMatchConfidence.LOOSE,
        )

    assert result.match is not None
    assert result.match.track.item_id == "exact"
    assert result.match.confidence == TrackMatchConfidence.EXACT
    assert all(
        call.args[1] == provider.instance_id and call.kwargs["strict_provider_instance"] is True
        for call in get_provider_item_mock.await_args_list
    )


async def test_find_provider_match_keeps_fallback_after_later_timeout(
    music: MusicController,
) -> None:
    """A later artist-query timeout does not discard an acceptable candidate."""
    base = create_track("spotify_1", "base")
    base.artists.append(
        ItemMapping(
            item_id="other-artist",
            provider="spotify_1",
            name="Other Artist",
            media_type=MediaType.ARTIST,
        )
    )
    candidate = create_track("qobuz_1", "candidate")
    provider = MagicMock()
    provider.instance_id = "qobuz_1"
    provider.domain = "qobuz"
    provider.supported_features = {ProviderFeature.SEARCH}
    provider.supported_media_types = {MediaType.TRACK}

    with (
        patch.object(
            music,
            "search_provider",
            AsyncMock(
                side_effect=(
                    SearchResults(tracks=[candidate]),
                    ResourceTemporarilyUnavailable("Later search timed out"),
                )
            ),
        ) as search_provider,
        patch.object(
            music.tracks,
            "get_provider_item",
            AsyncMock(return_value=candidate),
        ),
        patch.object(
            music.tracks,
            "_get_full_track_album",
            AsyncMock(return_value=None),
        ),
    ):
        result = await music.tracks.find_provider_match(
            base,
            provider,
            minimum_confidence=TrackMatchConfidence.LIKELY,
        )

    assert result.match is not None
    assert result.match.confidence == TrackMatchConfidence.LIKELY
    assert search_provider.await_count == 2


async def test_find_provider_match_reports_ambiguous_loose_candidates(
    music: MusicController,
) -> None:
    """Different tied best-effort versions are ambiguous rather than arbitrary."""
    base = create_track("spotify_1", "base", isrc="BASE")
    deluxe = create_track("qobuz_1", "deluxe", isrc="DELUXE")
    deluxe.version = "Deluxe"
    remaster = create_track("qobuz_1", "remaster", isrc="REMASTER")
    remaster.version = "2022 Remaster"
    provider = MagicMock()
    provider.instance_id = "qobuz_1"
    provider.domain = "qobuz"
    provider.supported_features = {ProviderFeature.SEARCH}
    provider.supported_media_types = {MediaType.TRACK}

    with (
        patch.object(
            music,
            "search_provider",
            AsyncMock(return_value=SearchResults(tracks=[deluxe, remaster])),
        ),
        patch.object(
            music.tracks,
            "get_provider_item",
            AsyncMock(
                side_effect=lambda item_id, *_args, **_kwargs: {
                    "deluxe": deluxe,
                    "remaster": remaster,
                }[item_id]
            ),
        ),
        patch.object(music.tracks, "_get_full_track_album", AsyncMock(return_value=None)),
    ):
        result = await music.tracks.find_provider_match(
            base,
            provider,
            minimum_confidence=TrackMatchConfidence.LOOSE,
        )

    assert result.match is None
    assert result.ambiguous is True


def test_tied_loose_matches_require_pairwise_compatibility() -> None:
    """A middle-duration candidate can not make incompatible outer candidates look equivalent."""
    tracks = [
        create_track("qobuz_1", "middle", duration=103, isrc="MIDDLE"),
        create_track("qobuz_1", "short", duration=100, isrc="SHORT"),
        create_track("qobuz_1", "long", duration=106, isrc="LONG"),
    ]
    matches = [
        TrackProviderMatch(
            track=track,
            mapping=next(iter(track.provider_mappings)),
            confidence=TrackMatchConfidence.LOOSE,
        )
        for track in tracks
    ]

    assert TracksController._matches_are_compatible(matches) is False


async def test_enrich_provider_mappings_uses_library_without_mutating_it(
    music: MusicController,
) -> None:
    """Library mappings seed playlist enrichment without being updated."""
    source = create_track("spotify_1", "source")
    library_track = create_track("spotify_1", "library")
    library_track.provider = "library"
    library_track.item_id = "42"
    qobuz_track = create_track("qobuz_1", "qobuz-track")
    qobuz_mapping = next(iter(qobuz_track.provider_mappings))
    library_track.provider_mappings.add(qobuz_mapping)
    stale_tidal_mapping = ProviderMapping(
        item_id="old-tidal-track",
        provider_domain="tidal",
        provider_instance="tidal_1",
        available=False,
    )
    library_track.provider_mappings.add(stale_tidal_mapping)
    original_mappings = set(library_track.provider_mappings)
    tidal_track = create_track("tidal_1", "tidal-track")
    tidal_mapping = next(iter(tidal_track.provider_mappings))
    tidal_provider = MagicMock()
    tidal_provider.name = "Tidal"
    tidal_provider.instance_id = "tidal_1"
    tidal_provider.domain = "tidal"
    tidal_provider.is_streaming_provider = True
    qobuz_provider = MagicMock()
    qobuz_provider.name = "Qobuz"
    qobuz_provider.instance_id = "qobuz_1"
    qobuz_provider.domain = "qobuz"
    qobuz_provider.is_streaming_provider = True
    qobuz_match = TrackProviderMatch(
        track=qobuz_track,
        mapping=qobuz_mapping,
        confidence=TrackMatchConfidence.LIKELY,
    )
    tidal_match = TrackProviderMatch(
        track=tidal_track,
        mapping=tidal_mapping,
        confidence=TrackMatchConfidence.LIKELY,
    )
    matches = {
        "qobuz": TrackProviderMatchResult(match=qobuz_match),
        "tidal": TrackProviderMatchResult(match=tidal_match),
    }

    with (
        patch.object(
            music.tracks,
            "get_library_match",
            AsyncMock(return_value=library_track),
        ),
        patch.object(
            music.tracks,
            "_get_full_track_album",
            AsyncMock(return_value=None),
        ) as get_full_track_album,
        patch.object(
            music.tracks,
            "find_provider_match",
            AsyncMock(side_effect=lambda _track, provider, **_kwargs: matches[provider.domain]),
        ),
        patch.object(
            MusicController,
            "providers",
            new_callable=PropertyMock,
            return_value=[qobuz_provider, tidal_provider],
        ),
    ):
        result = await music.tracks.enrich_provider_mappings(source)

    assert result.used_library_item is True
    assert result.track is not library_track
    assert result.track.provider_mappings == {
        *source.provider_mappings,
        qobuz_mapping,
        tidal_mapping,
    }
    assert library_track.provider_mappings == original_mappings
    assert result.matches == (qobuz_match, tidal_match)
    get_full_track_album.assert_awaited_once_with(source)


async def test_enrich_provider_mappings_skips_album_lookup_for_existing_domains(
    music: MusicController,
) -> None:
    """Trusted source mappings avoid unnecessary album and provider lookups."""
    source = create_track("spotify_1", "source")
    provider = MagicMock(spec=MusicProvider)
    provider.instance_id = "spotify_1"
    provider.domain = "spotify"
    provider.available = True
    provider.is_streaming_provider = True
    get_full_track_album = AsyncMock()
    find_provider_match = AsyncMock()

    with (
        patch.object(music.tracks, "get_library_match", AsyncMock(return_value=None)),
        patch.object(music.tracks, "_get_full_track_album", get_full_track_album),
        patch.object(music.tracks, "find_provider_match", find_provider_match),
        patch.object(music.mass, "get_provider", return_value=provider),
    ):
        result = await music.tracks.enrich_provider_mappings(
            source,
            provider_instance_ids={"spotify_1"},
        )

    assert result.track.provider_mappings == source.provider_mappings
    get_full_track_album.assert_not_awaited()
    find_provider_match.assert_not_awaited()


async def test_enrich_provider_mappings_does_not_substitute_unavailable_instance(
    music: MusicController,
) -> None:
    """A captured provider instance can not fall back to another account."""
    source = create_track("spotify_1", "source")
    unavailable_provider = MagicMock(spec=MusicProvider)
    unavailable_provider.instance_id = "qobuz_1"
    unavailable_provider.domain = "qobuz"
    unavailable_provider.available = False
    outside_scope_provider = MagicMock(spec=MusicProvider)
    outside_scope_provider.instance_id = "qobuz_2"
    outside_scope_provider.domain = "qobuz"
    outside_scope_provider.available = True
    find_provider_match = AsyncMock()

    def get_provider(
        _provider_instance_id: str,
        return_unavailable: bool = False,
    ) -> MusicProvider:
        return unavailable_provider if return_unavailable else outside_scope_provider

    with (
        patch.object(music.tracks, "get_library_match", AsyncMock(return_value=None)),
        patch.object(music.tracks, "find_provider_match", find_provider_match),
        patch.object(music.mass, "get_provider", side_effect=get_provider),
    ):
        result = await music.tracks.enrich_provider_mappings(
            source,
            provider_instance_ids={"qobuz_1"},
        )

    assert result.track.provider_mappings == set()
    find_provider_match.assert_not_awaited()


async def test_enrich_provider_mappings_tries_next_instance_after_miss(
    music: MusicController,
) -> None:
    """A miss on one account does not suppress another account of the same service."""
    source = create_track("spotify_1", "source")
    qobuz_track = create_track("qobuz_2", "qobuz-track")
    qobuz_mapping = next(iter(qobuz_track.provider_mappings))
    first_provider = MagicMock(spec=MusicProvider)
    first_provider.name = "Qobuz first"
    first_provider.instance_id = "qobuz_1"
    first_provider.domain = "qobuz"
    first_provider.available = True
    first_provider.is_streaming_provider = True
    second_provider = MagicMock(spec=MusicProvider)
    second_provider.name = "Qobuz second"
    second_provider.instance_id = "qobuz_2"
    second_provider.domain = "qobuz"
    second_provider.available = True
    second_provider.is_streaming_provider = True
    match = TrackProviderMatch(
        track=qobuz_track,
        mapping=qobuz_mapping,
        confidence=TrackMatchConfidence.LIKELY,
    )
    results = {
        "qobuz_1": TrackProviderMatchResult(),
        "qobuz_2": TrackProviderMatchResult(match=match),
    }

    with (
        patch.object(
            music.tracks,
            "get_library_match",
            AsyncMock(return_value=None),
        ),
        patch.object(
            music.tracks,
            "_get_full_track_album",
            AsyncMock(return_value=None),
        ),
        patch.object(
            music.tracks,
            "find_provider_match",
            AsyncMock(
                side_effect=lambda _track, provider, **_kwargs: results[provider.instance_id]
            ),
        ) as find_match,
        patch.object(
            music.mass,
            "get_provider",
            side_effect=lambda provider_instance_id, **_kwargs: {
                "qobuz_1": first_provider,
                "qobuz_2": second_provider,
            }[provider_instance_id],
        ),
    ):
        result = await music.tracks.enrich_provider_mappings(
            source,
            provider_instance_ids={"qobuz_1", "qobuz_2"},
        )

    assert find_match.await_count == 2
    assert qobuz_mapping in result.track.provider_mappings
    assert result.matches == (match,)


async def test_enrich_provider_mappings_filters_inaccessible_source_mappings(
    music: MusicController,
) -> None:
    """Builtin source entries retain only provider mappings allowed for the initiating user."""
    source = create_track("spotify_1", "source")
    allowed_track = create_track("spotify_2", "source")
    allowed_mapping = next(iter(allowed_track.provider_mappings))
    allowed_provider = MagicMock(spec=MusicProvider)
    allowed_provider.name = "Spotify allowed"
    allowed_provider.instance_id = "spotify_2"
    allowed_provider.domain = "spotify"
    allowed_provider.available = True
    allowed_provider.is_streaming_provider = True
    match = TrackProviderMatch(
        track=allowed_track,
        mapping=allowed_mapping,
        confidence=TrackMatchConfidence.EXACT,
    )

    with (
        patch.object(
            music.tracks,
            "get_library_match",
            AsyncMock(return_value=None),
        ),
        patch.object(
            music.tracks,
            "_get_full_track_album",
            AsyncMock(return_value=None),
        ),
        patch.object(
            music.tracks,
            "find_provider_match",
            AsyncMock(return_value=TrackProviderMatchResult(match=match)),
        ),
        patch.object(
            music.mass,
            "get_provider",
            return_value=allowed_provider,
        ),
    ):
        result = await music.tracks.enrich_provider_mappings(
            source,
            provider_instance_ids={"spotify_2"},
        )

    assert result.track.provider_mappings == {allowed_mapping}
    assert all(
        mapping.provider_instance != "spotify_1" for mapping in result.track.provider_mappings
    )


async def test_enrich_provider_mappings_preserves_allowed_untrusted_fallback(
    music: MusicController,
) -> None:
    """A Music Assistant copy keeps an allowed source mapping when revalidation finds no match."""
    source = create_track("qobuz_1", "source")
    source_mapping = next(iter(source.provider_mappings))
    provider = MagicMock(spec=MusicProvider)
    provider.name = "Qobuz"
    provider.instance_id = "qobuz_1"
    provider.domain = "qobuz"
    provider.available = True
    provider.is_streaming_provider = True

    with (
        patch.object(
            music.tracks,
            "get_library_match",
            AsyncMock(return_value=None),
        ),
        patch.object(
            music.tracks,
            "_get_full_track_album",
            AsyncMock(return_value=None),
        ),
        patch.object(
            music.tracks,
            "find_provider_match",
            AsyncMock(return_value=TrackProviderMatchResult()),
        ) as find_match,
        patch.object(
            music.mass,
            "get_provider",
            return_value=provider,
        ),
    ):
        result = await music.tracks.enrich_provider_mappings(
            source,
            provider_instance_ids={"qobuz_1"},
            trust_track_mappings=False,
        )

    assert result.track.provider_mappings == {source_mapping}
    assert find_match.await_args is not None
    assert find_match.await_args.kwargs["trust_base_mapping"] is False


async def test_enrich_provider_mappings_drops_unavailable_source_mappings(
    music: MusicController,
) -> None:
    """Unavailable source mappings are not copied into a migrated playlist."""
    source = create_track("qobuz_1", "source")
    source.provider_mappings = {
        ProviderMapping(
            item_id="unavailable",
            provider_domain="qobuz",
            provider_instance="qobuz_1",
            available=False,
        )
    }
    provider = MagicMock(spec=MusicProvider)
    provider.name = "Qobuz"
    provider.instance_id = "qobuz_1"
    provider.domain = "qobuz"
    provider.available = True
    provider.is_streaming_provider = True

    with (
        patch.object(music.tracks, "get_library_match", AsyncMock(return_value=None)),
        patch.object(
            music.tracks,
            "_get_full_track_album",
            AsyncMock(return_value=None),
        ),
        patch.object(
            music.tracks,
            "find_provider_match",
            AsyncMock(return_value=TrackProviderMatchResult()),
        ),
        patch.object(music.mass, "get_provider", return_value=provider),
    ):
        result = await music.tracks.enrich_provider_mappings(
            source,
            provider_instance_ids={"qobuz_1"},
            trust_track_mappings=False,
        )

    assert result.track.provider_mappings == set()


async def test_enrich_provider_mappings_stops_after_provider_failure(
    music: MusicController,
) -> None:
    """A timed-out provider is not queried again for every remaining migration track."""
    source = create_track("spotify_1", "source")
    provider = MagicMock(spec=MusicProvider)
    provider.name = "Qobuz"
    provider.instance_id = "qobuz_1"
    provider.domain = "qobuz"
    provider.available = True
    provider.is_streaming_provider = True
    failed_provider_instances: set[str] = set()

    with (
        patch.object(
            music.tracks,
            "get_library_match",
            AsyncMock(return_value=None),
        ),
        patch.object(
            music.tracks,
            "_get_full_track_album",
            AsyncMock(return_value=None),
        ),
        patch.object(
            music.tracks,
            "find_provider_match",
            AsyncMock(side_effect=ResourceTemporarilyUnavailable("Search timed out")),
        ) as find_match,
        patch.object(
            music.mass,
            "get_provider",
            return_value=provider,
        ),
    ):
        first_result = await music.tracks.enrich_provider_mappings(
            source,
            provider_instance_ids={"qobuz_1"},
            failed_provider_instances=failed_provider_instances,
        )
        second_result = await music.tracks.enrich_provider_mappings(
            source,
            provider_instance_ids={"qobuz_1"},
            failed_provider_instances=failed_provider_instances,
        )

    assert failed_provider_instances == {"qobuz_1"}
    assert find_match.await_count == 1
    assert first_result.failed_providers == ("Qobuz",)
    assert second_result.failed_providers == ()


async def test_overwrite_update_keeps_artists_when_none_are_given(
    mass: MusicAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """An overwrite update carrying no artists must not clear the stored ones."""
    db_track = await mass.music.tracks.add_item_to_library(create_track("spotify_1", "track1"))

    update = create_track("spotify_1", "track1")
    update.artists = UniqueList()
    await mass.music.tracks.update_item_in_library(db_track.item_id, update, overwrite=True)

    refreshed = await mass.music.tracks.get_library_item(db_track.item_id)
    assert [artist.name for artist in refreshed.artists] == ["Test Artist"]
    assert "Ignoring request to clear all artists" in caplog.text


async def test_overwrite_update_replaces_artists(mass: MusicAssistant) -> None:
    """An overwrite update carrying artists still replaces the stored ones."""
    db_track = await mass.music.tracks.add_item_to_library(create_track("spotify_1", "track1"))

    update = create_track("spotify_1", "track1")
    update.artists = UniqueList(
        [
            Artist(
                item_id="other_artist",
                provider="spotify_1",
                name="Other Artist",
                provider_mappings={
                    ProviderMapping(
                        item_id="other_artist",
                        provider_domain="spotify",
                        provider_instance="spotify_1",
                    )
                },
            )
        ]
    )
    await mass.music.tracks.update_item_in_library(db_track.item_id, update, overwrite=True)

    refreshed = await mass.music.tracks.get_library_item(db_track.item_id)
    assert [artist.name for artist in refreshed.artists] == ["Other Artist"]
