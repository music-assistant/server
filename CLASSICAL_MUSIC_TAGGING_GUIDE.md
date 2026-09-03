# Classical music tagging guide

A practical guide to tagging your classical music library so it works well in Music Assistant's Classical view (Composers / Works / Performers browse + Work-grouped playback). This guide is **tagger-agnostic** for the first two-thirds — apply the same fields whether you're using Mp3tag, Kid3, foobar2000's tag editor, or hand-editing FLAC metadata. The last section covers how to do most of this automatically with MusicBrainz Picard.

## Why tagging matters for classical

MA's Classical view depends on **structured metadata** that tells it:

- Who composed the music (separate from who's performing it).
- Who's conducting, which orchestra is playing, which soloists are featured.
- What composition (Work) the recording is of.
- Which movement of that composition the track represents, and where it sits in the movement sequence.
- Whether the track is classical in the first place.

The standard music tags (Artist, Album, Title, Genre) carry none of this distinctly — they get squashed together. Classical-specific tags let MA tell them apart and surface them in the right places (Composers tab, Performers tab, Work detail page with movements sequenced for gapless playback, etc.).

The richer your tagging, the better the experience. MA degrades gracefully when tags are missing — tracks with only a genre and composer still surface — but multi-movement playback, role-typed credit browsing, and cross-recording comparison need the full set.

## The essential tags

These are the fields MA reads. The names below are the **canonical Vorbis comment names** (used by FLAC, Ogg, etc.). Format-specific equivalents are in the table at the end of this guide — your tagger should let you set these values regardless of file format.

### Composer

```
COMPOSER: Ludwig van Beethoven
```

Always use the canonical full name. Don't abbreviate ("L. van Beethoven", "Beethoven, L.") or anglicise ("Bela Bartok" vs "Béla Bartók"). Consistency across your library matters — MA falls back to name matching when MusicBrainz IDs aren't set, so spelling drift produces duplicate Artist entries.

### Conductor

```
CONDUCTOR: Herbert von Karajan
```

When applicable (most symphonic, opera, choral recordings). Chamber music typically has no conductor — leave blank.

### Performers (with role / instrument)

The `PERFORMER` tag uses Picard's parens convention: `Name (role-or-instrument)`. One performer per entry; most tag editors support multi-valued PERFORMER tags.

```
PERFORMER: Berliner Philharmoniker (orchestra)
PERFORMER: Vienna State Opera Chorus (choir)
PERFORMER: Emerson String Quartet (ensemble)
PERFORMER: Anne-Sophie Mutter (violin)
PERFORMER: Yo-Yo Ma (cello)
PERFORMER: Daniel Barenboim (piano)
```

The text in parens is what MA uses to classify the credit:

| Parens contains | Role |
|---|---|
| `orchestra`, `philharmonic`, `symphony orchestra` | ORCHESTRA |
| `choir`, `chorus`, `chorale`, `schola` | CHOIR |
| `ensemble`, `quartet`, `quintet`, `trio`, `consort` | ENSEMBLE |
| A specific instrument (`violin`, `piano`, `soprano`, `cello`, `harpsichord`, …) | SOLOIST with that instrument |
| Empty parens or unrecognised text | Generic PERFORMER (with the parens text preserved) |

### Work title

```
WORK: Symphony No. 5 in C minor, Op. 67
```

**Just the composition title**, NOT including the movement. The movement goes in `MOVEMENTNAME` (below). Don't write `WORK: "Symphony No. 5: I. Allegro con brio"` — that conflates work and movement into one string.

For opera arias / song-cycle entries, use the parent work title: `WORK: La bohème` or `WORK: Winterreise`.

### Genre

```
GENRE: Classical
```

Plus optional sub-genres (`Baroque`, `Opera`, `Symphony`, `Chamber Music`, etc.). Most tag formats support multi-valued GENRE entries. Setting `Classical` is the simplest classification signal — MA's Classical view picks up any track tagged with it (or a classical-family sub-genre).

### Movement metadata (for multi-movement works)

```
MOVEMENTNAME:   I. Allegro con brio
MOVEMENTNUMBER: 1
MOVEMENTTOTAL:  4
```

These three tags together enable proper multi-movement playback: gapless within a Work, in correct order, with the right movement labels.

- `MOVEMENTNAME` is the title of *this* movement (e.g. "I. Allegro con brio", "II. Andante con moto"). Don't include the parent work title.
- `MOVEMENTNUMBER` is an integer — `1`, `2`, `3`, `4`.
- `MOVEMENTTOTAL` is the total number of movements in the parent Work.

For single-movement works (Pachelbel Canon, Albinoni Adagio, *Spiegel im Spiegel*, individual opera arias), leave the movement fields blank.

## MusicBrainz IDs — strongly recommended, even when tagging manually

MBIDs are unique identifiers MusicBrainz assigns to artists, works, and recordings. They're the **canonical resolution mechanism** in MA — when a tag carries both a name AND an MBID, the MBID determines the canonical entity. This solves several real problems:

- **Spelling variants resolve cleanly.** "Béla Bartók" and "Bela Bartok" (with and without diacritics) become the same Artist entity if both tags carry the same `MUSICBRAINZ_ARTISTID`. Without MBIDs, they're two separate Artists.
- **Multi-language works link correctly.** "La bohème" / "La boheme" / "Die Bohème" all collapse to one Work entity via the Work MBID.
- **Cross-track linking works.** Karajan on one track and Karajan on another are the same conductor only if they share an MBID; otherwise MA can't be sure (avoids accidental conflation across genuinely different people with similar names).
- **MusicBrainz enrichment becomes possible.** With the MBID present, MA can later fetch additional metadata (composer relationships, parent Work links, Wikipedia descriptions, etc.) without guessing.

### Even when tagging manually, set these MBIDs

You can look up MBIDs on **musicbrainz.org** without installing Picard. Search for the entity (composer, conductor, orchestra, work), open the page, and copy the UUID from the URL — e.g. `musicbrainz.org/artist/1f9df192-a621-4f54-8850-2c5373b7eac9` gives Beethoven's Artist MBID.

The most useful MBIDs to set:

```
MUSICBRAINZ_ARTISTID:       <album artist's MBID>
MUSICBRAINZ_ALBUMARTISTID:  <album artist's MBID>
MUSICBRAINZ_COMPOSERID:     1f9df192-a621-4f54-8850-2c5373b7eac9
MUSICBRAINZ_WORKID:         d03bff61-26fc-301b-98ac-4d8e85771cbc
```

For prolific composers (Bach, Beethoven, Mozart, etc.) the composer MBID is the most leveraged — set once per composer, applies across every track they wrote. Bookmark them. Conductors and orchestras you've collected a lot of are similar high-leverage candidates.

If you can't find an MBID, that's fine — MA falls back to name matching. Just be **strictly consistent** with the name spelling.

## Multi-value tags

When a field can legitimately carry multiple values (multiple lyricists on an opera, multiple composers on a collaborative work), Picard's convention is **semicolon-space separated** within a single tag, OR multiple separate tag entries if the format supports it.

```
LYRICIST: Giuseppe Giacosa; Luigi Illica
COMPOSER: Lennon; McCartney   (rare for classical, common for pop)
```

This applies to any credit-bearing tag: `COMPOSER`, `CONDUCTOR`, `LYRICIST`, `ARRANGER`, `MUSICBRAINZ_ARTISTID`, `MUSICBRAINZ_WORKID` (multi-Work case, see below).

### Multi-Work scenarios

For a track that's a recording of an arrangement or a movement-within-a-work, MB may have **two Work MBIDs** linked: one for the parent Work, one for the specific movement Work. Semicolon-separate them, **most general first → most specific last**:

```
MUSICBRAINZ_WORKID: <parent symphony MBID>; <movement Work MBID>
WORK: Symphony No. 5 in C minor, Op. 67; Symphony No. 5 in C minor, Op. 67: I. Allegro con brio
```

MA's parser takes the last entry as the primary Track.work (the more specific one) and links earlier entries via parent/arrangement relationships.

## Common pitfalls

A short list of things to watch for:

1. **Don't put the composer in the ARTIST field** unless they're literally performing on the track. The ARTIST field is for the headline performer (the conductor, soloist, ensemble, or whichever credit is the album's billed credit). The composer goes in `COMPOSER`. Putting Beethoven in ARTIST muddles the data.
2. **WORK is composition only.** Don't append movement info — that goes in MOVEMENTNAME.
3. **Movement metadata is essential for multi-movement playback.** Skipping MOVEMENTNUMBER / MOVEMENTTOTAL means MA can't sequence movements gaplessly. Many tag editors don't write these by default — check after saving.
4. **Be consistent with name spelling** across your library. Without MBIDs, name matching is all MA has. Use canonical full names (look up on MusicBrainz if unsure).
5. **One album-artist convention per release.** Don't mix "Various Artists" on one classical compilation and the composer's name on another similar compilation — pick a convention.
6. **Skip the Classical Extras Picard plugin** for new tagging. It writes non-standard tag names (`groupheading`, `top_work`, `is_classical`, `MOVEMENT`) which MA *does* read as fallbacks, but it also destructively rewrites your ARTIST field with potentially unwanted values. Standard Picard with iTunes movement tags enabled (covered below) gives MA everything it needs without that risk.

## Sort names (optional, for classical-convention alphabetisation)

By default MA uses the display name as the sort name — `Ludwig van Beethoven` sorts under "L", not "B". To get classical-convention surname-first sorting on the Composers tab's "Sort name" ordering, set the matching SORT tag explicitly:

| Display tag | Sort tag |
|---|---|
| `COMPOSER` | `COMPOSERSORT` |
| `ARTIST` | `ARTISTSORT` |
| `ALBUMARTIST` | `ALBUMARTISTSORT` |

Use `Lastname, Firstname` form (`Beethoven, Ludwig van`, `Mozart, Wolfgang Amadeus`). MA uses these verbatim.

## Optional but useful

```
is_classical: 1
```

Explicit Classical flag. The strongest classification signal — overrides any genre ambiguity. Useful for tracks in mixed-genre album contexts (a single classical track on a "best of film scores" album, for example). Originally from the Classical Extras convention but MA reads it directly.

```
LYRICIST: <librettist name>
ARRANGER: <arranger name>
```

For operas, song cycles, and arrangement-recordings. MA stores these but doesn't surface them in Classical browse tabs (they show on Track detail credit panels).

## Format-specific tag names

The names above are Vorbis comment names (FLAC, Ogg). Equivalents for other formats:

| Field | Vorbis (FLAC/Ogg) | ID3v2 (MP3) | MP4 (m4a/m4b/aac) |
|---|---|---|---|
| Composer | `COMPOSER` | `TCOM` | `©wrt` |
| Composer MBID | `MUSICBRAINZ_COMPOSERID` | `TXXX:MusicBrainz Composer Id` | freeform `MusicBrainz Composer Id` |
| Conductor | `CONDUCTOR` | `TPE3` | `----:com.apple.iTunes:CONDUCTOR` |
| Performer (with parens) | `PERFORMER` (multi-valued) | `TMCL` (instrument/name pairs) | freeform `Performer` |
| Work title | `WORK` | `TIT1` (or `TXXX:WORK`) | `©wrk` |
| Work MBID | `MUSICBRAINZ_WORKID` | `TXXX:MusicBrainz Work Id` | freeform `MusicBrainz Work Id` |
| Movement name | `MOVEMENTNAME` | `MVNM` | `©mvn` |
| Movement number | `MOVEMENTNUMBER` | `MVIN` (number part) | `©mvi` |
| Movement total | `MOVEMENTTOTAL` | `MVIN` (total part) | `©mvc` |
| Lyricist | `LYRICIST` | `TEXT` | freeform |
| Genre | `GENRE` | `TCON` | `©gen` |

Most modern tag editors handle the format-specific mapping automatically when you set the canonical field name — but if you're working with MP3s and the tag isn't writing as expected, check that your editor maps to the ID3 frame correctly.

## Using Picard to do this automatically (recommended workflow)

For libraries above a handful of albums, doing this manually is tedious. **MusicBrainz Picard** automates almost everything in this guide. Recommended setup:

1. **Install Picard** from picard.musicbrainz.org. Current versions handle classical metadata well.
2. **Enable iTunes-style movement tags.** In Picard preferences → Tags / Tag Compatibility (the exact menu path varies by version), enable the option for writing `MOVEMENTNAME` / `MOVEMENTNUMBER` / `MOVEMENTTOTAL` / `SHOWMOVEMENT`. Without this, Picard won't write the movement-sequencing tags MA needs.
3. **Do NOT install the Classical Extras plugin.** Standard Picard writes everything MA needs. Classical Extras destructively rewrites `ARTIST` and has other configuration quirks — it's been deliberately advised against for new tagging.
4. **Load your files** in Picard, cluster them by album, match each cluster against MusicBrainz, and save. Picard fetches all the metadata (composer, conductor, performers, work, movements, MBIDs) from MB and writes the standard tags.

### What Picard handles automatically

When matched against MusicBrainz, Picard writes (without further intervention):

- Composer and composer MBID
- Conductor and conductor MBID
- Performer credits with proper parens convention (orchestra, choir, ensemble, soloist with instrument) — derived from MB's Recording-Artist relationships
- Work title and Work MBID (including the parent Work and any movement-Work)
- Movement name / number / total (with iTunes tags enabled — see step 2)
- Lyricist, arranger where applicable
- Standard MBIDs for everything: Artist, Album, Track, Recording

For a deep classical library, this is dramatically faster than manual tagging and produces uniformly consistent results.

### Worked example

For a Karajan/Berlin Philharmonic recording of Beethoven Symphony No. 5 movement I, Picard matched against MB writes (roughly):

```
COMPOSER:                    Ludwig van Beethoven
MUSICBRAINZ_COMPOSERID:      1f9df192-a621-4f54-8850-2c5373b7eac9
CONDUCTOR:                   Herbert von Karajan
PERFORMER (multi-valued):    Berliner Philharmoniker (orchestra)
                             Herbert von Karajan (conductor)
WORK:                        Symphony No. 5 in C minor, Op. 67
MUSICBRAINZ_WORKID:          d03bff61-26fc-301b-98ac-4d8e85771cbc
MOVEMENTNAME:                I. Allegro con brio
MOVEMENTNUMBER:              1
MOVEMENTTOTAL:               4
SHOWMOVEMENT:                1
GENRE:                       Classical
```

No manual intervention needed. The result feeds MA's Classical view cleanly.

### When Picard's data is thin

Some MB releases have weaker structural data than others — compilations (`Greatest Classical Hits` type albums) often lack Recording-Work relationships, so Picard won't have a Work to write. Single-movement works (Pachelbel Canon, Albinoni Adagio) don't have movement metadata to write. This is just how the source data is — MA handles thin tags gracefully (the track still surfaces in the Classical view via the genre rule; it just won't have full role-typed credits or Work grouping).

If you have a thin-tagged track you want richer credits on, you can hand-edit the MA-recognised tags after Picard's pass to fill in the gaps. The two paths compose — Picard does the bulk; manual fills in the rest.

## Verification

After tagging (manual or Picard), spot-check that your edits stuck:

1. Open the file in your tag editor and confirm the values you intended are written.
2. For Picard-tagged files, verify the iTunes movement tags are present (search for `MOVEMENTNAME` / `MVNM` / `©mvn` depending on format). If they're absent, the iTunes movement tag option in Picard preferences isn't enabled — go back and enable it.
3. Load the file into Music Assistant and check that the track appears in the Classical view with the expected composer, performers, and movement structure.

That's it. The structure is straightforward; the value comes from applying it consistently across your library.
