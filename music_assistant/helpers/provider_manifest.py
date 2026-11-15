"""Read the manifest of a provider, from its file or from the data of the community catalog."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from music_assistant_models.provider import ProviderManifest

from music_assistant.helpers.json import json_loads
from music_assistant.helpers.menu_actions import read_menu_actions


async def read_manifest_file(path: str | Path) -> tuple[ProviderManifest, list[str]]:
    """
    Return the manifest in a manifest.json file, and why each left out menu action is left out.

    :param path: The manifest.json file.
    :raises OSError: The file cannot be read.
    :raises ValueError: The file is not a valid manifest.
    :raises LookupError: The manifest misses a mandatory key.
    """
    return manifest_from_json(await asyncio.to_thread(Path(path).read_text, encoding="utf-8"))


def manifest_from_json(text: str) -> tuple[ProviderManifest, list[str]]:
    """
    Return the manifest in the contents of a manifest.json file, and why each left out menu action is left out.

    :param text: The contents of the manifest.json file.
    :raises ValueError: The text is not a valid manifest.
    :raises LookupError: The manifest misses a mandatory key.
    """
    data = json_loads(text)
    if not isinstance(data, dict):
        msg = "a manifest is a JSON object"
        raise ValueError(msg)  # noqa: TRY004 - callers handle an invalid manifest as ValueError
    return manifest_from_dict(data)


def manifest_from_dict(data: dict[str, Any]) -> tuple[ProviderManifest, list[str]]:
    """
    Return the manifest in the parsed data of a manifest, and why each left out menu action is left out.

    Whatever ``menu_actions`` holds, it never makes the manifest invalid: the actions that break a
    rule are left out (see ``read_menu_actions``).

    :param data: The parsed contents of a manifest.json file.
    :raises ValueError: The data is not a valid manifest.
    :raises LookupError: The manifest misses a mandatory key.
    """
    fields = dict(data)
    raw_actions = fields.pop("menu_actions", [])
    manifest = ProviderManifest.from_dict(fields)
    manifest.menu_actions, problems = read_menu_actions(raw_actions)
    return manifest, problems
