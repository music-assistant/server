"""Tests pinning documentation against authoritative code constants."""

from __future__ import annotations

from pathlib import Path

from music_assistant.providers.yandex_ynison.streaming import PCM_LOSSLESS_PARAMS

_REPO_ROOT = Path(__file__).resolve().parent.parent


def test_claude_local_md_documents_lossless_profile_correctly() -> None:
    """
    `CLAUDE.local.md` auto-detection sentence must match the code constants.

    Derives the expected substring from `PCM_LOSSLESS_PARAMS` so a future
    constant change either flows through to the doc or fails this test
    rather than silently rotting.
    """
    bit_depth = PCM_LOSSLESS_PARAMS["bit_depth"]
    # `:g` renders 44100 → "44.1" and 48000 → "48" (no trailing ".0"), so the
    # pinned substring reads naturally regardless of the constant's value.
    sample_rate_khz = f"{PCM_LOSSLESS_PARAMS['sample_rate'] / 1000:g}"
    expected = f"{bit_depth}-bit/{sample_rate_khz}kHz"

    text = (_REPO_ROOT / "CLAUDE.local.md").read_text(encoding="utf-8")
    assert expected in text, (
        f"CLAUDE.local.md must document the lossless profile as {expected!r} "
        f"(matching provider.streaming.PCM_LOSSLESS_PARAMS); "
        f"found neither — doc has drifted from the code."
    )
