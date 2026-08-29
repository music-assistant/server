"""Tests for the tracks controller."""

from collections.abc import AsyncGenerator
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, PropertyMock, call, patch

import pytest
from music_assistant_models.enums import ContentType, ExternalID, MediaType, ProviderFeature
from music_assistant_models.errors import (
    InvalidDataError,
    MediaNotFoundError,
    ProviderUnavailableError,
    ResourceTemporarilyUnavailable,
)
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
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


async def test_find_provider_match_force_refreshes_untrusted_mapped_candidate(
    music: MusicController,
) -> None:
    """An untrusted mapping is revalidated without trusting a stale cached candidate."""
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
        ) as get_provider_item,
        patch.object(
            music.tracks,
            "_get_full_track_album",
            AsyncMock(return_value=None),
        ),
    ):
        await music.tracks.find_provider_match(source, provider, trust_base_mapping=False)
        untrusted_call = get_provider_item.await_args
        get_provider_item.reset_mock()

        await music.tracks.find_provider_match(
            source, provider, mapping_source=candidate, trust_base_mapping=True
        )
        trusted_call = get_provider_item.await_args

    assert untrusted_call is not None
    assert trusted_call is not None
    assert untrusted_call.kwargs["force_refresh"] is True
    assert trusted_call.kwargs["force_refresh"] is False


async def test_find_provider_match_skips_known_dead_mapping_hydration(
    music: MusicController,
) -> None:
    """A mapped candidate a caller already confirmed dead is not hydrated again here."""
    source = create_track("qobuz_1", "source")
    provider = MagicMock()
    provider.instance_id = "qobuz_1"
    provider.domain = "qobuz"
    # no SEARCH feature - isolates this test to the mapped-candidate hydration path
    provider.supported_features = set()
    provider.supported_media_types = {MediaType.TRACK}

    with patch.object(music.tracks, "get_provider_item", AsyncMock()) as get_provider_item:
        result = await music.tracks.find_provider_match(
            source,
            provider,
            trust_base_mapping=False,
            known_dead_mappings=frozenset({("qobuz_1", "source")}),
        )

    get_provider_item.assert_not_awaited()
    assert result.match is None


async def test_match_confidence_hydrates_album_after_initial_no_match(
    music: MusicController,
) -> None:
    """Full release evidence can resolve a duration-based first-pass rejection."""
    base = create_track("spotify_1", "base", duration=200)
    candidate = create_track("qobuz_1", "candidate", duration=210)
    base.disc_number = candidate.disc_number = 1
    base.track_number = candidate.track_number = 1
    mb_album_id = {(ExternalID.MB_ALBUM, "11111111-1111-1111-1111-111111111111")}
    base_album = create_album("spotify_1", "base-album", name="Album", external_ids=mb_album_id)
    candidate_album = create_album(
        "qobuz_1", "candidate-album", name="Album", external_ids=mb_album_id
    )
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

    with (
        patch.object(music.albums, "get_library_item_by_prov_id", AsyncMock(return_value=None)),
        patch.object(
            music.albums,
            "get_provider_item",
            AsyncMock(side_effect=error),
        ),
    ):
        result = await music.tracks._get_full_track_album(track)

    assert result is track.album


async def test_full_track_album_prefers_library_item_over_provider_fetch(
    music: MusicController,
) -> None:
    """A library copy of the album is preferred over a fresh provider fetch."""
    track = create_track("spotify_1", "track")
    track.album = ItemMapping(
        item_id="album",
        provider="qobuz_1",
        name="Album",
        media_type=MediaType.ALBUM,
    )
    library_album = create_album("library", "album_db_id", name="Album")

    with (
        patch.object(
            music.albums, "get_library_item_by_prov_id", AsyncMock(return_value=library_album)
        ),
        patch.object(music.albums, "get_provider_item", AsyncMock()) as get_provider_item,
    ):
        result = await music.tracks._get_full_track_album(track)

    assert result is library_album
    get_provider_item.assert_not_called()


async def test_full_track_album_domain_only_tries_every_allowed_instance(
    music: MusicController,
) -> None:
    """A bare domain album reference tries every allowed instance of that domain."""
    track = create_track("spotify_1", "track")
    track.album = ItemMapping(
        # a domain, not an instance id, as reconstructed from imported #EXTALBUM metadata
        item_id="album",
        provider="qobuz",
        name="Album",
        media_type=MediaType.ALBUM,
    )
    qobuz_1 = MagicMock(spec=MusicProvider)
    qobuz_1.instance_id = "qobuz_1"
    qobuz_1.domain = "qobuz"
    qobuz_2 = MagicMock(spec=MusicProvider)
    qobuz_2.instance_id = "qobuz_2"
    qobuz_2.domain = "qobuz"
    providers = {"qobuz": qobuz_1, "qobuz_1": qobuz_1, "qobuz_2": qobuz_2}
    album = create_album("qobuz_2", "album", name="Album")

    def get_provider(provider_instance_or_domain: str, **_kwargs: Any) -> MusicProvider | None:
        return providers.get(provider_instance_or_domain)

    with (
        # the runtime registry resolves the bare "qobuz" domain to whichever instance
        # happens to be registered first ("qobuz_1"), regardless of allow-listing
        patch.object(music.mass, "get_provider", side_effect=get_provider),
        patch.object(music.albums, "get_library_item_by_prov_id", AsyncMock(return_value=None)),
        patch.object(
            music.albums, "get_provider_item", AsyncMock(return_value=album)
        ) as albums_get,
    ):
        result = await music.tracks._get_full_track_album(
            track, allowed_provider_instances={"qobuz_2"}
        )

    albums_get.assert_awaited_once_with(
        "album", "qobuz_2", allow_fallback=False, strict_provider_instance=True
    )
    assert result is album


