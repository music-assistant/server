r"""
Replay the smart fades planner over real track pairs from a server's stored analysis.

Plans the transition of each pair the way playback would, without playing anything, and writes
``pairs.csv`` (one row per pair) and ``summary.txt`` (tiers and overlap lengths, quick fade
triggers, vocal and drum overlap, music style) into ``--out``. To measure a planner change, run
it on both sides of the change with the same databases, seed and buffer; ``--code`` loads
``music_assistant`` from another checkout, for a commit that does not have this script.

Both databases are copied with their -wal/-shm files into a temporary directory, checkpointed
there, and only those copies are read. The given paths are never written to and no other server
data, such as the auth database, is opened.

Usage policy: this reads stored analysis data and library metadata only. It never opens, decodes
or writes audio.

Example, from the repository root::

    python -m scripts.smart_fades_replay \
        --analysis-db ~/.musicassistant/audio_analysis.db \
        --library-db ~/.musicassistant/library.db \
        --pairs random --n 3000 --seed 20261010 --buffer 45 --out /tmp/replay
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import random
import re
import shutil
import sqlite3
import statistics
import sys
import tempfile
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from contextlib import closing
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

import numpy as np

if TYPE_CHECKING:
    import numpy.typing as npt

    from music_assistant.models.audio_analysis import AudioAnalysisData

# ruff: noqa: T201

# (item_id, provider) of an audio_analysis row: the provider domain for a streaming provider,
# the instance id for any other
TrackKey = tuple[str, str]
Row = dict[str, Any]

DEFAULT_CODE = Path(__file__).resolve().parents[1]
# below this much outgoing room playback skips the crossfade (MIN_CROSSFADE_DURATION)
MIN_CROSSFADE_SECONDS = 3.0
TIERS = ("FULL_BLEND", "TEMPO_BLEND", "QUICK_FADE")
SHORT_FADE_SECONDS = 8.0
_BPM_BANDS = ("<=8", "8-12", "12-20", ">20")

# Measuring sticks for the vocal and drum facts. They are fixed here, independent of the
# planner's own limits, so runs on both sides of a planner change measure alike.
WINDOW_BARS = 8
# vocal duty over the window above which a deck sings
DUTY_SINGS = 0.10
# a bar carries a kick when its low-band power reaches this share of the track's reference
KICK_FRACTION = 0.5
# a pair is rhythm-safe when either side has no kick or kicks overlap at most this much
CLASH_LIMIT_BARS = 2.0
# seconds per sample of the kick clash integral
SAMPLE_STEP = 0.02

UNKNOWN_BUCKET = "unknown"
# in tie-break order: equal votes go to the bucket listed first
BUCKETS = (
    "house/electronic",
    "hip-hop",
    "metal",
    "jazz",
    "classical",
    "ambient/downtempo",
    "latin/reggae/world",
    "country/folk",
    "soul/r&b/funk",
    "singer-songwriter/ballad",
    "rock",
    "pop",
)
_BUCKET_KEYWORDS = {
    "house/electronic": (
        "house, techno, trance, edm, electro, electronic, electronica, dance, club, dubstep, "
        "drum and bass, jungle, breakbeat, breaks, garage, rave, hardstyle, hardcore, idm, "
        "eurodance, bassline, big beat, indietronica, nu disco, elektronisch, synthwave"
    ),
    "hip-hop": "hip hop, hip-hop, hiphop, rap, trap, drill, grime, boom bap, gangsta, crunk",
    "metal": "metal",
    "jazz": "jazz, swing, bebop, big band",
    "classical": "classical, orchestral, opera, baroque, symphony",
    "ambient/downtempo": "ambient, downtempo, chill, lounge, trip hop, new age, easy listening",
    "latin/reggae/world": (
        "latin, reggae, reggaeton, ska, dancehall, dub, salsa, samba, bossa, cumbia, dembow, "
        "ragga, flamenco, afro, afrobeat, afrobeats, worldbeat, world, urbano, son cubano, "
        "tropical, isicathamiya, zouk, kizomba"
    ),
    "country/folk": "country, folk, bluegrass, americana, zydeco, cajun, celtic",
    "soul/r&b/funk": (
        "soul, r&b, rnb, rhythm and blues, funk, disco, motown, gospel, new jack swing, quiet storm"
    ),
    "singer-songwriter/ballad": "singer-songwriter, ballad, acoustic, chanson",
    "rock": (
        "rock, punk, grunge, indie, alternative, new wave, britpop, emo, aor, glam, "
        "psychedelic, progressive, blues, rock opera, rock and roll"
    ),
    "pop": "pop, schlager, christmas, musical, variété, kerst",
}
# longest keyword first, so of two keywords ending at the same place the longer one wins
_KEYWORD_ORDER = sorted(
    (
        (keyword, bucket)
        for bucket, keywords in _BUCKET_KEYWORDS.items()
        for keyword in keywords.split(", ")
    ),
    key=lambda item: -len(item[0]),
)

# why a shipped fade is as short as it is, in summary column order
CAUSES = (
    "QF: tempo",
    "QF: meter",
    "QF: beat_grid",
    "QF: re-anchored",
    "rejection -> rescue/fallback/handoff",
    "blend: longer rungs rejected",
    "blend: short top rung",
)
VOCAL_CLASSES = ("both sing", "outgoing only", "incoming only", "neither", "unknown")


@dataclass(slots=True)
class TrackInfo:
    """Library facts of one analysed track; only the name is set for a track not in the library."""

    name: str
    mapped: bool = False
    album: str = ""
    album_id: int | None = None
    # (album_id, disc_number, track_number) of every album the track is on
    album_positions: list[tuple[int, int | None, int | None]] = field(default_factory=list)
    bucket: str = UNKNOWN_BUCKET
    genre_source: str = ""
    genres: str = ""


class PlannerProbe:
    """Runs the loaded checkout's planner as playback does and keeps its internals per plan."""

    def __init__(self) -> None:
        """Wrap the planner's context build and candidate selection where the checkout has them."""
        # music_assistant loads from --code, so it is imported only once that is on sys.path
        from music_assistant.controllers.streams.smart_fades.models import (  # noqa: PLC0415
            SmartFadeNotApplicable,
        )
        from music_assistant.controllers.streams.smart_fades.planner import (  # noqa: PLC0415
            planner as planner_module,
        )

        self.hooks: list[str] = []
        self.context: Any = None
        # per selection pass: every scored entry, and the winner (None when all were rejected)
        self.passes: list[list[Any]] = []
        self.winners: list[Any] = []
        self._not_applicable: type[Exception] = SmartFadeNotApplicable
        self._planner_class: Any = planner_module.SmartCrossFadePlanner
        self._logger = logging.getLogger("scripts.smart_fades_replay.planner")
        self._logger.setLevel(logging.WARNING)
        self._hook_context(planner_module)
        self._hook_selection()

    def plan(
        self, fade_out: AudioAnalysisData, fade_in: AudioAnalysisData, buffer_duration: float
    ) -> tuple[Any, str, str]:
        """
        Plan one transition.

        Returns ``(plan, "plan", "")``, or ``(None, outcome, reason)`` with outcome
        ``not_applicable`` or ``error``.

        :param fade_out: Analysis of the outgoing track.
        :param fade_in: Analysis of the incoming track.
        :param buffer_duration: Seconds of outgoing tail playback would hold.
        """
        self.context = None
        self.passes.clear()
        self.winners.clear()
        planner = self._planner_class(self._logger)
        try:
            return planner.plan(fade_out, fade_in, buffer_duration), "plan", ""
        except self._not_applicable as err:
            return None, "not_applicable", str(err)
        except Exception as err:
            return None, "error", f"{type(err).__name__}: {err}"

    def _hook_context(self, planner_module: Any) -> None:
        """Keep the transition context of each plan."""
        build = getattr(planner_module, "build_transition_context", None)
        if build is None:
            return

        def build_and_keep(*args: Any, **kwargs: Any) -> Any:
            self.context = build(*args, **kwargs)
            return self.context

        planner_module.build_transition_context = build_and_keep
        self.hooks.append("context")

    def _hook_selection(self) -> None:
        """Keep every selection pass's scored entries and its winner."""
        try:
            from music_assistant.controllers.streams.smart_fades.planner.selection import (  # noqa: PLC0415
                CandidateSelector,
            )
        except ImportError:
            return
        select = CandidateSelector.select

        def select_and_keep(selector: Any, candidates: Any, ctx: Any) -> Any:
            self.passes.append([])
            winner = select(selector, candidates, ctx)
            self.winners.append(winner)
            return winner

        CandidateSelector.select = select_and_keep  # type: ignore[assignment]
        self.hooks.append("select")
        score = getattr(CandidateSelector, "_score", None)
        if score is None:
            return

        def score_and_keep(selector: Any, candidate: Any, ctx: Any) -> Any:
            entry = score(selector, candidate, ctx)
            if self.passes:
                self.passes[-1].append(entry)
            return entry

        CandidateSelector._score = score_and_keep  # type: ignore[assignment]
        self.hooks.append("score")


