"""Tests for the sonic similarity plugin API parameter handling and metadata filters."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from music_assistant.providers.sonic_similarity.constants import (
    METADATA_BONUS_SCALE,
    SIMILARITY_PRESETS,
)
from music_assistant.providers.sonic_similarity.helpers import (
    _parse_similar_params,
    _parse_weights,
    apply_filters,
    format_text_query,
)
from music_assistant.providers.sonic_similarity.similarity import Candidate, ScoredCandidate
from music_assistant.providers.sonic_similarity.vectors import FEATURE_GROUPS
from tests.providers.sonic_similarity.conftest import make_track

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any


class TestParseSimilarParams:
    """Tests for _parse_similar_params validation and normalization."""

    def test_item_id_alias(self) -> None:
        """Single item_id string wraps into item_ids list."""
        params = _parse_similar_params(item_id="abc")
        assert params.item_ids == ["abc"]

    def test_item_ids_list(self) -> None:
        """item_ids list passes through directly."""
        params = _parse_similar_params(item_ids=["a", "b"])
        assert params.item_ids == ["a", "b"]

    def test_item_id_and_item_ids_prefers_ids(self) -> None:
        """When both provided, item_ids takes precedence."""
        params = _parse_similar_params(item_id="old", item_ids=["new"])
        assert params.item_ids == ["new"]

    def test_no_ids_raises(self) -> None:
        """Must provide at least item_id or item_ids."""
        with pytest.raises(ValueError, match="item_id"):
            _parse_similar_params()

    def test_limit_clamped(self) -> None:
        """Limit is clamped to [1, 100]."""
        assert _parse_similar_params(item_id="x", limit=0).limit == 1
        assert _parse_similar_params(item_id="x", limit=200).limit == 100
        assert _parse_similar_params(item_id="x", limit=50).limit == 50

    def test_depth_clamped(self) -> None:
        """Depth is clamped to [1, 5]."""
        assert _parse_similar_params(item_id="x", depth=0).depth == 1
        assert _parse_similar_params(item_id="x", depth=10).depth == 5

    def test_diversity_clamped(self) -> None:
        """Diversity is clamped to [0.0, 1.0]."""
        assert _parse_similar_params(item_id="x", diversity=-1.0).diversity == 0.0
        assert _parse_similar_params(item_id="x", diversity=5.0).diversity == 1.0

    def test_blend_mode_validated(self) -> None:
        """Invalid blend_mode falls back to centroid."""
        assert _parse_similar_params(item_id="x", blend_mode="centroid").blend_mode == "centroid"
        assert _parse_similar_params(item_id="x", blend_mode="union").blend_mode == "union"
        assert _parse_similar_params(item_id="x", blend_mode="invalid").blend_mode == "centroid"

    def test_seed_weights_length_validated(self) -> None:
        """seed_weights length must match item_ids."""
        with pytest.raises(ValueError, match="seed_weights"):
            _parse_similar_params(item_ids=["a", "b"], seed_weights=[1.0])

    def test_candidates_scaled_with_filters(self) -> None:
        """Candidates doubled when filters are active."""
        no_filter = _parse_similar_params(item_id="x", candidates=50)
        with_filter = _parse_similar_params(item_id="x", candidates=50, filter_genres=["jazz"])
        assert with_filter.candidates == no_filter.candidates * 2

    def test_defaults(self) -> None:
        """Verify all default values."""
        params = _parse_similar_params(item_id="x")
        assert params.limit == 25
        assert params.depth == 1
        assert params.branch_factor == 5
        assert params.blend_mode == "centroid"
        assert params.candidates == 200
        assert params.seed_weights is None
        assert params.diversity == 0.0
        assert params.preset == "balanced"
        assert params.resolve is False
        assert params.filter_genres is None
        assert params.filter_providers is None
        assert params.exclude_track_ids is None
        assert params.exclude_artists is None


class TestFormatTextQuery:
    """Tests for the CLAP query template helper."""

    def test_appends_music_suffix(self) -> None:
        """A bare query is framed toward CLAP's caption form."""
        assert format_text_query("aggressive metal") == "aggressive metal music"

    def test_skips_when_music_present(self) -> None:
        """No double 'music' when the query already mentions it."""
        assert format_text_query("upbeat dance music") == "upbeat dance music"
        assert format_text_query("Music for studying") == "Music for studying"

    def test_matches_word_not_substring(self) -> None:
        """The skip guard matches the word 'music', not substrings like 'musician'."""
        assert format_text_query("musician") == "musician music"
        assert format_text_query("musical theatre") == "musical theatre music"

    def test_strips_whitespace(self) -> None:
        """Surrounding whitespace is trimmed before framing."""
        assert format_text_query("  jazzy  ") == "jazzy music"

    def test_empty_stays_empty(self) -> None:
        """An empty/whitespace query is left as an empty string."""
        assert format_text_query("   ") == ""