async def test_full_track_album_domain_reference_expands_past_incidentally_allowed_instance(
    music: MusicController,
) -> None:
    """A domain reference keeps expanding even when it happens to resolve to an allowed instance."""
    track = create_track("spotify_1", "track")
    track.album = ItemMapping(
        # a domain, not an instance id - the registry happens to resolve it to an
        # instance that is itself allowed, which must not stop expansion to siblings
        item_id="album",
        provider="qobuz",
        name="Album",
        media_type=MediaType.ALBUM,
    )
    qobuz_1 = MagicMock(spec=MusicProvider)
    qobuz_1.instance_id = "qobuz_1"
    qobuz_1.domain = "qobuz"
    qobuz_2 = MagicMock(spec=MusicProvider)
    qobuz_2.instance_id = "qobuz_2"
    qobuz_2.domain = "qobuz"
    providers = {"qobuz": qobuz_1, "qobuz_1": qobuz_1, "qobuz_2": qobuz_2}
    album = create_album("qobuz_2", "album", name="Album")

    def get_provider(provider_instance_or_domain: str, **_kwargs: Any) -> MusicProvider | None:
        return providers.get(provider_instance_or_domain)

    async def get_provider_item_side_effect(
        _item_id: str, instance_id: str, **_kwargs: Any
    ) -> Album:
        if instance_id == "qobuz_1":
            # this account no longer has the album - qobuz_2 must still be tried
            # even though qobuz_1 was itself an allowed instance
            raise MediaNotFoundError("gone")
        return album

    with (
        patch.object(music.mass, "get_provider", side_effect=get_provider),
        patch.object(music.albums, "get_library_item_by_prov_id", AsyncMock(return_value=None)),
        patch.object(
            music.albums,
            "get_provider_item",
            AsyncMock(side_effect=get_provider_item_side_effect),
        ) as get_,
    ):
        result = await music.tracks._get_full_track_album(
            track, allowed_provider_instances={"qobuz_1", "qobuz_2"}
        )

    assert result is album
    assert get_.await_args_list == [
        call("album", "qobuz_1", allow_fallback=False, strict_provider_instance=True),
        call("album", "qobuz_2", allow_fallback=False, strict_provider_instance=True),
    ]


async def test_full_track_album_domain_only_falls_back_after_instance_failure(
    music: MusicController,
) -> None:
    """A failing candidate instance does not block a sibling instance from supplying the album."""
    track = create_track("spotify_1", "track")
    track.album = ItemMapping(
        item_id="album",
        provider="qobuz",
        name="Album",
        media_type=MediaType.ALBUM,
    )
    qobuz_1 = MagicMock(spec=MusicProvider)
    qobuz_1.instance_id = "qobuz_1"
    qobuz_1.domain = "qobuz"
    qobuz_2 = MagicMock(spec=MusicProvider)
    qobuz_2.instance_id = "qobuz_2"
    qobuz_2.domain = "qobuz"
    qobuz_3 = MagicMock(spec=MusicProvider)
    qobuz_3.instance_id = "qobuz_3"
    qobuz_3.domain = "qobuz"
    # the bare "qobuz" domain resolves to an instance the user is not allowed to
    # use, forcing expansion across the allowed instances of that same domain
    providers = {"qobuz": qobuz_3, "qobuz_1": qobuz_1, "qobuz_2": qobuz_2, "qobuz_3": qobuz_3}
    album = create_album("qobuz_2", "album", name="Album")

    def get_provider(provider_instance_or_domain: str, **_kwargs: Any) -> MusicProvider | None:
        return providers.get(provider_instance_or_domain)

    async def albums_get_side_effect(_item_id: str, instance_id: str, **_kwargs: Any) -> Album:
        if instance_id == "qobuz_1":
            # this account no longer has the album - a sibling instance must still
            # be tried instead of falling back to the unhydrated mapping right away
            raise MediaNotFoundError("gone")
        return album

    with (
        patch.object(music.mass, "get_provider", side_effect=get_provider),
        patch.object(music.albums, "get_library_item_by_prov_id", AsyncMock(return_value=None)),
        patch.object(
            music.albums, "get_provider_item", AsyncMock(side_effect=albums_get_side_effect)
        ) as get_,
    ):
        result = await music.tracks._get_full_track_album(
            track, allowed_provider_instances={"qobuz_1", "qobuz_2"}
        )

    assert result is album
    assert get_.await_args_list == [
        call("album", "qobuz_1", allow_fallback=False, strict_provider_instance=True),
        call("album", "qobuz_2", allow_fallback=False, strict_provider_instance=True),
    ]


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
    # the second artist-credit query still runs and fails transiently, but the
    # already-found exact candidate is kept rather than raising
    assert search_provider.await_count == 2


async def test_exact_mapped_candidate_still_checked_against_search_results(
    music: MusicController,
) -> None:
    """An already-exact mapped candidate does not skip the search-based comparison."""
    mb_track = (
        ExternalID.MB_TRACK,
        "12345678-1234-1234-1234-123456789abc",
    )
    source = create_track("spotify_1", "source")
    source.external_ids.add(mb_track)
    library_track = create_track("spotify_1", "library")
    mapped_candidate = create_track("qobuz_1", "mapped")
    mapped_candidate.external_ids.add(mb_track)
    library_track.provider_mappings.add(next(iter(mapped_candidate.provider_mappings)))
    likely_candidate = create_track("qobuz_1", "likely", isrc="OTHER")
    provider = MagicMock()
    provider.instance_id = "qobuz_1"
    provider.domain = "qobuz"
    provider.supported_features = {ProviderFeature.SEARCH}
    provider.supported_media_types = {MediaType.TRACK}
    candidates = {
        "mapped": mapped_candidate,
        "likely": likely_candidate,
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
            AsyncMock(return_value=SearchResults(tracks=[likely_candidate])),
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

    # the mapped candidate is already exact (shared mb_track), but search must still run
    # so a conflicting or better candidate isn't silently missed
    assert search_provider.await_count == 1
    assert result.match is not None
    assert result.match.track.item_id == "mapped"
    assert result.match.confidence == TrackMatchConfidence.EXACT


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


async def test_find_provider_match_checks_every_artist_credit_query(
    music: MusicController,
) -> None:
    """An exact hit on the first artist-credit query must not skip later credited artists."""
    base = create_track("spotify_1", "base")
    base.artists.append(
        ItemMapping(
            item_id="other-artist",
            provider="spotify_1",
            name="Other Artist",
            media_type=MediaType.ARTIST,
        )
    )
    first_candidate = create_track("qobuz_1", "first")
    second_candidate = create_track("qobuz_1", "second")
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
                    SearchResults(tracks=[first_candidate]),
                    SearchResults(tracks=[second_candidate]),
                )
            ),
        ) as search_provider,
        patch.object(
            music.tracks,
            "get_provider_item",
            AsyncMock(
                side_effect=lambda item_id, *_args, **_kwargs: {
                    "first": first_candidate,
                    "second": second_candidate,
                }[item_id]
            ),
        ),
        # both candidates independently tie the base track at EXACT confidence, so a
        # single artist-credit query must not decide the outcome on its own
        patch.object(
            music.tracks,
            "_get_match_confidence",
            AsyncMock(
                side_effect=lambda _base, _candidate, base_album, **_kw: (
                    TrackMatchConfidence.EXACT,
                    base_album,
                )
            ),
        ),
    ):
        candidates = await music.tracks._search_provider_track_matches(
            base,
            provider,
            TrackMatchConfidence.LOOSE,
            None,
            None,
            True,
            None,
        )

    # the second artist's query must still run, and its candidate must still be
    # collected, even though the first query already produced an exact match
    assert search_provider.await_count == 2
    assert {match.track.item_id for _, match in candidates} == {"first", "second"}


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


