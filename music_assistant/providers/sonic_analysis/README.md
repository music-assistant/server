# Sonic Analysis Plugin

An experimental Music Assistant plugin that extracts audio fingerprints from your music library and uses them to find sonically similar tracks.

## How It Works

### Feature Extraction

When enabled, the plugin analyzes each track in your local library using [librosa](https://librosa.org/) to extract a **38-dimension sonic signature** — a compact numerical fingerprint that captures the overall "feel" of a song. The signature includes:

| Feature | Dimensions | What It Captures |
|---------|-----------|-----------------|
| MFCCs | 13 | Timbral texture — brightness, warmth, tone color |
| Chroma | 12 | Harmonic content — key, chord feel, tonal character |
| Spectral Contrast | 7 | Dynamic range per frequency band — peaks vs valleys |
| Tempo | 1 | BPM |
| Spectral Centroid | 1 | Overall brightness |
| Spectral Rolloff | 1 | High-frequency energy distribution |
| Spectral Flatness | 1 | Noise vs tonal balance (percussive vs melodic) |
| RMS Energy | 1 | Loudness / dynamics |
| Zero Crossing Rate | 1 | Percussiveness |

All time-varying features are collapsed to their mean across the track, producing a fixed-size vector that represents the song's overall sonic character.

### Similarity Search

Signatures are stored in SQLite and indexed using [USearch](https://github.com/unum-cloud/usearch), a fast approximate nearest neighbor (ANN) library based on the HNSW algorithm. This enables sub-millisecond similarity queries even at 500K+ tracks.

Before comparison, features are z-score normalized across the corpus so that high-range features (tempo: 60-200 BPM) don't dominate over low-range features (spectral flatness: 0-1).

### Feature Group Weighting

The 38 features are organized into five tunable groups. Each group's influence on the similarity score can be adjusted at query time — no re-indexing required:

| Group | Features | What It Controls |
|-------|----------|-----------------|
| **Timbre** | MFCCs 1-13 | Tone color, warmth, brightness |
| **Harmony** | Chroma 1-12 | Key, chord feel, tonal character |
| **Texture** | Spectral contrast 1-7 | Frequency band dynamics, peaks vs valleys |
| **Rhythm** | Tempo + spectral centroid | BPM and rhythmic feel |
| **Energy** | Rolloff, flatness, RMS, ZCR | Loudness, percussiveness, dynamics |

Two additional metadata-based weights are available:

- **Genre** (0-100%): Boosts tracks sharing genre tags with the seed (Jaccard similarity).
- **Year** (0-100%): Boosts tracks from a similar release year/decade (linear decay over 30 years).

### Presets

Named presets configure all weights at once for common use cases:

| Preset | Timbre | Harmony | Texture | Rhythm | Energy | Genre | Year | Use Case |
|--------|--------|---------|---------|--------|--------|-------|------|----------|
| `balanced` | 100% | 100% | 100% | 100% | 100% | 0% | 0% | Default — pure sonic, all groups equal |
| `vibe` | 80% | 50% | 60% | 30% | 100% | 0% | 0% | Mood matching — tone + energy, less rhythm |
| `party` | 30% | 20% | 30% | 100% | 80% | 0% | 0% | DJ mixing — tempo/energy focused |
| `genre_era` | 50% | 50% | 50% | 50% | 50% | 80% | 60% | Stay in genre + decade |
| `discover` | 100% | 80% | 70% | 50% | 70% | 0% | 0% | Cross-genre exploration |

Individual weights can override preset values in the same query.

### Analysis Triggers

- **On library sync**: Local/NFS tracks are analyzed in the background when added to the library
- **On plugin load**: A backfill task processes any unanalyzed tracks in the existing library

Streaming provider tracks (Tidal, Spotify, etc.) are not analyzed in this version.

## API Endpoints

All endpoints are served from the MA webserver when the plugin is enabled.

| Endpoint | Description |
|----------|-------------|
| `GET /api/sonic_analysis/status` | Plugin stats: DB count, index size, config |
| `GET /api/sonic_analysis/similar?item_id=X&limit=25` | Find similar tracks |
| `GET /api/sonic_analysis/signatures?limit=50&offset=0` | Browse stored signatures |
| `GET /api/sonic_analysis/make_playlist?item_id=X` | Create a playlist from similar tracks (2 tiers deep) |
| `GET /api/sonic_analysis/trigger_backfill` | Manually start library analysis |
| `GET /api/sonic_analysis/rebuild_index` | Rebuild the ANN index from stored signatures |
| `GET /api/sonic_analysis/clear_all` | Delete all signatures and reset the index |
| `GET /api/sonic_analysis/debug` | Built-in debug console UI |

### Similarity query parameters

The `/similar` and `/make_playlist` endpoints accept these optional parameters:

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `preset` | string | `balanced` | Named preset (balanced, vibe, party, genre_era, discover) |
| `timbre` | float | — | Override timbre weight (0.0-1.0) |
| `harmony` | float | — | Override harmony weight (0.0-1.0) |
| `texture` | float | — | Override texture weight (0.0-1.0) |
| `rhythm` | float | — | Override rhythm weight (0.0-1.0) |
| `energy` | float | — | Override energy weight (0.0-1.0) |
| `genre_weight` | float | — | Override genre weight (0.0-1.0) |
| `year_weight` | float | — | Override year weight (0.0-1.0) |
| `candidates` | int | 50 | Number of ANN candidates to fetch before re-ranking (max 500) |
| `limit` | int | 25 | Max results to return (max 100) |

Example: find tracks with similar tempo and energy, staying in genre:
```
/api/sonic_analysis/similar?item_id=8481&preset=party&genre_weight=0.5&candidates=100
```

## Dependencies

| Library | Purpose | License |
|---------|---------|---------|
| [librosa](https://librosa.org/) | Audio feature extraction (already a MA dependency) | ISC |
| [USearch](https://github.com/unum-cloud/usearch) | HNSW approximate nearest neighbor index | Apache 2.0 |
| [NumPy](https://numpy.org/) | Array operations (already a MA dependency) | BSD |

## Debug Console

Navigate to `http://<your-ma-server>:8095/api/sonic_analysis/debug` for a built-in web UI that lets you:

- View index status and signature counts
- Browse stored signatures
- Search for similar tracks by item ID
- Select presets or adjust individual feature group sliders in real time
- Set the candidate pool size for re-ranking precision
- Generate playlists from similar tracks ("Songs like [track name]")
- Trigger backfill, rebuild index, or clear all data
