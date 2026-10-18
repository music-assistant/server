"""Durable links between OIDC sessions and Music Assistant login tokens."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import secrets
import time
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from music_assistant_models.errors import AuthenticationFailed

from music_assistant.helpers.json import json_dumps

from .oidc import CLOCK_LEEWAY

if TYPE_CHECKING:
    from music_assistant_models.auth import User

    from music_assistant.controllers.webserver.auth import AuthenticationManager


@dataclass(frozen=True)
class OIDCSession:
    """Verified identity-provider session for one sign-in."""

    issuer: str
    client_id: str
    subject: str
    session_id: str | None
    issued_at: float
    expires_at: float


class OIDCSessionStore:
    """Persist OIDC sessions and serialize token creation with logout."""

    def __init__(self, auth: AuthenticationManager) -> None:
        """Initialize a store using the authentication manager's database."""
        self.auth = auth
        self._lock = asyncio.Lock()

    async def setup(self) -> None:
        """Create session tables while preserving existing accounts and tokens."""
        await self.auth.database.execute_write(
            """
            CREATE TABLE IF NOT EXISTS oidc_sessions (
                token_id TEXT PRIMARY KEY,
                issuer TEXT NOT NULL,
                client_id TEXT NOT NULL,
                subject TEXT NOT NULL,
                session_id TEXT,
                issued_at REAL NOT NULL,
                expires_at REAL NOT NULL
            )
            """
        )
        await self.auth.database.execute_write(
            """
            CREATE TABLE IF NOT EXISTS oidc_logouts (
                logout_id TEXT PRIMARY KEY,
                issuer TEXT NOT NULL,
                client_id TEXT NOT NULL,
                subject TEXT,
                session_id TEXT,
                issued_at REAL NOT NULL,
                expires_at REAL NOT NULL
            )
            """
        )
        await self.auth.database.execute_write(
            "CREATE INDEX IF NOT EXISTS idx_oidc_sessions_identity "
            "ON oidc_sessions(issuer, client_id, subject, session_id)"
        )
        await self._cleanup()

    async def create_token(self, user: User, name: str, session: OIDCSession) -> str:
        """Issue a token unless its identity-provider session has already ended."""
        async with self._lock:
            await self._cleanup()
            await self._ensure_session_active(session)
            token = await self.auth.create_token(user, name)
            token_id = self.auth.jwt_helper.decode_token(token)["jti"]
            try:
                await self.auth.database.insert(
                    "oidc_sessions", {"token_id": token_id, **asdict(session)}
                )
            except BaseException:
                # A token without a saved session link cannot be revoked by IdP logout.
                await self.auth.database.delete("auth_tokens", {"token_id": token_id})
                raise
            return token

    async def validate_session(self, session: OIDCSession) -> None:
        """Reject an ended session before applying its identity or role claims."""
        async with self._lock:
            await self._cleanup()
            await self._ensure_session_active(session)

    async def logout(self, issuer: str, client_id: str, claims: dict[str, Any]) -> int:
        """Apply a verified logout, retrying interrupted revocations."""
        logout_id = hashlib.sha256(
            json_dumps([issuer, client_id, claims["jti"]]).encode()
        ).hexdigest()
        async with self._lock:
            await self._cleanup()
            values = {
                "issuer": issuer,
                "client_id": client_id,
                "subject": claims.get("sub"),
                "session_id": claims.get("sid"),
                "issued_at": claims["iat"],
            }
            # Persist first to block concurrent callbacks; retain through the pending-login TTL.
            existing = await self.auth.database.get_row("oidc_logouts", {"logout_id": logout_id})
            if existing:
                values = {key: existing[key] for key in values}
            else:
                await self.auth.database.insert(
                    "oidc_logouts",
                    {"logout_id": logout_id, "expires_at": time.time() + 630, **values},
                )
            rows = await self.auth.database.get_rows_from_query(
                "SELECT token_id FROM oidc_sessions WHERE issuer = :issuer "
                "AND client_id = :client_id "
                "AND (:session_id IS NOT NULL OR issued_at <= :issued_at) "
                "AND (:subject IS NULL OR subject = :subject) "
                "AND (:session_id IS NULL OR session_id = :session_id)",
                values,
                limit=0,
            )
            if rows:
                # A cancelled commit must not leave live WebSockets after their tokens disappear.
                revoke_task = asyncio.create_task(
                    self._revoke_tokens([row["token_id"] for row in rows])
                )
                try:
                    await asyncio.shield(revoke_task)
                except asyncio.CancelledError:
                    while not revoke_task.done():
                        with contextlib.suppress(asyncio.CancelledError):
                            await asyncio.shield(revoke_task)
                    revoke_task.result()
                    raise
            await self._cleanup()
            return len(rows)

    async def revoke_identity(self, issuer: str, client_id: str, subject: str) -> None:
        """Revoke this identity's sessions when group mapping denies access."""
        await self.logout(
            issuer,
            client_id,
            # Include accepted ID tokens from a provider whose clock is ahead.
            {
                "sub": subject,
                "iat": time.time() + CLOCK_LEEWAY,
                "jti": secrets.token_urlsafe(32),
            },
        )

    async def _revoke_tokens(self, token_ids: list[str]) -> None:
        await self.auth.database.execute_write(
            "DELETE FROM auth_tokens WHERE token_id IN (SELECT value FROM json_each(:token_ids))",
            {"token_ids": json_dumps(token_ids)},
        )
        for token_id in token_ids:
            self.auth.webserver.disconnect_websockets_for_token(token_id)

    async def _ensure_session_active(self, session: OIDCSession) -> None:
        if session.expires_at <= time.time():
            raise AuthenticationFailed("The identity token has expired; sign in again")
        logout = await self.auth.database.get_rows_from_query(
            "SELECT logout_id FROM oidc_logouts WHERE issuer = :issuer "
            "AND client_id = :client_id "
            "AND (session_id IS NOT NULL OR issued_at >= :issued_at) "
            "AND (subject IS NULL OR subject = :subject) "
            "AND (session_id IS NULL OR session_id = :session_id)",
            asdict(session),
            limit=1,
        )
        if logout:
            raise AuthenticationFailed("The identity provider session has ended; sign in again")

    async def _cleanup(self) -> None:
        await self.auth.database.execute_write(
            "DELETE FROM oidc_sessions WHERE token_id NOT IN (SELECT token_id FROM auth_tokens)"
        )
        await self.auth.database.execute_write(
            "DELETE FROM oidc_logouts WHERE expires_at < :now", {"now": time.time()}
        )