class TestApplyFilters:
    """Tests for post-ANN filter pipeline."""

    def test_no_filters_passes_all(self) -> None:
        """All candidates pass with no active filters."""
        candidates = [ScoredCandidate("a", "prov1", 0.1), ScoredCandidate("b", "prov2", 0.2)]
        result = apply_filters(
            candidates, seed_ids=set(), exclude_track_ids=None, filter_providers=None
        )
        assert len(result) == 2

    def test_exclude_seed_ids(self) -> None:
        """Seed IDs are always excluded."""
        candidates = [ScoredCandidate("seed", "prov1", 0.1), ScoredCandidate("other", "prov1", 0.2)]
        result = apply_filters(
            candidates, seed_ids={"seed"}, exclude_track_ids=None, filter_providers=None
        )
        assert len(result) == 1
        assert result[0].item_id == "other"

    def test_exclude_track_ids(self) -> None:
        """Explicitly excluded track IDs are removed."""
        candidates = [
            ScoredCandidate("a", "p", 0.1),
            ScoredCandidate("b", "p", 0.2),
            ScoredCandidate("c", "p", 0.3),
        ]
        result = apply_filters(
            candidates, seed_ids=set(), exclude_track_ids={"a", "c"}, filter_providers=None
        )
        assert [r.item_id for r in result] == ["b"]

    def test_filter_providers(self) -> None:
        """Only candidates from listed providers are kept."""
        candidates = [
            ScoredCandidate("a", "prov1", 0.1),
            ScoredCandidate("b", "prov2", 0.2),
            ScoredCandidate("c", "prov1", 0.3),
        ]
        result = apply_filters(
            candidates, seed_ids=set(), exclude_track_ids=None, filter_providers={"prov1"}
        )
        assert [r.item_id for r in result] == ["a", "c"]

    def test_all_filters_combined(self) -> None:
        """Filters stack: seed exclusion + exclude_track_ids + filter_providers."""
        candidates = [
            ScoredCandidate("seed", "prov1", 0.0),
            ScoredCandidate("a", "prov1", 0.1),
            ScoredCandidate("b", "prov2", 0.2),
            ScoredCandidate("c", "prov1", 0.3),
        ]
        result = apply_filters(
            candidates, seed_ids={"seed"}, exclude_track_ids={"c"}, filter_providers={"prov1"}
        )
        assert [r.item_id for r in result] == ["a"]


class TestParseWeights:
    """Tests for _parse_weights dict-based weight parsing."""

    def test_default_preset(self) -> None:
        """Empty params return balanced preset defaults."""
        result = _parse_weights({})
        balanced = SIMILARITY_PRESETS["balanced"]
        assert result == balanced

    def test_named_preset(self) -> None:
        """Selecting a preset by name uses its values."""
        result = _parse_weights({"preset": "party"})
        party = SIMILARITY_PRESETS["party"]
        assert result["rhythm"] == party["rhythm"]
        assert result["timbre"] == party["timbre"]

    def test_unknown_preset_falls_back(self) -> None:
        """Unknown preset name falls back to balanced."""
        result = _parse_weights({"preset": "nonexistent"})
        assert result == SIMILARITY_PRESETS["balanced"]

    def test_individual_override(self) -> None:
        """Individual weight overrides take precedence over preset."""
        result = _parse_weights({"preset": "balanced", "rhythm_weight": "0.3"})
        assert abs(result["rhythm"] - 0.3) < 0.01
        assert result["timbre"] == SIMILARITY_PRESETS["balanced"]["timbre"]

    def test_clamping(self) -> None:
        """Values outside [0, 1] are clamped."""
        result = _parse_weights({"rhythm_weight": "1.5", "timbre_weight": "-0.3"})
        assert result["rhythm"] == 1.0
        assert result["timbre"] == 0.0

    def test_invalid_string_falls_back(self) -> None:
        """Non-numeric string falls back to preset default."""
        result = _parse_weights({"preset": "vibe", "rhythm_weight": "abc"})
        assert result["rhythm"] == SIMILARITY_PRESETS["vibe"]["rhythm"]

    def test_returns_dict(self) -> None:
        """Result is a plain dict with every preset weight key (7 audio groups + 2 metadata)."""
        result = _parse_weights({})
        assert isinstance(result, dict)
        for key in (
            "rhythm",
            "loudness",
            "timbre",
            "regularity",
            "mood",
            "tonal",
            "dynamics",
            "genre",
            "era",
        ):
            assert key in result


