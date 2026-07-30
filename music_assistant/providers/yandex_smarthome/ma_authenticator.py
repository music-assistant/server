"""
Music-Assistant cached-token authenticator for ya-dialogs-api.

Adapter that produces an authorized ``aiohttp.ClientSession`` behind the
:data:`ya_dialogs_api.AuthenticatorCM` Protocol — a no-arg
async-context-manager factory.

Cache fast-path:
- If a valid cached ``x_token`` is provided, we mint fresh Passport session
  cookies from it via ``refresh_passport_cookies``. If no valid cached token
  is available (or the cached token is rejected), we raise ``LoginFailed``
  with re-authentication guidance.

The dialogs API session keeps its own :class:`ya_passport_auth.PassportClient`
(with ``dialogs.yandex.ru`` in the allowed hosts) — the login flow only
supplies the credentials; the cookies are then minted into this client.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import aiohttp
    from ya_dialogs_api import AuthenticatorCM


_LOGGER = logging.getLogger(__name__)


def make_authenticator(
    *,
    cached_x_token: str | None = None,
) -> AuthenticatorCM:
    """
    Build an :data:`AuthenticatorCM` for ``ya_dialogs_api.auto_create_skill``.

    The returned no-arg callable produces an ``aiohttp.ClientSession``
    context manager. On ``__aenter__`` it reuses ``cached_x_token`` via the
    ``refresh_passport_cookies`` fast-path; if no valid cached token is
    available it raises ``LoginFailed`` with re-authentication guidance.

    :param cached_x_token: Optional Yandex Passport ``x_token`` from a prior
        login. If still valid, Passport session cookies are minted directly
        from it.
    """

    @asynccontextmanager
    async def _cm() -> AsyncIterator[aiohttp.ClientSession]:
        # Imports kept inline so the module can be imported without MA in test envs.
        from ya_passport_auth import ClientConfig, PassportClient  # noqa: PLC0415
        from ya_passport_auth.config import DEFAULT_ALLOWED_HOSTS  # noqa: PLC0415
        from ya_passport_auth.credentials import SecretStr as PpSecretStr  # noqa: PLC0415

        allowed = DEFAULT_ALLOWED_HOSTS | frozenset({"dialogs.yandex.ru"})
        config = ClientConfig(allowed_hosts=allowed)

        async with PassportClient.create(config=config) as client:
            # Cache fast-path: try the cached x_token. If it is still valid
            # Yandex returns fresh session cookies.
            if cached_x_token:
                try:
                    await client.refresh_passport_cookies(PpSecretStr(cached_x_token))
                    _LOGGER.info("auto-skill: reused cached Yandex Passport x_token")
                    yield client._session
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    from music_assistant_models.errors import LoginFailed  # noqa: PLC0415

                    raise LoginFailed(
                        "The linked Yandex Music account's session token was "
                        "rejected by Yandex. Re-authenticate the Yandex Music "
                        "provider and retry."
                    ) from exc

            from music_assistant_models.errors import LoginFailed  # noqa: PLC0415

            raise LoginFailed(
                "The linked Yandex Music account has no session token. "
                "Authenticate the Yandex Music provider (with Remember "
                "session enabled) and retry."
            )

    return _cm
