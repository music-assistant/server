"""Regression coverage for support#7079 genre exclusions."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import Genre, ProviderMapping, Track

from music_assistant.providers.smart_playlist import SmartPlaylistProvider
from music_assistant.providers.smart_playlist.helpers import SmartPlaylistRules


def _plugin() -> tuple[SmartPlaylistProvider, MagicMock]:
    mass = MagicMock()
    manifest = MagicMock(domain="smart_playlist")
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    mass.music.genres.get_track_ids_for_genres = AsyncMock(return_value=set())
    return SmartPlaylistProvider(mass, manifest, config, set()), mass


def _track(item_id: str = "1", provider: str = "library") -> Track:
    track = Track(
        item_id=item_id,
        provider=provider,
        name="The Night Santa Went Crazy",
        provider_mappings={
            ProviderMapping(
                item_id=item_id,
                provider_domain="spotify",
                provider_instance="spotify--1",
                available=True,
            )
        },
    )
    track.metadata.genres = {"Pop"}
    return track


@pytest.mark.parametrize("seed", [False, True])
@pytest.mark.parametrize(
    ("tags", "mapped", "stored_name"),
    [
        ({"Christmas Music", "Pop", "Music"}, False, "Christmas Music"),
        ({"Pop"}, True, "Christmas Music"),
        ({"Xmas", "Pop"}, False, "Christmas Music"),
        ({"Christmas Music", "Pop"}, False, "Old genre name"),
    ],
)
async def test_genre_exclusions_use_membership_and_current_aliases(
    seed: bool, tags: set[str], mapped: bool, stored_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Apply the real evaluator to both seed and library candidates with existing tags."""
    plugin, mass = _plugin()
    track = _track()
    track.metadata.genres = set(tags)
    genre = Genre(
        item_id="10",
        provider="library",
        name="Christmas Music",
        provider_mappings=set(),
        genre_aliases={"Holiday", "Xmas"},
    )
    mass.music.genres.get_library_item = AsyncMock(return_value=genre)
    mass.music.genres.get_track_ids_for_genres = AsyncMock(return_value={1} if mapped else set())
    monkeypatch.setattr(plugin, "_get_library_tracks", AsyncMock(return_value=[track]))
    monkeypatch.setattr(plugin, "_tracks_from_seeds", AsyncMock(return_value=[track]))
    rules = SmartPlaylistRules(
        seed_track_uris=["library://track/1"] if seed else [],
        excluded_genre_ids=[10],
        excluded_genre_names={10: stored_name},
    )

    assert await plugin._evaluate_rules(rules) == []
    if mapped:
        mass.music.genres.get_track_ids_for_genres.assert_awaited_once_with([10], track_ids=[1])
    else:
        mass.music.genres.get_track_ids_for_genres.assert_not_awaited()
    assert track.metadata.genres == tags


async def test_unrelated_christmas_genres_are_not_implicitly_merged() -> None:
    """A distinct genre and its old saved name do not exclude the current tag."""
    plugin, mass = _plugin()
    mass.music.genres.get_library_item = AsyncMock(
        return_value=Genre(
            item_id="62", provider="library", name="Christmas", provider_mappings=set()
        )
    )
    names = await plugin._resolve_excluded_genre_names(
        SmartPlaylistRules(excluded_genre_ids=[62], excluded_genre_names={62: "Christmas Music"})
    )
    track = _track()
    track.metadata.genres = {"Christmas Music", "Pop"}
    assert names == {"christmas"}
    assert plugin._apply_exclusions([track], SmartPlaylistRules(), names) == [track]


async def test_missing_genre_uses_saved_name_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    """Keep the saved fallback when a selected genre has been deleted."""
    plugin, mass = _plugin()
    mass.music.genres.get_library_item = AsyncMock(side_effect=MediaNotFoundError("gone"))
    rules = SmartPlaylistRules(
        excluded_genre_ids=[10], excluded_genre_names={10: "Christmas Music"}
    )
    assert await plugin._resolve_excluded_genre_names(rules) == {"christmas music"}
    assert "no longer exists" in caplog.text


async def test_genre_lookup_errors_are_not_silently_ignored() -> None:
    """Unexpected lookup failures propagate instead of weakening exclusions silently."""
    plugin, mass = _plugin()
    mass.music.genres.get_library_item = AsyncMock(side_effect=RuntimeError("database"))
    with pytest.raises(RuntimeError, match="database"):
        await plugin._resolve_excluded_genre_names(SmartPlaylistRules(excluded_genre_ids=[10]))


