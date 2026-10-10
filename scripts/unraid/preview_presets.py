r"""
Render one cover per AI Artwork style preset, to compare them before picking one.

Standalone (standard library only), so it runs anywhere Python 3.11+ is available, such as the
Cronicle container. The prompts come straight from the provider's constants.py, so they match
what Music Assistant sends.

Usage:
    python3 preview_presets.py --name "Indie Dance Sunset" \
        --context "Seed songs/playlists that define the vibe: Innerbloom - RÜFÜS DU SOL. " \
        --gemini-key-file /jobs/playlist-art/google_api_key --out /tmp/previews
    python3 preview_presets.py --name "Pride" --openrouter-key sk-or-... \
        --model google/gemini-2.5-flash-image --overlay-free
"""

from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import sys
import urllib.request
from pathlib import Path
from types import ModuleType
from typing import Any

# a standalone script that reports on stdout
# ruff: noqa: T201, INP001

CONSTANTS = (
    Path(__file__).resolve().parents[2] / "music_assistant/providers/ai_artwork/constants.py"
)


def main() -> None:
    """Generate the previews."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--name", required=True, help="playlist name")
    parser.add_argument("--context", default="", help="context sentences, as the provider adds")
    parser.add_argument("--styles", default="spotify,music_assistant,apple_music")
    parser.add_argument("--gemini-key-file", help="file holding a Google AI Studio API key")
    parser.add_argument("--openrouter-key", help="OpenRouter API key")
    parser.add_argument("--model", help="model id (default depends on the service)")
    parser.add_argument(
        "--overlay-free", action="store_true", help="ask for artwork without the title"
    )
    parser.add_argument("--constants", default=str(CONSTANTS), help="path to constants.py")
    parser.add_argument("--out", default="previews")
    parser.add_argument("--dry-run", action="store_true", help="only print the prompts")
    args = parser.parse_args()
    if not args.dry_run and not args.gemini_key_file and not args.openrouter_key:
        parser.error("pass --gemini-key-file or --openrouter-key")

    constants = _load_constants(args.constants)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for style_name in args.styles.split(","):
        preset = constants.STYLE_PRESETS[constants.Style(style_name.strip())]
        if args.overlay_free:
            text_rules = constants.NO_TEXT_RULES.format(area=preset.title_area)
        else:
            text_rules = f"{preset.typography.format(title=args.name)} {constants.TITLE_RULES}"
        prompt = constants.BASE_PROMPT.format(
            name=args.name,
            context=args.context.strip() + " " if args.context.strip() else "",
            concept=constants.DEFAULT_CONCEPT,
            style=f"{preset.art} {text_rules} {constants.FULL_BLEED}",
        )
        print(f"--- {style_name}\n{prompt}\n")
        if args.dry_run:
            continue
        if args.openrouter_key:
            image = _openrouter(
                args.openrouter_key, args.model or "google/gemini-2.5-flash-image", prompt
            )
        else:
            key = Path(args.gemini_key_file).read_text().strip()
            image = _gemini(key, args.model or "gemini-3.1-flash-image", prompt)
        target = out / f"{style_name}.png"
        target.write_bytes(image)
        print(f"wrote {target}\n")


def _load_constants(path: str) -> ModuleType:
    """Import the provider's constants module from its file."""
    spec = importlib.util.spec_from_file_location("ai_artwork_constants", path)
    if spec is None or spec.loader is None:
        sys.exit(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _post(url: str, body: dict[str, Any], headers: dict[str, str] | None = None) -> dict[str, Any]:
    request = urllib.request.Request(  # noqa: S310
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    with urllib.request.urlopen(request, timeout=300) as response:  # noqa: S310
        result: dict[str, Any] = json.loads(response.read())
        return result


def _gemini(key: str, model: str, prompt: str) -> bytes:
    response = _post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
        {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseModalities": ["IMAGE"],
                "imageConfig": {"aspectRatio": "1:1"},
            },
        },
    )
    for part in response["candidates"][0]["content"]["parts"]:
        if "inlineData" in part:
            return base64.b64decode(part["inlineData"]["data"])
    sys.exit(f"no image in response: {json.dumps(response)[:300]}")


def _openrouter(key: str, model: str, prompt: str) -> bytes:
    response = _post(
        "https://openrouter.ai/api/v1/chat/completions",
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "modalities": ["image", "text"],
        },
        {"Authorization": f"Bearer {key}"},
    )
    for image in response["choices"][0]["message"].get("images") or []:
        return base64.b64decode(image["image_url"]["url"].split(",", 1)[1])
    sys.exit(f"no image in response: {json.dumps(response)[:300]}")


if __name__ == "__main__":
    main()
