# Sonic Analysis Plugin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a plugin provider that extracts 38-dimension sonic signatures from tracks using librosa, indexes them with Voyager for fast ANN similarity search, and exposes a REST API for querying similar tracks.

**Architecture:** A shared helper module (`music_assistant/helpers/sonic_analysis.py`) handles pure librosa feature extraction and normalization. A plugin provider (`music_assistant/providers/sonic_analysis/`) orchestrates analysis triggers (sync and playback events), manages DB storage and the Voyager index, and registers the API endpoint. The DB table is the source of truth; the Voyager index is a derived acceleration structure.

**Tech Stack:** librosa (existing), voyager (new — ANN index), numpy (existing), aiosqlite (existing)

**Spec:** `docs/superpowers/specs/2026-03-19-sonic-analysis-design.md`

---

## File Structure

| Action | Path | Responsibility |
|--------|------|----------------|
| Create | `music_assistant/helpers/sonic_analysis.py` | Pure feature extraction, normalization, distance computation |
| Create | `music_assistant/providers/sonic_analysis/__init__.py` | Plugin provider: events, DB, Voyager index, API endpoint |
| Create | `music_assistant/providers/sonic_analysis/manifest.json` | Provider metadata and config schema |
| Modify | `music_assistant/constants.py` | Add `DB_TABLE_SONIC_SIGNATURES` constant |
| Modify | `pyproject.toml` | Add `voyager` dependency |
| Create | `tests/providers/sonic_analysis/__init__.py` | Test package init |
| Create | `tests/providers/sonic_analysis/test_helper.py` | Unit tests for helper module |
| Create | `tests/providers/sonic_analysis/test_provider.py` | Integration tests for plugin provider |

---

## Task 1: Add voyager dependency

**Files:**
- Modify: `pyproject.toml:12-48` (dependencies list)

- [ ] **Step 1: Add voyager to pyproject.toml**

In `pyproject.toml`, add `voyager` to the `[project] dependencies` list, after the `librosa` entry (line ~43):

```toml
  "voyager==2.1.0",
```

- [ ] **Step 2: Install the dependency**

Run: `cd /c/CodeProjects/server-bliss-rs-integration && uv sync`
Expected: voyager installs successfully

- [ ] **Step 3: Verify import**

Run: `cd /c/CodeProjects/server-bliss-rs-integration && python -c "import voyager; print(voyager.__version__)"`
Expected: Prints version number without error

- [ ] **Step 4: Commit**

```bash
cd /c/CodeProjects/server-bliss-rs-integration
git add pyproject.toml uv.lock
git commit -m "feat: add voyager dependency for ANN similarity search"
```

---

## Task 2: Add DB table constant

**Files:**
- Modify: `music_assistant/constants.py:158-159` (after existing DB table constants)

- [ ] **Step 1: Add the constant**

Add after `DB_TABLE_SMART_FADES_ANALYSIS` in `music_assistant/constants.py`:

```python
DB_TABLE_SONIC_SIGNATURES: Final[str] = "sonic_signatures"
```

- [ ] **Step 2: Commit**

```bash
cd /c/CodeProjects/server-bliss-rs-integration
git add music_assistant/constants.py
git commit -m "feat: add DB_TABLE_SONIC_SIGNATURES constant"
```

---

## Task 3: Helper module — feature extraction with TDD

**Files:**
- Create: `music_assistant/helpers/sonic_analysis.py`
- Create: `tests/providers/sonic_analysis/__init__.py`
- Create: `tests/providers/sonic_analysis/test_helper.py`

### 3a: Write failing test for SonicSignature dataclass

- [ ] **Step 1: Create test package init**

Create empty `tests/providers/sonic_analysis/__init__.py`.

- [ ] **Step 2: Write the failing test**

Create `tests/providers/sonic_analysis/test_helper.py`:

```python
"""Tests for the sonic analysis helper module."""

from __future__ import annotations

import numpy as np
import pytest


def test_sonic_signature_dataclass() -> None:
    """Test SonicSignature dataclass has correct structure."""
    from music_assistant.helpers.sonic_analysis import SIGNATURE_VERSION, SonicSignature

    sig = SonicSignature(
        features=[0.0] * 38,
        version=SIGNATURE_VERSION,
        feature_names=["test"] * 38,
    )
    assert len(sig.features) == 38
    assert sig.version == SIGNATURE_VERSION
    assert len(sig.feature_names) == 38
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd /c/CodeProjects/server-bliss-rs-integration && python -m pytest tests/providers/sonic_analysis/test_helper.py::test_sonic_signature_dataclass -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 4: Write minimal implementation**

Create `music_assistant/helpers/sonic_analysis.py`:

```python
"""Sonic analysis helper — audio feature extraction and similarity computation.

Pure analysis engine using librosa for feature extraction and numpy for
normalization/distance. No Music Assistant-specific dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import librosa
import numpy as np
import numpy.typing as npt

SIGNATURE_VERSION: int = 1

FEATURE_NAMES: list[str] = [
    # MFCCs (13)
    "mfcc_1", "mfcc_2", "mfcc_3", "mfcc_4", "mfcc_5", "mfcc_6", "mfcc_7",
    "mfcc_8", "mfcc_9", "mfcc_10", "mfcc_11", "mfcc_12", "mfcc_13",
    # Chroma (12)
    "chroma_1", "chroma_2", "chroma_3", "chroma_4", "chroma_5", "chroma_6",
    "chroma_7", "chroma_8", "chroma_9", "chroma_10", "chroma_11", "chroma_12",
    # Spectral contrast (7)
    "spectral_contrast_1", "spectral_contrast_2", "spectral_contrast_3",
    "spectral_contrast_4", "spectral_contrast_5", "spectral_contrast_6",
    "spectral_contrast_7",
    # Scalars (6)
    "tempo",
    "spectral_centroid",
    "spectral_rolloff",
    "spectral_flatness",
    "rms_energy",
    "zcr",
]

SIGNATURE_DIMENSIONS: int = len(FEATURE_NAMES)  # 38


@dataclass
class SonicSignature:
    """A track's sonic signature — a fixed-length feature vector."""

    features: list[float]
    version: int
    feature_names: list[str] = field(default_factory=lambda: list(FEATURE_NAMES))
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd /c/CodeProjects/server-bliss-rs-integration && python -m pytest tests/providers/sonic_analysis/test_helper.py::test_sonic_signature_dataclass -v`
Expected: PASS

### 3b: Write failing test for extract_signature

- [ ] **Step 6: Write the failing test**

Append to `tests/providers/sonic_analysis/test_helper.py`:

```python
def test_extract_signature_returns_correct_dimensions() -> None:
    """Test that extract_signature returns a 38-dimension signature."""
    from music_assistant.helpers.sonic_analysis import (
        SIGNATURE_DIMENSIONS,
        SIGNATURE_VERSION,
        extract_signature,
    )

    # Generate a 3-second 440Hz sine wave at 22050 Hz
    sr = 22050
    duration = 3.0
    t = np.linspace(0, duration, int(sr * duration), endpoint=False)
    audio = np.sin(2 * np.pi * 440 * t).astype(np.float32)

    sig = extract_signature(audio, sr)

    assert len(sig.features) == SIGNATURE_DIMENSIONS
    assert sig.version == SIGNATURE_VERSION
    assert len(sig.feature_names) == SIGNATURE_DIMENSIONS
    assert all(isinstance(f, float) for f in sig.features)
    assert all(np.isfinite(f) for f in sig.features)
