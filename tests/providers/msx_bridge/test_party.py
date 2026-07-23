"""Tests for the Party Mode endpoints (spec 0001)."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock

import segno

from music_assistant.providers.msx_bridge.http_server import _render_qr

if TYPE_CHECKING:
    import pytest
    from aiohttp.test_utils import TestClient

JOIN_URL = "http://ma.local:8095/?join=ABC123"


def _party_mock(
    url: str | None = JOIN_URL,
    name: str | None = "My Party",
    qr_text: str | None = "Scan to join!",
) -> Mock:
    """Return a mock Party plugin provider."""
    party = Mock()
    party.get_party_url = AsyncMock(return_value=url)
    config = Mock()
    config.party_name = name
    config.qr_text = qr_text
    party.get_party_config = AsyncMock(return_value=config)
    return party


# --- /api/party status endpoint ---


async def test_party_status_no_plugin(http_client: TestClient[Any, Any]) -> None:
    """GET /api/party should report inactive when the Party plugin is not loaded."""
    resp = await http_client.get("/api/party")
    assert resp.status == 200
    data = await resp.json()
    assert data == {"active": False}


async def test_party_status_guest_access_off(
    http_client: TestClient[Any, Any], mass_mock: Mock
) -> None:
    """GET /api/party should report inactive when guest access is disabled."""
    mass_mock.get_provider = Mock(return_value=_party_mock(url=None))
    resp = await http_client.get("/api/party")
    assert resp.status == 200
    data = await resp.json()
    assert data == {"active": False}


async def test_party_status_active(http_client: TestClient[Any, Any], mass_mock: Mock) -> None:
    """GET /api/party should return party name, caption and QR URL when active."""
    mass_mock.get_provider = Mock(return_value=_party_mock())
    resp = await http_client.get("/api/party")
    assert resp.status == 200
    data = await resp.json()
    assert data["active"] is True
    assert data["name"] == "My Party"
    assert data["qr_text"] == "Scan to join!"
    # relative so the kiosk works behind reverse proxies / mapped ports
    assert data["qr_url"] == "/api/party/qr.svg"
    # opaque version so clients refetch the QR only when the join code rotates
    assert data["qr_version"]
    assert JOIN_URL not in data["qr_version"]
    mass_mock.get_provider.assert_called_with("party")


async def test_party_status_survives_plugin_errors(
    http_client: TestClient[Any, Any], mass_mock: Mock
) -> None:
    """A raising Party plugin must degrade to inactive, not a 500."""
    party = _party_mock()
    party.get_party_url = AsyncMock(side_effect=RuntimeError("guest access broken"))
    mass_mock.get_provider = Mock(return_value=party)
    resp = await http_client.get("/api/party")
    assert resp.status == 200
    assert await resp.json() == {"active": False}


async def test_party_status_is_cached(http_client: TestClient[Any, Any], mass_mock: Mock) -> None:
    """Repeated status requests within the cache TTL must not re-query the plugin."""
    party = _party_mock()
    mass_mock.get_provider = Mock(return_value=party)
    resp1 = await http_client.get("/api/party")
    resp2 = await http_client.get("/api/party")
    assert resp1.status == resp2.status == 200
    party.get_party_url.assert_awaited_once()


async def test_party_status_active_without_custom_texts(
    http_client: TestClient[Any, Any], mass_mock: Mock
) -> None:
    """GET /api/party should tolerate unset party name and caption."""
    mass_mock.get_provider = Mock(return_value=_party_mock(name=None, qr_text=None))
    resp = await http_client.get("/api/party")
    assert resp.status == 200
    data = await resp.json()
    assert data["active"] is True
    assert data["name"] is None
    assert data["qr_text"] is None


async def test_party_status_does_not_leak_join_url(
    http_client: TestClient[Any, Any], mass_mock: Mock
) -> None:
    """GET /api/party must not include the raw join URL in the response."""
    mass_mock.get_provider = Mock(return_value=_party_mock())
    resp = await http_client.get("/api/party")
    body = await resp.text()
    assert JOIN_URL not in body


# --- /api/party/qr.svg image endpoint ---


async def test_party_qr_svg_active(http_client: TestClient[Any, Any], mass_mock: Mock) -> None:
    """GET /api/party/qr.svg should return an SVG QR code of the join URL."""
    mass_mock.get_provider = Mock(return_value=_party_mock())
    resp = await http_client.get("/api/party/qr.svg")
    assert resp.status == 200
    assert "image/svg+xml" in resp.headers["Content-Type"]
    body = await resp.text()
    assert "<svg" in body


async def test_party_qr_not_cached(http_client: TestClient[Any, Any], mass_mock: Mock) -> None:
    """The QR image must not be cacheable — the join code can rotate."""
    mass_mock.get_provider = Mock(return_value=_party_mock())
    resp = await http_client.get("/api/party/qr.svg")
    assert resp.status == 200
    assert "no-store" in resp.headers.get("Cache-Control", "")


async def test_party_qr_404_no_plugin(http_client: TestClient[Any, Any]) -> None:
    """GET /api/party/qr.svg should 404 when the Party plugin is not loaded."""
    resp = await http_client.get("/api/party/qr.svg")
    assert resp.status == 404


async def test_party_qr_404_guest_access_off(
    http_client: TestClient[Any, Any], mass_mock: Mock
) -> None:
    """GET /api/party/qr.svg should 404 when guest access is disabled."""
    mass_mock.get_provider = Mock(return_value=_party_mock(url=None))
    resp = await http_client.get("/api/party/qr.svg")
    assert resp.status == 404


async def test_party_qr_png(http_client: TestClient[Any, Any], mass_mock: Mock) -> None:
    """GET /api/party/qr.png should return a PNG QR for MSX pages on TVs without SVG."""
    mass_mock.get_provider = Mock(return_value=_party_mock())
    resp = await http_client.get("/api/party/qr.png")
    assert resp.status == 200
    assert "image/png" in resp.headers["Content-Type"]
    body = await resp.read()
    assert body.startswith(b"\x89PNG")


async def test_party_qr_png_404_inactive(http_client: TestClient[Any, Any]) -> None:
    """GET /api/party/qr.png should 404 when no party is active."""
    resp = await http_client.get("/api/party/qr.png")
    assert resp.status == 404


def test_render_qr_is_memoized(monkeypatch: pytest.MonkeyPatch) -> None:
    """Repeated QR renders for the same join URL and kind reuse the cached bytes."""
    calls: list[int] = []
    real_make = segno.make

    def _tracking_make(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return real_make(*args, **kwargs)

    monkeypatch.setattr(segno, "make", _tracking_make)
    _render_qr.cache_clear()
    first = _render_qr(JOIN_URL, "png")
    second = _render_qr(JOIN_URL, "png")
    assert first == second
    assert len(calls) == 1


async def test_party_qr_render_runs_off_event_loop(
    http_client: TestClient[Any, Any], mass_mock: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QR generation must run in a worker thread, never on the event loop."""
    mass_mock.get_provider = Mock(return_value=_party_mock())
    loop_thread = threading.get_ident()
    make_threads: list[int] = []
    real_make = segno.make

    def _tracking_make(*args: Any, **kwargs: Any) -> Any:
        make_threads.append(threading.get_ident())
        return real_make(*args, **kwargs)

    monkeypatch.setattr(segno, "make", _tracking_make)
    resp = await http_client.get("/api/party/qr.png")
    assert resp.status == 200
    assert make_threads
    assert loop_thread not in make_threads


