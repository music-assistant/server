"""Tests for the one refresh of an item's description after the preferred language changes."""

from __future__ import annotations

from time import time
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
from music_assistant_models.media_items import Album, MediaItemMetadata

from music_assistant.constants import CONF_LANGUAGE
from music_assistant.controllers.metadata import MetaDataController
from music_assistant.controllers.metadata.constants import (
    CONF_LANGUAGE_CHANGED,
    CONF_THUMB_CACHE_MAX_SIZE,
)
from music_assistant.controllers.metadata.enrichment import (
    _ONLINE_METADATA,
    _refresh_timestamp,
)

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

CHANGED = 1_000_000


def _controller(language: str = "de", changed: int = CHANGED) -> MetaDataController:
    """Create a bare MetaDataController with the given language, changed at ``changed``."""
    ctrl = MetaDataController.__new__(MetaDataController)
    ctrl.mass = MagicMock()
    values: dict[str, Any] = {CONF_LANGUAGE: f"{language}_DE", CONF_LANGUAGE_CHANGED: changed}
    ctrl.mass.config.get_raw_core_config_value = MagicMock(
        side_effect=lambda _domain, key, default=None: values.get(key, default)
    )
    return ctrl


def _metadata(
    description: str | None = "old text",
    language: str | None = "en",
    last_refresh: int | None = CHANGED - 1,
) -> MediaItemMetadata:
    """Return stored item metadata with the given description, language and refresh time."""
    return MediaItemMetadata(
        description=description, description_language=language, last_refresh=last_refresh
    )


@pytest.mark.parametrize(
    ("metadata", "changed", "expected"),
    [
        pytest.param(_metadata(), CHANGED, True, id="other language, refreshed before"),
        pytest.param(_metadata(language="de"), CHANGED, False, id="already preferred"),
        pytest.param(_metadata(language=None), CHANGED, False, id="unknown language"),
        pytest.param(_metadata(description=None), CHANGED, False, id="no description"),
        pytest.param(_metadata(last_refresh=CHANGED + 1), CHANGED, False, id="refreshed after"),
        pytest.param(_metadata(last_refresh=None), CHANGED, True, id="never refreshed"),
        pytest.param(_metadata(), 0, False, id="language never changed"),
    ],
)
def test_description_language_outdated(
    metadata: MediaItemMetadata, changed: int, expected: bool
) -> None:
    """Only a description in another known language, refreshed before the change, counts."""
    assert _controller(changed=changed)._description_language_outdated(metadata) is expected


_SWAPPED = {"description": "neuer Text", "description_language": "de"}


@pytest.mark.parametrize(
    ("language_outdated", "fresh", "expected"),
    [
        pytest.param(True, ("neuer Text", "de"), _SWAPPED, id="swaps to preferred language"),
        pytest.param(True, ("new text", "en"), {}, id="keeps it without preferred language"),
        pytest.param(False, ("neuer Text", "de"), {}, id="keeps it without language change"),
    ],
)
def test_fill_only_refresh_swaps_an_outdated_description(
    language_outdated: bool, fresh: tuple[str, str], expected: dict[str, str]
) -> None:
    """A refresh that only fills gaps swaps an outdated description for a preferred one."""
    stored = _metadata()
    source = MediaItemMetadata(description=fresh[0], description_language=fresh[1])

    replacements = _controller()._apply_fresh_values(
        stored, [(_ONLINE_METADATA, source)], (), False, language_outdated
    )

    assert replacements == expected


async def test_open_item_with_outdated_description_is_scheduled() -> None:
    """Opening an item refreshed recently, but before the language change, schedules a refresh."""
    ctrl = _controller(changed=int(time()) - 10)
    run_task = ctrl.mass.tasks.run_background_task = MagicMock()  # type: ignore[method-assign]
    album = Album(item_id="1", provider="library", name="Album", provider_mappings=set())
    album.metadata = _metadata(last_refresh=int(time()) - 20)

    ctrl.schedule_update_metadata(album)
    run_task.assert_called_once()

    run_task.reset_mock()
    album.metadata = _metadata(language="de", last_refresh=int(time()) - 20)
    ctrl.schedule_update_metadata(album)
    run_task.assert_not_called()


def test_failed_language_refresh_counts_as_done() -> None:
    """A refresh that failed after a language change is stamped no earlier than the change."""
    now = int(time())
    changed = now - 10

    assert _refresh_timestamp(True, changed) == changed
    assert _refresh_timestamp(False, changed) >= now


async def test_set_preferred_language_notes_a_change(
    metadata_controller: MetaDataController,
) -> None:
    """Choosing another language notes when it changed; another region of it does not."""
    metadata_controller.set_preferred_language("en_GB")
    assert metadata_controller.language_changed == 0

    metadata_controller.set_preferred_language("nl_NL")
    assert metadata_controller.language_changed > 0


async def test_settings_save_notes_a_language_change(
    metadata_controller: MetaDataController,
) -> None:
    """A language saved from the settings notes when it changed; other saves do not."""
    mass: MusicAssistant = metadata_controller.mass

    await mass.config.save_core_config("metadata", {CONF_THUMB_CACHE_MAX_SIZE: 1000})
    await mass.config.save_core_config("metadata", {CONF_LANGUAGE: "en_AU"})
    assert metadata_controller.language_changed == 0

    await mass.config.save_core_config("metadata", {CONF_LANGUAGE: "fr_FR"})
    assert metadata_controller.language_changed > 0
