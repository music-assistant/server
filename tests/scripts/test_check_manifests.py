"""Tests for the provider manifest.json validator."""

from scripts.check_manifests import PROVIDERS_PATH, _resolve_manifests, _validate, find_violations


def _valid_manifest() -> dict[str, object]:
    """Return a minimal valid manifest dict for the ``demo`` provider folder."""
    return {
        "type": "music",
        "domain": "demo",
        "name": "Demo",
        "description": "A demo provider.",
        "codeowners": ["@octocat"],
    }


def test_valid_manifest_has_no_issues() -> None:
    """A complete, consistent manifest produces no issues."""
    assert _validate(_valid_manifest(), "demo") == []


def test_missing_mandatory_keys() -> None:
    """Each absent mandatory key is reported."""
    issues = _validate({"type": "music", "domain": "demo"}, "demo")
    assert any("missing mandatory key 'name'" in issue for issue in issues)
    assert any("missing mandatory key 'description'" in issue for issue in issues)
    assert any("missing mandatory key 'codeowners'" in issue for issue in issues)


def test_invalid_type() -> None:
    """An unknown provider type is rejected."""
    manifest = _valid_manifest()
    manifest["type"] = "jukebox"
    assert any("is not one of" in issue for issue in _validate(manifest, "demo"))


def test_non_string_type_rejected() -> None:
    """A non-string type (list/dict) is reported instead of crashing the validator."""
    for bad_type in (["music"], {"type": "music"}):
        manifest = _valid_manifest()
        manifest["type"] = bad_type
        assert any("is not one of" in issue for issue in _validate(manifest, "demo"))


def test_domain_must_match_folder() -> None:
    """A domain that differs from the folder name is reported."""
    assert any(
        "must match the provider folder name" in issue
        for issue in _validate(_valid_manifest(), "other_folder")
    )


def test_codeowners_must_be_list() -> None:
    """A bare-string codeowners value (not a list) is rejected."""
    manifest = _valid_manifest()
    manifest["codeowners"] = "@octocat"
    assert any("must be a non-empty list" in issue for issue in _validate(manifest, "demo"))


def test_codeowners_entries_need_at_sign() -> None:
    """Codeowner handles must start with ``@``."""
    manifest = _valid_manifest()
    manifest["codeowners"] = ["octocat"]
    assert any(
        "must be a string starting with '@'" in issue for issue in _validate(manifest, "demo")
    )


def test_codeowners_null_rejected() -> None:
    """A present-but-null codeowners value is rejected, not silently accepted."""
    manifest = _valid_manifest()
    manifest["codeowners"] = None
    issues = _validate(manifest, "demo")
    assert [issue for issue in issues if "must be a non-empty list" in issue]
    assert not [issue for issue in issues if "missing mandatory key 'codeowners'" in issue]


def test_empty_text_fields_rejected() -> None:
    """Blank name/description values are reported."""
    manifest = _valid_manifest()
    manifest["name"] = "   "
    assert any(
        "'name' must be a non-empty string" in issue for issue in _validate(manifest, "demo")
    )


def test_real_tree_is_clean() -> None:
    """Every shipped provider manifest passes validation."""
    assert find_violations() == []


def test_resolve_manifests_from_folder() -> None:
    """A provider folder argument resolves to that provider's manifest.json."""
    folder = PROVIDERS_PATH / "_demo_music_provider"
    assert _resolve_manifests([str(folder)]) == [folder / "manifest.json"]


def test_resolve_manifests_ignores_non_manifest_paths() -> None:
    """Arguments that are not manifests (or outside providers) are ignored."""
    init_file = PROVIDERS_PATH / "_demo_music_provider" / "__init__.py"
    assert _resolve_manifests([str(init_file)]) == []


def test_find_violations_scoped_to_given_manifest() -> None:
    """Validating a single explicit manifest only inspects that manifest."""
    manifest = PROVIDERS_PATH / "_demo_music_provider" / "manifest.json"
    assert find_violations([manifest]) == []