class TestSimilarityPresets:
    """
    Structural invariants over the SIMILARITY_PRESETS weight dicts.

    Each preset is a hand-edited dict; these guard against a typo (a dropped,
    misspelled or extra key, or an out-of-range value) shipping as a runtime
    KeyError or a degenerate weighting.
    """

    @pytest.mark.parametrize("preset", SIMILARITY_PRESETS.values(), ids=SIMILARITY_PRESETS.keys())
    def test_exact_key_set(self, preset: dict[str, float]) -> None:
        """A preset weights exactly the feature groups plus the genre/era knobs."""
        assert set(preset) == set(FEATURE_GROUPS) | {"genre", "era"}

    @pytest.mark.parametrize("preset", SIMILARITY_PRESETS.values(), ids=SIMILARITY_PRESETS.keys())
    def test_weights_in_unit_range(self, preset: dict[str, float]) -> None:
        """Every weight sits in [0, 1] (preset baselines are not clamped at use)."""
        assert all(0.0 <= w <= 1.0 for w in preset.values())

    @pytest.mark.parametrize("preset", SIMILARITY_PRESETS.values(), ids=SIMILARITY_PRESETS.keys())
    def test_has_a_nonzero_audio_group(self, preset: dict[str, float]) -> None:
        """At least one audio group is weighted, so the distance stays meaningful."""
        assert any(preset[group] > 0.0 for group in FEATURE_GROUPS)


class TestSingleSeedAPI:
    """Cover the single-seed `item_id=` API: parsing, options, and parity with `item_ids=[...]`."""

    def test_single_and_multi_seed_param_parse(self) -> None:
        """item_id='abc' produces same params as item_ids=['abc']."""
        single = _parse_similar_params(item_id="abc")
        multi = _parse_similar_params(item_ids=["abc"])
        assert single.item_ids == multi.item_ids
        assert single.limit == multi.limit
        assert single.depth == multi.depth

    def test_single_seed_with_limit_and_preset(self) -> None:
        """Single-seed call with extra kwargs works."""
        params = _parse_similar_params(item_id="abc", limit=10, preset="vibe")
        assert params.item_ids == ["abc"]
        assert params.limit == 10
        assert params.preset == "vibe"

    def test_single_seed_with_weight_overrides(self) -> None:
        """Weight kwargs pass through."""
        params = _parse_similar_params(item_id="abc", timbre_weight="0.5")
        assert params.weight_overrides["timbre_weight"] == "0.5"


class TestHandleSimilarReason:
    """_handle_similar surfaces a `reason` that distinguishes empty-response causes."""

    @pytest.mark.asyncio
    async def test_corpus_not_ready_reason(self, make_plugin: Callable[..., Any]) -> None:
        """No corpus → reason = corpus_not_ready (regardless of seed match)."""
        plugin = make_plugin()  # no signatures → corpus_means/stds stay None

        result = await plugin._handle_similar(item_id="anything")

        assert result["analyzed"] is False
        assert result["reason"] == "corpus_not_ready"
        assert result["items"] == []

    @pytest.mark.asyncio
    async def test_seed_not_in_index_reason(self, make_plugin: Callable[..., Any]) -> None:
        """Corpus ready but no matching seed → reason = seed_not_in_index."""
        plugin = make_plugin(signatures={("spotify", "known_seed"): [0.1] * 18})

        result = await plugin._handle_similar(item_id="unknown_seed")

        assert result["analyzed"] is False
        assert result["reason"] == "seed_not_in_index"
        assert result["items"] == []


