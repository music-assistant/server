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


def test_tier_rejected() -> None:
    """A manifest that sets a tier is reported, whatever the value."""
    for tier in ("core", "community"):
        manifest = _valid_manifest()
        manifest["tier"] = tier
        assert _validate(manifest, "demo") == [
            "'tier' is set by the server and must not be in the manifest"
        ]


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


def _menu_action(**overrides: object) -> dict[str, object]:
    """Return a valid declared menu action, with fields changed."""
    return {
        "id": "send_to_frame",
        "label": "Show on the picture frame",
        "media_types": ["album", "playlist"],
        "scope": "library.read",
        "icon": "mdi-image-frame",
        "requires_player": False,
        **overrides,
    }


def test_valid_menu_actions_accepted() -> None:
    """Declared menu actions that follow the rules produce no issues, a null icon included."""
    manifest = _valid_manifest()
    manifest["menu_actions"] = [_menu_action(), _menu_action(id="play", icon=None)]
    assert _validate(manifest, "demo") == []


def test_menu_actions_with_wrong_types_rejected() -> None:
    """Menu actions of the wrong type are reported, as the server leaves them out."""
    manifest = _valid_manifest()
    manifest["menu_actions"] = {"id": "send_to_frame"}
    assert _validate(manifest, "demo") == ["'menu_actions' must be a list"]
    manifest["menu_actions"] = [_menu_action(label=None), _menu_action(requires_player="yes")]
    issues = _validate(manifest, "demo")
    assert [issue.split(":")[0] for issue in issues] == ["menu action 0", "menu action 1"]


def test_menu_actions_breaking_the_rules_rejected() -> None:
    """A built-in manifest whose menu actions break the server's rules fails the check."""
    manifest = _valid_manifest()
    manifest["menu_actions"] = [
        _menu_action(scope="everything"),
        _menu_action(id="unknown_type", media_types=["genre"]),
        _menu_action(id="twice"),
        _menu_action(id="twice"),
    ]
    assert _validate(manifest, "demo") == [
        "menu action 'send_to_frame': needs a scope that Music Assistant knows, such as "
        "'library.read'",
        "menu action 'unknown_type': needs media_types from: album, artist, audiobook, "
        "playlist, podcast, podcast_episode, radio, track",
        "menu action 'twice': another menu action of this provider has the same id",
    ]


def _mcp_tool(**overrides: object) -> dict[str, object]:
    """Return a valid declared MCP tool, with fields changed."""
    return {
        "id": "play_station",
        "description": "Play one of the user's saved stations of this service on a player.",
        "scope": "queues.control",
        "requires_player": True,
        "input_schema": {
            "type": "object",
            "properties": {"station": {"type": "string", "description": "The station."}},
            "required": ["station"],
        },
        **overrides,
    }


def test_valid_mcp_tools_accepted() -> None:
    """Declared MCP tools that follow the rules produce no issues, one without schema included."""
    manifest = _valid_manifest()
    without_schema = _mcp_tool(id="list_stations")
    del without_schema["input_schema"]
    manifest["mcp_tools"] = [_mcp_tool(), without_schema]
    assert _validate(manifest, "demo") == []


def test_mcp_tools_with_wrong_types_rejected() -> None:
    """MCP tools of the wrong type are reported, as the server leaves them out."""
    manifest = _valid_manifest()
    manifest["mcp_tools"] = {"id": "play_station"}
    assert _validate(manifest, "demo") == ["'mcp_tools' must be a list"]
    manifest["mcp_tools"] = [_mcp_tool(description=None), _mcp_tool(input_schema="none")]
    issues = _validate(manifest, "demo")
    assert [issue.split(":")[0] for issue in issues] == ["MCP tool 0", "MCP tool 1"]


def test_mcp_tools_breaking_the_rules_rejected() -> None:
    """A built-in manifest whose MCP tools break the server's rules fails the check."""
    manifest = _valid_manifest()
    manifest["mcp_tools"] = [
        _mcp_tool(scope="everything"),
        _mcp_tool(
            id="reserved",
            input_schema={
                "type": "object",
                "properties": {"player": {"type": "string", "description": "A player."}},
            },
        ),
        _mcp_tool(id="twice"),
        _mcp_tool(id="twice"),
    ]
    assert _validate(manifest, "demo") == [
        "MCP tool 'play_station': needs a scope that Music Assistant knows, such as 'library.read'",
        "MCP tool 'reserved': input_schema property 'player' is added by the server",
        "MCP tool 'twice': another MCP tool of this provider has the same id",
    ]


def test_mcp_tool_name_checked_with_the_folder_name() -> None:
    """The tool name limit is checked with the provider folder, the domain it must have."""
    folder = "d" * 40
    manifest = {**_valid_manifest(), "domain": folder, "mcp_tools": [_mcp_tool(id="x" * 23)]}
    assert _validate(manifest, folder) == [
        f"MCP tool '{'x' * 23}': needs a shorter id: the tool name '{folder}__{'x' * 23}' has "
        "more than 64 characters"
    ]