class _KickTrack(NamedTuple):
    """Per-bar kick presence of one track, in media time."""

    starts: npt.NDArray[np.float64]
    ends: npt.NDArray[np.float64]
    kick: npt.NDArray[np.bool_]


def main(argv: list[str] | None = None) -> int:
    """
    Replay the pairs and write ``pairs.csv`` and ``summary.txt``; returns the exit code.

    :param argv: Command line arguments; defaults to ``sys.argv[1:]``.
    """
    args = _parse_args(argv)
    code_file = _import_music_assistant(Path(args.code))
    print("music_assistant loaded from", code_file, flush=True)
    with tempfile.TemporaryDirectory(prefix="smart_fades_replay_") as tmp:
        analysis_db = _snapshot_db(Path(args.analysis_db).expanduser(), Path(tmp, "analysis"))
        analyses, version = _load_analyses(analysis_db)
        analyses = {k: v for k, v in analyses.items() if v.bpm and v.beats is not None}
        excluded = sum(1 for v in analyses.values() if (v.duration or 0.0) < args.min_track_seconds)
        analyses = {
            k: v for k, v in analyses.items() if (v.duration or 0.0) >= args.min_track_seconds
        }
        keys = sorted(analyses)
        if args.library_db:
            library_db = _snapshot_db(Path(args.library_db).expanduser(), Path(tmp, "library"))
            tracks = _load_tracks(library_db, keys)
        else:
            tracks = {key: TrackInfo(name=_unmapped_name(key)) for key in keys}
    if args.pairs == "random":
        pairs = random_pairs(
            keys,
            tracks,
            args.n,
            args.seed,
            _bucket_pool(keys, tracks, args.out_bucket),
            _bucket_pool(keys, tracks, args.in_bucket),
        )
    else:
        pairs = album_pairs(keys, tracks)

    from music_assistant.controllers.streams.smart_fades import helpers  # noqa: PLC0415

    ceiling = min(args.buffer, float(helpers.SMART_CROSSFADE_DURATION))
    probe = PlannerProbe()
    rows = [_replay_pair(probe, o, i, analyses, tracks, ceiling) for o, i in pairs]

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    _write_csv(rows, out / "pairs.csv")
    providers = Counter(provider.split("--")[0] for _item_id, provider in keys)
    header = (
        f"code: {code_file}\n"
        f"analysis db: {args.analysis_db}\n"
        f"library db: {args.library_db or 'none'}\n"
        f"smart_fades analysis_version {version}; tracks {len(keys)} {dict(providers)}; "
        f"excluded <{args.min_track_seconds:g}s: {excluded}; "
        f"unmapped in library: {sum(1 for key in keys if not tracks[key].mapped)}\n"
        f"pairs: {args.pairs} n={len(pairs)} seed={args.seed} buffer={args.buffer:g}s "
        f"out_bucket={args.out_bucket} in_bucket={args.in_bucket}; probe hooks: {probe.hooks}"
    )
    summary = summarize(rows, tracks, header)
    (out / "summary.txt").write_text(summary, encoding="utf-8")
    print(summary, end="")
    return 0


