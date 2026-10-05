"""
Shared fixtures for sonic_similarity plugin-instance unit tests.

The existing pure-function tests in this directory don't need scaffolding;
the tests that exercise SonicSimilarityPlugin behaviour do. This module
provides:

* ``mock_mass`` — a lightweight MagicMock standing in for MusicAssistant,
  with the specific surfaces the plugin touches (music, streams.audio_analysis,
  get_provider, storage_path) pre-wired as AsyncMocks / MagicMocks.
* ``make_plugin`` — a factory that returns a fully-constructed
  SonicSimilarityPlugin with a primed signature cache + corpus stats, so
  tests can call dispatcher hooks without going through loaded_in_mass.
* ``make_track`` / ``make_item_mapping`` — minimal Track/ItemMapping doubles
  used by tests that exercise the seed-selection / resolve paths.

Modeled on the yandex_smarthome / yandex_ynison test fixtures (MagicMock-
based, no real MusicAssistant instance), not on the heavyweight
``tests/conftest.py:mass`` fixture which starts a real MA.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.media_items import Album

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path

    from music_assistant.providers.sonic_similarity import SonicSimilarityPlugin


@pytest.fixture
def logger() -> logging.Logger:
    """Quiet logger for tests; attaches a NullHandler so logs don't leak."""
    lg = logging.getLogger("test_sonic_similarity")
    if not lg.handlers:
        lg.addHandler(logging.NullHandler())
    return lg


@pytest.fixture
def mock_mass(tmp_path: Path) -> MagicMock:
    """
    Mock MusicAssistant exposing the surfaces sonic_similarity touches.

    :param tmp_path: pytest-provided temp dir, used as storage_path.
    """
    mass = MagicMock()
    mass.storage_path = str(tmp_path)
    mass.cache = MagicMock()
    # @use_cache on _get_inspired_recommendations() awaits cache.get_with_freshness; return a
    # miss so the wrapped method runs. cache.set is fire-and-forget via create_task (mocked).
    mass.cache.get = AsyncMock(return_value=None)
    mass.cache.get_with_freshness = AsyncMock(return_value=(None, False, False))
    mass.create_task = MagicMock()  # fire-and-forget; we assert it was called
    mass.get_provider = MagicMock(return_value=None)

    # music.* surfaces
    mass.music = MagicMock()
    mass.music.recently_played = AsyncMock(return_value=[])
    mass.music.tracks = MagicMock()
    mass.music.tracks.get = AsyncMock()
    mass.music.database = MagicMock()
    mass.music.database.get_count_from_query = AsyncMock(return_value=0)

    # tasks controller (scheduled background jobs)
    mass.tasks = MagicMock()
    mass.tasks.register_scheduled_task = MagicMock()
    mass.tasks.unregister_scheduled_task = MagicMock()

    # streams.audio_analysis controller (the #3851 surface). The iter_*
    # methods return AsyncGenerators; tests configure their contents by
    # assigning a list to the corresponding ``_iter_*_data`` attribute on
    # the mass mock, which the generator closures yield from. The MagicMock
    # wrapper preserves call_count so tests can still assert call patterns.
    mass.streams = MagicMock()
    mass.streams.audio_analysis = MagicMock()
    mass._iter_audio_analysis_rows_data = []
    mass._iter_merged_audio_analysis_rows_data = []

    async def _iter_rows(*_args: Any, **_kwargs: Any) -> Any:
        for row in mass._iter_audio_analysis_rows_data:
            yield row

    async def _iter_merged(*_args: Any, **_kwargs: Any) -> Any:
        for entry in mass._iter_merged_audio_analysis_rows_data:
            yield entry

    mass.streams.audio_analysis.iter_audio_analysis_rows = MagicMock(side_effect=_iter_rows)
    mass.streams.audio_analysis.iter_merged_audio_analysis_rows = MagicMock(
        side_effect=_iter_merged
    )
    mass.streams.audio_analysis.get_coverage = AsyncMock(return_value=None)
    return mass


