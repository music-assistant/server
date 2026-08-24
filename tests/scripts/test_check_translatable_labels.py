"""Tests for the translatable browse/recommendation label check."""

import ast

from scripts.check_translatable_labels import (
    PROVIDERS_PATH,
    _is_hardcoded_label,
    _is_shipped_provider_file,
    _resolve_python_files,
    find_violations,
)


def _first_call(source: str) -> ast.Call:
    """Return the first ``ast.Call`` node in a source snippet."""
    return next(node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Call))


def test_flags_literal_name_without_translation_key() -> None:
    """A folder built with a literal ``name`` and no ``translation_key`` is flagged."""
    assert _is_hardcoded_label(_first_call('BrowseFolder(item_id="x", name="Top Charts")'))
    assert _is_hardcoded_label(_first_call('RecommendationFolder(name="For You")'))


def test_allows_literal_name_with_translation_key() -> None:
    """A literal name paired with a ``translation_key`` is acceptable."""
    call = _first_call('BrowseFolder(name="Top Charts", translation_key="top_charts")')
    assert not _is_hardcoded_label(call)


def test_ignores_dynamic_names() -> None:
    """A name built from data (variable, attribute, f-string) is real content, not a label."""
    assert not _is_hardcoded_label(_first_call("BrowseFolder(name=artist.name)"))
    assert not _is_hardcoded_label(_first_call("BrowseFolder(name=folder_title)"))
    assert not _is_hardcoded_label(_first_call('BrowseFolder(name=f"Radio: {title}")'))


def test_ignores_other_constructors() -> None:
    """Constructors that are not browse/recommendation folders are not checked here."""
    assert not _is_hardcoded_label(_first_call('Album(name="Greatest Hits")'))


def test_real_tree_is_clean() -> None:
    """No shipped provider hardcodes a browse/recommendation folder label."""
    assert find_violations() == []


def test_is_shipped_provider_file_skips_templates() -> None:
    """Template/test providers and out-of-tree paths are not shipped provider files."""
    assert _is_shipped_provider_file(PROVIDERS_PATH / "spotify" / "__init__.py")
    assert not _is_shipped_provider_file(PROVIDERS_PATH / "_demo_music_provider" / "__init__.py")
    assert not _is_shipped_provider_file(PROVIDERS_PATH / "test" / "__init__.py")


def test_resolve_python_files_skips_template_folder() -> None:
    """Resolving a template provider folder yields no files to scan."""
    assert _resolve_python_files([str(PROVIDERS_PATH / "_demo_music_provider")]) == []


def test_explicit_empty_paths_scans_nothing() -> None:
    """An explicit empty path list scans nothing (no fallback to a full scan)."""
    assert find_violations([]) == []