def tag_bucket(tag: str) -> str | None:
    """
    Return the style bucket of one genre tag, or None when no keyword matches.

    The keyword that ends last wins, so the head noun decides ("pop rock" is rock, "dance-pop"
    is pop); of two ending at the same place the longer wins ("rock opera" is rock).

    :param tag: A genre tag as stored in the library.
    """
    tag = tag.lower().strip()
    best: tuple[tuple[int, int], str] | None = None
    for keyword, bucket in _KEYWORD_ORDER:
        for match in re.finditer(re.escape(keyword), tag):
            rank = (match.end(), len(keyword))
            if best is None or rank > best[0]:
                best = (rank, bucket)
    return best[1] if best else None


def style_bucket(tags: Iterable[str]) -> str:
    """
    Return the style bucket most of a track's genre tags vote for.

    :param tags: The track's genre tags.
    """
    votes = Counter(bucket for tag in tags if (bucket := tag_bucket(tag)))
    if not votes:
        return UNKNOWN_BUCKET
    top = max(votes.values())
    return next(bucket for bucket in BUCKETS if votes.get(bucket) == top)


def vocal_class(out_duty: float | None, in_duty: float | None) -> str:
    """
    Classify which deck sings over the windows either side of a transition.

    :param out_duty: Vocal duty of the outgoing window, None without a vocal timeline.
    :param in_duty: Vocal duty of the incoming window, None without a vocal timeline.
    """
    if out_duty is None or in_duty is None:
        return "unknown"
    out_sings, in_sings = out_duty > DUTY_SINGS, in_duty > DUTY_SINGS
    if out_sings and in_sings:
        return "both sing"
    if out_sings:
        return "outgoing only"
    if in_sings:
        return "incoming only"
    return "neither"


def random_pairs(
    keys: list[TrackKey],
    tracks: dict[TrackKey, TrackInfo],
    n: int,
    seed: int,
    out_pool: list[TrackKey] | None = None,
    in_pool: list[TrackKey] | None = None,
) -> list[tuple[TrackKey, TrackKey]]:
    """
    Draw up to n distinct ordered pairs of tracks from different albums.

    :param keys: Every track to draw from, sorted.
    :param tracks: Library facts per track, for the album check.
    :param n: Number of pairs to draw.
    :param seed: Random seed; the same seed and keys draw the same pairs.
    :param out_pool: Draw the outgoing track from these only.
    :param in_pool: Draw the incoming track from these only.
    """
    rng = random.Random(seed)
    pairs: list[tuple[TrackKey, TrackKey]] = []
    seen: set[tuple[TrackKey, TrackKey]] = set()
    for _ in range(n * 50):
        if len(pairs) >= n:
            break
        if out_pool is None and in_pool is None:
            fade_out, fade_in = rng.sample(keys, 2)
        else:
            fade_out, fade_in = rng.choice(out_pool or keys), rng.choice(in_pool or keys)
            if fade_out == fade_in:
                continue
        album_id = tracks[fade_out].album_id
        if (album_id is not None and album_id == tracks[fade_in].album_id) or (
            (fade_out, fade_in) in seen
        ):
            continue
        seen.add((fade_out, fade_in))
        pairs.append((fade_out, fade_in))
    return pairs


def album_pairs(
    keys: list[TrackKey], tracks: dict[TrackKey, TrackInfo]
) -> list[tuple[TrackKey, TrackKey]]:
    """
    Return every pair of consecutive tracks on an album, across disc boundaries.

    :param keys: Every track to pair.
    :param tracks: Library facts per track, for the album positions.
    """
    by_album: dict[int, list[tuple[int, int, TrackKey]]] = defaultdict(list)
    for key in keys:
        for album_id, disc, track_number in tracks[key].album_positions:
            if track_number is not None:
                by_album[album_id].append((disc or 1, track_number, key))
    pairs: list[tuple[TrackKey, TrackKey]] = []
    for entries in by_album.values():
        # one track per position: the first by sort order when an album lists two
        at: dict[tuple[int, int], TrackKey] = {}
        for disc, track_number, key in sorted(entries):
            at.setdefault((disc, track_number), key)
        positions = sorted(at)
        for here, there in pairwise(positions):
            next_on_disc = there[0] == here[0] and there[1] == here[1] + 1
            next_disc = there[0] == here[0] + 1 and there[1] == 1
            if next_on_disc or next_disc:
                pairs.append((at[here], at[there]))
    return pairs


