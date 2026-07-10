"""Guard the startup import graph against re-introducing heavy imports."""

import subprocess
import sys

# Heavy third-party modules that must only be imported on first use
# (audio analysis, image processing, local file tag scanning).
HEAVY_MODULES = ("numpy", "PIL", "mutagen")


def _loaded_modules_after(import_statement: str) -> set[str]:
    """Return the top-level module names loaded by running the given import."""
    code = (
        f"import sys; {import_statement}; print('\\n'.join(m.split('.')[0] for m in sys.modules))"
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    return set(result.stdout.split())


def test_server_import_defers_heavy_modules() -> None:
    """Importing the full server module must not import numpy, PIL or mutagen."""
    loaded = _loaded_modules_after("import music_assistant.mass")
    assert not loaded.intersection(HEAVY_MODULES)


def test_package_root_import_is_lazy() -> None:
    """Importing a submodule through the package root must not drag in the server."""
    code = (
        "import sys; import music_assistant.constants; print('music_assistant.mass' in sys.modules)"
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "False"
