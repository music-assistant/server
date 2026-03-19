"""Sonic Analysis plugin provider."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
from music_assistant_models.enums import ConfigEntryType, ProviderFeature

from music_assistant.constants import DB_TABLE_SONIC_SIGNATURES
from music_assistant.helpers.sonic_analysis import (
    SIGNATURE_VERSION,
    SonicSignature,
)
from music_assistant.models.plugin import PluginProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES: set[ProviderFeature] = set()

VOYAGER_INDEX_FILENAME = "sonic_signatures.voy"
CORPUS_STATS_ITEM_ID = "__corpus_stats__"

CONF_ANALYZE_ON_PLAY = "analyze_on_play"
CONF_ANALYZE_ON_SYNC = "analyze_on_sync"
CONF_MAX_CONCURRENT_ANALYSES = "max_concurrent_analyses"

try:
    import voyager
except ImportError:
    voyager = None


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return SonicAnalysisProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    return (
        ConfigEntry(
            key=CONF_ANALYZE_ON_PLAY,
            type=ConfigEntryType.BOOLEAN,
            label="Analyze on play",
            description="Automatically extract a sonic signature when a track is played.",
            default_value=True,
        ),
        ConfigEntry(
            key=CONF_ANALYZE_ON_SYNC,
            type=ConfigEntryType.BOOLEAN,
            label="Analyze on sync",
            description="Automatically extract sonic signatures during library sync.",
            default_value=True,
        ),
        ConfigEntry(
            key=CONF_MAX_CONCURRENT_ANALYSES,
            type=ConfigEntryType.INTEGER,
            label="Max concurrent analyses",
            description="Maximum number of tracks analysed in parallel.",
            default_value=2,
            required=True,
        ),
    )


class SonicAnalysisProvider(PluginProvider):
    """Plugin provider that extracts sonic signatures and enables similarity-based discovery."""

    corpus_means: list[float] | None
    corpus_stds: list[float] | None
    _on_unload: list[Callable[[], None]]

    async def handle_async_init(self) -> None:
        """Handle async initialisation: create DB table and load corpus stats."""
        self._on_unload = []
        self.corpus_means = None
        self.corpus_stds = None

        await self._create_db_table()
        await self._load_corpus_stats()

    async def _create_db_table(self) -> None:
        """Create the sonic_signatures table if it does not already exist."""
        assert self.mass.music.database is not None
        await self.mass.music.database.execute(
            f"""CREATE TABLE IF NOT EXISTS {DB_TABLE_SONIC_SIGNATURES}(
                [id] INTEGER PRIMARY KEY AUTOINCREMENT,
                [item_id] TEXT NOT NULL,
                [provider] TEXT NOT NULL,
                [features] TEXT NOT NULL,
                [version] INTEGER NOT NULL,
                [timestamp] REAL NOT NULL DEFAULT (cast(strftime('%s','now') as int)),
                UNIQUE(item_id, provider)
            )"""
        )
        await self.mass.music.database.commit()

    async def _load_corpus_stats(self) -> None:
        """Load corpus statistics from the special sentinel DB row, if present."""
        assert self.mass.music.database is not None
        row = await self.mass.music.database.get_row(
            DB_TABLE_SONIC_SIGNATURES,
            {"item_id": CORPUS_STATS_ITEM_ID, "provider": CORPUS_STATS_ITEM_ID},
        )
        if row is None:
            return
        try:
            payload: dict[str, list[float]] = json.loads(row["features"])
            self.corpus_means = payload["means"]
            self.corpus_stds = payload["stds"]
        except (KeyError, ValueError, TypeError):
            self.logger.warning("Failed to deserialise corpus stats from DB; resetting.")
            self.corpus_means = None
            self.corpus_stds = None

    async def _save_corpus_stats(self, means: list[float], stds: list[float]) -> None:
        """Persist corpus statistics as a sentinel row in the DB.

        :param means: Per-feature mean values computed over the corpus.
        :param stds: Per-feature standard deviation values computed over the corpus.
        """
        assert self.mass.music.database is not None
        payload = json.dumps({"means": means, "stds": stds})
        await self.mass.music.database.insert_or_replace(
            DB_TABLE_SONIC_SIGNATURES,
            {
                "item_id": CORPUS_STATS_ITEM_ID,
                "provider": CORPUS_STATS_ITEM_ID,
                "features": payload,
                "version": SIGNATURE_VERSION,
            },
        )
        self.corpus_means = means
        self.corpus_stds = stds

    async def get_sonic_signature(self, item_id: str, provider: str) -> SonicSignature | None:
        """Return the stored sonic signature for a track, or None if not found.

        :param item_id: Provider-scoped track identifier.
        :param provider: Provider domain that owns the track.
        """
        assert self.mass.music.database is not None
        row = await self.mass.music.database.get_row(
            DB_TABLE_SONIC_SIGNATURES,
            {"item_id": item_id, "provider": provider},
        )
        if row is None:
            return None
        try:
            features: list[float] = json.loads(row["features"])
            return SonicSignature(features=features, version=int(row["version"]))
        except (KeyError, ValueError, TypeError):
            self.logger.warning(
                "Failed to deserialise sonic signature for %s/%s", provider, item_id
            )
            return None

    async def set_sonic_signature(
        self, item_id: str, provider: str, signature: SonicSignature
    ) -> None:
        """Persist a sonic signature for a track.

        :param item_id: Provider-scoped track identifier.
        :param provider: Provider domain that owns the track.
        :param signature: The sonic signature to store.
        """
        assert self.mass.music.database is not None
        await self.mass.music.database.insert_or_replace(
            DB_TABLE_SONIC_SIGNATURES,
            {
                "item_id": item_id,
                "provider": provider,
                "features": json.dumps(signature.features),
                "version": signature.version,
            },
        )

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider.

        Called when provider is deregistered (e.g. MA exiting or config reloading).

        :param is_removed: True when the provider is permanently removed from configuration.
        """
        for unload_cb in self._on_unload:
            unload_cb()