# --- /msx/party.json native MSX page ---


async def test_msx_party_page_active(http_client: TestClient[Any, Any], mass_mock: Mock) -> None:
    """GET /msx/party.json should show the QR image item when a party is active."""
    mass_mock.get_provider = Mock(return_value=_party_mock())
    resp = await http_client.get("/msx/party.json")
    assert resp.status == 200
    data = await resp.json()
    assert data["headline"] == "My Party"
    items = data["items"]
    # PNG, not SVG: MSX image slots on older TV engines cannot decode SVG
    assert any(item.get("image", "").endswith("/api/party/qr.png") for item in items)
    body = await resp.text()
    assert JOIN_URL not in body


async def test_msx_party_page_refreshes_player_activity(
    http_client: TestClient[Any, Any], mass_mock: Mock
) -> None:
    """Viewing the party page must register/refresh the player like other MSX pages."""
    resp = await http_client.get("/msx/party.json?device_id=partytv")
    assert resp.status == 200
    mass_mock.players.register.assert_awaited()


async def test_msx_party_page_inactive(http_client: TestClient[Any, Any]) -> None:
    """GET /msx/party.json should render a page without a QR when no party is active."""
    resp = await http_client.get("/msx/party.json")
    assert resp.status == 200
    data = await resp.json()
    assert not any("/api/party/qr.svg" in (item.get("image") or "") for item in data["items"])


# --- MSX menu entry ---


async def test_msx_menu_shows_party_when_active(
    http_client: TestClient[Any, Any], mass_mock: Mock
) -> None:
    """The MSX menu should include a Party entry pointing at the party page when active."""
    mass_mock.get_provider = Mock(return_value=_party_mock())
    resp = await http_client.get("/msx/menu.json")
    assert resp.status == 200
    data = await resp.json()
    party_items = [i for i in data["items"] if i.get("label") == "Party"]
    assert len(party_items) == 1
    assert "/msx/party.json" in party_items[0]["content"]


async def test_msx_menu_hides_party_when_inactive(http_client: TestClient[Any, Any]) -> None:
    """The MSX menu should not show a Party entry when no party is active."""
    resp = await http_client.get("/msx/menu.json")
    assert resp.status == 200
    data = await resp.json()
    assert not any(i.get("label") == "Party" for i in data["items"])


async def test_msx_menu_survives_plugin_errors(
    http_client: TestClient[Any, Any], mass_mock: Mock
) -> None:
    """A raising Party plugin must not break the main menu."""
    party = _party_mock()
    party.get_party_url = AsyncMock(side_effect=RuntimeError("guest access broken"))
    mass_mock.get_provider = Mock(return_value=party)
    resp = await http_client.get("/msx/menu.json")
    assert resp.status == 200
    data = await resp.json()
    assert any(i.get("label") == "Albums" for i in data["items"])
    assert not any(i.get("label") == "Party" for i in data["items"])


async def test_tv_plugin_menu_has_party_entry(http_client: TestClient[Any, Any]) -> None:
    """The TV interaction plugin's client-side menu must link to the party page."""
    resp = await http_client.get("/msx/plugin.html")
    assert resp.status == 200
    body = await resp.text()
    assert "/msx/party.json" in body
