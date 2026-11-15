"""Tests for the menu actions a provider declares, as a client of a real server sees them."""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING, Any

import pytest
from music_assistant_models.auth import UserRole
from music_assistant_models.enums import ProviderTier

from music_assistant.mass import PROVIDERS_PATH

if TYPE_CHECKING:
    from pathlib import Path

    from music_assistant.mass import MusicAssistant

DOMAIN = "frame"
DEMO_DOMAIN = "_demo_plugin_provider"
FAVORITES_URI = "builtin://playlist/all_favorite_tracks"


async def _call(
    mass: MusicAssistant,
    token: str,
    command: str,
    language: str = "en",
    **args: Any,
) -> tuple[int, Any]:
    """
    Call an API command over HTTP the way a client does, and return the status and answer.

    :param mass: The server to call.
    :param token: The token of the calling user.
    :param command: The API command.
    :param language: The language of the client.
    :param args: The arguments of the command.
    """
    async with mass.http_session_no_ssl.post(
        f"{mass.webserver.internal_base_url}/api",
        json={"message_id": "1", "command": command, "args": args},
        headers={"Authorization": f"Bearer {token}", "Accept-Language": language},
    ) as response:
        if response.content_type == "application/json":
            return response.status, await response.json()
        return response.status, await response.text()


async def _token(mass: MusicAssistant, role: str) -> str:
    """
    Create a user with the given role and return a token of it.

    :param mass: The server to create the user on.
    :param role: The role of the user.
    """
    user = await mass.webserver.auth.create_user(username=f"user_{role}", role=role)
    return await mass.webserver.auth.create_token(user, "test")


def _write_provider(location: Path) -> Path:
    """
    Write a community provider that declares valid and invalid menu actions, with its texts.

    :param location: Folder that holds the provider folders.
    """
    provider_dir = location / DOMAIN
    (provider_dir / "translations").mkdir(parents=True)
    action = {
        "id": "send_to_frame",
        "label": "Show on the picture frame",
        "media_types": ["album", "playlist"],
        "icon": "mdi-image-frame",
        "command": f"{DOMAIN}/send_to_frame",
    }
    manifest = {
        "type": "plugin",
        "domain": DOMAIN,
        "name": "Picture frame",
        "description": "test provider",
        "codeowners": [],
        "menu_actions": [
            action,
            {
                **action,
                "id": "play_on_frame",
                "label": "Play on the frame",
                "command": f"{DOMAIN}/play_on_frame",
                "plays": True,
            },
            {**action, "id": "foreign", "command": "players/cmd/play"},
            {**action, "id": "genre", "media_types": ["genre"]},
        ],
    }
    (provider_dir / "manifest.json").write_text(json.dumps(manifest))
    labels = {"send_to_frame": {"label": "Show on the picture frame"}}
    (provider_dir / "strings.json").write_text(json.dumps({"menu_actions": labels}))
    labels_nl = {"send_to_frame": {"label": "Toon op de fotolijst"}}
    (provider_dir / "translations" / "nl.json").write_text(json.dumps({"menu_actions": labels_nl}))
    return provider_dir


async def test_menu_actions_reach_client_translated(
    mass: MusicAssistant, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A client gets the valid menu actions of a provider, labelled in its own language."""
    provider_dir = _write_provider(tmp_path / "community_providers")
    with caplog.at_level(logging.WARNING):
        await mass.register_provider_manifest(str(provider_dir), ProviderTier.COMMUNITY)
    token = await _token(mass, UserRole.GUEST)

    answers = {}
    for language in ("nl", "en"):
        status, manifests = await _call(mass, token, "providers/manifests", language)
        assert status == 200
        answers[language] = next(m for m in manifests if m["domain"] == DOMAIN)["menu_actions"]

    assert answers["nl"] == [
        {
            "id": "send_to_frame",
            "label": "Toon op de fotolijst",
            "media_types": ["album", "playlist"],
            "command": "frame/send_to_frame",
            "icon": "mdi-image-frame",
            "plays": False,
        },
        {
            "id": "play_on_frame",
            "label": "Play on the frame",
            "media_types": ["album", "playlist"],
            "command": "frame/play_on_frame",
            "icon": "mdi-image-frame",
            "plays": True,
        },
    ]
    assert [action["label"] for action in answers["en"]] == [
        "Show on the picture frame",
        "Play on the frame",
    ]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert [w for w in warnings if w.startswith(f"Provider {DOMAIN}: left out")] == [
        "Provider frame: left out menu action 'foreign': needs a command in the namespace of "
        "its provider ('frame/...')",
        "Provider frame: left out menu action 'genre': needs media_types from: album, artist, "
        "audiobook, playlist, podcast, podcast_episode, radio, track",
    ]


@pytest.mark.parametrize("role", [UserRole.GUEST, UserRole.ADMIN])
async def test_demo_menu_action_answers_with_message(mass: MusicAssistant, role: str) -> None:
    """The command of the demo menu action answers a user that may read the library."""
    await _setup_demo_plugin(mass)
    token = await _token(mass, role)

    status, result = await _call(mass, token, f"{DEMO_DOMAIN}/show_item", uri=FAVORITES_URI)

    assert status == 200
    item = await mass.music.get_item_by_uri(FAVORITES_URI)
    assert result == {
        "message": f"The demo plugin was asked about {item.name}",
        "open_url": None,
    }


async def test_demo_menu_action_refuses_user_without_scope(mass: MusicAssistant) -> None:
    """The command of the demo menu action refuses a user whose role grants no library access."""
    await _setup_demo_plugin(mass)
    # a role that is neither builtin nor custom grants no scopes
    token = await _token(mass, "retired_role")

    status, result = await _call(mass, token, f"{DEMO_DOMAIN}/show_item", uri=FAVORITES_URI)

    assert status == 403
    assert "library.read" in result


async def test_demo_menu_action_command_removed_on_unload(mass: MusicAssistant) -> None:
    """The command of the demo menu action goes away with the provider."""
    instance_id = await _setup_demo_plugin(mass)
    assert f"{DEMO_DOMAIN}/show_item" in mass.command_handlers

    await mass.unload_provider(instance_id)

    assert f"{DEMO_DOMAIN}/show_item" not in mass.command_handlers


async def _setup_demo_plugin(mass: MusicAssistant) -> str:
    """
    Register and set up the demo plugin provider, and return its instance id.

    :param mass: The server to set it up on.
    """
    await mass.register_provider_manifest(
        os.path.join(PROVIDERS_PATH, DEMO_DOMAIN), ProviderTier.CORE
    )
    step = await mass.config.setup_provider(DEMO_DOMAIN)
    assert step.result is not None
    instance_id = step.result["instance_id"]
    provider = mass.get_provider(instance_id)
    assert provider is not None
    await provider.initialized.wait()
    return instance_id
