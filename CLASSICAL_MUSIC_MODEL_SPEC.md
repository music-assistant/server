# Classical Music Support — Model Specification

**Status:** Design specification, staged across 10 implementable PRs.
**Companion docs:** [`CLASSICAL_MUSIC_EXECUTIVE_SUMMARY.md`](CLASSICAL_MUSIC_EXECUTIVE_SUMMARY.md), [`CLASSICAL_MUSIC_STAGE_1_MODELS.md`](CLASSICAL_MUSIC_STAGE_1_MODELS.md), [`CLASSICAL_MUSIC_TAGGING_GUIDE.md`](CLASSICAL_MUSIC_TAGGING_GUIDE.md), [`CLASSICAL_MUSIC_WORKS_LITE.md`](CLASSICAL_MUSIC_WORKS_LITE.md).

## Executive summary

MA's flat artist model doesn't fit classical music. Composer, conductor, orchestra, and soloist all get squashed into `Track.artists`; there's no representation of a composition (Work) distinct from a recording; movements have no structural link back to their parent work. This spec adds first-class support for that missing structure — all additive, all non-breaking. Pop, rock, jazz and electronic users see no change to their existing views. Classical listeners get composer, work and performer-based browse, role-typed credits, and movement-aware playback, comparable to Roon or Apple Music Classical but working with MA's existing diverse library and provider stack.

## Background

Classical recordings carry credit information that doesn't fit MA's current flat artist model. Today the composer, the conductor, the orchestra or ensemble, and any soloists all get squashed into `Track.artists` (no role distinction) or into the unstructured `Track.metadata.performers: set[str]` field. The consequences ripple through the app: users can't browse or search by composer, conductor, orchestra, or soloist; multiple recordings of the same composition can't be grouped; movements can't be sequenced gaplessly; catalog numbers like Op. 67 or BWV 1041 aren't discoverable; and the same recording appearing on different albums has no rollup to a canonical composition.

Standard tags (the MusicBrainz Picard mapping) and MusicBrainz itself already model this richer structure. This spec brings the MA data model in line so the rest of the stack — server, frontend, integrations — can adopt incrementally.

## What classical listeners actually want

Synthesised from the MA Discord "Better Classical Music Support" threads, Roon's classical forum discussions, and Apple Music Classical / IDAGIO design choices:

1. **Browse by composer as the primary axis.** "Show me all my Bach" is the single most-cited ask.
2. **Work as a first-class browseable entity.** Multiple recordings of the same composition grouped under one entry; movements playable as a unit, gapless.
3. **Distinct conductor / orchestra / soloist credits.** Filterable to "all Karajan recordings", "all Berlin Philharmonic recordings", "all violin recordings" without fuzzy text matching.
4. **Catalog numbers (BWV, K., Op., HWV) parsed and searchable.** Often the canonical handle for a work.
5. **Roll-up across granularity.** The same recording / track / movement appears in many shapes: a single track, a movement of a full work on its source album, the same single track on a compilation, a transcription across instruments (Mussorgsky's *Pictures at an Exhibition* piano original ↔ Ravel's orchestration; Bach organ ↔ piano transcriptions). The data needs to detect each as distinct *and* roll it up to album / work / composer / performer where appropriate.

