"""Helpers for generating API documentation and OpenAPI specifications."""

from __future__ import annotations

import collections.abc
import inspect
from collections.abc import Callable
from dataclasses import MISSING
from datetime import datetime
from enum import Enum
from types import NoneType, UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints

from music_assistant_models.player import Player as PlayerState

from music_assistant.helpers.api import APICommandHandler


def _format_type_name(type_hint: Any) -> str:
    """Format a type hint as a user-friendly string, using JSON types instead of Python types."""
    if type_hint is NoneType or type_hint is type(None):
        return "null"

    # Handle internal Player model - replace with PlayerState
    if hasattr(type_hint, "__name__") and type_hint.__name__ == "Player":
        if (
            hasattr(type_hint, "__module__")
            and type_hint.__module__ == "music_assistant.models.player"
        ):
            return "PlayerState"

    # Handle PluginSource - replace with PlayerSource (parent type)
    if hasattr(type_hint, "__name__") and type_hint.__name__ == "PluginSource":
        if (
            hasattr(type_hint, "__module__")
            and type_hint.__module__ == "music_assistant.models.plugin"
        ):
            return "PlayerSource"

    # Map Python types to JSON types
    type_name_mapping = {
        "str": "string",
        "int": "integer",
        "float": "number",
        "bool": "boolean",
        "dict": "object",
        "list": "array",
        "tuple": "array",
        "set": "array",
        "frozenset": "array",
        "Sequence": "array",
        "UniqueList": "array",
        "None": "null",
    }

    if hasattr(type_hint, "__name__"):
        type_name = str(type_hint.__name__)
        return type_name_mapping.get(type_name, type_name)

    type_str = str(type_hint).replace("NoneType", "null")
    # Replace Python types with JSON types in complex type strings
    for python_type, json_type in type_name_mapping.items():
        type_str = type_str.replace(python_type, json_type)
    return type_str


def _generate_type_alias_description(type_alias: Any, alias_name: str) -> str:
    """Generate a human-readable description of a type alias from its definition.

    :param type_alias: The type alias to describe (e.g., ConfigValueType)
    :param alias_name: The name of the alias for display
    :return: A human-readable description string
    """
    # Get the union args
    args = get_args(type_alias)
    if not args:
        return f"Type alias for {alias_name}."

    # Convert each type to a readable name
    type_names = []
    for arg in args:
        origin = get_origin(arg)
        if origin in (list, tuple):
            # Handle list types
            inner_args = get_args(arg)
            if inner_args:
                inner_type = inner_args[0]
                if inner_type is bool:
                    type_names.append("array of boolean")
                elif inner_type is int:
                    type_names.append("array of integer")
                elif inner_type is float:
                    type_names.append("array of number")
                elif inner_type is str:
                    type_names.append("array of string")
                else:
                    type_names.append(
                        f"array of {getattr(inner_type, '__name__', str(inner_type))}"
                    )
            else:
                type_names.append("array")
        elif arg is type(None) or arg is NoneType:
            type_names.append("null")
        elif arg is bool:
            type_names.append("boolean")
        elif arg is int:
            type_names.append("integer")
        elif arg is float:
            type_names.append("number")
        elif arg is str:
            type_names.append("string")
        elif hasattr(arg, "__name__"):
            type_names.append(arg.__name__)
        else:
            type_names.append(str(arg))

    # Format the list nicely
    if len(type_names) == 1:
        types_str = type_names[0]
    elif len(type_names) == 2:
        types_str = f"{type_names[0]} or {type_names[1]}"
    else:
        types_str = f"{', '.join(type_names[:-1])}, or {type_names[-1]}"

    return f"Type alias for {alias_name.lower()} types. Can be {types_str}."


def _get_type_schema(  # noqa: PLR0911, PLR0915
    type_hint: Any, definitions: dict[str, Any]
) -> dict[str, Any]:
    """Convert a Python type hint to an OpenAPI schema."""
    # Check if type_hint matches a type alias that was expanded by get_type_hints()
    # Import type aliases to compare against
    from music_assistant_models.config_entries import (  # noqa: PLC0415
        ConfigValueType as config_value_type,  # noqa: N813
    )
    from music_assistant_models.media_items import (  # noqa: PLC0415
        MediaItemType as media_item_type,  # noqa: N813
    )

    if type_hint == config_value_type:
        # This is the expanded ConfigValueType, treat it as the type alias
        return _get_type_schema("ConfigValueType", definitions)
    if type_hint == media_item_type:
        # This is the expanded MediaItemType, treat it as the type alias
        return _get_type_schema("MediaItemType", definitions)

    # Handle string type hints from __future__ annotations
    if isinstance(type_hint, str):
        # Handle simple primitive type names
        if type_hint in ("str", "string"):
            return {"type": "string"}
        if type_hint in ("int", "integer"):
            return {"type": "integer"}
        if type_hint in ("float", "number"):
            return {"type": "number"}
        if type_hint in ("bool", "boolean"):
            return {"type": "boolean"}

        # Special handling for type aliases - create proper schema definitions
        if type_hint == "ConfigValueType":
            if "ConfigValueType" not in definitions:
                from music_assistant_models.config_entries import (  # noqa: PLC0415
                    ConfigValueType as config_value_type,  # noqa: N813
                )

                # Dynamically create oneOf schema with description from the actual type
                cvt_args = get_args(config_value_type)
                definitions["ConfigValueType"] = {
                    "description": _generate_type_alias_description(
                        config_value_type, "configuration value"
                    ),
                    "oneOf": [_get_type_schema(arg, definitions) for arg in cvt_args],
                }
            return {"$ref": "#/components/schemas/ConfigValueType"}

        if type_hint == "MediaItemType":
            if "MediaItemType" not in definitions:
                from music_assistant_models.media_items import (  # noqa: PLC0415
                    MediaItemType as media_item_type,  # noqa: N813
                )

                # Dynamically create oneOf schema with description from the actual type
                mit_origin = get_origin(media_item_type)
                if mit_origin in (Union, UnionType):
                    mit_args = get_args(media_item_type)
                    definitions["MediaItemType"] = {
                        "description": _generate_type_alias_description(
                            media_item_type, "media item"
                        ),
                        "oneOf": [_get_type_schema(arg, definitions) for arg in mit_args],
                    }
                else:
                    definitions["MediaItemType"] = _get_type_schema(media_item_type, definitions)
            return {"$ref": "#/components/schemas/MediaItemType"}

        # Handle PluginSource - replace with PlayerSource (parent type)
        if type_hint == "PluginSource":
            return _get_type_schema("PlayerSource", definitions)

        # Check if it looks like a simple class name (no special chars, starts with uppercase)
        # Examples: "PlayerType", "DeviceInfo", "PlaybackState"
        # Exclude generic types like "Any", "Union", "Optional", etc.
        excluded_types = {"Any", "Union", "Optional", "List", "Dict", "Tuple", "Set"}
        if type_hint.isidentifier() and type_hint[0].isupper() and type_hint not in excluded_types:
            # Create a schema reference for this type
            if type_hint not in definitions:
                definitions[type_hint] = {"type": "object"}
            return {"$ref": f"#/components/schemas/{type_hint}"}

        # If it's "Any", return generic object without creating a schema
        if type_hint == "Any":
            return {"type": "object"}

        # For complex type expressions like "str | None", "list[str]", return generic object
        return {"type": "object"}

    # Handle None type
    if type_hint is NoneType or type_hint is type(None):
        return {"type": "null"}

    # Handle internal Player model - replace with external PlayerState
    if hasattr(type_hint, "__name__") and type_hint.__name__ == "Player":
        # Check if this is the internal Player (from music_assistant.models.player)
        if (
            hasattr(type_hint, "__module__")
            and type_hint.__module__ == "music_assistant.models.player"
        ):
            # Replace with PlayerState from music_assistant_models
            return _get_type_schema(PlayerState, definitions)

    # Handle PluginSource - replace with PlayerSource (parent type)
    if hasattr(type_hint, "__name__") and type_hint.__name__ == "PluginSource":
        # Check if this is PluginSource from music_assistant.models.plugin
        if (
            hasattr(type_hint, "__module__")
            and type_hint.__module__ == "music_assistant.models.plugin"
        ):
            # Replace with PlayerSource from music_assistant.models.player
            from music_assistant.models.player import PlayerSource  # noqa: PLC0415

            return _get_type_schema(PlayerSource, definitions)

    # Handle Union types (including Optional)
    origin = get_origin(type_hint)
    if origin is Union or origin is UnionType:
        args = get_args(type_hint)
        # Check if it's Optional (Union with None)
        non_none_args = [arg for arg in args if arg not in (NoneType, type(None))]
        if (len(non_none_args) == 1 and NoneType in args) or type(None) in args:
            # It's Optional[T], make it nullable
            schema = _get_type_schema(non_none_args[0], definitions)
            schema["nullable"] = True
            return schema
        # It's a union of multiple types
        return {"oneOf": [_get_type_schema(arg, definitions) for arg in args]}

    # Handle UniqueList (treat as array)
    if hasattr(type_hint, "__name__") and type_hint.__name__ == "UniqueList":
        args = get_args(type_hint)
        if args:
            return {"type": "array", "items": _get_type_schema(args[0], definitions)}
        return {"type": "array", "items": {}}

    # Handle Sequence types (from collections.abc or typing)
    if origin is collections.abc.Sequence or (
        hasattr(origin, "__name__") and origin.__name__ == "Sequence"
    ):
        args = get_args(type_hint)
        if args:
            return {"type": "array", "items": _get_type_schema(args[0], definitions)}
        return {"type": "array", "items": {}}

    # Handle set/frozenset types
    if origin in (set, frozenset):
        args = get_args(type_hint)
        if args:
            return {"type": "array", "items": _get_type_schema(args[0], definitions)}
        return {"type": "array", "items": {}}

    # Handle list/tuple types
    if origin in (list, tuple):
        args = get_args(type_hint)
        if args:
            return {"type": "array", "items": _get_type_schema(args[0], definitions)}
        return {"type": "array", "items": {}}

    # Handle dict types
    if origin is dict:
        args = get_args(type_hint)
        if len(args) == 2:
            return {
                "type": "object",
                "additionalProperties": _get_type_schema(args[1], definitions),
            }
        return {"type": "object", "additionalProperties": True}

    # Handle Enum types - add them to definitions as explorable objects
    if inspect.isclass(type_hint) and issubclass(type_hint, Enum):
        enum_name = type_hint.__name__
        if enum_name not in definitions:
            enum_values = [item.value for item in type_hint]
            enum_type = type(enum_values[0]).__name__ if enum_values else "string"
            openapi_type = {
                "str": "string",
                "int": "integer",
                "float": "number",
                "bool": "boolean",
            }.get(enum_type, "string")

            # Create a detailed enum definition with descriptions
            enum_values_str = ", ".join(str(v) for v in enum_values)
            definitions[enum_name] = {
                "type": openapi_type,
                "enum": enum_values,
                "description": f"Enum: {enum_name}. Possible values: {enum_values_str}",
            }
        return {"$ref": f"#/components/schemas/{enum_name}"}

    # Handle datetime
    if type_hint is datetime:
        return {"type": "string", "format": "date-time"}

    # Handle primitive types - check both exact type and type name
    if type_hint is str or (hasattr(type_hint, "__name__") and type_hint.__name__ == "str"):
        return {"type": "string"}
    if type_hint is int or (hasattr(type_hint, "__name__") and type_hint.__name__ == "int"):
        return {"type": "integer"}
    if type_hint is float or (hasattr(type_hint, "__name__") and type_hint.__name__ == "float"):
        return {"type": "number"}
    if type_hint is bool or (hasattr(type_hint, "__name__") and type_hint.__name__ == "bool"):
        return {"type": "boolean"}

    # Handle complex types (dataclasses, models)
    # Check for __annotations__ or if it's a class (not already handled above)
    if hasattr(type_hint, "__annotations__") or (
        inspect.isclass(type_hint) and not issubclass(type_hint, (str, int, float, bool, Enum))
    ):
        type_name = getattr(type_hint, "__name__", str(type_hint))
        # Add to definitions if not already there
        if type_name not in definitions:
            properties = {}
            required = []

            # Check if this is a dataclass with fields
            if hasattr(type_hint, "__dataclass_fields__"):
                # Resolve type hints to handle forward references from __future__ annotations
                try:
                    resolved_hints = get_type_hints(type_hint)
                except Exception:
                    resolved_hints = {}

                # Use dataclass fields to get proper info including defaults and metadata
                for field_name, field_info in type_hint.__dataclass_fields__.items():
                    # Skip fields marked with serialize="omit" in metadata
                    if field_info.metadata:
                        # Check for mashumaro field_options
                        if "serialize" in field_info.metadata:
                            if field_info.metadata["serialize"] == "omit":
                                continue

                    # Use resolved type hint if available, otherwise fall back to field type
                    field_type = resolved_hints.get(field_name, field_info.type)
                    field_schema = _get_type_schema(field_type, definitions)

                    # Add default value if present
                    if field_info.default is not MISSING:
                        field_schema["default"] = field_info.default
                    elif (
                        hasattr(field_info, "default_factory")
                        and field_info.default_factory is not MISSING
                    ):
                        # Has a default factory - don't add anything, just skip
                        pass

                    properties[field_name] = field_schema

                    # Check if field is required (not Optional and no default)
                    has_default = field_info.default is not MISSING or (
                        hasattr(field_info, "default_factory")
                        and field_info.default_factory is not MISSING
                    )
                    is_optional = get_origin(field_type) in (
                        Union,
                        UnionType,
                    ) and NoneType in get_args(field_type)
                    if not has_default and not is_optional:
                        required.append(field_name)
            elif hasattr(type_hint, "__annotations__"):
                # Fallback for non-dataclass types with annotations
                for field_name, field_type in type_hint.__annotations__.items():
                    properties[field_name] = _get_type_schema(field_type, definitions)
                    # Check if field is required (not Optional)
                    if not (
                        get_origin(field_type) in (Union, UnionType)
                        and NoneType in get_args(field_type)
                    ):
                        required.append(field_name)
            else:
                # Class without dataclass fields or annotations - treat as generic object
                pass  # Will create empty properties

            definitions[type_name] = {
                "type": "object",
                "properties": properties,
            }
            if required:
                definitions[type_name]["required"] = required

        return {"$ref": f"#/components/schemas/{type_name}"}

    # Handle Any
    if type_hint is Any:
        return {"type": "object"}

    # Fallback - for types we don't recognize, at least return a generic object type
    return {"type": "object"}


