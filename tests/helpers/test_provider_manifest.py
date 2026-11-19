"""Tests for reading the manifest of a provider."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from mashumaro.exceptions import MissingField

from music_assistant.helpers.provider_manifest import (
    manifest_from_dict,
    manifest_from_json,
    read_manifest_file,
)

if TYPE_CHECKING:
    from pathlib import Path

ACTION = {
    "id": "send_to_frame",
    "label": "Show on the picture frame",
    "media_types": ["album"],
    "scope": "library.read",
}
PAGE = {"label": "Picture frame", "scope": "library.read"}
TOOL = {
    "id": "show_photo",
    "description": "Show a photo of the playing album on the picture frame.",
    "scope": "library.read",
}


def _manifest(**keys: Any) -> dict[str, Any]:
    """Return the data of a valid manifest, with the given keys added."""
    return {
        "type": "plugin",
        "domain": "frame",
        "name": "Picture frame",
        "description": "A provider.",
        "codeowners": [],
        **keys,
    }


def test_manifest_without_menu_actions() -> None:
    """A manifest without menu_actions has none and no problems."""
    manifest, problems = manifest_from_dict(_manifest())
    assert manifest.domain == "frame"
    assert manifest.menu_actions == []
    assert manifest.mcp_tools == []
    assert problems == []


def test_valid_menu_actions_kept() -> None:
    """Valid menu actions are kept as declared."""
    second = {**ACTION, "id": "play", "requires_player": True}
    manifest, problems = manifest_from_dict(_manifest(menu_actions=[ACTION, second]))
    assert [action.to_dict() for action in manifest.menu_actions] == [
        {**ACTION, "icon": None, "requires_player": False},
        {**second, "icon": None},
    ]
    assert problems == []


@pytest.mark.parametrize(
    ("menu_actions", "problem"),
    [
        ({"id": "send_to_frame"}, "'menu_actions' must be a list"),
        ("send_to_frame", "'menu_actions' must be a list"),
        (None, "'menu_actions' must be a list"),
        (["send_to_frame"], "menu action 0: needs string id"),
        ([{"id": "send_to_frame", "label": "Show"}], "menu action 0: needs string id"),
        ([{**ACTION, "label": None}], "menu action 0: needs string id"),
        ([{**ACTION, "requires_player": "false"}], "menu action 0: needs string id"),
        ([{**ACTION, "media_types": ["genre"]}], "menu action 'send_to_frame': needs media_types"),
    ],
)
def test_broken_menu_actions_never_break_the_manifest(menu_actions: object, problem: str) -> None:
    """Whatever menu_actions holds, the manifest is read, the action left out and its problem named."""
    manifest, problems = manifest_from_dict(_manifest(menu_actions=menu_actions))
    assert manifest.domain == "frame"
    assert manifest.menu_actions == []
    assert len(problems) == 1
    assert problems[0].startswith(problem)


def test_valid_mcp_tools_kept() -> None:
    """Valid MCP tools are kept as declared."""
    manifest, problems = manifest_from_dict(_manifest(mcp_tools=[TOOL]))
    assert [tool.to_dict() for tool in manifest.mcp_tools] == [
        {**TOOL, "input_schema": {}, "requires_player": False}
    ]
    assert problems == []


@pytest.mark.parametrize(
    ("mcp_tools", "problem"),
    [
        ({"id": "show_photo"}, "'mcp_tools' must be a list"),
        (None, "'mcp_tools' must be a list"),
        (["show_photo"], "MCP tool 0: needs string id"),
        ([{**TOOL, "input_schema": []}], "MCP tool 0: needs string id"),
        ([{**TOOL, "input_schema": {"type": "array"}}], "MCP tool 'show_photo': needs an"),
        ([{**TOOL, "id": "x" * 51}], "MCP tool '" + "x" * 51 + "': needs an id"),
    ],
)
def test_broken_mcp_tools_never_break_the_manifest(mcp_tools: object, problem: str) -> None:
    """Whatever mcp_tools holds, the manifest is read, the tool left out and its problem named."""
    manifest, problems = manifest_from_dict(_manifest(mcp_tools=mcp_tools))
    assert manifest.domain == "frame"
    assert manifest.mcp_tools == []
    assert len(problems) == 1
    assert problems[0].startswith(problem)


def test_problems_of_menu_actions_and_mcp_tools_both_named() -> None:
    """The problems of the menu actions and of the MCP tools are both returned, in that order."""
    manifest, problems = manifest_from_dict(
        _manifest(menu_actions=[{**ACTION, "scope": "x"}], mcp_tools=[{**TOOL, "scope": "x"}])
    )
    assert (manifest.menu_actions, manifest.mcp_tools) == ([], [])
    assert [problem.split(":")[0] for problem in problems] == [
        "menu action 'send_to_frame'",
        "MCP tool 'show_photo'",
    ]


def test_valid_page_kept() -> None:
    """A valid page is kept as declared."""
    manifest, problems = manifest_from_dict(_manifest(page=PAGE))
    assert manifest.page is not None
    assert manifest.page.to_dict() == {**PAGE, "icon": None}
    assert problems == []


@pytest.mark.parametrize(
    ("page", "problem"),
    [
        ("Picture frame", "page: needs string label and scope"),
        ({"label": "Picture frame"}, "page: needs string label and scope"),
        ({**PAGE, "scope": "everything"}, "page: needs a scope"),
    ],
)
def test_broken_page_never_breaks_the_manifest(page: object, problem: str) -> None:
    """Whatever page holds, the manifest is read, the page left out and its problem named."""
    manifest, problems = manifest_from_dict(_manifest(page=page))
    assert manifest.domain == "frame"
    assert manifest.page is None
    assert len(problems) == 1
    assert problems[0].startswith(problem)


def test_invalid_manifest_still_fails() -> None:
    """A manifest that misses a mandatory key still fails, whatever its menu actions."""
    data = _manifest(menu_actions=[ACTION])
    del data["codeowners"]
    with pytest.raises(MissingField):
        manifest_from_dict(data)


@pytest.mark.parametrize("text", ["[]", "not json"])
def test_manifest_text_that_is_no_object_fails(text: str) -> None:
    """A manifest file that is no JSON object fails as an invalid manifest."""
    with pytest.raises(ValueError):  # noqa: PT011 - both the parser and the reader raise it
        manifest_from_json(text)


async def test_read_manifest_file(tmp_path: Path) -> None:
    """A manifest file with a broken action is read without it."""
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(_manifest(menu_actions=[ACTION, {**ACTION, "id": None}])))
    manifest, problems = await read_manifest_file(path)
    assert [action.id for action in manifest.menu_actions] == ["send_to_frame"]
    assert problems == [
        "menu action 1: needs string id, label and scope, a list of string media_types, and "
        "optionally a string icon and a boolean requires_player"
    ]
