"""
Every rendered config entry must resolve its user-facing text.

Entry texts live in ``strings.json`` under ``config_entries.<key>.<field>``
and are resolved by MA at serialization (``translation_key`` re-keys an
entry when one config key needs several texts). An entry that neither
passes ``label=`` in code nor is authored in ``strings.json`` would render
empty in the UI — these tests walk the form through its major states and
fail on any such entry.

Entries whose text is composed at runtime (dispatcher outcome / update
messages, probe descriptions) pass ``label=``/``description=`` directly;
their keys must NOT be authored in ``strings.json``, or the static
translation would silently override the dynamic text.
"""

from __future__ import annotations

import re
from typing import Any
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import ProviderType
from ya_dialogs_api import SkillCreationArtifacts, SkillCreationState, dump_artifacts

from music_assistant.providers.yandex_alice import get_config_entries
from music_assistant.providers.yandex_alice.constants import (
    CATEGORY_AUTHORIZATION,
    CATEGORY_SETTINGS,
    CATEGORY_SKILL,
    CONF_AUTH_X_TOKEN,
    CONF_DIALOG_AUTO_CREATE_ARTIFACTS,
    CONF_EDIT_MODE,
    CONF_EXTERNAL_BASE_URL,
    CONF_PENDING_DUPLICATE_SKILL_ID,
    CONF_PENDING_DUPLICATE_SKILL_NAME,
    CONF_YM_INSTANCE,
)

from .localization import authored_texts, load_strings

_PLACEHOLDER = re.compile(r"\{(\d+)\}")


def _make_mass(ym_instances: dict[str, str] | None = None) -> MagicMock:
    mass = MagicMock()
    mass.players.all_players = MagicMock(return_value=[])
    mass.webserver = None
    mass.config.get.return_value = {
        inst_id: {"domain": "yandex_music", "name": name}
        for inst_id, name in (ym_instances or {}).items()
    }
    return mass


def _artifacts(state: SkillCreationState, **kwargs: Any) -> str:
    return dump_artifacts(SkillCreationArtifacts(state=state, **kwargs))


_FORM_STATES: dict[str, dict[str, Any]] = {
    "signed_out": {},
    "signed_in_idle": {CONF_AUTH_X_TOKEN: "tok"},
    "pipeline_running": {
        CONF_AUTH_X_TOKEN: "tok",
        CONF_DIALOG_AUTO_CREATE_ARTIFACTS: _artifacts(
            SkillCreationState.APP_CREATED, skill_id="sk-1"
        ),
    },
    "failed": {
        CONF_AUTH_X_TOKEN: "tok",
        CONF_EXTERNAL_BASE_URL: "https://ma.example.org",
        CONF_DIALOG_AUTO_CREATE_ARTIFACTS: _artifacts(SkillCreationState.FAILED, last_error="boom"),
    },
    "failed_no_detail": {
        CONF_AUTH_X_TOKEN: "tok",
        CONF_DIALOG_AUTO_CREATE_ARTIFACTS: _artifacts(SkillCreationState.FAILED),
    },
    "duplicate_pending": {
        CONF_AUTH_X_TOKEN: "tok",
        CONF_PENDING_DUPLICATE_SKILL_ID: "sk-dup",
        CONF_PENDING_DUPLICATE_SKILL_NAME: "My Skill",
    },
    "registered": {
        CONF_AUTH_X_TOKEN: "tok",
        CONF_DIALOG_AUTO_CREATE_ARTIFACTS: _artifacts(SkillCreationState.DONE, skill_id="sk-1"),
    },
    "registered_edit_mode": {
        CONF_AUTH_X_TOKEN: "tok",
        CONF_DIALOG_AUTO_CREATE_ARTIFACTS: _artifacts(SkillCreationState.DONE, skill_id="sk-1"),
        CONF_EDIT_MODE: True,
    },
    "borrowing": {CONF_YM_INSTANCE: "ym-a"},
    "borrowing_error": {CONF_YM_INSTANCE: "ym-broken"},
}


