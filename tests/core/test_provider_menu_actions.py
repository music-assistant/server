"""Tests for the menu actions a provider declares, as a client of a real server uses them."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from music_assistant_models.auth import UserRole
from music_assistant_models.enums import ProviderTier
from music_assistant_models.errors import (
    ActionUnavailable,
    InsufficientPermissions,
    InvalidDataError,
    PlayerUnavailableError,
    ProviderUnavailableError,
)

from music_assistant import providers as providers_package
from music_assistant.helpers.util import purge_provider_modules
from music_assistant.mass import PROVIDERS_PATH

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
    from pathlib import Path

    from aiohttp import ClientWebSocketResponse

    from music_assistant.mass import MusicAssistant

FRAME = "menu_frame"
PLAIN = "menu_plain"
LABELS = "menu_labels"
DEMO = "_demo_plugin_provider"
ALBUM_URI = "library://album/1"
TRACK_URI = "library://track/1"
FRAME_CODE = """
from music_assistant_models.config_entries import ConfigActionResult

from music_assistant.models.plugin import PluginProvider

# the calls of handle_menu_action, as (instance id, action id)
CALLS = []


class FrameProvider(PluginProvider):
    async def handle_menu_action(self, action_id, uri, player_id):
        CALLS.append((self.instance_id, action_id))
        args = [action_id, self.instance_id, uri, str(player_id)]
        return ConfigActionResult(
            message=" ".join(args), translation_key="done", translation_args=args
        )


async def setup(mass, manifest, config):
    return FrameProvider(mass, manifest, config)
"""
PLAIN_CODE = """
from music_assistant.models.plugin import PluginProvider


async def setup(mass, manifest, config):
    return PluginProvider(mass, manifest, config)
"""
SHOW = {
    "id": "send_to_frame",
    "label": "Show on the picture frame",
    "media_types": ["album", "playlist"],
    "scope": "library.read",
    "icon": "mdi-image-frame",
}
PLAY = {
    "id": "play_on_frame",
    "label": "Play on the frame",
    "media_types": ["track"],
    "scope": "queues.control",
    "requires_player": True,
}
EDIT = {**SHOW, "id": "change_library", "label": "Change the library", "scope": "library.write"}


@pytest.fixture
def location(tmp_path: Path) -> Iterator[Path]:
    """Return a folder of community providers; what a test imports from it is undone."""
    folder = tmp_path / "community_providers"
    folder.mkdir()
    original_path = list(providers_package.__path__)
    imported_before = set(sys.modules)
    yield folder
    providers_package.__path__[:] = original_path
    for domain in (FRAME, PLAIN):
        if f"{providers_package.__name__}.{domain}" not in imported_before:
            purge_provider_modules(domain)


async def test_menu_actions_reach_client_translated(
    mass: MusicAssistant, location: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A client gets the valid menu actions of a provider, labelled in its own language."""
    with caplog.at_level(logging.WARNING):
        await _register(mass, location)
    token = await _token(mass, UserRole.GUEST)

    answers = {}
    for language in ("nl", "en"):
        async with mass.http_session_no_ssl.post(
            f"{mass.webserver.internal_base_url}/api",
            json={"message_id": "1", "command": "providers/manifests"},
            headers={"Authorization": f"Bearer {token}", "Accept-Language": language},
        ) as response:
            manifests = await response.json()
        answers[language] = next(m for m in manifests if m["domain"] == FRAME)["menu_actions"]

    assert answers["nl"] == [
        {**SHOW, "label": "Toon op de fotolijst", "requires_player": False},
        {**PLAY, "icon": None},
        {**EDIT, "requires_player": False},
    ]
    assert [action["label"] for action in answers["en"]] == [
        "Show on the picture frame",
        "Play on the frame",
        "Change the library",
    ]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert [w for w in warnings if w.startswith(f"Provider {FRAME}: left out")] == [
        f"Provider {FRAME}: left out menu action 'unknown_scope': needs a scope that Music "
        "Assistant knows, such as 'library.read'",
        f"Provider {FRAME}: left out menu action 2: needs string id, label and scope, a list of "
        "string media_types, and optionally a string icon and a boolean requires_player",
    ]