def summarize(rows: list[Row], tracks: dict[TrackKey, TrackInfo], header: str) -> str:
    """
    Render the run summary.

    :param rows: One row per replayed pair, as written to ``pairs.csv``.
    :param tracks: Library facts of every track the pairs were drawn from.
    :param header: Lines describing the run, put first.
    """
    plans = [row for row in rows if row["outcome"] == "plan"]
    lines = [header]
    lines += _outcome_section(rows, plans)
    lines += _overlap_section(plans)
    lines += _trigger_section(rows, plans)
    lines += _vocal_section(plans)
    lines += _rhythm_section(plans)
    lines += _style_section(rows, tracks)
    lines += _not_applicable_section(rows)
    return "\n".join(lines) + "\n"


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse and check the command line."""
    parser = argparse.ArgumentParser(
        prog="python -m scripts.smart_fades_replay",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--analysis-db", required=True, help="the server's audio_analysis.db")
    parser.add_argument(
        "--library-db", help="the server's library.db, for names, albums and genres"
    )
    parser.add_argument(
        "--code",
        default=str(DEFAULT_CODE),
        help="checkout to load music_assistant from (default: this one)",
    )
    parser.add_argument("--pairs", choices=("random", "album"), default="random")
    parser.add_argument("--n", type=int, default=3000, help="number of random pairs")
    parser.add_argument("--seed", type=int, default=20261010, help="random pair seed")
    parser.add_argument(
        "--buffer", type=float, default=45.0, help="outgoing room ceiling in seconds"
    )
    buckets = (*BUCKETS, UNKNOWN_BUCKET)
    parser.add_argument("--out-bucket", choices=buckets, help="random outgoing tracks: this style")
    parser.add_argument("--in-bucket", choices=buckets, help="random incoming tracks: this style")
    parser.add_argument(
        "--min-track-seconds",
        type=float,
        default=30.0,
        help="leave out shorter tracks (jingles, effects)",
    )
    parser.add_argument("--out", required=True, help="directory for pairs.csv and summary.txt")
    args = parser.parse_args(argv)
    needs_library = args.pairs == "album" or args.out_bucket or args.in_bucket
    if needs_library and not args.library_db:
        parser.error("--pairs album and the bucket filters need --library-db")
    return args


def _import_music_assistant(code: Path) -> str:
    """Import music_assistant from a checkout and return the file it loaded from."""
    code = code.expanduser().resolve()
    if not (code / "music_assistant" / "__init__.py").is_file():
        raise SystemExit(f"no music_assistant package in {code}")
    sys.path.insert(0, str(code))
    import music_assistant  # noqa: PLC0415

    loaded = Path(music_assistant.__file__).resolve()
    if not loaded.is_relative_to(code):
        raise SystemExit(f"music_assistant loaded from {loaded}, not from {code}")
    return str(loaded)


def _snapshot_db(source: Path, target_dir: Path) -> Path:
    """Copy a database and its -wal/-shm files into target_dir and fold the WAL into the copy."""
    if not source.is_file():
        raise SystemExit(f"no database at {source}")
    target_dir.mkdir(parents=True)
    target = target_dir / source.name
    for suffix in ("", "-wal", "-shm"):
        side_file = source.with_name(source.name + suffix)
        if side_file.is_file():
            shutil.copyfile(side_file, target.with_name(target.name + suffix))
    # the checkpoint writes to the copy only; every read after it opens the copy read-only
    with closing(sqlite3.connect(target)) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA journal_mode=DELETE")
    return target


def _open_read_only(path: Path) -> sqlite3.Connection:
    """Open a database read-only."""
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _load_analyses(path: Path) -> tuple[dict[TrackKey, AudioAnalysisData], int | None]:
    """
    Rebuild every track's AudioAnalysisData as the smart fades mixer loads it.

    Only smart_fades rows count, as for the mixer; rows from an older analyser version than the
    newest in the database are left out, as they wait for re-analysis. Returns the analyses and
    that version.
    """
    from music_assistant.controllers.streams import audio_analysis  # noqa: PLC0415

    domain = audio_analysis.SMART_FADES_ANALYSIS_DOMAIN
    with closing(_open_read_only(path)) as conn:
        version = conn.execute(
            "SELECT MAX(analysis_version) FROM audio_analysis "
            "WHERE media_type = 'track' AND aa_provider_domain = ?",
            (domain,),
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT id, item_id, provider, aa_provider_domain, "
            "CAST(header AS BLOB) AS header, payload FROM audio_analysis "
            "WHERE media_type = 'track' AND aa_provider_domain = ? AND analysis_version = ?",
            (domain, version),
        ).fetchall()
    analyses: dict[TrackKey, AudioAnalysisData] = {}
    for row in rows:
        data = audio_analysis._merged_from_rows([dict(row)], {domain}, (domain,))
        if data is not None:
            analyses[(row["item_id"], row["provider"])] = data
    return analyses, version


def _load_tracks(path: Path, keys: list[TrackKey]) -> dict[TrackKey, TrackInfo]:
    """
    Look up each analysed track's name, albums and style bucket in the library.

    The bucket comes from the first genre source that yields one: the track's genres, its
    albums', its artists', then the library's normalized genre mappings.
    """
    tracks: dict[TrackKey, TrackInfo] = {}
    with closing(_open_read_only(path)) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
        has_genre_mappings = {"genres", "genre_media_item_mapping"} <= tables
        for key in keys:
            tracks[key] = _track_info(conn, key, has_genre_mappings)
    return tracks


def _track_info(conn: sqlite3.Connection, key: TrackKey, has_genre_mappings: bool) -> TrackInfo:
    """Look up one track's library facts."""
    item_id, provider = key
    mapping = conn.execute(
        "SELECT item_id FROM provider_mappings WHERE media_type = 'track' "
        "AND provider_item_id = ? AND (provider_instance = ? OR provider_domain = ?)",
        (item_id, provider, provider),
    ).fetchone()
    if mapping is None:
        return TrackInfo(name=_unmapped_name(key))
    lib_id = mapping["item_id"]
    track = conn.execute(
        "SELECT name, metadata FROM tracks WHERE item_id = ?", (lib_id,)
    ).fetchone()
    artists = conn.execute(
        "SELECT a.item_id, a.name, a.metadata FROM track_artists ta "
        "JOIN artists a ON a.item_id = ta.artist_id WHERE ta.track_id = ? ORDER BY a.item_id",
        (lib_id,),
    ).fetchall()
    albums = conn.execute(
        "SELECT at.album_id, at.disc_number, at.track_number, al.name, al.metadata "
        "FROM album_tracks at JOIN albums al ON al.item_id = at.album_id "
        "WHERE at.track_id = ? ORDER BY at.album_id",
        (lib_id,),
    ).fetchall()
    info = TrackInfo(
        name=f"{'/'.join(a['name'] for a in artists)} - {track['name'] if track else ''}",
        mapped=True,
        album=albums[0]["name"] if albums else "",
        album_id=albums[0]["album_id"] if albums else None,
        album_positions=[(a["album_id"], a["disc_number"], a["track_number"]) for a in albums],
    )
    sources = [
        ("track", _genres(track["metadata"]) if track else []),
        ("album", [tag for album in albums for tag in _genres(album["metadata"])]),
        ("artist", [tag for artist in artists for tag in _genres(artist["metadata"])]),
    ]
    if has_genre_mappings:
        items = [
            ("track", lib_id),
            *(("album", album["album_id"]) for album in albums),
            *(("artist", artist["item_id"]) for artist in artists),
        ]
        names = [
            row["name"]
            for media_type, media_id in items
            for row in conn.execute(
                "SELECT g.name FROM genre_media_item_mapping m JOIN genres g "
                "ON g.item_id = m.genre_id WHERE m.media_type = ? AND m.media_id = ?",
                (media_type, media_id),
            )
        ]
        sources.append(("normalized", names))
    for source, tags in sources:
        if not tags:
            continue
        # tags without a style (such as "Soundtrack") fall through to the next source
        if (bucket := style_bucket(tags)) != UNKNOWN_BUCKET:
            info.bucket, info.genre_source, info.genres = bucket, source, "|".join(tags)
            break
        if not info.genres:
            info.genre_source, info.genres = f"{source} (no style)", "|".join(tags)
    return info


