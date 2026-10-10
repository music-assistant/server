r"""
Replay the smart fades planner over random track pairs from a server's stored analysis.

Plans the transition of each pair the way playback would, without playing anything, and writes
``pairs.csv`` (one row per pair) and ``summary.txt`` (tiers and overlap lengths, quick fade
triggers, vocal and drum overlap, music style) into ``--out``. To measure a planner change, run
it on both sides of the change with the same databases, seed and buffer; ``--code`` loads
``music_assistant`` from another checkout.

Both databases are copied with their -wal file into a temporary directory and only those copies
are read. The given paths are never written to and no other server data, such as the auth
database, is opened.

Usage policy: this reads stored analysis data and library metadata only. It never opens, decodes
or writes audio.

Example, from the repository root::

    python -m scripts.smart_fades_replay \
        --analysis-db ~/.musicassistant/audio_analysis.db \
        --library-db ~/.musicassistant/library.db \
        --n 3000 --seed 20261010 --buffer 45 --out /tmp/replay
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
import sys
import tempfile
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

import numpy as np

if TYPE_CHECKING:
    import numpy.typing as npt

    from music_assistant.controllers.streams.smart_fades.models import (
        BandProfile,
        Deck,
        TransitionPlan,
    )
    from music_assistant.controllers.streams.smart_fades.planner.candidates import Candidate
    from music_assistant.controllers.streams.smart_fades.planner.context import TransitionContext
    from music_assistant.controllers.streams.smart_fades.planner.selection import ScoredCandidate
    from music_assistant.models.audio_analysis import AudioAnalysisData

# ruff: noqa: T201

# (item_id, provider) of an audio_analysis row: the provider domain for a streaming provider,
# the instance id for any other
TrackKey = tuple[str, str]
Row = dict[str, Any]

DEFAULT_CODE = Path(__file__).resolve().parents[1]
SHORT_FADE_SECONDS = 8.0
BPM_BANDS = ("<=8", "8-12", "12-20", ">20")

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
_KEYWORD_BUCKET = {
    keyword: bucket
    for bucket, keywords in _BUCKET_KEYWORDS.items()
    for keyword in keywords.split(", ")
}
# longest first, so of two keywords starting at the same place the longer one matches
_KEYWORD_PATTERN = re.compile(
    "|".join(re.escape(keyword) for keyword in sorted(_KEYWORD_BUCKET, key=len, reverse=True))
)

# why a shipped fade is as short as it is, in summary column order
CAUSES = (
    "QF: tempo",
    "QF: meter",
    "QF: beat_grid",
    "rejection -> rescue/fallback/handoff",
    "blend: longer rungs rejected",
    "blend: short top rung",
)
VOCAL_CLASSES = ("both sing", "outgoing only", "incoming only", "neither", "unknown")
# shipped via and source of the plans that no selection pass won
_UNPHRASED = {
    "FALLBACK_CROSSFADE": ("fallback", "fallback-crossfade"),
    "SHORT_VOCAL_HANDOFF": ("handoff", "emergency-handoff"),
}


@dataclass(slots=True)
class TrackInfo:
    """Library facts of one analysed track; only the name is set for a track not in the library."""

    name: str
    mapped: bool = False
    album: str = ""
    album_id: int | None = None
    bucket: str = UNKNOWN_BUCKET
    genre_source: str = ""


class Replayer:
    """Plans pairs with the loaded checkout's planner as playback would, one CSV row each."""

    def __init__(
        self,
        analyses: dict[TrackKey, AudioAnalysisData],
        tracks: dict[TrackKey, TrackInfo],
        ceiling: float,
    ) -> None:
        """
        Load the planner and wrap its candidate selection to keep every pass of a plan.

        :param analyses: Analysis per track.
        :param tracks: Library facts per track.
        :param ceiling: Most seconds of outgoing tail to plan with.
        """
        # music_assistant loads from --code, so it is imported only once that is on sys.path
        from music_assistant.controllers.streams.audio import (  # noqa: PLC0415
            MIN_CROSSFADE_DURATION,
        )
        from music_assistant.controllers.streams.smart_fades.helpers import (  # noqa: PLC0415
            SMART_CROSSFADE_DURATION,
        )
        from music_assistant.controllers.streams.smart_fades.models import (  # noqa: PLC0415
            SmartFadeNotApplicable,
        )
        from music_assistant.controllers.streams.smart_fades.planner import (  # noqa: PLC0415
            SmartCrossFadePlanner,
        )
        from music_assistant.controllers.streams.smart_fades.planner.selection import (  # noqa: PLC0415
            CandidateSelector,
        )

        self._analyses = analyses
        self._tracks = tracks
        self._ceiling = min(ceiling, float(SMART_CROSSFADE_DURATION))
        self._min_room = float(MIN_CROSSFADE_DURATION)
        self._not_applicable = SmartFadeNotApplicable
        self._logger = logging.getLogger("scripts.smart_fades_replay.planner")
        self._logger.setLevel(logging.WARNING)
        self._planner = SmartCrossFadePlanner(self._logger)
        self._passes: list[_SelectionPass] = []
        select, score = CandidateSelector.select, CandidateSelector._score

        def select_and_keep(
            self_: CandidateSelector, candidates: Sequence[Candidate], ctx: TransitionContext
        ) -> ScoredCandidate | None:
            self._passes.append(selection_pass := _SelectionPass(ctx))
            selection_pass.winner = select(self_, candidates, ctx)
            return selection_pass.winner

        def score_and_keep(
            self_: CandidateSelector, candidate: Candidate, ctx: TransitionContext
        ) -> ScoredCandidate:
            entry = score(self_, candidate, ctx)
            self._passes[-1].scored.append(entry)
            return entry

        CandidateSelector.select = select_and_keep  # type: ignore[assignment]
        CandidateSelector._score = score_and_keep  # type: ignore[assignment]

    def replay(self, out_key: TrackKey, in_key: TrackKey) -> Row:
        """
        Plan one pair and return its CSV row.

        :param out_key: The outgoing track.
        :param in_key: The incoming track.
        """
        fade_out, fade_in = self._analyses[out_key], self._analyses[in_key]
        out_info, in_info = self._tracks[out_key], self._tracks[in_key]
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
            "bpm_out": round(fade_out.bpm or 0.0, 2),
            "bpm_in": round(fade_in.bpm or 0.0, 2),
            "bpb_out": fade_out.beats_per_bar or 4,
            "bpb_in": fade_in.beats_per_bar or 4,
        }
        # playback holds up to half the outgoing track and skips the fade below the minimum room
        buffer_duration = float(min(self._ceiling, int((fade_out.duration or 0.0) / 2)))
        row["buffer"] = buffer_duration
        if buffer_duration < self._min_room:
            return {**row, "outcome": "no_crossfade"}
        self._passes.clear()
        try:
            plan = self._planner.plan(fade_out, fade_in, buffer_duration)
        except self._not_applicable as err:
            return {**row, "outcome": "not_applicable", "reason": str(err)}
        except Exception as err:
            return {**row, "outcome": "error", "reason": f"{type(err).__name__}: {err}"}
        if not self._passes:
            raise SystemExit(
                "the loaded planner bypassed the candidate selection this script wraps"
            )
        ctx = self._passes[0].context
        row["outcome"] = "plan"
        row.update(_plan_facts(ctx, plan, self._passes))
        row.update(_vocal_facts(ctx, plan))
        row.update(_rhythm_facts(ctx, plan))
        row["cause"] = _cause(row)
        return row