def _parse_docstring(  # noqa: PLR0915
    func: Callable[..., Any],
) -> tuple[str, str, dict[str, str]]:
    """Parse docstring to extract summary, description and parameter descriptions.

    Returns:
        Tuple of (short_summary, full_description, param_descriptions)

    Handles multiple docstring formats:
    - reStructuredText (:param name: description)
    - Google style (Args: section)
    - NumPy style (Parameters section)
    """
    docstring = inspect.getdoc(func)
    if not docstring:
        return "", "", {}

    lines = docstring.split("\n")
    description_lines = []
    param_descriptions = {}
    current_section = "description"
    current_param = None

    for line in lines:
        stripped = line.strip()

        # Check for section headers
        if stripped.lower() in ("args:", "arguments:", "parameters:", "params:"):
            current_section = "params"
            current_param = None
            continue
        if stripped.lower() in (
            "returns:",
            "return:",
            "yields:",
            "raises:",
            "raises",
            "examples:",
            "example:",
            "note:",
            "notes:",
            "see also:",
            "warning:",
            "warnings:",
        ):
            current_section = "other"
            current_param = None
            continue

        # Parse :param style
        if stripped.startswith(":param "):
            current_section = "params"
            parts = stripped[7:].split(":", 1)
            if len(parts) == 2:
                current_param = parts[0].strip()
                desc = parts[1].strip()
                if desc:
                    param_descriptions[current_param] = desc
            continue

        if stripped.startswith((":type ", ":rtype", ":return")):
            current_section = "other"
            current_param = None
            continue

        # Detect bullet-style params even without explicit section header
        # Format: "- param_name: description"
        if stripped.startswith("- ") and ":" in stripped:
            # This is likely a bullet-style parameter
            current_section = "params"
            content = stripped[2:]  # Remove "- "
            parts = content.split(":", 1)
            param_name = parts[0].strip()
            desc_part = parts[1].strip() if len(parts) > 1 else ""
            if param_name and not param_name.startswith(("return", "yield", "raise")):
                current_param = param_name
                if desc_part:
                    param_descriptions[current_param] = desc_part
            continue

        # In params section, detect param lines (indented or starting with name)
        if current_section == "params" and stripped:
            # Google/NumPy style: "param_name: description" or "param_name (type): description"
            if ":" in stripped and not stripped.startswith(" "):
                # Likely a parameter definition
                if "(" in stripped and ")" in stripped:
                    # Format: param_name (type): description
                    param_part = stripped.split(":")[0]
                    param_name = param_part.split("(")[0].strip()
                    desc_part = ":".join(stripped.split(":")[1:]).strip()
                else:
                    # Format: param_name: description
                    parts = stripped.split(":", 1)
                    param_name = parts[0].strip()
                    desc_part = parts[1].strip() if len(parts) > 1 else ""

                if param_name and not param_name.startswith(("return", "yield", "raise")):
                    current_param = param_name
                    if desc_part:
                        param_descriptions[current_param] = desc_part
            elif current_param and stripped:
                # Continuation of previous parameter description
                param_descriptions[current_param] = (
                    param_descriptions.get(current_param, "") + " " + stripped
                ).strip()
            continue

        # Collect description lines (only before params/returns sections)
        if current_section == "description" and stripped:
            description_lines.append(stripped)
        elif current_section == "description" and not stripped and description_lines:
            # Empty line in description - keep it for paragraph breaks
            description_lines.append("")

    # Join description lines, removing excessive empty lines
    description = "\n".join(description_lines).strip()
    # Collapse multiple empty lines into one
    while "\n\n\n" in description:
        description = description.replace("\n\n\n", "\n\n")

    # Extract first sentence/line as summary
    summary = ""
    if description:
        # Get first line or first sentence (whichever is shorter)
        first_line = description.split("\n")[0]
        # Try to get first sentence (ending with .)
        summary = first_line.split(".")[0] + "." if "." in first_line else first_line

    return summary, description, param_descriptions


def generate_openapi_spec(
    command_handlers: dict[str, APICommandHandler],
    server_url: str = "http://localhost:8095",
    version: str = "1.0.0",
) -> dict[str, Any]:
    """Generate simplified OpenAPI 3.0 specification focusing on data models.

    This spec documents the single /api endpoint and all data models/schemas.
    For detailed command documentation, see the Commands Reference page.
    """
    definitions: dict[str, Any] = {}

    # Build all schemas from command handlers (this populates definitions)
    for handler in command_handlers.values():
        # Skip aliases - they are for backward compatibility only
        if handler.alias:
            continue
        # Build parameter schemas
        for param_name in handler.signature.parameters:
            if param_name == "self":
                continue
            # Skip return_type parameter (used only for type hints)
            if param_name == "return_type":
                continue
            param_type = handler.type_hints.get(param_name, Any)
            # Skip Any types as they don't provide useful schema information
            if param_type is not Any and str(param_type) != "typing.Any":
                _get_type_schema(param_type, definitions)

        # Build return type schema
        return_type = handler.type_hints.get("return", Any)
        # Skip Any types as they don't provide useful schema information
        if return_type is not Any and str(return_type) != "typing.Any":
            _get_type_schema(return_type, definitions)

    # Build a single /api endpoint with generic request/response
    paths = {
        "/api": {
            "post": {
                "summary": "Execute API command",
                "description": (
                    "Execute any Music Assistant API command.\n\n"
                    "See the **Commands Reference** page for a complete list of available "
                    "commands with examples."
                ),
                "operationId": "execute_command",
                "security": [{"bearerAuth": []}],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["command"],
                                "properties": {
                                    "command": {
                                        "type": "string",
                                        "description": (
                                            "The command to execute (e.g., 'players/all')"
                                        ),
                                        "example": "players/all",
                                    },
                                    "args": {
                                        "type": "object",
                                        "description": "Command arguments (varies by command)",
                                        "additionalProperties": True,
                                        "example": {},
                                    },
                                },
                            },
                            "examples": {
                                "get_players": {
                                    "summary": "Get all players",
                                    "value": {"command": "players/all", "args": {}},
                                },
                                "play_media": {
                                    "summary": "Play media on a player",
                                    "value": {
                                        "command": "players/cmd/play",
                                        "args": {"player_id": "player123"},
                                    },
                                },
                            },
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "Successful command execution",
                        "content": {
                            "application/json": {
                                "schema": {"description": "Command result (varies by command)"}
                            }
                        },
                    },
                    "400": {"description": "Bad request - invalid command or parameters"},
                    "401": {"description": "Unauthorized - authentication required"},
                    "403": {"description": "Forbidden - insufficient permissions"},
                    "500": {"description": "Internal server error"},
                },
            }
        },
        "/auth/login": {
            "post": {
                "summary": "Authenticate with credentials",
                "description": "Login with username and password to obtain an access token.",
                "operationId": "auth_login",
                "tags": ["Authentication"],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "provider_id": {
                                        "type": "string",
                                        "description": "Auth provider ID (defaults to 'builtin')",
                                        "example": "builtin",
                                    },
                                    "credentials": {
                                        "type": "object",
                                        "description": "Provider-specific credentials",
                                        "properties": {
                                            "username": {"type": "string"},
                                            "password": {"type": "string"},
                                        },
                                    },
                                },
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "Login successful",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "success": {"type": "boolean"},
                                        "token": {"type": "string"},
                                        "user": {"type": "object"},
                                    },
                                }
                            }
                        },
                    },
                    "400": {"description": "Invalid credentials"},
                },
            }
        },
        "/auth/providers": {
            "get": {
                "summary": "Get available auth providers",
                "description": "Returns list of configured authentication providers.",
                "operationId": "auth_providers",
                "tags": ["Authentication"],
                "responses": {
                    "200": {
                        "description": "List of auth providers",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "providers": {
                                            "type": "array",
                                            "items": {"type": "object"},
                                        }
                                    },
                                }
                            }
                        },
                    }
                },
            }
        },
        "/setup": {
            "post": {
                "summary": "Initial server setup",
                "description": (
                    "Handle initial setup of the Music Assistant server including creating "
                    "the first admin user. Only accessible when no users exist "
                    "(onboard_done=false)."
                ),
                "operationId": "setup",
                "tags": ["Server"],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["username", "password"],
                                "properties": {
                                    "username": {"type": "string"},
                                    "password": {"type": "string"},
                                    "display_name": {"type": "string"},
                                },
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "Setup completed successfully",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "success": {"type": "boolean"},
                                        "token": {"type": "string"},
                                        "user": {"type": "object"},
                                    },
                                }
                            }
                        },
                    },
                    "400": {"description": "Setup already completed or invalid request"},
                },
            }
        },
        "/info": {
            "get": {
                "summary": "Get server info",
                "description": (
                    "Returns server information including schema version and authentication status."
                ),
                "operationId": "get_info",
                "tags": ["Server"],
                "responses": {
                    "200": {
                        "description": "Server information",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "schema_version": {"type": "integer"},
                                        "server_version": {"type": "string"},
                                        "onboard_done": {"type": "boolean"},
                                        "homeassistant_addon": {"type": "boolean"},
                                    },
                                }
                            }
                        },
                    }
                },
            }
        },
    }

    # Build OpenAPI spec
    return {
        "openapi": "3.0.0",
        "info": {
            "title": "Music Assistant API",
            "version": version,
            "description": (
                "Music Assistant API provides control over your music library, "
                "players, and playback.\n\n"
                "This specification documents the API structure and data models. "
                "For a complete list of available commands with examples, "
                "see the Commands Reference page."
            ),
            "contact": {
                "name": "Music Assistant",
                "url": "https://music-assistant.io",
            },
        },
        "servers": [{"url": server_url, "description": "Music Assistant Server"}],
        "paths": paths,
        "components": {
            "schemas": definitions,
            "securitySchemes": {
                "bearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "description": "Access token obtained from /auth/login or /auth/setup",
                }
            },
        },
    }


