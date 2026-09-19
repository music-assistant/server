"""
Borrow mode: the plugin uses a linked Yandex Music instance's account.

Covers spec 0001 — account-source dropdown in the Authorization block,
read-only use of the borrowed x_token in the skill pipeline, and graceful
rendering when the linked instance is unavailable.
"""

from __future__ import annotations

from typing import Any
from unittest import mock

from music_assistant_models.enums import ProviderType
from ya_passport_auth.ma import BORROW_SOURCE_OWN

from music_assistant.providers.yandex_alice import get_config_entries
from music_assistant.providers.yandex_alice.constants import CONF_AUTH_X_TOKEN, CONF_YM_INSTANCE

from .localization import entry_text


def _make_mass(
    instances: dict[str, str] | None = None,
    owner_x_token: str | None = "test-x-ym",  # noqa: S107 — test stub, not a secret
) -> mock.MagicMock:
    """Mass stub: raw provider list + a loadable yandex_music owner."""
    mass = mock.MagicMock()
    mass.config.get.return_value = {
        inst_id: {"domain": "yandex_music", "name": name}
        for inst_id, name in (instances or {}).items()
    }
    owner = mock.MagicMock()
    owner.domain = "yandex_music"
    owner.type = ProviderType.MUSIC
    owner.config.get_value = lambda key: {"x_token": owner_x_token}.get(key)
    mass.get_provider.return_value = owner
    return mass


def _by_key(entries: tuple[Any, ...]) -> dict[str, Any]:
    return {e.key: e for e in entries}


async def test_dropdown_lists_instances_and_own() -> None:
    """The account-source dropdown offers every Yandex Music instance plus own credentials."""
    mass = _make_mass({"ym-a": "Main", "ym-b": "Second"})
    entries = await get_config_entries(mass, values={})
    source = _by_key(entries)[CONF_YM_INSTANCE]
    option_values = [opt.value for opt in source.options]
    assert BORROW_SOURCE_OWN in option_values
    assert "ym-a" in option_values
    assert "ym-b" in option_values


async def test_borrowing_hides_sign_in_and_out() -> None:
    """Borrow mode removes the Sign-in/Sign-out actions and names the source instance."""
    mass = _make_mass({"ym-a": "Main"})
    entries = await get_config_entries(mass, values={CONF_YM_INSTANCE: "ym-a"})
    by_key = _by_key(entries)
    assert "sign_in" not in by_key
    assert "clear_auth" not in by_key
    assert "label_auth_borrowed" in by_key
    assert "Yandex Music" in entry_text("label_auth_borrowed")


async def test_borrowed_token_feeds_actions_and_is_not_persisted() -> None:
    """A borrowed token authorizes the skill block without being stored in own config."""
    mass = _make_mass({"ym-a": "Main"}, owner_x_token="test-x-ym")
    values: dict[str, Any] = {CONF_YM_INSTANCE: "ym-a"}
    entries = await get_config_entries(mass, values=values)
    # Own token storage must stay empty while borrowing.
    assert not values.get(CONF_AUTH_X_TOKEN)
    hidden = _by_key(entries)[CONF_AUTH_X_TOKEN]
    assert not hidden.value
    # The skill block considers us authorized (borrowed token present):
    # the sign-in CTA is absent and the create-skill path is offered.
    assert "sign_in" not in _by_key(entries)


async def test_borrow_source_error_is_rendered_not_raised() -> None:
    """An unavailable source instance renders an alert instead of raising."""
    mass = _make_mass({"ym-a": "Main"})
    mass.get_provider.return_value = None  # instance not loaded
    entries = await get_config_entries(mass, values={CONF_YM_INSTANCE: "ym-a"})
    alerts = [e for e in entries if e.type.value == "alert"]
    assert alerts
    # The alert text is a strings.json template; the raw error message
    # travels through translation_params.
    params = " ".join(p for e in alerts for p in (getattr(e, "translation_params", None) or []))
    assert "not loaded" in params or "Yandex Music" in params


async def test_stale_selection_normalizes_to_own() -> None:
    """A selection pointing at a removed instance falls back to own credentials."""
    mass = _make_mass({"ym-a": "Main"})
    values: dict[str, Any] = {CONF_YM_INSTANCE: "removed-instance"}
    entries = await get_config_entries(mass, values=values)
    assert values[CONF_YM_INSTANCE] == BORROW_SOURCE_OWN
    assert "sign_in" in _by_key(entries)
