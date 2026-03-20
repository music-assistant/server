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

Before comparison, features are z-score normalized across the corpus so that high-range features (tempo: 60-200 BPM) don't dominate over low-range features (spectral flatness: 0-1). Similarity is measured using cosine distance on the normalized vectors.

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
- Generate playlists from similar tracks
- Trigger backfill, rebuild index, or clear all data
