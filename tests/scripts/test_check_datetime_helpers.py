"""Tests for the datetime-helper usage check."""

import ast

from scripts.check_datetime_helpers import _naive_now_call, main


def _first_call(source: str) -> ast.Call:
    """Return the first ``ast.Call`` node in a source snippet."""
    return next(node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Call))


def test_matches_direct_datetime_now_variants() -> None:
    """All common direct ``datetime`` now/utcnow call shapes are matched."""
    assert _naive_now_call(_first_call("datetime.now(UTC)")) == "datetime.now"
    assert _naive_now_call(_first_call("datetime.utcnow()")) == "datetime.utcnow"
    assert _naive_now_call(_first_call("datetime.datetime.now(tz)")) == "datetime.datetime.now"
    assert _naive_now_call(_first_call("datetime.datetime.utcnow()")) == "datetime.datetime.utcnow"


def test_ignores_unrelated_now_calls() -> None:
    """A ``now()`` on a non-datetime object and other time calls are not matched."""
    assert _naive_now_call(_first_call("time.time()")) is None
    assert _naive_now_call(_first_call("clock.now()")) is None
    assert _naive_now_call(_first_call("helpers_datetime.utc()")) is None


def test_real_tree_matches_baseline() -> None:
    """The shipped tree has no datetime call sites beyond the grandfathered baseline."""
    assert main([]) == 0
