# Audio Analysis Data Storage Design

## Context

Audio Analysis (AA) providers are plugins that process PCM audio during streaming and produce analysis results (beat tracking, key detection, mood, etc.). Currently, `SmartFadesProvider` is the only AA provider, storing results in a dedicated `smart_fades_analysis` table with typed columns. As more AA providers are added, we need a flexible storage model that doesn't require central code changes for each new provider.

**Key constraints:**
- AA providers are plugins -- we don't know beforehand what properties they produce
- **Core code (mixer, streams controller) cannot have hard coupling with providers** -- consumers must not import from `music_assistant.providers.*`
- The `SmartFadesMixer` (consumer) currently uses typed `SmartFadesAnalysis` objects with numpy arrays
- Providers currently call `mass.music.set_smart_fades_analysis()` directly in their `finalize()`
- The `AudioAnalysisController` calls `provider.finalize()` as fire-and-forget
- Different providers may produce overlapping data (e.g., both detect BPM)

**Current architecture note:** `SmartFadesAnalysis` already lives in `models/smart_fades.py` (shared models layer), not in the provider directory. The mixer imports from models, not providers. This is the right pattern.

**Current storage in `smart_fades_analysis` table:**
- Typed columns: `item_id`, `provider`, `fragment`, `bpm REAL`, `beats TEXT` (JSON), `downbeats TEXT` (JSON), `confidence REAL`, `duration REAL`
- Extended columns added by migration: `musical_key TEXT`, `phrase_boundaries TEXT`, `energy_curve TEXT`, `spectral_centroid_curve TEXT`
- Keyed by `UNIQUE(item_id, provider, fragment)`

---

## Option A: Broad Typed Dataclass (MediaItemMetadata pattern)

One central `AudioAnalysisData` dataclass in `models/` with all-optional typed fields covering known MIR (Music Information Retrieval) concepts. Stored as a single JSON column. Multiple providers contribute partial data via an `update()` merge method. Consumers import only the shared model.

### Data Model
```python
# models/audio_analysis.py -- shared, no provider dependency
@dataclass(kw_only=True)
class AudioAnalysisData(DataClassDictMixin):
    # Beat tracking
    bpm: float | None = None
    beats: npt.NDArray[np.float64] | None = None
    downbeats: npt.NDArray[np.float64] | None = None
    beat_confidence: float | None = None
    # Key / harmony
    musical_key: MusicalKey | None = None
    time_signature: TimeSignature | None = None
    # Structure
    phrase_boundaries: list[PhraseBoundary] | None = None
    # Energy curves
    energy_curve: npt.NDArray[np.float32] | None = None
    spectral_centroid_curve: npt.NDArray[np.float32] | None = None
    # Mood (future)
    mood: str | None = None
    valence: float | None = None
    arousal: float | None = None
    # Meta
    duration: float | None = None
    analysis_version: int = 1

    def update(self, new_values: AudioAnalysisData) -> AudioAnalysisData:
        """Merge new data (non-None fields overwrite None fields)."""
        ...
```

### Storage
```sql
CREATE TABLE audio_analysis(
    item_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    analysis_data json NOT NULL,
    analysis_version INTEGER DEFAULT 1,
    UNIQUE(item_id, provider)
);
```

### Consumer Access (fully decoupled from providers)
```python
from music_assistant.models.audio_analysis import AudioAnalysisData
analysis = await mass.music.get_audio_analysis(item_id, provider)
if analysis and analysis.bpm and analysis.beats is not None:
    # Fully typed, zero provider imports
```

### How a new provider stores data (zero central changes... for MIR fields)
```python
# Provider just fills in the fields it can:
data = AudioAnalysisData(mood="energetic", valence=0.8, arousal=0.9)
await self.mass.music.set_audio_analysis(item_id, provider, data)
# Existing data (e.g., beats from smart_fades) is preserved via update()
```

### Decoupling assessment
**Excellent** -- consumers only import from `models/audio_analysis.py`. The mixer never needs to know which provider produced the data.

