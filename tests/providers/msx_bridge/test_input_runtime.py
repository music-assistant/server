"""Native search regressions through the real shipped TVX Input Plugin."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from music_assistant.providers.msx_bridge import http_server


@pytest.mark.parametrize(
    ("scenario", "query"),
    [
        ("cancel", ""),
        ("stale", ""),
        ("timeout", ""),
        ("error", ""),
        ("success", "trooper"),
        ("success", "ЧайФ & | @"),
        ("success", "no hits"),
    ],
)
def test_native_input_completion_and_cancellation(scenario: str, query: str) -> None:
    """Cancelled or superseded requests never leave busy or replace a newer result."""
    node = shutil.which("node")
    if not node:
        pytest.skip("Native input runtime tests require Node.js")
    static = Path(http_server.__file__).parent / "static"
    runner = Path(__file__).with_name("input_runtime.cjs")
    result = subprocess.run(  # noqa: S603 - fixed runtime and shipped code
        [node, str(runner)],
        input=json.dumps(
            {
                "library": (static / "tvx-plugin.min.js").read_text(),
                "script": (static / "input.js").read_text(),
                "scenario": scenario,
                "query": query,
            }
        ),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "ok"
