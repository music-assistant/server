"""Tests for the AudioMuse-AI plugin provider (id mapping, similar tracks, search)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MediaNotFoundError

from music_assistant.providers.audiomuse_ai.client import AudioMuseError
from music_assistant.providers.audiomuse_ai.constants import (
    CONF_ENABLE_DISCOVER_ROW,
    CONF_MEDIA_PROVIDER,
)
from music_assistant.providers.audiomuse_ai.provider import AudioMuseAiPlugin

MEDIA_PROVIDER = "jellyfin--test"

# the undecorated recommendations coroutine (bypasses @use_cache in tests)
_RECOMMENDATIONS = cast("Any", AudioMuseAiPlugin.recommendations).__wrapped__


def _track(item_id: str, mapped_id: str | None = None) -> MagicMock:
    """Build a lightweight (hashable) stand-in for a Track with one provider mapping."""
    mappings = []
    if mapped_id is not None:
        mappings.append(SimpleNamespace(provider_instance=MEDIA_PROVIDER, item_id=mapped_id))
    track = MagicMock()
    track.item_id = item_id
    track.provider = "library"
    track.uri = f"library://track/{item_id}"
    track.provider_mappings = mappings
    return track


def _mock_client(**methods: Any) -> Any:
    """Build an untyped stand-in for AudioMuseClient with the given async methods."""
    client = MagicMock()
    for name, value in methods.items():
        setattr(client, name, value)
    return client


def _provider(
    library: dict[str, MagicMock] | None = None,
    config_values: dict[str, Any] | None = None,
    recent: list[Any] | None = None,
) -> AudioMuseAiPlugin:
    """
    Build a provider instance without running the (heavy) base __init__.

    ``library`` maps media-server item ids to the Track stand-ins that
    ``mass.music.tracks.get`` should resolve them to; unknown ids raise
    MediaNotFoundError like the real controller. ``config_values`` backs
    ``config.get_value``; ``recent`` backs ``mass.music.recently_played``.
    """
    prov = AudioMuseAiPlugin.__new__(AudioMuseAiPlugin)
    config = MagicMock()
    config.instance_id = "audiomuse_ai--test"
    config.get_value = MagicMock(side_effect=lambda key: (config_values or {}).get(key))
    prov.config = config
    prov.logger = MagicMock()

    async def _get(item_id: str, _prov: str, **_kwargs: Any) -> MagicMock:
        if library is not None and item_id in library:
            return library[item_id]
        raise MediaNotFoundError(f"not found: {item_id}")

    mass = MagicMock()
    mass.music.tracks.get = AsyncMock(side_effect=_get)
    mass.music.recently_played = AsyncMock(return_value=recent or [])
    prov.mass = mass
    prov._client = _mock_client()
    prov._media_provider = MEDIA_PROVIDER
    prov._unregister_handles = []
    return prov


class TestDispatchContract:
    """The generic dispatch properties the rest of MA keys its behavior on."""

    def test_claims_priority_over_music_providers(self) -> None:
        """Priority below the provider default (50) so sonic matches lead similar_tracks."""
        prov = _provider()
        assert prov.priority < 50

    def test_declares_ordered_similarity(self) -> None:
        """ordered_similarity=True keeps dynamic radio in provider order (no shuffle)."""
        prov = _provider()
        assert prov.ordered_similarity is True


class TestSeedItemId:
    """Mapping an MA track to the media-server item id AudioMuse-AI knows."""

    def test_returns_mapping_for_configured_provider(self) -> None:
        """The mapping on the configured provider instance is used as seed."""
        prov = _provider()
        track = _track("1", mapped_id="ms-42")
        assert prov._seed_item_id(cast("Any", track)) == "ms-42"

    def test_returns_none_without_matching_mapping(self) -> None:
        """No mapping on the configured provider -> None (no AudioMuse query)."""
        prov = _provider()
        track = _track("1", mapped_id=None)
        assert prov._seed_item_id(cast("Any", track)) is None


class TestGetSimilarTracks:
    """The cross-provider SIMILAR_TRACKS hook."""

    async def test_resolves_and_filters(self) -> None:
        """Seed echoes and unresolvable ids are dropped, order preserved."""
        sim_a = _track("a", mapped_id="ms-a")
        prov = _provider(library={"ms-a": sim_a})
        client = _mock_client(
            similar_tracks=AsyncMock(
                return_value=[
                    {"item_id": "ms-seed"},  # the seed itself -> dropped
                    {"item_id": "ms-a"},
                    {"item_id": None},  # no id -> dropped
                    {"item_id": "ms-gone"},  # unresolvable -> dropped
                ]
            )
        )
        prov._client = client
        seed = _track("1", mapped_id="ms-seed")

        result = await prov.get_similar_tracks(cast("Any", seed), limit=10)
        assert result == [sim_a]
        client.similar_tracks.assert_awaited_once_with("ms-seed", 10)

    async def test_no_mapping_returns_empty(self) -> None:
        """A track without a mapping on the configured provider yields []."""
        prov = _provider()
        client = _mock_client(similar_tracks=AsyncMock())
        prov._client = client
        result = await prov.get_similar_tracks(cast("Any", _track("1")), limit=10)
        assert result == []
        client.similar_tracks.assert_not_awaited()

    async def test_api_error_returns_empty(self) -> None:
        """AudioMuse-AI API failures degrade to an empty result, not an exception."""
        prov = _provider()
        prov._client = _mock_client(similar_tracks=AsyncMock(side_effect=AudioMuseError("boom")))
        seed = _track("1", mapped_id="ms-seed")
        assert await prov.get_similar_tracks(cast("Any", seed), limit=10) == []

    async def test_not_loaded_returns_empty(self) -> None:
        """Before handle_async_init the client is None -> []."""
        prov = _provider()
        prov._client = None
        seed = _track("1", mapped_id="ms-seed")
        assert await prov.get_similar_tracks(cast("Any", seed), limit=10) == []


class TestSearch:
    """Free-text search across the CLAP + lyrics engines."""

    async def test_non_track_media_types_short_circuit(self) -> None:
        """Search only answers track queries."""
        prov = _provider()
        result = await prov.search("dreamy synths", [MediaType.ALBUM], limit=5)
        assert not result.tracks

    async def test_interleaves_and_dedupes_engines(self) -> None:
        """CLAP and lyrics results are interleaved and deduplicated in order."""
        library = {f"ms-{i}": _track(str(i), mapped_id=f"ms-{i}") for i in ("c1", "c2", "l1", "l2")}
        prov = _provider(library=library)
        prov._client = _mock_client(
            clap_search=AsyncMock(return_value=[{"item_id": "ms-c1"}, {"item_id": "ms-c2"}]),
            lyrics_search=AsyncMock(
                return_value=[{"item_id": "ms-l1"}, {"item_id": "ms-c1"}, {"item_id": "ms-l2"}]
            ),
        )

        result = await prov.search("dreamy synths", [MediaType.TRACK], limit=10)
        assert [t.item_id for t in result.tracks] == ["c1", "l1", "c2", "l2"]

    async def test_engine_failure_degrades_to_other_engine(self) -> None:
        """One engine failing must not take down the whole search."""
        library = {"ms-l1": _track("l1", mapped_id="ms-l1")}
        prov = _provider(library=library)
        prov._client = _mock_client(
            clap_search=AsyncMock(side_effect=AudioMuseError("down")),
            lyrics_search=AsyncMock(return_value=[{"item_id": "ms-l1"}]),
        )

        result = await prov.search("sad piano", [MediaType.TRACK], limit=10)
        assert [t.item_id for t in result.tracks] == ["l1"]


class TestRecommendations:
    """The 'Inspired by recently played' discover folder."""

    async def test_disabled_returns_empty(self) -> None:
        """With the discover row disabled no folder is produced."""
        prov = _provider(config_values={CONF_ENABLE_DISCOVER_ROW: False})
        assert await _RECOMMENDATIONS(prov) == []

    async def test_builds_folder_from_recent_seeds(self) -> None:
        """Recent tracks seed similar lookups; the union becomes one folder."""
        recent_track = _track("recent", mapped_id="ms-recent")
        reco_track = _track("reco", mapped_id="ms-reco")
        prov = _provider(
            library={"recent": recent_track, "ms-reco": reco_track},
            config_values={
                CONF_ENABLE_DISCOVER_ROW: True,
                CONF_MEDIA_PROVIDER: MEDIA_PROVIDER,
            },
            recent=[SimpleNamespace(item_id="recent", provider="library")],
        )
        prov._client = _mock_client(
            similar_tracks=AsyncMock(
                return_value=[{"item_id": "ms-recent"}, {"item_id": "ms-reco"}]
            )
        )

        folders = await _RECOMMENDATIONS(prov)
        assert len(folders) == 1
        assert [t.item_id for t in folders[0].items] == ["reco"]

    async def test_no_mappable_recent_tracks_returns_empty(self) -> None:
        """Recent tracks that don't map into the configured provider are skipped."""
        recent_track = _track("recent", mapped_id=None)
        prov = _provider(
            library={"recent": recent_track},
            config_values={CONF_ENABLE_DISCOVER_ROW: True},
            recent=[SimpleNamespace(item_id="recent", provider="library")],
        )
        assert await _RECOMMENDATIONS(prov) == []