### Pros
- Familiar pattern (mirrors `MediaItemMetadata`, which works well in practice)
- **Full type safety** for consumers -- IDE autocomplete, mypy
- **Full decoupling** -- consumers import shared model, not provider types
- Simple single-table storage, no DB migration when adding new fields to dataclass
- The MIR domain is well-defined and finite (BPM, key, beats, mood, energy, structure) -- unlike arbitrary plugin data, this vocabulary stabilizes
- Merge strategy resolves multi-provider contributions

### Cons
- **Adding truly novel analysis types requires editing the central dataclass** -- but in practice MIR concepts are well-known and bounded
- **Growing dataclass** -- though bounded by MIR domain (probably ~20-30 fields max)
- **Last-write-wins for overlapping data** -- if two providers set `bpm`, merge strategy must decide which to keep (can use confidence-based selection)
- **Entire JSON blob deserialized** even if consumer only needs one field
- **Numpy arrays in one big blob** -- expensive if many large arrays

---

## Option B: Flexible Key-Value JSON Blob Per Provider

One table where each row stores one AA provider's results for one track as an opaque JSON blob. Providers define their own schema. No central dataclass.

### Data Model
Each provider defines its own result type internally. No shared model.

### Storage
```sql
CREATE TABLE audio_analysis(
    item_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    aa_provider_domain TEXT NOT NULL,
    analysis_data TEXT NOT NULL,       -- provider-specific JSON blob
    analysis_version INTEGER DEFAULT 1,
    UNIQUE(item_id, provider, aa_provider_domain)
);
```

### Consumer Access
```python
raw = await mass.music.get_audio_analysis(item_id, provider, "smart_fades")
if raw:
    bpm = raw["bpm"]  # untyped dict access
```

### Decoupling assessment
**The mixer is decoupled from provider code BUT has no type safety.** The mixer would need to know string keys like `"bpm"`, `"beats"` and that beats is a list that needs `np.array()` wrapping. This is implicit coupling -- changing the smart_fades output format silently breaks the mixer.

### Pros
- Maximum storage flexibility -- providers are fully independent
- Per-provider attribution -- separate rows per AA provider
- Single table, no migrations for new provider types
- Zero central model changes

### Cons
- **No type safety** for consumers -- raw `dict[str, Any]`, string-key access, runtime errors
- **Implicit coupling** -- consumer knows provider's dict structure via convention, not types
- **Violates the decoupling spirit** -- the mixer doesn't import the provider, but it hard-codes knowledge of the provider's data format (string keys)
- No IDE support
- No SQL queryability

---

## Option C: Per-Provider Dedicated Tables (current approach generalized)

Each AA provider gets its own table with typed columns (like the current `smart_fades_analysis` table). Providers register their table creation SQL.

### Storage
```sql
CREATE TABLE smart_fades_analysis(item_id, provider, bpm REAL, beats TEXT, ...);
CREATE TABLE mood_analysis(item_id, provider, mood TEXT, valence REAL, ...);
```

### Consumer Access
```python
analysis = await mass.music.get_smart_fades_analysis(item_id, provider, fragment)
```

### Decoupling assessment
**Poor** -- requires dedicated get/set methods in `MusicController` per provider. The controller method names themselves encode provider coupling (`get_smart_fades_analysis`). This is what we have today and want to move away from.

### Pros
- Full type safety per provider
- Full SQL queryability
- Independent schema evolution

### Cons
- **Table proliferation** -- N providers = N tables
- **Central controller coupling** -- each new provider needs new get/set methods in music.py
- **Hard coupling by name** -- consumers call provider-specific methods
- Migration complexity per provider

---

## Option D: Typed Common Fields + Flexible Provider Extension (Hybrid)

One table with typed SQL columns for common MIR properties (bpm, key, duration) plus a `provider_data` JSON column for provider-specific extended data. Each AA provider gets its own row.