async def test_find_provider_match_keeps_fallback_after_later_hydration_failure(
    music: MusicController,
) -> None:
    """A later hydration failure does not discard an acceptable candidate."""
    base = create_track("spotify_1", "base")
    candidate = create_track("qobuz_1", "candidate")
    failing_candidate = create_track("qobuz_1", "failing")
    provider = MagicMock()
    provider.instance_id = "qobuz_1"
    provider.domain = "qobuz"
    provider.supported_features = {ProviderFeature.SEARCH}
    provider.supported_media_types = {MediaType.TRACK}
    get_provider_item = AsyncMock(
        side_effect=(
            candidate,
            ResourceTemporarilyUnavailable("Hydration failed"),
        )
    )

    with (
        patch.object(
            music,
            "search_provider",
            AsyncMock(return_value=SearchResults(tracks=[candidate, failing_candidate])),
        ),
        patch.object(music.tracks, "get_provider_item", get_provider_item),
        patch.object(music.tracks, "_get_full_track_album", AsyncMock(return_value=None)),
    ):
        result = await music.tracks.find_provider_match(
            base,
            provider,
            minimum_confidence=TrackMatchConfidence.LIKELY,
        )

    assert result.match is not None
    assert result.match.track.item_id == candidate.item_id
    assert get_provider_item.await_count == 2


async def test_find_provider_match_skips_invalid_data_search_candidate(
    music: MusicController,
) -> None:
    """An unusable hydrated search result is skipped, not fatal to the whole search."""
    base = create_track("spotify_1", "base")
    unusable_candidate = create_track("qobuz_1", "unreadable")
    good_candidate = create_track("qobuz_1", "candidate")
    provider = MagicMock()
    provider.instance_id = "qobuz_1"
    provider.domain = "qobuz"
    provider.supported_features = {ProviderFeature.SEARCH}
    provider.supported_media_types = {MediaType.TRACK}

    async def get_provider_item(item_id: str, *_args: object, **_kwargs: object) -> Track:
        if item_id == "unreadable":
            raise InvalidDataError("Corrupt provider response")
        return good_candidate

    with (
        patch.object(
            music,
            "search_provider",
            AsyncMock(return_value=SearchResults(tracks=[unusable_candidate, good_candidate])),
        ),
        patch.object(music.tracks, "get_provider_item", AsyncMock(side_effect=get_provider_item)),
        patch.object(music.tracks, "_get_full_track_album", AsyncMock(return_value=None)),
    ):
        result = await music.tracks.find_provider_match(
            base,
            provider,
            minimum_confidence=TrackMatchConfidence.LIKELY,
        )

    assert result.match is not None
    assert result.match.track.item_id == good_candidate.item_id


async def test_search_result_with_asymmetric_composite_credit_is_hydrated(
    music: MusicController,
) -> None:
    """A search result whose artist credit is split differently is not pre-filtered out."""
    base = create_track("spotify_1", "base")
    # the base track carries one composite artist entry, as a third-party M3U would
    base.artists = UniqueList(
        [
            Artist(
                item_id="composite-artist",
                provider="spotify_1",
                name="Artist A, Artist B",
                provider_mappings=set(),
            )
        ]
    )
    # the provider represents the same credit as two separate artists - a plain
    # artist-list comparison never pairwise-matches this against the composite entry
    search_result = create_track("qobuz_1", "candidate")
    search_result.artists = UniqueList(
        [
            Artist(
                item_id="artist-a", provider="qobuz_1", name="Artist A", provider_mappings=set()
            ),
            Artist(
                item_id="artist-b", provider="qobuz_1", name="Artist B", provider_mappings=set()
            ),
        ]
    )
    provider = MagicMock()
    provider.instance_id = "qobuz_1"
    provider.domain = "qobuz"
    provider.supported_features = {ProviderFeature.SEARCH}
    provider.supported_media_types = {MediaType.TRACK}

    get_provider_item = AsyncMock(return_value=search_result)
    with (
        patch.object(
            music,
            "search_provider",
            AsyncMock(return_value=SearchResults(tracks=[search_result])),
        ),
        patch.object(music.tracks, "get_provider_item", get_provider_item),
        patch.object(
            music.tracks,
            "_get_match_confidence",
            AsyncMock(
                side_effect=lambda _base, _candidate, base_album, **_kw: (
                    TrackMatchConfidence.EXACT,
                    base_album,
                )
            ),
        ),
    ):
        candidates = await music.tracks._search_provider_track_matches(
            base,
            provider,
            TrackMatchConfidence.LOOSE,
            None,
            None,
            True,
            None,
        )

    # the candidate must reach hydration (and the full evidence comparator) instead
    # of being discarded by a plain artist-list pre-filter
    get_provider_item.assert_awaited_once()
    assert {match.track.item_id for _, match in candidates} == {"candidate"}


async def test_find_provider_match_falls_through_search_after_invalid_mapped_candidate(
    music: MusicController,
) -> None:
    """A stale/unreadable mapped candidate falls through to search, instead of aborting."""
    source = create_track("qobuz_1", "source")
    search_candidate = create_track("qobuz_1", "found")
    provider = MagicMock()
    provider.instance_id = "qobuz_1"
    provider.domain = "qobuz"
    provider.supported_features = {ProviderFeature.SEARCH}
    provider.supported_media_types = {MediaType.TRACK}

    async def get_provider_item(item_id: str, *_args: object, **_kwargs: object) -> Track:
        if item_id == "source":
            # resolving the trusted mapping directly - simulate a stale, unreadable
            # catalog entry rather than a confirmed removal
            raise InvalidDataError("Unreadable catalog entry")
        return search_candidate

    with (
        patch.object(
            music,
            "search_provider",
            AsyncMock(return_value=SearchResults(tracks=[search_candidate])),
        ),
        patch.object(music.tracks, "get_provider_item", AsyncMock(side_effect=get_provider_item)),
        patch.object(music.tracks, "_get_full_track_album", AsyncMock(return_value=None)),
    ):
        result = await music.tracks.find_provider_match(
            source,
            provider,
            minimum_confidence=TrackMatchConfidence.LIKELY,
            trust_base_mapping=False,
        )

    assert result.match is not None


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


