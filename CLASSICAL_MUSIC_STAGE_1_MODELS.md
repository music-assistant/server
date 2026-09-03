# Classical music support — Stage 1: Model changes

**Target repo:** `music-assistant-models`
**Status:** Draft for review
**Companion docs:** [`CLASSICAL_MUSIC_MODEL_SPEC.md`](CLASSICAL_MUSIC_MODEL_SPEC.md) (full design across all stages), [`CLASSICAL_MUSIC_PROPOSAL.md`](CLASSICAL_MUSIC_PROPOSAL.md) (community-facing summary).

## What this PR does

Adds first-class classical-music metadata to the shared models package:

- A new `Work` MediaItem representing a musical composition (e.g. *Symphony No. 5 in C minor, Op. 67*) independently of any specific recording.
- An `ArtistRole` enum and `Credit` type so artists on a track or album can carry their role (composer, conductor, orchestra, soloist with instrument, …) instead of being a flat list.
- A `Period` enum (Medieval / Renaissance / Baroque / Classical / Romantic / Modern / Contemporary) and additive `Artist.period` field for filtering composers by period — populated in Stage 4 (tag fallback) and Stage 6 (MB enrichment).
- Additive fields on `Track` and `Album` for work/movement linkage and the role-typed credits list.

Strictly **non-breaking**. No existing field changes type or is removed. Old consumers continue working without code changes.

## Why

Classical recordings carry credit information that doesn't fit MA's current flat artist model. Today:

- Composer, conductor, orchestra/ensemble, and soloists with instruments all get squashed into `Track.artists` (no role distinction) or the unstructured `Track.metadata.performers: set[str]` field. Users can't browse or search by composer, conductor, orchestra, etc.
- A "Work" — the *composition*, distinct from any specific recording — has no representation, even though MusicBrainz exposes Works as entities with their own MBID. Multiple recordings of *Beethoven's 5th* can't be grouped, and movements have no link back to the parent composition.

Standard tags (the MusicBrainz Picard mapping) and MusicBrainz itself already model this richer structure. This PR brings the shared models in line so the rest of the stack — server, frontend, integrations — can adopt incrementally.

## Scope

This PR is model-only:

- New types (`ArtistRole`, `Credit`, `Work`, `WorkType`, `Period`, `MediaType.WORK`, `ExternalID.MB_WORK`).
- Additive fields on `Track` and `Album` (and one on `Artist`).
- Serialisation support (mashumaro round-trip via `DataClassDictMixin`), dispatcher wiring, exports.

Not in this PR:

- Database schema changes (Stage 2).
- Server controllers / API endpoints (Stage 3).
- Tag parsing (Stage 4).
- Provider mapping (Stage 5).
- MusicBrainz enrichment (Stage 6).
- Frontend Classical view (Stage 7).
- Search integration (Stages 8–9).
- Playback / queue behaviour (Stage 10).

## Non-goals

- Not building a "Classical view" or any UI — this is only the shared models.
- Not deprecating or migrating away from any existing field. `Track.artists`, `Album.artists`, `Track.metadata.performers` all stay as they are.
- Not adding validation of tag values beyond enum membership. The parser (Stage 4) is responsible for normalising input; the model just holds it.

## Backwards compatibility

All additions have safe defaults (`None`, empty list, empty set). Old serialised data deserialises without error — new fields simply carry their defaults. Old code that reads `Track.artists` or `Album.artists` sees no change in shape or content.

### Synchronisation rule for `artists` vs `credits[role=MAIN_ARTIST]`

The existing flat `artists` list stays canonical for the headline credit. Each artist in `Track.artists` is also expected to appear in `Track.credits` with `role=MAIN_ARTIST`. The server layer (Stage 3) is responsible for keeping these in sync when either is written. Clients reading either will see consistent data.

## New types

### `ArtistRole` (enum)

```python
class ArtistRole(StrEnum):
    """Role an artist plays on a track or album credit."""

    MAIN_ARTIST = "main_artist"        # headline credit; equivalent to existing `artists` list
    COMPOSER = "composer"
    LYRICIST = "lyricist"
    ARRANGER = "arranger"
    CONDUCTOR = "conductor"
    ORCHESTRA = "orchestra"            # whole-orchestra credit
    ENSEMBLE = "ensemble"              # chamber group, band, etc.
    CHOIR = "choir"
    SOLOIST = "soloist"                # featured performer (usually with an instrument)
    PERFORMER = "performer"            # any other performing musician
```

Notes:

- `MAIN_ARTIST` exists so `credits` can be a complete list including the headline credit — the artists in `Track.artists` will appear in `Track.credits` with `role=MAIN_ARTIST`.
- The set is scoped to roles that matter for classical music. Pop/electronic roles (`REMIXER`) and general production roles (`PRODUCER`) are intentionally omitted; they can be added later if a consumer needs them.
- Forward-compatible: consumers should fall back to `PERFORMER` for unknown values added in future versions.