def _track_with_metadata(
    item_id: str,
    *,
    provider: str = "spotify",
    genres: list[str] | None = None,
    artist_names: tuple[str, ...] = (),
) -> MagicMock:
    """Build a Track-like mock with ``metadata.genres`` set (make_track doesn't)."""
    track = make_track(item_id, provider=provider, artists=artist_names)
    if genres is not None:
        track.metadata = MagicMock()
        track.metadata.genres = genres
    else:
        track.metadata = None
    return track


def _make_candidate(item_id: str, *, distance: float = 0.5) -> Candidate:
    """Build a Candidate with the minimum fields needed for filter/rerank tests."""
    return Candidate(
        item_id=item_id, provider="spotify", features=[0.0] * 18, distance=distance, generation=0
    )


class TestApplyMetadataFilters:
    """_apply_metadata_filters drops candidates that don't pass the genre/artist gates."""

    @pytest.mark.asyncio
    async def test_no_filters_returns_input_unchanged(
        self, make_plugin: Callable[..., Any]
    ) -> None:
        """With no filter_genres and no exclude_artists the input passes through."""
        plugin = make_plugin(signatures={("spotify", "a"): [0.1] * 18})
        cands = [_make_candidate("a"), _make_candidate("b")]

        result = await plugin._apply_metadata_filters(cands, {})

        assert result == cands

    @pytest.mark.asyncio
    async def test_genre_filter_drops_non_matching(self, make_plugin: Callable[..., Any]) -> None:
        """Candidates without overlapping genres are dropped."""
        plugin = make_plugin(signatures={("spotify", "a"): [0.1] * 18})
        resolved = {
            ("a", "spotify"): _track_with_metadata("a", genres=["Rock", "Indie"]),
            ("b", "spotify"): _track_with_metadata("b", genres=["Pop"]),
        }

        result = await plugin._apply_metadata_filters(
            [_make_candidate("a"), _make_candidate("b")],
            resolved,
            filter_genres=["rock"],
        )

        assert [c.item_id for c in result] == ["a"]

    @pytest.mark.asyncio
    async def test_artist_exclusion_drops_matching(self, make_plugin: Callable[..., Any]) -> None:
        """Candidates whose artist is in exclude_artists are dropped (case-insensitive)."""
        plugin = make_plugin(signatures={("spotify", "a"): [0.1] * 18})
        resolved = {
            ("a", "spotify"): _track_with_metadata("a", artist_names=("Banned Artist",)),
            ("b", "spotify"): _track_with_metadata("b", artist_names=("Other Artist",)),
        }

        result = await plugin._apply_metadata_filters(
            [_make_candidate("a"), _make_candidate("b")],
            resolved,
            exclude_artists=["banned artist"],
        )

        assert [c.item_id for c in result] == ["b"]

    @pytest.mark.asyncio
    async def test_unresolved_tracks_are_dropped(self, make_plugin: Callable[..., Any]) -> None:
        """Candidates whose track resolve returned None are silently dropped."""
        plugin = make_plugin(signatures={("spotify", "a"): [0.1] * 18})
        resolved = {
            ("a", "spotify"): None,  # unresolved miss
            ("b", "spotify"): _track_with_metadata("b", genres=["rock"]),
        }

        result = await plugin._apply_metadata_filters(
            [_make_candidate("a"), _make_candidate("b")],
            resolved,
            filter_genres=["rock"],
        )

        assert [c.item_id for c in result] == ["b"]


