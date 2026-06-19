"""
Generate the English source file that Lokalise syncs against.

Concatenates every ``strings.json`` authoring file (music_assistant/strings.json + each
per-provider/per-controller strings.json) into one flat, fully-qualified ``key -> English``
JSON at ``music_assistant/translations/en.json``. ``lokalise-upload.yml`` pushes that file to
Lokalise; ``lokalise-download.yml`` pulls the translated languages back into translations/.

A string value may reference another (typically shared ``common.``) string with the Home
Assistant style token ``[%key:owner::path::to::key%]`` (``::`` separates the dotted key
segments). Such a reference declares that an owner reuses a shared string: the target is
validated to exist and the referencing key is omitted from the generated source, so Lokalise
translates the shared string once while the server resolves the owner's key at runtime via its
owner -> common fallback.

Standalone (no ``music_assistant`` imports) so it runs under any music-assistant-models version
and without the full server import chain.

Usage:
    uv run -m scripts.build_translations            # (re)generate the source file
    uv run -m scripts.build_translations --check    # verify it is up to date (CI/pre-commit)
"""

from __future__ import annotations

import os
import re
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
# Home Assistant style reference token: [%key:owner::path::to::key%] -> owner.path.to.key
REFERENCE_PATTERN = re.compile(r"^\[%key:(.+)%\]$")


def build_translations_source() -> dict[str, str]:
    """Assemble the flat English source from all authoring strings.json files."""
    raw: dict[str, str] = {}
    for prefix, path in _collect_source_files():
        with open(path, "rb") as file:
            data = orjson.loads(file.read())
        _flatten_into(data, prefix, raw)
    return _resolve_references(raw)


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


def _resolve_references(raw: dict[str, str]) -> dict[str, str]:
    """
    Drop reference-valued keys after validating each points at an existing concrete string.

    A reference value ``[%key:owner::path::to::key%]`` declares that this key reuses a shared
    string defined elsewhere; the referenced key stays the single translatable source, so the
    referencing key is left out of the generated catalog (the server resolves it at runtime via
    its owner -> common fallback).

    :param raw: The flattened catalog, still containing any reference-valued keys.
    :raises ValueError: When a reference points at a key that is not a concrete string.
    """
    concrete = {key: value for key, value in raw.items() if not REFERENCE_PATTERN.match(value)}
    unresolved: list[str] = []
    for key, value in raw.items():
        if not (match := REFERENCE_PATTERN.match(value)):
            continue
        target = match.group(1).replace("::", ".")
        if target not in concrete:
            unresolved.append(f"{key} -> {target}")
    if unresolved:
        raise ValueError(
            "Unresolved translation reference target(s): " + ", ".join(sorted(unresolved))
        )
    return concrete


def _render(catalog: dict[str, str]) -> bytes:
    """Render the catalog as deterministic, sorted, indented JSON."""
    return orjson.dumps(
        dict(sorted(catalog.items())),
        option=orjson.OPT_INDENT_2 | orjson.OPT_APPEND_NEWLINE,
    )


def main() -> int:
    """Generate (or, with --check, validate) the Lokalise source file."""
    try:
        source = build_translations_source()
    except ValueError as err:
        print(str(err), file=sys.stderr)
        return 1
    rendered = _render(source)
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
    print(f"Wrote {len(source)} source strings to {SOURCE_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
