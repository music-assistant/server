"""Unit tests for genre helper functions (_normalize_genre_name)."""

from music_assistant.controllers.media.genres import GenreController

_normalize = GenreController._normalize_genre_name


def test_normalize_basic() -> None:
    """Test basic genre name normalization."""
    result = _normalize("Rock")
    assert result == ("Rock", "Rock", "rock", "rock")


def test_normalize_with_spaces() -> None:
    """Test normalization of multi-word genre name."""
    result = _normalize("Classic Rock")
    assert result is not None
    name, sort_name, search_name, search_sort_name = result
    assert name == "Classic Rock"
    assert sort_name == "Classic Rock"
    # replace_space=True strips spaces from search names
    assert search_name == "classicrock"
    assert search_sort_name == "classicrock"


def test_normalize_strips_whitespace() -> None:
    """Test that leading/trailing whitespace is stripped."""
    result = _normalize("  Jazz  ")
    assert result is not None
    name, sort_name, search_name, search_sort_name = result
    assert name == "Jazz"
    assert sort_name == "Jazz"
    assert search_name == "jazz"
    assert search_sort_name == "jazz"


def test_normalize_empty_string() -> None:
    """Test that empty string returns None."""
    assert _normalize("") is None


def test_normalize_whitespace_only() -> None:
    """Test that whitespace-only string returns None."""
    assert _normalize("   ") is None


def test_normalize_special_characters() -> None:
    """Test diacritics are handled via create_safe_string (unidecode)."""
    result = _normalize("Café")
    assert result is not None
    name, _sort_name, search_name, _search_sort_name = result
    assert name == "Café"
    assert search_name == "cafe"


def test_normalize_unicode() -> None:
    """Test unicode transliteration."""
    result = _normalize("Electrónica")
    assert result is not None
    name, _sort_name, search_name, _search_sort_name = result
    assert name == "Electrónica"
    assert search_name == "electronica"


def test_normalize_ampersand() -> None:
    """Test R&B search_name normalization (& is stripped)."""
    result = _normalize("R&B")
    assert result is not None
    name, _sort_name, search_name, _search_sort_name = result
    assert name == "R&B"
    assert search_name == "rb"


def test_normalize_slash() -> None:
    """Test compound genre name with special chars."""
    result = _normalize("Drum & Bass / Jungle")
    assert result is not None
    name, _sort_name, search_name, _search_sort_name = result
    assert name == "Drum & Bass / Jungle"
    # All non-alphanumeric chars (including spaces, &, /) are stripped
    assert search_name == "drumbassjungle"


def test_normalize_returns_four_tuple() -> None:
    """Test that a valid input always returns a tuple of exactly 4 strings."""
    result = _normalize("Pop")
    assert result is not None
    assert isinstance(result, tuple)
    assert len(result) == 4
    assert all(isinstance(s, str) for s in result)