async def test_tied_likely_matches_from_missing_evidence_are_ambiguous(
    music: MusicController,
) -> None:
    """Tied LIKELY matches that disagree (e.g. explicit vs. clean) are ambiguous, not arbitrary."""
    # the base track carries no explicitness metadata, so neither candidate is rejected
    # against it individually - only comparing the candidates to each other exposes the conflict
    base = create_track("spotify_1", "base", isrc="BASE")
    explicit_version = create_track("qobuz_1", "explicit", isrc="EXPLICIT")
    explicit_version.metadata.explicit = True
    clean_version = create_track("qobuz_1", "clean", isrc="CLEAN")
    clean_version.metadata.explicit = False
    provider = MagicMock()
    provider.instance_id = "qobuz_1"
    provider.domain = "qobuz"
    provider.supported_features = {ProviderFeature.SEARCH}
    provider.supported_media_types = {MediaType.TRACK}

    with (
        patch.object(
            music,
            "search_provider",
            AsyncMock(return_value=SearchResults(tracks=[explicit_version, clean_version])),
        ),
        patch.object(
            music.tracks,
            "get_provider_item",
            AsyncMock(
                side_effect=lambda item_id, *_args, **_kwargs: {
                    "explicit": explicit_version,
                    "clean": clean_version,
                }[item_id]
            ),
        ),
        patch.object(music.tracks, "_get_full_track_album", AsyncMock(return_value=None)),
    ):
        result = await music.tracks.find_provider_match(
            base,
            provider,
            minimum_confidence=TrackMatchConfidence.LIKELY,
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

    assert TracksController._matches_are_compatible(matches, TrackMatchConfidence.LOOSE) is False


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
    get_full_track_album.assert_awaited_once_with(source, allowed_provider_instances=None)


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


async def test_enrich_provider_mappings_hydrates_evidence_from_wider_allowed_scope(
    music: MusicController,
) -> None:
    """Narrowing the search targets must not also narrow album-evidence hydration."""
    source = create_track("spotify_1", "source")
    qobuz_provider = MagicMock(spec=MusicProvider)
    qobuz_provider.name = "Qobuz"
    qobuz_provider.instance_id = "qobuz_1"
    qobuz_provider.domain = "qobuz"
    qobuz_provider.available = True
    qobuz_provider.is_streaming_provider = True
    get_full_track_album = AsyncMock(return_value=None)
    find_provider_match = AsyncMock(return_value=TrackProviderMatchResult())

    with (
        patch.object(music.tracks, "get_library_match", AsyncMock(return_value=None)),
        patch.object(music.tracks, "_get_full_track_album", get_full_track_album),
        patch.object(music.tracks, "find_provider_match", find_provider_match),
        patch.object(music.mass, "get_provider", return_value=qobuz_provider),
    ):
        await music.tracks.enrich_provider_mappings(
            source,
            # the search is narrowed to qobuz only, but the source's own album may still
            # live on spotify, which remains part of the caller's wider allowed snapshot
            provider_instance_ids={"qobuz_1"},
            evidence_provider_instances={"qobuz_1", "spotify_1"},
        )

    get_full_track_album.assert_awaited_once_with(
        source, allowed_provider_instances={"qobuz_1", "spotify_1"}
    )
    assert find_provider_match.call_args.kwargs["allowed_provider_instances"] == {
        "qobuz_1",
        "spotify_1",
    }


async def test_enrich_provider_mappings_defaults_evidence_scope_to_search_scope(
    music: MusicController,
) -> None:
    """Without an explicit evidence scope, hydration still respects the search scope."""
    source = create_track("spotify_1", "source")
    qobuz_provider = MagicMock(spec=MusicProvider)
    qobuz_provider.name = "Qobuz"
    qobuz_provider.instance_id = "qobuz_1"
    qobuz_provider.domain = "qobuz"
    qobuz_provider.available = True
    qobuz_provider.is_streaming_provider = True
    get_full_track_album = AsyncMock(return_value=None)
    find_provider_match = AsyncMock(return_value=TrackProviderMatchResult())

    with (
        patch.object(music.tracks, "get_library_match", AsyncMock(return_value=None)),
        patch.object(music.tracks, "_get_full_track_album", get_full_track_album),
        patch.object(music.tracks, "find_provider_match", find_provider_match),
        patch.object(music.mass, "get_provider", return_value=qobuz_provider),
    ):
        await music.tracks.enrich_provider_mappings(
            source,
            provider_instance_ids={"qobuz_1"},
        )

    get_full_track_album.assert_awaited_once_with(source, allowed_provider_instances={"qobuz_1"})


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


async def test_enrich_provider_mappings_prefers_stronger_match_within_same_domain(
    music: MusicController,
) -> None:
    """A weaker match on one account does not stop a stronger one on a sibling account."""
    source = create_track("spotify_1", "source")
    loose_track = create_track("qobuz_1", "loose-track")
    loose_mapping = next(iter(loose_track.provider_mappings))
    exact_track = create_track("qobuz_2", "exact-track")
    exact_mapping = next(iter(exact_track.provider_mappings))
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
    loose_match = TrackProviderMatch(
        track=loose_track, mapping=loose_mapping, confidence=TrackMatchConfidence.LOOSE
    )
    exact_match = TrackProviderMatch(
        track=exact_track, mapping=exact_mapping, confidence=TrackMatchConfidence.EXACT
    )
    results = {
        "qobuz_1": TrackProviderMatchResult(match=loose_match),
        "qobuz_2": TrackProviderMatchResult(match=exact_match),
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

    # both accounts must be evaluated - an early LOOSE hit on the first must not
    # prevent the stronger EXACT hit on the second from being found and preferred
    assert find_match.await_count == 2
    assert result.matches == (exact_match,)
    assert exact_mapping in result.track.provider_mappings
    assert loose_mapping not in result.track.provider_mappings


async def test_enrich_provider_mappings_prefers_higher_quality_among_compatible_ties(
    music: MusicController,
) -> None:
    """Compatible top-tier matches on sibling accounts are resolved by mapping quality."""
    source = create_track("spotify_1", "source")
    # a shared MB_TRACK id makes the two candidates EXACT-compatible with each other -
    # required for an EXACT tie-break now that same tier requires same-release evidence,
    # not just an agreeing ISRC, between the tied candidates themselves
    mb_track = (ExternalID.MB_TRACK, "12345678-1234-1234-1234-123456789abc")
    lossy_track = create_track("qobuz_1", "lossy-track")
    lossy_track.external_ids.add(mb_track)
    lossy_mapping = next(iter(lossy_track.provider_mappings))
    lossy_mapping.audio_format = AudioFormat(content_type=ContentType.MP3)
    lossless_track = create_track("qobuz_2", "lossless-track")
    lossless_track.external_ids.add(mb_track)
    lossless_mapping = next(iter(lossless_track.provider_mappings))
    lossless_mapping.audio_format = AudioFormat(content_type=ContentType.FLAC)
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
    # qobuz_1 sorts first and would win under a naive "first in iteration order" pick,
    # even though qobuz_2's mapping is the higher-quality one
    lossy_match = TrackProviderMatch(
        track=lossy_track, mapping=lossy_mapping, confidence=TrackMatchConfidence.EXACT
    )
    lossless_match = TrackProviderMatch(
        track=lossless_track, mapping=lossless_mapping, confidence=TrackMatchConfidence.EXACT
    )
    results = {
        "qobuz_1": TrackProviderMatchResult(match=lossy_match),
        "qobuz_2": TrackProviderMatchResult(match=lossless_match),
    }

    with (
        patch.object(music.tracks, "get_library_match", AsyncMock(return_value=None)),
        patch.object(music.tracks, "_get_full_track_album", AsyncMock(return_value=None)),
        patch.object(
            music.tracks,
            "find_provider_match",
            AsyncMock(
                side_effect=lambda _track, provider, **_kwargs: results[provider.instance_id]
            ),
        ),
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

    assert result.matches == (lossless_match,)
    assert lossless_mapping in result.track.provider_mappings
    assert lossy_mapping not in result.track.provider_mappings


async def test_enrich_provider_mappings_treats_tied_conflicting_matches_as_ambiguous(
    music: MusicController,
) -> None:
    """Two same-confidence providers that disagree with each other are both ambiguous."""
    source = create_track("spotify_1", "source")
    # missing explicitness evidence on the source lets each candidate independently tie
    # with it, but the two candidates plainly disagree with each other (explicit vs. clean)
    qobuz_track = create_track("qobuz_1", "qobuz-track", isrc="QOBUZ")
    qobuz_track.metadata.explicit = True
    qobuz_mapping = next(iter(qobuz_track.provider_mappings))
    deezer_track = create_track("deezer_1", "deezer-track", isrc="DEEZER")
    deezer_track.metadata.explicit = False
    deezer_mapping = next(iter(deezer_track.provider_mappings))
    qobuz_provider = MagicMock(spec=MusicProvider)
    qobuz_provider.name = "Qobuz"
    qobuz_provider.instance_id = "qobuz_1"
    qobuz_provider.domain = "qobuz"
    qobuz_provider.available = True
    qobuz_provider.is_streaming_provider = True
    deezer_provider = MagicMock(spec=MusicProvider)
    deezer_provider.name = "Deezer"
    deezer_provider.instance_id = "deezer_1"
    deezer_provider.domain = "deezer"
    deezer_provider.available = True
    deezer_provider.is_streaming_provider = True
    qobuz_match = TrackProviderMatch(
        track=qobuz_track, mapping=qobuz_mapping, confidence=TrackMatchConfidence.LIKELY
    )
    deezer_match = TrackProviderMatch(
        track=deezer_track, mapping=deezer_mapping, confidence=TrackMatchConfidence.LIKELY
    )
    results = {
        "qobuz_1": TrackProviderMatchResult(match=qobuz_match),
        "deezer_1": TrackProviderMatchResult(match=deezer_match),
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
        ),
        patch.object(
            music.mass,
            "get_provider",
            side_effect=lambda provider_instance_id, **_kwargs: {
                "qobuz_1": qobuz_provider,
                "deezer_1": deezer_provider,
            }[provider_instance_id],
        ),
    ):
        result = await music.tracks.enrich_provider_mappings(
            source,
            provider_instance_ids={"qobuz_1", "deezer_1"},
        )

    # neither can be trusted over the other at the same confidence - a tie-break
    # would otherwise pick one arbitrarily based on which provider was visited first
    assert result.matches == ()
    assert set(result.ambiguous_providers) == {"Qobuz", "Deezer"}
    assert qobuz_mapping not in result.track.provider_mappings
    assert deezer_mapping not in result.track.provider_mappings


async def test_enrich_provider_mappings_treats_conflicting_exact_release_evidence_as_ambiguous(
    music: MusicController,
) -> None:
    """Two independently EXACT candidates with conflicting MB_TRACK IDs are ambiguous."""
    source = create_track("spotify_1", "source")
    # same ISRC/duration ties the two candidates at LIKELY release evidence, but each
    # references a different MusicBrainz release track - they cannot both be the
    # authoritative EXACT release, even though each independently matched as EXACT
    qobuz_track = create_track("qobuz_1", "qobuz-track")
    qobuz_track.external_ids.add((ExternalID.MB_TRACK, "11111111-1111-1111-1111-111111111111"))
    qobuz_mapping = next(iter(qobuz_track.provider_mappings))
    deezer_track = create_track("deezer_1", "deezer-track")
    deezer_track.external_ids.add((ExternalID.MB_TRACK, "22222222-2222-2222-2222-222222222222"))
    deezer_mapping = next(iter(deezer_track.provider_mappings))
    qobuz_provider = MagicMock(spec=MusicProvider)
    qobuz_provider.name = "Qobuz"
    qobuz_provider.instance_id = "qobuz_1"
    qobuz_provider.domain = "qobuz"
    qobuz_provider.available = True
    qobuz_provider.is_streaming_provider = True
    deezer_provider = MagicMock(spec=MusicProvider)
    deezer_provider.name = "Deezer"
    deezer_provider.instance_id = "deezer_1"
    deezer_provider.domain = "deezer"
    deezer_provider.available = True
    deezer_provider.is_streaming_provider = True
    qobuz_match = TrackProviderMatch(
        track=qobuz_track, mapping=qobuz_mapping, confidence=TrackMatchConfidence.EXACT
    )
    deezer_match = TrackProviderMatch(
        track=deezer_track, mapping=deezer_mapping, confidence=TrackMatchConfidence.EXACT
    )
    results = {
        "qobuz_1": TrackProviderMatchResult(match=qobuz_match),
        "deezer_1": TrackProviderMatchResult(match=deezer_match),
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
        ),
        patch.object(
            music.mass,
            "get_provider",
            side_effect=lambda provider_instance_id, **_kwargs: {
                "qobuz_1": qobuz_provider,
                "deezer_1": deezer_provider,
            }[provider_instance_id],
        ),
    ):
        result = await music.tracks.enrich_provider_mappings(
            source,
            provider_instance_ids={"qobuz_1", "deezer_1"},
        )

    # a shared ISRC/duration only ties them at LIKELY once their conflicting release
    # evidence is considered - neither can be trusted as the selected EXACT release
    assert result.matches == ()
    assert set(result.ambiguous_providers) == {"Qobuz", "Deezer"}
    assert qobuz_mapping not in result.track.provider_mappings
    assert deezer_mapping not in result.track.provider_mappings


async def test_enrich_provider_mappings_same_domain_tie_blocks_weaker_cross_domain_match(
    music: MusicController,
) -> None:
    """An unresolved same-domain tie at EXACT blocks a weaker match from another domain."""
    source = create_track("spotify_1", "source")
    # two personal accounts on the same domain each independently match as EXACT but
    # reference a different MusicBrainz release track, so _select_best_match_per_domain
    # discards both as an unresolved same-domain tie - deezer's confident but merely
    # LIKELY match must not be quietly accepted in their place afterwards
    qobuz_a_track = create_track("qobuz_a", "qobuz-a-track")
    qobuz_a_track.external_ids.add((ExternalID.MB_TRACK, "11111111-1111-1111-1111-111111111111"))
    qobuz_a_mapping = next(iter(qobuz_a_track.provider_mappings))
    qobuz_b_track = create_track("qobuz_b", "qobuz-b-track")
    qobuz_b_track.external_ids.add((ExternalID.MB_TRACK, "22222222-2222-2222-2222-222222222222"))
    qobuz_b_mapping = next(iter(qobuz_b_track.provider_mappings))
    deezer_track = create_track("deezer_1", "deezer-track")
    deezer_mapping = next(iter(deezer_track.provider_mappings))
    qobuz_a_provider = MagicMock(spec=MusicProvider)
    qobuz_a_provider.name = "Qobuz A"
    qobuz_a_provider.instance_id = "qobuz_a"
    qobuz_a_provider.domain = "qobuz"
    qobuz_a_provider.available = True
    qobuz_a_provider.is_streaming_provider = True
    qobuz_b_provider = MagicMock(spec=MusicProvider)
    qobuz_b_provider.name = "Qobuz B"
    qobuz_b_provider.instance_id = "qobuz_b"
    qobuz_b_provider.domain = "qobuz"
    qobuz_b_provider.available = True
    qobuz_b_provider.is_streaming_provider = True
    deezer_provider = MagicMock(spec=MusicProvider)
    deezer_provider.name = "Deezer"
    deezer_provider.instance_id = "deezer_1"
    deezer_provider.domain = "deezer"
    deezer_provider.available = True
    deezer_provider.is_streaming_provider = True
    qobuz_a_match = TrackProviderMatch(
        track=qobuz_a_track, mapping=qobuz_a_mapping, confidence=TrackMatchConfidence.EXACT
    )
    qobuz_b_match = TrackProviderMatch(
        track=qobuz_b_track, mapping=qobuz_b_mapping, confidence=TrackMatchConfidence.EXACT
    )
    deezer_match = TrackProviderMatch(
        track=deezer_track, mapping=deezer_mapping, confidence=TrackMatchConfidence.LIKELY
    )
    results = {
        "qobuz_a": TrackProviderMatchResult(match=qobuz_a_match),
        "qobuz_b": TrackProviderMatchResult(match=qobuz_b_match),
        "deezer_1": TrackProviderMatchResult(match=deezer_match),
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
        ),
        patch.object(
            music.mass,
            "get_provider",
            side_effect=lambda provider_instance_id, **_kwargs: {
                "qobuz_a": qobuz_a_provider,
                "qobuz_b": qobuz_b_provider,
                "deezer_1": deezer_provider,
            }[provider_instance_id],
        ),
    ):
        result = await music.tracks.enrich_provider_mappings(
            source,
            provider_instance_ids={"qobuz_a", "qobuz_b", "deezer_1"},
        )

    assert result.matches == ()
    assert "Deezer" in result.ambiguous_providers
    assert deezer_mapping not in result.track.provider_mappings


