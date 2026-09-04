"""
Wire the stub ``music_assistant`` package to the installed MA server code.

The root conftest registers ``music_assistant`` / ``music_assistant.providers``
as empty stub packages so ``provider/`` can be imported as
``music_assistant.providers.dlna_receiver`` without the full server. That stub
also shadows the *installed* ``music-assistant`` test dependency, which the
provider module needs for ``music_assistant.models.plugin`` and
``music_assistant.helpers.util``.

This conftest points the stub's ``__path__`` at the installed package so those
submodules import for real, and bypasses ``music_assistant/models/__init__.py``
(whose import chain pulls in server-only runtime deps like ``hass_client``)
with the same stub-package trick. In the upstream repo, where the genuine
package is importable, this is a no-op.
"""

from __future__ import annotations

import pathlib
import sys
import types


def _wire_installed_music_assistant() -> None:
    stub = sys.modules.get("music_assistant")
    if stub is None or getattr(stub, "__file__", None) is not None:
        # Real package (upstream repo) — nothing to do.
        return
    try:
        import music_assistant_models  # noqa: PLC0415
    except ImportError:
        return
    assert music_assistant_models.__file__ is not None
    site_packages = pathlib.Path(music_assistant_models.__file__).parent.parent
    real_pkg = site_packages / "music_assistant"
    if not real_pkg.is_dir():
        return
    if str(real_pkg) not in stub.__path__:
        stub.__path__.append(str(real_pkg))
    if "music_assistant.models" not in sys.modules:
        models_pkg = types.ModuleType("music_assistant.models")
        models_pkg.__path__ = [str(real_pkg / "models")]
        models_pkg.__package__ = "music_assistant.models"
        sys.modules["music_assistant.models"] = models_pkg


_wire_installed_music_assistant()
