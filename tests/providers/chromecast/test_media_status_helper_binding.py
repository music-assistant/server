"""
Test that guards the media-status test setup itself.

The media-status tests drive the handler with a MagicMock as ``self``, so a helper
that is not bound to its real implementation answers as a mock and its part of the
handler is skipped without any test failing.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

from music_assistant.providers.chromecast.player import ChromecastPlayer
from tests.providers.chromecast.helpers import MEDIA_STATUS_HELPERS

# helpers the tests deliberately keep mocked, to drive the handler down a chosen path
MOCKED_SEAMS = {"_flow_stream_underrun"}


def _private_methods_called_by_handler() -> set[str]:
    """Return the names of the private ChromecastPlayer methods _handle_media_status calls."""
    source = textwrap.dedent(inspect.getsource(ChromecastPlayer._handle_media_status))
    return {
        node.func.attr
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "self"
        and node.func.attr.startswith("_")
        and callable(getattr(ChromecastPlayer, node.func.attr, None))
    }


def test_every_media_status_helper_is_bound() -> None:
    """Every helper the handler delegates to is bound, so no part of it is skipped."""
    assert _private_methods_called_by_handler() - MOCKED_SEAMS == set(MEDIA_STATUS_HELPERS)