@dataclass(slots=True)
class _SelectionPass:
    """One candidate selection of a plan: its context, every scored entry, and the winner."""

    context: TransitionContext
    scored: list[ScoredCandidate] = field(default_factory=list)
    # None when every candidate was rejected
    winner: ScoredCandidate | None = None


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
        with closing(_snapshot(Path(args.analysis_db), Path(tmp, "analysis"))) as conn:
            analyses, version = _load_analyses(conn)
        analyses = {k: v for k, v in analyses.items() if v.bpm and v.beats is not None}
        excluded = sum(1 for v in analyses.values() if (v.duration or 0.0) < args.min_track_seconds)
        analyses = {
            k: v for k, v in analyses.items() if (v.duration or 0.0) >= args.min_track_seconds
        }
        keys = sorted(analyses)
        if args.library_db:
            with closing(_snapshot(Path(args.library_db), Path(tmp, "library"))) as conn:
                tracks = {key: _track_info(conn, key) for key in keys}
        else:
            tracks = {key: TrackInfo(name=_unmapped_name(key)) for key in keys}
    pairs = random_pairs(
        keys,
        tracks,
        args.n,
        args.seed,
        _bucket_pool(keys, tracks, args.out_bucket),
        _bucket_pool(keys, tracks, args.in_bucket),
    )
    replayer = Replayer(analyses, tracks, args.buffer)
    rows = [replayer.replay(out_key, in_key) for out_key, in_key in pairs]

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
        f"pairs: n={len(pairs)} seed={args.seed} buffer={args.buffer:g}s "
        f"out_bucket={args.out_bucket} in_bucket={args.in_bucket}"
    )
    summary = summarize(rows, tracks, header)
    (out / "summary.txt").write_text(summary, encoding="utf-8")
    print(summary, end="")
    return 0


