"""
Fail when a provider icon SVG exceeds the 5 KB size budget.

Provider icons (``icon.svg``, ``icon_dark.svg``, ``icon_monochrome.svg``, ...) ship inside the
package and are served to every UI client, so an unoptimized multi-hundred-KB export bloats the
install and the wire. New icons must stay at or below 5 KB; the icons that already exceeded it when
this check was introduced are grandfathered through ``scripts/lint_baselines/oversized_provider_icons.txt``
and should be shrunk over time (run an SVG optimizer such as ``svgo``).

Usage:
    uv run -m scripts.check_provider_icons
    uv run -m scripts.check_provider_icons --update-baseline   # after (re)optimizing icons
"""

from __future__ import annotations

import sys
from pathlib import Path

from scripts.lint_baseline import diff_baseline, load_baseline, render_baseline

# ruff: noqa: T201

# repo paths (this file lives at <repo>/scripts/check_provider_icons.py)
REPO_ROOT = Path(__file__).resolve().parents[1]
PROVIDERS_PATH = REPO_ROOT / "music_assistant" / "providers"
BASELINE_PATH = REPO_ROOT / "scripts" / "lint_baselines" / "oversized_provider_icons.txt"

# Maximum allowed size for a provider icon SVG (5 KB).
MAX_ICON_SIZE = 5 * 1024

_BASELINE_HEADER = (
    "Provider icon SVGs that already exceeded the 5 KB budget when the check was introduced.\n"
    "New icons must stay <= 5 KB; shrink these (e.g. with svgo) and drop them from this list.\n"
    "Regenerate with: uv run -m scripts.check_provider_icons --update-baseline"
)


def find_oversized_icons() -> dict[str, int]:
    """Return ``{repo-relative path: 1}`` for every provider icon SVG larger than ``MAX_ICON_SIZE``."""
    # Only top-level provider icons (``providers/<name>/*.svg``); nested vendored assets are skipped.
    return {
        svg_path.relative_to(REPO_ROOT).as_posix(): 1
        for svg_path in sorted(PROVIDERS_PATH.glob("*/*.svg"))
        if svg_path.stat().st_size > MAX_ICON_SIZE
    }


def main(argv: list[str] | None = None) -> int:
    """Report provider icons that newly exceed the size budget; return 1 when any were found."""
    argv = sys.argv[1:] if argv is None else argv
    current = find_oversized_icons()
    if "--update-baseline" in argv:
        BASELINE_PATH.write_text(render_baseline(current, _BASELINE_HEADER), encoding="utf-8")
        print(f"Wrote {len(current)} entries to {BASELINE_PATH.relative_to(REPO_ROOT)}")
        return 0
    regressions, improvements = diff_baseline(current, load_baseline(BASELINE_PATH))
    if not regressions and not improvements:
        return 0
    if regressions:
        print(
            f"Provider icon SVG(s) exceed the {MAX_ICON_SIZE // 1024} KB budget:", file=sys.stderr
        )
        for path in sorted(regressions):
            print(f"  {path} ({(REPO_ROOT / path).stat().st_size} bytes)", file=sys.stderr)
        print("Optimize them (e.g. with svgo) to <= 5 KB.", file=sys.stderr)
    if improvements:
        print(
            "These icons are now within budget; drop them from the baseline with "
            "`uv run -m scripts.check_provider_icons --update-baseline`:",
            file=sys.stderr,
        )
        for path in sorted(improvements):
            print(f"  {path}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
