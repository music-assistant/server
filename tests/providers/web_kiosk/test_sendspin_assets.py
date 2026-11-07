"""Tests for the packaged Sendspin browser assets."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from music_assistant.providers.web_kiosk.http_server import STATIC_DIR

if TYPE_CHECKING:
    from aiohttp.test_utils import TestClient

BUNDLE_ROOT = STATIC_DIR / "web" / "sendspin-js"
PROVIDER_ROOT = STATIC_DIR.parent


def test_sendspin_bundle_ships_the_sources_that_build_it() -> None:
    """BUILD.json names build inputs that travel with the provider for review and rebuilds."""
    metadata = json.loads((BUNDLE_ROOT / "BUILD.json").read_text(encoding="utf-8"))

    entry = PROVIDER_ROOT / metadata["source"]
    build = PROVIDER_ROOT / metadata["build"]
    assert entry.is_file()
    assert build.is_file()
    assert "createSendspinClaimProof" in entry.read_text(encoding="utf-8")
    assert "sendspin-js" in build.read_text(encoding="utf-8")


def test_sendspin_bundle_integrity() -> None:
    """The synchronized bundle includes its version, checksum and licenses."""
    metadata = json.loads((BUNDLE_ROOT / "BUILD.json").read_text(encoding="utf-8"))
    assert metadata["packages"]["@sendspin/sendspin-js"] == "5.0.0"
    assert (BUNDLE_ROOT / "VERSION.txt").read_text().strip() == "@sendspin/sendspin-js@5.0.0"
    assert hashlib.sha256((BUNDLE_ROOT / "index.js").read_bytes()).hexdigest() == metadata["sha256"]
    assert metadata["licensePlacement"] == "bundle-header"
    header = (BUNDLE_ROOT / "index.js").read_text(encoding="utf-8").split("*/", 1)[0]
    for package, version in metadata["packages"].items():
        assert f"{package}@{version}" in header
    assert "Apache License" in header
    assert "The MIT License (MIT)" in header
    assert "Copyright (c) 2022 Paul Miller" in header


async def test_sendspin_bundle_is_served(http_client: TestClient[Any, Any]) -> None:
    """The browser can fetch the generated ESM entry through the kiosk route."""
    response = await http_client.get("/web/sendspin-js/index.js")
    assert response.status == 200
    assert response.content_type in {"text/javascript", "application/javascript"}
    assert await response.read() == (BUNDLE_ROOT / "index.js").read_bytes()