async def test_provider_candidates_resolve_in_batches_across_all_mappings() -> None:
    """Provider item IDs are resolved instead of being mistaken for library IDs."""
    plugin, mass = _plugin()
    tracks = [_track("1", "spotify--1"), _track("2", "spotify--1")]
    library = _track("99")
    library.provider_mappings = tracks[0].provider_mappings | tracks[1].provider_mappings
    mass.music.tracks.get_library_items_by_prov_id = AsyncMock(return_value=[library])

    resolved = await plugin._build_track_id_map(tracks)

    assert {id(track) for track in resolved[99]} == {id(track) for track in tracks}
    mass.music.tracks.get_library_items_by_prov_id.assert_awaited_once_with(
        provider_instance_id_or_domain="spotify--1",
        provider_item_ids=["1", "2"],
        limit=0,
    )


async def test_all_provider_mappings_can_resolve_the_same_candidate() -> None:
    """An unmatched mapping must not hide another mapping's library association."""
    plugin, mass = _plugin()
    track = _track("source-id", "spotify--1")
    track.provider_mappings.add(
        ProviderMapping(item_id="other-id", provider_domain="qobuz", provider_instance="qobuz--1")
    )
    library = _track("99")
    library.provider_mappings = {
        mapping for mapping in track.provider_mappings if mapping.provider_domain == "qobuz"
    }

    async def resolve(provider_instance_id_or_domain: str, **_kwargs: object) -> list[Track]:
        return [library] if provider_instance_id_or_domain == "qobuz--1" else []

    mass.music.tracks.get_library_items_by_prov_id = AsyncMock(side_effect=resolve)
    assert await plugin._build_track_id_map([track]) == {99: [track]}
    assert mass.music.tracks.get_library_items_by_prov_id.await_count == 2


async def test_provider_identity_resolution_batches_without_result_truncation() -> None:
    """Resolve each input batch completely without an extra pagination query."""
    plugin, mass = _plugin()
    tracks = [_track(str(index), "spotify--1") for index in range(501)]
    library = [_track(str(index + 1000)) for index in range(501)]
    for candidate, stored in zip(tracks, library, strict=True):
        stored.provider_mappings = candidate.provider_mappings
    mass.music.tracks.get_library_items_by_prov_id = AsyncMock(
        side_effect=[library[:500], library[500:]]
    )
    resolved = await plugin._build_track_id_map(tracks)
    assert len(resolved) == 501
    assert resolved[1500] == [tracks[-1]]
    calls = mass.music.tracks.get_library_items_by_prov_id.await_args_list
    assert [(len(call.kwargs["provider_item_ids"]), call.kwargs["limit"]) for call in calls] == [
        (500, 0),
        (1, 0),
    ]


async def test_matching_raw_tags_avoid_provider_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """Do not resolve a provider track already rejected by its genre tags."""
    plugin, mass = _plugin()
    track = _track("provider-id", "spotify--1")
    track.metadata.genres = {"Christmas Music", "Pop"}
    mass.music.genres.get_library_item = AsyncMock(
        return_value=Genre(
            item_id="10", provider="library", name="Christmas Music", provider_mappings=set()
        )
    )
    mass.music.tracks.get_library_items_by_prov_id = AsyncMock()
    monkeypatch.setattr(plugin, "_get_library_tracks", AsyncMock(return_value=[track]))
    assert await plugin._evaluate_rules(SmartPlaylistRules(excluded_genre_ids=[10])) == []
    mass.music.tracks.get_library_items_by_prov_id.assert_not_awaited()
    mass.music.genres.get_track_ids_for_genres.assert_not_awaited()


async def test_unmapped_candidates_do_not_query_membership(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep unknown provider candidates without issuing an empty membership query."""
    plugin, mass = _plugin()
    track = _track("provider-id", "spotify--1")
    mass.music.genres.get_library_item = AsyncMock(
        return_value=Genre(
            item_id="10", provider="library", name="Christmas Music", provider_mappings=set()
        )
    )
    mass.music.tracks.get_library_items_by_prov_id = AsyncMock(return_value=[])
    monkeypatch.setattr(plugin, "_get_library_tracks", AsyncMock(return_value=[track]))
    assert await plugin._evaluate_rules(SmartPlaylistRules(excluded_genre_ids=[10])) == [track]
    mass.music.tracks.get_library_items_by_prov_id.assert_awaited_once()
    mass.music.genres.get_track_ids_for_genres.assert_not_awaited()