### `Credit` (dataclass)

```python
@dataclass(kw_only=True)
class Credit(DataClassDictMixin):
    """A single role-tagged artist credit on a track or album."""

    artist: Artist | ItemMapping
    role: ArtistRole
    instrument: str | None = None      # only meaningful for SOLOIST / PERFORMER
    position: int = 0                  # ordering *within a role group*; lower first
```

Notes:

- Free-form `instrument` string is intentional. Picard writes "violin", "piano", "soprano vocals", etc. We preserve the string as-is rather than enumerate.
- `position` is **per-role**: each role group has its own ordering starting at 0. So a track with two SOLOIST entries and three PERFORMER entries has positions 0–1 within SOLOIST and 0–2 within PERFORMER, not a global 0–4 sequence.
- Composer Sort Order, MusicBrainz Composer ID, and similar per-role tags do not need separate fields — they live on the underlying `Artist` (`sort_name`, `external_ids`).
- Inherits `DataClassDictMixin` so `Credit` instances nested inside `Track.credits` / `Album.credits` round-trip through the same mashumaro serialisation pipeline as the surrounding `MediaItem`. Uses `kw_only=True` to match the existing dataclass style in `media_item.py`.

### `Work` (MediaItem)

```python
@dataclass(kw_only=True)
class Work(MediaItem):
    """
    A musical composition, distinct from any specific recording of it.

    Multiple recordings of the same work share a Work entity (matched by
    MusicBrainz Work MBID where available). Movements of a multi-part work
    link to the parent work.
    """

    __hash__ = _MediaItemBase.__hash__
    __eq__ = _MediaItemBase.__eq__

    media_type: MediaType = MediaType.WORK
    composers: UniqueList[Artist | ItemMapping] = field(default_factory=UniqueList)
    catalog_numbers: list[str] = field(default_factory=list)   # ["Op. 67", "BWV 1041", "K. 525"]
    work_type: WorkType | None = None
    parent_work: ItemMapping | None = None                     # for movements / sub-works
    arrangement_of: UniqueList[ItemMapping] = field(default_factory=UniqueList)

    composition_year: int | None = None
    language: str | None = None                                # for vocal works ("Italian", "German", …)
    musical_key: str | None = None                             # "C minor", "B flat major"
    # inherited: item_id, name, sort_name, version, favorite, metadata, external_ids, …
```

```python
class WorkType(StrEnum):
    """High-level work classification, mirrors the MusicBrainz Work `type` field."""

    SYMPHONY = "symphony"
    CONCERTO = "concerto"
    SONATA = "sonata"
    SUITE = "suite"
    OPERA = "opera"
    ORATORIO = "oratorio"
    CANTATA = "cantata"
    MASS = "mass"
    SONG_CYCLE = "song_cycle"
    QUARTET = "quartet"             # string quartet, etc.
    OVERTURE = "overture"
    BALLET = "ballet"
    OTHER = "other"
```

Notes:

- `Work` is a full MediaItem so it gets `external_ids` (for the MusicBrainz Work MBID), images, descriptions, sort_name, search_name, etc. for free.
- `catalog_numbers` is a list because the same work can have multiple catalog references (Op. number plus a thematic catalog like K. or BWV). Stored as strings.
- `WorkType` covers the most common 12 types plus `OTHER`. MusicBrainz has ~25 types; we cover the ones that matter for browsing/grouping. Adding new variants later is non-breaking.
- `parent_work` is optional and self-referential. Movements *can* be modelled as separate Works with a parent link, but the **default is parent Work only with `movement_*` fields on Track** (see `Track` below). Movement-Works only created when the source supplies a distinct MBID for them.
- `arrangement_of` captures transcriptions, orchestrations, and reductions where one Work is derived from another (Mussorgsky's *Pictures at an Exhibition* piano original ↔ Ravel's orchestration). MB models these as distinct Works connected by an "arrangement of" relationship. The list form handles medleys.
- `composition_year` is the year the Work was composed (or, for an arrangement Work, the year the arrangement was made). Populated at enrichment time from MB Work begin-date's year portion. Int rather than string keeps range filtering simple.
- `language` for vocal works (opera / lieder / song cycles). Null for instrumental works.
- `musical_key` is the tonal key (`"C minor"`, `"B flat major"`). Often embedded in a work's title but stored as a separate filterable field for cross-work browsing.
- `MediaType.WORK` is a new variant of the existing `MediaType` enum.

### `MediaType.WORK`

Added value to the existing `MediaType` enum. Old consumers that switch over `MediaType` will fall through to their default case — same behaviour as encountering any future unknown type.

### `Period` (enum)

