"""Shared fixtures and working-tree aliasing for Yandex Disk provider tests."""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

_PROVIDER_PKG = "music_assistant.providers.filesystem_yandex_disk"


def _alias_working_tree_provider(provider_dir: Path) -> None:
    """
    Alias the ``provider`` working-tree package onto the upstream import path.

    In the provider-repo layout, tests must exercise the working tree, not the
    provider snapshot baked into the venv's music_assistant install. In the
    upstream (inlined) layout there is no sibling ``provider/`` directory and
    the package under test IS the checkout itself, so the aliasing no-ops
    instead of failing collection.

    :param provider_dir: Path to the ``provider`` working-tree directory.
    """
    provider_dir = provider_dir.resolve()
    if not provider_dir.is_dir():
        return
    existing = sys.modules.get(_PROVIDER_PKG)
    if existing is not None:
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
        del sys.modules[_PROVIDER_PKG]
        raise
    parent = importlib.import_module("music_assistant.providers")
    setattr(parent, "filesystem_yandex_disk", module)  # noqa: B010


# The assignment + is_dir() shape (not a bare call argument) keeps this
# dereference visible to the rewrite-safe gate in CI.
_PROVIDER_DIR = Path(__file__).resolve().parent.parent / "provider"
if _PROVIDER_DIR.is_dir():
    _alias_working_tree_provider(_PROVIDER_DIR)
