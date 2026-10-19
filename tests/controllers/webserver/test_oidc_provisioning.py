"""Failure-atomic OIDC provisioning and first-administrator side effects."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from pathlib import Path
from sqlite3 import IntegrityError
from typing import Any
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest
from music_assistant_models.auth import AuthProviderType, UserRole

from music_assistant.controllers.webserver.controller import WebserverController
from music_assistant.controllers.webserver.helpers.auth_providers import get_or_create_ha_user
from music_assistant.controllers.webserver.helpers.oidc_provider import (
    OIDCLoginProvider,
    oidc_identity_id,
)
from music_assistant.helpers.database import DatabaseConnection

from .test_oidc import IdentityProvider, auth_manager, idp, provider
from .test_oidc_bootstrap import bootstrap_server, deployment_file

__all__ = ["auth_manager", "bootstrap_server", "deployment_file", "idp", "provider"]


@pytest.fixture
async def legacy_playlog(
    bootstrap_server: WebserverController, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[DatabaseConnection]:
    """Keep legacy playlog ownership in a real temporary database."""
    database = DatabaseConnection(str(tmp_path / "legacy.db"))
    await database.setup()
    await database.execute_write("CREATE TABLE media_progress (userid TEXT)")
    await database.insert("media_progress", {"userid": None})
    monkeypatch.setattr(bootstrap_server.mass.music, "database", database)
    try:
        yield database
    finally:
        await database.close()


@pytest.mark.parametrize("failure", ["error", "cancel"])
@pytest.mark.parametrize("after_insert", [False, True])
async def test_incomplete_bootstrap_rolls_back_account_and_first_user_effects(
    bootstrap_server: WebserverController,
    legacy_playlog: DatabaseConnection,
    idp: IdentityProvider,
    failure: str,
    after_insert: bool,
) -> None:
    """Failed or cancelled linking cannot publish an orphan administrator or claim playlog."""
    auth = bootstrap_server.auth
    system_user = await auth.get_homeassistant_system_user()
    configured = auth.login_providers["oidc"]
    assert isinstance(configured, OIDCLoginProvider)
    idp.overrides["groups"] = ["music-admins"]

    with _link_boundary(after_insert=after_insert, fail=failure == "error") as (reached, _):
        task = asyncio.create_task(
            configured.complete_authorization(idp.pending, {"code": "valid-code"})
        )
        try:
            await asyncio.wait_for(reached.wait(), timeout=5)
            assert await auth.get_user_by_username("listener", include_disabled=True) is None
            assert not bool(auth.has_users)
            assert (await legacy_playlog.get_rows("media_progress"))[0]["userid"] is None
            if failure == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                with pytest.raises(RuntimeError, match="link failed"):
                    await task
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert not bool(auth.has_users)
    assert bool(auth.oidc_bootstrap_required)
    assert await auth.get_user_by_username("listener", include_disabled=True) is None
    assert not await auth.database.get_rows("user_auth_providers")
    assert await auth.get_user(system_user.user_id) is not None
    assert (await legacy_playlog.get_rows("media_progress"))[0]["userid"] is None
    assert (
        await get_or_create_ha_user(
            bootstrap_server.mass, "unrelated-ha", "listener", None, None, allow_create=False
        )
        is None
    )

    user = await configured.complete_authorization(idp.pending, {"code": "valid-code"})
    assert user.role == UserRole.ADMIN
    assert auth.has_users
    assert not auth.oidc_bootstrap_required
    assert (await legacy_playlog.get_rows("media_progress"))[0]["userid"] == user.user_id


@pytest.mark.parametrize("phase", ["commit", "migration"])
async def test_cancellation_after_commit_handoff_finishes_first_user_effects(
    bootstrap_server: WebserverController,
    legacy_playlog: DatabaseConnection,
    idp: IdentityProvider,
    phase: str,
) -> None:
    """Once commit starts, repeated cancellation waits for the linked account and migration."""
    auth = bootstrap_server.auth
    configured = auth.login_providers["oidc"]
    assert isinstance(configured, OIDCLoginProvider)
    idp.overrides["groups"] = ["music-admins"]
    reached = asyncio.Event()
    finish = asyncio.Event()
    method_name = "_commit_linked_user" if phase == "commit" else "_migrate_playlog_to_first_user"
    original = getattr(auth, method_name)

    async def delayed(*args: Any) -> None:
        reached.set()
        await finish.wait()
        await original(*args)

    with patch.object(auth, method_name, side_effect=delayed):
        task = asyncio.create_task(
            configured.complete_authorization(idp.pending, {"code": "valid-code"})
        )
        try:
            await asyncio.wait_for(reached.wait(), timeout=5)
            task.cancel()
            await asyncio.wait({task}, timeout=0.02)
            assert not task.done()
            task.cancel()
            await asyncio.wait({task}, timeout=0.02)
            assert not task.done()
            finish.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            finish.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    user = await auth.get_user_by_username("listener")
    assert user is not None
    assert user.role == UserRole.ADMIN
    assert auth.has_users
    assert not auth.oidc_bootstrap_required
    links = await auth.database.get_rows("user_auth_providers", {"user_id": user.user_id})
    assert len(links) == 1
    assert links[0]["provider_type"] == AuthProviderType.OIDC
    assert (await legacy_playlog.get_rows("media_progress"))[0]["userid"] == user.user_id
    assert (
        await get_or_create_ha_user(
            bootstrap_server.mass, "unrelated-ha", "listener", None, None, allow_create=False
        )
        is None
    )
    retried = await configured.complete_authorization(idp.pending, {"code": "valid-code"})
    assert retried.user_id == user.user_id


async def test_failed_commit_preserves_bootstrap_and_legacy_playlog(
    bootstrap_server: WebserverController, legacy_playlog: DatabaseConnection, idp: IdentityProvider
) -> None:
    """A failed auth commit rolls back both rows before first-user state changes."""
    auth = bootstrap_server.auth
    configured = auth.login_providers["oidc"]
    assert isinstance(configured, OIDCLoginProvider)
    idp.overrides["groups"] = ["music-admins"]
    with (
        patch.object(
            auth, "_commit_linked_user", new=AsyncMock(side_effect=RuntimeError("commit failed"))
        ),
        pytest.raises(RuntimeError, match="commit failed"),
    ):
        await configured.complete_authorization(idp.pending, {"code": "valid-code"})
    assert not auth.has_users
    assert auth.oidc_bootstrap_required
    assert not await auth.database.get_rows("users")
    assert not await auth.database.get_rows("user_auth_providers")
    assert (await legacy_playlog.get_rows("media_progress"))[0]["userid"] is None


async def test_existing_identity_constraint_cannot_leave_an_unlinked_user(
    provider: OIDCLoginProvider, idp: IdentityProvider
) -> None:
    """A provider identity conflict rolls back the new user without changing its owner."""
    owner = await provider.complete_authorization(idp.pending, {"code": "valid-code"})
    auth = provider.auth_manager
    identity = oidc_identity_id(provider.client.issuer, "subject-1")
    with pytest.raises(IntegrityError):
        await auth.create_user("conflicting-user", provider_link=(AuthProviderType.OIDC, identity))
    assert await auth.get_user_by_username("conflicting-user", include_disabled=True) is None
    linked = await auth.get_user_by_provider_link(AuthProviderType.OIDC, identity)
    assert linked is not None
    assert linked.user_id == owner.user_id


async def test_provisioning_rollback_does_not_undo_another_auth_writer(
    provider: OIDCLoginProvider, idp: IdentityProvider
) -> None:
    """An isolated provisioning transaction cannot roll back an unrelated shared-connection write."""
    auth = provider.auth_manager
    started = asyncio.Event()

    async def unrelated_write() -> None:
        started.set()
        await auth.database.insert("settings", {"key": "unrelated", "value": "kept"})

    with _link_boundary() as (reached, _):
        task = asyncio.create_task(
            provider.complete_authorization(idp.pending, {"code": "valid-code"})
        )
        writer = None
        try:
            await asyncio.wait_for(reached.wait(), timeout=5)
            writer = asyncio.create_task(unrelated_write())
            await started.wait()
            await asyncio.wait({writer}, timeout=0.02)
            assert not writer.done()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.wait_for(writer, timeout=5)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if writer is not None:
                await asyncio.gather(writer, return_exceptions=True)
    assert await auth.get_user_by_username("listener", include_disabled=True) is None
    row = await auth.database.get_row("settings", {"key": "unrelated"})
    assert row is not None
    assert row["value"] == "kept"


@contextmanager
def _link_boundary(
    *, after_insert: bool = False, fail: bool = False
) -> Iterator[tuple[asyncio.Event, asyncio.Event]]:
    """Pause or fail the provider-link write in the isolated transaction."""
    reached = asyncio.Event()
    finish = asyncio.Event()
    execute = aiosqlite.Connection.execute

    async def paused(connection: aiosqlite.Connection, query: str, parameters: Any) -> Any:
        cursor = await execute(connection, query, parameters) if after_insert else None
        reached.set()
        if fail:
            raise RuntimeError("link failed")
        await finish.wait()
        return cursor if after_insert else await execute(connection, query, parameters)

    def intercept(connection: aiosqlite.Connection, query: str, parameters: Any = None) -> Any:
        if query.startswith("INSERT INTO user_auth_providers"):
            return paused(connection, query, parameters)
        return execute(connection, query, parameters)

    with patch.object(aiosqlite.Connection, "execute", new=intercept):
        yield reached, finish