def _genres(metadata: str | None) -> list[str]:
    """Return the genre tags of a library item's metadata JSON."""
    try:
        value = json.loads(metadata or "{}").get("genres") or []
    except AttributeError, TypeError, ValueError:
        return []
    return [tag for tag in value if isinstance(tag, str)]


def _unmapped_name(key: TrackKey) -> str:
    """Name a track the library does not know."""
    return f"(unmapped {key[1]} {key[0]})"


def _bucket_pool(
    keys: list[TrackKey], tracks: dict[TrackKey, TrackInfo], bucket: str | None
) -> list[TrackKey] | None:
    """Return the tracks of one style bucket, or None when no bucket is asked for."""
    if bucket is None:
        return None
    pool = [key for key in keys if tracks[key].bucket == bucket]
    if not pool:
        raise SystemExit(f"no track in style bucket {bucket!r}")
    return pool


def _replay_pair(
    probe: PlannerProbe,
    out_key: TrackKey,
    in_key: TrackKey,
    analyses: dict[TrackKey, AudioAnalysisData],
    tracks: dict[TrackKey, TrackInfo],
    ceiling: float,
) -> Row:
    """Plan one pair and return its CSV row."""
    fade_out, fade_in = analyses[out_key], analyses[in_key]
    out_info, in_info = tracks[out_key], tracks[in_key]
    assert fade_out.bpm is not None
    assert fade_in.bpm is not None
    row: Row = {
        "out_item": out_key[0],
        "out_provider": out_key[1],
        "in_item": in_key[0],
        "in_provider": in_key[1],
        "out_track": out_info.name,
        "in_track": in_info.name,
        "out_album": out_info.album,
        "in_album": in_info.album,
        "out_bucket": out_info.bucket,
        "in_bucket": in_info.bucket,
        "out_duration": round(fade_out.duration or 0.0, 2),
        "in_duration": round(fade_in.duration or 0.0, 2),
        "bpm_out": round(fade_out.bpm, 2),
        "bpm_in": round(fade_in.bpm, 2),
        "bpm_diff_pct": round(abs(1.0 - fade_in.bpm / fade_out.bpm) * 100, 2),
        "bpb_out": fade_out.beats_per_bar or 4,
        "bpb_in": fade_in.beats_per_bar or 4,
    }
    # playback holds up to half the outgoing track, under the smart fades ceiling
    buffer_duration = float(min(ceiling, int((fade_out.duration or 0.0) / 2)))
    row["buffer"] = buffer_duration
    if buffer_duration < MIN_CROSSFADE_SECONDS:
        row["outcome"] = "no_crossfade"
        return row
    plan, outcome, reason = probe.plan(fade_out, fade_in, buffer_duration)
    row["outcome"] = outcome
    if plan is None:
        row["reason"] = reason
        return row
    row.update(_plan_facts(probe, plan))
    row.update(_vocal_facts(probe.context, plan, fade_out, fade_in))
    row.update(_rhythm_facts(probe.context, plan, fade_out))
    row["cause"] = _cause(row)
    return row


def _plan_facts(probe: PlannerProbe, plan: Any) -> Row:
    """Tier, shape and provenance of a shipped plan."""
    strategy = _name(_attr(plan, "metrics", "strategy"))
    fadein_trim = _attr(plan, "fadein_trim_start")
    return {
        "ctx_tier": _name(_attr(probe.context, "tier")),
        "tier": _name(plan.tier),
        "qf_trigger": _quick_fade_trigger(probe.context),
        "strategy": strategy,
        **_selection_facts(probe, strategy),
        "overlap_s": round(float(plan.crossfade_duration), 3),
        "anchor_s": round(float(plan.fade_out_window), 3),
        "fadeout_trim_s": round(
            float(_attr(plan, "fadeout_trim", "trimmed_seconds", default=0.0)), 3
        ),
        "fadein_trim_s": round(float(fadein_trim), 3) if fadein_trim is not None else "",
        "tempo_stretch": bool(_attr(plan, "tempo_plan")),
    }