def tag_bucket(tag: str) -> str | None:
    """
    Return the style bucket of one genre tag, or None when no keyword matches.

    The last keyword in the tag wins, so the head noun decides ("pop rock" is rock, "dance-pop"
    is pop); of two starting at the same place the longer wins ("rock opera" is rock).

    :param tag: A genre tag as stored in the library.
    """
    matches = _KEYWORD_PATTERN.findall(tag.lower())
    return _KEYWORD_BUCKET[matches[-1]] if matches else None


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
    lines += _trigger_section(plans)
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
    parser.add_argument("--library-db", help="the server's library.db, for names, albums, genres")
    parser.add_argument(
        "--code",
        default=str(DEFAULT_CODE),
        help="checkout to load music_assistant from (default: this one)",
    )
    parser.add_argument("--n", type=int, default=3000, help="number of pairs")
    parser.add_argument("--seed", type=int, default=20261010, help="random pair seed")
    parser.add_argument(
        "--buffer", type=float, default=45.0, help="outgoing room ceiling in seconds"
    )
    buckets = (*BUCKETS, UNKNOWN_BUCKET)
    parser.add_argument("--out-bucket", choices=buckets, help="outgoing tracks of this style only")
    parser.add_argument("--in-bucket", choices=buckets, help="incoming tracks of this style only")
    parser.add_argument(
        "--min-track-seconds",
        type=float,
        default=30.0,
        help="leave out shorter tracks (jingles, effects)",
    )
    parser.add_argument("--out", required=True, help="directory for pairs.csv and summary.txt")
    args = parser.parse_args(argv)
    if (args.out_bucket or args.in_bucket) and not args.library_db:
        parser.error("the bucket filters need --library-db")
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


def _snapshot(source: Path, target_dir: Path) -> sqlite3.Connection:
    """Copy a database and its -wal file into target_dir and open the copy."""
    source = source.expanduser()
    if not source.is_file():
        raise SystemExit(f"no database at {source}")
    target_dir.mkdir(parents=True)
    for suffix in ("", "-wal"):
        if (side_file := source.with_name(source.name + suffix)).is_file():
            shutil.copyfile(side_file, target_dir / side_file.name)
    conn = sqlite3.connect(target_dir / source.name)
    conn.row_factory = sqlite3.Row
    return conn


