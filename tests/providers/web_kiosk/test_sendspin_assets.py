"""Tests that the vendored Sendspin browser client can load from the static server."""

from __future__ import annotations

import re
from pathlib import Path

VENDOR_ROOT = Path(__file__).resolve().parents[1] / "provider" / "static" / "web" / "sendspin-js"
# A real import statement. Comments that mention a package name do not count.
STATEMENT_FROM = re.compile(r"""^(?:import|export)\b.*?\bfrom\s+['"]([^'"]+)['"]""", re.MULTILINE)
DYNAMIC_IMPORT = re.compile(r"""import\s*\(\s*['"]([^'"]+)['"]""")
# opus-encdec is an optional fallback for browsers without WebCodecs. The kiosk
# asks those browsers for FLAC/PCM instead of shipping the package.
ALLOWED_BARE_IMPORTS = {
    "opus-encdec/dist/libopus-decoder.js",
    "opus-encdec/src/oggOpusDecoder.js",
}


def test_vendored_sendspin_js_is_5() -> None:
    """The kiosk ships the latest published sendspin-js release."""
    version = (VENDOR_ROOT / "VERSION.txt").read_text(encoding="utf-8")
    assert version.strip() == "@sendspin/sendspin-js@5.0.0"


def test_vendored_sendspin_js_imports_are_browser_loadable() -> None:
    """
    Every import points at a file the static server can return.

    Static ``from "@noble/..."`` imports count too. A browser rejects those
    package names, which is how Sendspin mode failed to start.
    """
    missing: list[str] = []
    bare: set[str] = set()
    for js in VENDOR_ROOT.rglob("*.js"):
        text = js.read_text(encoding="utf-8")
        for spec in STATEMENT_FROM.findall(text):
            if spec.startswith("."):
                target = (js.parent / spec).resolve()
                if not target.is_file():
                    missing.append(f"{js.relative_to(VENDOR_ROOT)} -> {spec}")
            else:
                bare.add(spec)
        for spec in DYNAMIC_IMPORT.findall(text):
            if spec.startswith("."):
                target = (js.parent / spec).resolve()
                if not target.is_file():
                    missing.append(f"{js.relative_to(VENDOR_ROOT)} -> {spec}")
            else:
                bare.add(spec)

    assert missing == []
    assert bare == ALLOWED_BARE_IMPORTS
    noble = (VENDOR_ROOT / "vendor" / "NOBLE.txt").read_text(encoding="utf-8")
    assert "@noble/curves@1.9.7" in noble
    assert (VENDOR_ROOT / "vendor" / "noble-curves" / "ed25519.js").is_file()
