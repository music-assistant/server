"""Configuration helpers for the Yandex Ynison plugin."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


def list_yandex_music_instances(mass: MusicAssistant) -> list[tuple[str, str]]:
    """List configured yandex_music provider instances as (instance_id, display_name) pairs."""
    instances: list[tuple[str, str]] = []
    raw_providers = mass.config.get("providers", {})
    for instance_id, prov_conf in raw_providers.items():
        if prov_conf.get("domain") != "yandex_music":
            continue
        if not prov_conf.get("enabled", True):
            continue
        display_name = prov_conf.get("name") or instance_id
        instances.append((str(instance_id), str(display_name)))
    return instances
