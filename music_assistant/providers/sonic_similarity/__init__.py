"""Sonic Similarity plugin.

Two similarity engines in one plugin, both backed by usearch HNSW:

* **Traits (18-dim weighted-Euclidean)** (always on): per-track signature
  assembled from sonic_analysis scalars (BPM, energy, loudness, …) and
  ranked with a configurable weight preset. Atomic mmap-view rebuild.

* **Character (1024-dim CLAP cosine)** (opt-in via the ``enable_clap_index`` config
  entry): builds a second usearch index over the CLAP audio embeddings
  already stored by sonic_analysis under
  ``audio_analysis.extra_data["clap_embedding"]``. Track-to-track
  semantic-audio similarity, with no additional dependencies beyond
  usearch itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import ProviderFeature

from music_assistant.providers.sonic_similarity.constants import (
    AA_PROVIDER_DOMAIN,
    ACTION_REBUILD_18DIM,
    ACTION_REBUILD_CLAP,
    CONF_DISCOVER_DIVERSITY,
    CONF_DISCOVER_ENGINE,
    CONF_DISCOVER_PRESET,
    CONF_ENABLE_CLAP_INDEX,
    CONF_ENABLE_DISCOVER_ROW,
    CONF_ENABLE_TEXT_SEARCH,
    CONF_LABEL_STATUS_18DIM,
    CONF_LABEL_STATUS_CLAP,
    CONF_LABEL_STATUS_TEXT,
    CONF_SIMILAR_DIVERSITY,
    CONF_SIMILAR_PRESET,
    CONF_SIMILAR_TRACKS_ENGINE,
    SIMILAR_ENGINE_18DIM,
    SIMILAR_ENGINE_CLAP,
    SUPPORTED_FEATURES,
)
from music_assistant.providers.sonic_similarity.provider import (
    SonicSimilarityPlugin as SonicSimilarityPlugin,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider instance with given configuration."""
    features = SUPPORTED_FEATURES.copy()
    if bool(config.get_value(CONF_ENABLE_TEXT_SEARCH)):
        features.add(ProviderFeature.SEARCH)
    return SonicSimilarityPlugin(mass, manifest, config, features)


