"""Configuration helpers for the Yandex Ynison plugin."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


async def list_yandex_music_instances(mass: MusicAssistant) -> list[tuple[str, str]]:
    """List configured yandex_music provider instances as (instance_id, display_name) pairs."""
    configs = await mass.config.get_provider_configs(provider_domain="yandex_music")
    return [
        (config.instance_id, config.name or config.instance_id)
        for config in configs
        if config.enabled
    ]