def _selection_facts(probe: PlannerProbe, strategy: str) -> Row:
    """Which pass shipped the plan, the winner's generator and bars, and what it beat."""
    winner = next((entry for entry in probe.winners if entry is not None), None)
    if probe.winners and probe.winners[0] is not None:
        via = "main"
    elif winner is not None:
        via = "rescue"
    elif strategy == "FALLBACK_CROSSFADE":
        via = "fallback"
    elif strategy == "SHORT_VOCAL_HANDOFF":
        via = "handoff"
    else:
        via = "unknown"
    if winner is None:
        # the fallback and the handoff are built from a 1-bar spec of their own
        source = {"fallback": "fallback-crossfade", "handoff": "emergency-handoff"}.get(via, "")
        return {"shipped_via": via, "source": source, "bars": 1, "longer_rejected": 0}
    spec = _attr(winner, "candidate", "spec")
    bars = _attr(spec, "bars", default=0)
    main_pass = probe.passes[0] if probe.passes else []
    longer_rejected = sum(
        1
        for entry in main_pass
        if _attr(entry, "rejected", default=False)
        and _attr(entry, "candidate", "spec", "bars", default=0) > bars
    )
    return {
        "shipped_via": via,
        "source": _attr(spec, "source", default=""),
        "bars": bars,
        "longer_rejected": longer_rejected,
    }


def _quick_fade_trigger(ctx: Any) -> str:
    """Return what made the context tier a quick fade, or "" for a blend."""
    if _name(_attr(ctx, "tier")) != "QUICK_FADE":
        return ""
    if hasattr(ctx, "quick_fade_trigger"):
        return str(_attr(ctx, "quick_fade_trigger", "value", default=""))
    # a planner that does not record the trigger: rederive it in its tier check order
    from music_assistant.controllers.streams.smart_fades.planner import context  # noqa: PLC0415

    if _attr(ctx, "cross_meter"):
        return "meter"
    blendable: Callable[[Any], bool] | None = getattr(context, "_tail_is_blendable", None)
    downbeats, anchor = _attr(ctx, "outgoing", "downbeats"), _attr(ctx, "default_anchor")
    if blendable is None or downbeats is None or anchor is None:
        return "unknown"
    return "tempo" if blendable(downbeats[downbeats <= anchor]) else "beat_grid"


def _vocal_facts(
    ctx: Any, plan: Any, fade_out: AudioAnalysisData, fade_in: AudioAnalysisData
) -> Row:
    """Vocal duty of the 8 outgoing bars before the anchor and the 8 incoming bars after the trim."""
    anchor = float(plan.fade_out_window)
    trim = float(_attr(plan, "fadein_trim_start", default=0.0))
    out_length = min(WINDOW_BARS * _bar_seconds(fade_out), anchor)
    in_length = WINDOW_BARS * _bar_seconds(fade_in)
    out_windows = _attr(ctx, "vocal_out_scoring", "windows")
    in_windows = _attr(ctx, "vocal_in_scoring", "windows")
    out_duty = (
        _coverage(out_windows, anchor - out_length, anchor) / out_length
        if out_windows is not None and out_length > 0
        else None
    )
    in_duty = (
        _coverage(in_windows, trim, trim + in_length) / in_length
        if in_windows is not None
        else None
    )
    return {
        "out_vocal_duty": round(out_duty, 3) if out_duty is not None else "",
        "in_vocal_duty": round(in_duty, 3) if in_duty is not None else "",
        "vocal_class": vocal_class(out_duty, in_duty),
    }


def _rhythm_facts(ctx: Any, plan: Any, fade_out: AudioAnalysisData) -> Row:
    """Kick overlap if the 8 outgoing bars before the anchor faded over the incoming head."""
    out_track = _kick_track(_attr(ctx, "outgoing_profile"))
    in_track = _kick_track(_attr(ctx, "incoming_profile"))
    offset = _attr(ctx, "buffer_offset")
    if out_track is None or in_track is None or offset is None:
        return {"rhythm_safe": ""}
    bar_out = _bar_seconds(fade_out)
    anchor = float(plan.fade_out_window)
    length = min(WINDOW_BARS * bar_out, anchor)
    times = np.arange(0.0, length, SAMPLE_STEP) + SAMPLE_STEP / 2
    out_kick, out_bars = _kicks_at(out_track, offset + anchor - length + times)
    in_kick, in_bars = _kicks_at(in_track, times)
    # 4p(1-p) peaks mid-fade, where both decks play loudest together
    weight = 4 * (times / length) * (1 - times / length)
    out_kick_bars = int(out_track.kick[out_bars].sum())
    in_kick_bars = int(in_track.kick[in_bars].sum())
    clash_bars = round(float(((out_kick & in_kick) * weight).sum() * SAMPLE_STEP) / bar_out, 3)
    return {
        "out_kick_bars": out_kick_bars,
        "in_kick_bars": in_kick_bars,
        "kick_clash_bars": clash_bars,
        "rhythm_safe": not out_kick_bars or not in_kick_bars or clash_bars <= CLASH_LIMIT_BARS,
    }


