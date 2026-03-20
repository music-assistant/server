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

### Genre & Year Weighting

By default, similarity is purely sonic — two tracks that *sound* alike will match regardless of their tagged genre or release year. This can surface surprising cross-genre connections but may also produce results that feel mismatched.

To refine results, two optional weights can be applied at query time (no re-indexing required):

- **Genre weight** (0-100%): Boosts tracks that share genre tags with the seed. Uses Jaccard similarity (proportion of shared genres) as the bonus signal.
- **Year weight** (0-100%): Boosts tracks from a similar release year/decade. Decays linearly — same year = full bonus, 30+ years apart = no bonus.

The weights re-rank the top candidates from the ANN index by blending the sonic distance with metadata bonuses. At 0% for both, results are identical to pure sonic similarity.

### Analysis Triggers

- **On library sync**: Local/NFS tracks are analyzed in the background when added to the library
- **On plugin load**: A backfill task processes any unanalyzed tracks in the existing library

Streaming provider tracks (Tidal, Spotify, etc.) are not analyzed in this version.

## API Endpoints

All endpoints are served from the MA webserver when the plugin is enabled.

| Endpoint | Description |
|----------|-------------|
| `GET /api/sonic_analysis/status` | Plugin stats: DB count, index size, config |
| `GET /api/sonic_analysis/similar?item_id=X&limit=25&genre_weight=0.5&year_weight=0.3` | Find similar tracks (weights optional, 0-1) |
| `GET /api/sonic_analysis/signatures?limit=50&offset=0` | Browse stored signatures |
| `GET /api/sonic_analysis/make_playlist?item_id=X&genre_weight=0.5&year_weight=0.3` | Create a playlist from similar tracks (2 tiers deep, weights optional) |
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
- Adjust genre and year weight sliders to tune metadata influence in real time
- Generate playlists from similar tracks ("Songs like [track name]")
- Trigger backfill, rebuild index, or clear all data
