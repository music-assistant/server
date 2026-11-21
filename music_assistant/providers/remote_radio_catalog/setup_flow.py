"""Native setup flow; adding the provider again creates another independent instance."""

from dataclasses import replace
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from music_assistant.models.setup_flow import SetupFlowError

from . import CONF_CATALOG_URL
from .catalog import validate_url

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession

ENTRIES = (ConfigEntry(key=CONF_CATALOG_URL, type=ConfigEntryType.STRING, required=True),)


async def run_setup(session: SetupSession) -> None:
    """Collect per-instance settings; provider display names use MA's native name field."""
    errors: dict[str, str | SetupFlowError] | None = None
    # Ignore legacy experimental refresh_interval values without changing the catalog URL.
    values = {
        key: value
        for key, value in {**session.context.setup_data, **session.context.values}.items()
        if key != "refresh_interval"
    }
    while True:
        submitted = await session.form(
            [replace(entry, value=values.get(entry.key, entry.value)) for entry in ENTRIES],
            step_id="user",
            errors=errors,
            last_step=True,
        )
        values.update(submitted)
        try:
            validate_url(values.get(CONF_CATALOG_URL))
        except ValueError:
            errors = {CONF_CATALOG_URL: "invalid_catalog_url"}
            continue
        try:
            await session.finish(values)
            return
        except SetupFlowError as err:
            errors = {"base": err}