```

- [ ] **Step 7: Run test to verify it fails**

Run: `cd /c/CodeProjects/server-bliss-rs-integration && python -m pytest tests/providers/sonic_analysis/test_helper.py::test_extract_signature_returns_correct_dimensions -v`
Expected: FAIL with `ImportError` (extract_signature not defined)

- [ ] **Step 8: Implement extract_signature**

Add to `music_assistant/helpers/sonic_analysis.py`:

```python
def extract_signature(
    audio: npt.NDArray[np.float32],
    sample_rate: int = 22050,
) -> SonicSignature:
    """Extract a sonic signature from raw audio data.

    :param audio: mono audio as a float32 numpy array.
    :param sample_rate: sample rate of the audio (default 22050 Hz).
    """
    features: list[float] = []

    # MFCCs (13 dimensions)
    mfccs = librosa.feature.mfcc(y=audio, sr=sample_rate, n_mfcc=13)
    features.extend(float(v) for v in np.mean(mfccs, axis=1))

    # Chroma (12 dimensions)
    chroma = librosa.feature.chroma_stft(y=audio, sr=sample_rate)
    features.extend(float(v) for v in np.mean(chroma, axis=1))

    # Spectral contrast (7 dimensions, n_bands=6 pinned)
    contrast = librosa.feature.spectral_contrast(y=audio, sr=sample_rate, n_bands=6)
    features.extend(float(v) for v in np.mean(contrast, axis=1))

    # Tempo (1 dimension)
    tempo, _ = librosa.beat.beat_track(y=audio, sr=sample_rate)
    bpm = float(tempo.item()) if hasattr(tempo, "item") else float(tempo)
    features.append(bpm)

    # Spectral centroid (1 dimension)
    centroid = librosa.feature.spectral_centroid(y=audio, sr=sample_rate)
    features.append(float(np.mean(centroid)))

    # Spectral rolloff (1 dimension)
    rolloff = librosa.feature.spectral_rolloff(y=audio, sr=sample_rate)
    features.append(float(np.mean(rolloff)))

    # Spectral flatness (1 dimension)
    flatness = librosa.feature.spectral_flatness(y=audio)
    features.append(float(np.mean(flatness)))

    # RMS energy (1 dimension)
    rms = librosa.feature.rms(y=audio)
    features.append(float(np.mean(rms)))

    # ZCR (1 dimension)
    zcr = librosa.feature.zero_crossing_rate(y=audio)
    features.append(float(np.mean(zcr)))

    return SonicSignature(
        features=features,
        version=SIGNATURE_VERSION,
    )
```

- [ ] **Step 9: Run test to verify it passes**

Run: `cd /c/CodeProjects/server-bliss-rs-integration && python -m pytest tests/providers/sonic_analysis/test_helper.py::test_extract_signature_returns_correct_dimensions -v`
Expected: PASS

### 3c: Write failing test for normalization and distance

- [ ] **Step 10: Write the failing tests**

Append to `tests/providers/sonic_analysis/test_helper.py`:

```python
def test_normalize_features() -> None:
    """Test z-score normalization of feature vectors."""
    from music_assistant.helpers.sonic_analysis import normalize_features

    raw = [100.0, 0.5, 200.0]
    means = [50.0, 0.25, 100.0]
    stds = [50.0, 0.25, 100.0]

    result = normalize_features(raw, means, stds)

    assert len(result) == 3
    assert result[0] == pytest.approx(1.0)  # (100 - 50) / 50
    assert result[1] == pytest.approx(1.0)  # (0.5 - 0.25) / 0.25
    assert result[2] == pytest.approx(1.0)  # (200 - 100) / 100


def test_normalize_features_zero_std() -> None:
    """Test normalization handles zero std (constant feature) gracefully."""
    from music_assistant.helpers.sonic_analysis import normalize_features

    raw = [5.0]
    means = [5.0]
    stds = [0.0]

    result = normalize_features(raw, means, stds)
    assert result[0] == pytest.approx(0.0)


def test_compute_distance_identical() -> None:
    """Test that distance between identical signatures is 0."""
    from music_assistant.helpers.sonic_analysis import compute_distance

    sig = [1.0, 2.0, 3.0]
    assert compute_distance(sig, sig) == pytest.approx(0.0, abs=1e-6)


def test_compute_distance_different() -> None:
    """Test that distance between different signatures is positive."""
    from music_assistant.helpers.sonic_analysis import compute_distance

    sig_a = [1.0, 0.0, 0.0]
    sig_b = [0.0, 1.0, 0.0]
    dist = compute_distance(sig_a, sig_b)
    assert dist > 0.0


def test_compute_corpus_stats() -> None:
    """Test corpus statistics computation over multiple signatures."""
    from music_assistant.helpers.sonic_analysis import compute_corpus_stats

    sigs = [
        [10.0, 20.0, 30.0],
        [20.0, 40.0, 60.0],
        [30.0, 60.0, 90.0],
    ]
    means, stds = compute_corpus_stats(sigs)
    assert means[0] == pytest.approx(20.0)
    assert means[1] == pytest.approx(40.0)
    assert means[2] == pytest.approx(60.0)
    assert all(s > 0.0 for s in stds)
```

- [ ] **Step 11: Run tests to verify they fail**

Run: `cd /c/CodeProjects/server-bliss-rs-integration && python -m pytest tests/providers/sonic_analysis/test_helper.py -k "normalize or distance or corpus" -v`
Expected: FAIL with `ImportError`

- [ ] **Step 12: Implement normalization and distance functions**

Add to `music_assistant/helpers/sonic_analysis.py`:

```python
def normalize_features(
    raw_features: list[float],
    corpus_means: list[float],
    corpus_stds: list[float],
) -> list[float]:
    """Apply per-feature z-score normalization.

    :param raw_features: raw feature values from extract_signature.
    :param corpus_means: per-feature means across the corpus.
    :param corpus_stds: per-feature standard deviations across the corpus.
    """
    result: list[float] = []
    for val, mean, std in zip(raw_features, corpus_means, corpus_stds):
        if std == 0.0:
            result.append(0.0)
        else:
            result.append((val - mean) / std)
    return result


