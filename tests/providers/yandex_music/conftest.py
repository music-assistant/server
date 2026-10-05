"""Shared fixtures and stubs for Yandex Music provider tests."""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import inspect
import logging
import sys
from pathlib import Path
from types import MethodType
from typing import Any
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import ItemMapping

from music_assistant.mass import MusicAssistant

_PROVIDER_PKG = "music_assistant.providers.yandex_music"


def _alias_working_tree_provider(provider_dir: Path) -> None:
    """
    Alias the ``provider`` working-tree package onto the upstream import path.

    In the provider-repo layout, tests must exercise the working tree, not the
    provider snapshot baked into the venv's music_assistant install. In the
    upstream (inlined) layout there is no sibling ``provider/`` directory and
    the package under test IS the checkout itself, so the aliasing must no-op
    instead of failing collection.

    :param provider_dir: Path to the ``provider`` working-tree directory.
    """
    provider_dir = provider_dir.resolve()
    if not provider_dir.is_dir():
        return
    existing = sys.modules.get(_PROVIDER_PKG)
    if existing is not None:
        # Something imported the provider before this conftest ran — silently
        # testing the venv snapshot instead of the working tree must be fatal.
        loaded_from = Path(getattr(existing, "__file__", "") or "").resolve().parent
        if loaded_from != provider_dir:
            raise RuntimeError(
                f"{_PROVIDER_PKG} was already imported from {loaded_from}; "
                f"tests must run against {provider_dir}"
            )
        return
    spec = importlib.util.spec_from_file_location(
        _PROVIDER_PKG,
        provider_dir / "__init__.py",
        submodule_search_locations=[str(provider_dir)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load provider package from {provider_dir}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_PROVIDER_PKG] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        # Mirror the import machinery: a failed exec must not leave a
        # half-initialized module registered under the package name.
        del sys.modules[_PROVIDER_PKG]
        raise
    # Regular imports also bind the submodule as an attribute of its parent
    # package; monkeypatch and friends resolve dotted paths via getattr.
    parent = importlib.import_module("music_assistant.providers")
    setattr(parent, "yandex_music", module)  # noqa: B010


# The assignment + is_dir() shape (not a bare call argument) keeps this
# dereference visible to the rewrite-safe Rule C gate in CI.
_PROVIDER_DIR = Path(__file__).resolve().parent.parent / "provider"
if _PROVIDER_DIR.is_dir():
    _alias_working_tree_provider(_PROVIDER_DIR)


def provider_dir() -> Path:
    """Directory of the provider package under test, in either layout."""
    pkg = importlib.import_module(_PROVIDER_PKG)
    pkg_file = pkg.__file__
    assert pkg_file is not None  # a real package always has a file
    return Path(pkg_file).resolve().parent


def use_real_create_task(mass: MagicMock | MusicAssistant) -> None:
    """
    Give a mocked Music Assistant the real task-creation implementation.

    :param mass: The mock standing in for the Music Assistant instance.
    """
    mass._tracked_tasks = {}
    mass.verify_event_loop_thread = MagicMock()  # type: ignore[method-assign]
    real_create_task = MethodType(MusicAssistant.create_task, mass)

    def _create_task(target: Any, *args: Any, **kwargs: Any) -> Any:
        if not (inspect.iscoroutine(target) or inspect.iscoroutinefunction(target)):
            return MagicMock()
        mass.loop = asyncio.get_running_loop()
        return real_create_task(target, *args, **kwargs)

    mass.create_task = MagicMock(side_effect=_create_task)  # type: ignore[method-assign]


class ProviderStub:
    """
    Minimal provider-like object for parser tests (no Mock).

    Provides the minimal interface needed by parse_* functions.
    """

    domain = "yandex_music"
    instance_id = "yandex_music_instance"

    def __init__(self) -> None:
        """Initialize stub with minimal client."""
        self.client = type("ClientStub", (), {"user_id": 12345})()

    def get_item_mapping(self, media_type: MediaType | str, key: str, name: str) -> ItemMapping:
        """Return ItemMapping for the given media type, key and name."""
        return ItemMapping(
            media_type=MediaType(media_type) if isinstance(media_type, str) else media_type,
            item_id=key,
            provider=self.instance_id,
            name=name,
        )


class ConfigStub:
    """Minimal config stub for provider tests."""

    def __init__(self, values: dict[str, object] | None = None) -> None:
        """Initialize with optional config values."""
        self._values = values or {}

    def get_value(self, key: str, default: object = None) -> object:
        """Return config value or default."""
        return self._values.get(key, default)


class StreamingProviderStub:
    """
    Minimal provider stub for streaming tests (no Mock).

    Provides the minimal interface needed by YandexMusicStreamingManager.
    """

    domain = "yandex_music"
    instance_id = "yandex_music_instance"
    logger = logging.getLogger("yandex_music_test_streaming")

    def __init__(self) -> None:
        """Initialize stub with minimal client."""
        self.client = type("ClientStub", (), {"user_id": 12345})()
        self.mass = type("MassStub", (), {})()
        self.config = ConfigStub()
        self._warning_count = 0

    def _count_warning(self, *args: object, **kwargs: object) -> None:
        """Track warning calls for test assertions."""
        self._warning_count += 1


class TrackingLogger:
    """Logger that tracks calls for test assertions without using Mock."""

    def __init__(self) -> None:
        """Initialize with empty call counters."""
        self._debug_count = 0
        self._info_count = 0
        self._warning_count = 0
        self._error_count = 0

    def debug(self, *args: object, **kwargs: object) -> None:
        """Track debug calls."""
        self._debug_count += 1

    def info(self, *args: object, **kwargs: object) -> None:
        """Track info calls."""
        self._info_count += 1

    def warning(self, *args: object, **kwargs: object) -> None:
        """Track warning calls."""
        self._warning_count += 1

    def error(self, *args: object, **kwargs: object) -> None:
        """Track error calls."""
        self._error_count += 1


class StreamingProviderStubWithTracking:
    """
    Provider stub with tracking logger for assertions.

    Use this when you need to verify logging behavior.
    """

    domain = "yandex_music"
    instance_id = "yandex_music_instance"

    def __init__(self) -> None:
        """Initialize stub with tracking logger."""
        self.client = type("ClientStub", (), {"user_id": 12345})()
        self.mass = type("MassStub", (), {})()
        self.config = ConfigStub()
        self.logger = TrackingLogger()


# Minimal client-like object for yandex_music de_json (library requires client, not None)
DE_JSON_CLIENT = type("ClientStub", (), {"report_unknown_fields": False})()


@pytest.fixture
def provider_stub() -> ProviderStub:
    """Return a real provider stub (no Mock)."""
    return ProviderStub()


@pytest.fixture
def streaming_provider_stub() -> StreamingProviderStub:
    """Return a streaming provider stub (no Mock)."""
    return StreamingProviderStub()


@pytest.fixture
def streaming_provider_stub_with_tracking() -> StreamingProviderStubWithTracking:
    """Return a streaming provider stub with tracking logger."""
    return StreamingProviderStubWithTracking()
