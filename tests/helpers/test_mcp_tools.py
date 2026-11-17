"""Tests for the rules of the MCP tools a provider declares."""

from __future__ import annotations

from typing import Any

import pytest
from music_assistant_models.auth import Scope
from music_assistant_models.provider import ProviderMCPTool

from music_assistant.helpers.mcp_tools import (
    MAX_MCP_TOOL_DESCRIPTION_LENGTH,
    MAX_MCP_TOOL_ENUM_VALUE_LENGTH,
    MAX_MCP_TOOL_ENUM_VALUES,
    MAX_MCP_TOOL_ID_LENGTH,
    MAX_MCP_TOOL_NAME_LENGTH,
    MAX_MCP_TOOL_PROPERTIES,
    MAX_MCP_TOOL_PROPERTY_DESCRIPTION_LENGTH,
    read_mcp_tools,
)

DOMAIN = "radio_service"
TYPES_PROBLEM = (
    "MCP tool 0: needs string id, description and scope, and optionally an object "
    "input_schema and a boolean requires_player"
)


def _property(**overrides: Any) -> dict[str, Any]:
    """Return a valid declared input schema property, with the given fields changed."""
    return {"type": "string", "description": "The name of the station.", **overrides}


def _schema(**properties: Any) -> dict[str, Any]:
    """Return a valid declared input schema with the given properties, none of them required."""
    return {"type": "object", "properties": properties}


def _tool(**overrides: Any) -> dict[str, Any]:
    """Return a valid declared MCP tool, as in a manifest file, with the given fields changed."""
    return {
        "id": "play_station",
        "description": "Play one of the user's saved stations of this service on a player.",
        "scope": "queues.control",
        "requires_player": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "station": _property(),
                "shuffle": {
                    "type": "boolean",
                    "description": "Shuffle the station.",
                    "default": False,
                },
            },
            "required": ["station"],
        },
        **overrides,
    }


def test_valid_tools_kept_untouched() -> None:
    """Valid tools are kept as declared, in their order, without problems."""
    without_schema = _tool(id="list_stations", scope="library.read")
    del without_schema["input_schema"]
    del without_schema["requires_player"]
    kept, problems = read_mcp_tools([_tool(), without_schema], DOMAIN)
    assert kept == [
        ProviderMCPTool(
            id="play_station",
            description="Play one of the user's saved stations of this service on a player.",
            scope=Scope.QUEUES_CONTROL,
            input_schema=_tool()["input_schema"],
            requires_player=True,
        ),
        ProviderMCPTool(
            id="list_stations",
            description="Play one of the user's saved stations of this service on a player.",
            scope=Scope.LIBRARY_READ,
        ),
    ]
    assert problems == []


@pytest.mark.parametrize(
    "prop",
    [
        _property(type="integer", default=3),
        _property(type="number", default=3),
        _property(type="number", default=0.5),
        _property(type="boolean", default=True),
        _property(enum=["rock", "jazz"], default="jazz"),
        _property(enum=["x" * MAX_MCP_TOOL_ENUM_VALUE_LENGTH] * MAX_MCP_TOOL_ENUM_VALUES),
        _property(description="x" * MAX_MCP_TOOL_PROPERTY_DESCRIPTION_LENGTH),
        _property(type="array", items={"type": "string"}),
        _property(type="array", items={"type": "integer"}, default=[1, 2]),
        _property(type="array", items={"type": "string", "enum": ["a", "b"]}, default=["b"]),
    ],
)
def test_property_following_the_rules_accepted(prop: dict[str, Any]) -> None:
    """A property of any allowed type, with a fitting enum, items or default, is kept."""
    kept, problems = read_mcp_tools([_tool(input_schema=_schema(value=prop))], DOMAIN)
    assert [tool.input_schema["properties"]["value"] for tool in kept] == [prop]
    assert problems == []


def test_limits_reached_accepted() -> None:
    """A tool at every length and count limit is kept."""
    tool_id = "x" * MAX_MCP_TOOL_ID_LENGTH
    properties = {f"p{index}": _property() for index in range(MAX_MCP_TOOL_PROPERTIES - 1)}
    properties["a" + "b" * 31] = _property()
    tool = _tool(
        id=tool_id,
        description="x" * MAX_MCP_TOOL_DESCRIPTION_LENGTH,
        input_schema={"type": "object", "properties": properties, "required": list(properties)},
    )
    kept, problems = read_mcp_tools([tool], DOMAIN)
    assert [tool.id for tool in kept] == [tool_id]
    assert problems == []


