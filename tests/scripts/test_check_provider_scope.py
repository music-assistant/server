"""Tests for the advisory provider-scope check."""

from scripts.check_provider_scope import classify, provider_name


def test_provider_name_detects_package_and_tests() -> None:
    """Provider package and provider test paths resolve to the provider name."""
    assert provider_name("music_assistant/providers/spotify/__init__.py") == "spotify"
    assert provider_name("tests/providers/spotify/test_spotify.py") == "spotify"
    assert provider_name("music_assistant/helpers/audio.py") is None
    assert provider_name("README.md") is None


def test_single_provider_only_is_in_scope() -> None:
    """A change confined to one provider (and its tests) reports no shared files."""
    providers, shared = classify(
        [
            "music_assistant/providers/spotify/__init__.py",
            "tests/providers/spotify/test_spotify.py",
        ]
    )
    assert providers == {"spotify"}
    assert shared == []


def test_generated_artifacts_are_not_shared() -> None:
    """Generated requirements/translations that accompany a provider change are ignored."""
    providers, shared = classify(
        [
            "music_assistant/providers/spotify/manifest.json",
            "requirements_all.txt",
            "music_assistant/translations/en.json",
        ]
    )
    assert providers == {"spotify"}
    assert shared == []


def test_shared_code_alongside_provider_is_reported() -> None:
    """Editing shared server code in a provider PR surfaces those files."""
    providers, shared = classify(
        [
            "music_assistant/providers/spotify/__init__.py",
            "music_assistant/helpers/audio.py",
            "tests/helpers/test_audio.py",
        ]
    )
    assert providers == {"spotify"}
    assert shared == ["music_assistant/helpers/audio.py", "tests/helpers/test_audio.py"]


def test_no_provider_change_reports_nothing() -> None:
    """A pure core change has no provider context, so nothing is reported as out-of-scope."""
    providers, shared = classify(["music_assistant/helpers/audio.py"])
    assert providers == set()
    assert shared == ["music_assistant/helpers/audio.py"]