async def test_translated_labels_checked(mass: MusicAssistant, location: Path) -> None:
    """A translated label reaches a client only when it is one readable line of at most 80."""
    action = {"media_types": ["album"], "scope": "library.read"}
    menu_actions = [
        {**action, "id": "good", "label": "Show on the picture frame"},
        {**action, "id": "two_lines", "label": "Show on two lines"},
        {**action, "id": "long", "label": "Show a long text"},
        {**action, "id": "invisible", "label": "Show nothing"},
    ]
    texts = {
        "good": "Show on the frame",
        "two_lines": "Show on\ntwo lines",
        "long": "x" * 81,
        "invisible": "\u200b",
    }
    texts_nl = {
        "good": "Toon op de fotolijst",
        "two_lines": "Toon \u202eop twee regels",
        "long": "y" * 80,
        "invisible": "\u200b\ufeff",
    }
    _write_provider(
        location,
        LABELS,
        PLAIN_CODE,
        {"menu_actions": menu_actions},
        {"menu_actions": {key: {"label": text} for key, text in texts.items()}},
        {"menu_actions": {key: {"label": text} for key, text in texts_nl.items()}},
    )
    await mass.register_provider_manifest(str(location / LABELS), ProviderTier.COMMUNITY)
    token = await _token(mass, UserRole.GUEST)

    labels = {}
    for language in ("en", "nl"):
        async with mass.http_session_no_ssl.post(
            f"{mass.webserver.internal_base_url}/api",
            json={"message_id": "1", "command": "providers/manifests"},
            headers={"Authorization": f"Bearer {token}", "Accept-Language": language},
        ) as response:
            manifests = await response.json()
        manifest = next(m for m in manifests if m["domain"] == LABELS)
        labels[language] = [action["label"] for action in manifest["menu_actions"]]

    assert labels == {
        "en": ["Show on the frame", "Show on two lines", "Show a long text", "Show nothing"],
        "nl": ["Toon op de fotolijst", "Show on two lines", "y" * 80, "Show nothing"],
    }


@pytest.mark.parametrize(
    ("language", "message"),
    [
        ("nl", "send_to_frame door {0} voor library://album/1 op None"),
        ("en", "send_to_frame by {0} for library://album/1 on None"),
    ],
)
async def test_answer_translated_for_the_caller(
    mass: MusicAssistant, location: Path, language: str, message: str
) -> None:
    """The answer of a menu action reads in the language of the caller, from the provider's texts."""
    instance_id = (await _register(mass, location))[0]
    token = await _token(mass, UserRole.GUEST)

    async with _client(mass, token, language) as client:
        answer = await _invoke(client, instance_id, "send_to_frame", ALBUM_URI)

    assert answer == {"result": {"message": message.format(instance_id), "open_url": None}}


async def test_each_instance_answers_for_itself(mass: MusicAssistant, location: Path) -> None:
    """Of a provider with two instances, each instance runs the actions chosen for it."""
    instance_ids = await _register(mass, location)
    token = await _token(mass, UserRole.GUEST)

    async with _client(mass, token) as client:
        messages = [
            (await _invoke(client, instance_id, "send_to_frame", ALBUM_URI))["result"]["message"]
            for instance_id in instance_ids
        ]

    assert len(set(instance_ids)) == 2
    assert _hook_calls() == [(instance_id, "send_to_frame") for instance_id in instance_ids]
    assert messages == [
        f"send_to_frame by {instance_id} for {ALBUM_URI} on None" for instance_id in instance_ids
    ]


async def test_action_on_a_player(mass: MusicAssistant, location: Path) -> None:
    """An action that requires a player gets the checked player; any other action gets none."""
    instance_id = (await _register(mass, location))[0]
    player_id = await _demo_player(mass)
    token = await _token(mass, UserRole.GUEST)

    async with _client(mass, token) as client:
        play = await _invoke(client, instance_id, "play_on_frame", TRACK_URI, player_id)
        show = await _invoke(client, instance_id, "send_to_frame", ALBUM_URI, player_id)

    assert play["result"]["message"].endswith(f" on {player_id}")
    assert show["result"]["message"].endswith(" on None")