async def _render(state: str) -> tuple[Any, ...]:
    """Render the form in the named state (borrow states get a linked instance)."""
    if state == "borrowing":
        mass = _make_mass({"ym-a": "Main"})
        owner = MagicMock()
        owner.domain = "yandex_music"
        owner.type = ProviderType.MUSIC
        owner.config.get_value = lambda key: {"x_token": "test-x-ym"}.get(key)
        mass.get_provider.return_value = owner
    elif state == "borrowing_error":
        mass = _make_mass({"ym-broken": "Main"})
        mass.get_provider.return_value = None
    else:
        mass = _make_mass()
    entries: tuple[Any, ...] = await get_config_entries(mass, values=dict(_FORM_STATES[state]))
    return entries


@pytest.mark.asyncio
@pytest.mark.parametrize("state", _FORM_STATES)
async def test_every_visible_entry_resolves_text(state: str) -> None:
    """Non-hidden entries carry an in-code label or an authored strings.json text."""
    entries = await _render(state)
    assert entries
    for entry in entries:
        if getattr(entry, "hidden", False):
            continue
        texts = authored_texts(entry)
        in_code_label = getattr(entry, "label", None)
        assert in_code_label or "label" in texts, (
            f"{state}: entry '{entry.key}' has no label in code and none authored in strings.json"
        )
        if entry.type.value == "action":
            assert getattr(entry, "action_label", None) or "action_label" in texts, (
                f"{state}: action entry '{entry.key}' has no action_label anywhere"
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("state", _FORM_STATES)
async def test_dynamic_texts_are_not_shadowed_by_strings_json(state: str) -> None:
    """Runtime-composed label=/description= must stay unauthored in strings.json."""
    for entry in await _render(state):
        texts = authored_texts(entry)
        for field in ("label", "description"):
            if not getattr(entry, field, None):
                continue
            assert field not in texts, (
                f"{state}: entry '{entry.key}' passes a runtime {field} but the same "
                f"field is also authored in strings.json — the static translation "
                "would override the dynamic text"
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("state", _FORM_STATES)
async def test_placeholders_receive_translation_params(state: str) -> None:
    """
    Authored ``{n}`` placeholders must be fed by enough translation_params.

    ``action_label`` is resolved by MA without params, so it may not
    carry placeholders at all (checked separately below).
    """
    for entry in await _render(state):
        texts = authored_texts(entry)
        params = getattr(entry, "translation_params", None) or []
        for field in ("label", "description"):
            placeholders = [int(m) for m in _PLACEHOLDER.findall(texts.get(field, ""))]
            if not placeholders:
                continue
            assert len(params) > max(placeholders), (
                f"{state}: '{entry.key}.{field}' uses placeholder "
                f"{{{max(placeholders)}}} but the entry provides "
                f"only {len(params)} translation_params"
            )


def test_action_labels_carry_no_placeholders() -> None:
    """MA resolves action_label without params — placeholders would render literally."""
    for key, texts in load_strings()["config_entries"].items():
        assert not _PLACEHOLDER.search(texts.get("action_label", "")), (
            f"'{key}.action_label' contains a {{n}} placeholder, but MA never "
            "substitutes translation_params into action_label"
        )


def test_states_only_entries_are_authored() -> None:
    """Texts of entries rendered only in hard-to-simulate states stay authored."""
    authored = load_strings()["config_entries"]
    assert "label" in authored.get("label_diagnostics_inactive", {})
    manifest_results = {
        "manifest_export_exists",
        "manifest_export_write_error",
        "manifest_export_success",
        "manifest_import_empty",
        "manifest_import_decode_error",
        "manifest_import_invalid",
        "manifest_import_write_error",
        "manifest_import_success",
        "manifest_reset_absent",
        "manifest_reset_delete_error",
        "manifest_reset_success",
        "manifest_validate_absent",
        "manifest_validate_read_error",
        "manifest_validate_invalid",
        "manifest_validate_success",
    }
    assert manifest_results <= set(authored)


def test_config_categories_are_authored() -> None:
    """The three form section slugs resolve from config_categories."""
    categories = load_strings()["config_categories"]
    assert {CATEGORY_AUTHORIZATION, CATEGORY_SKILL, CATEGORY_SETTINGS} <= set(categories)
