"""
Fixture generation script for nicovideo provider tests.

This script uses a test user session to fetch actual responses from the Niconico API
and saves them as static fixtures for testing.

Note:
Fixtures generated with a user session will contain personal user data.
Always submit fixtures created with a dedicated test account only.

Usage:
1. Set up a TEST_USER_SESSION.
2. Run this file in the terminal.
3. Fixture files will be generated in the fixtures directory.
4. Use fixtures in tests.

"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from niconico import NicoNico
from niconico.exceptions import LoginFailureError

sys.path.append(str(Path(__file__).resolve().parent.parent.parent.parent.parent.parent))

from tests.providers.nicovideo.fixtures.scripts.fixture_generator import FixtureGenerator

# Test user session - DO NOT COMMIT
TEST_USER_SESSION = ""

# Logging configuration
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def generate_all_fixtures(test_user_session: str) -> None:
    """Generate all fixtures using the FixtureGenerator class."""
    # Initialize NicoNico client
    client = NicoNico()

    try:
        # Login
        logger.info("Logging in with test user session...")
        client.login_with_session(test_user_session)
        logger.info("Login successful!")

        logger.info("=== Generating fixtures ===")
        generator = FixtureGenerator()
        await generator.generate_all_fixtures(client)

    except LoginFailureError as e:
        logger.error(f"Login failed: {e}")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")


if __name__ == "__main__":
    asyncio.run(generate_all_fixtures(TEST_USER_SESSION))
