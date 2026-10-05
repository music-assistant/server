"""
Fail when a provider or controller icon exceeds its size budget.

Icons (``icon.svg``, ``icon_dark.svg``, ``icon.png``, ...) ship inside the package and are served
to every UI client, so an unoptimized export bloats the install and the wire. Budgets:

- SVG: 5 KB (vector, should stay tiny).
- PNG: 20 KB (raster, needs more headroom than vector).

New icons must stay within budget; icons that already exceeded it when this check was introduced are
grandfathered through ``scripts/lint_baselines/oversized_provider_icons.txt`` and should be shrunk
over time (optimize svgs with a tool such as ``svgo``, and re-export/compress oversized pngs).

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
BASELINE_PATH = REPO_ROOT / "scripts" / "lint_baselines" / "oversized_provider_icons.txt"

# Icons live one level below these roots (``<root>/<name>/icon*.svg|png``).
ICON_ROOTS = (
    REPO_ROOT / "music_assistant" / "providers",
    REPO_ROOT / "music_assistant" / "controllers",
)

# Maximum allowed size per format. Raster pngs get more headroom than vector svgs.
MAX_SIZE_BY_SUFFIX = {".svg": 5 * 1024, ".png": 20 * 1024}

# The exact icon filenames that get served to clients; keep in sync with detect_provider_icons
# in music_assistant/helpers/images.py. Only these are size-checked, so unrelated svg/png assets
# that may live in a provider/controller folder are left alone.
ICON_FILENAMES = tuple(
    f"{stem}{suffix}"
    for stem in ("icon", "icon_dark", "icon_monochrome")
    for suffix in MAX_SIZE_BY_SUFFIX
)

_BASELINE_HEADER = (
    "Provider/controller icons that already exceeded their size budget when the check was "
    "introduced.\nNew icons must stay within budget (svg <= 5 KB, png <= 20 KB); shrink these and "
    "drop them from this list.\nRegenerate with: uv run -m scripts.check_provider_icons "
    "--update-baseline"
)


def find_oversized_icons() -> dict[str, int]:
    """Return ``{repo-relative path: 1}`` for every icon file larger than its per-format budget."""
    # Match only the known icon filenames one level below each root (``<root>/<name>/<icon>``),
    # so unrelated svg/png assets in a provider/controller folder are never flagged.
    oversized: dict[str, int] = {}
    for root in ICON_ROOTS:
        for name in ICON_FILENAMES:
            for icon_path in sorted(root.glob(f"*/{name}")):
                if icon_path.stat().st_size > MAX_SIZE_BY_SUFFIX[icon_path.suffix.lower()]:
                    oversized[icon_path.relative_to(REPO_ROOT).as_posix()] = 1
    return oversized


def main(argv: list[str] | None = None) -> int:
    """Report icons that newly exceed their size budget; return 1 when any were found."""
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
        print("Icon(s) exceed their size budget (svg <= 5 KB, png <= 20 KB):", file=sys.stderr)
        for path in sorted(regressions):
            budget = MAX_SIZE_BY_SUFFIX[Path(path).suffix.lower()]
            size = (REPO_ROOT / path).stat().st_size
            print(f"  {path} ({size} bytes, budget {budget // 1024} KB)", file=sys.stderr)
        print("Optimize them to fit the budget.", file=sys.stderr)
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
