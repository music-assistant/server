"""Tests verifying that importing vendored_clap does not pollute the process warnings filter."""

from __future__ import annotations

import ast
import importlib
import inspect
import warnings
from pathlib import Path

import music_assistant.providers.sonic_analysis.vendored_clap.clap_wrapper as cw_module


def test_import_does_not_add_unscoped_ignore_all_filter() -> None:
    """
    Importing clap_wrapper must not inject a process-wide ignore-all entry into warnings.filters.

    An unscoped filterwarnings("ignore") at module level would silently suppress
    all subsequent warnings across every other MA provider for the lifetime of the
    process.  The filter must live inside a with-catch_warnings block in load_clap().
    """
    filters_before = list(warnings.filters)

    # Force a fresh evaluation of the module-level code.  importlib.reload re-executes
    # all module-level statements, so any bare filterwarnings("ignore") call would fire.
    importlib.reload(cw_module)

    filters_after = list(warnings.filters)

    # The reload must not have added an unscoped "ignore all" entry.
    new_entries = [f for f in filters_after if f not in filters_before]
    unscoped_ignore_all = [
        f
        for f in new_entries
        if f[0] == "ignore" and f[2] is Warning and f[1] is None and f[3] is None
    ]
    assert unscoped_ignore_all == [], (
        f"clap_wrapper module-level code installed a process-wide ignore-all filter: "
        f"{unscoped_ignore_all}. "
        f"Use 'with warnings.catch_warnings(): warnings.filterwarnings(\"ignore\")' "
        f"inside load_clap() instead."
    )


def test_no_module_level_filterwarnings_call_in_source() -> None:
    """
    The clap_wrapper source must not contain a bare filterwarnings call at module scope.

    Parses the AST to detect any top-level Expr containing a call to
    warnings.filterwarnings(), which would install a permanent process-wide filter.
    """
    clap_wrapper_path = (
        Path(__file__).parent.parent.parent.parent
        / "music_assistant"
        / "providers"
        / "sonic_analysis"
        / "vendored_clap"
        / "clap_wrapper.py"
    )
    source = clap_wrapper_path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    for node in ast.iter_child_nodes(tree):
        # We only care about top-level expression statements (bare calls)
        if not isinstance(node, ast.Expr):
            continue
        call = node.value
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        # Matches both `warnings.filterwarnings(...)` and bare `filterwarnings(...)`
        is_filterwarnings = (isinstance(func, ast.Attribute) and func.attr == "filterwarnings") or (
            isinstance(func, ast.Name) and func.id == "filterwarnings"
        )
        assert not is_filterwarnings, (
            f"clap_wrapper.py has a module-level call to filterwarnings() at line {node.lineno}. "
            f"Move it inside 'with warnings.catch_warnings():' in load_clap()."
        )


def test_load_clap_uses_catch_warnings_context_manager() -> None:
    """load_clap() must wrap model-loading code in a with warnings.catch_warnings() block."""
    source = inspect.getsource(cw_module.CLAPWrapper.load_clap)
    assert "catch_warnings" in source, (
        "CLAPWrapper.load_clap() does not use warnings.catch_warnings(). "
        "The filterwarnings('ignore') call must be scoped inside that context manager."
    )
