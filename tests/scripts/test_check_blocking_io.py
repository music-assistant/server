"""Tests for the blocking-IO-in-async check."""

import ast

from scripts.check_blocking_io import _BlockingCallVisitor, main


def _blocking_calls(source: str) -> list[str]:
    """Return the dotted blocking calls the visitor finds inside async scopes in a snippet."""
    return [call for _, call in _BlockingCallVisitor().collect(ast.parse(source))]


def test_flags_blocking_call_in_async_function() -> None:
    """A module-qualified blocking call directly inside ``async def`` is flagged."""
    source = "async def f():\n    return os.walk(path)\n"
    assert _blocking_calls(source) == ["os.walk"]


def test_flags_requests_and_urllib() -> None:
    """``requests`` verbs and ``urllib.request.urlopen`` are recognized inside async code."""
    source = "async def f():\n    requests.get(url)\n    urllib.request.urlopen(url)\n"
    assert _blocking_calls(source) == ["requests.get", "urllib.request.urlopen"]


def test_ignores_blocking_call_in_sync_function() -> None:
    """The same call in a plain ``def`` (e.g. a to_thread target) is not flagged."""
    source = "def f():\n    return os.walk(path)\n"
    assert _blocking_calls(source) == []


def test_ignores_sync_function_nested_in_async() -> None:
    """A blocking call inside a sync helper nested in an async function is not flagged."""
    source = (
        "async def outer():\n"
        "    def _worker():\n"
        "        return shutil.rmtree(path)\n"
        "    return await asyncio.to_thread(_worker)\n"
    )
    assert _blocking_calls(source) == []


def test_ignores_function_reference_passed_to_to_thread() -> None:
    """Passing a blocking function as a reference (not calling it) is not flagged."""
    source = "async def f():\n    return await asyncio.to_thread(os.listdir, path)\n"
    assert _blocking_calls(source) == []


def test_real_tree_matches_baseline() -> None:
    """The shipped tree has no blocking calls beyond the grandfathered baseline."""
    assert main([]) == 0
