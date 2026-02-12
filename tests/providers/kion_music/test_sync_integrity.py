"""Integration tests for Kion Music provider synchronization integrity.

Verifies that synced Kion provider maintains correct branding,
experimental feature settings, and API endpoints.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

KION_DIR = Path("music_assistant/providers/kion_music")


def test_no_yandex_branding() -> None:
    """Ensure Kion has no Yandex branding after sync.

    Note: API protocol strings like "YandexMusicDesktopAppWindows" are preserved
    as they are required by the API server.
    """
    for py_file in KION_DIR.glob("*.py"):
        content = py_file.read_text()

        # Filter out library imports (these should use yandex_music library)
        lines = [
            line
            for line in content.split("\n")
            if not line.strip().startswith(("from yandex_music", "import yandex_music"))
        ]
        clean_content = "\n".join(lines)

        # Filter out API protocol strings (required by server)
        # e.g., "YandexMusicDesktopAppWindows" is a client identifier
        clean_content = clean_content.replace('"YandexMusicDesktopAppWindows"', "")
        clean_content = clean_content.replace("'YandexMusicDesktopAppWindows'", "")

        # Check for Yandex branding in actual code (not imports or API strings)
        assert "YandexMusic" not in clean_content, (
            f"Found YandexMusic class/variable in {py_file.name}"
        )
        assert "Yandex Music provider" not in clean_content, (
            f"Found 'Yandex Music provider' in {py_file.name}"
        )


def test_my_mix_branding() -> None:
    """Ensure My Wave was renamed to My Mix in Kion."""
    kion_files = list(KION_DIR.glob("*.py"))

    for py_file in kion_files:
        content = py_file.read_text()

        # My Wave should be renamed to My Mix
        assert "My Wave" not in content, f"Found 'My Wave' in {py_file.name}"
        assert "Моя волна" not in content, f"Found 'Моя волна' in {py_file.name}"

        # If file has mix-related code, verify correct branding
        if "my_mix" in content.lower() or "мой микс" in content.lower():
            # At least one of the correct brandings should exist
            has_my_mix = "My Mix" in content or "my_mix" in content
            has_russian = "Мой Микс" in content
            assert has_my_mix or has_russian, f"File {py_file.name} has mix code but wrong branding"


def test_my_mix_constants() -> None:
    """Verify My Wave constants were renamed to My Mix."""
    constants_file = KION_DIR / "constants.py"

    if not constants_file.exists():
        pytest.skip("constants.py not found")

    content = constants_file.read_text()

    # Check that My Wave constants don't exist
    assert "CONF_MY_WAVE_" not in content, "Found CONF_MY_WAVE_ constants"
    assert "MY_WAVE_PLAYLIST" not in content, "Found MY_WAVE_PLAYLIST constant"
    assert "ROTOR_STATION_MY_WAVE" not in content, "Found ROTOR_STATION_MY_WAVE"

    # If file has my_mix constants, verify they're correctly named
    if "my_mix" in content.lower():
        # Should have My Mix constants
        my_mix_constant_found = (
            "CONF_MY_MIX_" in content
            or "MY_MIX_PLAYLIST" in content
            or "ROTOR_STATION_MY_MIX" in content
        )
        assert my_mix_constant_found, "My Mix constants not properly renamed"


def test_experimental_features_disabled() -> None:
    """Verify experimental features are disabled by default."""
    init_file = KION_DIR / "__init__.py"

    if not init_file.exists():
        pytest.skip("__init__.py not found")

    content = init_file.read_text()

    # Check for experimental feature config entries
    if "CONF_ENABLE_RECOMMENDATIONS" in content:
        # Should have default_value=False or default=False
        has_false_default = "default_value=False" in content or "default=False" in content
        assert has_false_default, "CONF_ENABLE_RECOMMENDATIONS should default to False"

    if "CONF_ENABLE_MY_MIX" in content or "CONF_ENABLE_MY_WAVE" in content:
        # Should have default_value=False
        has_false_default = "default_value=False" in content or "default=False" in content
        assert has_false_default, "My Mix feature should default to False"

    # Check for experimental warnings
    if "enable_recommendations" in content.lower() or "enable_my" in content.lower():
        has_experimental_marker = "Experimental" in content or "experimental" in content
        assert has_experimental_marker, "Experimental features should be marked as such"


def test_kion_api_endpoint_preserved() -> None:
    """Verify KION uses correct API endpoint."""
    api_client = KION_DIR / "api_client.py"
    constants = KION_DIR / "constants.py"

    if not api_client.exists() or not constants.exists():
        pytest.skip("api_client.py or constants.py not found")

    api_content = api_client.read_text()
    constants_content = constants.read_text()

    # Check for KION API endpoint (updated to ya_proxy_api)
    assert "music.mts.ru/ya_proxy_api" in constants_content, (
        "KION API endpoint missing or incorrect in constants.py"
    )

    # Check for DEFAULT_BASE_URL constant (replaces KION_BASE_URL)
    assert "DEFAULT_BASE_URL" in constants_content, "DEFAULT_BASE_URL constant not found"

    # Verify Yandex endpoint not present in api_client
    assert "api.music.yandex.net" not in api_content, "Found Yandex API endpoint (should be KION)"


def test_kion_manifest_correct() -> None:
    """Verify manifest.json is correct for KION."""
    manifest_file = KION_DIR / "manifest.json"

    if not manifest_file.exists():
        pytest.skip("manifest.json not found")

    manifest = json.loads(manifest_file.read_text())

    # Check domain
    assert manifest["domain"] == "kion_music", "Domain should be 'kion_music'"

    # Check name
    assert manifest["name"] == "KION Music", "Name should be 'KION Music'"

    # Check description mentions KION or MTS
    description = manifest.get("description", "")
    assert "KION" in description or "MTS" in description, "Description should mention KION or MTS"

    # Check documentation URL
    documentation = manifest.get("documentation", "")
    if documentation:
        assert "kion-music" in documentation.lower(), (
            "Documentation URL should reference kion-music"
        )


def test_kion_provider_class_exists() -> None:
    """Verify KionMusicProvider class exists."""
    provider_file = KION_DIR / "provider.py"

    if not provider_file.exists():
        pytest.skip("provider.py not found")

    content = provider_file.read_text()

    # Should have KionMusicProvider class
    assert "class KionMusicProvider" in content, "KionMusicProvider class not found"

    # Should not have YandexMusicProvider class
    assert "class YandexMusicProvider" not in content, (
        "Found YandexMusicProvider class (should be KionMusicProvider)"
    )


def test_kion_client_class_exists() -> None:
    """Verify KionMusicClient class exists."""
    api_client_file = KION_DIR / "api_client.py"

    if not api_client_file.exists():
        pytest.skip("api_client.py not found")

    content = api_client_file.read_text()

    # Should have KionMusicClient class
    assert "class KionMusicClient" in content, "KionMusicClient class not found"

    # Should not have YandexMusicClient class
    assert "class YandexMusicClient" not in content, (
        "Found YandexMusicClient class (should be KionMusicClient)"
    )


def test_no_yandex_references_in_comments() -> None:
    """Verify no Yandex references in comments and docstrings."""
    for py_file in KION_DIR.glob("*.py"):
        content = py_file.read_text()

        # Allow library imports
        lines = [
            line
            for line in content.split("\n")
            if not line.strip().startswith(("from yandex_music", "import yandex_music"))
        ]

        # Check comments and docstrings don't reference Yandex provider
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("#") or '"""' in line or "'''" in line:
                # Allow "yandex-music" (library name) but not "Yandex Music" (service)
                if "Yandex Music" in line and "yandex-music" not in line.lower():
                    pytest.fail(f"Found 'Yandex Music' in comment/docstring at {py_file.name}:{i}")


def test_kion_imports_yandex_music_library() -> None:
    """Verify Kion still imports yandex_music library (correct behavior).

    Both Yandex Music and Kion Music providers use the same yandex_music library,
    just with different API endpoints. Library imports should be preserved.
    """
    # Check all Python files in Kion provider
    kion_files = list(KION_DIR.glob("*.py"))

    if not kion_files:
        pytest.skip("No Python files found in Kion provider")

    # Should find yandex_music imports in at least one file (typically api_client.py)
    has_import = False
    for py_file in kion_files:
        content = py_file.read_text()
        if "from yandex_music" in content or "import yandex_music" in content:
            has_import = True
            break

    assert has_import, "Should import yandex_music library (shared dependency) in at least one file"
