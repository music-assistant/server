"""Tests for auto_skill_state: artifact serialisation and state transitions."""

from __future__ import annotations

import dataclasses

from music_assistant.providers.yandex_smarthome.auto_skill_state import (
    SkillCreationArtifacts,
    SkillCreationState,
    dump_artifacts,
    load_artifacts,
    mark_failed,
)


def test_default_artifacts_state_is_none() -> None:
    """Freshly-constructed artifacts sit at state NONE with no fields set."""
    artifacts = SkillCreationArtifacts()
    assert artifacts.state == SkillCreationState.NONE
    assert artifacts.skill_id is None
    assert artifacts.logo_id is None
    assert artifacts.oauth_app_id is None
    assert artifacts.last_error is None


def test_dump_load_roundtrip_none() -> None:
    """NONE state with no fields round-trips cleanly."""
    original = SkillCreationArtifacts()
    restored = load_artifacts(dump_artifacts(original))
    assert restored == original


def test_dump_load_roundtrip_populated() -> None:
    """Fully-populated artifacts round-trip without loss."""
    original = SkillCreationArtifacts(
        state=SkillCreationState.OAUTH_ATTACHED,
        skill_id="7584419b-6815-4a68-8e32-b9fb111b596d",
        logo_id="be043706-a868-4999-83c8-f17bbd60745d",
        oauth_app_id="50de2b35-e593-417d-83d1-764544996fbb",
        last_error=None,
    )
    restored = load_artifacts(dump_artifacts(original))
    assert restored == original


def test_dump_load_roundtrip_failed_with_error() -> None:
    """FAILED state with an error string round-trips."""
    original = SkillCreationArtifacts(
        state=SkillCreationState.FAILED,
        skill_id="some-skill-id",
        last_error="HTTP 401: Unauthorized",
    )
    restored = load_artifacts(dump_artifacts(original))
    assert restored == original


def test_load_empty_returns_default() -> None:
    """Empty/None input yields a fresh default artifacts object."""
    assert load_artifacts(None) == SkillCreationArtifacts()
    assert load_artifacts("") == SkillCreationArtifacts()


def test_load_invalid_json_returns_default() -> None:
    """Corrupt JSON doesn't crash config — returns default."""
    assert load_artifacts("{not-json") == SkillCreationArtifacts()
    assert load_artifacts("null") == SkillCreationArtifacts()
    assert load_artifacts('"just-a-string"') == SkillCreationArtifacts()


def test_load_unknown_state_falls_back_to_none() -> None:
    """Unrecognised state values don't crash — reset to NONE."""
    raw = '{"state": "totally_bogus", "skill_id": "abc"}'
    result = load_artifacts(raw)
    assert result.state == SkillCreationState.NONE
    # Other fields still parse correctly
    assert result.skill_id == "abc"


def test_load_ignores_extra_fields() -> None:
    """Forward-compat: extra JSON keys are silently dropped."""
    raw = (
        '{"state": "done", "skill_id": "s1", "logo_id": "l1", '
        '"oauth_app_id": "o1", "future_field": "ignored"}'
    )
    result = load_artifacts(raw)
    assert result.state == SkillCreationState.DONE
    assert result.skill_id == "s1"


def test_load_empty_string_fields_become_none() -> None:
    """Empty-string fields normalise to None (matches default)."""
    raw = '{"state": "none", "skill_id": "", "logo_id": ""}'
    result = load_artifacts(raw)
    assert result.skill_id is None
    assert result.logo_id is None


def test_artifacts_frozen() -> None:
    """Artifacts are immutable — must copy via dataclasses.replace."""
    artifacts = SkillCreationArtifacts()
    try:
        artifacts.state = SkillCreationState.DONE  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        pass
    else:
        msg = "SkillCreationArtifacts must be frozen"
        raise AssertionError(msg)


def test_mark_failed_preserves_ids() -> None:
    """mark_failed() flips state to FAILED but keeps skill/oauth IDs for retry."""
    partial = SkillCreationArtifacts(
        state=SkillCreationState.OAUTH_CREATED,
        skill_id="s1",
        logo_id="l1",
        oauth_app_id="o1",
    )
    failed = mark_failed(partial, "network error")
    assert failed.state == SkillCreationState.FAILED
    assert failed.last_error == "network error"
    assert failed.skill_id == "s1"
    assert failed.logo_id == "l1"
    assert failed.oauth_app_id == "o1"


def test_state_is_string_enum() -> None:
    """State values compare equal to plain strings — useful for config storage."""
    assert SkillCreationState.DONE.value == "done"
    assert SkillCreationState.FAILED.value == "failed"