```python
class Period(StrEnum):
    """Classical music period / era. Used on Artist (composer) for browse filtering."""

    MEDIEVAL = "medieval"           # c. 500 – 1400
    RENAISSANCE = "renaissance"     # c. 1400 – 1600
    BAROQUE = "baroque"             # c. 1600 – 1750
    CLASSICAL = "classical"         # c. 1750 – 1820
    ROMANTIC = "romantic"           # c. 1820 – 1900
    MODERN = "modern"               # c. 1900 – 1975
    CONTEMPORARY = "contemporary"   # c. 1975 – present
```

Seven buckets matching Apple Music Classical / Roon / IMSLP / Wikipedia consensus. Date ranges are documentation only; inference rules (MB enrichment from composer dates, GENRE-tag fallback) live in the master spec's Classification policy. Population is deferred to Stage 4 (tag fallback) and Stage 6 (MB enrichment) — this PR just establishes the enum so the model is ready.

## Modified types

### `Track`

Additive fields only:

```python
@dataclass
class Track(MediaItem):
    # ... existing fields unchanged ...

    credits: list[Credit] = field(default_factory=list)
    work: ItemMapping | None = None
    movement_number: int | None = None
    movement_total: int | None = None
    movement_name: str | None = None       # e.g. "I. Allegro con brio"

    @property
    def composers(self) -> list[Artist | ItemMapping]:
        """Artists credited as composers, in tagged order."""
        return [c.artist for c in sorted(self.credits, key=lambda x: x.position)
                if c.role == ArtistRole.COMPOSER]

    @property
    def conductors(self) -> list[Artist | ItemMapping]:
        """Artists credited as conductors, in tagged order."""
        return [c.artist for c in sorted(self.credits, key=lambda x: x.position)
                if c.role == ArtistRole.CONDUCTOR]

    @property
    def performers_with_instruments(self) -> list[tuple[Artist | ItemMapping, str | None]]:
        """Performers and soloists with their instrument, in tagged order."""
        return [(c.artist, c.instrument)
                for c in sorted(self.credits, key=lambda x: x.position)
                if c.role in (ArtistRole.PERFORMER, ArtistRole.SOLOIST)]
```

Notes:

- `movement_name` is separate from `Track.name` because in non-classical contexts the display name is usually the full string (`"Symphony No. 5: I. Allegro"`) while the movement name alone is `"I. Allegro"` — keeping them split lets the UI choose.
- Convenience properties cover the common access patterns without forcing every consumer to filter `credits` by role manually.

### `Album`

Additive fields only:

```python
@dataclass
class Album(MediaItem):
    # ... existing fields unchanged ...

    credits: list[Credit] = field(default_factory=list)

    @property
    def composers(self) -> list[Artist | ItemMapping]: ...
    @property
    def conductors(self) -> list[Artist | ItemMapping]: ...
```

Same convenience-property pattern as Track.

For compilation albums, `Album.composers` and `Album.conductors` may return long lists — a "100 Greatest Classical Hits" compilation could have 50+ distinct composers. This is intentional: the data is honest about what's there, and display logic in the frontend can collapse to a placeholder like "Various composers" above some threshold. This is *not* the same as the existing `Album.artists = [Various Artists]` pattern (which uses a single placeholder Artist entity); the new credit-based properties always carry the actual list.

### `Artist`

Additive field only:

```python
@dataclass
class Artist(MediaItem):
    # ... existing fields unchanged ...

    period: Period | None = None
```

Set on composer Artists only (Artists with `COMPOSER` role on at least one track credit); null for performers. Population paths and inference rules live in the master spec's Classification policy section. This PR just establishes the field on the model.

## Supporting changes

A few small additions are required across the model package to make the new types fully usable. These are mechanical and uncontroversial but worth listing so reviewers can map every new type onto a working end-to-end flow.

### `ExternalID.MB_WORK`

Added to `enums.py` so a Work's MusicBrainz Work ID round-trips through the existing `external_ids` set. Also added to the `is_musicbrainz` property tuple so MBID validation (`is_valid_uuid`) and uniqueness handling (`is_unique`) cover it the same way they cover the other MB IDs.

```python
class ExternalID(StrEnum):
    ...
    MB_WORK = "musicbrainz_workid"

    @property
    def is_musicbrainz(self) -> bool:
        return self in (
            ExternalID.MB_RELEASEGROUP,
            ExternalID.MB_ALBUM,
            ExternalID.MB_TRACK,
            ExternalID.MB_ARTIST,
            ExternalID.MB_RECORDING,
            ExternalID.MB_WORK,
        )
```

### `_MediaItemBase.mbid` getter/setter extended for Work

The existing `mbid` property in `_MediaItemBase` switches on `media_type` for ARTIST / ALBUM / TRACK. A WORK branch is added to both getter and setter so `Work.mbid` reads/writes via `ExternalID.MB_WORK` consistently with the other MediaItem types.