### Data Model
```python
# models/audio_analysis.py -- shared model
@dataclass(kw_only=True)
class AudioAnalysisResult(DataClassDictMixin):
    # Common typed fields (queryable in SQL)
    bpm: float | None = None
    beat_confidence: float | None = None
    musical_key_root: str | None = None
    musical_key_mode: str | None = None
    duration: float | None = None
    # Provider-specific extension data
    provider_data: dict[str, Any] = field(default_factory=dict)
    # Meta
    aa_provider_domain: str = ""
    analysis_version: int = 1
```

### Storage
```sql
CREATE TABLE audio_analysis(
    item_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    aa_provider_domain TEXT NOT NULL,
    bpm REAL,
    beat_confidence REAL,
    musical_key_root TEXT,
    musical_key_mode TEXT,
    duration REAL,
    provider_data TEXT,          -- JSON blob for everything else
    analysis_version INTEGER DEFAULT 1,
    UNIQUE(item_id, provider, aa_provider_domain)
);
```

### Consumer Access
```python
from music_assistant.models.audio_analysis import AudioAnalysisResult
result = await mass.music.get_audio_analysis(item_id, provider, "smart_fades")
# Common fields are typed:
bpm = result.bpm
# Provider-specific data requires knowledge of structure:
beats = np.array(result.provider_data["beats"])  # untyped
```

### Decoupling assessment
**Mixed** -- common fields are fully decoupled and typed. But accessing provider_data (e.g., beat arrays for crossfading) reintroduces implicit coupling. The mixer needs beats/downbeats, which are in provider_data.

### Pros
- SQL queryability on common fields (BPM range queries, harmonic mixing)
- Per-provider attribution
- Common fields fully typed and decoupled
- Single table

### Cons
- **Two access layers** -- common fields typed, provider_data not
- **The data the mixer actually needs (beats, downbeats) ends up in the untyped provider_data**
- "Common fields" design bottleneck
- More complex

---

## Option E: Provider-Keyed JSON with Typed Registry

Simple storage (one table, one JSON column per AA provider per track) combined with a typed registry that maps AA provider domains to result dataclass types. Consumers specify the expected type.

### Consumer Access
```python
analysis = await mass.music.get_audio_analysis(
    item_id, provider, "smart_fades",
    result_type=ExtendedSmartFadesAnalysis,  # type defined in models/
)
```

### Decoupling assessment
**Good, but with a caveat.** The consumer doesn't import from the provider, but it must:
1. Know the AA provider domain string `"smart_fades"` -- coupling by name
2. Know the result type `ExtendedSmartFadesAnalysis` -- this lives in `models/`, so technically decoupled, BUT the type is provider-specific. The mixer is saying "give me the smart_fades result."

This is still a form of coupling: the mixer is aware of which specific AA provider produces the data it needs.

### Pros
- True plugin architecture for storage -- zero central changes for new providers
- Typed consumer access via result_type parameter
- Single table, no migrations
- Per-provider attribution

