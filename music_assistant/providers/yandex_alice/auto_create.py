"""
Dialog-skill creation orchestrator — blocking single-click pipeline.

Each click on *Create skill* runs ``auto_create_skill`` end-to-end
against the Yandex Dialogs developer console using the cached
``x_token`` already captured by the Step 1 Device Flow (see
``provider.auth_page.perform_device_auth``). The pipeline is
``create_app → upload_logo → update_draft → request_deploy`` — a
handful of fast HTTP calls to ``dialogs.yandex.ru``; the whole click
typically completes in 5-15 seconds.

Before the create_app step we run a duplicate-name pre-check — if a
skill with the same name already exists in the user's account we
short-circuit into a UX state that lets the user pick *Recreate* or
*Adopt*.

Two helpers expose the resolution paths:

- :func:`adopt_existing_skill` — pre-position artifacts to
  ``APP_CREATED`` with the discovered ``skill_id`` and run the rest
  of the pipeline (re-deploys against our backend URL).
- :func:`delete_existing_skill_then_recreate` — call ``delete_skill``
  then run a fresh pipeline.

This module is **stateless w.r.t. the form**: it does not persist
anything between clicks. The previous self-resuming Device Flow
machinery was removed because MA's frontend does not round-trip
hidden ``ConfigEntry``s between successive ACTION clicks in
*add-provider* mode, so a multi-click flow is impossible there.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from ya_dialogs_api import (
    DialogsSkillCreator,
    EntityDraft,
    IntentDraft,
    SkillCreationArtifacts,
    SkillCreationState,
    auto_create_skill,
)
from ya_dialogs_api.errors import DialogsValidationError
from ya_passport_auth.exceptions import InvalidCredentialsError

from .auth_session import cached_authenticated_session, make_cached_authenticator
from .constants import DIALOG_CHANNEL
from .skill_logo import load_skill_logo_bytes

if TYPE_CHECKING:
    import aiohttp

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "AutoCreateOutcome",
    "LocalAutoCreateStage",
    "adopt_existing_skill",
    "delete_existing_skill_then_recreate",
    "run_create_skill",
]


class LocalAutoCreateStage(StrEnum):
    """
    High-level UX stage derived from artifacts + cached token.

    Drives the section routing in :mod:`provider.auto_create_view`
    and the button label / cancel visibility in Step 2.

    Not persisted directly: rendered on every click from
    ``(SkillCreationArtifacts, cached_x_token_present,
    duplicate_pending)``.
    """

    IDLE = "idle"
    PIPELINE_RUNNING = "pipeline_running"
    DUPLICATE_DETECTED = "duplicate_detected"
    DONE = "done"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class AutoCreateOutcome:
    """
    Result of one click on the Create skill / Recreate / Adopt button.

    The dispatcher in :mod:`provider.__init__` writes ``artifacts``
    and (when set) ``x_token`` into the form ``values`` so MA persists
    them on the next save. ``user_message`` feeds the LABEL the user
    sees while the flow is in progress.
    """

    artifacts: SkillCreationArtifacts
    """Latest snapshot — overwrites CONF_DIALOG_AUTO_CREATE_ARTIFACTS."""

    x_token: str | None
    """Newly minted x_token, ``""`` to clear cache, ``None`` to leave existing."""

    user_message: str
    """Human-readable status for the LABEL entry."""

    stage: LocalAutoCreateStage
    """High-level UX stage — drives button label + section routing."""

    pending_duplicate_skill_id: str | None = None
    """skill_id of a pre-existing same-name skill found by the
    duplicate pre-check. Non-None signals
    ``stage=DUPLICATE_DETECTED``; the dispatcher persists this in a
    hidden config entry so the next render shows the Recreate / Adopt
    resolution UI."""

    pending_duplicate_skill_name: str | None = None
    """Display name of the duplicate skill (as registered in Yandex)."""


# ---------------------------------------------------------------------------
# Duplicate pre-check
# ---------------------------------------------------------------------------


async def _pre_check_duplicate(
    cached_x_token: str,
    skill_name: str,
) -> tuple[str, str] | None:
    """
    Probe Yandex for an existing skill with the same name (case-insensitive).

    Returns ``(skill_id, registered_name)`` or ``None`` if no match.
    Any exception is logged and treated as "no match" — the duplicate
    check is best-effort guidance, not a hard gate; if Yandex is
    flaky we let ``auto_create_skill`` proceed and surface its own
    ``DialogsDuplicateSkillError`` from there.
    """
    target = skill_name.strip().casefold()
    if not target:
        return None
    try:
        async with cached_authenticated_session(cached_x_token) as session:
            creator = DialogsSkillCreator(session, channel=DIALOG_CHANNEL)
            csrf = await creator.fetch_csrf()
            skills = await creator.list_existing_skills(csrf)
    except Exception as exc:
        _LOGGER.debug("auto-create: duplicate pre-check failed: %r", exc)
        return None
    for entry in skills:
        candidate = str(entry.get("appName") or entry.get("name") or "").strip()
        if not candidate:
            continue
        if candidate.casefold() == target:
            sid = str(entry.get("id") or entry.get("skill_id") or "").strip()
            if sid:
                return (sid, candidate)
    return None


# ---------------------------------------------------------------------------
# Pipeline runner
# ---------------------------------------------------------------------------


def _make_logging_creator_factory() -> Callable[[aiohttp.ClientSession], DialogsSkillCreator]:
    """
    Build a creator factory that logs update_draft payload + errors.

    Wrapping is applied to the *instance* method post-construction so
    we don't need to subclass ``DialogsSkillCreator`` (which has
    ``__slots__`` and is monkeypatched in tests). Returns a no-op
    factory if patching fails for any reason — production debugging
    should never break the create flow.
    """

    def _factory(session: aiohttp.ClientSession) -> DialogsSkillCreator:
        creator = DialogsSkillCreator(session, channel=DIALOG_CHANNEL)
        original_update_draft = creator.update_draft

        async def _logged_update_draft(
            csrf: str, skill_id: str, payload: Mapping[str, Any]
        ) -> None:
            _LOGGER.debug(
                "update_draft payload: %s",
                json.dumps(payload, ensure_ascii=False)[:4000],
            )
            try:
                await original_update_draft(csrf, skill_id, payload)
            except DialogsValidationError as exc:
                _LOGGER.warning(
                    "update_draft REJECTED by Yandex: status=%s yandex_error=%r fields=%r",
                    getattr(exc, "http_status", None),
                    getattr(exc, "yandex_error", None),
                    getattr(exc, "fields", None),
                )
                raise

        with contextlib.suppress(AttributeError, TypeError):
            creator.update_draft = _logged_update_draft  # type: ignore[method-assign]
        return creator

    return _factory


async def _run_pipeline(
    *,
    cached_x_token: str,
    skill_name: str,
    backend_uri: str,
    description: str,
    structured_examples: list[dict[str, Any]] | None,
    activation_phrases: list[str] | None,
    intents: list[IntentDraft],
    entities: list[EntityDraft],
    artifacts: SkillCreationArtifacts,
    skip_duplicate_check: bool = False,
) -> AutoCreateOutcome:
    """
    Run the OAuth-free aliceSkill pipeline end-to-end on cached cookies.

    Error handling has two layers:

    1. ``ya_dialogs_api.auto_create_skill`` itself catches every
       :class:`DialogsApiError` subclass internally and returns
       artifacts with ``state=FAILED`` and a populated ``last_error``.
    2. :class:`InvalidCredentialsError` from
       ``ya_passport_auth.refresh_passport_cookies`` is the one
       exception that escapes the library because cookie refresh
       happens inside the authenticator context manager. We translate
       it into ``outcome.x_token=""`` so the dispatcher clears the
       cache and the next click triggers a fresh sign-in.

    Before the create_app step (i.e. when ``artifacts.state == NONE``)
    we run a duplicate-name pre-check and short-circuit into
    ``DUPLICATE_DETECTED`` if a match is found. Bypass with
    ``skip_duplicate_check=True`` (Recreate / Adopt resumption paths).
    """
    if not skip_duplicate_check and artifacts.state == SkillCreationState.NONE:
        duplicate = await _pre_check_duplicate(cached_x_token, skill_name)
        if duplicate is not None:
            existing_id, existing_name = duplicate
            return AutoCreateOutcome(
                artifacts=artifacts,
                x_token=None,
                user_message=(
                    f"A skill named «{existing_name}» already exists in your "
                    "Yandex Dialogs account. Choose Recreate or Adopt below."
                ),
                stage=LocalAutoCreateStage.DUPLICATE_DETECTED,
                pending_duplicate_skill_id=existing_id,
                pending_duplicate_skill_name=existing_name,
            )

    authenticator = make_cached_authenticator(cached_x_token)

    try:
        result = await auto_create_skill(
            authenticator=authenticator,
            skill_name=skill_name,
            artifacts=artifacts,
            backend_uri=backend_uri,
            channel=DIALOG_CHANNEL,
            description=description,
            structured_examples=structured_examples,
            activation_phrases=activation_phrases,
            intents=intents,
            entities=entities,
            logo_bytes=load_skill_logo_bytes(),
            creator_factory=_make_logging_creator_factory(),
        )
    except InvalidCredentialsError as exc:
        _LOGGER.warning("auto-create: cached x_token rejected by Passport: %s", exc)
        failed = dataclasses.replace(
            artifacts,
            state=SkillCreationState.FAILED,
            last_error="Cached auth has expired. Click 'Sign in' again to re-authenticate.",
        )
        return AutoCreateOutcome(
            artifacts=failed,
            x_token="",
            user_message=str(failed.last_error),
            stage=LocalAutoCreateStage.FAILED,
        )

    if result.state == SkillCreationState.DONE:
        skill_url = (
            f"https://dialogs.yandex.ru/developer/skills/{result.skill_id}"
            if result.skill_id
            else "https://dialogs.yandex.ru/developer"
        )
        message = (
            f"Skill created (skill_id={result.skill_id}). "
            f"Yandex moderation queue: 5-15 minutes. "
            f"Check on-air status: {skill_url}"
        )
        return AutoCreateOutcome(
            artifacts=result,
            x_token=None,
            user_message=message,
            stage=LocalAutoCreateStage.DONE,
        )

    return _outcome_from_failed_pipeline(result)


def _outcome_from_failed_pipeline(result: SkillCreationArtifacts) -> AutoCreateOutcome:
    """Translate a FAILED ``SkillCreationArtifacts`` into a UX outcome."""
    msg = result.last_error or "Pipeline failed without a description."
    return AutoCreateOutcome(
        artifacts=result,
        x_token=None,
        user_message=msg,
        stage=LocalAutoCreateStage.FAILED,
    )


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


async def run_create_skill(
    *,
    cached_x_token: str,
    skill_name: str,
    backend_uri: str,
    description: str,
    structured_examples: list[dict[str, Any]] | None,
    activation_phrases: list[str] | None,
    intents: list[IntentDraft],
    entities: list[EntityDraft],
    artifacts: SkillCreationArtifacts,
) -> AutoCreateOutcome:
    """
    Drive the create_skill click — duplicate pre-check + full pipeline.

    Pre-conditions:

    - ``cached_x_token`` is non-empty (caller must have run
      :func:`provider.auth_page.perform_device_auth` first).
    - ``backend_uri`` is fully assembled (HTTPS public URL +
      webhook secret).
    - ``intents`` / ``entities`` come from
      :class:`SkillManifestProvider` (effective manifest).

    Backup-restore safety: if ``artifacts.state == NONE`` but the
    caller has a saved ``skill_id`` in form values, the dispatcher
    pre-positions ``artifacts.state = APP_CREATED`` *before* invoking
    this function so the library skips create_app and patches the
    existing skill rather than creating a duplicate.
    """
    return await _run_pipeline(
        cached_x_token=cached_x_token,
        skill_name=skill_name,
        backend_uri=backend_uri,
        description=description,
        structured_examples=structured_examples,
        activation_phrases=activation_phrases,
        intents=intents,
        entities=entities,
        artifacts=artifacts,
    )


async def adopt_existing_skill(
    *,
    cached_x_token: str,
    skill_name: str,
    backend_uri: str,
    description: str,
    structured_examples: list[dict[str, Any]] | None,
    activation_phrases: list[str] | None,
    intents: list[IntentDraft],
    entities: list[EntityDraft],
    existing_skill_id: str,
) -> AutoCreateOutcome:
    """
    Re-deploy an existing skill against this MA's webhook URL.

    Pre-positions ``artifacts`` to ``APP_CREATED`` (with the
    discovered ``skill_id``) so ``auto_create_skill`` skips
    create_app and runs ``upload_logo → update_draft →
    request_deploy`` against the existing skill instead. The library
    is idempotent on each sub-step, so adopting an already-on-air
    skill is safe.
    """
    artifacts = SkillCreationArtifacts(
        state=SkillCreationState.APP_CREATED,
        skill_id=existing_skill_id,
    )
    return await _run_pipeline(
        cached_x_token=cached_x_token,
        skill_name=skill_name,
        backend_uri=backend_uri,
        description=description,
        structured_examples=structured_examples,
        activation_phrases=activation_phrases,
        intents=intents,
        entities=entities,
        artifacts=artifacts,
        skip_duplicate_check=True,
    )


async def delete_existing_skill_then_recreate(
    *,
    cached_x_token: str,
    skill_name: str,
    backend_uri: str,
    description: str,
    structured_examples: list[dict[str, Any]] | None,
    activation_phrases: list[str] | None,
    intents: list[IntentDraft],
    entities: list[EntityDraft],
    existing_skill_id: str,
) -> AutoCreateOutcome:
    """Delete the duplicate skill in Yandex, then run a fresh pipeline."""
    try:
        async with cached_authenticated_session(cached_x_token) as session:
            creator = DialogsSkillCreator(session, channel=DIALOG_CHANNEL)
            csrf = await creator.fetch_csrf()
            await creator.delete_skill(csrf, existing_skill_id)
    except Exception as exc:
        _LOGGER.exception("auto-create: delete_skill failed (skill_id=%s)", existing_skill_id)
        failed = SkillCreationArtifacts(
            state=SkillCreationState.FAILED,
            last_error=f"Failed to delete the existing skill: {exc!r}",
        )
        return AutoCreateOutcome(
            artifacts=failed,
            x_token=None,
            user_message=str(failed.last_error),
            stage=LocalAutoCreateStage.FAILED,
        )

    return await _run_pipeline(
        cached_x_token=cached_x_token,
        skill_name=skill_name,
        backend_uri=backend_uri,
        description=description,
        structured_examples=structured_examples,
        activation_phrases=activation_phrases,
        intents=intents,
        entities=entities,
        artifacts=SkillCreationArtifacts(),
        skip_duplicate_check=True,
    )
