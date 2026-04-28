"""Low-level client for the undocumented dialogs.yandex.ru developer API.

Implements the 8-step sequence captured from Chrome DevTools HAR for
creating a Smart Home skill with account-linking:

    1. GET  /developer                       → extract CSRF (secretkey)
    2. GET  /developer/app-store-api/snapshot → existing skills list (optional)
    3. POST /developer/app-store-api/apps                      → skill_id
    4. POST /developer/app-store-api/apps/{id}/draft/upload-logo → logo_id
    5. PATCH /developer/app-store-api/apps/{id}/draft/update    → settings
    6. POST /developer/app-store-api/oauth/apps                → oauth_app_id
    7. POST /developer/app-store-api/apps/{id}/oauthApp        → bind oauth
    8. POST /developer/app-store-api/apps/{id}/draft/request-deploy → publish

This is an UNDOCUMENTED, PRIVATE API. It may break at any time. The
caller is responsible for surfacing that risk to the user (see
``provider.auto_skill_ui``).

Authentication: passport session cookies (``Session_id`` / ``sessionid2``)
must already be present in the supplied ``aiohttp.ClientSession``'s
cookie jar. Obtain them via ``ya_passport_auth.PassportClient``:

    creds = await client.login_device_code(...)
    await client.refresh_passport_cookies(creds.x_token)
    creator = DialogsSkillCreator(client._session)

The CSRF token (returned by ``fetch_csrf``) must be passed as the
``x-csrf-token`` header on every mutating request.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp

from .auto_skill_state import SkillCreationArtifacts, SkillCreationState
from .constants import (
    CLOUD_OAUTH_AUTHORIZE_URL,
    CLOUD_OAUTH_TOKEN_URL,
    CLOUD_SKILL_CLIENT_ID_TEMPLATE,
    CLOUD_SKILL_CLIENT_SECRET,
    CLOUD_SKILL_WEBHOOK_TEMPLATE,
    CONNECTION_TYPE_CLOUD_PLUS,
    CONNECTION_TYPE_DIRECT,
    DIRECT_API_BASE_PATH,
    DIRECT_AUTH_BASE_PATH,
    DIRECT_OAUTH_CLIENT_ID,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Mapping

    from music_assistant.mass import MusicAssistant

__all__ = [
    "DEVICE_FLOW_TIMEOUT_SECONDS",
    "DIALOGS_API_BASE",
    "DIALOGS_CSRF_REGEX",
    "DIALOGS_DEV_BASE",
    "DIALOGS_DEV_HTML_URL",
    "DialogsApiError",
    "DialogsCsrfError",
    "DialogsDuplicateSkillError",
    "DialogsSkillCreator",
    "auto_create_skill",
    "build_draft_payload",
    "build_oauth_app_payload",
    "check_preconditions",
    "derive_auth_urls",
    "derive_backend_uri",
    "derive_client_id",
    "load_default_logo_bytes",
]

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Endpoints / patterns
# ---------------------------------------------------------------------------
DIALOGS_DEV_BASE = "https://dialogs.yandex.ru"
DIALOGS_DEV_HTML_URL = f"{DIALOGS_DEV_BASE}/developer"
DIALOGS_API_BASE = f"{DIALOGS_DEV_BASE}/developer/app-store-api"

# The developer console embeds a CSRF token in its HTML as:
#   ..."secretkey":"u9c94f1aca53bf156be4..."...
# Captured from HAR 2026-04-24. If Yandex re-renders differently, this
# regex will miss and ``fetch_csrf`` raises ``DialogsCsrfError`` so the
# user falls back to manual setup.
DIALOGS_CSRF_REGEX = re.compile(r'"secretkey":"([^"]+)"')

SMART_HOME_CHANNEL = "smartHome"
_MAX_HTML_RESPONSE_BYTES = 2 * 1024 * 1024  # 2 MiB

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class DialogsApiError(Exception):
    """Base error for dialogs.yandex.ru API failures."""

    def __init__(
        self,
        message: str,
        *,
        step: str,
        http_status: int | None = None,
        yandex_error: str | None = None,
    ) -> None:
        """Initialise with the pipeline step that failed for clearer messages."""
        super().__init__(message)
        self.step = step
        self.http_status = http_status
        self.yandex_error = yandex_error


class DialogsCsrfError(DialogsApiError):
    """Raised when the CSRF token cannot be extracted from the developer page."""


class DialogsDuplicateSkillError(DialogsApiError):
    """Raised when create_app rejects because a skill with the same name exists."""


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class DialogsSkillCreator:
    """Thin async wrapper over dialogs.yandex.ru developer-console API.

    Every method is idempotent on the transport layer: a single call
    either succeeds or raises. Retry / state-machine logic lives in the
    orchestrator (see :func:`auto_create_skill`).
    """

    __slots__ = ("_logger", "_session")

    def __init__(
        self,
        session: aiohttp.ClientSession,
        logger: logging.Logger | None = None,
    ) -> None:
        """Take a session that already carries Passport auth cookies."""
        self._session = session
        self._logger = logger or _LOGGER

    # -----------------------------------------------------------------------
    # Step 1: CSRF token extraction
    # -----------------------------------------------------------------------

    async def fetch_csrf(self) -> str:
        """Fetch the developer page HTML and extract the CSRF ``secretkey``.

        Caller uses the returned value as the ``x-csrf-token`` header on
        all mutating requests. Returns a fresh token on every call; the
        orchestrator caches it for the duration of a single attempt.
        """
        async with self._session.get(DIALOGS_DEV_HTML_URL) as resp:
            if resp.status == 401:
                raise DialogsApiError(
                    "not authenticated — passport session cookies missing or expired",
                    step="fetch_csrf",
                    http_status=401,
                )
            if resp.status != 200:
                raise DialogsApiError(
                    f"dialogs.yandex.ru/developer returned HTTP {resp.status}",
                    step="fetch_csrf",
                    http_status=resp.status,
                )
            # Enforce the size cap while reading so an oversized
            # response can't buffer fully in memory (T5 pattern from
            # ya-passport-auth).
            body = bytearray()
            async for chunk in resp.content.iter_chunked(8192):
                body.extend(chunk)
                if len(body) > _MAX_HTML_RESPONSE_BYTES:
                    raise DialogsApiError(
                        "developer page response exceeded size cap",
                        step="fetch_csrf",
                    )
            html = body.decode(resp.get_encoding() or "utf-8", errors="replace")

        match = DIALOGS_CSRF_REGEX.search(html)
        if not match:
            raise DialogsCsrfError(
                "could not locate CSRF token in developer page HTML — "
                "Yandex may have changed the rendering format",
                step="fetch_csrf",
            )
        token = match.group(1).strip()
        if not token:
            raise DialogsCsrfError(
                "CSRF token matched but is empty",
                step="fetch_csrf",
            )
        self._logger.debug("dialogs CSRF token fetched (len=%d)", len(token))
        return token

    # -----------------------------------------------------------------------
    # Step 2: list existing skills (for duplicate-name detection)
    # -----------------------------------------------------------------------

    async def list_existing_skills(self, csrf: str) -> list[dict[str, Any]]:
        """Return the user's existing skills from the snapshot endpoint.

        The dashboard uses this to populate its skill list; we use it to
        warn the user before they hit a duplicate-name 4xx on create_app.
        """
        url = f"{DIALOGS_API_BASE}/snapshot"
        data = await self._get_json(url, csrf=csrf, step="list_existing_skills")
        result = data.get("result")
        if not isinstance(result, dict):
            return []
        skills = result.get("skills")
        if not isinstance(skills, list):
            return []
        return [s for s in skills if isinstance(s, dict)]

    # -----------------------------------------------------------------------
    # Step 3: create the skill app
    # -----------------------------------------------------------------------

    async def create_app(self, csrf: str, name: str) -> str:
        """Create a Smart Home skill with the given name.

        Returns the newly-minted ``skill_id`` (UUID). Raises
        :class:`DialogsDuplicateSkillError` if the name is already taken
        by another skill on this account.
        """
        url = f"{DIALOGS_API_BASE}/apps"
        payload = {
            "channel": SMART_HOME_CHANNEL,
            "language": "ru",
            "isYangoConsole": False,
            "appName": name,
        }
        data = await self._post_json(url, payload, csrf=csrf, step="create_app")
        result = data.get("result")
        if not isinstance(result, dict):
            raise DialogsApiError(
                "create_app response missing 'result' object",
                step="create_app",
            )
        skill_id = result.get("id") or result.get("skill_id")
        if not isinstance(skill_id, str) or not skill_id:
            raise DialogsApiError(
                "create_app response missing skill id",
                step="create_app",
            )
        self._logger.info("dialogs skill created: id=%s name=%r", skill_id, name)
        return skill_id

    # -----------------------------------------------------------------------
    # Step 4: upload logo
    # -----------------------------------------------------------------------

    async def upload_logo(self, csrf: str, skill_id: str, png: bytes) -> str:
        """Upload a PNG logo for the skill.

        Returns a ``logo_id`` that must be referenced in ``update_draft``.
        The logo file is sent as multipart with the field name ``file``
        and filename ``icon.png`` (matching the HAR capture).
        """
        url = f"{DIALOGS_API_BASE}/apps/{skill_id}/draft/upload-logo?channel={SMART_HOME_CHANNEL}"
        form = aiohttp.FormData()
        form.add_field(
            "file",
            png,
            filename="icon.png",
            content_type="image/png",
        )
        headers = {"x-csrf-token": csrf}
        async with self._session.post(url, data=form, headers=headers) as resp:
            body = await resp.text()
            if resp.status != 200:
                raise DialogsApiError(
                    f"upload_logo HTTP {resp.status}: {body[:200]}",
                    step="upload_logo",
                    http_status=resp.status,
                )
            data = _try_json(body)
        result = data.get("result") if isinstance(data, dict) else None
        if not isinstance(result, dict):
            raise DialogsApiError(
                "upload_logo response missing 'result'",
                step="upload_logo",
            )
        logo_id = result.get("id")
        if not isinstance(logo_id, str) or not logo_id:
            raise DialogsApiError(
                "upload_logo response missing logo id",
                step="upload_logo",
            )
        return logo_id

    # -----------------------------------------------------------------------
    # Step 5: update draft settings
    # -----------------------------------------------------------------------

    async def update_draft(self, csrf: str, skill_id: str, payload: Mapping[str, Any]) -> None:
        """PATCH the skill draft with backend URL / publishing metadata."""
        url = f"{DIALOGS_API_BASE}/apps/{skill_id}/draft/update"
        await self._patch_json(url, dict(payload), csrf=csrf, step="update_draft")

    # -----------------------------------------------------------------------
    # Step 6: create OAuth app (account-linking)
    # -----------------------------------------------------------------------

    async def create_oauth_app(
        self,
        csrf: str,
        *,
        name: str,
        client_id: str,
        client_secret: str,
        authorize_url: str,
        token_url: str,
        refresh_url: str,
    ) -> str:
        """Create the OAuth app that powers account-linking in the skill.

        Returns the OAuth-app UUID which is then bound to the skill via
        ``attach_oauth``.
        """
        url = f"{DIALOGS_API_BASE}/oauth/apps"
        payload = {
            "name": name,
            "clientId": client_id,
            "clientSecret": client_secret,
            "authorizationUrl": authorize_url,
            "tokenUrl": token_url,
            "refreshTokenUrl": refresh_url,
            "scope": "",
            "yandexClientId": "",
        }
        data = await self._post_json(url, payload, csrf=csrf, step="create_oauth_app")
        result = data.get("result")
        if not isinstance(result, dict):
            raise DialogsApiError(
                "create_oauth_app response missing 'result'",
                step="create_oauth_app",
            )
        oauth_app_id = result.get("id")
        if not isinstance(oauth_app_id, str) or not oauth_app_id:
            raise DialogsApiError(
                "create_oauth_app response missing oauth app id",
                step="create_oauth_app",
            )
        return oauth_app_id

    # -----------------------------------------------------------------------
    # Step 7: bind OAuth app to the skill
    # -----------------------------------------------------------------------

    async def attach_oauth(self, csrf: str, skill_id: str, oauth_app_id: str) -> None:
        """Attach an existing OAuth app to the skill's account-linking slot."""
        url = f"{DIALOGS_API_BASE}/apps/{skill_id}/oauthApp?channel={SMART_HOME_CHANNEL}"
        payload = {"oauthAppId": oauth_app_id}
        await self._post_json(url, payload, csrf=csrf, step="attach_oauth")

    # -----------------------------------------------------------------------
    # Step 8: publish (send for moderation)
    # -----------------------------------------------------------------------

    async def request_deploy(self, csrf: str, skill_id: str) -> None:
        """Send the draft to moderation / publish.

        Body is empty; all params are in the query string. Returns on
        2xx; otherwise raises.
        """
        url = (
            f"{DIALOGS_API_BASE}/apps/{skill_id}/draft/request-deploy?channel={SMART_HOME_CHANNEL}"
        )
        headers = {"x-csrf-token": csrf}
        async with self._session.post(url, headers=headers) as resp:
            body = await resp.text()
            if resp.status not in (200, 201, 202, 204):
                raise DialogsApiError(
                    f"request_deploy HTTP {resp.status}: {body[:200]}",
                    step="request_deploy",
                    http_status=resp.status,
                )

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    async def _get_json(self, url: str, *, csrf: str, step: str) -> dict[str, Any]:
        headers = {"x-csrf-token": csrf}
        async with self._session.get(url, headers=headers) as resp:
            body = await resp.text()
            if resp.status != 200:
                raise DialogsApiError(
                    f"GET {url} HTTP {resp.status}: {body[:200]}",
                    step=step,
                    http_status=resp.status,
                )
            data = _try_json(body)
        if not isinstance(data, dict):
            raise DialogsApiError(
                f"GET {url} returned non-object JSON",
                step=step,
            )
        return data

    async def _post_json(
        self, url: str, payload: dict[str, Any], *, csrf: str, step: str
    ) -> dict[str, Any]:
        return await self._send_json("POST", url, payload, csrf=csrf, step=step)

    async def _patch_json(
        self, url: str, payload: dict[str, Any], *, csrf: str, step: str
    ) -> dict[str, Any]:
        return await self._send_json("PATCH", url, payload, csrf=csrf, step=step)

    async def _send_json(
        self,
        method: str,
        url: str,
        payload: dict[str, Any],
        *,
        csrf: str,
        step: str,
    ) -> dict[str, Any]:
        headers = {
            "x-csrf-token": csrf,
            "content-type": "application/json",
        }
        async with self._session.request(method, url, json=payload, headers=headers) as resp:
            body = await resp.text()
            # Only ``create_app`` can fail with duplicate-name errors —
            # other endpoints use 409 for unrelated conflicts and would
            # be misclassified as duplicates if the mapping were global.
            duplicate_candidate = step == "create_app" and (
                resp.status == 409 or (resp.status in (400, 422) and _looks_like_duplicate(body))
            )
            if duplicate_candidate:
                raise DialogsDuplicateSkillError(
                    f"{step}: skill with this name already exists",
                    step=step,
                    http_status=resp.status,
                    yandex_error=_extract_error_code(body),
                )
            if resp.status not in (200, 201, 202):
                raise DialogsApiError(
                    f"{method} {url} HTTP {resp.status}: {body[:200]}",
                    step=step,
                    http_status=resp.status,
                    yandex_error=_extract_error_code(body),
                )
            data = _try_json(body)
        if not isinstance(data, dict):
            raise DialogsApiError(
                f"{method} {url} returned non-object JSON",
                step=step,
            )
        return data


