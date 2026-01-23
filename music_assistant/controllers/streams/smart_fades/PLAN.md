# Streaming Audio Feature Extraction for Smart Fades

## Summary

Enable whole-song audio analysis for smart fades without holding entire PCM audio in memory. Extract features (beats, BPM, key, phrases) incrementally as audio streams, then run final analysis when track completes.

**Key insight**: Librosa's `beat_track()` cannot process streaming audio directly, BUT it can accept a pre-computed onset envelope. We can compute onset envelopes (and chroma features) frame-by-frame as audio streams, accumulating only lightweight feature data (~500KB for a 10-min song vs ~210MB for raw PCM).

## Current State

- Smart fades analyzes only **45 seconds** of INTRO/OUTRO audio ([fades.py:31](fades.py#L31))
- Analysis uses `librosa.beat.beat_track(y=audio_array, ...)` requiring complete audio
- PCM buffer at 48kHz stereo float32 = ~17MB per 45s fragment
- Results cached in SQLite via `MusicController.set_smart_fades_analysis()`

## Proposed Architecture

```
Stream Controller → PCM chunks → StreamingFeatureExtractor
                                         ↓
                              FeatureAccumulator (lightweight)
                              • onset_envelope (~40KB/min)
                              • chroma_frames (~120KB/min)
                                         ↓
                              On track complete:
                              ExtendedAnalysisProcessor
                              • beat_track(onset_envelope=...)
                              • key estimation from chroma
                              • phrase segmentation
                                         ↓
                              ExtendedSmartFadesAnalysis → Database
```

## Librosa Return Types (for data model alignment)

| Function | Return Type | Shape | Dtype | Units |
|----------|-------------|-------|-------|-------|
| `beat_track()` | `Tuple[ndarray, ndarray]` | tempo: `(1,)`, beats: `(n_beats,)` | `float64`, `int64` | BPM, frame indices |
| `onset_strength()` | `ndarray` | `(n_frames,)` | `float32` | spectral flux magnitude |
| `chroma_stft()` | `ndarray` | `(12, n_frames)` | `float32` | normalized energy [0,1] |
| `rms()` | `ndarray` | `(1, n_frames)` | `float32` | root-mean-square energy |
| `spectral_centroid()` | `ndarray` | `(1, n_frames)` | `float32` | center of mass of spectrum (Hz) |

**Notes**:
- Librosa has NO built-in key estimation. We implement using Krumhansl-Schmuckler profiles correlated with averaged chroma.
- RMS and spectral centroid enable energy/brightness matching for smoother crossfades.

## Files to Create

| File | Purpose |
|------|---------|
| `streaming_extractor.py` | Processes PCM chunks, extracts features incrementally |
| `feature_accumulator.py` | Thread-safe storage for accumulated features |
| `extended_analysis.py` | Final analysis (beat tracking, key, phrases) on accumulated data |

## Files to Modify

| File | Changes |
|------|---------|
| `music_assistant/models/smart_fades.py` | Add `ExtendedSmartFadesAnalysis`, `MusicalKey`, `PhraseBoundary` models; add `FULL_SONG = 3` fragment type |
| `music_assistant/controllers/streams/streams_controller.py` | Hook feature extraction into `get_queue_item_stream()` (~line 1318) |
| `music_assistant/controllers/music.py` | Add migration, `set_extended_smart_fades_analysis()`, `get_extended_smart_fades_analysis()` |

## Implementation Steps

### Phase 1: Data Models

1. **Extend `models/smart_fades.py`**
   - Add `FULL_SONG = 3` to `SmartFadesAnalysisFragment` enum
   - Add `MusicalKey` dataclass:
     ```python
     @dataclass
     class MusicalKey(DataClassDictMixin):
         root: str           # "C", "C#", "D", etc. (from chroma bin index)
         mode: str           # "major" or "minor"
         confidence: float   # correlation coefficient [0, 1]
     ```
   - Add `PhraseBoundary` dataclass:
     ```python
     @dataclass
     class PhraseBoundary(DataClassDictMixin):
         time: float         # position in seconds (float64 from librosa)
         confidence: float   # detection confidence [0, 1]
         boundary_type: str  # "phrase", "section"
     ```
   - Add `ExtendedSmartFadesAnalysis` dataclass:
     ```python
     @dataclass
     class ExtendedSmartFadesAnalysis(DataClassDictMixin):
         # Core fields (aligned with librosa beat_track output)
         bpm: float                              # from tempo array, converted to float
         beats: npt.NDArray[np.float64]          # beat times in seconds
         downbeats: npt.NDArray[np.float64]      # downbeat times in seconds
         confidence: float                       # beat interval consistency
         duration: float                         # total track duration

         # Extended fields
         musical_key: MusicalKey | None = None
         phrase_boundaries: list[PhraseBoundary] = field(default_factory=list)

         # Per-second energy/brightness curves (downsampled from per-frame librosa output)
         # Per-second is sufficient for crossfade timing decisions (~10KB for 10-min song)
         energy_curve: npt.NDArray[np.float32] = None       # RMS energy per second
         spectral_centroid_curve: npt.NDArray[np.float32] = None  # Spectral centroid (Hz) per second

         full_song_analysis: bool = False
         analysis_version: int = 2

         # Storage identifiers
         item_id: str = ""
         provider: str = ""
     ```
   - Add `to_fragment_analysis()` method for backward compatibility

### Phase 2: Feature Accumulation

2. **Create `feature_accumulator.py`**
   - Thread-safe class storing:
     - `onset_envelope: list[NDArray[np.float32]]` - chunks of onset strength
     - `chroma_frames: list[NDArray[np.float32]]` - chunks of chroma (12, n_frames)
     - `rms_frames: list[NDArray[np.float32]]` - chunks of RMS energy (1, n_frames)
     - `spectral_centroid_frames: list[NDArray[np.float32]]` - chunks of spectral centroid (1, n_frames)
   - Methods: `add_onset_strength()`, `add_chroma()`, `add_rms()`, `add_spectral_centroid()`, getters, `clear()`
   - Memory monitoring with `get_memory_usage_bytes()`

3. **Create `streaming_extractor.py`**
   - `StreamingFeatureExtractor` class with cancellation support
   - Constructor params:
     - `streams: StreamsController`
     - `sample_rate: int`
     - `hop_length: int = 512`
     - `n_fft: int = 2048`
   - `start_extraction(track_id, provider_id, queue_id, session_id)` - initialize for new track, store session context
   - `process_chunk(chunk, pcm_format)` - extract features (runs in thread pool via `asyncio.to_thread`)
   - `should_abort()` - check if extraction should stop (see Phase 5)
   - `finalize_analysis()` - complete analysis when track ends
   - Key implementation details:
     - Use `librosa.onset.onset_strength(..., center=False)` for streaming compatibility
     - Use `librosa.feature.chroma_stft(..., center=False)` for streaming compatibility
     - Use `librosa.feature.rms(..., center=False)` for energy analysis
     - Use `librosa.feature.spectral_centroid(..., center=False)` for brightness analysis
     - Maintain overlap buffer between chunks for STFT continuity (`n_fft - hop_length` samples)

### Phase 3: Extended Analysis

4. **Create `extended_analysis.py`**
   - `ExtendedAnalysisProcessor` class
   - `analyze(accumulator, sample_rate, hop_length, duration)` method (runs in thread pool)
   - Beat tracking: `librosa.beat.beat_track(onset_envelope=accumulated_envelope, sr=sr, hop_length=hop_length)`
     - Returns `(tempo_array, beat_frames)` - convert frames to times with `librosa.frames_to_time()`
   - Key estimation using Krumhansl-Schmuckler profiles:
     - Average chroma across time: `chroma_avg = np.mean(chroma, axis=1)` → shape `(12,)`
     - Correlate with major/minor key profiles for all 12 roots
     - Return best match with correlation as confidence
   - Phrase detection: onset density changes at downbeat positions
   - Energy/brightness curves:
     - Downsample per-frame RMS to per-second by averaging frames within each second
     - Downsample per-frame spectral centroid similarly
     - Store as `NDArray[float32]` with length = track duration in seconds

### Phase 4: Database Migration and Integration

5. **Database Schema Migration**

   The existing `smart_fades_analysis` table schema (from `music.py:2426-2439`):
   ```sql
   CREATE TABLE IF NOT EXISTS smart_fades_analysis(
       [id] INTEGER PRIMARY KEY AUTOINCREMENT,
       [item_id] TEXT NOT NULL,
       [provider] TEXT NOT NULL,
       [fragment] INTEGER NOT NULL,        -- 1=INTRO, 2=OUTRO, 3=FULL_SONG (new)
       [bpm] REAL NOT NULL,
       [beats] TEXT NOT NULL,              -- JSON array of beat times
       [downbeats] TEXT NOT NULL,          -- JSON array of downbeat times
       [confidence] REAL NOT NULL,
       [duration] REAL,
       [analysis_version] INTEGER DEFAULT 1,  -- Already exists!
       [timestamp_created] INTEGER DEFAULT (cast(strftime('%s','now') as int)),
       UNIQUE(item_id,provider,fragment)
   );
   ```

   **New columns to add via migration:**
   ```sql
   ALTER TABLE smart_fades_analysis ADD COLUMN musical_key TEXT;           -- JSON: {"root":"C","mode":"major","confidence":0.85}
   ALTER TABLE smart_fades_analysis ADD COLUMN phrase_boundaries TEXT;     -- JSON array: [{"time":30.5,"confidence":0.9,"boundary_type":"section"},...]
   ALTER TABLE smart_fades_analysis ADD COLUMN energy_curve TEXT;          -- JSON array of per-second RMS values
   ALTER TABLE smart_fades_analysis ADD COLUMN spectral_centroid_curve TEXT; -- JSON array of per-second centroid values (Hz)
   ALTER TABLE smart_fades_analysis ADD COLUMN full_song_analysis INTEGER DEFAULT 0;  -- Boolean flag
   ```

   **Version semantics:**
   - `analysis_version = 1`: Legacy format (INTRO/OUTRO fragments, no key/phrases)
   - `analysis_version = 2`: Extended format (FULL_SONG fragment, includes key/phrases)

6. **Update `controllers/music.py`**

   **Add migration in `__migrate_database()` method** (follow existing pattern ~line 2095):
   ```python
   # Add after existing migrations, with appropriate version check
   if prev_version <= XX:  # Use next version number
       # Add extended smart fades analysis columns
       for col_def in [
           "musical_key TEXT",
           "phrase_boundaries TEXT",
           "energy_curve TEXT",
           "spectral_centroid_curve TEXT",
           "full_song_analysis INTEGER DEFAULT 0",
       ]:
           try:
               await self._database.execute(
                   f"ALTER TABLE {DB_TABLE_SMART_FADES_ANALYSIS} ADD COLUMN {col_def}"
               )
           except Exception as err:
               if "duplicate column" not in str(err):
                   raise
   ```

   **Implement `set_extended_smart_fades_analysis()`:**
   - Serialize `musical_key` to JSON (or None)
   - Serialize `phrase_boundaries` list to JSON array
   - Set `analysis_version = 2`
   - Set `full_song_analysis = 1`
   - Set `fragment = SmartFadesAnalysisFragment.FULL_SONG` (3)
   - Use `insert_or_replace` on unique constraint `(item_id, provider, fragment)`

   **Implement `get_extended_smart_fades_analysis()`:**
   - Query by `(item_id, provider, fragment=FULL_SONG)`
   - Check `analysis_version` to determine deserialization:
     - Version 1: Return `None` (legacy data, not extended)
     - Version 2: Deserialize `musical_key` and `phrase_boundaries` from JSON
   - Return `ExtendedSmartFadesAnalysis` object

   **Update existing `get_smart_fades_analysis()` (optional enhancement):**
   - If `analysis_version >= 2` and querying for INTRO/OUTRO, can derive from FULL_SONG data
   - Extract relevant portion of beats/downbeats for the fragment
   - This allows FULL_SONG analysis to serve fragment requests

### Phase 5: Stream Integration with Seek/Skip Detection

7. **Update `controllers/streams/streams_controller.py`**

   In `get_queue_item_stream()` (~line 1318):
   - **Eligibility checks** (only extract when ALL conditions met):
     - `seek_position == 0` (not seeking into track)
     - No existing full-song analysis in cache
     - Track is `MediaType.TRACK`
   - Create `StreamingFeatureExtractor` instance with queue context:
     ```python
     extractor.start_extraction(
         track_id=streamdetails.item_id,
         provider_id=streamdetails.provider,
         queue_id=queue.queue_id,
         session_id=queue.session_id,  # Store for abort detection
     )
     ```
   - For each yielded chunk, call `extractor.process_chunk()` via `create_task()` (non-blocking)

   **Abort detection** (in `StreamingFeatureExtractor.should_abort()`):
   - Check if `queue.session_id != stored_session_id` (user skipped/seeked)
   - Check if `queue.current_item.queue_item_id != stored_queue_item_id` (track changed)
   - If abort detected: set `_aborted = True`, skip remaining processing

   **On stream completion**:
   - If `extractor._aborted`: discard partial data, fall back to existing fragment analysis
   - If completed normally: call `_finalize_feature_extraction()` background task

   Add `_finalize_feature_extraction()` method to complete and store analysis

   Add concurrency control with semaphore (max 3 concurrent extractions)

### Phase 6: Fallback Behavior

8. **Graceful degradation logic**
   - When seeking (`seek_position > 0`): skip streaming extraction entirely
   - When skipped mid-track: abort extraction, existing fragment analysis still works
   - When extraction fails: log warning, crossfade uses fragment analysis as before
   - No changes needed to mixer - it already falls back to fragment analysis

## Memory Budget

| Data | Current (45s fragments) | Proposed (full song) |
|------|------------------------|----------------------|
| Raw PCM buffer | ~17MB per fragment | 0 (streaming) |
| Onset envelope | N/A | ~40KB/min (~4 bytes × ~170 frames/sec) |
| Chroma features | N/A | ~120KB/min (~48 bytes × ~170 frames/sec) |
| RMS energy | N/A | ~40KB/min (same as onset) |
| Spectral centroid | N/A | ~80KB/min (~8 bytes × ~170 frames/sec, float64) |
| **Total for 10-min song** | ~34MB (2 fragments) | ~2.8MB features in memory |
| **Stored (per-second curves)** | N/A | ~10KB (energy + centroid downsampled) |

## Error Handling

| Scenario | Handling |
|----------|----------|
| Track very short (<5s) | Analysis may have low confidence, but still attempted |
| Seek position > 0 | Skip extraction entirely |
| User skips mid-track | Abort extraction (detected via session_id change), discard partial data |
| Stream interrupted | Catch `GeneratorExit`/`CancelledError`, discard partial data |
| librosa exception | Catch in thread, return None, log warning |
| Memory limit (>5MB) | Stop accumulating, finalize with partial data |

## Backward Compatibility

- Existing `SmartFadesAnalysis` model unchanged
- `ExtendedSmartFadesAnalysis.to_fragment_analysis()` converts to legacy format
- Database migration adds nullable columns (old data still works)
- `analysis_version` field determines which model to deserialize to:
  - Version 1 → `SmartFadesAnalysis` (legacy)
  - Version 2 → `ExtendedSmartFadesAnalysis` (extended)
- Mixer unchanged - already falls back to fragment analysis when full-song unavailable

## Verification

1. **Unit tests**
   - Test `FeatureAccumulator` thread safety
   - Test `StreamingFeatureExtractor` with sample audio chunks
   - Test abort detection with mock queue state changes
   - Test key estimation accuracy with known songs
   - Test database migration (add columns to existing table)
   - Test deserialization based on `analysis_version`

2. **Integration tests**
   - Stream a full track and verify analysis is stored with `analysis_version=2`
   - Verify extraction skipped when `seek_position > 0`
   - Verify extraction aborted when user skips mid-track
   - Verify graceful handling of stream interruption
   - Verify legacy data (version 1) still loads correctly after migration

3. **Manual testing**
   - Play several tracks from start to finish
   - Verify extended analysis appears in database with `fragment=3` (FULL_SONG)
   - Skip a track mid-playback, verify no partial/corrupt analysis stored
   - Seek into a track, verify extraction is skipped
   - Check database has new columns after upgrade

4. **Run pre-commit**
   ```bash
   pre-commit run --all-files
   ```

5. **Run tests**
   ```bash
   pytest tests/controllers/streams/smart_fades/
   ```