def _split_union_type(type_str: str) -> list[str]:
    """Split a union type on | but respect brackets and parentheses.

    This ensures that list[A | B] and (A | B) are not split at the inner |.
    """
    parts = []
    current_part = ""
    bracket_depth = 0
    paren_depth = 0
    i = 0
    while i < len(type_str):
        char = type_str[i]
        if char == "[":
            bracket_depth += 1
            current_part += char
        elif char == "]":
            bracket_depth -= 1
            current_part += char
        elif char == "(":
            paren_depth += 1
            current_part += char
        elif char == ")":
            paren_depth -= 1
            current_part += char
        elif char == "|" and bracket_depth == 0 and paren_depth == 0:
            # Check if this is a union separator (has space before and after)
            if (
                i > 0
                and i < len(type_str) - 1
                and type_str[i - 1] == " "
                and type_str[i + 1] == " "
            ):
                parts.append(current_part.strip())
                current_part = ""
                i += 1  # Skip the space after |, the loop will handle incrementing i
            else:
                current_part += char
        else:
            current_part += char
        i += 1
    if current_part.strip():
        parts.append(current_part.strip())
    return parts


def _extract_generic_inner_type(type_str: str) -> str | None:
    """Extract inner type from generic type like list[T] or dict[K, V].

    :param type_str: Type string like "list[str]" or "dict[str, int]"
    :return: Inner type string "str" or "str, int", or None if not a complete generic type
    """
    # Find the matching closing bracket
    bracket_count = 0
    start_idx = type_str.index("[") + 1
    end_idx = -1
    for i in range(start_idx, len(type_str)):
        if type_str[i] == "[":
            bracket_count += 1
        elif type_str[i] == "]":
            if bracket_count == 0:
                end_idx = i
                break
            bracket_count -= 1

    # Check if this is a complete generic type (ends with the closing bracket)
    if end_idx == len(type_str) - 1:
        return type_str[start_idx:end_idx].strip()
    return None


def _parse_dict_type_params(inner_type: str) -> tuple[str, str] | None:
    """Parse key and value types from dict inner type string.

    :param inner_type: The content inside dict[...], e.g., "str, ConfigValueType"
    :return: Tuple of (key_type, value_type) or None if parsing fails
    """
    # Split on comma to get key and value types
    # Need to be careful with nested types like dict[str, list[int]]
    parts = []
    current_part = ""
    bracket_depth = 0
    for char in inner_type:
        if char == "[":
            bracket_depth += 1
            current_part += char
        elif char == "]":
            bracket_depth -= 1
            current_part += char
        elif char == "," and bracket_depth == 0:
            parts.append(current_part.strip())
            current_part = ""
        else:
            current_part += char
    if current_part:
        parts.append(current_part.strip())

    if len(parts) == 2:
        return parts[0], parts[1]
    return None


def _python_type_to_json_type(type_str: str, _depth: int = 0) -> str:
    """Convert Python type string to JSON/JavaScript type string.

    Args:
        type_str: The type string to convert
        _depth: Internal recursion depth tracker (do not set manually)
    """
    import re  # noqa: PLC0415

    # Prevent infinite recursion
    if _depth > 50:
        return "any"

    # Remove typing module prefix and class markers
    type_str = type_str.replace("typing.", "").replace("<class '", "").replace("'>", "")

    # Remove module paths from type names (e.g., "music_assistant.models.Artist" -> "Artist")
    type_str = re.sub(r"[\w.]+\.(\w+)", r"\1", type_str)

    # Check for type aliases that should be preserved as-is
    # These will have schema definitions in the API docs
    if type_str in ("ConfigValueType", "MediaItemType"):
        return type_str

    # Map Python types to JSON types
    type_mappings = {
        "str": "string",
        "int": "integer",
        "float": "number",
        "bool": "boolean",
        "dict": "object",
        "Dict": "object",
        "list": "array",
        "tuple": "array",
        "Tuple": "array",
        "None": "null",
        "NoneType": "null",
    }

    # Check for List/list/UniqueList/tuple with type parameter BEFORE checking for union types
    # This is important because list[A | B] contains " | " but should be handled as a list first
    # codespell:ignore
    if type_str.startswith(("list[", "List[", "UniqueList[", "tuple[", "Tuple[")):
        inner_type = _extract_generic_inner_type(type_str)
        if inner_type:
            # Handle variable-length tuple (e.g., tuple[str, ...])
            # The ellipsis means "variable length of this type"
            if inner_type.endswith(", ..."):
                # Remove the ellipsis and just use the type
                inner_type = inner_type[:-5].strip()
            # Recursively convert the inner type
            inner_json_type = _python_type_to_json_type(inner_type, _depth + 1)
            # For list[A | B], wrap in parentheses to keep it as one unit
            # This prevents "Array of A | B" from being split into separate union parts
            if " | " in inner_json_type:
                return f"Array of ({inner_json_type})"
            return f"Array of {inner_json_type}"

    # Check for dict/Dict with type parameters BEFORE checking for union types
    # This is important because dict[str, A | B] contains " | "
    # but should be handled as a dict first
    # codespell:ignore
    if type_str.startswith(("dict[", "Dict[")):
        inner_type = _extract_generic_inner_type(type_str)
        if inner_type:
            parsed = _parse_dict_type_params(inner_type)
            if parsed:
                key_type_str, value_type_str = parsed
                key_type = _python_type_to_json_type(key_type_str, _depth + 1)
                value_type = _python_type_to_json_type(value_type_str, _depth + 1)
                # Use more descriptive format: "object with {key_type} keys and {value_type} values"
                return f"object with {key_type} keys and {value_type} values"

    # Handle Union types by splitting on | and recursively processing each part
    if " | " in type_str:
        # Use helper to split on | but respect brackets
        parts = _split_union_type(type_str)

        # Filter out None/null types (None, NoneType, null all mean JSON null)
        parts = [part for part in parts if part not in ("None", "NoneType", "null")]

        # If splitting didn't help (only one part or same as input), avoid infinite recursion
        if not parts or (len(parts) == 1 and parts[0] == type_str):
            # Can't split further, return as-is or "any"
            return type_str if parts else "any"

        if parts:
            converted_parts = [_python_type_to_json_type(part, _depth + 1) for part in parts]
            # Remove duplicates while preserving order
            seen = set()
            unique_parts = []
            for part in converted_parts:
                if part not in seen:
                    seen.add(part)
                    unique_parts.append(part)
            return " | ".join(unique_parts)
        return "any"

    # Check for Union/Optional types with brackets
    if "Union[" in type_str or "Optional[" in type_str:
        # Extract content from Union[...] or Optional[...]
        union_match = re.search(r"(?:Union|Optional)\[([^\]]+)\]", type_str)
        if union_match:
            inner = union_match.group(1)
            # Recursively process the union content
            return _python_type_to_json_type(inner, _depth + 1)

    # Direct mapping for basic types
    for py_type, json_type in type_mappings.items():
        if type_str == py_type:
            return json_type

    # Check if it's a complex type (starts with capital letter)
    complex_match = re.search(r"^([A-Z][a-zA-Z0-9_]*)$", type_str)
    if complex_match:
        return complex_match.group(1)

    # Default to the original string if no mapping found
    return type_str


def _make_type_links(type_str: str, server_url: str, as_list: bool = False) -> str:
    """Convert type string to HTML with links to schemas reference for complex types.

    Args:
        type_str: The type string to convert
        server_url: Base server URL for building links
        as_list: If True and type contains |, format as "Any of:" bullet list
    """
    import re  # noqa: PLC0415
    from re import Match  # noqa: PLC0415

    # Find all complex types (capitalized words that aren't basic types)
    def replace_type(match: Match[str]) -> str:
        type_name = match.group(0)
        # Check if it's a complex type (starts with capital letter)
        # Exclude basic types and "Array" (which is used in "Array of Type")
        excluded = {"Union", "Optional", "List", "Dict", "Array", "None", "NoneType"}
        if type_name[0].isupper() and type_name not in excluded:
            # Create link to our schemas reference page
            schema_url = f"{server_url}/api-docs/schemas#schema-{type_name}"
            return f'<a href="{schema_url}" class="type-link">{type_name}</a>'
        return type_name

    # If it's a union type with multiple options and as_list is True, format as bullet list
    if as_list and " | " in type_str:
        # Use the bracket/parenthesis-aware splitter
        parts = _split_union_type(type_str)
        # Only use list format if there are 3+ options
        if len(parts) >= 3:
            html = '<div class="type-union"><span class="type-union-label">Any of:</span><ul>'
            for part in parts:
                linked_part = re.sub(r"\b[A-Z][a-zA-Z0-9_]*\b", replace_type, part)
                html += f"<li>{linked_part}</li>"
            html += "</ul></div>"
            return html

    # Replace complex type names with links
    result: str = re.sub(r"\b[A-Z][a-zA-Z0-9_]*\b", replace_type, type_str)
    return result


def generate_commands_json(command_handlers: dict[str, APICommandHandler]) -> list[dict[str, Any]]:
    """Generate JSON representation of all available API commands.

    This is used by client libraries to sync their methods with the server API.

    Returns a list of command objects with the following structure:
    {
        "command": str,  # Command name (e.g., "music/tracks/library_items")
        "category": str,  # Category (e.g., "Music")
        "summary": str,  # Short description
        "description": str,  # Full description
        "parameters": [  # List of parameters
            {
                "name": str,
                "type": str,  # JSON type (string, integer, boolean, etc.)
                "required": bool,
                "description": str
            }
        ],
        "return_type": str,  # Return type
        "authenticated": bool,  # Whether authentication is required
        "required_role": str | None,  # Required user role (if any)
    }
    """
    commands_data = []

    for command, handler in sorted(command_handlers.items()):
        # Skip aliases - they are for backward compatibility only
        if handler.alias:
            continue
        # Parse docstring
        summary, description, param_descriptions = _parse_docstring(handler.target)

        # Get return type
        return_type = handler.type_hints.get("return", Any)
        # If type is already a string (e.g., "ConfigValueType"), use it directly
        return_type_str = _python_type_to_json_type(
            return_type if isinstance(return_type, str) else str(return_type)
        )

        # Extract category from command name
        category = command.split("/")[0] if "/" in command else "general"
        category_display = category.replace("_", " ").title()

        # Build parameters list
        parameters = []
        for param_name, param in handler.signature.parameters.items():
            if param_name in ("self", "return_type"):
                continue

            is_required = param.default is inspect.Parameter.empty
            param_type = handler.type_hints.get(param_name, Any)
            # If type is already a string (e.g., "ConfigValueType"), use it directly
            type_str = param_type if isinstance(param_type, str) else str(param_type)
            json_type_str = _python_type_to_json_type(type_str)
            param_desc = param_descriptions.get(param_name, "")

            parameters.append(
                {
                    "name": param_name,
                    "type": json_type_str,
                    "required": is_required,
                    "description": param_desc,
                }
            )

        commands_data.append(
            {
                "command": command,
                "category": category_display,
                "summary": summary or "",
                "description": description or "",
                "parameters": parameters,
                "return_type": return_type_str,
                "authenticated": handler.authenticated,
                "required_role": handler.required_role,
            }
        )

    return commands_data