### `media_from_dict()` dispatcher

The package-level `media_from_dict()` factory in `media_items/__init__.py` gains a `"work"` branch returning `Work.from_dict(media_item)`. Without this, deserialising a Work from a dict raises `InvalidDataError("Unknown media type")`.

### `MediaItemType` type alias

Extended from:

```python
MediaItemType = Artist | Album | Track | Radio | Playlist | Audiobook | Podcast | PodcastEpisode | Genre
```

to:

```python
MediaItemType = Artist | Album | Track | Work | Radio | Playlist | Audiobook | Podcast | PodcastEpisode | Genre
```

`PlayableMediaItemType` is **not** changed — Work is a composition, not directly playable; recordings of it (Tracks) are.

### Public exports

`Credit` and `Work` are added to the `__all__` list and imports in `media_items/__init__.py` so consumers can do `from music_assistant_models.media_items import Credit, Work`.

## Deferred to later stages

- Database schema, migrations, indexes → Stage 2.
- Server controllers / API endpoints → Stage 3.
- Tag parsing (Picard mapping, fallbacks, movement inference) → Stage 4.
- Provider-specific credit mapping → Stage 5.
- MusicBrainz enrichment (Recording-Work links, composer dates, Work-Artist relationships) → Stage 6.
- Frontend Classical view → Stage 7.
- Search integration → Stages 8-9.
- Playback / queue behaviour → Stage 10.

## Examples

A track from "Karajan conducts Beethoven Symphony No. 5", second movement:

```python
Track(
    name="Symphony No. 5 in C minor, Op. 67: II. Andante con moto",
    artists=[                                  # headline credit (unchanged)
        ItemMapping(name="Berlin Philharmonic Orchestra", ...),
        ItemMapping(name="Herbert von Karajan", ...),
    ],
    credits=[                                  # full role-typed credits
        Credit(artist=ItemMapping(name="Ludwig van Beethoven", ...),
               role=ArtistRole.COMPOSER, position=0),
        Credit(artist=ItemMapping(name="Herbert von Karajan", ...),
               role=ArtistRole.CONDUCTOR, position=0),
        Credit(artist=ItemMapping(name="Berlin Philharmonic Orchestra", ...),
               role=ArtistRole.ORCHESTRA, position=0),
        # MAIN_ARTIST entries duplicate the `artists` list, intentionally:
        Credit(artist=ItemMapping(name="Berlin Philharmonic Orchestra", ...),
               role=ArtistRole.MAIN_ARTIST, position=0),
        Credit(artist=ItemMapping(name="Herbert von Karajan", ...),
               role=ArtistRole.MAIN_ARTIST, position=1),
    ],
    work=ItemMapping(name="Symphony No. 5 in C minor, Op. 67",
                     item_id="...", provider="library"),
    movement_number=2,
    movement_total=4,
    movement_name="II. Andante con moto",
    # ... existing fields ...
)
```

A concerto track with multiple soloists:

```python
Track(
    name="Concerto for Violin, Cello and Piano in C major, Op. 56: I. Allegro",
    credits=[
        Credit(artist=..., role=ArtistRole.COMPOSER),     # Beethoven
        Credit(artist=..., role=ArtistRole.SOLOIST,
               instrument="violin", position=0),           # e.g. Mutter
        Credit(artist=..., role=ArtistRole.SOLOIST,
               instrument="cello", position=1),            # e.g. Ma
        Credit(artist=..., role=ArtistRole.SOLOIST,
               instrument="piano", position=2),            # e.g. Barenboim
        Credit(artist=..., role=ArtistRole.CONDUCTOR),     # e.g. Karajan
        Credit(artist=..., role=ArtistRole.ORCHESTRA),     # e.g. BPO
    ],
    ...
)
```

The corresponding Work:

```python
Work(
    name="Symphony No. 5 in C minor, Op. 67",
    composers=[ItemMapping(name="Ludwig van Beethoven", ...)],
    catalog_numbers=["Op. 67"],
    work_type=WorkType.SYMPHONY,
    composition_year=1808,
    musical_key="C minor",
    external_ids={(ExternalID.MB_WORK, "d03bff61-26fc-301b-98ac-4d8e85771cbc")},
    ...
)
```

## Testing

Standard mashumaro round-trip tests for each new type:

- `ArtistRole` enum serialises to its string value and deserialises back.
- `Credit` round-trips through `to_dict()` / `from_dict()`.
- `Work` round-trips including nested `composers` / `external_ids`.
- `Track` and `Album` with new fields serialise cleanly with empty defaults.
- `Track` and `Album` with populated `credits` round-trip losslessly.
- `Track.composers` / `.conductors` / `.performers_with_instruments` return correctly ordered results.
- `media_from_dict()` correctly dispatches `{"media_type": "work", ...}` to `Work`.