async def _collect_status_text(
    mass: MusicAssistant, instance_id: str | None
) -> tuple[str, str, str]:
    """Return (18-dim, CLAP, text-encoder) label-text triples for the plugin page.

    Each string is single-line, safe to render in a LABEL config entry, and
    degrades gracefully when the provider is not yet loaded.

    :param mass: MusicAssistant instance used to look up the loaded provider.
    :param instance_id: Provider instance id to inspect, or None before the
        provider is loaded.
    """
    eighteen = "Traits engine: not yet loaded"
    clap = "Character engine: disabled"
    text = "Text encoder: disabled"
    if not instance_id:
        return eighteen, clap, text
    provider = mass.get_provider(instance_id)
    if not isinstance(provider, SonicSimilarityPlugin):
        return eighteen, clap, text

    # Optional coverage lookup against the upstream AA provider via #3851's API.
    coverage_pct: float | None = None
    try:
        coverage = await mass.streams.audio_analysis.get_coverage(AA_PROVIDER_DOMAIN)
    except Exception:
        coverage = None
    if coverage is not None:
        total = coverage.analyzed + coverage.pending
        if total > 0:
            coverage_pct = round(100.0 * coverage.analyzed / total, 1)

    # 18-dim line.
    index_size = len(provider._search_index) if provider._search_index is not None else 0
    parts = [
        f"{index_size:,} tracks indexed",
        f"{len(provider._signature_cache):,} signatures cached",
        f"corpus stats {'ready' if provider.corpus_means is not None else 'pending'}",
    ]
    if coverage_pct is not None:
        parts.append(f"{coverage_pct}% coverage")
    eighteen = "Traits engine: " + " · ".join(parts)
    if (err_18dim := provider._last_rebuild_error.get("Traits")) is not None:
        eighteen += f" — last rebuild failed: {err_18dim}"

    # CLAP line (only meaningful when the index is built).
    if provider._clap_index is not None:
        clap_size = len(provider._clap_index)
        clap_parts = [f"{clap_size:,} embeddings indexed"]
        if coverage_pct is not None:
            clap_parts.append(f"{coverage_pct}% coverage")
        clap = "Character engine: " + " · ".join(clap_parts)
        if (err_clap := provider._last_rebuild_error.get("Character")) is not None:
            clap += f" — last rebuild failed: {err_clap}"

    # Text-encoder line — encoder state is independent of the index.
    if bool(provider.config.get_value(CONF_ENABLE_TEXT_SEARCH)):
        if provider._text_encoder is not None:
            text = "Text encoder: loaded (warm)"
        else:
            text = "Text encoder: cold (downloads on first query, ~500MB)"

    return eighteen, clap, text


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider.

    :param mass: MusicAssistant instance.
    :param instance_id: id of an existing provider instance (None if new instance setup).
    :param action: action key called from config entries UI.
    :param values: the (intermediate) raw values for config entries sent with the action.
    """
    from music_assistant_models.config_entries import (  # noqa: PLC0415
        ConfigEntry,
        ConfigValueOption,
    )
    from music_assistant_models.enums import ConfigEntryType  # noqa: PLC0415

    # Fire-and-forget rebuild on button click; per-engine locks serialise double-clicks.
    if instance_id and action in (ACTION_REBUILD_18DIM, ACTION_REBUILD_CLAP):
        provider = mass.get_provider(instance_id)
        if isinstance(provider, SonicSimilarityPlugin):
            if action == ACTION_REBUILD_18DIM:
                mass.create_task(provider._safe_rebuild("Traits", provider._rebuild_search_index))
            elif action == ACTION_REBUILD_CLAP and provider._clap_index is not None:
                mass.create_task(
                    provider._safe_rebuild("Character", provider._rebuild_clap_index_from_database)
                )

    status_18, status_clap, status_text = await _collect_status_text(mass, instance_id)

    return (
        # === Generic: the two opt-in engine toggles ===
        ConfigEntry(
            key=CONF_ENABLE_CLAP_INDEX,
            type=ConfigEntryType.BOOLEAN,
            default_value=False,
        ),
        ConfigEntry(
            key=CONF_ENABLE_TEXT_SEARCH,
            type=ConfigEntryType.BOOLEAN,
            default_value=False,
        ),
        # === Similarity search: engine choice + 18-dim tuning ===
        ConfigEntry(
            key=CONF_SIMILAR_TRACKS_ENGINE,
            type=ConfigEntryType.STRING,
            default_value=SIMILAR_ENGINE_18DIM,
            options=[
                ConfigValueOption(SIMILAR_ENGINE_18DIM),
                ConfigValueOption(SIMILAR_ENGINE_CLAP),
            ],
            category="Similarity search",
            depends_on=CONF_ENABLE_CLAP_INDEX,
            depends_on_value=True,
        ),
        ConfigEntry(
            key=CONF_SIMILAR_PRESET,
            type=ConfigEntryType.STRING,
            default_value="balanced",
            options=[
                ConfigValueOption("balanced"),
                ConfigValueOption("vibe"),
                ConfigValueOption("party"),
                ConfigValueOption("genre_era"),
                ConfigValueOption("discover"),
            ],
            category="Similarity search",
            depends_on=CONF_SIMILAR_TRACKS_ENGINE,
            depends_on_value=SIMILAR_ENGINE_18DIM,
        ),
        ConfigEntry(
            key=CONF_SIMILAR_DIVERSITY,
            type=ConfigEntryType.INTEGER,
            default_value=0,
            range=(0, 10),
            category="Similarity search",
            depends_on=CONF_SIMILAR_TRACKS_ENGINE,
            depends_on_value=SIMILAR_ENGINE_18DIM,
        ),
        # === Discover row ===
        ConfigEntry(
            key=CONF_ENABLE_DISCOVER_ROW,
            type=ConfigEntryType.BOOLEAN,
            default_value=True,
            category="Discover",
        ),
        ConfigEntry(
            key=CONF_DISCOVER_ENGINE,
            type=ConfigEntryType.STRING,
            default_value=SIMILAR_ENGINE_18DIM,
            options=[
                ConfigValueOption(SIMILAR_ENGINE_18DIM),
                ConfigValueOption(SIMILAR_ENGINE_CLAP),
            ],
            category="Discover",
            depends_on=CONF_ENABLE_DISCOVER_ROW,
            depends_on_value=True,
        ),
        ConfigEntry(
            key=CONF_DISCOVER_PRESET,
            type=ConfigEntryType.STRING,
            default_value="discover",
            options=[
                ConfigValueOption("discover"),
                ConfigValueOption("balanced"),
                ConfigValueOption("vibe"),
                ConfigValueOption("party"),
                ConfigValueOption("genre_era"),
            ],
            category="Discover",
            depends_on=CONF_DISCOVER_ENGINE,
            depends_on_value=SIMILAR_ENGINE_18DIM,
        ),
        ConfigEntry(
            key=CONF_DISCOVER_DIVERSITY,
            type=ConfigEntryType.INTEGER,
            default_value=2,
            range=(0, 10),
            category="Discover",
            depends_on=CONF_DISCOVER_ENGINE,
            depends_on_value=SIMILAR_ENGINE_18DIM,
        ),
        # === Status: each rebuild (advanced) sits directly under its own status row ===
        ConfigEntry(
            key=CONF_LABEL_STATUS_18DIM,
            type=ConfigEntryType.LABEL,
            label=status_18,
            category="Status",
        ),
        ConfigEntry(
            key=ACTION_REBUILD_18DIM,
            type=ConfigEntryType.ACTION,
            action=ACTION_REBUILD_18DIM,
            category="Status",
            advanced=True,
            required=False,
        ),
        ConfigEntry(
            key=CONF_LABEL_STATUS_CLAP,
            type=ConfigEntryType.LABEL,
            label=status_clap,
            category="Status",
            depends_on=CONF_ENABLE_CLAP_INDEX,
            depends_on_value=True,
        ),
        ConfigEntry(
            key=ACTION_REBUILD_CLAP,
            type=ConfigEntryType.ACTION,
            action=ACTION_REBUILD_CLAP,
            category="Status",
            advanced=True,
            required=False,
            depends_on=CONF_ENABLE_CLAP_INDEX,
            depends_on_value=True,
        ),
        ConfigEntry(
            key=CONF_LABEL_STATUS_TEXT,
            type=ConfigEntryType.LABEL,
            label=status_text,
            category="Status",
            depends_on=CONF_ENABLE_TEXT_SEARCH,
            depends_on_value=True,
        ),
    )
