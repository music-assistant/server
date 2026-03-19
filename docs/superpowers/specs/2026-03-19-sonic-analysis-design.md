# Sonic Analysis Plugin — Design Spec

## Summary

Add a plugin provider to Music Assistant that extracts sonic signatures (audio feature vectors) from tracks using librosa, indexes them with Voyager (Spotify's ANN library) for fast similarity search, and exposes a "similar tracks" API endpoint. This lays the foundation for future dynamic playlists, auto-DJ, and mood-based features.

## Goals

- Extract a 38-dimension sonic signature from any track in the library
- Store signatures persistently and index them for sub-millisecond similarity queries at 500K-1M track scale
- Support both local providers (background batch analysis) and streaming providers (analyze on play)
- Expose a REST API for querying similar tracks
- Keep the feature cleanly optional as a plugin provider

## Non-Goals (for this iteration)

- Dynamic playlist generation UI
- Auto-DJ / queue integration
- Mood tagging or genre classification from signatures
- Frontend UI for similarity browsing

## Architecture

### Approach: Plugin Provider + Shared Helper

Two components:

1. **Helper module** (`music_assistant/helpers/sonic_analysis.py`) — pure analysis engine with no MA-specific dependencies beyond numpy/librosa. Reusable by any future provider or controller.
2. **Plugin provider** (`music_assistant/providers/sonic_analysis/`) — orchestrates when analysis runs, manages storage, hosts the Voyager index, and registers the API endpoint.

### Dependencies

| Library | Purpose | License |
|---------|---------|---------|
| librosa (existing) | Audio feature extraction | ISC |
| voyager | ANN similarity index | Apache 2.0 |
| numpy (existing) | Array operations, normalization | BSD |

## Helper Module — `music_assistant/helpers/sonic_analysis.py`

### Feature Extraction

Takes raw audio as a numpy array + sample rate. Extracts a 38-dimension signature:

| Feature Group | librosa Function | Dimensions | What It Captures |
|---|---|---|---|
| MFCCs | `librosa.feature.mfcc(n_mfcc=13)` | 13 | Timbral texture (brightness, warmth) |
| Chroma | `librosa.feature.chroma_stft()` | 12 | Tonal/harmonic content (key, chord feel) |
| Spectral contrast | `librosa.feature.spectral_contrast(n_bands=6)` | 7 | Peaks vs valleys in spectrum per band |
| Tempo | `librosa.beat.beat_track()` | 1 | BPM |
| Spectral centroid | `librosa.feature.spectral_centroid()` | 1 | Brightness |
| Spectral rolloff | `librosa.feature.spectral_rolloff()` | 1 | High-frequency energy dropoff |
| Spectral flatness | `librosa.feature.spectral_flatness()` | 1 | Noise vs tonal (percussive vs melodic) |
| RMS energy | `librosa.feature.rms()` | 1 | Loudness/dynamics (mean) |
| ZCR | `librosa.feature.zero_crossing_rate()` | 1 | Percussiveness |

All time-series features (all except tempo) are collapsed to a single representative value by taking the mean across time frames. This produces a compact, storable fingerprint that captures overall "vibe." The `n_bands=6` parameter for spectral contrast is pinned explicitly to guarantee 7 output dimensions regardless of librosa version defaults.

All analysis uses 22050 Hz mono — the MIR standard. Higher sample rates add computation cost without improving feature quality.

### Data Model

```python
@dataclass
class SonicSignature:
    features: list[float]       # 38 floats
    version: int                # signature version for migration
    feature_names: list[str]    # ordered feature labels
```

The `version` field enables re-analysis if the feature set changes in future iterations.

### Feature Normalization

Similarity computation requires **per-feature z-score normalization across the corpus** so that features with large ranges (tempo: 60-200 BPM) don't dominate over features with small ranges (spectral flatness: 0-1).

**Corpus statistics management:**
- The plugin maintains a `normalization_stats` record: per-feature mean and std computed over all stored signatures
- Statistics are **recomputed** whenever a batch analysis completes (on initial backfill, and periodically as the corpus grows)
- Stored in the `sonic_signatures` DB table as a special row (`item_id = "__corpus_stats__"`) containing JSON with `{"means": [...], "stds": [...]}`
- On plugin load, corpus stats are loaded into memory for fast access
- The Voyager index stores **normalized** vectors (after z-score transform). When corpus stats are recomputed, the index is rebuilt from the DB table with the updated normalization — this is infrequent (only after significant corpus growth) and takes seconds even at 1M scale
- For single-track analysis (on-play), the new vector is normalized using the current corpus stats before insertion into the index. This is approximate but acceptable — the stats drift slowly as the corpus grows

### Similarity Computation

- `compute_distance(sig_a, sig_b) -> float` — cosine distance on z-score normalized vectors
- `normalize_features(raw_features, corpus_means, corpus_stds) -> list[float]` — applies per-feature z-score normalization

## Plugin Provider — `music_assistant/providers/sonic_analysis/`

### File Structure

```
music_assistant/providers/sonic_analysis/
├── __init__.py      # SonicAnalysisProvider class
├── manifest.json    # Provider metadata + config schema
```

### Configuration (manifest.json)

| Config Entry | Type | Default | Purpose |
|---|---|---|---|
| `enabled` | bool | true | Toggle the plugin |
| `analyze_on_play` | bool | true | Analyze streaming tracks on playback |
| `analyze_on_sync` | bool | true | Analyze local tracks on library sync |
| `max_concurrent_analyses` | int | 2 | Limit CPU load during batch processing |

### Event Subscriptions

- `EventType.MEDIA_ITEM_ADDED` — local/NFS tracks: queue for background analysis
- `EventType.MEDIA_ITEM_PLAYED` — streaming tracks: analyze if no signature exists yet

Unsubscribe callables returned by `mass.subscribe()` are stored as instance attributes and called in `unload()` to prevent dangling event handlers.

### Analysis Pipeline

**For local/NFS providers (sync-triggered):**
1. Get the track's `StreamDetails` via `mass.music.get_provider_item()`
2. Use `librosa.load(file_path, sr=22050, mono=True)` directly
3. Run `extract_signature()` on the result
4. All in `asyncio.to_thread()` since librosa is blocking

**For streaming providers (play-triggered):**
1. On `MEDIA_ITEM_PLAYED` event, check if signature already exists — skip if so
2. Re-fetch `StreamDetails` for the track via the music provider
3. Initiate a new audio stream via MA's `get_audio_stream()` to get PCM bytes
4. Convert PCM byte stream to numpy array
5. Run `extract_signature()` in `asyncio.to_thread()`
6. Store result

Note: This re-streams the track from the provider, which counts as an additional stream request. For paid streaming services, this is acceptable because: (a) analysis only happens once per track (results are cached), (b) the re-stream happens after playback completes so it doesn't interfere with the user experience, and (c) most streaming APIs don't count re-fetches against rate limits differently than playback. An alternative approach — tapping into the active playback pipeline like the smart_fades analyzer — could avoid the re-stream but would add complexity to the streams controller; this can be optimized in a future iteration if re-streaming proves problematic.

**Error handling:** If analysis fails (corrupt file, network timeout), log a warning and skip. Failed tracks can be retried on next play or sync cycle. Analysis never blocks playback or sync.

### Background Batch Processing

On `loaded_in_mass()`, schedule a background task that:
1. Queries for all local-provider tracks without signatures
2. Iterates through them, analyzing each
3. Respects `max_concurrent_analyses` via `asyncio.Semaphore`
4. Yields between tracks to avoid starving other MA tasks

### Database Storage

**Table: `sonic_signatures`**

| Column | Type | Purpose |
|---|---|---|
| `item_id` | TEXT | Track's library item ID |
| `provider` | TEXT | Source provider instance |
| `features` | TEXT | JSON-encoded float array (38 values) |
| `version` | INTEGER | Signature version for migration |
| `timestamp` | REAL | When analysis was performed |

Indexed on `(item_id, provider)` — same pattern as existing `loudness_measurements` and `smart_fades_analysis` tables.

**Table creation:** The plugin creates the table in `handle_async_init()` via `mass.music.database.execute(CREATE TABLE IF NOT EXISTS ...)`. This follows the pattern used by existing analysis tables but is owned by the plugin rather than the music controller, since the table only exists when the plugin is enabled.

The DB table is the **source of truth**. The Voyager index is a derived acceleration structure that can be rebuilt from the DB at any time.

### Voyager Similarity Index

- `E4M3Index` (8-bit quantized) — 38 dimensions, cosine distance space
- Stored at `Path(mass.storage_path) / "sonic_signatures.voy"`
- ~60-80 MB memory for 1M vectors
- Sub-millisecond queries

**Index lifecycle:**
- **On plugin load:** Load existing index from disk, or create new empty one
- **On track analyzed:** `index.add_items([vector], ids=[numeric_item_id])` — uses the batch API with explicit ID to map Voyager labels directly to MA library item IDs
- **On similarity query:** `index.query(target_vector, k=limit)` → returns item IDs + distances
- **On plugin unload:** Save index to disk
- **Persistence:** Saved to disk after each batch of insertions (not after every single track)
- **Recovery:** If the Voyager index file is corrupted or missing, rebuild from the DB table

### API Endpoint

**Route:** `GET /api/sonic_analysis/similar?item_id={item_id}&limit=25`

Registered via `mass.webserver.register_dynamic_route()` with a flat path (`/api/sonic_analysis/similar`). The `item_id` and `limit` are passed as query parameters parsed from `request.query`, avoiding path parameter routing which MA's webserver does not support.

**Response:**
```json
{
  "items": [
    {
      "track": { "/* standard Track model */" : "..." },
      "distance": 0.042
    }
  ],
  "seed_track_id": "123",
  "analyzed": true
}
```

**Behavior:**
- Seed track has signature → query Voyager, resolve item IDs to Track objects, return ranked by distance
- Seed track has no signature → return `{"analyzed": false, "items": []}` (200 status, not an error)
- Missing or invalid `item_id` → 400 Bad Request
- `limit` capped at 100, defaults to 25
- Endpoint registered on plugin load, unregistered on unload
- Uses MA's existing webserver authentication

## Testing Strategy

### Unit Tests (`tests/providers/test_sonic_analysis.py`)

- `extract_signature()` with synthetic audio (numpy sine wave) — verify 38 floats, version field, feature names
- `compute_distance()` — identical signatures return 0, known different signatures return expected distance
- Z-score normalization — verify centering and scaling

### Integration Tests

- Plugin lifecycle: load → analyze test track → store → query similar → unload
- Voyager index: add items, query, save/load persistence, rebuild from DB
- API endpoint: mock signatures in DB, verify `/api/tracks/{id}/similar` returns ranked results
- "No signature yet" case returns `{"analyzed": false}`

### Test Fixtures

- Short synthetic WAV file (1-2 seconds) in `tests/fixtures/` — no real music files in repo
- Pre-computed signature fixtures for deterministic distance tests

### What We Don't Test

- librosa's correctness (upstream responsibility)
- Voyager's search accuracy (upstream responsibility)
- We test our glue: pipeline produces a signature, stores it, indexes it, returns ranked results

## Future Extensions (not in scope)

These become straightforward once the sonic signature foundation exists:

- **Dynamic playlists** — "play something similar" queue generation
- **Multi-seed playlists** — average multiple seed track signatures for a blended "mood"
- **Auto-DJ transitions** — use tempo + energy features for smooth crossfades
- **Mood/energy tagging** — cluster signatures to auto-label tracks
- **Cross-provider discovery** — "this Tidal track sounds like these local files"
