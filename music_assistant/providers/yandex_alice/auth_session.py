"""
Yandex Passport session helpers for the auto-create / auto-update flows.

Two pieces of plumbing:

- :func:`passport_client_session` — single context-manager factory for
  :class:`PassportClient`, so tests can monkeypatch one entry point.
- :func:`make_cached_authenticator` — produces a no-arg async-context-manager
  factory (the ``AuthenticatorCM`` shape ``ya-dialogs-api`` expects) that
  populates Passport cookies from a cached ``x_token`` and refuses any
  Device Flow fallback. Used by both pipeline-step calls in auto-create
  (post-auth) and every auto-update call.

Cache-only-by-design: we never fall back to interactive Device Flow from
inside the authenticator. The caller (auto_create.py) decides separately
whether to run a Device Flow click; mixing the two would let a stale token
silently re-trigger user-code prompts mid-pipeline.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import aiohttp
from ya_passport_auth import PassportClient, SecretStr

AuthenticatorCM = Callable[[], AbstractAsyncContextManager[aiohttp.ClientSession]]


@asynccontextmanager
async def passport_client_session() -> AsyncIterator[PassportClient]:
    """
    Yield a :class:`PassportClient` that owns its session and cleans it up.

    Single entry point so tests can monkeypatch
    ``provider.auth_session.passport_client_session`` instead of patching
    ``ya_passport_auth.PassportClient.create`` directly.
    """
    async with PassportClient.create() as client:
        yield client


@asynccontextmanager
async def cached_authenticated_session(x_token: str) -> AsyncIterator[aiohttp.ClientSession]:
    """
    Yield an aiohttp session pre-populated with Yandex Passport cookies.

    Owns the session — closes it on exit. Any
    :class:`ya_passport_auth.InvalidCredentialsError` from
    ``refresh_passport_cookies`` propagates so the caller can clear the
    cached token and start a fresh Device Flow on the next click.

    :raises ValueError: ``x_token`` is empty.
    """
    if not x_token:
        msg = "x_token is empty — cached authenticator requires an existing token"
        raise ValueError(msg)

    secret = SecretStr(x_token)
    jar = aiohttp.CookieJar()
    async with aiohttp.ClientSession(cookie_jar=jar) as session:
        client = PassportClient(session=session)
        await client.refresh_passport_cookies(secret)
        yield session


def make_cached_authenticator(x_token: str) -> AuthenticatorCM:
    """
    Return an ``AuthenticatorCM`` matching the ya-dialogs-api contract.

    Each invocation of the returned factory opens a fresh
    :func:`cached_authenticated_session` — short-lived, scoped to a single
    pipeline run. ``ya-dialogs-api`` keeps the session open only for the
    duration of one ``auto_create_skill`` / ``auto_update_skill`` call.
    """
    if not x_token:
        msg = "x_token is empty"
        raise ValueError(msg)

    def _factory() -> AbstractAsyncContextManager[aiohttp.ClientSession]:
        return cached_authenticated_session(x_token)

    return _factory
