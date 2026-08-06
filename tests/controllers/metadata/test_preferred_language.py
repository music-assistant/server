"""Tests for the preferred language handling of the MetaDataController."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from music_assistant.constants import CONF_CORE, CONF_LANGUAGE
from music_assistant.controllers.metadata import MetaDataController
from music_assistant.controllers.metadata.constants import (
    CONF_THUMB_CACHE_MAX_SIZE,
    DEFAULT_LANGUAGE,
)

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


@pytest.fixture
async def metadata_controller(mass_minimal: MusicAssistant) -> MetaDataController:
    """Construct a MetaDataController with the minimal MA fixture."""
    controller = MetaDataController(mass_minimal)
    mass_minimal.metadata = controller
    return controller


async def _save_unrelated_metadata_setting(mass: MusicAssistant) -> None:
    """Save the metadata core config the way the settings UI does."""
    await mass.config.save_core_config("metadata", {CONF_THUMB_CACHE_MAX_SIZE: 1000})


async def test_locale_falls_back_to_the_default_language(
    metadata_controller: MetaDataController,
) -> None:
    """With nothing stored the locale reads as the default language."""
    assert metadata_controller.locale == DEFAULT_LANGUAGE
    assert metadata_controller.preferred_language == "en"


async def test_set_default_preferred_language_seeds_an_unset_language(
    metadata_controller: MetaDataController,
) -> None:
    """An external source can still seed the language while none has been chosen."""
    metadata_controller.set_default_preferred_language("nl-NL")

    assert metadata_controller.locale == "nl_NL"


@pytest.mark.parametrize("language", [DEFAULT_LANGUAGE, "nl_NL"])
async def test_chosen_language_survives_a_config_save(
    metadata_controller: MetaDataController, language: str
) -> None:
    """
    A chosen language stays stored when another metadata setting is saved.

    Only values differing from their config entry default are persisted, so a language
    equal to the default must not be dropped from storage by an unrelated save.
    """
    mass = metadata_controller.mass
    metadata_controller.set_preferred_language(language)

    await _save_unrelated_metadata_setting(mass)

    assert mass.config.get(f"{CONF_CORE}/metadata/values/{CONF_LANGUAGE}") == language
    assert metadata_controller.locale == language


@pytest.mark.parametrize("language", [DEFAULT_LANGUAGE, "nl_NL"])
async def test_set_default_preferred_language_never_overrides_a_chosen_language(
    metadata_controller: MetaDataController, language: str
) -> None:
    """A chosen language is not overwritten by a seeded default after a config save."""
    mass = metadata_controller.mass
    metadata_controller.set_preferred_language(language)
    await _save_unrelated_metadata_setting(mass)

    metadata_controller.set_default_preferred_language("de-DE")

    assert metadata_controller.locale == language
