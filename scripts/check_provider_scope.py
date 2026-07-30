"""
Advisory CI check: warn when a provider PR also changes unrelated, shared code.

A change that touches a single provider should normally stay inside that provider's package and
tests; mixing in unrelated core/helper/model edits makes review harder and couples the provider's
fate to changes that belong in their own PR. This is intentionally **advisory** — it emits GitHub
warning annotations (never fails the build), because some cross-cutting changes are legitimate.

Reads the changed file paths (one per line) from stdin, mirroring ``ci_test_scope``. Generated
artifacts that naturally accompany a provider change (``requirements_all.txt`` and the
``music_assistant/translations`` catalog) are not counted as out-of-scope.

Usage:
    git diff --name-only origin/dev... | uv run -m scripts.check_provider_scope
"""

from __future__ import annotations

import sys

# ruff: noqa: T201

# Generated files that are a legitimate by-product of a provider change (manifest deps -> the
# requirements lock; provider strings.json -> the translations catalog).
GENERATED_PREFIXES = ("music_assistant/translations/",)
GENERATED_FILES = frozenset({"requirements_all.txt"})


def provider_name(path: str) -> str | None:
    """Return the provider a path belongs to, or None if it is not provider-scoped."""
    parts = path.split("/")
    if len(parts) >= 4 and parts[0] in ("music_assistant", "tests") and parts[1] == "providers":
        return parts[2]
    return None


def classify(changed: list[str]) -> tuple[set[str], list[str]]:
    """
    Split changed paths into the providers they touch and the shared files outside any provider.

    :param changed: Changed file paths, relative to the repo root.
    :return: ``(providers, shared_files)`` — the set of touched provider names and the shared
        server/test files that fall outside any provider (excluding generated artifacts).
    """
    providers: set[str] = set()
    shared: list[str] = []
    for path in changed:
        if (provider := provider_name(path)) is not None:
            providers.add(provider)
        elif path in GENERATED_FILES or path.startswith(GENERATED_PREFIXES):
            continue
        elif path.startswith(("music_assistant/", "tests/")):
            shared.append(path)
    return providers, shared


def main() -> int:
    """Read changed files from stdin and emit advisory warnings; always returns 0."""
    changed = [line.strip() for line in sys.stdin if line.strip()]
    providers, shared = classify(changed)
    if not providers or not shared:
        return 0
    provider_list = ", ".join(sorted(providers))
    for path in shared:
        print(
            f"::warning file={path}::This file is outside the changed provider(s) "
            f"({provider_list}); consider splitting unrelated changes into a dedicated PR."
        )
    print(
        f"Provider PR ({provider_list}) also changes {len(shared)} shared file(s); "
        "consider moving unrelated changes to their own PR.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