@pytest.mark.parametrize(
    ("instance", "action_id", "uri", "player_id", "error"),
    [
        ("unknown_instance", "send_to_frame", ALBUM_URI, None, ProviderUnavailableError),
        (FRAME, "unknown_action", ALBUM_URI, None, ActionUnavailable),
        (FRAME, "unknown_scope", ALBUM_URI, None, ActionUnavailable),
        (FRAME, "change_library", ALBUM_URI, None, InsufficientPermissions),
        (FRAME, "send_to_frame", TRACK_URI, None, InvalidDataError),
        (FRAME, "play_on_frame", TRACK_URI, None, InvalidDataError),
        (FRAME, "play_on_frame", TRACK_URI, "hidden_player", InsufficientPermissions),
        (FRAME, "play_on_frame", TRACK_URI, "allowed_player", PlayerUnavailableError),
        (PLAIN, "send_to_frame", ALBUM_URI, None, ActionUnavailable),
    ],
)
async def test_refused(
    mass: MusicAssistant,
    location: Path,
    instance: str,
    action_id: str,
    uri: str,
    player_id: str | None,
    error: type[Exception],
) -> None:
    """A call the user may not make, or that does not fit the action, is refused."""
    instance_ids = {FRAME: (await _register(mass, location))[0], PLAIN: PLAIN}
    guest = await mass.webserver.auth.create_user(
        username="guest", role=UserRole.GUEST, player_filter=["allowed_player"]
    )
    token = await mass.webserver.auth.create_token(guest, "test")

    async with _client(mass, token) as client:
        answer = await _invoke(
            client, instance_ids.get(instance, instance), action_id, uri, player_id
        )

    assert answer == {"error_code": getattr(error, "error_code")}  # noqa: B009
    assert _hook_calls() == []


async def test_refused_for_an_unloaded_provider(mass: MusicAssistant, location: Path) -> None:
    """A menu action of a provider instance that is no longer loaded is refused."""
    instance_id = (await _register(mass, location))[0]
    await mass.unload_provider(instance_id)
    token = await _token(mass, UserRole.ADMIN)

    async with _client(mass, token) as client:
        answer = await _invoke(client, instance_id, "send_to_frame", ALBUM_URI)

    assert answer == {"error_code": ProviderUnavailableError.error_code}
    assert _hook_calls() == []


async def test_refused_for_an_unavailable_provider(mass: MusicAssistant, location: Path) -> None:
    """A menu action of a provider instance that is loaded but not available is refused."""
    instance_id = (await _register(mass, location))[0]
    provider = mass.get_provider(instance_id, return_unavailable=True)
    assert provider is not None
    provider.available = False
    token = await _token(mass, UserRole.ADMIN)

    async with _client(mass, token) as client:
        answer = await _invoke(client, instance_id, "send_to_frame", ALBUM_URI)

    assert answer == {"error_code": ProviderUnavailableError.error_code}
    assert _hook_calls() == []


async def test_demo_menu_action(mass: MusicAssistant) -> None:
    """The menu action of the demo plugin answers with a message that names the item."""
    await mass.register_provider_manifest(os.path.join(PROVIDERS_PATH, DEMO), ProviderTier.CORE)
    instance_id = await _setup(mass, DEMO)
    token = await _token(mass, UserRole.GUEST)
    uri = "builtin://playlist/all_favorite_tracks"

    async with _client(mass, token) as client:
        answer = await _invoke(client, instance_id, "show_item", uri)

    item = await mass.music.get_item_by_uri(uri)
    assert answer == {
        "result": {"message": f"The demo plugin was asked about {item.name}", "open_url": None}
    }


async def _register(mass: MusicAssistant, location: Path) -> list[str]:
    """
    Register the test providers, set up two instances of the frame and one plain instance.

    Returns the instance ids of the frame provider.

    :param mass: The server.
    :param location: The folder of community providers to write them in.
    """
    frame_manifest = {
        "multi_instance": True,
        "menu_actions": [
            SHOW,
            {**SHOW, "id": "unknown_scope", "scope": "library.everything"},
            {**SHOW, "id": None},
            PLAY,
            EDIT,
        ],
    }
    texts = {
        "menu_actions": {"send_to_frame": {"label": "Show on the picture frame"}},
        "config_actions": {"done": "{0} by {1} for {2} on {3}"},
    }
    texts_nl = {
        "menu_actions": {"send_to_frame": {"label": "Toon op de fotolijst"}},
        "config_actions": {"done": "{0} door {1} voor {2} op {3}"},
    }
    _write_provider(location, FRAME, FRAME_CODE, frame_manifest, texts, texts_nl)
    _write_provider(location, PLAIN, PLAIN_CODE, {"menu_actions": [SHOW]})
    mass.add_provider_location(str(location))
    for domain in (FRAME, PLAIN):
        await mass.register_provider_manifest(str(location / domain), ProviderTier.COMMUNITY)
    instance_ids = [await _setup(mass, FRAME), await _setup(mass, FRAME)]
    await _setup(mass, PLAIN)
    return instance_ids


