"""Serialize MCP tool definitions for the meta-tool catalog."""

from __future__ import annotations

from typing import Any


def _json_safe(value: Any) -> Any:
    """Recursively convert Pydantic models to JSON-serializable dicts."""
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


def build_tool_schema(tool: Any) -> dict[str, Any]:
    """Build a JSON-serializable tool definition (name, description, schemas)."""
    name = getattr(tool, "name", "")
    description = getattr(tool, "description", "") or ""
    input_schema = getattr(tool, "parameters", None) or {"type": "object"}
    output_schema = getattr(tool, "output_schema", None)
    annotations = getattr(tool, "annotations", None)
    title = getattr(tool, "title", None)

    result: dict[str, Any] = {
        "name": name,
        "description": description,
        "inputSchema": _json_safe(input_schema),
    }
    if output_schema:
        result["outputSchema"] = _json_safe(output_schema)
    if annotations:
        result["annotations"] = _json_safe(annotations)
    if title:
        result["title"] = title
    return result
