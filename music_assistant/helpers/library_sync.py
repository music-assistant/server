"""Context shared by library sync updates."""

from __future__ import annotations

from contextvars import ContextVar

# When set, overwrite updates replace metadata even when every field is empty.
# Provider sync uses this so clearing the last bio/image is authoritative; nested
# album/artist stubs on track updates leave it unset so empty stubs still merge.
REPLACE_EMPTY_METADATA: ContextVar[bool] = ContextVar("REPLACE_EMPTY_METADATA", default=False)
