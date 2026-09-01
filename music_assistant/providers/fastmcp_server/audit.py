"""Fixed-field, value-free security audit records."""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Callable
from dataclasses import dataclass

LOGGER = logging.getLogger(__name__)

ANONYMOUS_USER_ID = "ma-user:anonymous"
# Bandit B105: this is a non-secret audit category, never credential material.
NO_TOKEN_CLIENT_ID = "ma-token:none"  # nosec B105


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """One controlled security outcome without request or exception payloads."""

    user_id: str
    client_id: str
    command: str
    capability: str
    mode: str
    outcome: str


type AuditSink = Callable[[AuditRecord], None]


def emit_audit_record(record: AuditRecord) -> None:
    """Emit one structured record with a fixed message and fixed field set."""
    LOGGER.info(
        "MCP security audit",
        extra={"mcp_audit": dataclasses.asdict(record)},
    )


def is_privileged_capability(capability: str) -> bool:
    """Return whether successful and failed execution requires an audit record."""
    return capability.startswith(("edit:", "delete:", "config:", "system:", "control:", "debug:"))
