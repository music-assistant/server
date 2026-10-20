"""Tests for the working-tree provider aliasing performed in conftest."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from .conftest import _PROVIDER_PKG, _alias_working_tree_provider


def test_aliasing_is_noop_without_provider_working_tree(tmp_path: Path) -> None:
    """
    Skip aliasing when the ``provider/`` working tree does not exist.

    That is the upstream repo layout: the aliasing must silently no-op there
    instead of crashing test collection.
    """
    before = sys.modules.get(_PROVIDER_PKG)

    _alias_working_tree_provider(tmp_path / "provider")

    assert sys.modules.get(_PROVIDER_PKG) is before


def test_aliasing_rejects_preimported_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail loudly when the provider was already imported from another location."""
    provider_dir = tmp_path / "provider"
    provider_dir.mkdir()
    (provider_dir / "__init__.py").touch()
    snapshot = types.ModuleType(_PROVIDER_PKG)
    snapshot.__file__ = str(tmp_path / "venv_snapshot" / "__init__.py")
    monkeypatch.setitem(sys.modules, _PROVIDER_PKG, snapshot)

    with pytest.raises(RuntimeError, match="already imported"):
        _alias_working_tree_provider(provider_dir)


def test_aliasing_accepts_symlinked_working_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Treat a symlink to the already-imported working tree as the same location."""
    real_dir = tmp_path / "real_provider"
    real_dir.mkdir()
    (real_dir / "__init__.py").touch()
    link_dir = tmp_path / "provider"
    link_dir.symlink_to(real_dir, target_is_directory=True)
    already_imported = types.ModuleType(_PROVIDER_PKG)
    already_imported.__file__ = str(real_dir / "__init__.py")
    monkeypatch.setitem(sys.modules, _PROVIDER_PKG, already_imported)

    _alias_working_tree_provider(link_dir)

    assert sys.modules[_PROVIDER_PKG] is already_imported


def test_aliasing_unregisters_module_when_exec_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Leave no half-initialized module behind when the package fails to execute."""
    provider_dir = tmp_path / "provider"
    provider_dir.mkdir()
    (provider_dir / "__init__.py").write_text("raise ValueError('boom')\n")
    monkeypatch.delitem(sys.modules, _PROVIDER_PKG)

    with pytest.raises(ValueError, match="boom"):
        _alias_working_tree_provider(provider_dir)

    assert _PROVIDER_PKG not in sys.modules
