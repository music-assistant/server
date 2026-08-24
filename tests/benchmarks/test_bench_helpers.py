"""
Benchmarks for hot-path string/parsing helpers.

These helpers run on every media item during library sync, matching and metadata
parsing, so they sit squarely on the performance-critical path. They are pure,
CPU-bound functions, which makes them a good fit for CodSpeed's deterministic
simulation instrument.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.helpers import create_safe_string

from music_assistant.helpers.compare import (
    compare_strings,
    compare_version,
    loose_compare_strings,
)
from music_assistant.helpers.json import json_dumps, json_loads
from music_assistant.helpers.util import (
    clean_stream_title,
    parse_title_and_version,
    sanitize_http_header_value,
)

if TYPE_CHECKING:
    from pytest_codspeed import BenchmarkFixture

# A representative mix of real-world track/album titles as they arrive from
# streaming providers: plain titles, featurings, remaster suffixes, bracketed
# version info and accented / non-ASCII characters.
SAMPLE_TITLES = [
    "Bohemian Rhapsody",
    "Blinding Lights (Official Music Video)",
    "Smells Like Teen Spirit - Remastered 2021",
    "One More Time (feat. Romanthony)",
    "Björk - Jóga",
    "Take On Me [Extended Version]",
    "Stairway to Heaven (Live at MSG)",
    "Get Lucky (Radio Edit) [feat. Pharrell Williams]",
    "Numb / Encore",
    "Sí, señor - Acoustic",
]

SAMPLE_VERSIONS = [
    ("Remastered 2011", "2011 Remaster"),
    ("Deluxe Edition", "Deluxe"),
    ("", "original soundtrack"),
    ("Radio Edit", "radio version"),
    ("Live", "Live Version"),
]

SAMPLE_STREAM_TITLES = [
    "StreamTitle='AC/DC - Thunderstruck';StreamUrl='';",
    'text="Daft Punk - Around the World" song_spot="M"',
    "The Beatles - Here Comes the Sun",
    'title="Adele - Hello" artist="Adele"',
    "Now playing: Coldplay - Yellow on Radio X - advert.com",
]


def test_compare_strings_strict(benchmark: BenchmarkFixture) -> None:
    """Exact (strict) string comparison across the sample title set."""

    def run() -> int:
        matches = 0
        for base in SAMPLE_TITLES:
            for alt in SAMPLE_TITLES:
                if compare_strings(base, alt, strict=True):
                    matches += 1
        return matches

    benchmark(run)


def test_compare_strings_fuzzy(benchmark: BenchmarkFixture) -> None:
    """Fuzzy string comparison (difflib path) across the sample title set."""

    def run() -> int:
        matches = 0
        for base in SAMPLE_TITLES:
            for alt in SAMPLE_TITLES:
                if compare_strings(base, alt, strict=False):
                    matches += 1
        return matches

    benchmark(run)


def test_create_safe_string(benchmark: BenchmarkFixture) -> None:
    """Unicode normalization + regex cleanup used before every compare."""

    def run() -> list[str]:
        return [create_safe_string(title) for title in SAMPLE_TITLES]

    benchmark(run)


def test_loose_compare_strings(benchmark: BenchmarkFixture) -> None:
    """Partial-match comparison used to group versions of the same item."""

    def run() -> int:
        matches = 0
        for base in SAMPLE_TITLES:
            for alt in SAMPLE_TITLES:
                if loose_compare_strings(base, alt):
                    matches += 1
        return matches

    benchmark(run)


def test_compare_version(benchmark: BenchmarkFixture) -> None:
    """Version string comparison across a mix of representative pairs."""

    def run() -> int:
        return sum(compare_version(base, alt) for base, alt in SAMPLE_VERSIONS)

    benchmark(run)


def test_parse_title_and_version(benchmark: BenchmarkFixture) -> None:
    """Standard title/version splitting run on every parsed media item."""

    def run() -> list[tuple[str, str]]:
        return [parse_title_and_version(title) for title in SAMPLE_TITLES]

    benchmark(run)


def test_parse_title_and_version_for_search(benchmark: BenchmarkFixture) -> None:
    """Aggressive title stripping used to build search queries."""

    def run() -> list[tuple[str, str]]:
        return [parse_title_and_version(title, strip_for_search=True) for title in SAMPLE_TITLES]

    benchmark(run)


def test_clean_stream_title(benchmark: BenchmarkFixture) -> None:
    """Radio StreamTitle parsing, executed for every ICY metadata update."""

    def run() -> list[str]:
        return [clean_stream_title(line) for line in SAMPLE_STREAM_TITLES]

    benchmark(run)


def test_sanitize_http_header_value(benchmark: BenchmarkFixture) -> None:
    """Header sanitization applied to track metadata on every stream request."""

    def run() -> list[str]:
        return [sanitize_http_header_value(title) for title in SAMPLE_TITLES]

    benchmark(run)


def test_json_dumps(benchmark: BenchmarkFixture) -> None:
    """orjson-backed serialization used across the API and cache layers."""
    payload = {
        "items": [
            {
                "item_id": str(idx),
                "name": title,
                "provider": "spotify",
                "media_type": "track",
                "duration": 180 + idx,
                "artists": ["Some Artist", "Another Artist"],
                "metadata": {"explicit": bool(idx % 2), "popularity": idx * 3},
            }
            for idx, title in enumerate(SAMPLE_TITLES)
        ]
    }

    benchmark(lambda: json_dumps(payload))


def test_json_loads(benchmark: BenchmarkFixture) -> None:
    """orjson-backed deserialization of a representative API payload."""
    payload = {
        "items": [
            {
                "item_id": str(idx),
                "name": title,
                "provider": "spotify",
                "media_type": "track",
                "duration": 180 + idx,
            }
            for idx, title in enumerate(SAMPLE_TITLES)
        ]
    }
    raw = json_dumps(payload)

    benchmark(lambda: json_loads(raw))