async def test_enrich_provider_mappings_rejects_weaker_match_when_stronger_tier_is_ambiguous(
    music: MusicController,
) -> None:
    """A provider's own unresolved tie at a higher tier blocks a weaker confident match."""
    source = create_track("spotify_1", "source")
    # qobuz's own candidates tied ambiguously at EXACT and were discarded entirely, so
    # deezer's confident but merely LIKELY match must not be quietly accepted in its
    # place - that would substitute weaker, uncontested-looking evidence for a
    # disagreement that was never actually resolved
    deezer_track = create_track("deezer_1", "deezer-track")
    deezer_mapping = next(iter(deezer_track.provider_mappings))
    qobuz_provider = MagicMock(spec=MusicProvider)
    qobuz_provider.name = "Qobuz"
    qobuz_provider.instance_id = "qobuz_1"
    qobuz_provider.domain = "qobuz"
    qobuz_provider.available = True
    qobuz_provider.is_streaming_provider = True
    deezer_provider = MagicMock(spec=MusicProvider)
    deezer_provider.name = "Deezer"
    deezer_provider.instance_id = "deezer_1"
    deezer_provider.domain = "deezer"
    deezer_provider.available = True
    deezer_provider.is_streaming_provider = True
    deezer_match = TrackProviderMatch(
        track=deezer_track, mapping=deezer_mapping, confidence=TrackMatchConfidence.LIKELY
    )
    results = {
        "qobuz_1": TrackProviderMatchResult(
            ambiguous=True, ambiguous_confidence=TrackMatchConfidence.EXACT
        ),
        "deezer_1": TrackProviderMatchResult(match=deezer_match),
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
        ),
        patch.object(
            music.mass,
            "get_provider",
            side_effect=lambda provider_instance_id, **_kwargs: {
                "qobuz_1": qobuz_provider,
                "deezer_1": deezer_provider,
            }[provider_instance_id],
        ),
    ):
        result = await music.tracks.enrich_provider_mappings(
            source,
            provider_instance_ids={"qobuz_1", "deezer_1"},
        )

    assert result.matches == ()
    assert set(result.ambiguous_providers) == {"Qobuz", "Deezer"}
    assert deezer_mapping not in result.track.provider_mappings


