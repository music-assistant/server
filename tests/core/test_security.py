"""Tests for security helpers."""

from music_assistant.helpers.security import is_safe_name, is_safe_path


def test_is_safe_path_valid() -> None:
    """Safe paths with no traversal components are allowed."""
    assert is_safe_path("valid/path") is True
    assert is_safe_path("just_a_name") is True
    assert is_safe_path("a/b/c") is True
    assert is_safe_path("/absolute/path") is True


def test_is_safe_path_traversal() -> None:
    """Paths with traversal components are rejected."""
    assert is_safe_path("../parent") is False
    assert is_safe_path("../../double") is False


def test_is_safe_path_embedded_traversal() -> None:
    """Relative paths with traversal are rejected; absolute ones are normalised by normpath."""
    # normpath resolves absolute traversal to a clean path - considered safe
    assert is_safe_path("/valid/../traversal") is True
    # relative paths with embedded traversal are rejected
    assert is_safe_path("valid/../../escape") is False


def test_is_safe_name_valid() -> None:
    """Simple names without separators or traversal are safe."""
    assert is_safe_name("valid_name") is True
    assert is_safe_name("filename.mp3") is True
    assert is_safe_name("track123") is True


def test_is_safe_name_forward_slash() -> None:
    """Names with forward slashes are rejected."""
    assert is_safe_name("path/with/slash") is False


def test_is_safe_name_backslash() -> None:
    """Names with backslashes are rejected."""
    assert is_safe_name("path\\with\\backslash") is False


def test_is_safe_name_dotdot() -> None:
    """Names with '..' are rejected."""
    assert is_safe_name("..") is False
    assert is_safe_name("abc..def") is False
