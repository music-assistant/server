"""Proxy prefixes must survive OIDC canonicalization, cookies and callback destinations."""

from __future__ import annotations

import pytest
from aiohttp.test_utils import make_mocked_request
from yarl import URL

from music_assistant.controllers.webserver.controller import WebserverController, _oidc_cookie_name
from music_assistant.controllers.webserver.helpers.oidc_provider import OIDCLoginProvider

from .test_oidc import IdentityProvider, auth_manager, idp, provider

__all__ = ["auth_manager", "idp", "provider"]


@pytest.mark.parametrize("prefix", ["", "/ma", "/music/assistant", "/music%20room", "/ma%2Ftenant"])
@pytest.mark.parametrize("entry", ["http://music.example", "https://alias.example"])
@pytest.mark.parametrize("destination", ["default", "web", "native"])
async def test_proxy_prefix_survives_canonical_redirect_and_callback(
    provider: OIDCLoginProvider,
    idp: IdentityProvider,
    monkeypatch: pytest.MonkeyPatch,
    prefix: str,
    entry: str,
    destination: str,
) -> None:
    """Aliases and HTTPS upgrades retain the configured application path end to end."""
    base = f"https://music.example{prefix}"
    monkeypatch.setattr(WebserverController, "base_url", property(lambda _: base))
    monkeypatch.setattr(WebserverController, "external_url", property(lambda _: base))
    return_url = {
        "default": None,
        "web": f"{base}/?view=players#queue",
        "native": "musicassistant://auth",
    }[destination]
    url = URL(f"{entry}{prefix}/auth/oidc/start")
    if return_url:
        url = url.with_query(return_url=return_url)
    auth = provider.auth_manager
    response = await auth.webserver._handle_oidc_start(make_mocked_request("GET", str(url)))
    expected = URL(f"{base}/auth/oidc/start")
    if return_url:
        expected = expected.with_query(return_url=return_url)
    assert response.status == 302
    assert URL(response.headers["Location"]) == expected
    assert not response.cookies
    assert not auth.pending_logins._pending

    canonical = await auth.webserver._handle_oidc_start(
        make_mocked_request("GET", str(expected), headers={"X-Forwarded-Proto": "https"})
    )
    state = URL(canonical.headers["Location"]).query["state"]
    pending = auth.pending_logins.get(state)
    assert pending is not None
    assert pending.redirect_uri == f"{base}/auth/callback?provider_id=oidc"
    cookie = canonical.cookies[_oidc_cookie_name(state)]
    assert cookie["secure"]
    assert cookie["httponly"]
    assert not cookie["domain"]
    idp.pending = pending
    result = await auth.handle_oauth_callback("oidc", "valid-code", state)
    assert result.success
    assert result.return_url == (return_url or base)
