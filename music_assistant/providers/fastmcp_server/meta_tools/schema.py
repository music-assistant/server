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

    if hasattr(tool, "parameters"):
        input_schema = tool.parameters or {"type": "object"}
        output_schema = getattr(tool, "output_schema", None)
        annotations = getattr(tool, "annotations", None)
        title = getattr(tool, "title", None)
    elif hasattr(tool, "to_mcp_tool"):
        mcp_tool = tool.to_mcp_tool()
        payload = mcp_tool.model_dump(exclude_none=True) if hasattr(mcp_tool, "model_dump") else {}
        name = payload.get("name") or name
        description = payload.get("description") or description
        input_schema = payload.get("inputSchema") or {"type": "object"}
        output_schema = payload.get("outputSchema")
        annotations = payload.get("annotations")
        title = payload.get("title")
    elif hasattr(tool, "model_dump"):
        payload = tool.model_dump(exclude_none=True)
        name = payload.get("name") or name
        description = payload.get("description") or description
        input_schema = payload.get("inputSchema") or {"type": "object"}
        output_schema = payload.get("outputSchema")
        annotations = payload.get("annotations")
        title = payload.get("title")
    else:
        input_schema = {"type": "object"}
        output_schema = None
        annotations = None
        title = None

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