async def test_enrich_provider_mappings_accepts_stronger_match_despite_weaker_tier_ambiguity(
    music: MusicController,
) -> None:
    """A confident, strictly stronger match is accepted despite a weaker unresolved tie."""
    source = create_track("spotify_1", "source")
    # qobuz's own candidates only tied ambiguously at LOOSE, which is weaker evidence
    # than deezer's confident EXACT match - the stronger, uncontested match should
    # still be trusted since it isn't the tier that was actually left unresolved
    deezer_track = create_track("deezer_1", "deezer-track")
    deezer_mapping = next(iter(deezer_track.provider_mappings))
    qobuz_provider = MagicMock(spec=MusicProvider)
    qobuz_provider.name = "Qobuz"
    qobuz_provider.instance_id = "qobuz_1"
    qobuz_provider.domain = "qobuz"
    qobuz_provider.available = True
    qobuz_provider.is_streaming_provider = True
    deezer_provider = MagicMock(spec=MusicProvider)
    deezer_provider.name = "Deezer"
    deezer_provider.instance_id = "deezer_1"
    deezer_provider.domain = "deezer"
    deezer_provider.available = True
    deezer_provider.is_streaming_provider = True
    deezer_match = TrackProviderMatch(
        track=deezer_track, mapping=deezer_mapping, confidence=TrackMatchConfidence.EXACT
    )
    results = {
        "qobuz_1": TrackProviderMatchResult(
            ambiguous=True, ambiguous_confidence=TrackMatchConfidence.LOOSE
        ),
        "deezer_1": TrackProviderMatchResult(match=deezer_match),
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
        ),
        patch.object(
            music.mass,
            "get_provider",
            side_effect=lambda provider_instance_id, **_kwargs: {
                "qobuz_1": qobuz_provider,
                "deezer_1": deezer_provider,
            }[provider_instance_id],
        ),
    ):
        result = await music.tracks.enrich_provider_mappings(
            source,
            provider_instance_ids={"qobuz_1", "deezer_1"},
        )

    assert result.matches == (deezer_match,)
    assert result.ambiguous_providers == ("Qobuz",)
    assert deezer_mapping in result.track.provider_mappings