def _kick_track(profile: Any) -> _KickTrack | None:
    """Per-bar kick presence from a planner band profile, None when there is none."""
    try:
        starts = np.asarray(profile.bar_starts, dtype=np.float64)
        low = np.asarray(profile.bar_power["low"], dtype=np.float64)
        reference = float(profile.reference["low"])
    except AttributeError, KeyError, TypeError:
        return None
    if not len(starts) or reference <= 0:
        return None
    median_bar = float(np.median(np.diff(starts))) if len(starts) > 1 else 2.0
    ends = np.append(starts[1:], starts[-1] + median_bar)
    return _KickTrack(starts, ends, low >= KICK_FRACTION * reference)


def _kicks_at(
    track: _KickTrack, times: npt.NDArray[np.floating[Any]]
) -> tuple[npt.NDArray[np.bool_], npt.NDArray[np.intp]]:
    """Kick presence at each time, and the bars the times fall in."""
    index = np.searchsorted(track.starts, times, side="right") - 1
    clipped = np.clip(index, 0, len(track.starts) - 1)
    inside = (index >= 0) & (times < track.ends[clipped])
    return np.where(inside, track.kick[clipped], False), np.unique(clipped[inside])


def _coverage(windows: Iterable[tuple[float, float]], start: float, end: float) -> float:
    """Seconds of [start, end] the windows cover."""
    return sum(max(0.0, min(right, end) - max(left, start)) for left, right in windows)


def _bar_seconds(analysis: AudioAnalysisData) -> float:
    """Length of one bar of a track."""
    assert analysis.bpm is not None
    return (analysis.beats_per_bar or 4) * 60.0 / analysis.bpm


def _cause(row: Row) -> str:
    """Name what kept the shipped fade as short as it is; meaningful for short fades only."""
    if row["shipped_via"] in ("rescue", "fallback", "handoff"):
        return "rejection -> rescue/fallback/handoff"
    if row["tier"] == "QUICK_FADE":
        # a blend context whose shipped candidate re-anchored onto an unblendable grid
        return f"QF: {row['qf_trigger']}" if row["qf_trigger"] else "QF: re-anchored"
    if row["longer_rejected"]:
        return "blend: longer rungs rejected"
    return "blend: short top rung"


def _write_csv(rows: list[Row], path: Path) -> None:
    """Write the rows with every column any row has, in first-seen order."""
    columns = list(dict.fromkeys(column for row in rows for column in row))
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, restval="")
        writer.writeheader()
        writer.writerows(rows)


def _outcome_section(rows: list[Row], plans: list[Row]) -> list[str]:
    """Shipped tiers and strategies, and where the winners came from."""
    outcomes = Counter(row["tier"] if row["outcome"] == "plan" else row["outcome"] for row in rows)
    strategies = Counter(row["strategy"] for row in plans)
    return [
        "",
        "== outcome / shipped tier",
        "  " + ", ".join(f"{k}: {_pct(v, len(rows))}" for k, v in outcomes.most_common()),
        "  strategy: "
        + ", ".join(f"{k}: {_pct(v, len(plans))}" for k, v in strategies.most_common()),
        f"  shipped via: {dict(Counter(row['shipped_via'] for row in plans))}",
        f"  winner source: {dict(Counter(row['source'] for row in plans).most_common())}",
        "  winner bars: " + str(dict(sorted(Counter(str(row["bars"]) for row in plans).items()))),
    ]


def _overlap_section(plans: list[Row]) -> list[str]:
    """Shipped overlap length per tier."""
    lines = ["", "== shipped overlap (s): tier n median p10 p90 <4s <8s"]
    for tier in (*TIERS, "ALL"):
        values = [row["overlap_s"] for row in plans if tier in ("ALL", row["tier"])]
        if not values:
            continue
        lines.append(
            f"  {tier:12s} {len(values):5d} {statistics.median(values):6.2f} "
            f"{np.percentile(values, 10):6.2f} {np.percentile(values, 90):6.2f} "
            f"{100 * sum(v < 4 for v in values) / len(values):5.1f}% "
            f"{100 * sum(v < SHORT_FADE_SECONDS for v in values) / len(values):5.1f}%"
        )
    return lines


def _trigger_section(rows: list[Row], plans: list[Row]) -> list[str]:
    """Why quick fades happen, the tempo gaps, and the short fades by cause."""
    quick = [row for row in plans if row.get("ctx_tier") == "QUICK_FADE"]
    triggers = dict(Counter(row["qf_trigger"] for row in quick).most_common())
    bands = Counter(_bpm_band(row["bpm_diff_pct"]) for row in rows)
    gaps = sum(1 for row in plans if row["tier"] == "QUICK_FADE" and 8 < row["bpm_diff_pct"] <= 20)
    short = [row for row in plans if row["overlap_s"] < SHORT_FADE_SECONDS]
    causes = Counter(row["cause"] for row in short)
    return [
        "",
        f"== QUICK_FADE (context tier) {len(quick)}; first trigger: {triggers}",
        "  bpm diff (all pairs): "
        + ", ".join(f"{band}: {_pct(bands[band], len(rows))}" for band in _BPM_BANDS),
        f"  QUICK_FADE pairs 8-20% apart: {gaps}",
        "",
        f"== short (<8 s) shipped: {len(short)} of {len(plans)}, by cause",
        *(f"    {cause:40s} {count:5d}" for cause, count in causes.most_common()),
    ]


def _vocal_section(plans: list[Row]) -> list[str]:
    """Overlap length per vocal class, with the causes of the short fades."""
    lines = [
        "",
        "== vocal class (8-bar windows): n, <8 s, median overlap | causes of the <8 s ones",
    ]
    populations = (
        ("all", plans),
        ("rhythm-safe", [row for row in plans if row.get("rhythm_safe") is True]),
    )
    for title, population in populations:
        lines.append(f"  [{title}] " + " | ".join(CAUSES))
        for cls in VOCAL_CLASSES:
            members = [row for row in population if row["vocal_class"] == cls]
            if not members:
                continue
            short = [row for row in members if row["overlap_s"] < SHORT_FADE_SECONDS]
            causes = Counter(row["cause"] for row in short)
            lines.append(
                f"   {cls:13s} {len(members):5d} {100 * len(short) / len(members):5.1f}% "
                f"{statistics.median(row['overlap_s'] for row in members):5.2f}s | "
                + " | ".join(str(causes[cause]) for cause in CAUSES)
            )
    return lines


