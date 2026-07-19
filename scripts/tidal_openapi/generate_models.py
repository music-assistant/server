"""
Regenerate the Tidal official-API TypedDict models from the vendored OpenAPI spec.

This produces ``music_assistant/providers/tidal/_openapi_models.py`` from the
attribute schemas listed in ``SEED_SCHEMAS`` (plus their transitive ``$ref``
closure). Only the ``*_Attributes`` payloads are generated: the JSON:API
envelope (``data`` / ``included`` / ``links``) is handled generically in the
provider's api client, not modelled here.

Usage::

    python scripts/tidal_openapi/generate_models.py

To cover a new area in a later slice, add its ``*_Attributes`` schema name to
``SEED_SCHEMAS`` and rerun. To refresh the spec itself, re-download it (see
``README.md``) and rerun; a ``git diff`` on the spec then shows what changed
upstream (new fields, deprecations, removals).
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

# ruff: noqa: S603, S607, T201

HERE = Path(__file__).parent
SPEC_PATH = HERE / "tidal-api-oas.json"
OUTPUT_PATH = HERE.parent.parent / "music_assistant" / "providers" / "tidal" / "_openapi_models.py"

# Attribute schemas the provider parses. Extend this list as later slices add
# coverage (e.g. "Playlists_Attributes", "Artworks_Attributes").
SEED_SCHEMAS = [
    "Tracks_Attributes",
    "Albums_Attributes",
    "Artists_Attributes",
    "Playlists_Attributes",
]

FILE_HEADER = '''"""
TypedDict models for the official TIDAL API (openapi.tidal.com/v2).

DO NOT EDIT BY HAND. This file is generated from the vendored OpenAPI spec.
Regenerate with: python scripts/tidal_openapi/generate_models.py
"""
# ruff: noqa'''


def _collect_refs(obj: Any) -> list[str]:
    """Return the schema names referenced by ``$ref`` anywhere within ``obj``."""
    found: list[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "$ref" and isinstance(value, str):
                found.append(value.split("/")[-1])
            else:
                found.extend(_collect_refs(value))
    elif isinstance(obj, list):
        for value in obj:
            found.extend(_collect_refs(value))
    return found


def _closure(schemas: dict[str, Any], seeds: list[str]) -> set[str]:
    """Return the transitive ``$ref`` closure of ``seeds`` within ``schemas``."""
    seen: set[str] = set()
    stack = list(seeds)
    while stack:
        name = stack.pop()
        if name in seen or name not in schemas:
            continue
        seen.add(name)
        stack.extend(_collect_refs(schemas[name]))
    return seen


def main() -> int:
    """Generate the models file and return a process exit code."""
    spec = json.loads(SPEC_PATH.read_text())
    schemas = spec["components"]["schemas"]

    missing = [name for name in SEED_SCHEMAS if name not in schemas]
    if missing:
        print(f"Seed schemas not found in spec: {missing}", file=sys.stderr)
        return 1

    wanted = _closure(schemas, SEED_SCHEMAS)
    minimal = {
        "openapi": spec.get("openapi", "3.1.0"),
        "info": {"title": "TIDAL API (subset)", "version": spec["info"]["version"]},
        "paths": {},
        "components": {"schemas": {name: schemas[name] for name in sorted(wanted)}},
    }
    print(f"Generating {len(wanted)} schemas from {len(SEED_SCHEMAS)} seeds")

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tmp:
        json.dump(minimal, tmp)
        tmp_path = Path(tmp.name)

    try:
        subprocess.run(
            [
                "uvx",
                "--from",
                "datamodel-code-generator",
                "datamodel-codegen",
                "--input",
                str(tmp_path),
                "--input-file-type",
                "openapi",
                "--output",
                str(OUTPUT_PATH),
                "--output-model-type",
                "typing.TypedDict",
                "--target-python-version",
                "3.14",
                "--use-double-quotes",
                "--disable-timestamp",
                "--custom-file-header",
                FILE_HEADER,
            ],
            check=True,
        )
        # Match the repo's formatting so regeneration produces a stable diff.
        subprocess.run(["ruff", "format", str(OUTPUT_PATH)], check=True)
    finally:
        tmp_path.unlink(missing_ok=True)

    print(f"Wrote {OUTPUT_PATH.relative_to(HERE.parent.parent)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
