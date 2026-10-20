"""Tests for the AudioMuse-AI plugin provider (id mapping, similar tracks, search)."""

from __future__ import annotations

from functools import partial
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import (
    LoginFailed,
    MediaNotFoundError,
    ProviderUnavailableError,
)

from music_assistant.providers.audiomuse_ai import provider as provider_module
from music_assistant.providers.audiomuse_ai.client import AudioMuseError
from music_assistant.providers.audiomuse_ai.constants import (
    CONF_BASE_URL,
    CONF_ENABLE_DISCOVER_ROW,
    CONF_MEDIA_PROVIDER,
)
from music_assistant.providers.audiomuse_ai.provider import AudioMuseAiPlugin

MEDIA_PROVIDER = "jellyfin--test"

# the undecorated recommendations builder (bypasses @use_cache in tests)
_BUILD_RECOMMENDATIONS = cast("Any", AudioMuseAiPlugin._get_inspired_recommendations).__wrapped__
ROW_ID = "inspired_by_recently_played"


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
    MediaNotFoundError like the real controller. Stand-ins whose mapping
    matches a requested id are also returned by the batched library lookup.
    ``config_values`` backs ``config.get_value``; ``recent`` backs
    ``mass.music.recently_played``.
    The setup_data read by ``get_setup_value`` maps to ``MEDIA_PROVIDER``.
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

    async def _get_library_items_by_prov_id(
        provider_instance: str, provider_item_ids: list[str], **_kwargs: Any
    ) -> list[MagicMock]:
        return [
            track
            for track in (library or {}).values()
            if any(
                m.provider_instance == provider_instance and m.item_id in provider_item_ids
                for m in track.provider_mappings
            )
        ]

    mass = MagicMock()
    mass.music.tracks.get = AsyncMock(side_effect=_get)
    mass.music.tracks.get_library_items_by_prov_id = AsyncMock(
        side_effect=_get_library_items_by_prov_id
    )
    mass.music.recently_played = AsyncMock(return_value=recent or [])
    mass.config.get = MagicMock(return_value={CONF_MEDIA_PROVIDER: MEDIA_PROVIDER})
    mass.config.decrypt_string = MagicMock(side_effect=lambda value: value)
    prov.mass = mass
    prov._client = _mock_client()
    prov._media_provider = str(prov.get_setup_value(CONF_MEDIA_PROVIDER) or "")
    prov._unregister_handles = []
    cast("Any", prov)._get_inspired_recommendations = partial(_BUILD_RECOMMENDATIONS, prov)
    return prov