def _rhythm_section(plans: list[Row]) -> list[str]:
    """How often a long fade at the shipped anchor would put two kicks on top of each other."""
    lines = [
        "",
        "== rhythm (8 bars at the anchor vs incoming head): "
        "n, either kickless, kick clash >2 weighted bars",
    ]
    populations = (("QUICK_FADE", [row for row in plans if row["tier"] == "QUICK_FADE"]),)
    for title, population in (*populations, ("all plans", plans)):
        known = [row for row in population if row.get("rhythm_safe") != ""]
        kickless = sum(1 for row in known if not row["out_kick_bars"] or not row["in_kick_bars"])
        clash = sum(1 for row in known if row["kick_clash_bars"] > CLASH_LIMIT_BARS)
        lines.append(
            f"  {title:22s} {len(known):5d} kickless {_pct(kickless, len(known))} "
            f"clash {_pct(clash, len(known))}"
        )
    return lines


def _style_section(rows: list[Row], tracks: dict[TrackKey, TrackInfo]) -> list[str]:
    """Bucket coverage, and the outcome per outgoing and incoming style bucket."""
    coverage = Counter(info.bucket for info in tracks.values())
    sources = Counter(info.genre_source or "none" for info in tracks.values())
    bucketed = sum(count for bucket, count in coverage.items() if bucket != UNKNOWN_BUCKET)
    lines = [
        "",
        f"== style buckets over {len(tracks)} tracks: bucketed {_pct(bucketed, len(tracks))}; "
        f"source {dict(sources)}",
        "  " + ", ".join(f"{bucket}: {count}" for bucket, count in coverage.most_common()),
    ]
    for side, title in (("out", "outgoing"), ("in", "incoming")):
        lines += ["", f"== by {title} bucket"]
        by_bucket: dict[str, list[Row]] = defaultdict(list)
        for row in rows:
            by_bucket[row[f"{side}_bucket"]].append(row)
        for bucket in sorted(by_bucket, key=lambda b: -len(by_bucket[b])):
            lines.append(_style_line(bucket, by_bucket[bucket]))
    known = [row for row in rows if UNKNOWN_BUCKET not in (row["out_bucket"], row["in_bucket"])]
    lines += [
        "",
        "== same bucket vs cross bucket (both known)",
        _style_line("same bucket", [r for r in known if r["out_bucket"] == r["in_bucket"]]),
        _style_line("cross bucket", [r for r in known if r["out_bucket"] != r["in_bucket"]]),
    ]
    return lines


def _style_line(label: str, rows: list[Row]) -> str:
    """One style bucket's outcome: N/A, short fades, overlap, tier, vocals and rhythm."""
    plans = [row for row in rows if row["outcome"] == "plan"]
    if not plans:
        return f"  {label:28s} pairs {len(rows):5d} (no plans)"
    overlaps = [row["overlap_s"] for row in plans]
    one_sided = sum(1 for row in plans if row["vocal_class"] in ("outgoing only", "incoming only"))
    rhythm_safe = sum(1 for row in plans if row.get("rhythm_safe") is True)
    quick = sum(1 for row in plans if row["tier"] == "QUICK_FADE")
    return (
        f"  {label:28s} pairs {len(rows):5d} "
        f"N/A {100 * (len(rows) - len(plans)) / len(rows):4.1f}% "
        f"<8s {100 * sum(v < SHORT_FADE_SECONDS for v in overlaps) / len(overlaps):5.1f}% "
        f"median {statistics.median(overlaps):5.2f}s "
        f"QF {100 * quick / len(plans):5.1f}% "
        f"one-sided vocal {100 * one_sided / len(plans):5.1f}% "
        f"rhythm-safe {100 * rhythm_safe / len(plans):5.1f}% "
        f"FULL+TEMPO {100 * (len(plans) - quick) / len(plans):5.1f}%"
    )


def _not_applicable_section(rows: list[Row]) -> list[str]:
    """Pairs without a plan, by reason."""
    failed = [row for row in rows if row["outcome"] in ("not_applicable", "error")]
    reasons = Counter(
        f"error: {row['reason'].split(':')[0]}"
        if row["outcome"] == "error"
        else re.sub(r"\s*\(.*\)$", "", row["reason"])
        for row in failed
    )
    return [
        "",
        f"== not applicable / error: {len(failed)} pairs",
        *(f"  {reason}: {count}" for reason, count in reasons.most_common()),
    ]


def _bpm_band(diff_pct: float) -> str:
    """Tempo gap band of a pair."""
    if diff_pct <= 8:
        return "<=8"
    if diff_pct <= 12:
        return "8-12"
    return "12-20" if diff_pct <= 20 else ">20"


def _pct(count: int, total: int) -> str:
    """Format a count with its share of the total."""
    return f"{count} ({100 * count / total:.1f}%)" if total else "0"


def _attr(obj: Any, *names: str, default: Any = None) -> Any:
    """Read a nested attribute of a planner object, or the default when any step is missing."""
    for name in names:
        if obj is None:
            return default
        obj = getattr(obj, name, None)
    return default if obj is None else obj


def _name(value: Any) -> str:
    """Name of an enum member, or the value as text ("" for None)."""
    return str(getattr(value, "name", "" if value is None else value))


if __name__ == "__main__":
    sys.exit(main())
