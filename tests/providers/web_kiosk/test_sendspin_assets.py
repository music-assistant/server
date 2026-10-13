"""Tests for the packaged Sendspin browser assets."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from music_assistant.providers.web_kiosk.http_server import STATIC_DIR

if TYPE_CHECKING:
    from aiohttp.test_utils import TestClient

BUNDLE_ROOT = STATIC_DIR / "web" / "sendspin-js"


def test_sendspin_bundle_integrity() -> None:
    """The synchronized bundle includes its version, checksum and licenses."""
    metadata = json.loads((BUNDLE_ROOT / "BUILD.json").read_text(encoding="utf-8"))
    assert metadata["packages"]["@sendspin/sendspin-js"] == "5.0.0"
    assert (BUNDLE_ROOT / "VERSION.txt").read_text().strip() == "@sendspin/sendspin-js@5.0.0"
    assert hashlib.sha256((BUNDLE_ROOT / "index.js").read_bytes()).hexdigest() == metadata["sha256"]
    for package in metadata["packages"]:
        filename = package.replace("@", "").replace("/", "-") + ".txt"
        assert (BUNDLE_ROOT / "licenses" / filename).read_text(encoding="utf-8").strip()


async def test_sendspin_bundle_is_served(http_client: TestClient[Any, Any]) -> None:
    """The browser can fetch the generated ESM entry through the kiosk route."""
    response = await http_client.get("/web/sendspin-js/index.js")
    assert response.status == 200
    assert response.content_type in {"text/javascript", "application/javascript"}
    assert await response.read() == (BUNDLE_ROOT / "index.js").read_bytes()