class TestApplyMetadataReranking:
    """_apply_metadata_reranking shifts distances based on shared genre/year metadata."""

    @pytest.mark.asyncio
    async def test_no_seed_tracks_returns_unchanged(
        self, make_plugin: Callable[..., Any], mock_mass: MagicMock
    ) -> None:
        """If every seed resolve fails, return the input list as-is."""
        from music_assistant_models.errors import MusicAssistantError  # noqa: PLC0415

        plugin = make_plugin(signatures={("spotify", "seed"): [0.1] * 18})
        mock_mass.music.tracks.get = AsyncMock(side_effect=MusicAssistantError("seed missing"))
        cands = [_make_candidate("a", distance=0.5)]

        result = await plugin._apply_metadata_reranking(["seed"], cands, {"genre": 1.0}, {})

        assert result == cands

    @pytest.mark.asyncio
    async def test_genre_overlap_reduces_distance(
        self, make_plugin: Callable[..., Any], mock_mass: MagicMock
    ) -> None:
        """A candidate sharing all genres with the seed gets a negative bonus."""
        plugin = make_plugin(signatures={("spotify", "seed"): [0.1] * 18})
        mock_mass.music.tracks.get = AsyncMock(
            return_value=_track_with_metadata("seed", genres=["rock", "indie"])
        )
        resolved = {("a", "spotify"): _track_with_metadata("a", genres=["rock", "indie"])}

        result = await plugin._apply_metadata_reranking(
            ["seed"], [_make_candidate("a", distance=0.5)], {"genre": 1.0}, resolved
        )

        # Full overlap (jaccard = 1.0) with weight 1.0 → bonus = -METADATA_BONUS_SCALE
        assert result[0].distance == pytest.approx(0.5 - METADATA_BONUS_SCALE)

    @pytest.mark.asyncio
    async def test_zero_weight_leaves_distance_unchanged(
        self, make_plugin: Callable[..., Any], mock_mass: MagicMock
    ) -> None:
        """genre/era weights of 0 produce no rerank bonus even with full overlap."""
        plugin = make_plugin(signatures={("spotify", "seed"): [0.1] * 18})
        mock_mass.music.tracks.get = AsyncMock(
            return_value=_track_with_metadata("seed", genres=["rock"])
        )
        resolved = {("a", "spotify"): _track_with_metadata("a", genres=["rock"])}

        result = await plugin._apply_metadata_reranking(
            ["seed"], [_make_candidate("a", distance=0.5)], {"genre": 0.0, "era": 0.0}, resolved
        )

        assert result[0].distance == pytest.approx(0.5)

    @pytest.mark.asyncio
    async def test_results_sorted_by_distance_after_rerank(
        self, make_plugin: Callable[..., Any], mock_mass: MagicMock
    ) -> None:
        """After rerank, candidates are sorted by the new distance ascending."""
        plugin = make_plugin(signatures={("spotify", "seed"): [0.1] * 18})
        mock_mass.music.tracks.get = AsyncMock(
            return_value=_track_with_metadata("seed", genres=["rock"])
        )
        resolved = {
            ("a", "spotify"): _track_with_metadata("a", genres=["pop"]),  # no overlap → no bonus
            ("b", "spotify"): _track_with_metadata("b", genres=["rock"]),  # overlap → bonus
        }

        result = await plugin._apply_metadata_reranking(
            ["seed"],
            [_make_candidate("a", distance=0.4), _make_candidate("b", distance=0.45)],
            {"genre": 1.0},
            resolved,
        )

        # b started farther (0.45) but got the -0.1 bonus → ends ahead of a (0.4).
        assert [c.item_id for c in result] == ["b", "a"]

    @pytest.mark.asyncio
    async def test_bonus_is_bounded_by_scale(
        self, make_plugin: Callable[..., Any], mock_mass: MagicMock
    ) -> None:
        """Even max overlap with weight=1.0 doesn't shift distance more than METADATA_BONUS_SCALE."""
        plugin = make_plugin(signatures={("spotify", "seed"): [0.1] * 18})
        mock_mass.music.tracks.get = AsyncMock(
            return_value=_track_with_metadata("seed", genres=["rock", "indie", "alt"])
        )
        resolved = {("a", "spotify"): _track_with_metadata("a", genres=["rock", "indie", "alt"])}

        result = await plugin._apply_metadata_reranking(
            ["seed"], [_make_candidate("a", distance=0.5)], {"genre": 1.0}, resolved
        )

        shift = 0.5 - result[0].distance
        assert shift <= METADATA_BONUS_SCALE + 1e-9
