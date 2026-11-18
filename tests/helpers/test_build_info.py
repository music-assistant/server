"""Tests for identifying the official release builds."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from music_assistant.helpers import build_info as build_info_module
from music_assistant.helpers.build_info import get_official_build_info

BUILD_INFO = {"version": "2.11.0", "revision": "a" * 40, "wheel_sha256": "b" * 64}


@pytest.fixture
def build_info_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the build info lookup at a temporary file."""
    file = tmp_path / "build_info.json"
    monkeypatch.setattr(build_info_module, "BUILD_INFO_FILE", file)
    return file


async def test_official_build(monkeypatch: pytest.MonkeyPatch, build_info_file: Path) -> None:
    """The official image carries both the bundled credentials and the build info."""
    build_info_file.write_text(json.dumps(BUILD_INFO), encoding="utf-8")
    monkeypatch.setattr(build_info_module, "has_bundled_app_vars", lambda: True)

    assert await get_official_build_info() == BUILD_INFO


@pytest.mark.parametrize(
    ("bundled", "content"),
    [
        # a self-built image
        (False, json.dumps(BUILD_INFO)),
        # the release wheel installed outside the image
        (True, None),
        (True, "{ not valid json"),
        (True, json.dumps(["not", "a", "map"])),
    ],
)
async def test_unsupported_install(
    monkeypatch: pytest.MonkeyPatch, build_info_file: Path, bundled: bool, content: str | None
) -> None:
    """Anything but the official release image is not an official build."""
    if content is not None:
        build_info_file.write_text(content, encoding="utf-8")
    monkeypatch.setattr(build_info_module, "has_bundled_app_vars", lambda: bundled)

    assert await get_official_build_info() is None
