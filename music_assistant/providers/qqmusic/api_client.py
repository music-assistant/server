"""Authentication/session helpers for QQ Music provider."""

from __future__ import annotations

from http.cookies import SimpleCookie
from typing import TYPE_CHECKING, Final

from aiohttp import ClientError, ClientTimeout
from music_assistant_models.errors import InvalidDataError, LoginFailed

from .constants import QQMUSIC_MUSICU_URL

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

COOKIE_REQUIRED_ANY_KEYS: Final[tuple[str, ...]] = ("uin", "wxuin")
COOKIE_REQUIRED_SESSION_KEYS: Final[tuple[str, ...]] = ("qm_keyst", "qqmusic_key", "p_skey")


def parse_cookie_header(cookie_header: str) -> dict[str, str]:
    """Parse a browser cookie header string into a dict."""
    raw = (cookie_header or "").strip()
    if not raw:
        raise InvalidDataError("Cookie is required")
    parsed: dict[str, str] = {}
    cookie = SimpleCookie()
    cookie.load(raw)
    if cookie:
        for key, morsel in cookie.items():
            parsed[key] = morsel.value
        return parsed
    # fallback for malformed cookie header syntax
    for part in raw.split(";"):
        if "=" not in part:
            continue
        key, val = part.split("=", 1)
        key = key.strip()
        val = val.strip()
        if key and val:
            parsed[key] = val
    if not parsed:
        raise InvalidDataError("Cookie format is invalid")
    return parsed


def extract_uin(cookies: dict[str, str]) -> str:
    """Extract normalized numeric UIN from QQ Music cookies."""
    raw_uin = cookies.get("uin") or cookies.get("wxuin")
    if not raw_uin:
        raise InvalidDataError("Cookie missing 'uin' or 'wxuin'")
    uin = raw_uin.removeprefix("o")
    if not uin.isdigit():
        raise InvalidDataError("Cookie contains invalid uin/wxuin")
    return uin


def validate_cookie_shape(cookies: dict[str, str]) -> None:
    """Validate that cookie includes critical identifiers and session tokens."""
    if not any(cookies.get(key) for key in COOKIE_REQUIRED_ANY_KEYS):
        raise InvalidDataError("Cookie must include uin or wxuin")
    if not any(cookies.get(key) for key in COOKIE_REQUIRED_SESSION_KEYS):
        raise InvalidDataError("Cookie must include qm_keyst, qqmusic_key, or p_skey")


def parse_credential_fields(cookie_header: str) -> tuple[int, str, int]:
    """Parse credential fields for qqmusic_api from cookie header."""
    cookies = parse_cookie_header(cookie_header)
    validate_cookie_shape(cookies)
    uin = int(extract_uin(cookies))
    musickey = cookies.get("qqmusic_key") or cookies.get("qm_keyst")
    if not musickey:
        raise InvalidDataError("Cookie missing qqmusic_key/qm_keyst")
    login_type_raw = cookies.get("tmeLoginType", "2")
    login_type = int(login_type_raw) if login_type_raw.isdigit() else 2
    return (uin, musickey, login_type)


async def verify_session(mass: MusicAssistant, cookie_header: str) -> str:
    """Verify QQ Music session from cookie and return normalized uin."""
    cookies = parse_cookie_header(cookie_header)
    validate_cookie_shape(cookies)
    uin = extract_uin(cookies)
    headers = {
        "referer": "https://y.qq.com/",
        "origin": "https://y.qq.com",
        "user-agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
    }
    payload = {
        "comm": {"ct": 24, "cv": 0},
        "req_1": {
            "module": "userInfo.VipQueryServer",
            "method": "SRFVipQuery_V2",
            "param": {"uin_list": [uin]},
        },
    }
    try:
        async with mass.http_session.post(
            QQMUSIC_MUSICU_URL,
            json=payload,
            headers=headers,
            cookies=cookies,
            timeout=ClientTimeout(total=10),
        ) as response:
            if response.status != 200:
                raise LoginFailed(f"QQ Music validation failed with HTTP {response.status}")
            data = await response.json(content_type=None)
    except ClientError as err:
        raise LoginFailed(f"Unable to validate QQ Music cookie: {err}") from err
    except TimeoutError as err:
        raise LoginFailed("QQ Music validation request timed out") from err

    req_data = data.get("req_1") if isinstance(data, dict) else None
    if not isinstance(req_data, dict):
        raise LoginFailed("QQ Music validation returned unexpected response")
    code = req_data.get("code")
    if code not in (0, "0", None):
        raise LoginFailed(f"QQ Music validation failed (code={code})")

    return uin