def generate_commands_reference(  # noqa: PLR0915
    command_handlers: dict[str, APICommandHandler],
    server_url: str = "http://localhost:8095",
) -> str:
    """Generate HTML commands reference page with all available commands."""
    import json  # noqa: PLC0415

    # Group commands by category
    categories: dict[str, list[tuple[str, APICommandHandler]]] = {}
    for command, handler in sorted(command_handlers.items()):
        # Skip aliases - they are for backward compatibility only
        if handler.alias:
            continue
        category = command.split("/")[0] if "/" in command else "general"
        if category not in categories:
            categories[category] = []
        categories[category].append((command, handler))

    html = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Music Assistant API - Commands Reference</title>
    <style>
        :root {
            --fg: #000000;
            --background: #f5f5f5;
            --panel: #ffffff;
            --primary: #03a9f4;
            --text-secondary: rgba(0, 0, 0, 0.6);
            --border: rgba(0, 0, 0, 0.1);
            --success: #4caf50;
            --success-bg: rgba(76, 175, 80, 0.1);
            --success-border: rgba(76, 175, 80, 0.3);
        }

        @media (prefers-color-scheme: dark) {
            :root {
                --fg: #ffffff;
                --background: #181818;
                --panel: #232323;
                --text-secondary: rgba(255, 255, 255, 0.7);
                --border: rgba(255, 255, 255, 0.08);
                --success: #66bb6a;
                --success-bg: rgba(102, 187, 106, 0.15);
                --success-border: rgba(102, 187, 106, 0.4);
            }
        }

        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen,
                         Ubuntu, Cantarell, sans-serif;
            background: var(--background);
            color: var(--fg);
            line-height: 1.6;
        }
        .header {
            background: var(--panel);
            color: var(--fg);
            padding: 1.5rem 2rem;
            text-align: center;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            border-bottom: 1px solid var(--border);
        }
        .header h1 {
            font-size: 1.8em;
            margin-bottom: 0.3rem;
            font-weight: 600;
        }
        .header p {
            font-size: 0.95em;
            opacity: 0.9;
        }
        .nav-container {
            background: var(--panel);
            padding: 1rem 2rem;
            box-shadow: 0 2px 5px rgba(0,0,0,0.05);
            position: sticky;
            top: 0;
            z-index: 100;
            display: flex;
            flex-direction: column;
            gap: 1rem;
        }
        .search-box input {
            width: 100%;
            max-width: 600px;
            padding: 0.6rem 1rem;
            font-size: 0.95em;
            border: 2px solid var(--border);
            border-radius: 8px;
            display: block;
            margin: 0 auto;
        }
        .search-box input:focus {
            outline: none;
            border-color: var(--primary);
        }
        .quick-nav {
            display: flex;
            flex-wrap: wrap;
            gap: 0.5rem;
            justify-content: center;
            padding-top: 0.5rem;
            border-top: 1px solid var(--border);
        }
        .quick-nav a {
            padding: 0.4rem 1rem;
            background: var(--panel);
            color: var(--primary);
            text-decoration: none;
            border-radius: 6px;
            font-size: 0.9em;
            transition: all 0.2s;
        }
        .quick-nav a:hover {
            background: var(--primary);
            color: var(--fg);
        }
        .container {
            max-width: 1200px;
            margin: 2rem auto;
            padding: 0 2rem;
        }
        .category {
            background: var(--panel);
            margin-bottom: 2rem;
            border-radius: 12px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.08);
            overflow: hidden;
        }
        .category-header {
            background: var(--primary);
            color: var(--fg);
            padding: 1rem 1.5rem;
            font-size: 1.2em;
            font-weight: 600;
            cursor: pointer;
            user-select: none;
        }
        .category-header:hover {
            background: var(--primary);
        }
        .command {
            border-bottom: 1px solid var(--border);
        }
        .command:last-child {
            border-bottom: none;
        }
        .command-header {
            padding: 1rem 1.5rem;
            cursor: pointer;
            user-select: none;
            display: flex;
            justify-content: space-between;
            align-items: center;
            transition: background 0.2s;
        }
        .command-header:hover {
            background: var(--panel);
        }
        .command-title {
            display: flex;
            flex-direction: column;
            gap: 0.3rem;
            flex: 1;
        }
        .command-name {
            font-size: 1.1em;
            font-weight: 600;
            color: var(--primary);
            font-family: 'Monaco', 'Courier New', monospace;
        }
        .command-summary {
            font-size: 0.9em;
            color: var(--text-secondary);
        }
        .command-expand-icon {
            color: var(--primary);
            font-size: 1.2em;
            transition: transform 0.3s;
        }
        .command-expand-icon.expanded {
            transform: rotate(180deg);
        }
        .command-details {
            padding: 0 1.5rem 1.5rem 1.5rem;
            display: none;
        }
        .command-details.show {
            display: block;
        }
        .command-description {
            color: var(--text-secondary);
            margin-bottom: 1rem;
        }
        .return-type {
            background: #e8f5e9;
            padding: 0.5rem 1rem;
            margin: 1rem 0;
            border-radius: 6px;
            border-left: 3px solid #4caf50;
        }
        .return-type-label {
            font-weight: 600;
            color: #2e7d32;
            margin-right: 0.5rem;
        }
        .return-type-value {
            font-family: 'Monaco', 'Courier New', monospace;
            color: #2e7d32;
        }
        .params-section {
            margin: 1rem 0;
        }
        .params-title {
            font-weight: 600;
            color: #333;
            margin-bottom: 0.5rem;
        }
        .param {
            background: var(--panel);
            padding: 0.5rem 1rem;
            margin: 0.5rem 0;
            border-radius: 6px;
            border-left: 3px solid var(--primary);
        }
        .param-name {
            font-family: 'Monaco', 'Courier New', monospace;
            color: var(--primary);
            font-weight: 600;
        }
        .param-required {
            color: #e74c3c;
            font-size: 0.8em;
            font-weight: 600;
            margin-left: 0.5rem;
        }
        .param-type {
            color: var(--text-secondary);
            font-size: 0.9em;
            margin-left: 0.5rem;
        }
        .param-description {
            color: var(--text-secondary);
            margin-top: 0.25rem;
        }
        .example {
            background: #2d2d2d;
            color: #f8f8f2;
            padding: 1rem;
            border-radius: 8px;
            margin: 1rem 0;
            overflow-x: auto;
            position: relative;
        }
        .example-title {
            font-weight: 600;
            color: #333;
            margin-bottom: 0.5rem;
        }
        .example pre {
            margin: 0;
            font-family: 'Monaco', 'Courier New', monospace;
            font-size: 0.9em;
        }
        .copy-btn {
            position: absolute;
            top: 0.5rem;
            right: 0.5rem;
            background: var(--primary);
            color: var(--fg);
            border: none;
            padding: 0.4rem 0.8rem;
            border-radius: 4px;
            cursor: pointer;
            font-size: 0.8em;
        }
        .copy-btn:hover {
            background: var(--primary);
        }
        .hidden {
            display: none;
        }
        .tabs {
            margin: 1rem 0;
        }
        .tab-buttons {
            display: flex;
            gap: 0.5rem;
            border-bottom: 2px solid #ddd;
            margin-bottom: 1rem;
        }
        .tab-btn {
            background: none;
            border: none;
            padding: 0.8rem 1.5rem;
            font-size: 1em;
            cursor: pointer;
            color: var(--text-secondary);
            border-bottom: 3px solid transparent;
            transition: all 0.3s;
        }
        .tab-btn:hover {
            color: var(--primary);
        }
        .tab-btn.active {
            color: var(--primary);
            border-bottom-color: var(--primary);
        }
        .tab-content {
            display: none;
        }
        .tab-content.active {
            display: block;
        }
        .try-it-section {
            display: flex;
            flex-direction: column;
            gap: 1rem;
        }
        .json-input {
            width: 100%;
            min-height: 150px;
            padding: 1rem;
            font-family: 'Monaco', 'Courier New', monospace;
            font-size: 0.9em;
            border: 2px solid var(--border);
            border-radius: 8px;
            background: #2d2d2d;
            color: #f8f8f2;
            resize: vertical;
        }
        .json-input:focus {
            outline: none;
            border-color: var(--primary);
        }
        .try-btn {
            align-self: flex-start;
            background: var(--primary);
            color: var(--fg);
            border: none;
            padding: 0.8rem 2rem;
            border-radius: 8px;
            font-size: 1em;
            cursor: pointer;
            transition: background 0.3s;
        }
        .try-btn:hover {
            background: var(--primary);
        }
        .try-btn:disabled {
            background: #ccc;
            cursor: not-allowed;
        }
        .response-output {
            background: #2d2d2d;
            color: #f8f8f2;
            padding: 1rem;
            border-radius: 8px;
            font-family: 'Monaco', 'Courier New', monospace;
            font-size: 0.9em;
            min-height: 100px;
            white-space: pre-wrap;
            word-wrap: break-word;
            display: none;
        }
        .response-output.show {
            display: block;
        }
        .response-output.error {
            background: #ffebee;
            color: #c62828;
        }
        .response-output.success {
            background: #e8f5e9;
            color: #2e7d32;
        }
        .type-link {
            color: var(--primary);
            text-decoration: none;
            border-bottom: 1px dashed var(--primary);
            transition: all 0.2s;
        }
        .type-link:hover {
            color: var(--primary);
            border-bottom-color: var(--primary);
        }
        .type-union {
            margin-top: 0.5rem;
        }
        .type-union-label {
            font-weight: 600;
            color: #4a5568;
            display: block;
            margin-bottom: 0.25rem;
        }
        .type-union ul {
            margin: 0.25rem 0 0 0;
            padding-left: 1.5rem;
            list-style-type: disc;
        }
        .type-union li {
            margin: 0.25rem 0;
            color: #2d3748;
        }
        .param-type-union {
            display: block;
            margin-top: 0.25rem;
        }
        .auth-section {
            background: var(--panel);
            padding: 1rem;
            border-radius: 8px;
            margin-bottom: 1rem;
            border: 2px solid var(--border);
        }
        .auth-section.authenticated {
            border-color: var(--success-border);
            background: var(--success-bg);
        }
        .auth-status {
            display: flex;
            align-items: center;
            gap: 0.5rem;
            margin-bottom: 0.8rem;
            font-weight: 600;
            color: var(--fg);
        }
        .auth-status-dot {
            width: 10px;
            height: 10px;
            border-radius: 50%;
            background: #f44336;
        }
        .auth-status-dot.authenticated {
            background: var(--success);
        }
        .auth-form {
            display: flex;
            flex-direction: column;
            gap: 0.8rem;
        }
        .auth-form input {
            padding: 0.6rem;
            border: 2px solid var(--border);
            border-radius: 6px;
            font-size: 0.95em;
            background: var(--panel);
            color: var(--fg);
        }
        .auth-form input:focus {
            outline: none;
            border-color: var(--primary);
        }
        .auth-form button {
            padding: 0.6rem 1.2rem;
            background: var(--primary);
            color: white;
            border: none;
            border-radius: 6px;
            font-size: 0.95em;
            cursor: pointer;
            transition: background 0.3s;
        }
        .auth-form button:hover {
            filter: brightness(1.1);
        }
        .auth-form button:disabled {
            background: #ccc;
            cursor: not-allowed;
        }
        .auth-user-info {
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .auth-user-details {
            font-size: 0.9em;
            color: var(--text-secondary);
        }
        .auth-logout-btn {
            padding: 0.5rem 1rem;
            background: #f44336;
            color: white;
            border: none;
            border-radius: 6px;
            font-size: 0.9em;
            cursor: pointer;
            transition: background 0.3s;
        }
        .auth-logout-btn:hover {
            filter: brightness(0.9);
        }
        .auth-error {
            background: #ffebee;
            color: #c62828;
            padding: 0.6rem;
            border-radius: 6px;
            font-size: 0.9em;
            margin-top: 0.5rem;
        }
        .role-badge {
            display: inline-block;
            padding: 0.2rem 0.6rem;
            border-radius: 4px;
            font-size: 0.75em;
            font-weight: 600;
            margin-left: 0.5rem;
            text-transform: uppercase;
        }
        .role-badge.admin {
            background: #ffebee;
            color: #c62828;
        }
        .role-badge.user {
            background: #e3f2fd;
            color: #1976d2;
        }
        .header .logo {
            margin-bottom: 1rem;
        }
        .header .logo img {
            width: 60px;
            height: 60px;
            object-fit: contain;
        }
    </style>
</head>
<body>
    <div class="header">
        <div class="logo">
            <img src="../logo.png" alt="Music Assistant">
        </div>
        <h1>Commands Reference</h1>
        <p>Complete list of Music Assistant API commands</p>
    </div>

    <div class="nav-container">
        <div class="auth-section" id="authSection">
            <div class="auth-status">
                <span class="auth-status-dot" id="authDot"></span>
                <span id="authStatusText">Not Authenticated</span>
            </div>
            <div id="authFormContainer">
                <form class="auth-form" id="loginForm" onsubmit="return handleLogin(event)">
                    <input type="text" id="username" placeholder="Username" required />
                    <input type="password" id="password" placeholder="Password" required />
                    <button type="submit" id="loginBtn">Login</button>
                </form>
                <div id="authError" class="auth-error" style="display: none;"></div>
            </div>
            <div id="authUserInfo" class="auth-user-info" style="display: none;">
                <div class="auth-user-details">
                    <div>Logged in as: <strong id="authUsername"></strong></div>
                    <div>Role: <strong id="authRole"></strong></div>
                </div>
                <button class="auth-logout-btn" onclick="handleLogout()">Logout</button>
            </div>
        </div>
        <div class="search-box">
            <input type="text" id="search" placeholder="Search commands..." />
        </div>
        <div class="quick-nav">
"""

    # Add quick navigation links
    for category in sorted(categories.keys()):
        category_display = category.replace("_", " ").title()
        html += f'            <a href="#{category}">{category_display}</a>\n'

    html += """        </div>
    </div>

    <div class="container">
"""

    for category, commands in sorted(categories.items()):
        category_display = category.replace("_", " ").title()
        html += f'        <div class="category" id="{category}" data-category="{category}">\n'
        html += f'            <div class="category-header">{category_display}</div>\n'
        html += '            <div class="category-content">\n'

        for command, handler in commands:
            # Parse docstring
            summary, description, param_descriptions = _parse_docstring(handler.target)

            # Get return type
            return_type = handler.type_hints.get("return", Any)
            # If type is already a string (e.g., "ConfigValueType"), use it directly
            return_type_str = _python_type_to_json_type(
                return_type if isinstance(return_type, str) else str(return_type)
            )

            html += f'                <div class="command" data-command="{command}">\n'
            html += (
                '                    <div class="command-header" onclick="toggleCommand(this)">\n'
            )
            html += '                        <div class="command-title">\n'
            html += f'                            <div class="command-name">{command}'

            # Add role badge if required_role is set
            if handler.required_role:
                role_class = handler.required_role.lower()
                html += f'<span class="role-badge {role_class}">{handler.required_role}</span>'

            html += "</div>\n"
            if summary:
                summary_escaped = summary.replace("<", "&lt;").replace(">", "&gt;")
                html += (
                    f'                            <div class="command-summary">'
                    f"{summary_escaped}</div>\n"
                )
            html += "                        </div>\n"
            html += '                        <div class="command-expand-icon">▼</div>\n'
            html += "                    </div>\n"

            # Command details (collapsed by default)
            html += '                    <div class="command-details">\n'

            if description and description != summary:
                desc_escaped = description.replace("<", "&lt;").replace(">", "&gt;")
                html += (
                    f'                        <div class="command-description">'
                    f"{desc_escaped}</div>\n"
                )

            # Return type with links
            return_type_html = _make_type_links(return_type_str, server_url)
            html += '                        <div class="return-type">\n'
            html += '                            <span class="return-type-label">Returns:</span>\n'
            html += f'                            <span class="return-type-value">{return_type_html}</span>\n'  # noqa: E501
            html += "                        </div>\n"

            # Parameters
            params = []
            for param_name, param in handler.signature.parameters.items():
                if param_name == "self":
                    continue
                # Skip return_type parameter (used only for type hints)
                if param_name == "return_type":
                    continue
                is_required = param.default is inspect.Parameter.empty
                param_type = handler.type_hints.get(param_name, Any)
                # If type is already a string (e.g., "ConfigValueType"), use it directly
                type_str = param_type if isinstance(param_type, str) else str(param_type)
                json_type_str = _python_type_to_json_type(type_str)
                param_desc = param_descriptions.get(param_name, "")
                params.append((param_name, is_required, json_type_str, param_desc))

            if params:
                html += '                    <div class="params-section">\n'
                html += '                        <div class="params-title">Parameters:</div>\n'
                for param_name, is_required, type_str, param_desc in params:
                    # Convert type to HTML with links (use list format for unions)
                    type_html = _make_type_links(type_str, server_url, as_list=True)
                    html += '                        <div class="param">\n'
                    html += (
                        f'                            <span class="param-name">'
                        f"{param_name}</span>\n"
                    )
                    if is_required:
                        html += (
                            '                            <span class="param-required">'
                            "REQUIRED</span>\n"
                        )
                    # If it's a list format, display it differently
                    if "<ul>" in type_html:
                        html += (
                            '                            <div class="param-type-union">'
                            f"{type_html}</div>\n"
                        )
                    else:
                        html += (
                            f'                            <span class="param-type">'
                            f"{type_html}</span>\n"
                        )
                    if param_desc:
                        html += (
                            f'                            <div class="param-description">'
                            f"{param_desc}</div>\n"
                        )
                    html += "                        </div>\n"
                html += "                    </div>\n"

            # Build example curl command with JSON types
            example_args: dict[str, Any] = {}
            for param_name, is_required, type_str, _ in params:
                # Include optional params if few params
                if is_required or len(params) <= 2:
                    if type_str == "string":
                        example_args[param_name] = "example_value"
                    elif type_str == "integer":
                        example_args[param_name] = 0
                    elif type_str == "number":
                        example_args[param_name] = 0.0
                    elif type_str == "boolean":
                        example_args[param_name] = True
                    elif type_str == "object":
                        example_args[param_name] = {}
                    elif type_str == "null":
                        example_args[param_name] = None
                    elif type_str == "ConfigValueType":
                        # Type alias for config values - use string as example
                        example_args[param_name] = "example_value"
                    elif type_str == "MediaItemType":
                        # Type alias for media items - use placeholder object
                        example_args[param_name] = {
                            "_comment": "See MediaItemType schema for details"
                        }
                    elif type_str.startswith("Array of "):
                        # Array type with item type specified (e.g., "Array of Artist")
                        item_type = type_str[9:]  # Remove "Array of "
                        if item_type in {"string", "integer", "number", "boolean"}:
                            example_args[param_name] = []
                        else:
                            # Complex type array
                            example_args[param_name] = [
                                {"_comment": f"See {item_type} schema in Swagger UI"}
                            ]
                    else:
                        # Complex type (Artist, Player, etc.) - use placeholder object
                        # Extract the primary type if it's a union (e.g., "Artist | string")
                        primary_type = type_str.split(" | ")[0] if " | " in type_str else type_str
                        example_args[param_name] = {
                            "_comment": f"See {primary_type} schema in Swagger UI"
                        }

            request_body: dict[str, Any] = {"command": command}
            if example_args:
                request_body["args"] = example_args

            # Build cURL command with auth header if required
            curl_headers = '  -H "Content-Type: application/json"'
            if handler.authenticated:
                curl_headers += ' \\\n  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"'

            curl_cmd = (
                f"curl -X POST {server_url}/api \\\n"
                f"{curl_headers} \\\n"
                f"  -d '{json.dumps(request_body, indent=2)}'"
            )

            # Add tabs for curl example and try it
            html += '                    <div class="tabs">\n'
            html += '                        <div class="tab-buttons">\n'
            html += (
                '                            <button class="tab-btn active" '
                f"onclick=\"switchTab(this, 'curl-{command.replace('/', '-')}')\">cURL</button>\n"
            )
            html += (
                '                            <button class="tab-btn" '
                f"onclick=\"switchTab(this, 'tryit-{command.replace('/', '-')}')\">Try It</button>\n"  # noqa: E501
            )
            html += "                        </div>\n"

            # cURL tab
            html += f'                        <div id="curl-{command.replace("/", "-")}" class="tab-content active">\n'  # noqa: E501
            html += '                            <div class="example">\n'
            html += (
                '                                <button class="copy-btn" '
                'onclick="copyCode(this)">Copy</button>\n'
            )
            html += f"                                <pre>{curl_cmd}</pre>\n"
            html += "                            </div>\n"
            html += "                        </div>\n"

            # Try It tab
            html += f'                        <div id="tryit-{command.replace("/", "-")}" class="tab-content">\n'  # noqa: E501
            html += '                            <div class="try-it-section">\n'
            # HTML-escape the JSON for the textarea
            json_str = json.dumps(request_body, indent=2)
            # Escape HTML entities
            json_str_escaped = (
                json_str.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "&quot;")
                .replace("'", "&#39;")
            )
            html += f'                                <textarea class="json-input">{json_str_escaped}</textarea>\n'  # noqa: E501
            html += (
                f'                                <button class="try-btn" '
                f"onclick=\"tryCommand(this, '{command}')\">Execute</button>\n"
            )
            html += '                                <div class="response-output"></div>\n'
            html += "                            </div>\n"
            html += "                        </div>\n"

            html += "                    </div>\n"
            # Close command-details div
            html += "                    </div>\n"
            # Close command div
            html += "                </div>\n"

        html += "            </div>\n"
        html += "        </div>\n"

    html += """    </div>

    <script>
        // Authentication functionality
        const TOKEN_STORAGE_KEY = 'ma_api_token';
        const USER_STORAGE_KEY = 'ma_api_user';

        // Check for existing token on page load
        async function checkAuth() {
            const token = localStorage.getItem(TOKEN_STORAGE_KEY);
            const userStr = localStorage.getItem(USER_STORAGE_KEY);

            if (token && userStr) {
                try {
                    const user = JSON.parse(userStr);

                    // Validate token by making a JSON-RPC call that requires auth
                    const response = await fetch('/api', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                            'Authorization': `Bearer ${token}`
                        },
                        body: JSON.stringify({
                            command: 'info'
                        })
                    });

                    if (response.ok) {
                        // Token is valid
                        updateAuthUI(true, user);
                    } else {
                        // Token is invalid (revoked, expired, etc.)
                        clearAuth();
                    }
                } catch (e) {
                    // Network error or invalid JSON
                    clearAuth();
                }
            }
        }

        // Handle login form submission
        async function handleLogin(event) {
            event.preventDefault();

            const username = document.getElementById('username').value;
            const password = document.getElementById('password').value;
            const loginBtn = document.getElementById('loginBtn');
            const errorDiv = document.getElementById('authError');

            // Disable button and show loading
            loginBtn.disabled = true;
            loginBtn.textContent = 'Logging in...';
            errorDiv.style.display = 'none';

            try {
                const response = await fetch('/auth/login', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({
                        credentials: {
                            username: username,
                            password: password
                        }
                    })
                });

                const result = await response.json();

                if (result.success && result.token && result.user) {
                    // Store token and user info
                    localStorage.setItem(TOKEN_STORAGE_KEY, result.token);
                    localStorage.setItem(USER_STORAGE_KEY, JSON.stringify(result.user));

                    // Update UI
                    updateAuthUI(true, result.user);

                    // Clear form
                    document.getElementById('loginForm').reset();
                } else {
                    // Show error
                    errorDiv.textContent = result.error || 'Login failed';
                    errorDiv.style.display = 'block';
                }
            } catch (error) {
                errorDiv.textContent = 'Connection error: ' + error.message;
                errorDiv.style.display = 'block';
            } finally {
                loginBtn.disabled = false;
                loginBtn.textContent = 'Login';
            }

            return false;
        }

        // Handle logout
        function handleLogout() {
            clearAuth();
        }

        // Clear authentication
        function clearAuth() {
            localStorage.removeItem(TOKEN_STORAGE_KEY);
            localStorage.removeItem(USER_STORAGE_KEY);
            updateAuthUI(false, null);
        }

        // Update auth UI
        function updateAuthUI(authenticated, user) {
            const authSection = document.getElementById('authSection');
            const authDot = document.getElementById('authDot');
            const authStatusText = document.getElementById('authStatusText');
            const authFormContainer = document.getElementById('authFormContainer');
            const authUserInfo = document.getElementById('authUserInfo');

            if (authenticated && user) {
                authSection.classList.add('authenticated');
                authDot.classList.add('authenticated');
                authStatusText.textContent = 'Authenticated';
                authFormContainer.style.display = 'none';
                authUserInfo.style.display = 'flex';

                document.getElementById('authUsername').textContent = user.username;
                document.getElementById('authRole').textContent = user.role;

                // Update cURL examples with actual token
                updateCurlExamples();
            } else {
                authSection.classList.remove('authenticated');
                authDot.classList.remove('authenticated');
                authStatusText.textContent = 'Not Authenticated';
                authFormContainer.style.display = 'block';
                authUserInfo.style.display = 'none';

                // Reset cURL examples to placeholder
                updateCurlExamples();
            }
        }

        // Update all cURL examples with actual token or placeholder
        function updateCurlExamples() {
            const token = localStorage.getItem(TOKEN_STORAGE_KEY);
            const curlBlocks = document.querySelectorAll('.example pre');

            curlBlocks.forEach(block => {
                const curlText = block.textContent;

                // Only update if it contains an Authorization header
                if (curlText.includes('Authorization: Bearer')) {
                    if (token) {
                        // Replace placeholder with actual token
                        block.textContent = curlText.replace(
                            /Authorization: Bearer YOUR_ACCESS_TOKEN/g,
                            `Authorization: Bearer ${token}`
                        );
                    } else {
                        // Replace actual token with placeholder
                        block.textContent = curlText.replace(
                            /Authorization: Bearer \\S+/g,
                            'Authorization: Bearer YOUR_ACCESS_TOKEN'
                        );
                    }
                }
            });
        }

        // Initialize auth on page load
        checkAuth();

        // Search functionality
        document.getElementById('search').addEventListener('input', function(e) {
            const searchTerm = e.target.value.toLowerCase();
            const commands = document.querySelectorAll('.command');
            const categories = document.querySelectorAll('.category');

            commands.forEach(command => {
                const commandName = command.dataset.command;
                const commandText = command.textContent.toLowerCase();
                if (commandName.includes(searchTerm) || commandText.includes(searchTerm)) {
                    command.classList.remove('hidden');
                } else {
                    command.classList.add('hidden');
                }
            });

            // Hide empty categories
            categories.forEach(category => {
                const visibleCommands = category.querySelectorAll('.command:not(.hidden)');
                if (visibleCommands.length === 0) {
                    category.classList.add('hidden');
                } else {
                    category.classList.remove('hidden');
                }
            });
        });

        // Toggle command details
        function toggleCommand(header) {
            const command = header.parentElement;
            const details = command.querySelector('.command-details');
            const icon = header.querySelector('.command-expand-icon');

            details.classList.toggle('show');
            icon.classList.toggle('expanded');
        }

        // Copy to clipboard
        function copyCode(button) {
            const code = button.nextElementSibling.textContent;
            navigator.clipboard.writeText(code).then(() => {
                const originalText = button.textContent;
                button.textContent = 'Copied!';
                setTimeout(() => {
                    button.textContent = originalText;
                }, 2000);
            });
        }

        // Tab switching
        function switchTab(button, tabId) {
            const tabButtons = button.parentElement;
            const tabs = tabButtons.parentElement;

            // Remove active class from all buttons and tabs
            tabButtons.querySelectorAll('.tab-btn').forEach(btn => {
                btn.classList.remove('active');
            });
            tabs.querySelectorAll('.tab-content').forEach(content => {
                content.classList.remove('active');
            });

            // Add active class to clicked button and corresponding tab
            button.classList.add('active');
            document.getElementById(tabId).classList.add('active');
        }

        // Try command functionality
        async function tryCommand(button, commandName) {
            const section = button.parentElement;
            const textarea = section.querySelector('.json-input');
            const output = section.querySelector('.response-output');

            // Disable button while processing
            button.disabled = true;
            button.textContent = 'Executing...';

            // Clear previous output
            output.className = 'response-output show';
            output.textContent = 'Loading...';

            try {
                // Parse JSON from textarea
                let requestBody;
                try {
                    requestBody = JSON.parse(textarea.value);
                } catch (e) {
                    throw new Error('Invalid JSON: ' + e.message);
                }

                // Get stored token
                const token = localStorage.getItem(TOKEN_STORAGE_KEY);

                // Build headers
                const headers = {
                    'Content-Type': 'application/json',
                };

                // Add authorization header if token exists
                if (token) {
                    headers['Authorization'] = 'Bearer ' + token;
                }

                // Make API request
                const response = await fetch('/api', {
                    method: 'POST',
                    headers: headers,
                    body: JSON.stringify(requestBody)
                });

                let result;
                const contentType = response.headers.get('content-type');
                if (contentType && contentType.includes('application/json')) {
                    result = await response.json();
                } else {
                    const text = await response.text();
                    result = { error: text };
                }

                // Display result
                if (response.ok) {
                    output.className = 'response-output show success';
                    output.textContent = 'Success!\\n\\n' + JSON.stringify(result, null, 2);
                } else {
                    output.className = 'response-output show error';

                    // Handle 401/403 - token invalid or revoked
                    if (response.status === 401 || response.status === 403) {
                        clearAuth();
                        output.textContent = 'Authentication Error: Your session has expired '
                            + 'or token was revoked. Please login again.';
                    } else {
                        // Try to extract a meaningful error message
                        let errorMsg = 'Request failed';
                        if (result.error) {
                            errorMsg = result.error;
                        } else if (result.message) {
                            errorMsg = result.message;
                        } else if (typeof result === 'string') {
                            errorMsg = result;
                        } else {
                            errorMsg = JSON.stringify(result, null, 2);
                        }
                        output.textContent = 'Error: ' + errorMsg;
                    }
                }
            } catch (error) {
                output.className = 'response-output show error';
                // Provide more user-friendly error messages
                if (error.message.includes('Invalid JSON')) {
                    output.textContent = 'JSON Syntax Error: Please check your request format. '
                        + error.message;
                } else if (error.message.includes('Failed to fetch')) {
                    output.textContent = 'Connection Error: Unable to reach the API server. '
                        + 'Please check if the server is running.';
                } else {
                    output.textContent = 'Error: ' + error.message;
                }
            } finally {
                button.disabled = false;
                button.textContent = 'Execute';
            }
        }
    </script>