def _load_analyses(
    conn: sqlite3.Connection,
) -> tuple[dict[TrackKey, AudioAnalysisData], int | None]:
    """
    Rebuild every track's AudioAnalysisData as the smart fades mixer loads it.

    Only smart_fades rows count, as for the mixer; rows from an older analyser version than the
    newest in the database are left out, as they wait for re-analysis. Returns the analyses and
    that version.
    """
    from music_assistant.controllers.streams import audio_analysis  # noqa: PLC0415

    domain = audio_analysis.SMART_FADES_ANALYSIS_DOMAIN
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


def _track_info(conn: sqlite3.Connection, key: TrackKey) -> TrackInfo:
    """
    Look up one analysed track's name, album and style bucket in the library.

    The bucket comes from the first genre source that yields one: the track's genres, its
    albums', then its artists'.
    """
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
        "SELECT a.name, a.metadata FROM track_artists ta "
        "JOIN artists a ON a.item_id = ta.artist_id WHERE ta.track_id = ? ORDER BY a.item_id",
        (lib_id,),
    ).fetchall()
    albums = conn.execute(
        "SELECT al.item_id, al.name, al.metadata FROM album_tracks at "
        "JOIN albums al ON al.item_id = at.album_id WHERE at.track_id = ? ORDER BY at.album_id",
        (lib_id,),
    ).fetchall()
    info = TrackInfo(
        name=f"{'/'.join(a['name'] for a in artists)} - {track['name'] if track else ''}",
        mapped=True,
        album=albums[0]["name"] if albums else "",
        album_id=albums[0]["item_id"] if albums else None,
    )
    sources = (
        ("track", _genres(track["metadata"]) if track else []),
        ("album", [tag for album in albums for tag in _genres(album["metadata"])]),
        ("artist", [tag for artist in artists for tag in _genres(artist["metadata"])]),
    )
    for source, tags in sources:
        # tags without a style (such as "Soundtrack") fall through to the next source
        if (bucket := style_bucket(tags)) != UNKNOWN_BUCKET:
            info.bucket, info.genre_source = bucket, source
            break
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


def _plan_facts(ctx: TransitionContext, plan: TransitionPlan, passes: list[_SelectionPass]) -> Row:
    """Tier, shape and provenance of a shipped plan."""
    quick_fade = plan.tier.name == "QUICK_FADE"
    strategy = plan.metrics.strategy.name
    winner = next((p.winner for p in passes if p.winner is not None), None)
    bars: int | str = ""
    longer_rejected = 0
    if winner is None:
        via, source = _UNPHRASED[strategy]
    else:
        via = "main" if passes[0].winner is not None else "rescue"
        source, bars = winner.candidate.spec.source, winner.candidate.spec.bars
        longer_rejected = sum(
            1 for entry in passes[0].scored if entry.rejected and entry.candidate.spec.bars > bars
        )
    return {
        "bpm_diff_pct": round(ctx.bpm_diff_percent, 2),
        "ctx_tier": ctx.tier.name,
        "tier": plan.tier.name,
        # as the planner logs it: only the beat grid depends on the anchor a candidate moved to
        "qf_trigger": str(ctx.quick_fade_trigger or "beat_grid") if quick_fade else "",
        "strategy": strategy,
        "shipped_via": via,
        "source": source,
        "bars": bars,
        "longer_rejected": longer_rejected,
        "overlap_s": round(plan.crossfade_duration, 3),
        "anchor_s": round(plan.fade_out_window, 3),
        "fadeout_trim_s": round(plan.fadeout_trim.trimmed_seconds, 3) if plan.fadeout_trim else 0.0,
        "fadein_trim_s": round(plan.fadein_trim_start or 0.0, 3),
        "tempo_stretch": bool(plan.tempo_plan),
    }