async def test_enrich_provider_mappings_prefers_higher_confidence_over_visitation_order(
    music: MusicController,
) -> None:
    """A later provider's stronger match wins over an earlier, weaker, conflicting one."""
    source = create_track("spotify_1", "source")
    # "aaa" is visited first (alphabetically) but only ties at LOOSE, while the
    # conflicting "zzz" match is visited later yet is a much stronger EXACT hit
    loose_track = create_track("aaa_1", "loose-track", isrc="LOOSE")
    loose_track.metadata.explicit = True
    loose_mapping = next(iter(loose_track.provider_mappings))
    exact_track = create_track("zzz_1", "exact-track", isrc="EXACT")
    exact_track.metadata.explicit = False
    exact_mapping = next(iter(exact_track.provider_mappings))
    loose_provider = MagicMock(spec=MusicProvider)
    loose_provider.name = "Aaa"
    loose_provider.instance_id = "aaa_1"
    loose_provider.domain = "aaa"
    loose_provider.available = True
    loose_provider.is_streaming_provider = True
    exact_provider = MagicMock(spec=MusicProvider)
    exact_provider.name = "Zzz"
    exact_provider.instance_id = "zzz_1"
    exact_provider.domain = "zzz"
    exact_provider.available = True
    exact_provider.is_streaming_provider = True
    loose_match = TrackProviderMatch(
        track=loose_track, mapping=loose_mapping, confidence=TrackMatchConfidence.LOOSE
    )
    exact_match = TrackProviderMatch(
        track=exact_track, mapping=exact_mapping, confidence=TrackMatchConfidence.EXACT
    )
    results = {
        "aaa_1": TrackProviderMatchResult(match=loose_match),
        "zzz_1": TrackProviderMatchResult(match=exact_match),
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
        ),
        patch.object(
            music.mass,
            "get_provider",
            side_effect=lambda provider_instance_id, **_kwargs: {
                "aaa_1": loose_provider,
                "zzz_1": exact_provider,
            }[provider_instance_id],
        ),
    ):
        result = await music.tracks.enrich_provider_mappings(
            source,
            provider_instance_ids={"aaa_1", "zzz_1"},
        )

    assert result.matches == (exact_match,)
    assert "Aaa" in result.ambiguous_providers
    assert loose_mapping not in result.track.provider_mappings
    assert exact_mapping in result.track.provider_mappings