def _write_provider(
    location: Path,
    domain: str,
    code: str,
    manifest_keys: dict[str, Any],
    texts: dict[str, Any] | None = None,
    texts_nl: dict[str, Any] | None = None,
) -> None:
    """
    Write a plugin provider with its code, manifest and texts.

    :param location: The folder of community providers.
    :param domain: The domain of the provider.
    :param code: The code of its module.
    :param manifest_keys: Keys added to its manifest.
    :param texts: Its English texts.
    :param texts_nl: Its Dutch texts.
    """
    provider_dir = location / domain
    (provider_dir / "translations").mkdir(parents=True)
    manifest = {
        "type": "plugin",
        "domain": domain,
        "name": domain,
        "description": "test provider",
        "codeowners": [],
        **manifest_keys,
    }
    (provider_dir / "manifest.json").write_text(json.dumps(manifest))
    (provider_dir / "__init__.py").write_text(code)
    if texts:
        (provider_dir / "strings.json").write_text(json.dumps(texts))
    if texts_nl:
        (provider_dir / "translations" / "nl.json").write_text(json.dumps(texts_nl))


async def _setup(mass: MusicAssistant, domain: str) -> str:
    """
    Set up an instance of a provider and return its instance id once it is loaded.

    :param mass: The server.
    :param domain: The domain of the provider.
    """
    step = await mass.config.setup_provider(domain)
    assert step.result is not None
    instance_id: str = step.result["instance_id"]
    provider = mass.get_provider(instance_id)
    assert provider is not None
    await provider.initialized.wait()
    return instance_id


async def _demo_player(mass: MusicAssistant) -> str:
    """
    Set up the demo player provider and return the id of one of its players.

    :param mass: The server.
    """
    domain = "_demo_player_provider"
    await mass.register_provider_manifest(os.path.join(PROVIDERS_PATH, domain), ProviderTier.CORE)
    await _setup(mass, domain)
    async with asyncio.timeout(10):
        while not (
            players := [p for p in mass.players.all_players() if p.provider.startswith(domain)]
        ):
            await asyncio.sleep(0.05)
    return players[0].player_id


def _hook_calls() -> list[tuple[str, str]]:
    """Return the calls of handle_menu_action on the frame provider, as (instance, action)."""
    calls: list[tuple[str, str]] = sys.modules[f"{providers_package.__name__}.{FRAME}"].CALLS
    return calls


async def _token(mass: MusicAssistant, role: str) -> str:
    """
    Create a user with the given role and return a token of it.

    :param mass: The server to create the user on.
    :param role: The role of the user.
    """
    user = await mass.webserver.auth.create_user(username=f"user_{role}", role=role)
    return await mass.webserver.auth.create_token(user, "test")


@asynccontextmanager
async def _client(
    mass: MusicAssistant, token: str, language: str = "en"
) -> AsyncIterator[ClientWebSocketResponse]:
    """
    Connect to the websocket API as a client that signed in with the token and set its language.

    :param mass: The server.
    :param token: The token of the user.
    :param language: The language of the client.
    """
    url = f"{mass.webserver.internal_base_url}/ws"
    async with mass.http_session_no_ssl.ws_connect(url) as client:
        # the server greets a new connection with its info
        await client.receive_json(timeout=10)
        assert "result" in await _send(client, "auth", token=token)
        assert "result" in await _send(client, "translations/set_locale", locale=language)
        yield client


async def _invoke(
    client: ClientWebSocketResponse,
    instance_id: str,
    action_id: str,
    uri: str,
    player_id: str | None = None,
) -> dict[str, Any]:
    """
    Call providers/invoke_menu_action and return its result or error code.

    :param client: The connected client.
    :param instance_id: The provider instance.
    :param action_id: The menu action.
    :param uri: The uri of the media item.
    :param player_id: The player, if any.
    """
    args: dict[str, Any] = {"instance_id": instance_id, "action_id": action_id, "uri": uri}
    if player_id is not None:
        args["player_id"] = player_id
    answer = await _send(client, "providers/invoke_menu_action", **args)
    return {key: value for key, value in answer.items() if key in ("result", "error_code")}


async def _send(client: ClientWebSocketResponse, command: str, **args: Any) -> dict[str, Any]:
    """
    Send a command and return the answer to it.

    :param client: The connected client.
    :param command: The command.
    :param args: Its arguments.
    """
    message_id = uuid4().hex
    await client.send_json({"message_id": message_id, "command": command, "args": args})
    while True:
        message: dict[str, Any] = await client.receive_json(timeout=10)
        if message.get("message_id") == message_id:
            return message
