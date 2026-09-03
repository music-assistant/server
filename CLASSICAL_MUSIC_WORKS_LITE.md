# Classical support — "Works-lite" variant

A scaled-back landing for classical/role-aware metadata that fits the dev-team direction ("enhance the Artists view with new artist roles/types, for all music — don't transform MA into a classical app") while keeping a clean, non-destructive upgrade path to the full proposal in [`CLASSICAL_MUSIC_MODEL_SPEC.md`](CLASSICAL_MUSIC_MODEL_SPEC.md) if demand justifies it later.

## What this lands

Two things, both of which generalise beyond classical:

1. **Role-typed credits** (`ArtistRole` + `Credit`) surfaced in the existing Artists view — composer / conductor / soloist / performer-with-instrument, etc. Works for all genres (a rock band's credits use the same shape).
2. **Composer-page work grouping** — on a composer's artist detail page, their tracks are grouped by composition (multiple recordings of the same work clustered, movements in order) using lightweight track-level fields. No Works table, no Works browse tab, no Work detail page.

## What this does *not* build

- No `Work` MediaItem, `works` table, or `WorksController`.
- No standalone "Works" browse tab (compositions are reachable only through a composer's page).
- No Work detail page (no per-composition image / description / catalog-number search / cross-library "other recordings" aggregate).
- No parent-work / arrangement relationships.
- No `Period` browse, no `is_classical` classification, no separate Classical view.

## Model (music-assistant-models)

### Kept from the full Stage 1 design (Tier A — role credits)

```python
class ArtistRole(StrEnum):
    MAIN_ARTIST = "main_artist"
    COMPOSER = "composer"
    LYRICIST = "lyricist"
    ARRANGER = "arranger"
    CONDUCTOR = "conductor"
    ORCHESTRA = "orchestra"
    ENSEMBLE = "ensemble"
    CHOIR = "choir"
    SOLOIST = "soloist"
    PERFORMER = "performer"
    # For an all-music feature, consider also: PRODUCER, REMIXER, FEATURED, DJ.

@dataclass(kw_only=True)
class Credit(DataClassDictMixin):
    artist: Artist | ItemMapping
    role: ArtistRole
    instrument: str | None = None   # e.g. "guitar", "violin", "piano" — NOT a role
    position: int = 0               # ordering within a role group
```

Note: "guitarist" is **not** a role — it's `role=PERFORMER`/`SOLOIST` with `instrument="guitar"`.

### Works-lite track fields (plain fields — no entity, no table)

```python
@dataclass
class Track(MediaItem):
    # ... existing fields ...
    credits: list[Credit] = field(default_factory=list)

    work_name: str | None = None
    work_mbid: str | None = None
    movement_number: int | None = None
    movement_total: int | None = None
    movement_name: str | None = None
```

`Track.work` is deliberately stored as plain `work_name` + `work_mbid` rather than an `ItemMapping`, because with no `works` table an ItemMapping reference would dangle (its `item_id` would resolve to nothing).

## Schema (server)

- `track_artists` / `album_artists`: `role`, `instrument`, `position` columns with the uniqueness index `(owner_id, artist_id, role, COALESCE(instrument, ''))` — already implemented in the Stage 2 work; handles multi-role (Karajan = conductor + main_artist) and multi-instrument (one player, guitar + piano).
- `tracks`: five additive columns (`work_name`, `work_mbid`, `movement_number`, `movement_total`, `movement_name`).
- Index on `tracks(work_mbid)` deferred until the composer-page query proves slow.

## Composer-page grouping query

```sql
SELECT t.*
FROM tracks t
JOIN track_artists ta ON ta.track_id = t.item_id
WHERE ta.artist_id = :composer_id AND ta.role = 'composer'
ORDER BY COALESCE(t.work_mbid, t.work_name), t.movement_number;
```

Group in the controller by `work_mbid` (fall back to normalised `work_name`); order movements within each group. Produces:

```
Beethoven
  Symphony No. 5 in C minor, Op. 67
    ├─ Karajan / BPO (4 movements)
    └─ Kleiber / VPO (4 movements)
  Symphony No. 9 in D minor, Op. 125
    └─ ...
```

A `GROUP BY` over track-level fields — no join to a works table, no Work rows.

## Scale-up path to the full proposal

Scaling Works-lite up to the full `Work`-entity model is **additive schema + one forward backfill + a query swap**. No destructive migration, no column type changes, no re-enrichment.

**Schema — purely additive:**
- `CREATE TABLE works`, `work_artists`, `work_arrangements` — new tables, nothing altered.
- Add `Work` / `WorkType` / `MediaType.WORK` / `ExternalID.MB_WORK` to the model — additive (old consumers fall through on unknown `MediaType`).
- Add `Track.work` as an ItemMapping/FK **alongside** the existing `work_mbid` / `work_name` — additive. Plain fields stay as the denormalised seed, or get dropped later (trivial, non-urgent).

**Data — one forward backfill (same shape as the existing genre-table backfill, `music.py` `prev_version <= 28` block):**
- Scan tracks where `work_mbid` / `work_name` is set, create `Work` rows (dedup by MBID, else composer + normalised name), link tracks to them.
- Forward-only, idempotent, no data loss.

**Code — query swap, not migration:**
- Composer page changes from "group by `work_mbid`" to "join the `works` table".

**Why the stored fields matter:** the `work_mbid` / `work_name` stored in Works-lite *are* the seed for the backfill. Storing them now means the future upgrade is a local backfill, not a re-fetch from MusicBrainz of every track's work. Discarding them would make the upgrade an expensive re-enrichment.

**Honest caveat:** the backfill's dedup quality inherits whatever was stored. Tracks with only a `work_name` (no MBID) dedup fuzzily — "Symphony No. 5" vs "Symphony No 5 in C minor" may create two Work rows needing a later merge. A data-quality cleanup, not a migration blocker; improves over time as enrichment fills in MBIDs.

## Summary

| | Works-lite | Full proposal |
|---|---|---|
| Role-typed credits in Artists view | ✅ | ✅ |
| Composer page groups recordings by composition | ✅ | ✅ |
| Movements ordered within a composition | ✅ | ✅ |
| Standalone Works browse tab | ❌ | ✅ |
| Work detail page (image / description / catalog search) | ❌ | ✅ |
| Parent-work / arrangement relationships | ❌ | ✅ |
| Period browse / is_classical / separate Classical view | ❌ | ✅ |
| New tables required | tracks columns only | works + 2 junctions |
| Upgrade cost from Works-lite | — | additive schema + 1 backfill + query swap |