class TestHandleAsyncInit:
    """Provider setup against the AudioMuse-AI server."""

    async def test_rejected_token_raises_login_failed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A reachable server that rejects the token fails setup with LoginFailed."""
        prov = _provider()
        prov._client = None
        cast("Any", prov.mass.config.get).return_value = {
            CONF_MEDIA_PROVIDER: MEDIA_PROVIDER,
            CONF_BASE_URL: "http://audiomuse:8000",
        }
        client = _mock_client(
            health=AsyncMock(return_value=True),
            check_auth=AsyncMock(side_effect=LoginFailed("rejected")),
        )
        monkeypatch.setattr(provider_module, "AudioMuseClient", MagicMock(return_value=client))
        with pytest.raises(LoginFailed):
            await prov.handle_async_init()


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

    async def test_api_error_raises_unavailable(self) -> None:
        """AudioMuse-AI API failures surface as ProviderUnavailableError, not an empty answer."""
        prov = _provider()
        prov._client = _mock_client(similar_tracks=AsyncMock(side_effect=AudioMuseError("boom")))
        seed = _track("1", mapped_id="ms-seed")
        with pytest.raises(ProviderUnavailableError):
            await prov.get_similar_tracks(cast("Any", seed), limit=10)

    async def test_media_server_down_raises(self) -> None:
        """A media server failure during resolving propagates instead of truncating the result."""
        prov = _provider()
        prov._client = _mock_client(similar_tracks=AsyncMock(return_value=[{"item_id": "ms-a"}]))
        cast("Any", prov.mass).music.tracks.get.side_effect = ProviderUnavailableError("down")
        seed = _track("1", mapped_id="ms-seed")
        with pytest.raises(ProviderUnavailableError):
            await prov.get_similar_tracks(cast("Any", seed), limit=10)

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

    async def test_both_engines_failing_raises(self) -> None:
        """With every engine down the search raises, so no empty result gets cached."""
        prov = _provider()
        prov._client = _mock_client(
            clap_search=AsyncMock(side_effect=AudioMuseError("down")),
            lyrics_search=AsyncMock(side_effect=AudioMuseError("down")),
        )
        with pytest.raises(ProviderUnavailableError):
            await prov.search("sad piano", [MediaType.TRACK], limit=10)


class TestRecommendations:
    """The 'Inspired by recently played' discover folder."""

    async def test_row_descriptor_follows_config(self) -> None:
        """The row is advertised without items only while the discover row is enabled."""
        prov = _provider(config_values={CONF_ENABLE_DISCOVER_ROW: False})
        assert await prov.get_recommendations() == []

        prov = _provider(config_values={CONF_ENABLE_DISCOVER_ROW: True})
        folders = await prov.get_recommendations()
        assert [f.item_id for f in folders] == [ROW_ID]
        assert not folders[0].items
        cast("Any", prov.mass).music.recently_played.assert_not_awaited()

    async def test_builds_folder_from_recent_seeds(self) -> None:
        """Recent tracks seed similar lookups; the union becomes one folder."""
        recent_track = _track("recent", mapped_id="ms-recent")
        reco_track = _track("reco", mapped_id="ms-reco")
        prov = _provider(
            library={"recent": recent_track, "ms-reco": reco_track},
            config_values={CONF_ENABLE_DISCOVER_ROW: True},
            recent=[SimpleNamespace(item_id="recent", provider="library")],
        )
        prov._client = _mock_client(
            similar_tracks=AsyncMock(
                return_value=[{"item_id": "ms-recent"}, {"item_id": "ms-reco"}]
            )
        )

        items = await prov.get_recommendation_items(ROW_ID)
        assert [t.item_id for t in items] == ["reco"]

    async def test_seed_lookup_failure_raises(self) -> None:
        """An AudioMuse-AI failure raises instead of caching an empty row over the last one."""
        recent_track = _track("recent", mapped_id="ms-recent")
        prov = _provider(
            library={"recent": recent_track},
            config_values={CONF_ENABLE_DISCOVER_ROW: True},
            recent=[SimpleNamespace(item_id="recent", provider="library")],
        )
        prov._client = _mock_client(similar_tracks=AsyncMock(side_effect=AudioMuseError("down")))
        with pytest.raises(ProviderUnavailableError):
            await prov.get_recommendation_items(ROW_ID)

    async def test_no_mappable_recent_tracks_returns_empty(self) -> None:
        """Recent tracks that don't map into the configured provider are skipped."""
        recent_track = _track("recent", mapped_id=None)
        prov = _provider(
            library={"recent": recent_track},
            config_values={CONF_ENABLE_DISCOVER_ROW: True},
            recent=[SimpleNamespace(item_id="recent", provider="library")],
        )
        assert await prov.get_recommendation_items(ROW_ID) == []

    async def test_row_is_built_per_user(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Each calling user reaches the cached builder with their own id."""
        prov = _provider(config_values={CONF_ENABLE_DISCOVER_ROW: True})
        builder = AsyncMock(return_value=None)
        cast("Any", prov)._get_inspired_recommendations = builder
        for user_id in ("user-a", "user-b"):
            monkeypatch.setattr(
                provider_module,
                "get_current_user",
                lambda uid=user_id: SimpleNamespace(user_id=uid),
            )
            await prov.get_recommendation_items(ROW_ID)
        assert builder.await_args_list == [call("user-a"), call("user-b")]


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


class TestPlexIdTranslation:
    """Plex item ids carry a key prefix that AudioMuse-AI's bare rating keys lack."""

    async def test_similar_tracks_round_trip(self) -> None:
        """The seed is sent as a bare rating key and neighbours resolve to prefixed MA ids."""
        neighbour = _track("n", mapped_id="/library/metadata/43")
        prov = _provider(library={"/library/metadata/43": neighbour})
        cast("Any", prov.mass).config.get_raw_provider_config_value.return_value = "plex"
        client = _mock_client(
            similar_tracks=AsyncMock(return_value=[{"item_id": "42"}, {"item_id": "43"}])
        )
        prov._client = client
        seed = _track("1", mapped_id="/library/metadata/42")

        result = await prov.get_similar_tracks(cast("Any", seed), limit=10)
        assert result == [neighbour]
        client.similar_tracks.assert_awaited_once_with("42", 10)

    async def test_handle_similar_translates_ids(self) -> None:
        """The API command takes and returns MA ids, excluding the seed."""
        prov = _provider()
        cast("Any", prov.mass).config.get_raw_provider_config_value.return_value = "plex"
        client = _mock_client(
            similar_tracks=AsyncMock(return_value=[{"item_id": "42"}, {"item_id": "43"}])
        )
        prov._client = client

        result = await prov._handle_similar("/library/metadata/42", limit=5)
        client.similar_tracks.assert_awaited_once_with("42", 5)
        assert [item["item_id"] for item in result["items"]] == ["/library/metadata/43"]