def test_not_a_list() -> None:
    """A mcp_tools value that is not a list keeps no tool."""
    assert read_mcp_tools({"id": "play_station"}, DOMAIN) == ([], ["'mcp_tools' must be a list"])


@pytest.mark.parametrize(
    "item",
    [
        "play_station",
        None,
        {key: value for key, value in _tool().items() if key != "scope"},
        {key: value for key, value in _tool().items() if key != "description"},
        _tool(id=5),
        _tool(description=None),
        _tool(scope=["queues.control"]),
        _tool(input_schema=None),
        _tool(input_schema=[]),
        _tool(requires_player="true"),
        _tool(requires_player=1),
    ],
)
def test_tool_of_wrong_type_left_out(item: object) -> None:
    """A tool with a field of the wrong type is left out with its problem; the others stay."""
    kept, problems = read_mcp_tools([item, _tool(id="other")], DOMAIN)
    assert [tool.id for tool in kept] == ["other"]
    assert problems == [TYPES_PROBLEM]


@pytest.mark.parametrize(
    ("overrides", "problem"),
    [
        ({"id": "Play"}, "needs an id"),
        ({"id": ""}, "needs an id"),
        ({"id": "play-station"}, "needs an id"),
        ({"id": "x" * 33}, "needs an id"),
        ({"description": ""}, "needs a description"),
        ({"description": "x" * (MAX_MCP_TOOL_DESCRIPTION_LENGTH + 1)}, "needs a description"),
        ({"description": "Play\na station"}, "needs a description"),
        ({"description": "Play\u202ea station"}, "needs a description"),
        ({"description": "\u200b"}, "needs a description"),
        ({"scope": "everything"}, "needs a scope"),
        ({"scope": ""}, "needs a scope"),
        ({"input_schema": {"type": "array"}}, "needs an input_schema of type 'object'"),
        ({"input_schema": {"properties": {}}}, "needs an input_schema of type 'object'"),
        (
            {"input_schema": {**_schema(), "additionalProperties": False}},
            "needs an input_schema of type 'object'",
        ),
        ({"input_schema": {**_schema(), "$ref": "#/x"}}, "needs an input_schema of type 'object'"),
        ({"input_schema": {"type": "object", "properties": []}}, "needs input_schema properties"),
        (
            {
                "input_schema": _schema(
                    **{f"p{index}": _property() for index in range(MAX_MCP_TOOL_PROPERTIES + 1)}
                )
            },
            "needs input_schema properties",
        ),
        ({"input_schema": _schema(player=_property())}, "property 'player' is added by the"),
        ({"input_schema": _schema(instance=_property())}, "property 'instance' is added by the"),
        ({"input_schema": _schema(Station=_property())}, "property 'Station' needs a name"),
        ({"input_schema": _schema(**{"1st": _property()})}, "property '1st' needs a name"),
        ({"input_schema": _schema(_x=_property())}, "property '_x' needs a name"),
        ({"input_schema": _schema(**{"a-b": _property()})}, "property 'a-b' needs a name"),
        ({"input_schema": _schema(**{"a" * 33: _property()})}, "needs a name"),
        (
            {"input_schema": {**_schema(station=_property()), "required": ["other"]}},
            "needs input_schema required",
        ),
        (
            {"input_schema": {**_schema(station=_property()), "required": "station"}},
            "needs input_schema required",
        ),
        (
            {"input_schema": {**_schema(station=_property()), "required": [["station"]]}},
            "needs input_schema required",
        ),
    ],
)
def test_tool_breaking_a_rule_left_out(overrides: dict[str, Any], problem: str) -> None:
    """A tool that breaks a rule is left out with one problem naming it; the others stay."""
    kept, problems = read_mcp_tools([_tool(**overrides), _tool(id="other")], DOMAIN)
    assert [tool.id for tool in kept] == ["other"]
    assert len(problems) == 1
    assert problems[0].startswith(f"MCP tool '{overrides.get('id', 'play_station')}': ")
    assert problem in problems[0]


