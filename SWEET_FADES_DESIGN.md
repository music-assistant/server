# Sweet Fades Feature Design Document

## Overview
Implementation of DJ-like intelligent crossfading similar to Apple's AutoMix feature for Music Assistant.

**Key Features:**
- BPM matching with gradual tempo adjustment
- Low/high-pass filtering during transitions
- Smart intro/outro detection for dynamic crossfade timing
- Real-time and ad-hoc analysis modes

## Analysis Results

### pyCrossfade Library Analysis
- Uses Madmom for beat tracking and downbeat detection
- Implements gradual time-stretching to match BPMs
- Beat-matching at bar level to handle musical "humanization"
- Supports both linear tempo adjustment and crossfade EQ

### Music Assistant Integration Points

#### Audio Streaming Pipeline (`streams.py`)
- **Crossfade infrastructure**: Lines 104-111 (CrossfadeData), 857-878 (flow mode), 1129-1152 (single mode)
- **Buffer management**: Lines 1080-1082 (`track_loaded_in_buffer` hook point)
- **Stream completion**: Lines 1188-1199 (analysis trigger point)
- **Existing crossfade function**: `crossfade_pcm_parts` (audio.py)

#### Player Queue Management (`player_queues.py`)
- **Next track preloading**: `preload_next_queue_item` method
- **Track buffer loading**: `track_loaded_in_buffer` method
- **Next item detection**: `get_next_item` method

#### Audio Analysis Infrastructure (`audio.py`)
- **Loudness analysis**: Existing pattern for async audio analysis
- **FFmpeg integration**: Stream processing pipeline
- **Caching system**: Audio cache and analysis storage

## Architecture Decision

**Core Feature Extension (Not Plugin)**
- Requires deep audio pipeline integration
- Performance-critical real-time processing
- Needs access to crossfade infrastructure
- Must hook into buffer preloading mechanisms

## Technical Design

### 1. Audio Analysis Component
```python
# New: music_assistant/helpers/audio_analysis.py
class SweetFadesAnalyzer:
    async def analyze_track_realtime(self, pcm_stream, audio_format) -> SweetFadesAnalysis
    async def analyze_track_offline(self, streamdetails) -> SweetFadesAnalysis
```

**Dependencies:**
- `madmom` - Beat/downbeat tracking, tempo estimation
- `essentia` - Audio feature extraction, intro/outro detection
- `soundfile`/`librosa` - Audio I/O utilities

**Analysis Pipeline:**
1. Beat Detection (Madmom RNNBeatProcessor)
2. Tempo Estimation (BPM + downbeats)
3. Intro/Outro Detection (Essentia onset/offset)
4. Harmonic Analysis (future: key detection)

### 2. Database Schema
```sql
CREATE TABLE sweet_fades_analysis (
    item_id TEXT PRIMARY KEY,
    provider TEXT,
    bpm REAL,
    intro_duration REAL,
    outro_duration REAL,
    beats_json TEXT,
    downbeats_json TEXT,
    analysis_version INTEGER,
    created_at TIMESTAMP
);
```

### 3. Enhanced Crossfade Logic
```python
@dataclass
class SweetFadesAnalysis:
    bpm: float
    intro_duration: float
    outro_duration: float
    beats: list[float]
    downbeats: list[float]
    fade_in_start: float
    fade_out_start: float

async def sweet_crossfade_pcm_parts(
    fade_in_part: bytes,
    fade_out_part: bytes,
    current_analysis: SweetFadesAnalysis,
    next_analysis: SweetFadesAnalysis,
    pcm_format: AudioFormat,
) -> bytes
```

### 4. Integration Points

#### Real-time Mode (High-performance)
- **Hook**: `streams.py:1080-1082` when `track_loaded_in_buffer` called
- **Trigger**: First chunk → analyze next track immediately
- **Target**: Finish analysis before transition needed

#### Ad-hoc Mode (Low-power/Raspberry Pi)
- **Hook**: `streams.py:1188-1199` when track completes
- **Trigger**: Track finish → analyze for future use
- **Alternative**: During library sync

### 5. Configuration
```python
CONF_SWEET_FADES = "sweet_fades_enabled"
CONF_SWEET_FADES_MODE = "sweet_fades_mode"  # "realtime", "adhoc", "disabled"
CONF_SWEET_FADES_MIN_DURATION = "sweet_fades_min_duration"
CONF_SWEET_FADES_MAX_DURATION = "sweet_fades_max_duration"
```

### 6. Fallback Strategy
```python
def _get_crossfade_strategy(self, queue_item, next_item) -> str:
    if not sweet_fades_enabled:
        return "standard"
    if both_tracks_analyzed:
        return "sweet_fades"
    elif realtime_mode and next_track_available:
        return "realtime_analysis"
    else:
        return "standard"  # Graceful fallback
```

## Implementation Phases

### Phase 1: Basic BPM Matching
- [ ] Add Madmom dependency
- [ ] Implement basic BPM detection
- [ ] Enhance existing crossfade with tempo adjustment
- [ ] Database schema and caching

### Phase 2: Intelligent Transition Detection
- [ ] Add Essentia dependency
- [ ] Implement intro/outro detection
- [ ] Dynamic crossfade duration
- [ ] Crossfades curves? tri, qsin and iqsin 
- [ ] Real-time analysis pipeline

### Phase 3: Advanced Features
- [ ] Harmonic mixing (key detection)
- [ ] ML transition point optimization
- [ ] User preference learning

## File Structure
```
music_assistant/
├── helpers/
│   ├── audio_analysis.py          # NEW: Analysis engine
│   └── sweet_fades.py            # NEW: Enhanced crossfade
├── controllers/
│   ├── streams.py                # MODIFIED: Integration
│   └── player_queues.py          # MODIFIED: Analysis triggers
└── models/
    └── sweet_fades_analysis.py   # NEW: Data models
```

## Performance Considerations

**Real-time Mode:**
- Memory: ~10-30MB per analysis
- CPU: High during analysis, minimal during playback
- Latency: Must complete within 30-60s window

**Ad-hoc Mode:**
- Background processing during idle
- Storage: ~500 bytes per track
- Graceful fallback to standard crossfade

## Development Context

**Current branch**: `feat/sweet_fades`
**Analysis date**: 2025-08-24
**Analyzed files:**
- `/music_assistant/controllers/streams.py` - Audio streaming pipeline
- `/music_assistant/controllers/player_queues.py` - Queue management
- `/music_assistant/helpers/audio.py` - Audio processing helpers
- pyCrossfade library analysis - Reference implementation
- Plugin architecture evaluation - Ruled out for performance reasons

**Key findings:**
- Existing crossfade infrastructure can be enhanced
- Real-time audio analysis hooks available
- Database pattern established for audio analysis caching
- Async architecture supports background processing
