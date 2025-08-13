"""Main fixture generation orchestrator."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from niconico import NicoNico
from niconico.exceptions import LoginFailureError
from pydantic import BaseModel, ValidationError

from tests.providers.nicovideo.constants import GENERATED_FIXTURES_DIR
from tests.providers.nicovideo.fixtures.scripts.data_generators import (
    FixtureDataGenerators,
)
from tests.providers.nicovideo.fixtures.scripts.data_saver import (
    FixtureDataSaver,
)
from tests.providers.nicovideo.fixtures.scripts.path_to_type_mapper import (
    PathToTypeMapper,
)
from tests.providers.nicovideo.helpers import (
    stabilize_dynamic_fields_for_fixture,
    to_dict_for_fixture,
)
from tests.providers.nicovideo.types import (
    FixtureAPIResultOptional,
    FixtureCategory,
)

logger = logging.getLogger(__name__)


API_CALL_DELAY_SECONDS = 1.0
FIXTURE_LIMIT = 1


class FixtureGenerationOrchestrator:
    """Main orchestrator for fixture generation process."""

    def __init__(self) -> None:
        """Initialize the fixture generation orchestrator."""
        self.limit = FIXTURE_LIMIT

        # Initialize components with clear responsibilities
        self.path_to_type_mapping = PathToTypeMapper()
        self.data_saver = FixtureDataSaver()
        self.data_generators = FixtureDataGenerators(limit=self.limit)

    async def save_fixture[T: BaseModel, **P](
        self,
        category: FixtureCategory,
        name: str,
        api_call: Callable[P, FixtureAPIResultOptional[T]],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> FixtureAPIResultOptional[T]:
        """Save API response as fixture and return the data."""
        try:
            logger.info(f"Fetching {category}/{name}...")

            # Add delay before API call
            await asyncio.sleep(API_CALL_DELAY_SECONDS)

            # API call
            response = await asyncio.to_thread(api_call, *args, **kwargs)

            if response is None:
                logger.warning(f"No data returned for {category}/{name}")
                return None

            # If response is a list, truncate to self.limit
            if isinstance(response, list):
                response = response[: self.limit]

            # Stabilize the response data before processing
            response = stabilize_dynamic_fields_for_fixture(response)

            # Record type mapping for automatic generation
            self.path_to_type_mapping.record_type_mapping(response, category, name)

            # Convert to JSON serializable format
            data = to_dict_for_fixture(response)

            # Save fixture data
            fixture_path = GENERATED_FIXTURES_DIR / category / f"{name}.json"
            self.data_saver.save_fixture_data(data, fixture_path)

            # Return original response object
            return response

        except ValidationError as e:
            logger.error(f"Validation error for {category}/{name}:")
            detailed_errors = e.errors()
            for error in detailed_errors:
                logger.error(f"  Field: {error.get('loc', 'Unknown')}")
                logger.error(f"  Type: {error.get('type', 'Unknown')}")
                logger.error(f"  Message: {error.get('msg', 'Unknown')}")
                logger.error(f"  Input: {error.get('input', 'Unknown')}")
            logger.error(f"Full validation error: {e}")
            return None
        except Exception as e:
            logger.error(f"Failed to fetch {category}/{name}: {e}")
            return None

    async def run_all_fixtures(self, test_user_session: str) -> None:
        """Run all fixtures generation and post-processing."""
        try:
            client = NicoNico()

            logger.info("Logging in with user session...")
            client.login_with_session(test_user_session)
            logger.info("Login successful!")

            logger.info("=== Generating nicovideo fixtures ===")
            await self.data_generators.generate_all_fixtures(client, self.save_fixture)

            logger.info("=== Generating fixture types file ===")
            self.path_to_type_mapping.generate_fixture_types_file()

            logger.info("=== All fixtures generated successfully! ===")

            # Show diff summary
            self.data_saver.log_summary()

        except LoginFailureError as e:
            logger.error(f"Login failed: {e}")
            raise
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
            raise
