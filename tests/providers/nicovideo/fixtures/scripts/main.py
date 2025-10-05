"""
Fixture generation script for nicovideo provider tests.

This script uses authentication credentials to fetch actual responses from the Niconico API
and saves them as static fixtures for testing.

Note:
Fixtures generated with user credentials will contain personal user data.
Always submit fixtures created with a dedicated test account only.

Usage:
1. Set the NICONICO_SESSION environment variable
2. Run this file in the terminal
3. Fixture files will be generated in the tests/providers/nicovideo/generated directory
4. Use fixtures in tests

Authentication:
Environment variable (NICONICO_SESSION) is required for security.
This prevents accidental commits of hardcoded credentials.

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

# Logging configuration
logging.basicConfig(level=logging.INFO)


async def main() -> None:
    """Run fixture generation with environment variable authentication.

    Required environment variable:
        NICONICO_SESSION: User session token for Niconico API access

    This approach prevents accidental commits of hardcoded credentials
    while maintaining provider isolation (no repository-wide pre-commit hooks).
    """
    session = os.getenv("NICONICO_SESSION")

    if not session:
        msg = (
            "NICONICO_SESSION environment variable is required.\n"
            "Set it before running this script:\n"
            "  export NICONICO_SESSION='your_session_token'"
        )
        raise ValueError(msg)

    await FixtureGenerationOrchestrator().run_all_fixtures(session)


if __name__ == "__main__":
    asyncio.run(main())
