"""Fixtures for the player lifecycle scenarios."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from .harness import Scenario, xfail_origin_error

if TYPE_CHECKING:
    import pathlib
    from collections.abc import AsyncGenerator, Generator


@pytest.fixture
async def scenario(tmp_path: pathlib.Path) -> AsyncGenerator[Scenario]:
    """
    Boot a real server with fake player providers, ready for a scenario.

    :param tmp_path: Temporary directory for the server's storage.
    """
    scenario = Scenario(tmp_path)
    try:
        await scenario.start()
        yield scenario
    finally:
        await scenario.close()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    """Fail an expected failure that the test module itself did not raise."""
    report = yield
    if getattr(report, "wasxfail", None) is not None and (
        reason := xfail_origin_error(item.path, call)
    ):
        report.outcome = "failed"
        report.longrepr = reason
        del report.wasxfail
    return report