**Asks deliberately deferred or out of scope:** fuzzy matching across spelling variants without an MBID (resolved in the Matching policy via the MBID-canonical rule); curated classical playlists and commissioned composer artwork (content / sourcing concerns, not model). Period / era support: model field added (`Artist.period`, Decisions log #5 revised), filter-chip UI deferred to a future polish stage after Stage 7.

The mapping from each user need above to the implementing stage(s) appears in the Implementation stages table below.

## Goals

- Add first-class model support for the classical structure (Work, role-typed Credits, movements).
- Enable a dedicated Classical view without disturbing the existing Artists / Albums / Tracks views.
- Preserve every existing model field, name, and type — strictly additive.
- Land the changes in reviewable stages, each with independent value.
- Use MusicBrainz as the canonical enrichment source, aligning with Picard's tag conventions.

## Implementation stages

| # | Stage | Repo | Dep. | Notes |
|---|---|---|---|---|
| 1 | **Model changes** | `music-assistant-models` | — | New `Work` MediaItem, `ArtistRole` enum, `Credit` type, `Period` enum, additive fields on `Track`/`Album`/`Artist`. Fully non-breaking. *(See `CLASSICAL_MUSIC_STAGE_1_MODELS.md`.)* |
| 2 | **Database schema & migrations** | `music-assistant/server` | 1 | New `works` table, `work_arrangements` junction, role/instrument/position columns on `track_artists` and `album_artists`, `is_classical` columns, `period` column. |
| 3 | **Server controllers & API** | `music-assistant/server` | 2 | `WorksController` mirrors per-MediaType controllers; role-typed queries on tracks/albums; classical-scoped views. |
| 4 | **Local file tag parsing** | `music-assistant/server` | 3 | Picard tag mapping + Roon / Classical Extras fallbacks. Populates credits, work, movement fields. Also covers CUE sheet parsing (see PR #3751) — classical fields surface from `REM` lines in the cue file and/or the underlying audio file's own tags. |
| 5 | **Streaming provider mapping** | `music-assistant/server` | 3 | Per-provider extraction of composer / conductor / performer credits and MB Work IDs where available. |
| 6 | **MusicBrainz enrichment** | `music-assistant/server` | 3 | Recording-Work links, composer/conductor/orchestra relationships, Work metadata, composer birth/death dates for Period inference. |
| 7 | **Frontend Classical view** | `music-assistant/frontend` | 3 | New top-level "Classical" entry with three internal tabs: Composers / Works / Performers (Performers carries role-filter chips). Composer detail page (works listed), Work detail page (recordings collapsed under one composition), OTHER VERSIONS reused for unmatched-Work suggestions. **Extended Track credits panel** (structured role-typed credits on Track detail) is **deferred to a future polish stage** — see Decisions log #31. **No search inside the Classical view** — search lives in the global search bar (Stages 8 & 9). |
| 8 | **Basic Classical search** | both | 3, 7 | *Classical* master chip in the global search bar; flat list of up to 50 mixed results (composers / works / performers / classical tracks); substring match. |
| 9 | **Refined classical search** | both | 8 | Nested chip hierarchy (Composers / Works / Performers, plus performer-role chips under Performers). |
| 10 | **Playback / queue behaviour** | both | 3 | Gapless within a Work, no shuffle across movements, movement-aware queue display. |

## Backwards compatibility

All additions have safe defaults (`None`, empty list, empty set). No existing field changes type or is removed. Old serialised data deserialises without error. Old code reading `Track.artists`, `Album.artists`, `Track.metadata.performers` sees no change in shape or content.

### Synchronisation rule for `artists` vs `credits[role=MAIN_ARTIST]`

The existing flat `artists` list stays canonical for the headline credit. Each artist in `Track.artists` is also expected to appear in `Track.credits` with `role=MAIN_ARTIST`. The server layer (Stage 3) keeps these in sync when either is written. Clients reading either see consistent data. This is the answer to Decisions log #1.

## New types

### `ArtistRole` (enum)

```python
class ArtistRole(StrEnum):
    MAIN_ARTIST = "main_artist"        # headline credit; equivalent to existing `artists` list
    COMPOSER = "composer"
    LYRICIST = "lyricist"
    ARRANGER = "arranger"
    CONDUCTOR = "conductor"
    ORCHESTRA = "orchestra"
    ENSEMBLE = "ensemble"
    CHOIR = "choir"
    SOLOIST = "soloist"
    PERFORMER = "performer"
```

### Populating `ORCHESTRA` / `ENSEMBLE` / `CHOIR` / `SOLOIST`

Two paths, applied in order.

**1. Tag parser heuristic (Stage 4).** For `PERFORMER` tags using Picard's parens convention (`Name (role-or-instrument)`), the parenthetical is matched against a small keyword table:

| Parens contains | Role |
|---|---|
| `orchestra`, `philharmonic`, `symphony orchestra` | `ORCHESTRA` |
| `choir`, `chorus`, `chorale`, `schola` | `CHOIR` |
| `ensemble`, `quartet`, `quintet`, `trio`, `consort` | `ENSEMBLE` |
| A specific instrument (`violin`, `piano`, `soprano`, `cello`, `harpsichord`, …) | `SOLOIST` with that instrument |
| Empty parens or unrecognised text | `PERFORMER` (with the parens text preserved) |

**2. MusicBrainz enrichment (Stage 6).** MusicBrainz models these as distinct Recording-Artist relationship types — `performing orchestra`, `chorus`, `instrument` (with instrument attribute), `performer` — and the enrichment provider maps them directly to the corresponding `ArtistRole`. Canonical when MB data is available; the tag heuristic only fills the gap when it isn't.

### `Credit` (dataclass)

```python
@dataclass(kw_only=True)
class Credit(DataClassDictMixin):
    artist: Artist | ItemMapping
    role: ArtistRole
    instrument: str | None = None      # only meaningful for SOLOIST / PERFORMER
    position: int = 0                  # ordering within a role group
```

### `Work` (MediaItem)

```python
@dataclass(kw_only=True)
class Work(MediaItem):
    __hash__ = _MediaItemBase.__hash__
    __eq__ = _MediaItemBase.__eq__

    media_type: MediaType = MediaType.WORK
    composers: UniqueList[Artist | ItemMapping] = field(default_factory=UniqueList)
    catalog_numbers: list[str] = field(default_factory=list)
    work_type: WorkType | None = None
    parent_work: ItemMapping | None = None
    arrangement_of: UniqueList[ItemMapping] = field(default_factory=UniqueList)
    composition_year: int | None = None
    language: str | None = None
    musical_key: str | None = None
```

```python
class WorkType(StrEnum):
    SYMPHONY = "symphony"
    CONCERTO = "concerto"
    SONATA = "sonata"
    SUITE = "suite"
    OPERA = "opera"
    ORATORIO = "oratorio"
    CANTATA = "cantata"
    MASS = "mass"
    SONG_CYCLE = "song_cycle"
    QUARTET = "quartet"
    OVERTURE = "overture"
    BALLET = "ballet"
    OTHER = "other"
```

Notes on Work fields:

- `Work` is a full MediaItem so it gets `external_ids` (Work MBID via `ExternalID.MB_WORK`), images, descriptions, sort_name, search_name for free.
- `composers` and `arrangement_of` use `UniqueList` (matching `Album.artists` codebase pattern).
- `catalog_numbers` is a plain `list[str]` because the same work can have multiple catalog references.
- `parent_work` is optional and self-referential. Default rule is parent Work only with `movement_*` fields on Track; movement-Works are created only when the source supplies a distinct MBID for them.
- `arrangement_of` captures transcriptions and orchestrations (Mussorgsky's *Pictures at an Exhibition* ↔ Ravel's orchestration). List form handles medleys.
- `composition_year` is the year the Work was composed. For an arrangement Work, this is the year the arrangement was made. Populated from MB Work begin-date's year portion at enrichment time.
- `language` for vocal works (opera / lieder / song cycles). Null for instrumental works.
- `musical_key` is the tonal key. Often embedded in a work's title, stored as a separate filterable field for cross-work browsing.

### `MediaType.WORK`

Added value to the existing `MediaType` enum. Old consumers that switch over `MediaType` will fall through to their default case.

### `Period` (enum)

```python
class Period(StrEnum):
    MEDIEVAL = "medieval"           # c. 500 – 1400
    RENAISSANCE = "renaissance"     # c. 1400 – 1600
    BAROQUE = "baroque"             # c. 1600 – 1750
    CLASSICAL = "classical"         # c. 1750 – 1820
    ROMANTIC = "romantic"           # c. 1820 – 1900
    MODERN = "modern"               # c. 1900 – 1975
    CONTEMPORARY = "contemporary"   # c. 1975 – present
```

Seven buckets matching Apple Music Classical / Roon / IMSLP / Wikipedia consensus. Date ranges are documentation only; inference rules live in Classification policy below.

## Modified types

### `Track`

```python
@dataclass
class Track(MediaItem):
    # ... existing fields unchanged ...

    credits: list[Credit] = field(default_factory=list)
    work: ItemMapping | None = None
    movement_number: int | None = None
    movement_total: int | None = None
    movement_name: str | None = None

    @property
    def composers(self) -> list[Artist | ItemMapping]: ...
    @property
    def conductors(self) -> list[Artist | ItemMapping]: ...
    @property
    def performers_with_instruments(self) -> list[tuple[Artist | ItemMapping, str | None]]: ...
```

`movement_name` is kept separate from `Track.name` because in non-classical contexts the display name is usually the full string (`"Symphony No. 5: I. Allegro"`) while the movement name alone is `"I. Allegro"` — keeping them split lets the UI choose.

### `Album`

```python
@dataclass
class Album(MediaItem):
    # ... existing fields unchanged ...

    credits: list[Credit] = field(default_factory=list)
```

Same convenience properties as Track. For compilation albums, `Album.composers` may return long lists; frontend display can collapse to "Various composers" above some threshold. This is not the same as the existing `Album.artists = [Various Artists]` pattern.

### `Artist`

```python
@dataclass
class Artist(MediaItem):
    # ... existing fields unchanged ...

    period: Period | None = None
```

Set on composer Artists only (Artists with COMPOSER role on at least one track credit); null for performer-only artists.

## Field provenance

| Field | Primary source | Secondary | Notes |
|---|---|---|---|
| `Track.credits` | Local file tags (Stage 4) | Provider metadata (Stage 5), MB enrichment (Stage 6) | Merged: local first, provider augments, MB canonicalises |
| `Track.work` | `MUSICBRAINZ_WORKID` / `WORK` tag | MB Recording→Work relationship | Requires classical-context signal to materialise as Work entity |
| `Track.movement_*` | `MOVEMENTNUMBER` / `MOVEMENTTOTAL` / `MOVEMENTNAME` tags | Inferred from MB Recording title patterns | Picard's iTunes movement tag option needed |
| `Work.composers` | Composer credit on any linked track | MB Work-Artist relationship | Deduplicated by MBID then normalised name |
| `Work.catalog_numbers` | Parsed from work title / disambiguation | MB Work catalog-number attribute | List; multiple catalogs common (Op. + K., BWV etc.) |
| `Work.work_type` | MB Work type field | Inferred from title keywords ("Symphony", "Concerto") as fallback | Maps to `WorkType` enum; unknowns → `OTHER` |
| `Work.parent_work` | MB Work "part of" relationship | — | Only set for movement-Works with own MBID |
| `Work.arrangement_of` | MB Work "arrangement of" relationship | — | Source Work(s) |
| `Work.composition_year` | MB Work begin-date | — | Year portion only |
| `Work.language` | MB Work language attribute | — | Vocal works only |
| `Work.musical_key` | MB Work key attribute | — | |
| `Artist.period` | GENRE-tag period name **(primary)** | MB Work composer floruit inference | See Populating `Artist.period` below |
| `Track.is_classical` | Computed | — | See Computed is_classical fields |

### Tag fallbacks for non-Picard taggers

MA reads a small set of well-known fallback tag names from other classical taggers, with inline code comments identifying the source:

- **Roon**: `PART`, `ENSEMBLE`, `SOLOIST`, `PERSONNEL`, `SECTION`.
- **Classical Extras**: `groupheading`, `top_work`, `is_classical`, `movement`, and its trailing-`::`-separator hierarchy convention (stripped during parse).

The parser is permissive (reads all these); the UI is opinionated. We don't *recommend* any of these tools to users (each has its own failure modes), but reading their outputs lets users switch tools going forward without re-tagging existing files. See Decisions log #7 and #13.

### CUE sheet source (Stage 4)

Single-file rips with an accompanying CUE sheet are a first-class source, not an afterthought. The Stage 4 parser handles them alongside individual file tags via the cue sheet parser (PR #3751). Classical fields surface from two places in a CUE-sheet setup:

- **`REM` lines in the cue file** — the parser reads Picard-convention `REM COMPOSER`, `REM CONDUCTOR`, `REM PERFORMER`, `REM WORK`, `REM MOVEMENTNAME`, `REM MOVEMENTNUMBER`, `REM MOVEMENTTOTAL`, `REM MUSICBRAINZ_WORKID` and the other MB IDs. Follows the cue sheet's multi-value convention (repeat the line rather than delimiter-join, aligned by index with `PERFORMER`).
- **The underlying FLAC or WAV file's own tag block** — anything the audio file's Vorbis / ID3 tags carry is read the same way as a standard tagged file. Used for album-level fields the cue sheet doesn't override.

Track-level classical fields (per-movement composer, per-movement work info) belong in the CUE sheet's per-track `REM` lines, since the single audio file only carries one copy of its own tags and can't vary them per CUE track. Album-level classical fields (composer where an album has one, conductor of a full symphony cycle) can live in either the cue sheet's sheet-level directives or the audio file's tags — the cue sheet wins where both are present.

## Matching policy

### Canonical entity resolution via MBID

When a tag carries both an entity name and a MusicBrainz ID, the MBID determines the canonical entity. Two tracks tagged "Béla Bartók" and "Bela Bartok" with the same `MUSICBRAINZ_ARTISTID` merge to one Artist. Without an MBID, name matching is strict (no fuzzy / phonetic / token matching); spelling variants without MBIDs surface as separate entities and are exposed via OTHER VERSIONS rather than auto-merged.

### Work matching

Priority order:

1. `MUSICBRAINZ_WORKID` present → match or create the Work by MBID.
2. `WORK` tag present with composer → dedup by composer + normalised title.
3. `WORK` tag present without composer → dedup by normalised title alone (weaker; may create duplicates that a later enrichment pass merges).

Multi-value WORKID: see below.

### Arrangements

Arrangements are modelled as distinct Works with `arrangement_of` linking to their source Works. MusicBrainz treats these as separate Works connected by an "arrangement of" relationship, and MA mirrors that. Ravel's orchestration of *Pictures at an Exhibition* is its own Work with `arrangement_of = [Mussorgsky's piano original]`, its own `composition_year=1922`, and its own recordings underneath.

Per MB convention, an arrangement Work keeps the **original composer** as its composer (Mussorgsky, not Ravel); the arranger is credited via a Work-level arranger relationship (rendered on Work detail, not as a composer). This is a MB convention MA inherits.

### Multi-value tag handling (semicolon-separated)

Picard writes multi-value tags as semicolon-space separated within a single tag (or via multiple entries where the format supports it). MA's parser splits on `; ` for any credit-bearing tag: `COMPOSER`, `CONDUCTOR`, `LYRICIST`, `ARRANGER`, `MUSICBRAINZ_ARTISTID`, `MUSICBRAINZ_WORKID`.

### Multi-value `MUSICBRAINZ_WORKID` and `WORK` tags

Picard writes semicolon-separated values when a Recording is linked to more than one Work in MusicBrainz — typically parent + movement (Beethoven's *Moonlight* Sonata + its first movement) or arrangement + source. Parser convention: last entry is the canonical primary (Picard's convention is most-general → most-specific). Earlier entries resolve via Stage 6 MB enrichment into `Work.parent_work` (parent+movement) or `Work.arrangement_of` (arrangement+source). Relationship type cannot be reliably distinguished at parse time. Without MBIDs, a multi-value `WORK` tag is ambiguous; parser falls back to last-value-as-primary. See Decisions log #27.

### Within-track artist name resolution

The general rule (MBID canonical, no fuzzy matching) has one deliberate carve-out. **Within a single track**, when one credit is MBID-anchored and another credit on the same track has a text-only name that is a **substring** of the MBID-canonical name, the parser merges the text-only credit onto the canonical Artist entity. Catches the common honorific / formal-name case (`"Moura Lympany"` ⊂ `"Dame Moura Lympany"`; `"Karajan"` ⊂ `"Herbert von Karajan"`; `"Bach"` ⊂ `"Johann Sebastian Bach"`).

The carve-out is intentionally narrow: within a single track only, substring match only (no Levenshtein / token / phonetic), and anchored by at least one MBID on the track. Cross-track name resolution remains MBID-only. See Decisions log #28.

### Partial recordings

A track containing only part of a Work (e.g. just *The Great Gate of Kiev* from *Pictures at an Exhibition*) is modelled in MusicBrainz as a Recording-Work relationship with a "partial" attribute. See open questions for whether to surface this as a Track flag.

### Performance grouping within an album

Real-world classical compilations sometimes contain multiple recordings of the *same* Work on a single album — e.g. an album with three different recordings of Beethoven's 5th, each contributing 4 movements. Without disambiguation, all 12 movements would collapse under one Work entry with confused movement numbering.

**Rule (heuristic, no new tag required):** within a single album, group movements that share `(Work + conductor + ensemble)` as one performance. Three Karajan/BPO movements + four Bernstein/VPO movements + four Solti/Chicago movements naturally split into three performance groups based on the differing credit pairs.

If a real-world album turns up where this heuristic fails (same conductor + same ensemble recording the same Work twice on one album), a Roon-style `performance_id` field would disambiguate; deferred to Open Questions.

### Scale considerations

Not a model concern but worth flagging: real classical libraries hit the tens of thousands of tracks per composer (8000+ Bach tracks is realistic). The composer-level browse view **must be Work-grouped, not a flat track list** — a composer page is a list of Works first, with recordings nested underneath.

## Classification policy

Two related runtime decisions: (1) when does a track get a `Work` entity attached, and (2) when does a track appear in the Classical view? The rules differ because the cost of getting them wrong differs.

### Classical view scope by MediaType

The Classical view sources exclusively from `Track`, `Album`, `Artist`, and `Work` entities. **`Radio`, `Podcast`, `PodcastEpisode`, `Audiobook`, `Genre`, `Folder`, and other non-music-library MediaTypes are excluded regardless of their genre tags.** Opt-in by MediaType, not opt-out by exclusion list.

### When to create a `Work` entity

**Conservative.** A Work should be created only when there is positive evidence that the track is part of a defined composition **and** the composition is plausibly classical. In priority order:

1. **`MUSICBRAINZ_WORKID` is present AND classical-context signal is present** → match or create the Work; canonical signal when paired with classical context.
2. **`WORK` tag is present** (or the plugin fallbacks `groupheading` / `top_work`) **AND classical-context signal is present** → create the Work, deduplicate by composer + title.
3. **Composer is present AND movement info is present** (`MOVEMENTNAME`, `MOVEMENTNUMBER`, or `groupheading`) **AND classical-context signal is present** → infer a Work from the available signal.
4. **Otherwise: no Work.** A track with only a composer credit and nothing else does **not** become a Work. When `MUSICBRAINZ_WORKID` or `WORK` is present *without* a classical-context signal, the tag value is **preserved on the track row** so it survives for re-evaluation when more signal arrives — but no `Work` entity is created and no classification cascade fires.

**Classical-context signal — any one of:**

- A classical genre on the track, album, `artist.nfo`, or `album.nfo` (Classical, Baroque, Romantic, Symphony, Concerto, Opera, Sonata, Choral, Chamber music, …).
- `is_classical=1` tag set on the track or album.
- (Stage 6 enrichment only) MB Work `type` is a classical type — Symphony / Sonata / Concerto / Opera / Oratorio / Cantata / Mass / Song cycle / Quartet / Overture / Suite / Ballet. MB's "Song" type (used for pop / rock / jazz vocal numbers) **does not** count.

The reason for the gating: Picard with "Use track relationships" enabled writes `MUSICBRAINZ_WORKID` for rock, pop, and jazz tracks too — MB models rock songs as Works the same way it models symphonies. Without classical-context gating, every Beatles / Led Zeppelin / Coltrane track tagged through current Picard defaults would create a Work entity and trigger classical classification, polluting both the Works browse tab and the Classical view's track index. See Decisions log #34.

**Stage 6 re-evaluation pass.** When MB enrichment fetches a Work and its `type` field confirms a classical type, the Stage 4 hold is released: the Work entity is created and the classification cascade fires. This catches the thin-tagged classical compilation case — a `MUSICBRAINZ_WORKID` with no GENRE tag will eventually surface as classical once enrichment confirms it's a Symphony / Sonata / etc.

### When a track appears in the Classical view

**More liberal.** False negatives (classical track missing) feel broken; false positives (a soundtrack track appearing) feel mildly annoying. Default toward inclusion. A track appears in the Classical view if **any** of:

1. **`is_classical=1` tag is set** — explicit user signal, definitive.
2. **Track has a `Work` attached** (per the Work-creation rules above) — definitive.
3. **Genre tag matches a classical genre** (Classical, Baroque, Symphony, Concerto, Opera, Sonata, Choral, Chamber music, …).
4. **Track is on an album classified as classical** (see album-level rule below).

### Album-level classical classification

An album is classified as classical if a majority of its tracks satisfy any of the per-track rules above. Once an album is classical, **all** its tracks appear in the Classical view, even ones with thin metadata.

### Populating `Artist.period`

`Artist.period` is set on composer Artists only. Two tiered sources, applied in **priority order** — first non-null wins; existing values are not overwritten by lower-priority sources:

1. **Genre period from any source (Stage 4) — primary, user-controlled.** The Stage 4 parser inspects genre values from all sources MA already reads:
   - Multi-value `GENRE` tag on tracks where this Artist has a `COMPOSER` credit.
   - `<genre>` elements in `artist.nfo` for this composer.
   - `<genre>` elements in `album.nfo` for albums where this composer has track credits.
   
   If any period name appears in the combined genre set (case-insensitive match: `Baroque`, `Romantic`, `Medieval`, `Renaissance`, `Classical`, `Modern` / `20th Century`, `Contemporary` / `21st Century`), the corresponding `Period` value is stamped on the composer Artist. Source precedence within this tier when multiple sources name different periods: `artist.nfo` > `album.nfo` > track tags.
   
   **Tag-as-override deliberate inversion.** This sits *above* MB enrichment, not below, because period for boundary composers is genuinely subjective (Beethoven could reasonably be Classical or Romantic depending on which works the user listens to most). Giving genre priority makes period **user-overridable today without waiting for a manual-override UI**. Inversion is limited to this one field; the MBID-canonical rule still applies everywhere else.

2. **MusicBrainz enrichment (Stage 6) — secondary, automatic.** When the GENRE-tag path is silent and the Artist has an MBID with birth/death dates, the period is inferred from the composer's **floruit** (productive peak), approximated as the midpoint of `(birth_year + 25, death_year − 5)`:

   | Floruit midpoint | Period |
   |---|---|
   | before 1400 | `MEDIEVAL` |
   | 1400 – 1600 | `RENAISSANCE` |
   | 1600 – 1750 | `BAROQUE` |
   | 1750 – 1820 | `CLASSICAL` |
   | 1820 – 1900 | `ROMANTIC` |
   | 1900 – 1975 | `MODERN` |
   | after 1975, or still living | `CONTEMPORARY` |

   Worked examples:

   | Composer | Birth / death | Floruit midpoint | Bucket |
   |---|---|---|---|
   | Handel | 1685 – 1759 | 1720 | `BAROQUE` |
   | Bach | 1685 – 1750 | 1717 | `BAROQUE` |
   | Mozart | 1756 – 1791 | 1774 | `CLASSICAL` |
   | Haydn | 1732 – 1809 | 1769 | `CLASSICAL` |
   | Beethoven | 1770 – 1827 | 1808 | `CLASSICAL` |
   | Schubert | 1797 – 1828 | 1823 | `ROMANTIC` |
   | Brahms | 1833 – 1897 | 1875 | `ROMANTIC` |
   | Mahler | 1860 – 1911 | 1888 | `ROMANTIC` |
   | Schoenberg | 1874 – 1951 | 1923 | `MODERN` |
   | Pärt | 1935 – | 1985 (living, current year proxy) | `CONTEMPORARY` |

   Floruit-based rather than death-date based: a composer who lived long into the next stylistic period without producing significant new work there should still be bucketed by their primary output. Death-date would misplace Handel (d. 1759) into Classical despite being canonical Baroque.

3. **Manual override (future polish).** Out of scope for the initial implementation; the GENRE-tag path serves as the override mechanism for now.

`Artist.period` is null when neither source resolves. The Composers tab's period filter chip treats null as "unknown" and excludes those artists from period-specific filters but keeps them in the "All periods" view.

**Edge cases.** Composers spanning two periods (Beethoven, Schubert, Schoenberg, Mahler) get their closest-fit single period via the floruit rule; users disagreeing use the GENRE-tag path to override. Stylistic pastiches (a 1985 piece written in Baroque style) accept their composer's period; a future `Work.period` override addresses per-piece pinning if demand emerges.

### Expected outcomes

| Library content | Outcome |
|---|---|
| Bach box-set with full tags (Work + composer per track) | All tracks in Classical view, grouped under hundreds of Works |
| Pärt compilation with thin tags | All tracks in Classical view via album-level inheritance |
| Hans Zimmer film score (composer credits, "Soundtrack" genre, no Work info) | **Not** in Classical view; no Works created |
| Jazz album with composer credits ("Take Five" — Paul Desmond) | **Not** in Classical view; no Works |
| Singer-songwriter album where artist self-credits as composer | **Not** in Classical view; no Works |
| Rock / pop / jazz album tagged with Picard + "Use track relationships" ON (carries `COMPOSER`, `LYRICIST`, `PERFORMER`, `MUSICBRAINZ_WORKID` per MB's full song-level data) | **Not** in Classical view; no Works created. `MUSICBRAINZ_WORKID` preserved on the track but classical-context gating prevents Work creation. MB Work type at Stage 6 confirms `Song` type → no upgrade. |
| Classical compilation tagged only with basic fields (`ARTIST` / `TITLE` / `GENRE=Classical`, no composer / conductor / performer / work info) | Appears in the Classical view via the genre rule. Contributes only to the Performers / All chip — absent from the Composers tab, Works tab, and role-specific Performer chips because the structured data isn't there. |
| Classical compilation with `MUSICBRAINZ_WORKID` but no genre tag (thin-tagged but MB-linked) | Initially: Stage 4 holds the WORKID on the track, no Work entity created. After Stage 6 enrichment: MB confirms classical Work type, Work entity created, track classified as classical retroactively. |

### User overrides (future polish)

A per-track or per-album "treat as classical" / "exclude from classical" override for users whose libraries don't match these defaults. Out of scope for Phase 1; additive when added.

### Computed `is_classical` fields exposed to clients

Three derived boolean fields on the wire:

| Field | Definition | Use case |
|---|---|---|
| `Track.is_classical: bool` | True if the track satisfies any rule under "When a track appears in the Classical view". | Conditionally render classical-specific UI on Track detail. |
| `Album.is_classical: bool` | True per "Album-level classical classification". | Conditionally render classical-aware album views. |
| `Artist.is_classical: bool` | True if the artist has any credit on a track where `Track.is_classical=True`. | Cross-linking; conditionally render classical sections on Artist detail. |

All three are computed server-side, cached for performance, and recomputed when the underlying credits or classification inputs change. Clients treat them as read-only flags.

**The Classical view's tab indices filter by `Track.is_classical=True`.** The Composers tab is "artists with `role=COMPOSER` on at least one classical track", not "artists with any composer credit anywhere"; the Performers tab and its chip filters use the same scoping. Prevents non-classical composer credits from polluting the Classical view.

## Examples

A track from "Karajan conducts Beethoven Symphony No. 5", second movement:

```python
Track(
    name="Symphony No. 5 in C minor, Op. 67: II. Andante con moto",
    artists=[
        ItemMapping(name="Berlin Philharmonic Orchestra", ...),
        ItemMapping(name="Herbert von Karajan", ...),
    ],
    credits=[
        Credit(artist=ItemMapping(name="Ludwig van Beethoven", ...),
               role=ArtistRole.COMPOSER, position=0),
        Credit(artist=ItemMapping(name="Herbert von Karajan", ...),
               role=ArtistRole.CONDUCTOR, position=0),
        Credit(artist=ItemMapping(name="Berlin Philharmonic Orchestra", ...),
               role=ArtistRole.ORCHESTRA, position=0),
        Credit(artist=ItemMapping(name="Berlin Philharmonic Orchestra", ...),
               role=ArtistRole.MAIN_ARTIST, position=0),
        Credit(artist=ItemMapping(name="Herbert von Karajan", ...),
               role=ArtistRole.MAIN_ARTIST, position=1),
    ],
    work=ItemMapping(name="Symphony No. 5 in C minor, Op. 67", ...),
    movement_number=2,
    movement_total=4,
    movement_name="II. Andante con moto",
)
```

A triple concerto with multiple soloists:

```python
Track(
    credits=[
        Credit(artist=..., role=ArtistRole.COMPOSER),     # Beethoven
        Credit(artist=..., role=ArtistRole.SOLOIST,
               instrument="violin", position=0),
        Credit(artist=..., role=ArtistRole.SOLOIST,
               instrument="cello", position=1),
        Credit(artist=..., role=ArtistRole.SOLOIST,
               instrument="piano", position=2),
        Credit(artist=..., role=ArtistRole.CONDUCTOR),
        Credit(artist=..., role=ArtistRole.ORCHESTRA),
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
)
```

## Migration notes for downstream consumers

- **Old clients** see no change. New fields are additive with safe defaults.
- **New clients** should treat `Track.credits` as authoritative for role-typed queries. `Track.artists` remains canonical for the headline credit; the two are kept in sync at the server layer.
- **`ArtistRole` enum** may gain new values in future versions. Consumers should fall through to `PERFORMER` for unknown roles.
- **`WorkType` and `Period`** enums may gain new values. Consumers should fall through to `OTHER` (WorkType) or ignore (Period) for unknowns.

## Frontend integration approach

### Coexistence with standard browse views

The existing Artists / Albums / Tracks views are unchanged. The Classical view is a parallel lens over the same data, not a replacement.

### Where it lives in the navigation

Single top-level "Classical" entry in the main navigation, greyed out for users with no classical content (same pattern as Audiobooks / Podcasts). See Decisions log #6.

### Tab layout inside the Classical view

Three tabs: **Composers / Works / Performers**. The Performers tab carries role-filter chips: *All / Conductors / Orchestras / Chamber groups / Choirs / Soloists / Other performers*. Considered five tabs (separate Conductors and Ensembles) and four tabs (with a Search tab) — both rejected. See Decisions log #8.

Creator roles (`LYRICIST`, `ARRANGER`) are **not surfaced in the Performers tab** — see Decisions log #29.

### View structure (low-fidelity)

**Composers tab.** Alphabetical grid of composers with sort options (Composer / Sort name), a text filter (essential at scale — real libraries have hundreds of composers), and period filter chips (future polish). Click a composer → composer detail page.

**Works tab.** List of works with columns: Composer / Title / Catalog number / Composition year / Recordings count. Text filter, date range filter (composition year), sort options (Composer / Title / Year composed / Recording count). Arrangements appear as separate rows alongside originals, distinguished by "(arr. X)" in the title. Click a work → Work detail page.

**Performers tab.** Grid of performers with role-filter chips (see above). Click a performer → performer detail page.

#### Sort defaults per view

- Composers: Sort name ascending (surname-first when SORT tag present, else display name).
- Works: Composer name, then Title, then Composition year (implicit tiebreakers).
- Performers: Sort name / display name.

#### Per-performer scoping of counts on Performer detail pages

Counts on a Performer detail page (header summary stats, per-Work `recording_count`, per-collaborator counts) are **scoped to this performer's contributions**, not library-wide totals. Karajan's page showing "Beethoven Symphony No. 5 · 8 rec." means 8 Karajan recordings of Beethoven 5. Same principle as Decisions log #15's contextual filter on Work detail. See Decisions log #32.

#### "Other tracks" sections on Composer and Performer detail pages

To prevent empty / partially-empty detail pages when an entity's library credits are all on Workless tracks, both Composer detail and Performer detail render a secondary section after the canonical Works list:

**Composer detail:**
1. **Works** (primary) — canonical compositions sorted by catalog number (default).
2. **Other tracks** (secondary) — tracks credited to this composer where `Track.work IS NULL`. Hidden when empty. Sort options: **Name** (default, alphabetical by track title), **Year** (release year), **Date added**. Album-grouping sort deliberately omitted.

**Performer detail** (Conductor / Soloist / Orchestra / Ensemble / Choir / Other performer):
1. **Works performed** (primary) — canonical compositions where this performer has a credit on at least one linked track. Per-Work `recording_count` scoped to this performer.
2. **Other tracks** (secondary) — tracks where this performer has any non-composer credit and `Track.work IS NULL`. Hidden when empty. Same sort options as Composer detail's Other tracks: **Name** (default), **Year**, **Date added**.

The "Other tracks" surface is **local to detail pages only** — the Classical view's Works tab stays Work-centric and free of synthetic Work entities. See Decisions log #33.

### Search integration

**Stage 8 — Basic Classical chip.** Add a *Classical* master chip to the existing global search alongside the current chips. When selected, returns up to 50 mixed classical results (composers, works, performers, classical-credited tracks) in a flat list. Single-term substring match against extended `search_name` fields and role-typed credits.

**Stage 9 — Nested chip hierarchy.** Extends the Classical chip with a second level of chips (Composers / Works / Performers) when activated, plus a third level inside Performers for role narrowing.

### Future polish: instrument filter

A Soloist chip with a further instrument filter (violin / piano / cello / etc.) is a natural future refinement once real usage justifies it. Deferred.

### Future polish: work-type filter on the Works tab

Symphonies / Concertos / Operas / Chamber Music as filter chips on the Works tab. Deferred.

### Detail pages

**Composer detail:** header (name, dates, image, sort name), Works list grouped by work_type (Symphonies / Concertos / etc.) then sorted by catalog number, "Other tracks" section for Workless credits.

**Work detail:** header (title, composer, catalog numbers, key, composition_year, work_type), Recordings list with filter box (see #15a) and date range filter for performance year, Related Works section for arrangement chain (bidirectional — "arrangement of..." and "arrangements of this work..."), OTHER VERSIONS reused for unmatched suggestions.

**Performer detail:** header (name, role — Conductor / Orchestra / etc., image, sort name, per-scoped counts), Works Performed list (per-Work recording counts scoped to this performer), "Other tracks" section.

### Navigation pattern: contextual filter on Work detail

All "list of works" views navigate to the same Work detail page. When arrival happens from a performer-filtered context (Conductor / Soloist / Orchestra / Ensemble / Choir detail), the Work detail page applies an implicit recording filter to that performer with a "Show all" escape hatch. From Composer detail, Works tab, Search, or OTHER VERSIONS, no filter is applied. See Decisions log #15 (with refinement #15a).

### Recordings link back to source albums

Each recording row on Work detail links back to the album it lives on, so users can navigate from a specific recording to its full album context.

### Context menu navigation

Right-click / long-press context menus on tracks, albums, artists, works and recordings expose classical-aware options (View in Classical view, Show all recordings of this Work, etc.) when the entity is `is_classical=True`.

## Decisions log

Records of the substantive design questions that came up during drafting and their resolutions, so reviewers don't have to re-litigate them.

1. **Duplication between `artists` and `credits[role=MAIN_ARTIST]`.** *Resolved:* `artists` canonical for headline; `credits` canonical for non-headline roles; server keeps `MAIN_ARTIST` entries in `credits` mirroring `artists`.
2. **Movements as Works vs. just movement fields.** *Resolved:* parent Work only with `movement_*` fields on Track is the default. Movement-Works only created when the source supplies a distinct MBID for them.
3. **`WorkType` granularity.** *Resolved:* 12 common types + `OTHER`. Easy to extend later; consumers should fall back to `OTHER` for unknown values.
4. **`Credit.position` semantics.** *Resolved:* per-role ordering, each role group starts at 0.
5. **Period / era field.** *Resolved (revised):* in scope as a new optional `Artist.period: Period | None` field with seven enum values (Medieval / Renaissance / Baroque / Classical / Romantic / Modern / Contemporary). Original concern about "no canonical source" addressed by tiered population with a deliberate inversion: **GENRE-tag period is primary (user-controlled override path)**, **MB enrichment is secondary (automatic fallback)**. Inverts the usual MBID-canonical rule for this one field because period for boundary composers is genuinely subjective. MB inference uses **floruit midpoint** (approximated as `(birth + 25 + death − 5) / 2`), not death-date — death-date misplaces Handel (d. 1759) into Classical despite being canonical Baroque; floruit handles long-lived composers correctly. Lives on Artist (not Work). Used as a **filter chip** on Composers and Works tabs, not as a sort axis.
6. **Promoting classical sub-views to main nav vs. internal tabs.** *Resolved:* single top-level "Classical" entry with internal tabs.
7. **Whether to recommend Classical Extras (Picard plugin) to users.** *Resolved:* **actively recommend against it for new tagging.** Plugin destructively rewrites `ARTIST`, produces wrong data when MB lacks Work info, encodes hierarchy with trailing `::` separators, configuration variance is enormous. Standard Picard with iTunes-style movement tags enabled gives the parser the same signal without the destructive ARTIST rewrite. MA's Stage 4 parser supports the plugin's common output tag names as fallbacks so existing Classical-Extras-tagged libraries work without re-tagging.
8. **Tab layout inside the Classical view.** *Resolved:* three tabs — Composers / Works / Performers — with role-filter chips inside Performers. Considered five tabs (separate Conductors and Ensembles) and four tabs (with a Search tab), both rejected.
9. **"Other performers" chip in the Performers tab.** *Resolved:* include it. Catches `role=PERFORMER` (the catch-all role for credits that couldn't be more specifically classified — missing instrument, generic MB relationship, session musicians, etc.).
10. **Where Classical search lives.** *Resolved:* in the existing global search bar via a *Classical* master chip, not as a tab inside the Classical view. Auto-activate the Classical chip when search is invoked from the Classical view for context-aware default scope.
11. **Staging Classical search across two PRs.** *Resolved:* Stage 8 ships the basic Classical chip returning a flat list of up to 50 mixed results. Stage 9 adds the nested chip hierarchy.
12. **Search backend upgrade (FTS5, multi-term token-AND, ranked results).** *Resolved:* out of scope for the classical project entirely. MA-wide search infrastructure needs its own RFC.
13. **Support for Roon and Classical Extras tag conventions.** *Resolved:* the parser reads a small set of well-known fallback tag names from each. We don't *recommend* either tagger to users, but alternative tag names are read as fallbacks so users coming from those tools work without retagging.
14. **Classical as an album-type filter.** *Rejected.* The existing album-type filter (Live / Soundtrack / Compilation / etc.) draws from MusicBrainz's release-type taxonomy and describes production context. Classical is a genre/classification that cuts *across* release types.
15. **Navigation: contextual filter on Work detail.** *Resolved.* All "list of works" views navigate to the same Work detail page. When arrival happens from a performer-filtered context, the Work detail page applies an implicit recording filter to that performer with a "Show all" escape hatch. From Composer detail / Works tab / Search / OTHER VERSIONS, no filter is applied.
15a. **Explicit filter input on Work detail (refines #15).** The contextual filter described in #15 is reached via an **explicit filter affordance** at the top of the recordings list. A free-text input field (placeholder *"Filter recordings…"*) is available on every Work detail page. On performer-context arrival the input is replaced by the existing "Showing N recordings by [name]" status banner with SHOW ALL. On manual typing, filter applies dynamically. On Enter, input transforms into the same banner. Clicking the banner returns to editable input with the term pre-filled. Filter matches against any visible metadata on recording rows — all performer names, album name, recording year. Side benefit: addresses the scale problem for heavily-recorded works (Beethoven 5 has hundreds of MB recordings; Bach BWV 1041, Vivaldi *Four Seasons*, Pachelbel Canon are similar).
16. **Recording year provenance on Work detail.** *Resolved:* sourced from MusicBrainz Recording's first-release-date when MB enrichment is available. Displays original-recording dates correctly for reissues — a 1962 Karajan recording released in a 2010 box set displays as 1962, not 2010. Falls back to album release date when MB data isn't available.
17. **Grouping recordings on Work detail.** *Resolved:* recordings are grouped by MusicBrainz Recording ID when MB enrichment is present, else by the heuristic `(Work + conductor + ensemble + recording_year)`. Each recording collapses its movements into a nested ordered list beneath the header row. The source album is **not** the grouping key — a single recording can be re-released on multiple albums; the expanded view shows the source album as a separate link (`→ Album Title (year)`) so the user can navigate to the album context, but the grouping is per-performance, not per-release. Two recordings by the same conductor and ensemble from different years appear as separate rows (e.g. Karajan/BPO 1962 and Karajan/BPO 1977 are distinct performances of Beethoven's 5th).
18. **Sort order on Work detail recordings.** *Resolved (gap plus intent):* the Work detail recordings list has **no sort control** — the controls row holds a text filter and a date range filter and nothing else. There is also no sort in the current data path (frontend `getWorkRecordings` returns rows in whatever order the source hands them over; the mock fixtures happen to be in ascending year but nothing enforces it). This is deliberate at the UI level: alternative sorts on a single Work's recordings are not useful enough to justify a control. **Intent for the real implementation:** order by recording year ascending (oldest first) so chronological order shows the interpretive history of the Work naturally (1962 Karajan → 1977 Karajan → 1979 Bernstein reads as an arc). Conductor name as a secondary key for same-year ties (which exist in the current fixtures — the Archduke Trio fixture has a 1979 recording that would clash with a same-year Beethoven 5 recording under a shared performer). Note the asymmetry with the Works tab and the Composer detail works list, both of which do have deliberate sort orders (Works tab defaults to composer then catalog number; Composer detail sorts by catalog number so Op. / BWV / K. order is preserved, empty catalogs last).
19. **Related Works section on Work detail.** *Resolved:* Work detail has a Related Works section below the recordings list that expresses the arrangement chain in both directions. On an arrangement Work, it shows "This is an arrangement of..." with a link back to the source Work (from `arrangement_of`). On a source Work that has arrangements, it shows "Arrangements of this Work..." with links out to the derivative Works (reverse lookup on `arrangement_of`). Renders nothing when a Work has no arrangement links either way, so most Works do not show the section. The arrangement paths exist in the design but are currently unexercised in the mock fixtures; will fire naturally once real Work data with `arrangement_of` populated flows in from MB enrichment.
20. **"More info" appears on movement menu but not recording menu.** *Resolved:* movements are first-class Track entities with a canonical detail page. Recordings are emergent groupings, not entities, so they have no canonical detail page; every "tell me more" path is already covered by other menu entries.
21. **Classical context menus reuse standard multi-artist suppression to make room for the performer submenu.** *Resolved:* the standard MA context-menu builder suppresses the "Go to artist" entry when a track has more than one artist. Rather than fighting that rule, the classical menu construction **deliberately triggers it**: the synthetic Track built for a classical row pushes composer + every performer into a single `artists[]` array, guaranteeing more-than-one so the generic "Go to artist" is suppressed. That clears a slot, into which classical entries are then spliced — Go to composer, Go to work (omitted if workless per #38), and a Go to performer submenu populated from the classical credits. So the pop-music rule is preserved unchanged; the classical view uses it as a mechanism rather than replacing it. There is a real coupling here between the credit-packing in `synthesiseTrack` and the menu builder's suppression rule: break that coupling and a redundant menu entry reappears.
22. **Where Composer detail lives in the URL scheme.** *Resolved:* `/classical/composer/{artist_id}`. Same Artist entity as the standard Artist detail but under a classical-aware route that triggers the composer-centric view.
23. **Favouriting a recording.** *Resolved:* favouriting a recording is a multi-write under one user action — favourites all movement tracks that make up the recording atomically. Un-favourite is the inverse. See implementation note for Stage 3.
24. **Handling missing `MOVEMENTNUMBER` when `MOVEMENTNAME` and `MOVEMENTTOTAL` are present.** *Resolved:* parser attempts to infer the number from the movement name's leading roman numeral or number ("I.", "II.", "1.", "2:"). Falls back to file order within the source directory if parsing fails.
25. **Composer credit inheritance from album to track.** *Resolved:* no automatic inheritance. Composer must be explicitly present per-track. Album-level composer credit is stored on `Album.credits` but does not propagate to `Track.credits`.
26. **`WORK` tag with unusual case / whitespace.** *Resolved:* parser normalises via `unicodedata.normalize('NFC', s).strip()` and case-folds for dedup matching. Storage preserves original case.
27. **Multi-value `MUSICBRAINZ_WORKID` and `WORK` tags.** *Resolved:* Picard writes semicolon-separated values when a Recording is linked to more than one Work in MusicBrainz — typically parent + movement or arrangement + source. Parser splits on `; `; the **last entry is the canonical primary** (Picard's convention is most-general → most-specific). Earlier entries resolve via Stage 6 MB enrichment into `Work.parent_work` or `Work.arrangement_of`. Without MBIDs, a multi-value `WORK` tag is ambiguous; parser falls back to last-value-as-primary.
28. **Within-track artist name resolution (substring-only carve-out).** *Resolved:* the general rule (Matching policy: "Canonical entity resolution via MBID") is that fuzzy matching is not attempted. One **deliberate carve-out**: within a single track, when one credit is MBID-anchored and another credit on the same track has a text-only name that is a **substring** of the MBID-canonical name, the parser merges the text-only credit onto the canonical Artist entity. Catches the common honorific / formal-name variation case. The carve-out is intentionally narrow: within a single track only, substring-match only (no Levenshtein / token / phonetic), anchored by at least one MBID on the track.
29. **Creator roles (`LYRICIST`, `ARRANGER`) excluded from the Performers tab.** *Resolved:* the Performers tab surfaces **performing roles only** — `CONDUCTOR` / `ORCHESTRA` / `ENSEMBLE` / `CHOIR` / `SOLOIST` / `PERFORMER`. Creator roles are visible on Track detail's credits panel and on Recording rows but do **not** appear in any Classical-view browse tab. Considered putting them under the Performers / "Other performers" chip, rejected because it's a semantic mismatch (lyricists don't perform) and no major classical streaming service exposes librettist/lyricist as a browse axis.
30. **Per-entity `is_classical` computed fields.** *Resolved:* `Track.is_classical`, `Album.is_classical`, and `Artist.is_classical` are exposed as derived boolean fields on the wire, computed server-side per the Classification policy rules. Clients treat them as read-only. Fields are derived/cached, not stored canonical state, so recomputation on credit changes is Stage 3's implementation responsibility.
31. **Extended Track credits panel deferred to future polish.** *Resolved:* the structured role-typed credits panel on Track detail is **deferred**. Stage 7 ships without it. Track detail continues to use the existing `metadata.performers` flat-string display, with `Track.is_classical` gating any classical-specific UI. **Side effect:** `LYRICIST` and `ARRANGER` credits become **invisible in the UI** until the panel ships — the data exists in `track_artists`, available via the API, but no Track-detail surface currently renders it. Acceptable trade-off; the panel is fully additive when it lands.
32. **Per-performer scoping of counts on Performer detail pages.** *Resolved:* counts on a Performer detail page (header summary stats, per-Work `recording_count`, per-collaborator counts) are **scoped to this performer's contributions**, not library-wide totals. Same principle as Decisions log #15. Stage 3d API endpoints serving performer-scoped routes must compute counts with the performer filter applied.
33. **"Other tracks" sections on Composer and Performer detail pages.** *Resolved:* a composer or performer may have credits on tracks that lack `Work` linkage. To prevent empty / partially-empty detail pages, both Composer detail and Performer detail render a secondary **"Other tracks"** section below the canonical Works list. Both sections are hidden when empty. The "Other tracks" surface is **local to detail pages only** — the Classical view's Works tab stays Work-centric and free of synthetic Work entities. Considered creating synthetic `UNKNOWN` Work entities (rejected — pollutes the global Works tab). Considered tightening tab indices to exclude entities whose credits are all Workless (rejected — hides legitimately classical content). Sort options: Name (default), Year, Date added.
34. **Work-creation gated by classical-context signal.** *Resolved:* an early draft treated `MUSICBRAINZ_WORKID` or `WORK` as sufficient on its own. Real-world Picard behaviour breaks this: with "Use track relationships" enabled, Picard writes `MUSICBRAINZ_WORKID` for **rock / pop / jazz tracks** too — MB models a Beatles or Led Zeppelin song as a Work the same way it models a Beethoven symphony. Without gating, every Picard-tagged rock album would create Work entities and trigger classical classification, polluting both the Works browse tab and the Classical view's track index. **Fix:** Work creation now requires a `MUSICBRAINZ_WORKID` / `WORK` / composer-plus-movement signal **plus** a classical-context corroborator — classical genre on track / album / `artist.nfo` / `album.nfo`, `is_classical=1` tag, or (at Stage 6 enrichment) MB-confirmed classical Work type. Without classical context, the `MUSICBRAINZ_WORKID` is **preserved on the track row** for re-evaluation but no `Work` entity is created and no classification cascade fires.

35. **Serif typography reserved for classical identity text.** *Resolved:* the Classical section uses a scoped `--font-classical-serif` CSS custom property (Roboto Serif, ui-serif, Georgia fallback chain, `font-optical-sizing: auto`) applied to identity text only — the section title, tab labels, composer names, work titles, and movement titles. Movement titles are the only italic serif in the whole application ("I. Allegro con brio" set like a score marking). Ordinary UI text, chrome, and controls stay in the default sans-serif. Rationale: evokes a concert programme or liner notes without changing the app's overall visual identity. The custom property is scoped to `.classical-typography` on the shell so no non-classical view inherits it. Consumers rewriting the frontend should preserve the identity-only rule; applying serif to chrome breaks the effect.

36. **Nav entry conditionally disabled via `hasClassicalContent` probe.** *Resolved:* the "Classical" sidebar entry is disabled when `store.hasClassicalContent === false`. Same pattern as Audiobooks / Podcasts. The store field is tri-state: `undefined` (not yet probed), `true`, `false`. Nav is disabled only for explicit `false` — an undefined value keeps the entry enabled so the section isn't hidden while the probe is in flight. `hasClassicalContent()` is called once at app initialisation from `App.vue` after the API bootstraps, not per-render.

37. **URL is source of truth for tab and contextual filter; sort and search are local.** *Resolved:* filter state that identifies "what am I looking at" lives in the URL (active tab as path segment, performer-context filter as `?filterByArtistId=X`, role chip as `?role=conductor`). Filter state that is incidental or transient (typed search term, sort order) stays as local component state. The asymmetry is deliberate: a filtered performer-view is worth sharing via URL; a half-typed search term is not. Routing consequences: bare `/classical` redirects to the remembered tab (localStorage `frontend.classical.last_tab`); `router.replace` (not `push`) is used for chip changes so browser history doesn't pile up with chip clicks; "All" clears the query param entirely rather than encoding `role=all`.

38. **Menu absence over greyed-out entries.** *Resolved (UX principle for context menus):* when a classical menu entry has nothing to point at (a workless track's "Go to work", a composer submenu for a track with no composer credit, an empty performer submenu), the entry is **omitted entirely** rather than rendered greyed-out. Applies uniformly to `gotoComposer`, `gotoWork`, and the `Go to performer` submenu. Reasoning: greyed entries add visual noise and imply the user did something wrong; absence just presents what is available. Menu builders return `null` for these entries and the frame filters them out.

39. **Composer detail is a catalogue (navigation only); Performer detail is interactive.** *Resolved:* the Works list on Composer detail is a **catalogue view** — rows navigate but have no play, favourite, or context menu affordances. Reasoning: a composer's works page is for browsing what exists, not for immediate playback of specific recordings. The Works Performed list on Performer detail is the **inverse**: each row carries a right-click menu and a `ClassicalRowActions` cluster (library / favourite / play / overflow). Reasoning: a performer's page is about their recordings, so acting on those recordings from the list is the natural flow. Consequence: on Performer detail, the work row shows composer as a small muted caption *above* a bold work title — inverting the usual "Composer: Title" ordering — because the title is what the user is scanning for; the composer is context.

40. **Recording card credit hierarchy.** *Resolved:* recordings render two credit tiers with weight and size expressing importance. **Bold first line:** conductor / orchestra, followed by year in parentheses at normal weight, followed by duration in square brackets with `font-variant-numeric: tabular-nums`. **Lighter, smaller second line:** performer credits (soloists, ensembles, choirs). Rationale: conductor and orchestra are the recording's identity; soloists are subordinate detail. A missing name falls back to the raw ID rather than vanishing — an integration gap is made visible rather than silently swallowed.

41. **Primary click semantics vary by row type.** *Resolved:*

    | Row | Left click | Right click |
    |---|---|---|
    | Recording header | expand / collapse | recording menu |
    | Movement | play that movement (`.stop` on event) | movement menu (`.stop`) |
    | Other track | play the track | track menu |
    | Composer's work | navigate | (no menu) |
    | Performer's work | navigate | recording menu |

    Playing a whole recording is done from the explicit play button on the recording header, never from the header click. Movement-row events use `.stop` because they are nested inside the expanded recording body and would otherwise bubble to the recording's expand toggle.

42. **RecordingsFilter is one control in two states.** *Resolved (refines #15a):* the recordings filter and the "you are looking at a subset" banner are the same control in two states, not two components stacked. Editable state: plain search input with placeholder "Filter recordings…". Committed state: status banner (e.g. "Showing 3 recordings by Karajan") with a Show all button on the right. A commit is either user-driven (Enter, kind `"text"`) or arrives from context (navigating in from a performer, kind `"performer"`). Clicking the banner text returns to editable input pre-filled with the term. Matching is diacritic-blind substring via the shared `normalizeForFilter` helper, over a memoised haystack per recording (conductor / orchestra / performer names / credit names / source album / year as text). Empty-match state uses a `genericNoMatch` fallback message when other filters (date range) are also narrowing the list, so the message never wrongly blames the typed term.

43. **YearRangeFilter design.** *Resolved:* the date range filter on the Works tab (composition year) and Work detail (performance year) is a two-input year range with a shared "mat" (a rounded surface holding label + both boxes) so the pair reads as one control rather than two loose inputs beside a caption. Behaviour:

    - Placeholders are the real earliest and latest years present in the list, so the control advertises the span the library actually covers.
    - Part-typed years settle on blur AND on Enter, never while typing.
    - Left box pads with zeros, right box pads with nines: `19` becomes 1900 in the left box and 1999 in the right, so a part-typed year stands for the span it opens.
    - Bounds are read low-to-high regardless of which box they were typed into.
    - Items with no year on file drop out as soon as either bound is set (user's explicit choice).
    - Clear (×) is always laid out (hidden when inactive) so the mat never changes width as a range is typed.
    - Digits only, four characters max, sanitised on input.
    - Mat border takes accent colour when the range is active.
    - Accessibility: `role="group"` + `aria-labelledby` on the mat, generic per-box labels ("Earliest year", "Latest year") because the same control means composition year on one page and performance year on the other.
    - The range logic lives in a shared `useYearRange` composable so both pages behave identically.

    Which date each page filters on:
    - Works list: `Work.composition_year`.
    - Work detail: `recording.year` (performance year, per #16).
    - Show all on the filter banner and navigating to another work both clear the range.

## Correction to earlier entries

- **Decision #22 URL scheme.** The composer detail route is `/classical/composers/:id` (plural), matching the tab path segment. Same pluralisation for `/classical/works/:id` and `/classical/performers/:id`. Route `meta.hideTabs` is a misnomer — it does not hide the tab bar, only drops content padding so a detail page's InfoHeader banner can run full-bleed.
- **Sort defaults on Works tab.** Default sort is by composer, and within a composer by catalog number (canonical Op. / BWV / K. order), with empty catalogs sorting last. Options: Composer / Title / Year composed / Recording count.
- **Sort defaults on Composers tab.** Default is `sort_name` (surname-first when the SORT tag is present, else display name). Options: Sort name / Name / Work count.
- **Sort defaults on Performers tab.** Default is `name`; performers have no `sort_name` field. Options: Name / Recording count only.

## Open questions

1. **`Track.section` (Roon `SECTION` equivalent).** Roon supports a three-level hierarchy `WORK → SECTION → PART` for operas (e.g. "Le nozze di Figaro" → "Act 1" → "Cinque... dieci..."). Our model handles two levels. For Roon-style opera tagging, an additive `Track.section: str | None` field would capture the intermediate level cheaply. Defer until a concrete consumer needs it.
2. **`Track.performance_id` (Roon `WORKID` equivalent).** Would disambiguate multiple recordings of the same Work on a single album when the heuristic (Work + conductor + ensemble grouping) can't tell them apart. **No verified real-world example identified**; deferred until a user reports an album where the heuristic fails.
3. **Movements view on Work detail (group-by toggle).** A *Group by* toggle at the top of the recordings list — Recording (default) vs. Movement — would let users transpose the data to compare the same movement across recordings ("how does Furtwängler's Adagio compare to Karajan's?"). Frontend-only addition. Deferred to post-Stage 10 polish unless demand surfaces.

## References

### Tag standards and canonical mappings

- MusicBrainz Picard tag mapping documentation
- Vorbis comment recommendations
- iTunes movement tag conventions (`MVNM` / `MVIN` / `©mvn` / `©mvi`)
- MusicBrainz Work / Recording / Artist entity documentation

### Third-party tagging conventions and tools

- Roon classical tagging conventions
- Classical Extras (Picard plugin) — read for legacy compatibility, not recommended for new tagging
- Apple Music Classical tagging expectations

### Community discussions

- Music Assistant Discord "Better Classical Music Support" threads
- Roon classical community forum discussions

### UX precedent

- Roon Radio classical view
- Apple Music Classical navigation model
- IDAGIO composer / work / performer taxonomy

## Out of scope (future work)

- Streaming provider-specific classical enhancements beyond what their APIs expose.
- Curated classical playlists / commissioned composer artwork (content-sourcing concerns, not model).
- Score display / notation.
- Structured programme notes.
- Fuzzy cross-track artist name resolution.
- MA-wide search infrastructure improvements (FTS5, ranking) — separate initiative.
