"""Regression guards for the MA integration boundaries."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

from music_assistant.providers.msx_bridge import http_server, mappers, party, player, provider

PROVIDER_MODULES = (http_server, mappers, party, player, provider)


def test_provider_modules_have_one_canonical_identity() -> None:
    """Provider modules must be imported only through MA's installed package path."""
    for module in PROVIDER_MODULES:
        assert module.__name__.startswith("music_assistant.providers.msx_bridge.")
        assert module.__file__ is not None
        source = Path(module.__file__).resolve()
        loaded = {
            id(candidate)
            for candidate in tuple(sys.modules.values())
            if (filename := getattr(candidate, "__file__", None))
            and isinstance(filename, str)
            and Path(filename).resolve() == source
        }
        assert loaded == {id(module)}, f"Multiple module objects loaded from {source}"


def test_identity_guard_detects_duplicate_module_load() -> None:
    """The identity guard must reject a second module object loaded from the same file."""
    assert player.__file__ is not None
    alias = "music_assistant.providers.msx_bridge._duplicate_player"
    spec = importlib.util.spec_from_file_location(alias, player.__file__)
    assert spec is not None
    assert spec.loader is not None
    duplicate = importlib.util.module_from_spec(spec)
    sys.modules[alias] = duplicate
    try:
        spec.loader.exec_module(duplicate)
        with pytest.raises(AssertionError):
            test_provider_modules_have_one_canonical_identity()
    finally:
        sys.modules.pop(alias, None)


def test_known_ma_model_boundaries_do_not_use_reflection() -> None:
    """Known MA media and queue contracts stay statically expressed."""
    for module in (mappers, http_server):
        assert module.__file__ is not None
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "getattr(" not in source
        assert "hasattr(" not in source


def test_test_data_does_not_mock_ma_domain_models() -> None:
    """MA records must be concrete instances, not permissive spec mocks."""
    tests_dir = Path(__file__).parent
    forbidden = re.compile(
        r"(?:Mock|MagicMock)\(spec=(?:Album|Artist|Playlist|PlayerMedia|PlayerQueue|QueueItem|Track)"
    )
    for test_file in tests_dir.glob("test_*.py"):
        if test_file == Path(__file__):
            continue
        assert forbidden.search(test_file.read_text(encoding="utf-8")) is None


def test_root_fixture_uses_explicit_mass_fake() -> None:
    """The shared test mass fixture cannot grow arbitrary child mocks."""
    source = (Path(__file__).parent / "conftest.py").read_text(encoding="utf-8")
    assert "mass = Mock()" not in source
    assert "FakeMass(" in source
