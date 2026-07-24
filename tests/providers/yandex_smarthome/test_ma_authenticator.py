"""
Tests for provider/ma_authenticator.py.

Scope is intentionally narrow — the inline-imported PassportClient +
AuthenticationHelper integration is verified end-to-end against a real
Yandex Passport account in V.4 of the rollout plan. Here we cover what
is fast, deterministic, and unit-testable without stubbing those upstream
packages:

- session_id validation (the only synchronous gate inside ``make_authenticator``)
- HTML activation page rendering (escaping, JS payload safety) — the page now
  comes from ``ya_passport_auth.ma``; the escaping contract is re-asserted
  here against this provider's page config so a library regression is caught
  at the consumer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest
from ya_passport_auth.ma import DevicePageConfig, build_device_code_page

from music_assistant.providers.yandex_smarthome.ma_authenticator import make_authenticator

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

# The factory only validates its arguments here; no real MA instance is needed.
_NO_MASS = cast("MusicAssistant", None)

_PAGE = DevicePageConfig(domain="yandex_smarthome")


def _build_device_code_page(*, user_code: str, verification_url: str, status_url: str) -> str:
    return build_device_code_page(
        user_code=user_code,
        verification_url=verification_url,
        status_url=status_url,
        expires_in=300,
        strings=_PAGE.strings_for("en"),
    )


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


class TestBuildDeviceCodePage:
    """Activation page HTML escapes user-controlled inputs safely."""

    def test_renders_valid_html(self) -> None:
        """Returns non-empty HTML with the user code and verification URL."""
        html = _build_device_code_page(
            user_code="ABCD-1234",
            verification_url="https://ya.ru/device",
            status_url="https://ma.example.com/yandex_smarthome/device_code/sess1/status",
        )
        assert html.startswith("<!DOCTYPE html>")
        assert "ABCD-1234" in html
        assert "https://ya.ru/device" in html
        # Status URL embedded as JSON-quoted string (not raw)
        assert '"https://ma.example.com/yandex_smarthome/device_code/sess1/status"' in html

    def test_escapes_html_metacharacters_in_user_code(self) -> None:
        """User code from Yandex shouldn't contain HTML, but escape defensively."""
        html = _build_device_code_page(
            user_code="<script>alert(1)</script>",
            verification_url="https://ya.ru/device",
            status_url="https://ma.example.com/status",
        )
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html

    def test_escapes_quotes_in_verification_url(self) -> None:
        """Quote-escaping protects the href attribute."""
        html = _build_device_code_page(
            user_code="X",
            verification_url='https://ya.ru/device"><script>alert(1)</script>',
            status_url="https://ma.example.com/status",
        )
        assert '"><script>alert(1)</script>' not in html
        # Either &quot; or &#34; depending on html.escape config
        assert "&quot;" in html or "&#34;" in html

    def test_status_url_with_closing_script_tag_escaped(self) -> None:
        """
        Defensive escape of </script> sequence inside the JS string literal.

        Without the replace, a malicious status_url containing ``</script>`` would
        prematurely close the surrounding ``<script>`` block and inject HTML.
        """
        html = _build_device_code_page(
            user_code="X",
            verification_url="https://ya.ru/device",
            status_url="https://evil.example/path</script><img src=x>",
        )
        assert "</script><img" not in html
        # Must be escaped via the json.dumps()-then-replace dance
        assert "<\\/script>" in html
