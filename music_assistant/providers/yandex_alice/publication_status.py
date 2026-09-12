"""
Yandex Dialogs skill publication-status helper.

A single-shot lookup against ``/developer/app-store-api/snapshot`` that
classifies the user's skill into one of five stable status strings used
by the Step 3 banner:

* ``on_air`` — skill is live (Alice can answer voice commands).
* ``in_moderation`` — Yandex moderation queue, 5-15 min typical.
* ``draft`` — never deployed; waiting for the user to request publishing.
* ``rejected`` — Yandex moderation rejected the latest deploy.
* ``unknown`` — snapshot returned the skill but its state did not match
  any of the above (used as a graceful fallback rather than crashing).

The fetch is intentionally **out of band** of the auto-create / update
pipelines: callers invoke it once after a DONE outcome (or via the
manual *Refresh status* button) and persist the result so subsequent
form renders read it from values without making any HTTP call.
"""

from __future__ import annotations

import logging
from typing import Any

from ya_dialogs_api import DialogsSkillCreator

from .auth_session import cached_authenticated_session
from .constants import DIALOG_CHANNEL

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "STATUS_DRAFT",
    "STATUS_IN_MODERATION",
    "STATUS_ON_AIR",
    "STATUS_REJECTED",
    "STATUS_UNKNOWN",
    "classify_skill_entry",
    "fetch_skill_publication_status",
]

STATUS_ON_AIR = "on_air"
STATUS_IN_MODERATION = "in_moderation"
STATUS_DRAFT = "draft"
STATUS_REJECTED = "rejected"
STATUS_UNKNOWN = "unknown"


def classify_skill_entry(entry: dict[str, Any]) -> str:
    """
    Classify a single ``snapshot.result.skills[]`` dict.

    Priority order (highest first):

    1. ``draft.status == "rejected"`` → ``rejected``. Wins over on-air
       because the user's most recent deploy was rejected and the
       previously on-air version may already have been pulled.
    2. ``draft.status == "deployRequested"`` → ``in_moderation``. Wins
       over on-air because the user just clicked Update / Recreate and
       wants to see the moderation banner; the previous on-air version
       remains live for end users in the meantime.
    3. ``onAir`` truthy or ``firstPublishedAt`` non-empty → ``on_air``.
    4. ``draft.status == "inDevelopment"`` → ``draft``.
    5. otherwise → ``unknown``.
    """
    draft_obj = entry.get("draft")
    draft_status = ""
    if isinstance(draft_obj, dict):
        draft_status = str(draft_obj.get("status") or "").strip()

    if draft_status == "rejected":
        return STATUS_REJECTED
    if draft_status == "deployRequested":
        return STATUS_IN_MODERATION

    on_air = bool(entry.get("onAir") or entry.get("on_air"))
    first_published = entry.get("firstPublishedAt") or entry.get("first_published_at")
    if on_air or first_published:
        return STATUS_ON_AIR

    if draft_status == "inDevelopment":
        return STATUS_DRAFT
    return STATUS_UNKNOWN


async def fetch_skill_publication_status(
    cached_x_token: str,
    skill_id: str,
) -> str | None:
    """
    Fetch the live publication status for ``skill_id``.

    Returns one of the ``STATUS_*`` constants, or ``None`` if:

    * ``cached_x_token`` / ``skill_id`` is empty (caller hasn't signed
      in yet, or the skill hasn't been created),
    * the snapshot HTTP call fails for any reason (network, auth
      expiry, Yandex 5xx),
    * the skill_id is not present in the user's account (deleted
      out-of-band on Yandex side).

    Never raises — failures are logged at DEBUG and surface as ``None``
    so the caller can leave whatever cached value is already on file
    untouched.
    """
    if not cached_x_token or not skill_id:
        return None

    try:
        async with cached_authenticated_session(cached_x_token) as session:
            creator = DialogsSkillCreator(session, channel=DIALOG_CHANNEL)
            csrf = await creator.fetch_csrf()
            skills = await creator.list_existing_skills(csrf)
    except Exception as exc:
        _LOGGER.debug(
            "publication-status: snapshot fetch failed (skill_id=%s): %r",
            skill_id,
            exc,
        )
        return None

    for entry in skills:
        sid = str(entry.get("id") or entry.get("skill_id") or "").strip()
        if sid == skill_id:
            return classify_skill_entry(entry)

    _LOGGER.debug(
        "publication-status: skill_id=%s not found in snapshot (deleted?)",
        skill_id,
    )
    return None
