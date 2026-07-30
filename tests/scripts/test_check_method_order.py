"""Tests for the method-ordering check."""

import ast

from scripts.check_method_order import _class_violations, _is_private, main


def _first_class(source: str) -> ast.ClassDef:
    """Return the first ``ast.ClassDef`` node in a source snippet."""
    return next(node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.ClassDef))


def _names(source: str) -> list[str]:
    """Return the names of the misplaced methods in the first class of a snippet."""
    return [name for _lineno, name in _class_violations(_first_class(source))]


def test_is_private_classification() -> None:
    """Underscore-prefixed non-dunder names are private; dunders and public names are not."""
    assert _is_private("_helper") is True
    assert _is_private("__mangled") is True
    assert _is_private("__init__") is False
    assert _is_private("__repr__") is False
    assert _is_private("public") is False


def test_public_below_private_is_flagged() -> None:
    """A public method defined after a private one is reported."""
    source = (
        "class C:\n"
        "    def public_a(self): ...\n"
        "    def _helper(self): ...\n"
        "    def public_b(self): ...\n"
    )
    assert _names(source) == ["public_b"]


def test_dunder_below_private_is_flagged() -> None:
    """A dunder defined after a private method is reported (dunders belong at the top)."""
    source = "class C:\n    def _helper(self): ...\n    def __repr__(self): ...\n"
    assert _names(source) == ["__repr__"]


def test_private_methods_at_bottom_is_clean() -> None:
    """Public and dunder methods above private methods produce no violations."""
    source = (
        "class C:\n"
        "    def __init__(self): ...\n"
        "    def public(self): ...\n"
        "    def _helper_a(self): ...\n"
        "    def _helper_b(self): ...\n"
    )
    assert _names(source) == []


def test_nested_function_is_ignored() -> None:
    """A function nested inside a method does not affect the class ordering check."""
    source = (
        "class C:\n"
        "    def _helper(self):\n"
        "        def inner(): ...\n"
        "        return inner\n"
        "    def public(self): ...\n"
    )
    assert _names(source) == ["public"]


def test_real_tree_matches_baseline() -> None:
    """The shipped tree has no misplaced methods beyond the grandfathered baseline."""
    assert main([]) == 0