@pytest.mark.parametrize(
    ("prop", "problem"),
    [
        ("station", "needs an object with only the keys"),
        (_property(format="date"), "needs an object with only the keys"),
        (_property(properties={}), "needs an object with only the keys"),
        (_property(type="object"), "needs a type from"),
        (_property(type="null"), "needs a type from"),
        (_property(type=["string", "null"]), "needs a type from"),
        ({"type": "string"}, "needs a description"),
        (_property(description=""), "needs a description"),
        (
            _property(description="x" * (MAX_MCP_TOOL_PROPERTY_DESCRIPTION_LENGTH + 1)),
            "needs a description",
        ),
        (_property(description="Name\nof the station"), "needs a description"),
        (_property(type="array"), "needs items when"),
        (_property(items={"type": "string"}), "needs items when"),
        (_property(type="array", items={"type": "object"}), "needs items with a type"),
        (_property(type="array", items={"type": "array"}), "needs items with a type"),
        (_property(type="array", items="string"), "needs items with a type"),
        (_property(type="array", items={"type": ["string"]}), "needs items with a type"),
        (
            _property(type="array", items={"type": "string", "description": "x"}),
            "needs items with a type",
        ),
        (_property(type="integer", enum=["1"]), "needs an enum"),
        (_property(enum=[1, 2]), "needs an enum"),
        (_property(enum=[]), "needs an enum"),
        (_property(enum="rock"), "needs an enum"),
        (_property(enum=["x"] * (MAX_MCP_TOOL_ENUM_VALUES + 1)), "needs an enum"),
        (_property(enum=["x" * (MAX_MCP_TOOL_ENUM_VALUE_LENGTH + 1)]), "needs an enum"),
        (_property(type="array", items={"type": "integer", "enum": ["1"]}), "needs an enum"),
        (_property(type="array", items={"type": "string", "enum": [1]}), "needs an enum"),
        (_property(default=1), "needs a default"),
        (_property(default=None), "needs a default"),
        (_property(enum=["rock"], default="jazz"), "needs a default"),
        (_property(type="integer", default=1.5), "needs a default"),
        (_property(type="integer", default=True), "needs a default"),
        (_property(type="number", default=False), "needs a default"),
        (_property(type="number", default="1"), "needs a default"),
        (_property(type="boolean", default=0), "needs a default"),
        (_property(type="array", items={"type": "integer"}, default=1), "needs a default"),
        (_property(type="array", items={"type": "integer"}, default=["1"]), "needs a default"),
        (
            _property(type="array", items={"type": "string", "enum": ["a"]}, default=["b"]),
            "needs a default",
        ),
    ],
)
def test_property_breaking_a_rule_left_out(prop: object, problem: str) -> None:
    """A tool with a property that breaks a rule is left out with the property named."""
    kept, problems = read_mcp_tools(
        [_tool(input_schema=_schema(value=prop)), _tool(id="other")], DOMAIN
    )
    assert [tool.id for tool in kept] == ["other"]
    assert len(problems) == 1
    assert problems[0].startswith(
        f"MCP tool 'play_station': input_schema property 'value' {problem}"
    )


def test_tool_name_limit() -> None:
    """A tool is kept up to a name "<domain>__<id>" of the limit, and left out beyond it."""
    domain = "d" * 40
    at_limit = "x" * (MAX_MCP_TOOL_NAME_LENGTH - len(domain) - 2)
    kept, problems = read_mcp_tools([_tool(id=at_limit), _tool(id=at_limit + "x")], domain)
    assert [tool.id for tool in kept] == [at_limit]
    assert problems == [
        f"MCP tool '{at_limit}x': needs a shorter id: the tool name '{domain}__{at_limit}x' has "
        f"more than {MAX_MCP_TOOL_NAME_LENGTH} characters"
    ]


def test_second_tool_with_same_id_left_out() -> None:
    """Of two tools with the same id the first is kept."""
    kept, problems = read_mcp_tools([_tool(), _tool(scope="library.read")], DOMAIN)
    assert [tool.scope for tool in kept] == [Scope.QUEUES_CONTROL]
    assert problems == [
        "MCP tool 'play_station': another MCP tool of this provider has the same id"
    ]


def test_tools_beyond_the_limit_left_out() -> None:
    """Only the first ten valid tools are kept, each one beyond them is reported."""
    tools = [_tool(id=f"tool_{index}") for index in range(12)]
    # an invalid tool does not count towards the limit
    tools.insert(1, _tool(id="broken", scope="nothing"))
    kept, problems = read_mcp_tools(tools, DOMAIN)
    assert [tool.id for tool in kept] == [f"tool_{index}" for index in range(10)]
    assert problems[1:] == [
        f"MCP tool 'tool_{index}': a provider declares at most 10 MCP tools" for index in (10, 11)
    ]
