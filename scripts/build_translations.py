"""
Generate the English source file that Lokalise syncs against.

Concatenates every ``strings.json`` authoring file (music_assistant/strings.json + each
per-provider/per-controller strings.json) into one flat, fully-qualified ``key -> English``
JSON at ``music_assistant/translations/en.json``. ``lokalise-upload.yml`` pushes that file to
Lokalise; ``lokalise-download.yml`` pulls the translated languages back into translations/.

Standalone (no ``music_assistant`` imports) so it runs under any music-assistant-models version
and without the full server import chain.

Usage:
    uv run -m scripts.build_translations            # (re)generate the source file
    uv run -m scripts.build_translations --check    # verify it is up to date (CI/pre-commit)
"""

from __future__ import annotations

import os
import sys
from typing import Any

import orjson

# ruff: noqa: T201

# repo paths (this file lives at <repo>/scripts/build_translations.py)
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE_ROOT = os.path.join(_REPO_ROOT, "music_assistant")
PROVIDERS_PATH = os.path.join(PACKAGE_ROOT, "providers")
CONTROLLERS_PATH = os.path.join(PACKAGE_ROOT, "controllers")
TRANSLATIONS_PATH = os.path.join(PACKAGE_ROOT, "translations")
# the shared/common source strings file (subkeys: settings, media, ...) at the package root
ROOT_STRINGS_FILE = os.path.join(PACKAGE_ROOT, "strings.json")

SOURCE_LANGUAGE = "en"
SOURCE_FILE = os.path.join(TRANSLATIONS_PATH, f"{SOURCE_LANGUAGE}.json")
COMMON_PREFIX = "common."


def build_translations_source() -> dict[str, str]:
    """Assemble the flat English source from all authoring strings.json files."""
    source: dict[str, str] = {}
    for prefix, path in _collect_source_files():
        with open(path, "rb") as file:
            data = orjson.loads(file.read())
        _flatten_into(data, prefix, source)
    return source


def _collect_source_files() -> list[tuple[str, str]]:
    """Discover all English source strings.json files as (key prefix, path) pairs."""
    source_files: list[tuple[str, str]] = []
    # shared/common strings at the package root
    if os.path.isfile(ROOT_STRINGS_FILE):
        source_files.append((COMMON_PREFIX, ROOT_STRINGS_FILE))
    # per-provider strings (sibling of manifest.json); skip template/test providers (their
    # strings must not reach Lokalise as translator noise)
    for entry in _iter_subdirs(PROVIDERS_PATH):
        if entry.startswith("_") or entry == "test":
            continue
        path = os.path.join(PROVIDERS_PATH, entry, "strings.json")
        if os.path.isfile(path):
            source_files.append((f"provider.{entry}.", path))
    # per-package-controller strings
    for entry in _iter_subdirs(CONTROLLERS_PATH):
        path = os.path.join(CONTROLLERS_PATH, entry, "strings.json")
        if os.path.isfile(path):
            source_files.append((f"core.{entry}.", path))
    return source_files


def _iter_subdirs(path: str) -> list[str]:
    """Return non-hidden subdirectory names of a path (empty if it does not exist)."""
    if not os.path.isdir(path):
        return []
    return [
        entry
        for entry in os.listdir(path)  # noqa: PTH208
        if not entry.startswith(".") and os.path.isdir(os.path.join(path, entry))
    ]


def _flatten_into(data: dict[str, Any], prefix: str, out: dict[str, str]) -> None:
    """Flatten a nested strings dict into dotted, prefixed keys with string leaves."""
    for key, value in data.items():
        full_key = f"{prefix}{key}"
        if isinstance(value, dict):
            _flatten_into(value, f"{full_key}.", out)
        elif isinstance(value, str):
            out[full_key] = value


def _render(catalog: dict[str, str]) -> bytes:
    """Render the catalog as deterministic, sorted, indented JSON."""
    return orjson.dumps(
        dict(sorted(catalog.items())),
        option=orjson.OPT_INDENT_2 | orjson.OPT_APPEND_NEWLINE,
    )


def main() -> int:
    """Generate (or, with --check, validate) the Lokalise source file."""
    rendered = _render(build_translations_source())
    if "--check" in sys.argv[1:]:
        existing = b""
        if os.path.isfile(SOURCE_FILE):
            with open(SOURCE_FILE, "rb") as file:
                existing = file.read()
        if existing != rendered:
            print(
                f"{SOURCE_FILE} is out of date. "
                "Run `uv run -m scripts.build_translations` and commit the result.",
                file=sys.stderr,
            )
            return 1
        return 0
    os.makedirs(TRANSLATIONS_PATH, exist_ok=True)
    with open(SOURCE_FILE, "wb") as file:
        file.write(rendered)
    print(f"Wrote {len(build_translations_source())} source strings to {SOURCE_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
