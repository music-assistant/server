"""State model for experimental auto-create-skill feature.

Tracks progress of the multi-step skill creation flow against
dialogs.yandex.ru so partial failures can be retried from the
last successful step rather than starting over.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from dataclasses import dataclass
from enum import StrEnum

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "SkillCreationArtifacts",
    "SkillCreationState",
    "dump_artifacts",
    "load_artifacts",
]


class SkillCreationState(StrEnum):
    """Progress marker for the skill-creation pipeline.

    Linear states advance through the 6 HAR-captured API calls.
    ``FAILED`` replaces the stored linear state in the artifact;
    failure details are kept separately in ``last_error``, while
    captured artifact IDs (``skill_id`` / ``logo_id`` / ``oauth_app_id``)
    stay intact so a retry can resume from the partial results.
    """

    NONE = "none"
    APP_CREATED = "app_created"
    DRAFT_UPDATED = "draft_updated"
    OAUTH_CREATED = "oauth_created"
    OAUTH_ATTACHED = "oauth_attached"
    DEPLOY_REQUESTED = "deploy_requested"
    DONE = "done"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SkillCreationArtifacts:
    """Persistent state for a skill-creation attempt.

    Stored as a JSON blob in the ``CONF_AUTO_CREATE_ARTIFACTS``
    config entry and round-tripped through every call to
    ``get_config_entries``.
    """

    state: SkillCreationState = SkillCreationState.NONE
    skill_id: str | None = None
    logo_id: str | None = None
    oauth_app_id: str | None = None
    last_error: str | None = None


def dump_artifacts(artifacts: SkillCreationArtifacts) -> str:
    """Serialise artifacts to a JSON string for config storage."""
    return json.dumps(
        {
            "state": artifacts.state.value,
            "skill_id": artifacts.skill_id,
            "logo_id": artifacts.logo_id,
            "oauth_app_id": artifacts.oauth_app_id,
            "last_error": artifacts.last_error,
        },
        ensure_ascii=False,
    )


def load_artifacts(raw: str | None) -> SkillCreationArtifacts:
    """Deserialise artifacts from a config-stored JSON string.

    Returns a fresh ``SkillCreationArtifacts`` on any parse error
    or missing input — the feature is optional so config stays
    usable even if the blob is corrupted.
    """
    if not raw:
        return SkillCreationArtifacts()
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        _LOGGER.warning("auto-skill artifacts corrupt, resetting")
        return SkillCreationArtifacts()
    if not isinstance(data, dict):
        return SkillCreationArtifacts()

    try:
        state = SkillCreationState(str(data.get("state", SkillCreationState.NONE.value)))
    except ValueError:
        state = SkillCreationState.NONE

    def _opt_str(key: str) -> str | None:
        value = data.get(key)
        if value is None:
            return None
        return str(value) if value else None

    return SkillCreationArtifacts(
        state=state,
        skill_id=_opt_str("skill_id"),
        logo_id=_opt_str("logo_id"),
        oauth_app_id=_opt_str("oauth_app_id"),
        last_error=_opt_str("last_error"),
    )


def mark_failed(artifacts: SkillCreationArtifacts, error: str) -> SkillCreationArtifacts:
    """Return a copy of *artifacts* flipped to ``FAILED`` with an error."""
    return dataclasses.replace(
        artifacts,
        state=SkillCreationState.FAILED,
        last_error=error,
    )
