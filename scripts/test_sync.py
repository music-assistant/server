"""Unit tests for provider synchronization script."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.sync_kion_from_yandex import ProviderSynchronizer

CONFIG_PATH = Path(".github/sync-config.yml")


@pytest.fixture
def synchronizer() -> ProviderSynchronizer:
    """Create synchronizer instance for testing."""
    return ProviderSynchronizer(CONFIG_PATH)


def test_transformation_rules(synchronizer: ProviderSynchronizer) -> None:
    """Test transformation rules work correctly."""
    test_cases = [
        ("class YandexMusicProvider:", "class KionMusicProvider:"),
        ('"Yandex Music"', '"KION Music"'),
        ("YandexMusicClient", "KionMusicClient"),
        ("YandexMusicStreamingManager", "KionMusicStreamingManager"),
    ]

    for input_text, expected in test_cases:
        output = synchronizer._apply_transformations(input_text, "test.py")
        assert expected in output, f"Expected '{expected}' in output for '{input_text}'"
        assert input_text not in output, f"Input '{input_text}' should not be in output"


def test_my_mix_branding(synchronizer: ProviderSynchronizer) -> None:
    """Test My Wave → My Mix rebranding."""
    yandex_text = """
    async def get_my_wave_tracks(self):
        '''Get tracks from My Wave (Моя волна).'''
        return await self.client.get_my_wave()
    """

    output = synchronizer._apply_transformations(yandex_text, "provider.py")

    # Check My Mix branding
    assert "My Mix" in output, "Should contain 'My Mix'"
    assert "Мой Микс" in output, "Should contain 'Мой Микс'"
    assert "get_my_mix_tracks" in output, "Should contain 'get_my_mix_tracks'"

    # Check My Wave removed
    assert "My Wave" not in output, "Should not contain 'My Wave'"
    assert "Моя волна" not in output, "Should not contain 'Моя волна'"


def test_my_wave_constants(synchronizer: ProviderSynchronizer) -> None:
    """Test My Wave constant renaming."""
    input_text = """
    CONF_MY_WAVE_MAX_TRACKS = "my_wave_max_tracks"
    CONF_MY_WAVE_BATCH_SIZE = "my_wave_batch_size"
    ROTOR_STATION_MY_WAVE = "user:onyourwave"
    MY_WAVE_PLAYLIST_ID = "daily-playlist"
    """

    output = synchronizer._apply_transformations(input_text, "constants.py")

    # Check renamed constants
    assert "CONF_MY_MIX_MAX_TRACKS" in output
    assert "CONF_MY_MIX_BATCH_SIZE" in output
    assert "ROTOR_STATION_MY_MIX" in output
    assert "MY_MIX_PLAYLIST_ID" in output

    # Check originals removed
    assert "CONF_MY_WAVE_MAX_TRACKS" not in output
    assert "MY_WAVE_PLAYLIST_ID" not in output


def test_experimental_feature_defaults(synchronizer: ProviderSynchronizer) -> None:
    """Test that experimental features are disabled by default."""
    input_text = """
    ConfigEntry(
        key=CONF_ENABLE_RECOMMENDATIONS,
        default_value=True,
        description="Enable recommendations"
    )
    """

    output = synchronizer._apply_transformations(input_text, "__init__.py")

    # Should change to False and add experimental marker
    assert "default_value=False" in output, "Experimental features should default to False"
    assert "Experimental" in output, "Should add experimental marker"


def test_api_endpoint_transformation(synchronizer: ProviderSynchronizer) -> None:
    """Test API endpoint is correctly transformed."""
    input_text = 'GET_FILE_INFO_BASE_URL = "https://api.music.yandex.net"'
    output = synchronizer._apply_transformations(input_text, "api_client.py")

    assert "music.mts.ru/ya_api" in output, "Should use KION API endpoint"
    assert "KION_BASE_URL" in output, "Should use KION_BASE_URL constant"
    assert "api.music.yandex.net" not in output, "Should not contain Yandex API endpoint"


def test_api_endpoint_usage(synchronizer: ProviderSynchronizer) -> None:
    """Test API endpoint constant usage is transformed."""
    input_text = "base_url=GET_FILE_INFO_BASE_URL"
    output = synchronizer._apply_transformations(input_text, "api_client.py")

    assert "base_url=KION_BASE_URL" in output


def test_manifest_transformations(synchronizer: ProviderSynchronizer) -> None:
    """Test manifest.json transformations."""
    input_text = """
    {
      "domain": "yandex_music",
      "name": "Yandex Music",
      "description": "Stream music from Yandex Music service.",
      "documentation": "https://music-assistant.io/music-providers/yandex-music/"
    }
    """

    output = synchronizer._apply_transformations(input_text, "manifest.json")

    assert '"domain": "kion_music"' in output
    assert '"name": "KION Music"' in output
    assert "KION Music (MTS)" in output
    assert "kion-music" in output


def test_class_name_transformations(synchronizer: ProviderSynchronizer) -> None:
    """Test all class name transformations."""
    input_text = """
    class YandexMusicProvider:
        def __init__(self):
            self.client = YandexMusicClient()
            self.streaming = YandexMusicStreamingManager()
    """

    output = synchronizer._apply_transformations(input_text, "provider.py")

    assert "class KionMusicProvider:" in output
    assert "KionMusicClient()" in output
    assert "KionMusicStreamingManager()" in output
    assert "YandexMusic" not in output


def test_provider_name_in_docstrings(synchronizer: ProviderSynchronizer) -> None:
    """Test provider name transformations in docstrings."""
    input_text = '''
    """Yandex Music provider for Music Assistant.

    Provides access to the Yandex Music service.
    Uses the Yandex API for streaming.
    """
    '''

    output = synchronizer._apply_transformations(input_text, "provider.py")

    assert "KION Music provider" in output
    assert (
        "KION Music (MTS) service" in output
    )  # "Yandex Music service" → "KION Music (MTS) service"
    assert "KION API" in output
    assert "Yandex" not in output


def test_multiple_transformations_order(synchronizer: ProviderSynchronizer) -> None:
    """Test that transformations are applied in correct order."""
    input_text = "YandexMusicProvider class with My Wave feature"
    output = synchronizer._apply_transformations(input_text, "provider.py")

    # All transformations should be applied
    assert "KionMusicProvider" in output
    assert "My Mix" in output
    assert "YandexMusicProvider" not in output
    assert "My Wave" not in output


def test_preserve_yandex_music_library_import(
    synchronizer: ProviderSynchronizer,
) -> None:
    """Test that yandex-music library imports are preserved.

    Both Yandex Music and Kion Music providers use the same yandex_music library,
    just with different API endpoints. Library imports should NOT be transformed.
    """
    input_text = """
    from yandex_music import Client, Track
    from yandex_music.exceptions import NetworkError
    import yandex_music
    """

    output = synchronizer._apply_transformations(input_text, "api_client.py")

    # Library imports should remain unchanged (both providers use yandex_music library)
    assert "from yandex_music import Client, Track" in output
    assert "from yandex_music.exceptions import NetworkError" in output
    assert "import yandex_music" in output

    # But class names should still be transformed
    provider_code = "class YandexMusicProvider"
    provider_output = synchronizer._apply_transformations(provider_code, "provider.py")
    assert "KionMusicProvider" in provider_output


def test_config_load_error() -> None:
    """Test error handling for missing config file."""
    with pytest.raises(FileNotFoundError):
        ProviderSynchronizer(Path("nonexistent.yml"))


def test_transformation_count(synchronizer: ProviderSynchronizer) -> None:
    """Test that transformation count is tracked correctly."""
    input_text = "YandexMusicProvider with Yandex Music and My Wave"
    synchronizer._apply_transformations(input_text, "provider.py")

    # Should track number of transformations applied
    assert synchronizer.stats["transformations_applied"] > 0
