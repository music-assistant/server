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
        "tier": "community",
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


def test_tier_required_for_public_provider() -> None:
    """A public provider without a tier is reported; a demo provider may omit it."""
    manifest = _valid_manifest()
    del manifest["tier"]
    assert any("missing mandatory key 'tier'" in issue for issue in _validate(manifest, "demo"))
    manifest["domain"] = "_demo_music_provider"
    assert _validate(manifest, "_demo_music_provider") == []


def test_invalid_tier_rejected() -> None:
    """A tier outside core/community is rejected."""
    for bad_tier in ("official", ["core"], None):
        manifest = _valid_manifest()
        manifest["tier"] = bad_tier
        assert any("is not one of" in issue for issue in _validate(manifest, "demo"))


def test_core_tier_requires_org_codeowner() -> None:
    """Tier core is only valid with @music-assistant in codeowners."""
    manifest = _valid_manifest()
    manifest["tier"] = "core"
    assert any("requires '@music-assistant'" in issue for issue in _validate(manifest, "demo"))
    manifest["codeowners"] = ["@music-assistant", "@octocat"]
    assert _validate(manifest, "demo") == []


def test_support_url_must_be_https() -> None:
    """A support_url must be an https URL with a host when present."""
    manifest = _valid_manifest()
    manifest["support_url"] = "https://github.com/octocat/demo/issues"
    assert _validate(manifest, "demo") == []
    bad_urls = (
        "http://example.com",
        "https://",
        "https:///issues",
        "https://?report",
        "https://#support",
        "https://[::1",
        "",
        None,
        ["https://example.com"],
    )
    for bad_url in bad_urls:
        manifest["support_url"] = bad_url
        assert any("'support_url' must be" in issue for issue in _validate(manifest, "demo"))


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