</body>
</html>
"""

    return html


def generate_schemas_reference(  # noqa: PLR0915
    command_handlers: dict[str, APICommandHandler],
) -> str:
    """Generate HTML schemas reference page with all data models."""
    # Collect all unique schemas from commands
    schemas: dict[str, Any] = {}

    for handler in command_handlers.values():
        # Skip aliases - they are for backward compatibility only
        if handler.alias:
            continue
        # Collect schemas from parameters
        for param_name in handler.signature.parameters:
            if param_name == "self":
                continue
            # Skip return_type parameter (used only for type hints)
            if param_name == "return_type":
                continue
            param_type = handler.type_hints.get(param_name, Any)
            if param_type is not Any and str(param_type) != "typing.Any":
                _get_type_schema(param_type, schemas)

        # Collect schemas from return type
        return_type = handler.type_hints.get("return", Any)
        if return_type is not Any and str(return_type) != "typing.Any":
            _get_type_schema(return_type, schemas)

    # Build HTML
    html = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Music Assistant API - Schemas Reference</title>
    <style>
        :root {
            --fg: #000000;
            --background: #f5f5f5;
            --panel: #ffffff;
            --primary: #03a9f4;
            --text-secondary: rgba(0, 0, 0, 0.6);
            --border: rgba(0, 0, 0, 0.1);
            --success: #4caf50;
            --success-bg: rgba(76, 175, 80, 0.1);
            --success-border: rgba(76, 175, 80, 0.3);
        }

        @media (prefers-color-scheme: dark) {
            :root {
                --fg: #ffffff;
                --background: #181818;
                --panel: #232323;
                --text-secondary: rgba(255, 255, 255, 0.7);
                --border: rgba(255, 255, 255, 0.08);
                --success: #66bb6a;
                --success-bg: rgba(102, 187, 106, 0.15);
                --success-border: rgba(102, 187, 106, 0.4);
            }
        }

        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen,
                         Ubuntu, Cantarell, sans-serif;
            background: var(--background);
            color: var(--fg);
            line-height: 1.6;
        }
        .header {
            background: var(--panel);
            color: var(--fg);
            padding: 1.5rem 2rem;
            text-align: center;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            border-bottom: 1px solid var(--border);
        }
        .header h1 {
            font-size: 1.8em;
            margin-bottom: 0.3rem;
            font-weight: 600;
        }
        .header p {
            font-size: 0.95em;
            opacity: 0.9;
        }
        .nav-container {
            background: var(--panel);
            padding: 1rem 2rem;
            box-shadow: 0 2px 5px rgba(0,0,0,0.05);
            position: sticky;
            top: 0;
            z-index: 100;
        }
        .search-box input {
            width: 100%;
            max-width: 600px;
            padding: 0.6rem 1rem;
            font-size: 0.95em;
            border: 2px solid var(--border);
            border-radius: 8px;
            display: block;
            margin: 0 auto;
        }
        .search-box input:focus {
            outline: none;
            border-color: var(--primary);
        }
        .container {
            max-width: 1200px;
            margin: 2rem auto;
            padding: 0 2rem;
        }
        .schema {
            background: var(--panel);
            margin-bottom: 1.5rem;
            border-radius: 12px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.08);
            overflow: hidden;
            scroll-margin-top: 100px;
        }
        .schema-header {
            background: var(--primary);
            color: var(--fg);
            padding: 1rem 1.5rem;
            cursor: pointer;
            user-select: none;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .schema-header:hover {
            background: var(--primary);
        }
        .schema-name {
            font-size: 1.3em;
            font-weight: 600;
            font-family: 'Monaco', 'Courier New', monospace;
        }
        .schema-expand-icon {
            font-size: 1.2em;
            transition: transform 0.3s;
        }
        .schema-expand-icon.expanded {
            transform: rotate(180deg);
        }
        .schema-content {
            padding: 1.5rem;
            display: none;
        }
        .schema-content.show {
            display: block;
        }
        .schema-description {
            color: var(--text-secondary);
            margin-bottom: 1rem;
            font-style: italic;
        }
        .properties-section {
            margin-top: 1rem;
        }
        .properties-title {
            font-weight: 600;
            color: #333;
            margin-bottom: 0.5rem;
            font-size: 1.1em;
        }
        .property {
            background: var(--panel);
            padding: 0.75rem 1rem;
            margin: 0.5rem 0;
            border-radius: 6px;
            border-left: 3px solid var(--primary);
        }
        .property-name {
            font-family: 'Monaco', 'Courier New', monospace;
            color: var(--primary);
            font-weight: 600;
            font-size: 1em;
        }
        .property-required {
            display: inline-block;
            background: #e74c3c;
            color: var(--fg);
            padding: 0.15rem 0.5rem;
            border-radius: 4px;
            font-size: 0.75em;
            font-weight: 600;
            margin-left: 0.5rem;
        }
        .property-optional {
            display: inline-block;
            background: #95a5a6;
            color: var(--fg);
            padding: 0.15rem 0.5rem;
            border-radius: 4px;
            font-size: 0.75em;
            font-weight: 600;
            margin-left: 0.5rem;
        }
        .property-nullable {
            display: inline-block;
            background: #f39c12;
            color: var(--fg);
            padding: 0.15rem 0.5rem;
            border-radius: 4px;
            font-size: 0.75em;
            font-weight: 600;
            margin-left: 0.5rem;
        }
        .property-type {
            color: var(--text-secondary);
            font-size: 0.9em;
            margin-left: 0.5rem;
            font-family: 'Monaco', 'Courier New', monospace;
        }
        .property-description {
            color: var(--text-secondary);
            margin-top: 0.25rem;
            font-size: 0.95em;
        }
        .type-link {
            color: var(--primary);
            text-decoration: none;
            border-bottom: 1px dashed var(--primary);
            transition: all 0.2s;
        }
        .type-link:hover {
            color: var(--primary);
            border-bottom-color: var(--primary);
        }
        .hidden {
            display: none;
        }
        .back-link {
            display: inline-block;
            margin-bottom: 1rem;
            padding: 0.5rem 1rem;
            background: var(--primary);
            color: var(--fg);
            text-decoration: none;
            border-radius: 6px;
            transition: background 0.2s;
        }
        .back-link:hover {
            background: var(--primary);
        }
        .openapi-link {
            display: inline-block;
            padding: 0.5rem 1rem;
            background: #2e7d32;
            color: var(--fg);
            text-decoration: none;
            border-radius: 6px;
            transition: background 0.2s;
        }
        .openapi-link:hover {
            background: #1b5e20;
        }
        .enum-values {
            margin-top: 0.5rem;
            padding: 0.5rem;
            background: #fff;
            border-radius: 4px;
        }
        .enum-values-title {
            font-weight: 600;
            color: #555;
            font-size: 0.9em;
            margin-bottom: 0.25rem;
        }
        .enum-value {
            display: inline-block;
            padding: 0.2rem 0.5rem;
            margin: 0.2rem;
            background: #e8f5e9;
            border-radius: 4px;
            font-family: 'Monaco', 'Courier New', monospace;
            font-size: 0.85em;
            color: #2e7d32;
        }
        .header .logo {
            margin-bottom: 1rem;
        }
        .header .logo img {
            width: 60px;
            height: 60px;
            object-fit: contain;
        }
    </style>
</head>
<body>
    <div class="header">
        <div class="logo">
            <img src="../logo.png" alt="Music Assistant">
        </div>
        <h1>Schemas Reference</h1>
        <p>Data models and types used in the Music Assistant API</p>
    </div>

    <div class="nav-container">
        <div class="search-box">
            <input type="text" id="search" placeholder="Search schemas..." />
        </div>
    </div>

    <div class="container">
        <a href="/api-docs" class="back-link">← Back to API Documentation</a>
"""

    # Add each schema
    for schema_name in sorted(schemas.keys()):
        schema_def = schemas[schema_name]
        html += (
            f'        <div class="schema" id="schema-{schema_name}" data-schema="{schema_name}">\n'
        )
        html += '            <div class="schema-header" onclick="toggleSchema(this)">\n'
        html += f'                <div class="schema-name">{schema_name}</div>\n'
        html += '                <div class="schema-expand-icon">▼</div>\n'
        html += "            </div>\n"
        html += '            <div class="schema-content">\n'

        # Add description if available
        if "description" in schema_def:
            desc = schema_def["description"]
            html += f'                <div class="schema-description">{desc}</div>\n'

        # Add properties if available
        if "properties" in schema_def:
            html += '                <div class="properties-section">\n'
            html += '                    <div class="properties-title">Properties:</div>\n'

            # Get required fields list
            required_fields = schema_def.get("required", [])

            for prop_name, prop_def in schema_def["properties"].items():
                html += '                    <div class="property">\n'
                html += f'                        <span class="property-name">{prop_name}</span>\n'

                # Check if field is required
                is_required = prop_name in required_fields

                # Check if field is nullable (type is "null" or has null in anyOf/oneOf)
                is_nullable = False
                if "type" in prop_def and prop_def["type"] == "null":
                    is_nullable = True
                elif "anyOf" in prop_def:
                    is_nullable = any(item.get("type") == "null" for item in prop_def["anyOf"])
                elif "oneOf" in prop_def:
                    is_nullable = any(item.get("type") == "null" for item in prop_def["oneOf"])

                # Add required/optional badge
                if is_required:
                    html += (
                        '                        <span class="property-required">REQUIRED</span>\n'
                    )
                else:
                    html += (
                        '                        <span class="property-optional">OPTIONAL</span>\n'
                    )

                # Add nullable badge if applicable
                if is_nullable:
                    html += (
                        '                        <span class="property-nullable">NULLABLE</span>\n'
                    )

                # Add type
                if "type" in prop_def:
                    prop_type = prop_def["type"]
                    html += (
                        f'                        <span class="property-type">{prop_type}</span>\n'
                    )
                elif "$ref" in prop_def:
                    # Extract type name from $ref
                    ref_type = prop_def["$ref"].split("/")[-1]
                    html += (
                        f'                        <span class="property-type">'
                        f'<a href="#schema-{ref_type}" class="type-link">'
                        f"{ref_type}</a></span>\n"
                    )

                # Add description
                if "description" in prop_def:
                    prop_desc = prop_def["description"]
                    html += (
                        f'                        <div class="property-description">'
                        f"{prop_desc}</div>\n"
                    )

                # Add enum values if present
                if "enum" in prop_def:
                    html += '                        <div class="enum-values">\n'
                    html += (
                        '                            <div class="enum-values-title">'
                        "Possible values:</div>\n"
                    )
                    for enum_val in prop_def["enum"]:
                        html += (
                            f'                            <span class="enum-value">'
                            f"{enum_val}</span>\n"
                        )
                    html += "                        </div>\n"

                html += "                    </div>\n"

            html += "                </div>\n"

        html += "            </div>\n"
        html += "        </div>\n"

    html += """
        <div style="text-align: center; margin-top: 3rem; padding: 2rem 0;">
            <a href="/api-docs/openapi.json" class="openapi-link" download>
                📄 Download OpenAPI Spec
            </a>
        </div>
    </div>

    <script>
        // Search functionality
        document.getElementById('search').addEventListener('input', function(e) {
            const searchTerm = e.target.value.toLowerCase();
            const schemas = document.querySelectorAll('.schema');

            schemas.forEach(schema => {
                const schemaName = schema.dataset.schema;
                const schemaText = schema.textContent.toLowerCase();
                const nameMatch = schemaName.toLowerCase().includes(searchTerm);
                const textMatch = schemaText.includes(searchTerm);
                if (nameMatch || textMatch) {
                    schema.classList.remove('hidden');
                } else {
                    schema.classList.add('hidden');
                }
            });
        });

        // Toggle schema details
        function toggleSchema(header) {
            const schema = header.parentElement;
            const content = schema.querySelector('.schema-content');
            const icon = header.querySelector('.schema-expand-icon');

            content.classList.toggle('show');
            icon.classList.toggle('expanded');
        }

        // Handle deep linking - expand and scroll to schema on page load
        window.addEventListener('DOMContentLoaded', function() {
            const hash = window.location.hash;
            if (hash && hash.startsWith('#schema-')) {
                const schemaElement = document.querySelector(hash);
                if (schemaElement) {
                    // Expand the schema
                    const content = schemaElement.querySelector('.schema-content');
                    const icon = schemaElement.querySelector('.schema-expand-icon');
                    if (content && icon) {
                        content.classList.add('show');
                        icon.classList.add('expanded');
                    }
                    // Scroll to it
                    setTimeout(() => {
                        schemaElement.scrollIntoView({ behavior: 'smooth', block: 'center' });
                        // Highlight temporarily
                        schemaElement.style.transition = 'background-color 0.3s';
                        schemaElement.style.backgroundColor = '#fff3cd';
                        setTimeout(() => {
                            schemaElement.style.backgroundColor = '';
                        }, 2000);
                    }, 100);
                }
            }
        });

        // Listen for hash changes (when user clicks a type link)
        window.addEventListener('hashchange', function() {
            const hash = window.location.hash;
            if (hash && hash.startsWith('#schema-')) {
                const schemaElement = document.querySelector(hash);
                if (schemaElement) {
                    // Expand if collapsed
                    const content = schemaElement.querySelector('.schema-content');
                    const icon = schemaElement.querySelector('.schema-expand-icon');
                    if (content && !content.classList.contains('show')) {
                        content.classList.add('show');
                        icon.classList.add('expanded');
                    }
                    // Scroll to it
                    schemaElement.scrollIntoView({ behavior: 'smooth', block: 'center' });
                    // Highlight temporarily
                    schemaElement.style.transition = 'background-color 0.3s';
                    schemaElement.style.backgroundColor = '#fff3cd';
                    setTimeout(() => {
                        schemaElement.style.backgroundColor = '';
                    }, 2000);
                }
            }
        });
    </script>
</body>
</html>
"""

    return html