class TestHandleSimilarCommand:
    """The audiomuse_ai/similar API command."""

    async def test_formats_neighbours(self) -> None:
        """Raw AudioMuse entries are shaped for the frontend, seed excluded."""
        prov = _provider()
        prov._client = _mock_client(
            similar_tracks=AsyncMock(
                return_value=[
                    {"item_id": "seed", "title": "Seed", "author": "A", "distance": 0.0},
                    {"item_id": "n1", "title": "One", "author": "B", "distance": 0.1},
                ]
            )
        )
        result = await prov._handle_similar("seed", limit=5)
        assert result["analyzed"] is True
        assert result["items"] == [
            {
                "item_id": "n1",
                "provider": MEDIA_PROVIDER,
                "name": "One",
                "artist": "B",
                "distance": 0.1,
            }
        ]

    async def test_not_loaded(self) -> None:
        """Without a client the command reports not_loaded instead of raising."""
        prov = _provider()
        prov._client = None
        result = await prov._handle_similar("seed")
        assert result["analyzed"] is False
        assert result["reason"] == "not_loaded"

    async def test_api_error_reported(self) -> None:
        """API failures surface as analyzed=False with the error message."""
        prov = _provider()
        prov._client = _mock_client(
            similar_tracks=AsyncMock(side_effect=AudioMuseError("server exploded"))
        )
        result = await prov._handle_similar("seed")
        assert result["analyzed"] is False
        assert "server exploded" in result["reason"]