def compute_distance(sig_a: list[float], sig_b: list[float]) -> float:
    """Compute cosine distance between two feature vectors.

    :param sig_a: first feature vector (normalized).
    :param sig_b: second feature vector (normalized).
    """
    a = np.array(sig_a, dtype=np.float32)
    b = np.array(sig_b, dtype=np.float32)
    dot = np.dot(a, b)
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0.0 or norm_b == 0.0:
        return 1.0
    similarity = dot / (norm_a * norm_b)
    return float(1.0 - similarity)


def compute_corpus_stats(
    all_features: list[list[float]],
) -> tuple[list[float], list[float]]:
    """Compute per-feature mean and std across the corpus.

    :param all_features: list of raw feature vectors.
    """
    arr = np.array(all_features, dtype=np.float64)
    means = np.mean(arr, axis=0).tolist()
    stds = np.std(arr, axis=0).tolist()
    return means, stds
```

- [ ] **Step 13: Run all helper tests**

Run: `cd /c/CodeProjects/server-bliss-rs-integration && python -m pytest tests/providers/sonic_analysis/test_helper.py -v`
Expected: All PASS

- [ ] **Step 14: Commit**

```bash
cd /c/CodeProjects/server-bliss-rs-integration
git add music_assistant/helpers/sonic_analysis.py tests/providers/sonic_analysis/
git commit -m "feat: add sonic analysis helper with feature extraction and similarity"
```

---

## Task 4: Provider manifest and config

**Files:**
- Create: `music_assistant/providers/sonic_analysis/manifest.json`

- [ ] **Step 1: Create manifest.json**

Create `music_assistant/providers/sonic_analysis/manifest.json`:

```json
{
  "type": "plugin",
  "domain": "sonic_analysis",
  "name": "Sonic Analysis",
  "description": "Extracts sonic signatures from tracks and enables similarity-based track discovery.",
  "codeowners": [],
  "requirements": ["voyager==2.1.0"],
  "documentation": "https://music-assistant.io/plugin-support/sonic-analysis/",
  "multi_instance": false
}
```

- [ ] **Step 2: Commit**

```bash
cd /c/CodeProjects/server-bliss-rs-integration
git add music_assistant/providers/sonic_analysis/manifest.json
git commit -m "feat: add sonic analysis provider manifest"
```

---

## Task 5: Plugin provider — DB setup and signature storage

**Files:**
- Create: `music_assistant/providers/sonic_analysis/__init__.py`

### 5a: Write failing test for DB table creation

- [ ] **Step 1: Write the failing test**

Create `tests/providers/sonic_analysis/test_provider.py`:

```python
"""Tests for the sonic analysis plugin provider."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

import numpy as np
import pytest

from music_assistant.helpers.sonic_analysis import SIGNATURE_VERSION, SonicSignature


@pytest.fixture
def mass_mock() -> Mock:
    """Return a mock MusicAssistant instance."""
    mass = Mock()
    mass.music = Mock()
    mass.music.database = AsyncMock()
    mass.music.database.execute = AsyncMock()
    mass.music.database.insert_or_replace = AsyncMock()
    mass.music.database.get_rows = AsyncMock(return_value=[])
    mass.music.database.get_row = AsyncMock(return_value=None)
    mass.storage_path = "/tmp/test_ma_storage"
    mass.webserver = Mock()
    mass.webserver.register_dynamic_route = Mock(return_value=lambda: None)
    mass.webserver.unregister_dynamic_route = Mock()
    mass.subscribe = Mock(return_value=lambda: None)
    mass.create_task = Mock()
    return mass


@pytest.fixture
def manifest_mock() -> Mock:
    """Return a mock provider manifest."""
    manifest = Mock()
    manifest.domain = "sonic_analysis"
    return manifest


@pytest.fixture
def config_mock() -> Mock:
    """Return a mock provider config."""
    config = Mock()
    config.name = "Sonic Analysis"
    config.instance_id = "sonic_analysis_test"
    config.enabled = True
    config.get_value.side_effect = lambda key, default=None: {
        "analyze_on_play": True,
        "analyze_on_sync": True,
        "max_concurrent_analyses": 2,
    }.get(key, default)
    return config


async def test_handle_async_init_creates_table(
    mass_mock: Mock, manifest_mock: Mock, config_mock: Mock
) -> None:
    """Test that handle_async_init creates the sonic_signatures table."""
    from music_assistant.providers.sonic_analysis import SonicAnalysisProvider

    provider = SonicAnalysisProvider(mass_mock, manifest_mock, config_mock, set())
    await provider.handle_async_init()

    mass_mock.music.database.execute.assert_called()
    create_call = mass_mock.music.database.execute.call_args_list[0]
    sql = create_call[0][0]
    assert "sonic_signatures" in sql
    assert "CREATE TABLE IF NOT EXISTS" in sql
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /c/CodeProjects/server-bliss-rs-integration && python -m pytest tests/providers/sonic_analysis/test_provider.py::test_handle_async_init_creates_table -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement the provider skeleton with DB setup**

Create `music_assistant/providers/sonic_analysis/__init__.py`:

```python
"""Sonic Analysis plugin provider.

Extracts sonic signatures from tracks using librosa, indexes them with
Voyager for fast ANN similarity search, and exposes a similar-tracks API.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import librosa
import numpy as np
from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
from music_assistant_models.enums import ConfigEntryType, EventType, ProviderFeature

from music_assistant.constants import DB_TABLE_SONIC_SIGNATURES
from music_assistant.helpers.sonic_analysis import (
    SIGNATURE_DIMENSIONS,
    SIGNATURE_VERSION,
    SonicSignature,
    compute_corpus_stats,
    extract_signature,
    normalize_features,
)
from music_assistant.models.plugin import PluginProvider

if TYPE_CHECKING:
    from collections.abc import Callable

    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType
    from music_assistant_models.config_entries import ProviderConfig

SUPPORTED_FEATURES: set[ProviderFeature] = set()

VOYAGER_INDEX_FILENAME = "sonic_signatures.voy"

CORPUS_STATS_ITEM_ID = "__corpus_stats__"


