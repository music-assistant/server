"""
Tests for provider/ma_authenticator.py.

Scope is intentionally narrow — the inline-imported PassportClient integration is
verified end-to-end against a real Yandex Passport account during rollout. Here we
cover the only synchronous, deterministic gate: session_id validation inside
``make_authenticator``.

The setup flow drives the Passport Device Flow natively and hands the resulting
x_token to ``make_authenticator`` with ``allow_device_flow=False`` (the cached-token
fast-path), so the library's MA-hosted activation page is no longer exercised by this
provider and its escaping is covered by the ya-passport-auth package's own tests.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from music_assistant.providers.yandex_smarthome.ma_authenticator import make_authenticator

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

# The factory only validates its arguments here; no real MA instance is needed.
_NO_MASS = cast("MusicAssistant", None)


class TestSessionIdValidation:
    """make_authenticator rejects unsafe session ids before doing anything else."""

    def test_safe_id_accepted(self) -> None:
        """Letters, digits, dashes, underscores, len <= 64 are allowed."""
        # Should not raise; we discard the returned factory.
        make_authenticator(
            mass=_NO_MASS,
            session_id="abc-123_DEF",
        )

    def test_max_length_accepted(self) -> None:
        """64-character ids are at the inclusive upper bound."""
        make_authenticator(mass=_NO_MASS, session_id="a" * 64)

    @pytest.mark.parametrize(
        "bad",
        [
            "",  # empty
            "a" * 65,  # too long
            "has spaces",
            "../etc/passwd",  # path traversal
            "with/slash",
            "with\\backslash",
            "with;semicolon",
            "<script>",
            "with$dollar",
            "with.dot",  # dots not allowed (reserved in URLs)
        ],
    )
    def test_unsafe_ids_rejected(self, bad: str) -> None:
        """Anything with metacharacters or out-of-bounds length is rejected."""
        with pytest.raises(ValueError, match="invalid session_id"):
            make_authenticator(mass=_NO_MASS, session_id=bad)
