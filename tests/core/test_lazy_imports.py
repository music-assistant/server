"""Guard the startup import graph against re-introducing heavy imports."""

import subprocess
import sys

# Heavy third-party modules that must stay off the startup import graph and only
# load on first use: numpy (audio analysis) and mutagen (local file tag scanning).
# PIL is intentionally NOT in this list — thumbnails are a universal feature, so it
# stays eagerly imported.
HEAVY_MODULES = ("numpy", "mutagen")


def _modules_loaded_by(import_statement: str) -> set[str]:
    """
    Return the dotted module names newly loaded by running the given import.

    Runs in a fresh interpreter and returns the delta of ``sys.modules`` around
    the import, so anything already present (e.g. preloaded via ``sitecustomize``)
    neither masks a regression nor trips the assertions.

    :param import_statement: A single import statement to execute.
    """
    code = (
        "import sys\n"
        "before = set(sys.modules)\n"
        f"{import_statement}\n"
        "print('\\n'.join(sorted(set(sys.modules) - before)))\n"
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    return set(result.stdout.split())


def test_server_import_defers_heavy_modules() -> None:
    """Importing the full server module must not import numpy or mutagen (PIL stays eager)."""
    top_level = {name.split(".")[0] for name in _modules_loaded_by("import music_assistant.mass")}
    assert not top_level.intersection(HEAVY_MODULES)


def test_package_root_import_is_lazy() -> None:
    """Importing a submodule through the package root must not drag in the server."""
    assert "music_assistant.mass" not in _modules_loaded_by("import music_assistant.constants")


def test_audio_analysis_model_is_numpy_free() -> None:
    """Constructing and (de)serializing AudioAnalysisData must not import numpy."""
    loaded = _modules_loaded_by(
        "from music_assistant.models.audio_analysis import AudioAnalysisData; "
        "AudioAnalysisData.from_dict({'loudness_integrated': -12.0, 'beats': [1.0, 2.0]}).to_dict()"
    )
    assert "numpy" not in {name.split(".")[0] for name in loaded}