def _vocal_facts(ctx: TransitionContext, plan: TransitionPlan) -> Row:
    """Vocal duty of the 8 outgoing bars before the anchor and the 8 incoming bars after the trim."""
    anchor, trim = plan.fade_out_window, plan.fadein_trim_start or 0.0
    out_length = min(WINDOW_BARS * _bar_seconds(ctx.outgoing), anchor)
    in_length = WINDOW_BARS * _bar_seconds(ctx.incoming)
    out_duty = in_duty = None
    if ctx.vocal_out_scoring is not None and out_length > 0:
        out_duty = (
            _coverage(ctx.vocal_out_scoring.windows, anchor - out_length, anchor) / out_length
        )
    if ctx.vocal_in_scoring is not None:
        in_duty = _coverage(ctx.vocal_in_scoring.windows, trim, trim + in_length) / in_length
    return {
        "out_vocal_duty": round(out_duty, 3) if out_duty is not None else "",
        "in_vocal_duty": round(in_duty, 3) if in_duty is not None else "",
        "vocal_class": vocal_class(out_duty, in_duty),
    }


def _rhythm_facts(ctx: TransitionContext, plan: TransitionPlan) -> Row:
    """Kick overlap if the 8 outgoing bars before the anchor faded over the trimmed incoming head."""
    out_track, in_track = _kick_track(ctx.outgoing_profile), _kick_track(ctx.incoming_profile)
    if out_track is None or in_track is None:
        return {"rhythm_safe": ""}
    bar_out = _bar_seconds(ctx.outgoing)
    anchor, trim = plan.fade_out_window, plan.fadein_trim_start or 0.0
    length = min(WINDOW_BARS * bar_out, anchor)
    times = np.arange(0.0, length, SAMPLE_STEP) + SAMPLE_STEP / 2
    out_kick, out_bars = _kicks_at(out_track, ctx.buffer_offset + anchor - length + times)
    in_kick, in_bars = _kicks_at(in_track, trim + times)
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


def _kick_track(profile: BandProfile | None) -> _KickTrack | None:
    """Per-bar kick presence from a planner band profile, None when there is none."""
    if profile is None or not len(profile.bar_starts) or profile.reference["low"] <= 0:
        return None
    starts = profile.bar_starts
    median_bar = float(np.median(np.diff(starts))) if len(starts) > 1 else 2.0
    ends = np.append(starts[1:], starts[-1] + median_bar)
    kick = profile.bar_power["low"] >= KICK_FRACTION * profile.reference["low"]
    return _KickTrack(starts, ends, kick)


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


def _bar_seconds(deck: Deck) -> float:
    """Length of one bar of a deck."""
    return deck.beats_per_bar * 60.0 / deck.bpm


def _cause(row: Row) -> str:
    """Name what kept the shipped fade as short as it is; meaningful for short fades only."""
    if row["shipped_via"] != "main":
        return "rejection -> rescue/fallback/handoff"
    if row["tier"] == "QUICK_FADE":
        return f"QF: {row['qf_trigger']}"
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
    for tier in ("FULL_BLEND", "TEMPO_BLEND", "QUICK_FADE", "ALL"):
        values = np.array([row["overlap_s"] for row in plans if tier in ("ALL", row["tier"])])
        if not len(values):
            continue
        p10, median, p90 = np.percentile(values, (10, 50, 90))
        lines.append(
            f"  {tier:12s} {len(values):5d} {median:6.2f} {p10:6.2f} {p90:6.2f} "
            f"{100 * np.mean(values < 4):5.1f}% {100 * np.mean(values < SHORT_FADE_SECONDS):5.1f}%"
        )
    return lines


