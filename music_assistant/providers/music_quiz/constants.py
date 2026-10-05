"""Constants for the Music Quiz provider."""

from __future__ import annotations

# guards applied to every AI request and response in this provider, so a wedged or
# runaway engine fails the round instead of the whole quiz
AI_QUERY_TIMEOUT_SECONDS = 30.0
MAX_AI_PROMPT_BYTES = 8192
MAX_AI_RESPONSE_BYTES = 4096
MAX_AI_RESPONSE_LINES = 32