@pytest.fixture
def make_plugin(
    mock_mass: MagicMock, logger: logging.Logger
) -> Callable[..., SonicSimilarityPlugin]:
    """
    Return a factory that builds a SonicSimilarityPlugin with a primed corpus.

    Bypasses loaded_in_mass — the corpus is populated directly on the
    instance so dispatcher-hook tests don't need to mock out rebuild
    machinery. Use the ``signatures`` arg to seed the index; without it
    the corpus stays empty (useful for the corpus-not-ready paths).

    :param clap_enabled: When True, attaches a MagicMock ClapIndex so
        _handle_similar_clap and friends can be exercised without a real
        usearch backing.
    :param text_search_enabled: When True, the config returns True for
        CONF_ENABLE_TEXT_SEARCH (encoder stays lazy until first call).
    :param discover_row_enabled: Config value for CONF_ENABLE_DISCOVER_ROW
        (default True, matching the production default).
    :param discover_preset: Config value for CONF_DISCOVER_PRESET.
    :param discover_diversity: Config value for CONF_DISCOVER_DIVERSITY (0-10 integer scale).
    :param discover_engine: Config value for CONF_DISCOVER_ENGINE ("18dim"/"clap";
        None defaults to 18-dim via the provider's fallback).
    :param signatures: Optional dict of {(provider, item_id): vector}
        used to populate ``_signature_cache`` / ``_signatures_by_id`` /
        ``_provider_by_item_id`` and to set non-None corpus_means/stds.
    """
    from music_assistant.providers.sonic_similarity import (  # noqa: PLC0415
        SonicSimilarityPlugin,
    )
    from music_assistant.providers.sonic_similarity.constants import (  # noqa: PLC0415
        SUPPORTED_FEATURES,
    )

    def _make(
        *,
        clap_enabled: bool = False,
        text_search_enabled: bool = False,
        discover_row_enabled: bool = True,
        discover_preset: str = "discover",
        discover_diversity: int = 2,
        discover_engine: str | None = None,
        signatures: dict[tuple[str, str], list[float]] | None = None,
    ) -> SonicSimilarityPlugin:
        manifest = MagicMock()
        manifest.instance_id = "test-instance-id"
        manifest.domain = "sonic_similarity"
        config = MagicMock()
        config.instance_id = "test-instance-id"
        config_values = {
            "log_level": "GLOBAL",
            "enable_clap_index": clap_enabled,
            "enable_text_search": text_search_enabled,
            "enable_discover_row": discover_row_enabled,
            "discover_preset": discover_preset,
            "discover_diversity": discover_diversity,
            "discover_engine": discover_engine,
        }
        config.get_value = lambda key: config_values.get(key)
        plugin = SonicSimilarityPlugin(mock_mass, manifest, config, SUPPORTED_FEATURES)
        plugin.logger = logger
        if signatures:
            for (provider, item_id), vec in signatures.items():
                plugin._signature_cache[(item_id, provider)] = vec
                plugin._signatures_by_id[item_id] = vec
                plugin._provider_by_item_id[item_id] = provider
            plugin.corpus_means = [0.0] * 18
            plugin.corpus_stds = [1.0] * 18
            # Minimal search-index double: len() / search() responding
            # convincingly is enough for the dispatcher-hook tests.
            search_index = MagicMock()
            search_index.__len__ = MagicMock(return_value=len(signatures))
            plugin._search_index = search_index
        if clap_enabled:
            clap_index = MagicMock()
            clap_index.__len__ = MagicMock(return_value=0)
            clap_index.contains = MagicMock(return_value=False)
            clap_index.add = AsyncMock()
            clap_index.save = AsyncMock()
            clap_index.search = AsyncMock(return_value=[])
            clap_index.get_embedding_by_item_id = MagicMock(return_value=None)
            plugin._clap_index = clap_index
        return plugin

    return _make


def make_track(
    item_id: str,
    *,
    provider: str = "spotify",
    provider_domain: str | None = None,
    name: str = "Test Track",
    artists: Iterable[str] = (),
    album_year: int | None = None,
    genres: Iterable[str] = (),
) -> MagicMock:
    """
    Return a Track-like MagicMock with the attributes our code reads.

    :param item_id: Underlying provider item id (used as the seed in
        signature cache lookups).
    :param provider: provider_instance / provider_domain shorthand for the
        single provider mapping attached. Use ``provider_domain`` to
        override the domain separately (e.g. ``"library"``).
    :param provider_domain: When set, overrides ``provider`` for the
        mapping's ``provider_domain`` field while keeping ``provider`` as
        the instance id.
    :param name: Display name.
    :param artists: Iterable of artist names; each becomes a sub-mock
        with a ``.name`` attribute.
    :param album_year: When given, attaches an Album mock with this year;
        otherwise ``album`` is None.
    :param genres: Iterable of genre names exposed via ``metadata.genres``;
        empty leaves ``metadata.genres`` None (matching _track_genres' empty case).
    """
    track = MagicMock()
    track.item_id = item_id
    track.provider = provider
    track.name = name
    track.artists = [_artist_mock(a) for a in artists]
    if album_year is not None:
        # spec=Album so the rerank's isinstance(track.album, Album) check passes.
        album = MagicMock(spec=Album)
        album.year = album_year
        track.album = album
    else:
        track.album = None
    metadata = MagicMock()
    metadata.genres = list(genres) or None
    track.metadata = metadata
    mapping = MagicMock()
    mapping.item_id = item_id
    mapping.provider_instance = provider
    mapping.provider_domain = provider_domain or provider
    track.provider_mappings = [mapping]
    return track


def _artist_mock(name: str) -> MagicMock:
    """Build an Artist double; can't pass ``name`` to MagicMock kwarg directly."""
    artist = MagicMock()
    artist.name = name
    return artist


def make_item_mapping(item_id: str, *, provider: str = "library") -> MagicMock:
    """
    Return an ItemMapping-like MagicMock with item_id + provider only.

    :param item_id: Library item id from ``recently_played``.
    :param provider: Provider slot the mapping belongs to (typically ``"library"``).
    """
    mapping = MagicMock()
    mapping.item_id = item_id
    mapping.provider = provider
    return mapping


def make_analysis_row(
    *,
    item_id: str,
    provider: str = "spotify",
    clap_embedding: Any = None,
    aa_provider_domain: str = "sonic_analysis",
) -> dict[str, Any]:
    """
    Build an audio_analysis DB row dict in the shape iter_audio_analysis_rows yields.

    :param item_id: Track id (matches a key in ``_signature_cache``).
    :param provider: Provider instance the row belongs to.
    :param clap_embedding: Value stored under ``extra_data["clap_embedding"]``.
        Use a 1024-element list of floats for happy paths; pass None to
        simulate rows that lack an embedding.
    :param aa_provider_domain: Defaults to ``"sonic_analysis"``, the
        single AA-provider the plugin reads in tests.
    """
    import json  # noqa: PLC0415

    extra_data: dict[str, Any] = {}
    if clap_embedding is not None:
        extra_data["clap_embedding"] = clap_embedding
    analysis_payload = {"extra_data": extra_data}
    return {
        "item_id": item_id,
        "provider": provider,
        "aa_provider_domain": aa_provider_domain,
        "analysis_data": json.dumps(analysis_payload),
    }
