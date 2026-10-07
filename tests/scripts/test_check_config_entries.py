"""Tests for the ConfigEntry definition check."""

import ast

from scripts.check_config_entries import (
    _entry_key,
    _is_required_without_value,
    _options_entry_calls,
    main,
)


def _first_entry(source: str) -> ast.Call:
    """Return the first ``ConfigEntry(...)`` call node in a source snippet."""
    return next(
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "ConfigEntry"
    )


def _entry(*args: str) -> ast.Call:
    """Return the ConfigEntry call node for a snippet built from the given keyword arguments."""
    return _first_entry(f"ConfigEntry({', '.join(args)})")


def test_required_without_default_is_flagged() -> None:
    """An explicitly required entry without a default can never resolve to a value."""
    assert _is_required_without_value(_entry("key=CONF_TOKEN", "required=True")) is True


def test_implicitly_required_without_default_is_flagged() -> None:
    """``required`` defaults to True in the model, so omitting it is still required."""
    assert _is_required_without_value(_entry("key=CONF_TOKEN")) is True


def test_default_value_satisfies_required() -> None:
    """A required entry with a default resolves to that default."""
    assert (
        _is_required_without_value(_entry("key=CONF_LOCALE", "required=True", 'default_value="us"'))
        is False
    )
    assert _is_required_without_value(_entry("key=CONF_PLAYERS", "default_value=[]")) is False


def test_preset_value_satisfies_required() -> None:
    """An entry carrying a preset value resolves to that value."""
    assert _is_required_without_value(_entry("key=CONF_URL", 'value="http://localhost"')) is False


def test_explicit_none_default_is_flagged() -> None:
    """A literal ``None`` default leaves a required entry just as unresolvable."""
    assert _is_required_without_value(_entry("key=CONF_TOKEN", "default_value=None")) is True


def test_optional_entry_is_not_flagged() -> None:
    """An entry that is not required may resolve to nothing."""
    assert _is_required_without_value(_entry("key=CONF_PORT", "required=False")) is False


def test_computed_flags_are_not_flagged() -> None:
    """A computed ``required`` or ``default_value`` cannot be judged statically."""
    assert _is_required_without_value(_entry("key=CONF_SECRET", "required=not stored")) is False
    assert _is_required_without_value(_entry("key=CONF_TTS", "default_value=entities[0]")) is False


def test_ui_only_types_are_not_flagged() -> None:
    """UI-only entries hold no value: the model clears ``required`` for them."""
    assert (
        _is_required_without_value(_entry("key=CONF_ACTION", "type=ConfigEntryType.ACTION"))
        is False
    )
    assert (
        _is_required_without_value(_entry("key=CONF_NOTE", "type=ConfigEntryType.LABEL")) is False
    )
    # an explicit required=True on a UI-only type is honoured by the check, not silently ignored
    assert (
        _is_required_without_value(
            _entry("key=CONF_ACTION", "type=ConfigEntryType.ACTION", "required=True")
        )
        is True
    )


def test_only_get_config_entries_calls_are_collected() -> None:
    """Setup-flow form entries are required by design, so only options entries are collected."""
    source = (
        "_FORM = (ConfigEntry(key='token', type=ConfigEntryType.SECURE_STRING),)\n"
        "class P:\n"
        "    async def get_config_entries(self):\n"
        "        return (ConfigEntry(key='quality', default_value='high'),)\n"
        "    async def run_setup(self, session):\n"
        "        return ConfigEntry(key='password')\n"
    )
    tree = ast.parse(source)
    collected = _options_entry_calls(tree)
    assert len(collected) == 1
    keys = [
        _entry_key(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and id(node) in collected
    ]
    assert keys == ["'quality'"]


def test_entry_key_rendering() -> None:
    """A key is rendered from a literal, a constant reference or an attribute access."""
    assert _entry_key(_entry("key='locale'")) == "'locale'"
    assert _entry_key(_entry("key=CONF_LOCALE")) == "CONF_LOCALE"
    assert _entry_key(_entry("key=constants.CONF_LOCALE")) == "CONF_LOCALE"
    assert _entry_key(_entry("type=ConfigEntryType.STRING")) == "<unknown>"


def test_real_tree_is_clean() -> None:
    """The shipped tree defines no invalid config entries."""
    assert main() == 0
