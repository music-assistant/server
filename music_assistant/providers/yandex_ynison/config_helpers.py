"""Configuration helpers for the Yandex Ynison plugin."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .constants import CONF_TOKEN, CONF_X_TOKEN

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


def find_sibling_token(
    mass: MusicAssistant,
    instance_id: str | None,
) -> tuple[str | None, str | None]:
    """Find token and x_token from an existing sibling ynison instance."""
    raw_providers = mass.config.get("providers", {})
    for prov_conf in raw_providers.values():
        if prov_conf.get("domain") != "yandex_ynison":
            continue
        if instance_id and prov_conf.get("instance_id") == instance_id:
            continue
        prov_values = prov_conf.get("values", {})
        token = prov_values.get(CONF_TOKEN)
        if token:
            x_token = prov_values.get(CONF_X_TOKEN)
            return mass.config.decrypt_string(str(token)), (
                mass.config.decrypt_string(str(x_token)) if x_token else None
            )
    return None, None