async def setup(
    mass: MusicAssistant,
    manifest: ProviderManifest,
    config: ProviderConfig,
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return SonicAnalysisProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    return (
        ConfigEntry(
            key="analyze_on_play",
            type=ConfigEntryType.BOOLEAN,
            label="Analyze tracks on playback",
            default_value=True,
            description="Analyze streaming tracks after they are played.",
        ),
        ConfigEntry(
            key="analyze_on_sync",
            type=ConfigEntryType.BOOLEAN,
            label="Analyze tracks on library sync",
            default_value=True,
            description="Analyze local tracks when they are added to the library.",
        ),
        ConfigEntry(
            key="max_concurrent_analyses",
            type=ConfigEntryType.INTEGER,
            label="Max concurrent analyses",
            default_value=2,
            description="Maximum number of tracks to analyze simultaneously.",
        ),
    )


class SonicAnalysisProvider(PluginProvider):
    """Sonic Analysis plugin provider."""

    _unsubscribes: list[Callable[[], None]]
    _corpus_means: list[float]
    _corpus_stds: list[float]
    _analysis_semaphore: asyncio.Semaphore

    async def handle_async_init(self) -> None:
        """Handle async initialization."""
        self._unsubscribes = []
        self._corpus_means = []
        self._corpus_stds = []

        max_concurrent = self.config.get_value("max_concurrent_analyses") or 2
        self._analysis_semaphore = asyncio.Semaphore(int(max_concurrent))

        # Create the sonic_signatures table
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
        await self.mass.music.database.execute(
            f"CREATE INDEX IF NOT EXISTS idx_sonic_sig_item "
            f"ON {DB_TABLE_SONIC_SIGNATURES}(item_id, provider)"
        )

        # Load corpus stats from DB
        await self._load_corpus_stats()

    async def _load_corpus_stats(self) -> None:
        """Load corpus normalization statistics from DB."""
        row = await self.mass.music.database.get_row(
            DB_TABLE_SONIC_SIGNATURES,
            {"item_id": CORPUS_STATS_ITEM_ID},
        )
        if row and row["features"]:
            stats = json.loads(row["features"])
            self._corpus_means = stats.get("means", [])
            self._corpus_stds = stats.get("stds", [])

    async def _save_corpus_stats(
        self, means: list[float], stds: list[float]
    ) -> None:
        """Save corpus normalization statistics to DB."""
        self._corpus_means = means
        self._corpus_stds = stds
        stats_json = json.dumps({"means": means, "stds": stds})
        await self.mass.music.database.insert_or_replace(
            DB_TABLE_SONIC_SIGNATURES,
            {
                "item_id": CORPUS_STATS_ITEM_ID,
                "provider": "__system__",
                "features": stats_json,
                "version": SIGNATURE_VERSION,
            },
        )

    async def get_sonic_signature(
        self, item_id: str, provider: str
    ) -> SonicSignature | None:
        """Get a stored sonic signature for a track.

        :param item_id: the track's library item ID.
        :param provider: the source provider instance.
        """
        row = await self.mass.music.database.get_row(
            DB_TABLE_SONIC_SIGNATURES,
            {"item_id": item_id, "provider": provider},
        )
        if not row or row["item_id"] == CORPUS_STATS_ITEM_ID:
            return None
        return SonicSignature(
            features=json.loads(row["features"]),
            version=row["version"],
        )

    async def set_sonic_signature(
        self, item_id: str, provider: str, signature: SonicSignature
    ) -> None:
        """Store a sonic signature for a track.

        :param item_id: the track's library item ID.
        :param provider: the source provider instance.
        :param signature: the extracted sonic signature.
        """
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
        """Handle unload/cleanup."""
        for unsub in self._unsubscribes:
            unsub()
        self._unsubscribes.clear()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /c/CodeProjects/server-bliss-rs-integration && python -m pytest tests/providers/sonic_analysis/test_provider.py::test_handle_async_init_creates_table -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /c/CodeProjects/server-bliss-rs-integration
git add music_assistant/providers/sonic_analysis/__init__.py tests/providers/sonic_analysis/test_provider.py
git commit -m "feat: add sonic analysis provider with DB table setup and signature storage"
```

---

## Task 6: Voyager index integration

**Files:**
- Modify: `music_assistant/providers/sonic_analysis/__init__.py`
- Modify: `tests/providers/sonic_analysis/test_provider.py`

### 6a: Write failing test for Voyager index lifecycle

- [ ] **Step 1: Write the failing test**

Append to `tests/providers/sonic_analysis/test_provider.py`:

```python
async def test_voyager_index_add_and_query(tmp_path: Path) -> None:
    """Test adding items to Voyager index and querying similar."""
    from music_assistant.providers.sonic_analysis import SonicAnalysisProvider

    mass_mock_local = Mock()
    mass_mock_local.music = Mock()
    mass_mock_local.music.database = AsyncMock()
    mass_mock_local.music.database.execute = AsyncMock()
    mass_mock_local.music.database.get_rows = AsyncMock(return_value=[])
    mass_mock_local.music.database.get_row = AsyncMock(return_value=None)
    mass_mock_local.music.database.insert_or_replace = AsyncMock()
    mass_mock_local.storage_path = str(tmp_path)
    mass_mock_local.webserver = Mock()
    mass_mock_local.webserver.register_dynamic_route = Mock(return_value=lambda: None)
    mass_mock_local.webserver.unregister_dynamic_route = Mock()
    mass_mock_local.subscribe = Mock(return_value=lambda: None)
    mass_mock_local.create_task = Mock()

    manifest = Mock()
    manifest.domain = "sonic_analysis"
    config = Mock()
    config.name = "Sonic Analysis"
    config.instance_id = "sonic_analysis_test"
    config.enabled = True
    config.get_value.side_effect = lambda key, default=None: {
        "analyze_on_play": True,
        "analyze_on_sync": True,
        "max_concurrent_analyses": 2,
    }.get(key, default)

    provider = SonicAnalysisProvider(mass_mock_local, manifest, config, set())
    await provider.handle_async_init()

    # Set corpus stats so normalization works
    means = [0.0] * 38
    stds = [1.0] * 38
    await provider._save_corpus_stats(means, stds)

    # Add a few vectors
    provider._init_voyager_index()
    vec_a = [float(i) for i in range(38)]
    vec_b = [float(i) + 0.1 for i in range(38)]
    vec_c = [float(38 - i) for i in range(38)]  # very different

    provider._add_to_index(1, vec_a)
    provider._add_to_index(2, vec_b)
    provider._add_to_index(3, vec_c)

    # Query similar to vec_a — vec_b should be closest
    results = provider._query_index(vec_a, k=2)
    assert len(results) == 2
    # First result should be item 1 (itself) or item 2 (nearly identical)
    result_ids = [r[0] for r in results]
    assert 1 in result_ids or 2 in result_ids
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /c/CodeProjects/server-bliss-rs-integration && python -m pytest tests/providers/sonic_analysis/test_provider.py::test_voyager_index_add_and_query -v`
Expected: FAIL with `AttributeError` (_init_voyager_index not found)

- [ ] **Step 3: Implement Voyager index methods**

Add to `SonicAnalysisProvider` class in `music_assistant/providers/sonic_analysis/__init__.py`:

```python
    def _init_voyager_index(self) -> None:
        """Initialize or load the Voyager ANN index."""
        import voyager

        index_path = Path(self.mass.storage_path) / VOYAGER_INDEX_FILENAME
        if index_path.exists():
            self._voyager_index = voyager.Index.load(str(index_path))
            self.logger.info(
                "Loaded Voyager index with %d items", self._voyager_index.num_elements
            )
        else:
            self._voyager_index = voyager.Index(
                voyager.Space.Cosine,
                num_dimensions=SIGNATURE_DIMENSIONS,
                storage_data_type=voyager.StorageDataType.E4M3,
            )
            self.logger.info("Created new Voyager index")

    def _add_to_index(self, item_id_int: int, normalized_features: list[float]) -> None:
        """Add a normalized feature vector to the Voyager index.

        :param item_id_int: numeric ID to use as the Voyager label.
        :param normalized_features: z-score normalized feature vector.
        """
        vector = np.array([normalized_features], dtype=np.float32)
        self._voyager_index.add_items(vector, ids=np.array([item_id_int]))

    def _query_index(
        self, normalized_features: list[float], k: int = 25
    ) -> list[tuple[int, float]]:
        """Query the Voyager index for similar items.

        :param normalized_features: z-score normalized query vector.
        :param k: number of nearest neighbors to return.
        """
        if self._voyager_index.num_elements == 0:
            return []
        actual_k = min(k, self._voyager_index.num_elements)
        vector = np.array([normalized_features], dtype=np.float32)
        ids, distances = self._voyager_index.query(vector, k=actual_k)
        return [(int(ids[0][i]), float(distances[0][i])) for i in range(len(ids[0]))]

    def _save_voyager_index(self) -> None:
        """Save the Voyager index to disk."""
        if not hasattr(self, "_voyager_index"):
            return
        index_path = Path(self.mass.storage_path) / VOYAGER_INDEX_FILENAME
        self._voyager_index.save(str(index_path))
        self.logger.debug("Saved Voyager index to %s", index_path)
```

Also add to `handle_async_init()`, after loading corpus stats:

```python
        # Initialize Voyager index
        self._init_voyager_index()
```

And update `unload()` to save the index:

```python
    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/cleanup."""
        for unsub in self._unsubscribes:
            unsub()
        self._unsubscribes.clear()
        self._save_voyager_index()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /c/CodeProjects/server-bliss-rs-integration && python -m pytest tests/providers/sonic_analysis/test_provider.py::test_voyager_index_add_and_query -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /c/CodeProjects/server-bliss-rs-integration
git add music_assistant/providers/sonic_analysis/__init__.py tests/providers/sonic_analysis/test_provider.py
git commit -m "feat: add Voyager ANN index integration for similarity search"
```

---

## Task 7: Event subscriptions and analysis pipeline

**Files:**
- Modify: `music_assistant/providers/sonic_analysis/__init__.py`
- Modify: `tests/providers/sonic_analysis/test_provider.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/providers/sonic_analysis/test_provider.py`:

```python
async def test_loaded_in_mass_subscribes_events(
    mass_mock: Mock, manifest_mock: Mock, config_mock: Mock
) -> None:
    """Test that loaded_in_mass subscribes to the correct events."""
    from music_assistant.providers.sonic_analysis import SonicAnalysisProvider

    provider = SonicAnalysisProvider(mass_mock, manifest_mock, config_mock, set())
    await provider.handle_async_init()
    await provider.loaded_in_mass()

    # Should have subscribed to MEDIA_ITEM_PLAYED and MEDIA_ITEM_ADDED
    assert mass_mock.subscribe.call_count >= 2
    event_types = [call[0][1] for call in mass_mock.subscribe.call_args_list]
    assert EventType.MEDIA_ITEM_PLAYED in event_types
    assert EventType.MEDIA_ITEM_ADDED in event_types
```

Add `EventType` to the test file imports:

```python
from music_assistant_models.enums import EventType
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /c/CodeProjects/server-bliss-rs-integration && python -m pytest tests/providers/sonic_analysis/test_provider.py::test_loaded_in_mass_subscribes_events -v`
Expected: FAIL

- [ ] **Step 3: Implement loaded_in_mass with event subscriptions**

Add to `SonicAnalysisProvider` class:

```python
    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        await super().loaded_in_mass()

        # Subscribe to events
        if self.config.get_value("analyze_on_play"):
            self._unsubscribes.append(
                self.mass.subscribe(
                    self._on_media_item_played, EventType.MEDIA_ITEM_PLAYED
                )
            )

        if self.config.get_value("analyze_on_sync"):
            self._unsubscribes.append(
                self.mass.subscribe(
                    self._on_media_item_added, EventType.MEDIA_ITEM_ADDED
                )
            )

    async def _on_media_item_played(self, event: object) -> None:
        """Handle media item played event — analyze if no signature exists."""
        if not event.data or not hasattr(event.data, "item_id"):
            return
        item_id = str(event.data.item_id)
        provider = str(event.data.provider) if hasattr(event.data, "provider") else ""
        # Check if signature already exists
        existing = await self.get_sonic_signature(item_id, provider)
        if existing:
            return
        # Re-stream and analyze the track in the background
        self.mass.create_task(self._fetch_and_analyze(item_id, provider))

    async def _on_media_item_added(self, event: object) -> None:
        """Handle media item added event — queue for background analysis."""
        if not event.data or not hasattr(event.data, "item_id"):
            return
        item_id = str(event.data.item_id)
        provider = str(event.data.provider) if hasattr(event.data, "provider") else ""
        # Queue analysis as a background task
        self.mass.create_task(self._fetch_and_analyze(item_id, provider))

    async def _fetch_and_analyze(self, item_id: str, provider: str) -> None:
        """Fetch audio for a track and run sonic analysis.

        :param item_id: the track's library item ID.
        :param provider: the source provider instance.
        """
        try:
            # Get stream details for the track
            streamdetails = await self.mass.music.get_provider_stream_details(item_id)
            if not streamdetails:
                return

            # If local file, use librosa.load directly
            if streamdetails.path and isinstance(streamdetails.path, str):
                audio, sr = await asyncio.to_thread(
                    librosa.load, streamdetails.path, sr=22050, mono=True
                )
            else:
                # For streaming providers, get PCM via MA's audio pipeline
                pcm_data = bytearray()
                async for chunk in self.mass.music.get_provider_audio_stream(streamdetails):
                    pcm_data.extend(chunk)
                if not pcm_data:
                    return
                audio = (
                    np.frombuffer(bytes(pcm_data), dtype="<i2").astype(np.float32)
                ) / np.float32(32768.0)
                sr = streamdetails.audio_format.sample_rate or 44100
                # Resample to 22050 if needed
                if sr != 22050:
                    audio = await asyncio.to_thread(
                        librosa.resample, audio, orig_sr=sr, target_sr=22050
                    )
                    sr = 22050

            await self._analyze_track(item_id, provider, audio, sr)
        except Exception:
            self.logger.warning(
                "Failed to fetch and analyze track %s", item_id, exc_info=True
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /c/CodeProjects/server-bliss-rs-integration && python -m pytest tests/providers/sonic_analysis/test_provider.py::test_loaded_in_mass_subscribes_events -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /c/CodeProjects/server-bliss-rs-integration
git add music_assistant/providers/sonic_analysis/__init__.py tests/providers/sonic_analysis/test_provider.py
git commit -m "feat: add event subscriptions for play and sync triggers"
```

---

## Task 8: Analysis pipeline — analyze a track end-to-end

**Files:**
- Modify: `music_assistant/providers/sonic_analysis/__init__.py`
- Modify: `tests/providers/sonic_analysis/test_provider.py`

This task implements the core `_analyze_track()` method that takes a track's audio, extracts the signature, normalizes it, stores it in the DB, and adds it to the Voyager index.

- [ ] **Step 1: Write the failing test**

Append to `tests/providers/sonic_analysis/test_provider.py`:

```python
async def test_analyze_track_stores_signature(
    mass_mock: Mock, manifest_mock: Mock, config_mock: Mock
) -> None:
    """Test that _analyze_track extracts, stores, and indexes a signature."""
    from music_assistant.providers.sonic_analysis import SonicAnalysisProvider

    provider = SonicAnalysisProvider(mass_mock, manifest_mock, config_mock, set())
    await provider.handle_async_init()

    # Set up corpus stats
    await provider._save_corpus_stats([0.0] * 38, [1.0] * 38)

    # Generate synthetic audio
    sr = 22050
    t = np.linspace(0, 3.0, int(sr * 3.0), endpoint=False)
    audio = np.sin(2 * np.pi * 440 * t).astype(np.float32)

    await provider._analyze_track("test_item_1", "test_provider", audio, sr)

    # Verify signature was stored in DB
    mass_mock.music.database.insert_or_replace.assert_called()
    store_calls = [
        c for c in mass_mock.music.database.insert_or_replace.call_args_list
        if c[0][1].get("item_id") == "test_item_1"
    ]
    assert len(store_calls) == 1

    stored_features = json.loads(store_calls[0][0][1]["features"])
    assert len(stored_features) == 38

    # Verify item was added to Voyager index
    assert provider._voyager_index.num_elements >= 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /c/CodeProjects/server-bliss-rs-integration && python -m pytest tests/providers/sonic_analysis/test_provider.py::test_analyze_track_stores_signature -v`
Expected: FAIL with `AttributeError` (_analyze_track not found)

- [ ] **Step 3: Implement _analyze_track**

Add to `SonicAnalysisProvider` class:

```python
    async def _analyze_track(
        self,
        item_id: str,
        provider_instance: str,
        audio: npt.NDArray[np.float32],
        sample_rate: int,
    ) -> SonicSignature | None:
        """Extract and store a sonic signature for a track.

        :param item_id: the track's library item ID.
        :param provider_instance: the source provider instance ID.
        :param audio: mono audio as a float32 numpy array.
        :param sample_rate: sample rate of the audio.
        """
        try:
            async with self._analysis_semaphore:
                signature = await asyncio.to_thread(
                    extract_signature, audio, sample_rate
                )
        except Exception:
            self.logger.warning(
                "Sonic analysis failed for track %s", item_id, exc_info=True
            )
            return None

        # Store raw signature in DB (source of truth)
        await self.set_sonic_signature(item_id, provider_instance, signature)

        # Normalize and add to Voyager index
        if self._corpus_means and self._corpus_stds:
            normalized = normalize_features(
                signature.features, self._corpus_means, self._corpus_stds
            )
            try:
                item_id_int = int(item_id)
            except ValueError:
                item_id_int = hash(item_id) & 0x7FFFFFFF
            self._add_to_index(item_id_int, normalized)

        return signature
```

Add the numpy import at the top of the file:

```python
import numpy.typing as npt
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /c/CodeProjects/server-bliss-rs-integration && python -m pytest tests/providers/sonic_analysis/test_provider.py::test_analyze_track_stores_signature -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /c/CodeProjects/server-bliss-rs-integration
git add music_assistant/providers/sonic_analysis/__init__.py tests/providers/sonic_analysis/test_provider.py
git commit -m "feat: add end-to-end track analysis pipeline"
```

---

## Task 9: API endpoint — similar tracks

**Files:**
- Modify: `music_assistant/providers/sonic_analysis/__init__.py`
- Modify: `tests/providers/sonic_analysis/test_provider.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/providers/sonic_analysis/test_provider.py`:

```python
async def test_api_similar_tracks_no_signature(
    mass_mock: Mock, manifest_mock: Mock, config_mock: Mock
) -> None:
    """Test API returns analyzed=false when track has no signature."""
    from unittest.mock import MagicMock

    from aiohttp.test_utils import make_mocked_request

    from music_assistant.providers.sonic_analysis import SonicAnalysisProvider

    provider = SonicAnalysisProvider(mass_mock, manifest_mock, config_mock, set())
    await provider.handle_async_init()

    request = make_mocked_request("GET", "/api/sonic_analysis/similar?item_id=999&limit=10")
    response = await provider._handle_similar_tracks(request)

    assert response.status == 200
    body = json.loads(response.body)
    assert body["analyzed"] is False
    assert body["items"] == []


async def test_api_similar_tracks_missing_item_id(
    mass_mock: Mock, manifest_mock: Mock, config_mock: Mock
) -> None:
    """Test API returns 400 when item_id is missing."""
    from aiohttp.test_utils import make_mocked_request

    from music_assistant.providers.sonic_analysis import SonicAnalysisProvider

    provider = SonicAnalysisProvider(mass_mock, manifest_mock, config_mock, set())
    await provider.handle_async_init()

    request = make_mocked_request("GET", "/api/sonic_analysis/similar")
    response = await provider._handle_similar_tracks(request)

    assert response.status == 400
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /c/CodeProjects/server-bliss-rs-integration && python -m pytest tests/providers/sonic_analysis/test_provider.py -k "test_api" -v`
Expected: FAIL

- [ ] **Step 3: Implement the API handler**

Add to `SonicAnalysisProvider` class:

```python
    async def _handle_similar_tracks(self, request: object) -> object:
        """Handle GET /api/sonic_analysis/similar endpoint.

        :param request: aiohttp web request.
        """
        from aiohttp import web

        item_id = request.query.get("item_id")
        if not item_id:
            return web.json_response(
                {"error": "item_id query parameter is required"}, status=400
            )

        limit_str = request.query.get("limit", "25")
        try:
            limit = min(int(limit_str), 100)
        except ValueError:
            limit = 25

        # Check if the seed track has a signature
        # Try all providers — find any signature for this item_id
        rows = await self.mass.music.database.get_rows(
            DB_TABLE_SONIC_SIGNATURES,
            {"item_id": item_id},
        )
        sig_rows = [r for r in rows if r["item_id"] != CORPUS_STATS_ITEM_ID]

        if not sig_rows:
            return web.json_response({
                "items": [],
                "seed_track_id": item_id,
                "analyzed": False,
            })

        # Use the first available signature
        seed_features = json.loads(sig_rows[0]["features"])

        # Normalize seed features
        if self._corpus_means and self._corpus_stds:
            normalized_seed = normalize_features(
                seed_features, self._corpus_means, self._corpus_stds
            )
        else:
            normalized_seed = seed_features

        # Query Voyager index
        results = self._query_index(normalized_seed, k=limit + 1)

        # Build response, excluding the seed track itself
        try:
            seed_id_int = int(item_id)
        except ValueError:
            seed_id_int = hash(item_id) & 0x7FFFFFFF

        items = []
        for result_id, distance in results:
            if result_id == seed_id_int:
                continue
            # Resolve the Voyager ID back to a full Track object
            try:
                track = await self.mass.music.tracks.get(str(result_id))
            except Exception:
                self.logger.debug("Could not resolve track %d", result_id)
                continue
            items.append({
                "track": track.to_dict() if hasattr(track, "to_dict") else {"item_id": str(result_id)},
                "distance": round(distance, 6),
            })
            if len(items) >= limit:
                break

        return web.json_response({
            "items": items,
            "seed_track_id": item_id,
            "analyzed": True,
        })
```

Add route registration in `loaded_in_mass()`:

```python
        # Register API endpoint
        self._unsubscribes.append(
            self.mass.webserver.register_dynamic_route(
                "/api/sonic_analysis/similar",
                self._handle_similar_tracks,
                "GET",
            )
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /c/CodeProjects/server-bliss-rs-integration && python -m pytest tests/providers/sonic_analysis/test_provider.py -k "test_api" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /c/CodeProjects/server-bliss-rs-integration
git add music_assistant/providers/sonic_analysis/__init__.py tests/providers/sonic_analysis/test_provider.py
git commit -m "feat: add similar tracks API endpoint"
```

---

## Task 10: Voyager index rebuild from DB

**Files:**
- Modify: `music_assistant/providers/sonic_analysis/__init__.py`
- Modify: `tests/providers/sonic_analysis/test_provider.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/providers/sonic_analysis/test_provider.py`:

```python
async def test_rebuild_index_from_db(tmp_path: Path) -> None:
    """Test rebuilding Voyager index from stored signatures in DB."""
    from music_assistant.providers.sonic_analysis import SonicAnalysisProvider

    mass_local = Mock()
    mass_local.music = Mock()
    mass_local.music.database = AsyncMock()
    mass_local.music.database.execute = AsyncMock()
    mass_local.music.database.insert_or_replace = AsyncMock()
    mass_local.music.database.get_row = AsyncMock(return_value=None)
    mass_local.storage_path = str(tmp_path)
    mass_local.webserver = Mock()
    mass_local.webserver.register_dynamic_route = Mock(return_value=lambda: None)
    mass_local.subscribe = Mock(return_value=lambda: None)
    mass_local.create_task = Mock()

    # Simulate stored signatures in DB
    mass_local.music.database.get_rows = AsyncMock(return_value=[
        {"item_id": "1", "provider": "test", "features": json.dumps([float(i) for i in range(38)]), "version": 1},
        {"item_id": "2", "provider": "test", "features": json.dumps([float(i) + 5 for i in range(38)]), "version": 1},
    ])

    config_local = Mock()
    config_local.name = "Sonic Analysis"
    config_local.instance_id = "test"
    config_local.enabled = True
    config_local.get_value.side_effect = lambda key, default=None: {
        "max_concurrent_analyses": 2,
    }.get(key, default)

    manifest_local = Mock()
    manifest_local.domain = "sonic_analysis"

    provider = SonicAnalysisProvider(mass_local, manifest_local, config_local, set())
    await provider.handle_async_init()

    # Set corpus stats and rebuild
    provider._corpus_means = [0.0] * 38
    provider._corpus_stds = [1.0] * 38
    await provider._rebuild_voyager_index()

    assert provider._voyager_index.num_elements == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /c/CodeProjects/server-bliss-rs-integration && python -m pytest tests/providers/sonic_analysis/test_provider.py::test_rebuild_index_from_db -v`
Expected: FAIL

- [ ] **Step 3: Implement _rebuild_voyager_index**

Add to `SonicAnalysisProvider` class:

```python
    async def _rebuild_voyager_index(self) -> None:
        """Rebuild the Voyager index from all signatures in the DB."""
        import voyager

        rows = await self.mass.music.database.get_rows(
            DB_TABLE_SONIC_SIGNATURES,
        )
        sig_rows = [r for r in rows if r["item_id"] != CORPUS_STATS_ITEM_ID]

        if not sig_rows:
            self.logger.info("No signatures in DB — index is empty")
            return

        # Recompute corpus stats from all raw signatures
        all_features = [json.loads(r["features"]) for r in sig_rows]
        means, stds = compute_corpus_stats(all_features)
        await self._save_corpus_stats(means, stds)

        # Create fresh index
        self._voyager_index = voyager.Index(
            voyager.Space.Cosine,
            num_dimensions=SIGNATURE_DIMENSIONS,
        )

        # Add all normalized vectors
        for row in sig_rows:
            features = json.loads(row["features"])
            normalized = normalize_features(features, means, stds)
            try:
                item_id_int = int(row["item_id"])
            except ValueError:
                item_id_int = hash(row["item_id"]) & 0x7FFFFFFF
            self._add_to_index(item_id_int, normalized)

        self._save_voyager_index()
        self.logger.info(
            "Rebuilt Voyager index with %d items", self._voyager_index.num_elements
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /c/CodeProjects/server-bliss-rs-integration && python -m pytest tests/providers/sonic_analysis/test_provider.py::test_rebuild_index_from_db -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /c/CodeProjects/server-bliss-rs-integration
git add music_assistant/providers/sonic_analysis/__init__.py tests/providers/sonic_analysis/test_provider.py
git commit -m "feat: add Voyager index rebuild from DB for recovery"
```

---

## Task 11: Background batch processing for existing library

**Files:**
- Modify: `music_assistant/providers/sonic_analysis/__init__.py`
- Modify: `tests/providers/sonic_analysis/test_provider.py`

This task implements the background sweep that analyzes all existing local-provider tracks that don't have signatures yet. This runs on plugin load so that existing libraries get backfilled.

- [ ] **Step 1: Write the failing test**

Append to `tests/providers/sonic_analysis/test_provider.py`:

```python
async def test_background_backfill_schedules_analysis(
    mass_mock: Mock, manifest_mock: Mock, config_mock: Mock
) -> None:
    """Test that loaded_in_mass schedules background backfill."""
    from music_assistant.providers.sonic_analysis import SonicAnalysisProvider

    # Mock mass.music.tracks.library_items to return some tracks
    mock_track_1 = Mock()
    mock_track_1.item_id = "1"
    mock_track_1.provider_mappings = [Mock(provider_instance="filesystem")]
    mock_track_2 = Mock()
    mock_track_2.item_id = "2"
    mock_track_2.provider_mappings = [Mock(provider_instance="filesystem")]

    mass_mock.music.tracks = Mock()
    mass_mock.music.tracks.library_items = AsyncMock(
        return_value=[mock_track_1, mock_track_2]
    )

    provider = SonicAnalysisProvider(mass_mock, manifest_mock, config_mock, set())
    await provider.handle_async_init()
    await provider.loaded_in_mass()

    # Verify background task was scheduled via mass.create_task
    assert mass_mock.create_task.called
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /c/CodeProjects/server-bliss-rs-integration && python -m pytest tests/providers/sonic_analysis/test_provider.py::test_background_backfill_schedules_analysis -v`
Expected: FAIL

- [ ] **Step 3: Implement background backfill**

Add to `loaded_in_mass()` in `SonicAnalysisProvider`, after the event subscriptions:

```python
        # Schedule background backfill of unanalyzed local tracks
        if self.config.get_value("analyze_on_sync"):
            self.mass.create_task(self._backfill_unanalyzed_tracks())
```

Add the backfill method to `SonicAnalysisProvider`:

```python
    async def _backfill_unanalyzed_tracks(self) -> None:
        """Background task: analyze all local tracks without signatures."""
        self.logger.info("Starting background sonic analysis backfill...")
        analyzed_count = 0
        skipped_count = 0

        try:
            tracks = await self.mass.music.tracks.library_items()
        except Exception:
            self.logger.warning("Could not fetch library tracks for backfill", exc_info=True)
            return

        for track in tracks:
            item_id = str(track.item_id)

            # Check if signature already exists
            has_signature = False
            for mapping in track.provider_mappings:
                existing = await self.get_sonic_signature(item_id, mapping.provider_instance)
                if existing:
                    has_signature = True
                    break

            if has_signature:
                skipped_count += 1
                continue

            # Analyze the track
            for mapping in track.provider_mappings:
                try:
                    await self._fetch_and_analyze(item_id, mapping.provider_instance)
                    analyzed_count += 1
                    break
                except Exception:
                    self.logger.debug(
                        "Backfill: failed to analyze track %s via %s",
                        item_id, mapping.provider_instance,
                    )
                    continue

            # Yield to other tasks periodically
            await asyncio.sleep(0)

        # Recompute corpus stats and rebuild index after batch
        if analyzed_count > 0:
            await self._rebuild_voyager_index()

        self.logger.info(
            "Backfill complete: %d analyzed, %d already had signatures",
            analyzed_count, skipped_count,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /c/CodeProjects/server-bliss-rs-integration && python -m pytest tests/providers/sonic_analysis/test_provider.py::test_background_backfill_schedules_analysis -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /c/CodeProjects/server-bliss-rs-integration
git add music_assistant/providers/sonic_analysis/__init__.py tests/providers/sonic_analysis/test_provider.py
git commit -m "feat: add background batch analysis for existing library tracks"
```

---

## Task 12: Run full test suite and lint

**Files:** All files from previous tasks

- [ ] **Step 1: Run all sonic analysis tests**

Run: `cd /c/CodeProjects/server-bliss-rs-integration && python -m pytest tests/providers/sonic_analysis/ -v`
Expected: All tests PASS

- [ ] **Step 2: Run pre-commit hooks**

Run: `cd /c/CodeProjects/server-bliss-rs-integration && pre-commit run --all-files`
Expected: All checks pass. If ruff or mypy finds issues, fix them.

- [ ] **Step 3: Fix any lint/type issues found**

Apply any fixes from pre-commit output. Common issues:
- Missing type annotations
- Import ordering
- Line length
- Docstring format (use Sphinx `:param:` style per CLAUDE.md)

- [ ] **Step 4: Re-run tests after fixes**

Run: `cd /c/CodeProjects/server-bliss-rs-integration && python -m pytest tests/providers/sonic_analysis/ -v`
Expected: All PASS

- [ ] **Step 5: Commit any lint fixes**

```bash
cd /c/CodeProjects/server-bliss-rs-integration
git add -u
git commit -m "chore: fix lint and type issues in sonic analysis"
```

---

## Task 13: Final integration — run full project test suite

- [ ] **Step 1: Run the full test suite**

Run: `cd /c/CodeProjects/server-bliss-rs-integration && python -m pytest --timeout=120`
Expected: All existing tests still pass, no regressions

- [ ] **Step 2: Verify pre-commit is clean**

Run: `cd /c/CodeProjects/server-bliss-rs-integration && pre-commit run --all-files`
Expected: All checks pass

- [ ] **Step 3: Commit if any final adjustments were needed**

```bash
cd /c/CodeProjects/server-bliss-rs-integration
git add -u
git commit -m "chore: final adjustments after full test suite run"
```
