"""Stable, redacted failure codes for all model-visible tool errors."""

from __future__ import annotations

from enum import StrEnum

from fastmcp.exceptions import ToolError


class ToolFailureCode(StrEnum):
    """Finite public error vocabulary shared by meta-tools and MCP Apps."""

    AUTHENTICATION_REQUIRED = "authentication_required"
    NOT_FOUND_OR_FORBIDDEN = "not_found_or_forbidden"
    INVALID_ARGUMENTS = "invalid_arguments"
    CONFIRMATION_REQUIRED = "confirmation_required"
    OPERATION_CANCELLED = "operation_cancelled"
    CATALOG_CHANGED = "catalog_changed"
    EXECUTION_TIMEOUT = "execution_timeout"
    EXECUTION_FAILED = "execution_failed"
    RESPONSE_TOO_LARGE = "response_too_large"


def tool_failure(code: ToolFailureCode, message: str) -> ToolError:
    """Build a public failure without request data or exception text."""
    return ToolError(f"[{code}] {message}")
