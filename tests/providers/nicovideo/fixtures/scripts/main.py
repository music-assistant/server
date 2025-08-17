"""
Fixture generation script for nicovideo provider tests.

This script uses authentication credentials to fetch actual responses from the Niconico API
and saves them as static fixtures for testing.

Note:
Fixtures generated with user credentials will contain personal user data.
Always submit fixtures created with a dedicated test account only.

Usage:
1. Set up environment variables (NICONICO_SESSION) OR
2. Set up a TEST_USER_SESSION in this file.
3. Run this file in the terminal.
4. Fixture files will be generated in the tests/providers/nicovideo/generated directory.
5. Use fixtures in tests.

Authentication Priority:
1. TEST_USER_SESSION (if provided) - for local testing
2. Environment variables (NICONICO_SESSION) - for CI/CD

"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent.parent.parent.parent.parent))

from tests.providers.nicovideo.fixtures.scripts.generation_orchestrator import (
    FixtureGenerationOrchestrator,
)

# Test user session - DO NOT COMMIT - for local testing only
TEST_USER_SESSION = ""

# Logging configuration
logging.basicConfig(level=logging.INFO)


async def main() -> None:
    """Run fixture generation with appropriate authentication."""
    session = TEST_USER_SESSION if TEST_USER_SESSION else os.getenv("NICONICO_SESSION")

    if not session:
        raise ValueError("No session found for authentication.")

    await FixtureGenerationOrchestrator().run_all_fixtures(session)


if __name__ == "__main__":
    asyncio.run(main())