def _trigger_section(plans: list[Row]) -> list[str]:
    """Why quick fades happen, the tempo gaps, and the short fades by cause."""
    quick = [row for row in plans if row["tier"] == "QUICK_FADE"]
    triggers = dict(Counter(row["qf_trigger"] for row in quick).most_common())
    bands = Counter(_bpm_band(row["bpm_diff_pct"]) for row in plans)
    gaps = sum(1 for row in quick if 8 < row["bpm_diff_pct"] <= 20)
    short = [row for row in plans if row["overlap_s"] < SHORT_FADE_SECONDS]
    causes = Counter(row["cause"] for row in short)
    return [
        "",
        f"== QUICK_FADE {len(quick)}; trigger: {triggers}",
        "  bpm diff (planned pairs): "
        + ", ".join(f"{band}: {_pct(bands[band], len(plans))}" for band in BPM_BANDS),
        f"  QUICK_FADE pairs 8-20% apart: {gaps}",
        "",
        f"== short (<8 s) shipped: {len(short)} of {len(plans)}, by cause",
        *(f"    {cause:40s} {count:5d}" for cause, count in causes.most_common()),
    ]


def _vocal_section(plans: list[Row]) -> list[str]:
    """Overlap length per vocal class, with the causes of the short fades."""
    lines = [
        "",
        "== vocal class (8-bar windows): n, <8 s, median overlap | <8 s by cause:",
        "   " + " | ".join(CAUSES),
    ]
    rhythm_safe = [row for row in plans if row["rhythm_safe"] is True]
    for title, population in (("all", plans), ("rhythm-safe", rhythm_safe)):
        lines.append(f"  [{title}]")
        for cls in VOCAL_CLASSES:
            overlaps = np.array(
                [row["overlap_s"] for row in population if row["vocal_class"] == cls]
            )
            if not len(overlaps):
                continue
            causes = Counter(
                row["cause"]
                for row in population
                if row["vocal_class"] == cls and row["overlap_s"] < SHORT_FADE_SECONDS
            )
            lines.append(
                f"   {cls:13s} {len(overlaps):5d} "
                f"{100 * np.mean(overlaps < SHORT_FADE_SECONDS):5.1f}% "
                f"{np.median(overlaps):5.2f}s | " + " | ".join(str(causes[c]) for c in CAUSES)
            )
    return lines


def _rhythm_section(plans: list[Row]) -> list[str]:
    """How often a long fade at the shipped anchor would put two kicks on top of each other."""
    lines = [
        "",
        "== rhythm (8 bars at the anchor vs incoming head): "
        "n, either kickless, kick clash >2 weighted bars",
    ]
    quick = [row for row in plans if row["tier"] == "QUICK_FADE"]
    for title, population in (("QUICK_FADE", quick), ("all plans", plans)):
        known = [row for row in population if row["rhythm_safe"] != ""]
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
    return lines


def _style_line(label: str, rows: list[Row]) -> str:
    """One style bucket's outcome: N/A, short fades, overlap, tier, vocals and rhythm."""
    plans = [row for row in rows if row["outcome"] == "plan"]
    if not plans:
        return f"  {label:28s} pairs {len(rows):5d} (no plans)"
    overlaps = np.array([row["overlap_s"] for row in plans])
    one_sided = sum(1 for row in plans if row["vocal_class"] in ("outgoing only", "incoming only"))
    rhythm_safe = sum(1 for row in plans if row["rhythm_safe"] is True)
    quick = sum(1 for row in plans if row["tier"] == "QUICK_FADE")
    return (
        f"  {label:28s} pairs {len(rows):5d} "
        f"N/A {100 * (len(rows) - len(plans)) / len(rows):4.1f}% "
        f"<8s {100 * np.mean(overlaps < SHORT_FADE_SECONDS):5.1f}% "
        f"median {np.median(overlaps):5.2f}s "
        f"QF {100 * quick / len(plans):5.1f}% "
        f"one-sided vocal {100 * one_sided / len(plans):5.1f}% "
        f"rhythm-safe {100 * rhythm_safe / len(plans):5.1f}%"
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


if __name__ == "__main__":
    sys.exit(main())