def generate_html_docs(  # noqa: PLR0915
    command_handlers: dict[str, APICommandHandler],
    server_url: str = "http://localhost:8095",
    version: str = "1.0.0",
) -> str:
    """Generate HTML documentation from API command handlers."""
    # Group commands by category
    categories: dict[str, list[tuple[str, APICommandHandler]]] = {}
    for command, handler in sorted(command_handlers.items()):
        # Skip aliases - they are for backward compatibility only
        if handler.alias:
            continue
        category = command.split("/")[0] if "/" in command else "general"
        if category not in categories:
            categories[category] = []
        categories[category].append((command, handler))

    # Start building HTML
    html_parts = [
        """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Music Assistant API Documentation</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto,
                Oxygen, Ubuntu, Cantarell, sans-serif;
            line-height: 1.6;
            color: #333;
            background: #f5f5f5;
        }
        .container {
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
        }
        .header {
            background: linear-gradient(135deg, var(--primary) 0%, var(--primary) 100%);
            color: var(--fg);
            padding: 40px 20px;
            text-align: center;
            margin-bottom: 30px;
            border-radius: 8px;
            box-shadow: 0 4px 6px rgba(0,0,0,0.1);
        }
        .header h1 {
            font-size: 2.5em;
            margin-bottom: 10px;
        }
        .header p {
            font-size: 1.1em;
            opacity: 0.9;
        }
        .intro {
            background: var(--panel);
            padding: 30px;
            margin-bottom: 30px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        .intro h2 {
            color: var(--primary);
            margin-bottom: 15px;
        }
        .intro h3 {
            color: var(--primary);
            margin: 20px 0 10px 0;
        }
        .intro pre {
            background: var(--panel);
            padding: 15px;
            border-radius: 4px;
            overflow-x: auto;
            border-left: 4px solid var(--primary);
        }
        .intro code {
            font-family: 'Monaco', 'Courier New', monospace;
            font-size: 0.9em;
        }
        .category {
            background: var(--panel);
            margin-bottom: 30px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            overflow: hidden;
        }
        .category-header {
            background: var(--primary);
            color: var(--fg);
            padding: 20px;
            font-size: 1.5em;
            font-weight: bold;
            text-transform: capitalize;
        }
        .command {
            border-bottom: 1px solid #e0e0e0;
            padding: 20px;
        }
        .command:last-child {
            border-bottom: none;
        }
        .command-name {
            font-size: 1.2em;
            font-weight: bold;
            color: var(--primary);
            font-family: 'Monaco', 'Courier New', monospace;
            margin-bottom: 10px;
        }
        .command-description {
            color: var(--text-secondary);
            margin-bottom: 15px;
        }
        .params, .returns {
            margin-top: 15px;
        }
        .params h4, .returns h4 {
            color: var(--primary);
            margin-bottom: 10px;
            font-size: 1em;
        }
        .param {
            background: var(--panel);
            padding: 10px;
            margin: 5px 0;
            border-radius: 4px;
            border-left: 3px solid var(--primary);
        }
        .param-name {
            font-weight: bold;
            color: #333;
            font-family: 'Monaco', 'Courier New', monospace;
        }
        .param-type {
            color: var(--primary);
            font-style: italic;
            font-size: 0.9em;
        }
        .param-required {
            color: #e74c3c;
            font-size: 0.85em;
            font-weight: bold;
        }
        .param-optional {
            color: #95a5a6;
            font-size: 0.85em;
        }
        .param-description {
            color: var(--text-secondary);
            margin-top: 5px;
        }
        .return-type {
            background: var(--panel);
            padding: 10px;
            border-radius: 4px;
            border-left: 3px solid var(--primary);
            font-family: 'Monaco', 'Courier New', monospace;
            color: var(--primary);
        }
        .nav {
            background: var(--panel);
            padding: 20px;
            margin-bottom: 30px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        .nav h3 {
            color: var(--primary);
            margin-bottom: 15px;
        }
        .nav ul {
            list-style: none;
        }
        .nav li {
            margin: 5px 0;
        }
        .nav a {
            color: var(--primary);
            text-decoration: none;
            text-transform: capitalize;
        }
        .nav a:hover {
            text-decoration: underline;
        }
        .download-link {
            display: inline-block;
            background: var(--primary);
            color: var(--fg);
            padding: 10px 20px;
            border-radius: 4px;
            text-decoration: none;
            margin-top: 10px;
        }
        .download-link:hover {
            background: var(--primary);
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>Music Assistant API Documentation</h1>
            <p>Version """,
        version,
        """</p>
        </div>

        <div class="intro">
            <h2>Getting Started</h2>
            <p>Music Assistant provides two ways to interact with the API:</p>

            <h3>🔌 WebSocket API (Recommended)</h3>
            <p>
                The WebSocket API provides full access to all commands
                and <strong>real-time event updates</strong>.
            </p>
            <ul style="margin-left: 20px; margin-top: 10px;">
                <li><strong>Endpoint:</strong> <code>ws://""",
        server_url.replace("http://", "").replace("https://", ""),
        """/ws</code></li>
                <li>
                    <strong>Best for:</strong> Applications that need live
                    updates and real-time communication
                </li>
                <li>
                    <strong>Bonus:</strong> When connected, you automatically
                    receive event messages for state changes
                </li>
            </ul>
            <p style="margin-top: 10px;"><strong>Sending commands:</strong></p>
            <pre><code>{
  "message_id": "unique-id-123",
  "command": "players/all",
  "args": {}
}</code></pre>
            <p style="margin-top: 10px;"><strong>Receiving events:</strong></p>
            <p>
                Once connected, you will automatically receive event messages
                whenever something changes:
            </p>
            <pre><code>{
  "event": "player_updated",
  "data": {
    "player_id": "player_123",
    ...player data...
  }
}</code></pre>

            <h3>🌐 REST API (Simple)</h3>
            <p>
                The REST API provides a simple HTTP interface for
                executing commands.
            </p>
            <ul style="margin-left: 20px; margin-top: 10px;">
                <li><strong>Endpoint:</strong> <code>POST """,
        server_url,
        """/api</code></li>
                <li>
                    <strong>Best for:</strong> Simple, incidental commands
                    without need for real-time updates
                </li>
            </ul>
            <p style="margin-top: 10px;"><strong>Example request:</strong></p>
            <pre><code>{
  "command": "players/all",
  "args": {}
}</code></pre>

            <h3>📥 OpenAPI Specification</h3>
            <p>Download the OpenAPI 3.0 specification for automated client generation:</p>
            <a href="/openapi.json" class="download-link">Download openapi.json</a>

            <h3>🚀 Interactive API Explorers</h3>
            <p>
                Try out the API interactively with our API explorers.
                Test endpoints, see live responses, and explore the full API:
            </p>
            <div style="margin-top: 15px;">
                <a href="/api-explorer" class="download-link" style="margin-right: 10px;">
                    Swagger UI Explorer
                </a>
                <a href="/api-docs" class="download-link">
                    ReDoc Documentation
                </a>
            </div>

            <h3>📡 WebSocket Events</h3>
            <p>
                When connected via WebSocket, you automatically receive
                real-time event notifications:
            </p>
            <div style="margin-top: 15px; margin-left: 20px;">
                <strong>Player Events:</strong>
                <ul style="margin-left: 20px;">
                    <li><code>player_added</code> - New player discovered</li>
                    <li><code>player_updated</code> - Player state changed</li>
                    <li><code>player_removed</code> - Player disconnected</li>
                    <li><code>player_config_updated</code> - Player settings changed</li>
                </ul>

                <strong style="margin-top: 10px; display: block;">Queue Events:</strong>
                <ul style="margin-left: 20px;">
                    <li><code>queue_added</code> - New queue created</li>
                    <li><code>queue_updated</code> - Queue state changed</li>
                    <li><code>queue_items_updated</code> - Queue content changed</li>
                    <li><code>queue_time_updated</code> - Playback position updated</li>
                </ul>

                <strong style="margin-top: 10px; display: block;">Library Events:</strong>
                <ul style="margin-left: 20px;">
                    <li><code>media_item_added</code> - New media added to library</li>
                    <li><code>media_item_updated</code> - Media metadata updated</li>
                    <li><code>media_item_deleted</code> - Media removed from library</li>
                    <li><code>media_item_played</code> - Media playback started</li>
                </ul>

                <strong style="margin-top: 10px; display: block;">System Events:</strong>
                <ul style="margin-left: 20px;">
                    <li><code>providers_updated</code> - Provider status changed</li>
                    <li><code>sync_tasks_updated</code> - Sync progress updated</li>
                    <li><code>application_shutdown</code> - Server shutting down</li>
                </ul>
            </div>
        </div>

        <div class="nav">
            <h3>Quick Navigation</h3>
            <ul>
""",
    ]

    # Add navigation links
    for category in sorted(categories.keys()):
        html_parts.append(
            f'                <li><a href="#{category}">{category}</a> '
            f"({len(categories[category])} commands)</li>\n"
        )

    html_parts.append(
        """            </ul>
        </div>
"""
    )

    # Add commands by category
    for category, commands in sorted(categories.items()):
        html_parts.append(f'        <div class="category" id="{category}">\n')
        html_parts.append(f'            <div class="category-header">{category}</div>\n')

        for command, handler in commands:
            _, description, param_descriptions = _parse_docstring(handler.target)

            html_parts.append('            <div class="command">\n')
            html_parts.append(f'                <div class="command-name">{command}</div>\n')

            if description:
                html_parts.append(
                    f'                <div class="command-description">{description}</div>\n'
                )

            # Parameters
            params_html = []
            for param_name, param in handler.signature.parameters.items():
                if param_name == "self":
                    continue
                # Skip return_type parameter (used only for type hints)
                if param_name == "return_type":
                    continue

                param_type = handler.type_hints.get(param_name, Any)
                is_required = param.default is inspect.Parameter.empty
                param_desc = param_descriptions.get(param_name, "")

                # Format type name
                type_name = _format_type_name(param_type)
                if get_origin(param_type):
                    origin = get_origin(param_type)
                    args = get_args(param_type)
                    if origin is Union or origin is UnionType:
                        type_name = " | ".join(_format_type_name(arg) for arg in args)
                    elif origin in (list, tuple):
                        if args:
                            inner_type = _format_type_name(args[0])
                            type_name = f"{origin.__name__}[{inner_type}]"
                    elif origin is dict:
                        if len(args) == 2:
                            key_type = _format_type_name(args[0])
                            val_type = _format_type_name(args[1])
                            type_name = f"dict[{key_type}, {val_type}]"

                required_badge = (
                    '<span class="param-required">required</span>'
                    if is_required
                    else '<span class="param-optional">optional</span>'
                )

                # Format default value
                default_str = ""
                if not is_required and param.default is not None:
                    try:
                        if isinstance(param.default, str):
                            default_str = f' = "{param.default}"'
                        elif isinstance(param.default, Enum):
                            default_str = f" = {param.default.value}"
                        elif isinstance(param.default, (int, float, bool, list, dict)):
                            default_str = f" = {param.default}"
                    except Exception:  # noqa: S110
                        pass  # Can't serialize, skip default

                params_html.append(
                    f'                    <div class="param">\n'
                    f'                        <span class="param-name">{param_name}</span>\n'
                    f'                        <span class="param-type">'
                    f"({type_name}{default_str})</span>\n"
                    f"                        {required_badge}\n"
                )
                if param_desc:
                    params_html.append(
                        f'                        <div class="param-description">'
                        f"{param_desc}</div>\n"
                    )
                params_html.append("                    </div>\n")

            if params_html:
                html_parts.append('                <div class="params">\n')
                html_parts.append("                    <h4>Parameters</h4>\n")
                html_parts.extend(params_html)
                html_parts.append("                </div>\n")

            # Return type
            return_type = handler.type_hints.get("return", Any)
            if return_type and return_type is not NoneType:
                type_name = _format_type_name(return_type)
                if get_origin(return_type):
                    origin = get_origin(return_type)
                    args = get_args(return_type)
                    if origin in (list, tuple) and args:
                        inner_type = _format_type_name(args[0])
                        type_name = f"{origin.__name__}[{inner_type}]"
                    elif origin is Union or origin is UnionType:
                        type_name = " | ".join(_format_type_name(arg) for arg in args)

                html_parts.append('                <div class="returns">\n')
                html_parts.append("                    <h4>Returns</h4>\n")
                html_parts.append(
                    f'                    <div class="return-type">{type_name}</div>\n'
                )
                html_parts.append("                </div>\n")

            html_parts.append("            </div>\n")

        html_parts.append("        </div>\n")

    html_parts.append(
        """    </div>
</body>
</html>
"""
    )

    return "".join(html_parts)
