"""Tests for the MSX Bridge Provider."""

from pathlib import Path
from pkgutil import extend_path

# The provider suite runs beside a Music Assistant checkout and reuses shared
# test helpers from it while keeping provider-specific tests in this package.
__path__ = extend_path(__path__, __name__)
_ma_tests = Path(__file__).resolve().parents[1] / "ma-server" / "tests"
if _ma_tests.is_dir() and str(_ma_tests) not in __path__:
    __path__.append(str(_ma_tests))
