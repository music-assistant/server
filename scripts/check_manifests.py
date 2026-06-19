"""
Fail when a provider ``manifest.json`` is missing mandatory keys or is internally inconsistent.

Every provider ships a ``manifest.json`` whose mandatory fields (``type``, ``domain``, ``name``,
``description``, ``codeowners``) are required by ``ProviderManifest`` at load time. A manifest that
omits one of them — or whose ``domain`` does not match its folder, whose ``type`` is not a real
provider type, or whose ``codeowners`` are malformed — only fails once the server tries to load the
provider. This pre-commit/CI guard scans every provider manifest up front and prints each problem.

When invoked with file or folder arguments (as pre-commit does for the changed files) only the
manifests in scope are validated; with no arguments every provider manifest is scanned.

Usage:
    uv run -m scripts.check_manifests [PATH ...]
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

# ruff: noqa: T201

# repo paths (this file lives at <repo>/scripts/check_manifests.py)
REPO_ROOT = Path(__file__).resolve().parents[1]
PROVIDERS_PATH = REPO_ROOT / "music_assistant" / "providers"

# Mandatory keys, mirroring the non-defaulted fields of
# music_assistant_models.provider.ProviderManifest.
MANDATORY_KEYS = ("type", "domain", "name", "description", "codeowners")

# Valid provider types, mirroring music_assistant_models.enums.ProviderType (minus ``unknown``,
# which is a runtime fallback and must never be authored into a manifest).
VALID_TYPES = frozenset({"music", "player", "metadata", "plugin", "core", "audio_analysis"})


def find_violations(manifest_paths: Iterable[Path] | None = None) -> list[str]:
    """
    Return a sorted list of ``path: message`` for every malformed provider manifest.

    :param manifest_paths: Specific ``manifest.json`` files to validate; when omitted every
        provider manifest in the tree is scanned.
    """
    if manifest_paths is None:
        manifest_paths = PROVIDERS_PATH.glob("*/manifest.json")
    violations: list[str] = []
    for manifest_path in sorted(manifest_paths):
        rel = manifest_path.relative_to(REPO_ROOT).as_posix()
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as err:
            violations.append(f"{rel}: invalid JSON ({err})")
            continue
        violations.extend(f"{rel}: {issue}" for issue in _validate(data, manifest_path.parent.name))
    return violations


def main(argv: Sequence[str] | None = None) -> int:
    """Print every malformed provider manifest; return 1 when any were found."""
    args = list(sys.argv[1:] if argv is None else argv)
    violations = find_violations(_resolve_manifests(args) if args else None)
    if not violations:
        return 0
    print("Invalid provider manifest.json file(s) found:", file=sys.stderr)
    for violation in violations:
        print(f"  {violation}", file=sys.stderr)
    return 1


def _resolve_manifests(args: Sequence[str]) -> list[Path]:
    """
    Map file/folder arguments to the ``manifest.json`` paths they cover.

    A ``manifest.json`` argument is taken as-is; a provider folder contributes its own (and any
    direct child's) manifest. Only top-level provider manifests
    (``music_assistant/providers/<name>/manifest.json``) are kept; nested files such as a vendored
    web app's ``manifest.json`` are ignored.

    :param args: Raw path arguments (e.g. the files pre-commit passes for a commit).
    """
    manifests: set[Path] = set()
    for raw in args:
        path = Path(raw).resolve()
        if path.is_dir():
            manifests.update(path.glob("manifest.json"))
            manifests.update(path.glob("*/manifest.json"))
        elif path.name == "manifest.json" and path.is_file():
            manifests.add(path)
    return sorted(p for p in manifests if _is_provider_manifest(p))


def _is_provider_manifest(path: Path) -> bool:
    """Return True when ``path`` is a top-level provider ``manifest.json``."""
    try:
        parts = path.resolve().relative_to(PROVIDERS_PATH).parts
    except ValueError:
        return False
    return len(parts) == 2 and parts[1] == "manifest.json"


def _validate(data: dict[str, object], folder_name: str) -> list[str]:
    """
    Return every problem found in a single parsed manifest.

    :param data: The parsed manifest contents.
    :param folder_name: The provider folder name the manifest must declare as its ``domain``.
    """
    issues: list[str] = []
    for key in MANDATORY_KEYS:
        if key not in data:
            issues.append(f"missing mandatory key '{key}'")
    provider_type = data.get("type")
    if provider_type is not None and (
        not isinstance(provider_type, str) or provider_type not in VALID_TYPES
    ):
        issues.append(f"type '{provider_type}' is not one of {sorted(VALID_TYPES)}")
    if (domain := data.get("domain")) is not None and domain != folder_name:
        issues.append(f"domain '{domain}' must match the provider folder name '{folder_name}'")
    for text_key in ("name", "description"):
        value = data.get(text_key)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            issues.append(f"'{text_key}' must be a non-empty string")
    if "codeowners" in data:
        issues.extend(_validate_codeowners(data["codeowners"]))
    return issues


def _validate_codeowners(codeowners: object) -> list[str]:
    """Return problems with the ``codeowners`` field (must be a non-empty list of ``@`` handles)."""
    if not isinstance(codeowners, list) or not codeowners:
        return ["'codeowners' must be a non-empty list"]
    return [
        f"codeowner '{owner}' must be a string starting with '@'"
        for owner in codeowners
        if not (isinstance(owner, str) and owner.startswith("@"))
    ]


if __name__ == "__main__":
    raise SystemExit(main())