async def test_enrich_provider_mappings_stops_at_ambiguous_top_tier(
    music: MusicController,
) -> None:
    """A conflicting top tier blocks a weaker, otherwise-clean, tier from being accepted."""
    source = create_track("spotify_1", "source")
    # qobuz and deezer tie at the strongest tier (EXACT) but disagree with each other
    qobuz_track = create_track("qobuz_1", "qobuz-track", isrc="QOBUZ")
    qobuz_track.metadata.explicit = True
    qobuz_mapping = next(iter(qobuz_track.provider_mappings))
    deezer_track = create_track("deezer_1", "deezer-track", isrc="DEEZER")
    deezer_track.metadata.explicit = False
    deezer_mapping = next(iter(deezer_track.provider_mappings))
    # aaa is a single, internally-consistent match, but only at a weaker tier (LIKELY)
    aaa_track = create_track("aaa_1", "aaa-track", isrc="AAA")
    aaa_mapping = next(iter(aaa_track.provider_mappings))
    qobuz_provider = MagicMock(spec=MusicProvider)
    qobuz_provider.name = "Qobuz"
    qobuz_provider.instance_id = "qobuz_1"
    qobuz_provider.domain = "qobuz"
    qobuz_provider.available = True
    qobuz_provider.is_streaming_provider = True
    deezer_provider = MagicMock(spec=MusicProvider)
    deezer_provider.name = "Deezer"
    deezer_provider.instance_id = "deezer_1"
    deezer_provider.domain = "deezer"
    deezer_provider.available = True
    deezer_provider.is_streaming_provider = True
    aaa_provider = MagicMock(spec=MusicProvider)
    aaa_provider.name = "Aaa"
    aaa_provider.instance_id = "aaa_1"
    aaa_provider.domain = "aaa"
    aaa_provider.available = True
    aaa_provider.is_streaming_provider = True
    qobuz_match = TrackProviderMatch(
        track=qobuz_track, mapping=qobuz_mapping, confidence=TrackMatchConfidence.EXACT
    )
    deezer_match = TrackProviderMatch(
        track=deezer_track, mapping=deezer_mapping, confidence=TrackMatchConfidence.EXACT
    )
    aaa_match = TrackProviderMatch(
        track=aaa_track, mapping=aaa_mapping, confidence=TrackMatchConfidence.LIKELY
    )
    results = {
        "qobuz_1": TrackProviderMatchResult(match=qobuz_match),
        "deezer_1": TrackProviderMatchResult(match=deezer_match),
        "aaa_1": TrackProviderMatchResult(match=aaa_match),
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
        ),
        patch.object(
            music.mass,
            "get_provider",
            side_effect=lambda provider_instance_id, **_kwargs: {
                "qobuz_1": qobuz_provider,
                "deezer_1": deezer_provider,
                "aaa_1": aaa_provider,
            }[provider_instance_id],
        ),
    ):
        result = await music.tracks.enrich_provider_mappings(
            source,
            provider_instance_ids={"qobuz_1", "deezer_1", "aaa_1"},
        )

    # the conflicting EXACT tier can't be resolved, and a weaker LIKELY match doesn't
    # settle which EXACT candidate was right - it must not be substituted in instead
    assert result.matches == ()
    assert set(result.ambiguous_providers) == {"Qobuz", "Deezer"}
    assert "Aaa" not in result.ambiguous_providers
    assert qobuz_mapping not in result.track.provider_mappings
    assert deezer_mapping not in result.track.provider_mappings
    assert aaa_mapping not in result.track.provider_mappings


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
    # the second call skips querying (already confirmed failed) but must still report
    # the instance as failed, or this entry would be misattributed as "no match found"
    assert second_result.failed_providers == ("qobuz_1",)


async def test_enrich_provider_mappings_reports_unavailable_allowed_provider(
    music: MusicController,
) -> None:
    """An allowed instance that is currently unreachable is reported, not silently skipped."""
    source = create_track("spotify_1", "source")
    provider = MagicMock(spec=MusicProvider)
    provider.name = "Qobuz"
    provider.instance_id = "qobuz_1"
    provider.domain = "qobuz"
    provider.available = False
    provider.is_streaming_provider = True
    failed_provider_instances: set[str] = set()

    with (
        patch.object(music.tracks, "get_library_match", AsyncMock(return_value=None)),
        patch.object(music.tracks, "_get_full_track_album", AsyncMock(return_value=None)),
        patch.object(music.tracks, "find_provider_match", AsyncMock()) as find_match,
        patch.object(music.mass, "get_provider", return_value=provider),
    ):
        result = await music.tracks.enrich_provider_mappings(
            source,
            provider_instance_ids={"qobuz_1"},
            failed_provider_instances=failed_provider_instances,
        )

    # never queried - the caller should still learn this instance couldn't be tried
    find_match.assert_not_awaited()
    assert result.failed_providers == ("Qobuz",)
    assert failed_provider_instances == {"qobuz_1"}


async def test_enrich_provider_mappings_reports_fully_unloaded_allowed_provider(
    music: MusicController,
) -> None:
    """An allowed instance that has fully unloaded is reported, not silently skipped."""
    source = create_track("spotify_1", "source")
    failed_provider_instances: set[str] = set()

    with (
        patch.object(music.tracks, "get_library_match", AsyncMock(return_value=None)),
        patch.object(music.tracks, "_get_full_track_album", AsyncMock(return_value=None)),
        patch.object(music.tracks, "find_provider_match", AsyncMock()) as find_match,
        # no provider object left at all for this instance - fully unregistered
        patch.object(music.mass, "get_provider", return_value=None),
    ):
        result = await music.tracks.enrich_provider_mappings(
            source,
            provider_instance_ids={"qobuz_1"},
            failed_provider_instances=failed_provider_instances,
        )

    # never queried - the caller should still learn this instance couldn't be tried,
    # using its instance id as display name since no provider object remains
    find_match.assert_not_awaited()
    assert result.failed_providers == ("qobuz_1",)
    assert failed_provider_instances == {"qobuz_1"}


def test_get_provider_mapping_breaks_quality_ties_deterministically() -> None:
    """Equal-quality domain mappings resolve to the same one regardless of set order."""
    provider = MagicMock(spec=MusicProvider)
    provider.instance_id = "qobuz_3"
    provider.domain = "qobuz"
    # both mappings share the same quality and neither is unique, so only the
    # identity tie-breaker (domain, instance, item id) can decide between them
    mapping_a = ProviderMapping(
        item_id="track_a",
        provider_domain="qobuz",
        provider_instance="qobuz_1",
        audio_format=AudioFormat(),
    )
    mapping_b = ProviderMapping(
        item_id="track_b",
        provider_domain="qobuz",
        provider_instance="qobuz_2",
        audio_format=AudioFormat(),
    )
    # a real set's iteration order is stable within one process regardless of
    # insertion order, so a plain list is used here to control the input order
    # directly and prove the tie-break, not just the incidental set layout
    track_forward = create_track("spotify_1", "source")
    track_forward.provider_mappings = cast("set[ProviderMapping]", [mapping_a, mapping_b])
    track_reversed = create_track("spotify_1", "source")
    track_reversed.provider_mappings = cast("set[ProviderMapping]", [mapping_b, mapping_a])

    resolved_forward = TracksController._get_provider_mapping(track_forward, provider)
    resolved_reversed = TracksController._get_provider_mapping(track_reversed, provider)

    assert resolved_forward is not None
    assert resolved_forward.item_id == "track_a"
    assert resolved_reversed is not None
    assert resolved_reversed.item_id == "track_a"


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