# ---------------------------------------------------------------------------
# Module-private helpers
# ---------------------------------------------------------------------------


def _try_json(body: str) -> Any:
    """Parse JSON defensively — return None on any error."""
    if not body:
        return None
    try:
        return json.loads(body)
    except (ValueError, TypeError):
        return None


def _looks_like_duplicate(body: str) -> bool:
    """Heuristic for whether a 4xx body indicates a duplicate-name error."""
    if not body:
        return False
    lowered = body.lower()
    return any(
        kw in lowered for kw in ("already exists", "duplicate", "exists with name", "not_unique")
    )


def _extract_error_code(body: str) -> str | None:
    """Pull Yandex error code/message out of a 4xx response body (best-effort)."""
    data = _try_json(body)
    if not isinstance(data, dict):
        return None
    for key in ("error", "errorCode", "message", "code"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return None


# ---------------------------------------------------------------------------
# Pure helpers: backend/oauth URLs, payload builders, preconditions
#
# All of these are side-effect-free and separately unit-testable; the
# orchestrator in a later commit wires them together.
# ---------------------------------------------------------------------------


def derive_backend_uri(mass: MusicAssistant, connection_type: str) -> str:
    """Return the Backend URL the skill should point at for *connection_type*.

    cloud_plus → yaha-cloud.ru relay (fixed URL).
    direct     → ``{mass.webserver.base_url}`` + our API path (requires
                 HTTPS base URL; see :func:`check_preconditions`).
    """
    if connection_type == CONNECTION_TYPE_CLOUD_PLUS:
        return CLOUD_SKILL_WEBHOOK_TEMPLATE
    if connection_type == CONNECTION_TYPE_DIRECT:
        base = str(mass.webserver.base_url).rstrip("/")
        return f"{base}{DIRECT_API_BASE_PATH}"
    msg = f"auto-create is not supported for connection_type={connection_type!r}"
    raise ValueError(msg)


def derive_auth_urls(mass: MusicAssistant, connection_type: str) -> tuple[str, str]:
    """Return (authorize_url, token_url) for the OAuth app.

    cloud_plus uses the yaha-cloud relay's OAuth endpoints; direct
    uses the MA webserver's own authorize/token endpoints (served by
    provider/direct.py).
    """
    if connection_type == CONNECTION_TYPE_CLOUD_PLUS:
        return CLOUD_OAUTH_AUTHORIZE_URL, CLOUD_OAUTH_TOKEN_URL
    if connection_type == CONNECTION_TYPE_DIRECT:
        base = str(mass.webserver.base_url).rstrip("/")
        return (
            f"{base}{DIRECT_AUTH_BASE_PATH}/authorize",
            f"{base}{DIRECT_AUTH_BASE_PATH}/token",
        )
    msg = f"auto-create is not supported for connection_type={connection_type!r}"
    raise ValueError(msg)


def derive_client_id(connection_type: str, cloud_instance_id: str) -> str:
    """Return the OAuth client_id to register in the skill's account linking.

    cloud_plus uses ``yandex_smart_home:{instance_id}`` (yaha-cloud
    protocol); direct uses the fixed Yandex social redirect base URL
    (its existing Yandex OAuth client expects this exact ID).
    """
    if connection_type == CONNECTION_TYPE_CLOUD_PLUS:
        if not cloud_instance_id:
            msg = "cloud_plus requires a registered cloud_instance_id"
            raise ValueError(msg)
        return CLOUD_SKILL_CLIENT_ID_TEMPLATE.format(instance_id=cloud_instance_id)
    if connection_type == CONNECTION_TYPE_DIRECT:
        return DIRECT_OAUTH_CLIENT_ID
    msg = f"auto-create is not supported for connection_type={connection_type!r}"
    raise ValueError(msg)


def build_draft_payload(
    *,
    connection_type: str,
    skill_name: str,
    backend_uri: str,
    logo_id: str | None,
    developer_name: str = "Music Assistant user",
) -> dict[str, Any]:
    """Compose the PATCH /draft/update body for a Smart Home skill.

    Matches the HAR sample field-for-field; every key that the
    dashboard UI sends on save is reproduced so Yandex's validator
    sees a complete draft and allows ``request-deploy`` afterwards.
    """
    if connection_type not in (CONNECTION_TYPE_CLOUD_PLUS, CONNECTION_TYPE_DIRECT):
        msg = f"auto-create is not supported for connection_type={connection_type!r}"
        raise ValueError(msg)

    return {
        "logo2": None,
        "name": skill_name,
        "voice": "shitova.us",
        "logoId": logo_id,
        "skillAccess": "private",
        "hideInStore": False,
        "noteForModerator": "",
        "backendSettings": {
            "uri": backend_uri,
            "functionId": "",
            "backendType": "webhook",
        },
        "publishingSettings": {
            "brandVerificationWebsite": "",
            "category": "smart_home",
            "developerName": developer_name,
            "secondaryTitle": "",
            "email": "",  # server pulls from the authenticated session
            "smartHome": {
                "deepLinks": {
                    "android": {"url": ""},
                    "ios": {"url": "", "fallbackUrl": ""},
                },
            },
            "multilingualSettings": {
                "ru": {
                    "name": skill_name,
                    "secondaryTitle": "",
                    "externalSettingsDescription": skill_name,
                    "supportedUnitsDescription": skill_name,
                },
            },
        },
        "oauthAppId": None,
        "isTrustedSmartHomeSkill": False,
        "enableAllAvailableRegions": True,
        "selectedRegions": [],
        "channel": SMART_HOME_CHANNEL,
    }


def build_oauth_app_payload(
    *,
    skill_name: str,
    client_id: str,
    client_secret: str,
    authorize_url: str,
    token_url: str,
) -> dict[str, Any]:
    """Compose the POST /oauth/apps body for account-linking.

    Values come from :func:`derive_client_id`, :func:`derive_auth_urls`,
    and :func:`derive_backend_uri`'s caller context. ``refreshTokenUrl``
    always equals ``token_url`` — Yandex's flow uses the same endpoint
    for both grant types.
    """
    return {
        "name": skill_name,
        "clientId": client_id,
        "clientSecret": client_secret,
        "authorizationUrl": authorize_url,
        "tokenUrl": token_url,
        "refreshTokenUrl": token_url,
        "scope": "",
        "yandexClientId": "",
    }


def check_preconditions(
    *,
    connection_type: str,
    mass: MusicAssistant,
    cloud_instance_id: str,
    direct_client_secret: str,
) -> None:
    """Validate that auto-create can run for the given connection type.

    Raises :class:`ValueError` with a human-readable message on failure.
    Called before any network I/O so the UI can surface the error
    without a half-created skill on Yandex's side.
    """
    if connection_type == CONNECTION_TYPE_CLOUD_PLUS:
        if not cloud_instance_id:
            msg = (
                "Cloud Plus requires a registered yaha-cloud instance first. "
                "Use the 'Register with cloud' action."
            )
            raise ValueError(msg)
        return

    if connection_type == CONNECTION_TYPE_DIRECT:
        if not direct_client_secret:
            msg = "Direct mode requires a generated Client Secret"
            raise ValueError(msg)
        try:
            base = str(mass.webserver.base_url)
        except Exception as exc:
            msg = f"MA webserver base URL is not available: {exc}"
            raise ValueError(msg) from exc
        if not base.startswith("https://"):
            msg = (
                "Direct mode requires MA to be reachable over HTTPS from the "
                f"public internet (got base_url={base!r}). Yandex will reject "
                "a skill with a non-HTTPS backend."
            )
            raise ValueError(msg)
        return

    msg = (
        f"auto-create is not supported for connection_type={connection_type!r}; "
        "use cloud_plus or direct."
    )
    raise ValueError(msg)


# ---------------------------------------------------------------------------
# Orchestrator: device flow + resumable pipeline
# ---------------------------------------------------------------------------

# Hard cap on how long we'll wait for the user to enter the code.
DEVICE_FLOW_TIMEOUT_SECONDS = 300.0

_DEVICE_CODE_PAGE_PATH = "/yandex_smarthome/device_code"
# Keep the intermediate HTML page alive long enough for one more poll
# after state flips to done/failed — ~1s is plenty, the page polls
# every 2s so we're just covering the in-flight window.
_POST_AUTH_GRACE_SECONDS = 1
# Server-suggested interval from Yandex is 5s (RFC 8628) but after the
# user has confirmed the code we want to detect it promptly; 2s is the
# RFC-recommended minimum. If Yandex ever returns SLOW_DOWN, the library
# bumps the interval automatically.
_DEVICE_FLOW_POLL_INTERVAL = 2.0
_SAFE_SESSION_ID_RE = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")


def _build_device_code_page(user_code: str, verification_url: str, status_url: str) -> str:
    """Render the HTML page shown during Device Flow login.

    Yandex's ya.ru/device page does not pre-fill from query params and
    strips them on redirect-to-login, so the only reliable way to show
    the code is to host our own page in MA's webserver that displays
    the code prominently and opens ya.ru/device in a new tab.

    Pattern copied from ``ma-provider-yandex-station/provider/auth.py``.
    """
    import html  # noqa: PLC0415

    safe_code = html.escape(user_code)
    safe_url = html.escape(verification_url, quote=True)
    safe_status_url = json.dumps(status_url).replace("</", "<\\/")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <title>Yandex Smart Home — Device Code</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        :root {{ color-scheme: light; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            margin: 0; padding: 2rem 1rem;
            display: flex; align-items: center; justify-content: center;
            min-height: 100vh; box-sizing: border-box;
            background: #f5f5f7; color: #1d1d1f;
        }}
        .card {{
            background: #ffffff; color: #1d1d1f;
            border-radius: 14px; padding: 2rem;
            max-width: 28rem; width: 100%;
            box-shadow: 0 4px 20px rgba(0,0,0,.08);
            text-align: center;
        }}
        h1 {{ margin: 0 0 .5rem; font-size: 1.25rem; }}
        p {{ margin: .5rem 0 1.25rem; color: #4a4a52; line-height: 1.45; }}
        #code {{
            display: inline-block;
            font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
            font-size: 2rem; font-weight: 600; letter-spacing: .15em;
            padding: .75rem 1.25rem; border-radius: 10px;
            background: #f2f2f7; color: #1d1d1f;
            user-select: all;
        }}
        button, .btn {{
            display: inline-block; margin-top: 1.5rem; padding: .75rem 1.5rem;
            font-size: 1rem; font-weight: 600; text-decoration: none;
            border: none; border-radius: 10px; cursor: pointer;
            background: #ffcc00; color: #1d1d1f;
        }}
        button:hover, .btn:hover {{ background: #ffd633; }}
        #copy {{
            margin-top: .75rem; background: transparent; color: #1d1d1f;
            border: 1px solid #c8c8cd; padding: .4rem 1rem;
            font-size: .85rem; font-weight: 400;
        }}
        #copy:hover {{ background: #f2f2f7; }}
    </style>
</head>
<body>
    <div class="card" id="card">
        <h1>Authorise Music Assistant for skill creation</h1>
        <p>Open the link below, log in to your Yandex account, and enter this code.</p>
        <div id="code">{safe_code}</div>
        <div>
            <button id="copy" type="button">Copy code</button>
        </div>
        <a class="btn" href="{safe_url}" target="_blank" rel="noopener">Continue to Yandex</a>
    </div>
    <script>
        const copyButton = document.getElementById('copy');
        const codeElement = document.getElementById('code');
        const card = document.getElementById('card');
        const statusUrl = {safe_status_url};

        function selectCodeForManualCopy() {{
            if (!codeElement) return;
            const selection = window.getSelection();
            if (!selection) return;
            const range = document.createRange();
            range.selectNodeContents(codeElement);
            selection.removeAllRanges();
            selection.addRange(range);
            if (copyButton) copyButton.textContent = 'Press Ctrl/Cmd+C';
        }}

        copyButton?.addEventListener('click', async function() {{
            const code = codeElement?.textContent?.trim();
            if (!code) return;
            if (!navigator.clipboard?.writeText) {{
                selectCodeForManualCopy();
                return;
            }}
            try {{
                await navigator.clipboard.writeText(code);
                this.textContent = 'Copied';
            }} catch {{
                selectCodeForManualCopy();
            }}
        }});

        function showResult(title, message) {{
            card.innerHTML = '<h1>' + title + '</h1><p>' + message + '</p>';
        }}

        async function pollStatus() {{
            try {{
                const r = await fetch(statusUrl, {{ cache: 'no-store' }});
                if (r.ok) {{
                    const data = await r.json();
                    if (data.state === 'done') {{
                        showResult('Authorisation successful', 'You can close this window.');
                        setTimeout(() => {{ try {{ window.close(); }} catch (e) {{}} }}, 300);
                        return;
                    }}
                    if (data.state === 'failed') {{
                        showResult(
                            'Authorisation failed',
                            'Return to Music Assistant and try again.'
                        );
                        return;
                    }}
                }}
            }} catch (e) {{ /* network hiccup — retry */ }}
            setTimeout(pollStatus, 2000);
        }}
        setTimeout(pollStatus, 2000);
    </script>
</body>
</html>"""


async def _default_authenticator(
    *,
    mass: MusicAssistant,
    session_id: str,
    timeout: float,
) -> AsyncIterator[aiohttp.ClientSession]:
    """Real-world authentication path — runs Device Flow and yields a session.

    Serves an intermediate HTML page through MA's webserver so the user
    sees the short ``user_code`` (Yandex's ya.ru/device does not pre-fill
    from query params). The popup is opened via
    :class:`AuthenticationHelper` using the frontend-provided
    ``session_id`` — that's how MA's UI knows which popup session to
    render and later close.

    Pattern copied from ``ma-provider-yandex-station/provider/auth.py``.
    """
    from aiohttp import web  # noqa: PLC0415
    from ya_passport_auth import ClientConfig, PassportClient  # noqa: PLC0415
    from ya_passport_auth.config import DEFAULT_ALLOWED_HOSTS  # noqa: PLC0415

    from music_assistant.helpers.auth import AuthenticationHelper  # noqa: PLC0415

    if not _SAFE_SESSION_ID_RE.match(session_id):
        msg = "invalid session_id for device authentication"
        raise ValueError(msg)

    allowed = DEFAULT_ALLOWED_HOSTS | frozenset({"dialogs.yandex.ru"})
    config = ClientConfig(allowed_hosts=allowed)

    async with PassportClient.create(config=config) as client:
        device_session = await client.start_device_login()
        # Don't log user_code — it's a time-limited credential (grants
        # Yandex sign-in for the device-flow window) and writing it to
        # shared log backends would leak access.
        _LOGGER.info(
            "device flow started — verification_url=%s",
            device_session.verification_url,
        )

        page_path = f"{_DEVICE_CODE_PAGE_PATH}/{session_id}"
        status_path = f"{page_path}/status"
        # MA frontend requires an absolute URL in signal_event(AUTH_SESSION,
        # ...). The URL comes from mass.webserver.base_url which the user
        # configures in Settings → Core → Webserver → Base URL. If they
        # haven't touched it and MA is behind Docker/reverse-proxy, it may
        # point at an unreachable internal address — the warning log below
        # gives them the path so they can open it manually if the popup
        # fails to load.
        base_url = str(mass.webserver.base_url).rstrip("/")
        status_url = f"{base_url}{status_path}"
        page_url = f"{base_url}{page_path}"
        state = {"value": "pending"}

        page_html = _build_device_code_page(
            device_session.user_code,
            device_session.verification_url,
            status_url,
        )

        async def _serve_page(_request: web.Request) -> web.Response:
            return web.Response(
                text=page_html,
                content_type="text/html",
                charset="utf-8",
                headers={
                    "Cache-Control": "no-store",
                    "Pragma": "no-cache",
                    "Expires": "0",
                },
            )

        async def _serve_status(_request: web.Request) -> web.Response:
            return web.json_response(
                {"state": state["value"]},
                headers={"Cache-Control": "no-store"},
            )

        mass.webserver.register_dynamic_route(page_path, _serve_page, "GET")
        mass.webserver.register_dynamic_route(status_path, _serve_status, "GET")
        _LOGGER.warning(
            "auto-skill: device-code popup URL %s (path=%s) "
            "— if the popup does not open or points at an unreachable "
            "address, open the path directly in your browser (the page "
            "displays the user_code) or fix Settings → Core → Webserver "
            "→ Base URL",
            page_url,
            page_path,
        )
        try:
            async with AuthenticationHelper(mass, session_id) as auth_helper:
                auth_helper.send_url(page_url)
                try:
                    creds = await client.poll_device_until_confirmed(
                        device_session,
                        total_timeout=timeout,
                        poll_interval=_DEVICE_FLOW_POLL_INTERVAL,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    state["value"] = "failed"
                    await asyncio.sleep(_POST_AUTH_GRACE_SECONDS)
                    raise
                state["value"] = "done"
                await asyncio.sleep(_POST_AUTH_GRACE_SECONDS)
        finally:
            mass.webserver.unregister_dynamic_route(page_path, "GET")
            mass.webserver.unregister_dynamic_route(status_path, "GET")

        await client.refresh_passport_cookies(creds.x_token)
        yield client._session


def _build_authenticator_cm(
    authenticator: Callable[..., AsyncIterator[aiohttp.ClientSession]],
    *,
    mass: MusicAssistant,
    session_id: str,
    timeout: float,
) -> Any:
    """Wrap *authenticator* so it supports ``async with`` uniformly.

    The default implementation is a plain async generator, but callers
    may inject an already-decorated ``@asynccontextmanager``. Re-wrapping
    a CM factory with ``asynccontextmanager`` is *not* idempotent — the
    outer wrapper would call ``__anext__`` on the inner CM object and
    crash — so detect the CM result and pass it through unchanged.
    """
    result = authenticator(mass=mass, session_id=session_id, timeout=timeout)
    if hasattr(result, "__aenter__") and hasattr(result, "__aexit__"):
        return result

    # Adapt the async iterator returned above into a proper context
    # manager. We have to drive the existing iterator (not call the
    # authenticator again) to avoid leaving a half-created generator
    # unawaited and to preserve any work it already did (e.g. starting
    # a Device Flow session).
    @asynccontextmanager
    async def _cm() -> AsyncIterator[aiohttp.ClientSession]:
        session = await result.__anext__()
        try:
            yield session
        finally:
            try:
                await result.__anext__()
            except StopAsyncIteration:
                pass
            else:
                msg = "authenticator yielded more than one session"
                raise RuntimeError(msg)

    return _cm()


async def auto_create_skill(  # noqa: PLR0913
    *,
    mass: MusicAssistant,
    connection_type: str,
    skill_name: str,
    artifacts: SkillCreationArtifacts,
    cloud_instance_id: str,
    direct_client_secret: str,
    logo_bytes: bytes,
    session_id: str,
    progress_cb: Callable[[SkillCreationArtifacts], Awaitable[None]] | None = None,
    authenticator: Callable[..., AsyncIterator[aiohttp.ClientSession]] | None = None,
    creator_factory: Callable[[aiohttp.ClientSession], DialogsSkillCreator] | None = None,
    timeout: float = DEVICE_FLOW_TIMEOUT_SECONDS,
    developer_name: str = "Music Assistant user",
) -> SkillCreationArtifacts:
    """End-to-end flow: Device Flow → passport cookies → skill pipeline.

    Resumes from ``artifacts.state`` — steps that already completed
    (skill_id present, etc.) are skipped. On any exception, returns
    artifacts with ``state=FAILED`` and a human-readable ``last_error``
    instead of re-raising, so the config-flow UI can render the message
    without crashing.

    ``progress_cb`` is invoked after each successful step with the
    updated artifacts; the caller uses it to persist state to MA config
    so a subsequent retry resumes from the latest completed step.

    ``authenticator`` and ``creator_factory`` are injection points for
    tests; production callers leave them as ``None`` to use the real
    Device Flow and a real :class:`DialogsSkillCreator`.
    """
    # Precondition failures surface unmodified (caller decides message).
    check_preconditions(
        connection_type=connection_type,
        mass=mass,
        cloud_instance_id=cloud_instance_id,
        direct_client_secret=direct_client_secret,
    )

    auth_fn = authenticator or _default_authenticator
    creator_fn = creator_factory or DialogsSkillCreator

    try:
        async with _build_authenticator_cm(
            auth_fn, mass=mass, session_id=session_id, timeout=timeout
        ) as session:
            creator = creator_fn(session)
            return await _run_pipeline_with_recovery(
                creator=creator,
                artifacts=artifacts,
                connection_type=connection_type,
                skill_name=skill_name,
                cloud_instance_id=cloud_instance_id,
                direct_client_secret=direct_client_secret,
                logo_bytes=logo_bytes,
                mass=mass,
                developer_name=developer_name,
                progress_cb=progress_cb,
            )
    except asyncio.CancelledError:
        # Preserve cooperative cancellation — do not absorb into FAILED.
        raise
    except ValueError:
        raise
    except Exception as exc:
        _LOGGER.exception("auto-create hit unexpected error")
        return dataclasses.replace(artifacts, state=SkillCreationState.FAILED, last_error=repr(exc))


async def _run_pipeline_with_recovery(
    *,
    creator: DialogsSkillCreator,
    artifacts: SkillCreationArtifacts,
    connection_type: str,
    skill_name: str,
    cloud_instance_id: str,
    direct_client_secret: str,
    logo_bytes: bytes,
    mass: MusicAssistant,
    developer_name: str,
    progress_cb: Callable[[SkillCreationArtifacts], Awaitable[None]] | None,
) -> SkillCreationArtifacts:
    """Fetch CSRF and run the pipeline, preserving partial state on failure.

    Holds a ``current`` reference that ``_execute_pipeline`` updates via
    ``progress_cb``, so a mid-pipeline raise lets us surface whatever
    progress was captured (skill_id / logo_id / oauth_app_id) as a
    FAILED artifact instead of losing it.
    """
    current = artifacts

    async def _track(a: SkillCreationArtifacts) -> None:
        nonlocal current
        current = a
        if progress_cb is not None:
            await progress_cb(a)

    try:
        _LOGGER.info("auto-skill: fetching CSRF from dialogs.yandex.ru")
        csrf = await creator.fetch_csrf()
        _LOGGER.info("auto-skill: CSRF acquired, starting skill pipeline")
        return await _execute_pipeline(
            creator=creator,
            csrf=csrf,
            artifacts=artifacts,
            connection_type=connection_type,
            skill_name=skill_name,
            cloud_instance_id=cloud_instance_id,
            direct_client_secret=direct_client_secret,
            logo_bytes=logo_bytes,
            mass=mass,
            developer_name=developer_name,
            progress_cb=_track,
        )
    except DialogsApiError as exc:
        _LOGGER.warning("auto-create failed at %s: %s", exc.step, exc, exc_info=True)
        return dataclasses.replace(current, state=SkillCreationState.FAILED, last_error=str(exc))


async def _execute_pipeline(  # noqa: PLR0913, PLR0915
    *,
    creator: DialogsSkillCreator,
    csrf: str,
    artifacts: SkillCreationArtifacts,
    connection_type: str,
    skill_name: str,
    cloud_instance_id: str,
    direct_client_secret: str,
    logo_bytes: bytes,
    mass: MusicAssistant,
    developer_name: str,
    progress_cb: Callable[[SkillCreationArtifacts], Awaitable[None]] | None,
) -> SkillCreationArtifacts:
    """Advance through states sequentially, skipping completed steps."""
    state = artifacts.state

    # -- Step 3: create app --
    if state in (SkillCreationState.NONE, SkillCreationState.FAILED):
        _LOGGER.info("auto-skill: [1/5] creating skill app")
        new_skill_id = await creator.create_app(csrf, skill_name)
        artifacts = dataclasses.replace(
            artifacts,
            state=SkillCreationState.APP_CREATED,
            skill_id=new_skill_id,
            last_error=None,
        )
        await _maybe_save(progress_cb, artifacts)
        state = artifacts.state

    if artifacts.skill_id is None:
        msg = "internal error: skill_id missing after create_app"
        raise RuntimeError(msg)
    skill_id: str = artifacts.skill_id

    # -- Step 4+5: upload logo and update draft (merged step) --
    if state == SkillCreationState.APP_CREATED:
        logo_id = artifacts.logo_id
        if logo_id is None:
            _LOGGER.info("auto-skill: [2/5] uploading logo")
            logo_id = await creator.upload_logo(csrf, skill_id, logo_bytes)
            artifacts = dataclasses.replace(artifacts, logo_id=logo_id)

        backend_uri = derive_backend_uri(mass, connection_type)
        draft = build_draft_payload(
            connection_type=connection_type,
            skill_name=skill_name,
            backend_uri=backend_uri,
            logo_id=logo_id,
            developer_name=developer_name,
        )
        _LOGGER.info("auto-skill: [3/5] updating draft with settings")
        await creator.update_draft(csrf, skill_id, draft)
        artifacts = dataclasses.replace(artifacts, state=SkillCreationState.DRAFT_UPDATED)
        await _maybe_save(progress_cb, artifacts)
        state = artifacts.state

    # -- Step 6: create OAuth app --
    if state == SkillCreationState.DRAFT_UPDATED:
        client_id = derive_client_id(connection_type, cloud_instance_id)
        client_secret = (
            CLOUD_SKILL_CLIENT_SECRET
            if connection_type == CONNECTION_TYPE_CLOUD_PLUS
            else direct_client_secret
        )
        authorize_url, token_url = derive_auth_urls(mass, connection_type)
        _LOGGER.info("auto-skill: [4/5] creating OAuth app + attaching")
        oauth_app_id = await creator.create_oauth_app(
            csrf,
            name=skill_name,
            client_id=client_id,
            client_secret=client_secret,
            authorize_url=authorize_url,
            token_url=token_url,
            refresh_url=token_url,
        )
        artifacts = dataclasses.replace(
            artifacts,
            state=SkillCreationState.OAUTH_CREATED,
            oauth_app_id=oauth_app_id,
        )
        await _maybe_save(progress_cb, artifacts)
        state = artifacts.state

    if artifacts.oauth_app_id is None:
        msg = "internal error: oauth_app_id missing after create_oauth_app"
        raise RuntimeError(msg)
    oauth_app_id_str: str = artifacts.oauth_app_id

    # -- Step 7: attach OAuth app to skill --
    if state == SkillCreationState.OAUTH_CREATED:
        await creator.attach_oauth(csrf, skill_id, oauth_app_id_str)
        artifacts = dataclasses.replace(artifacts, state=SkillCreationState.OAUTH_ATTACHED)
        await _maybe_save(progress_cb, artifacts)
        state = artifacts.state

    # -- Step 8: publish --
    # Checkpoint state=DEPLOY_REQUESTED *before* calling request_deploy
    # so a crash after the call reached Yandex but before we returned
    # can skip straight to DONE on retry (Yandex accepts the idempotent
    # re-deploy but we'd rather not re-drive the flow end-to-end).
    if state == SkillCreationState.OAUTH_ATTACHED:
        artifacts = dataclasses.replace(artifacts, state=SkillCreationState.DEPLOY_REQUESTED)
        await _maybe_save(progress_cb, artifacts)
        state = artifacts.state

    if state == SkillCreationState.DEPLOY_REQUESTED:
        _LOGGER.info("auto-skill: [5/5] publishing skill")
        await creator.request_deploy(csrf, skill_id)
        artifacts = dataclasses.replace(artifacts, state=SkillCreationState.DONE)
        await _maybe_save(progress_cb, artifacts)

    return artifacts


# Minimal 1x1 transparent PNG — used when the packaged logo is missing
# (e.g. during unit tests before the asset commit lands). Real installs
# pick up provider/auto_skill_logo.png instead.
_FALLBACK_LOGO_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6300010000050001d0a0c9a30000000049454e44ae426082"
)


def load_default_logo_bytes() -> bytes:
    """Return PNG bytes for the skill logo.

    Reads ``provider/auto_skill_logo.png`` if it exists; otherwise
    returns a 1x1 transparent PNG so tests can run without the asset.
    """
    path = Path(__file__).parent / "auto_skill_logo.png"
    if path.is_file():
        return path.read_bytes()
    return _FALLBACK_LOGO_PNG


async def _maybe_save(
    progress_cb: Callable[[SkillCreationArtifacts], Awaitable[None]] | None,
    artifacts: SkillCreationArtifacts,
) -> None:
    """Call ``progress_cb`` if provided, swallowing any save errors."""
    if progress_cb is None:
        return
    try:
        await progress_cb(artifacts)
    except Exception:
        _LOGGER.exception("progress_cb raised; continuing pipeline anyway")