### Cons
- **Consumer names the provider** (`"smart_fades"`) -- coupling by convention
- **Provider-specific types must live in models/** -- but they're conceptually provider-owned
- If the smart_fades provider is not installed, the mixer still references its types
- No SQL queryability

---

## Option F: Option A + Per-Provider Rows (Recommended)

**A refinement of Option A that keeps per-provider rows while using a shared typed model.** Each AA provider stores its contributions as a partial `AudioAnalysisData` (a shared typed model with all-optional fields). The controller provides both per-provider access and an aggregated view.

### Data Model
```python
# models/audio_analysis.py -- shared, no provider dependency
@dataclass(kw_only=True)
class AudioAnalysisData(DataClassDictMixin):
    """Audio analysis data using standard MIR concepts.

    All fields optional -- each AA provider fills in what it can produce.
    Multiple providers' data is stored in separate rows and can be
    aggregated for consumers.
    """
    # Rhythm
    bpm: float | None = None
    beats: npt.NDArray[np.float64] | None = None
    downbeats: npt.NDArray[np.float64] | None = None
    beat_confidence: float | None = None
    time_signature: TimeSignature | None = None
    # Harmony
    musical_key: MusicalKey | None = None
    # Structure
    phrase_boundaries: list[PhraseBoundary] | None = None
    # Timbre / energy
    energy_curve: npt.NDArray[np.float32] | None = None
    spectral_centroid_curve: npt.NDArray[np.float32] | None = None
    # Mood / semantic
    mood: str | None = None
    valence: float | None = None
    arousal: float | None = None
    # General
    duration: float | None = None
    analysis_version: int = 1

    class Config(BaseConfig):
        serialization_strategy = {
            np.ndarray: {
                "serialize": lambda x: x.tolist(),
                "deserialize": lambda x: np.array(x),
            }
        }

    def update(self, new_values: AudioAnalysisData) -> AudioAnalysisData:
        """Merge new data. For each field: keep existing if new is None,
        otherwise prefer highest confidence or newest value."""
        ...
```

### Storage
```sql
CREATE TABLE audio_analysis(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT NOT NULL,
    provider TEXT NOT NULL,           -- music provider (e.g. spotify domain)
    aa_provider_domain TEXT NOT NULL,  -- which AA provider produced this
    analysis_data json NOT NULL,       -- AudioAnalysisData as JSON
    analysis_version INTEGER DEFAULT 1,
    timestamp_created INTEGER DEFAULT (cast(strftime('%s','now') as int)),
    UNIQUE(item_id, provider, aa_provider_domain)
);
```

### Controller API
```python
# Store one provider's contribution
async def set_audio_analysis(
    self, item_id: str, provider: str,
    aa_provider_domain: str, analysis: AudioAnalysisData,
) -> None:
    """Store analysis results from one AA provider."""
    data_json = await asyncio.to_thread(lambda: json_dumps(analysis.to_dict()))
    await self.database.insert_or_replace(DB_TABLE_AUDIO_ANALYSIS, {
        "item_id": item_id, "provider": prov_key,
        "aa_provider_domain": aa_provider_domain,
        "analysis_data": data_json,
        "analysis_version": analysis.analysis_version,
    })

# Get aggregated view (what consumers typically want)
async def get_audio_analysis(
    self, item_id: str, provider: str,
) -> AudioAnalysisData | None:
    """Get merged analysis data from all AA providers for this track."""
    rows = await self.database.get_rows(DB_TABLE_AUDIO_ANALYSIS, {
        "item_id": item_id, "provider": prov_key,
    })
    if not rows:
        return None
    merged = AudioAnalysisData()
    for row in rows:
        row_data = await asyncio.to_thread(
            lambda r=row: AudioAnalysisData.from_dict(json_loads(r["analysis_data"]))
        )
        merged.update(row_data)
    return merged

# Get specific provider's contribution (for debugging/admin)
async def get_audio_analysis_by_provider(
    self, item_id: str, provider: str, aa_provider_domain: str,
) -> AudioAnalysisData | None:
    """Get analysis data from a specific AA provider."""
    ...
```

### Consumer Access (SmartFadesMixer -- fully decoupled)
```python
from music_assistant.models.audio_analysis import AudioAnalysisData

# The mixer doesn't know or care which provider produced the data
analysis = await mass.music.get_audio_analysis(item_id, provider)
if (
    analysis
    and analysis.bpm
    and analysis.beats is not None
    and analysis.beat_confidence and analysis.beat_confidence > 0.3
):
    # All typed, all from shared model, zero provider coupling
    fade_analysis = SmartFadesAnalysis(
        fragment=fragment,
        bpm=analysis.bpm,
        beats=analysis.beats,
        downbeats=analysis.downbeats or np.array([]),
        confidence=analysis.beat_confidence,
        duration=analysis.duration or 0.0,
    )
```

### How a provider stores data
```python
# In SmartFadesProvider.finalize():
data = AudioAnalysisData(
    bpm=result_bpm,
    beats=result_beats,
    downbeats=result_downbeats,
    beat_confidence=confidence,
    musical_key=key_result,
    phrase_boundaries=phrases,
    energy_curve=energy,
    duration=duration,
)
await self.mass.music.set_audio_analysis(
    item_id, provider, self.domain, data
)
```

### How a new "mood detection" provider stores data (zero central changes if it uses existing fields)
```python
data = AudioAnalysisData(
    mood="energetic",
    valence=0.8,
    arousal=0.9,
    bpm=122.0,  # this provider also detects BPM
)
await self.mass.music.set_audio_analysis(item_id, provider, self.domain, data)
```

### Decoupling assessment
**Excellent** -- the mixer says `get_audio_analysis(item_id, provider)` and gets back a typed `AudioAnalysisData`. It has zero knowledge of which AA provider(s) contributed the data. No provider imports, no provider domain strings, no provider-specific types.

### Pros
- **Full decoupling** -- consumers import only from `models/audio_analysis.py`, never reference any provider
- **Full type safety** -- all fields typed, IDE autocomplete, mypy checking
- **Familiar pattern** -- exactly mirrors `MediaItemMetadata` which is proven to work in this codebase
- **Per-provider attribution preserved** -- separate rows in DB, queryable by `aa_provider_domain`
- **Aggregated view for consumers** -- merged result from all providers
- **No DB migration for new MIR fields** -- just add to dataclass (JSON storage)
- **Overlapping data resolved** -- merge strategy in `update()` (confidence-based, newest-wins, etc.)
- **Domain-bounded growth** -- MIR vocabulary is well-known: rhythm, harmony, structure, timbre, mood. ~20-30 fields covers the standard MIR feature set
- **Single table** -- no proliferation

### Cons
- **Truly novel analysis categories require central model edit** -- but MIR is a mature field
- **Large JSON blobs** -- a provider producing beats + energy curves + spectral centroids creates a sizable JSON (though no worse than current approach)
- **No SQL queryability** on analysis fields (could be added later with indexed columns)
- **All-or-nothing deserialization** per row

---

## Comparison Matrix

| Criterion | A (Broad, merged) | B (JSON blob) | C (Per tables) | D (Hybrid) | E (Registry) | **F (A + per-provider rows)** |
|---|---|---|---|---|---|---|
| Core-provider decoupling | **Excellent** | Implicit coupling | **Poor** | Mixed | Good (names provider) | **Excellent** |
| Type safety (consumer) | **Excellent** | Poor | Excellent | Medium | Good | **Excellent** |
| Plugin independence | Medium | Excellent | Medium | Good | Excellent | **Good** |
| SQL queryability | None | None | Full | Common fields | None | None (upgradeable) |
| Schema evolution | Easy | Free | Hard | Medium | Free | **Easy** |
| Per-provider attribution | No | Yes | Yes | Yes | Yes | **Yes** |
| Multi-provider aggregation | Built-in | Manual | Manual | Manual | Manual | **Built-in** |
| Table count | 1 | 1 | N | 1 | 1 | **1** |
| New provider = central changes? | If new MIR field | No | Controller method | No | No | **If new MIR field** |

---

## Recommendation

**Option F (Broad Typed Model + Per-Provider Rows)** best satisfies all constraints:

1. **Full decoupling** -- the mixer imports only `AudioAnalysisData` from models, never references any provider
2. **Full type safety** -- all analysis properties are typed fields on a dataclass
3. **Proven pattern** -- identical to how `MediaItemMetadata` works (a bounded vocabulary of typed optional fields, multiple providers contribute, consumers get a merged view)
4. **MIR domain is well-bounded** -- BPM, beats, key, time signature, mood, energy, structure cover the standard feature set. This won't grow unbounded like arbitrary plugin data would
5. **Per-provider rows** allow attribution and selective querying
6. **Aggregation API** gives consumers a clean merged view
7. **SQL queryability upgradeable** -- add indexed columns later if needed (single ALTER TABLE)

### Merge strategy decision
Start with simple **latest-write-wins** for the aggregation API. When a second AA provider actually exists, refine the merge logic (e.g., confidence-based selection). No need to over-design this now.
