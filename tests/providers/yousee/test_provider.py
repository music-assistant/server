"""Test the YouSee Musik provider wiring on top of the shared 24-7 base."""

from music_assistant.providers.music247e.api_client import Music247eAPIClient
from music_assistant.providers.telmore.api_client import TelmoreAPIClient
from music_assistant.providers.yousee.api_client import YouSeeAPIClient
from music_assistant.providers.yousee.auth_manager import YouSeeAuthManager
from music_assistant.providers.yousee.provider import YouSeeMusikProvider


def test_provider_wiring() -> None:
    """YouSee keeps its quality config key, endpoint and its own throttler."""
    assert YouSeeMusikProvider.CONF_QUALITY_KEY == "yousee_quality"
    assert YouSeeMusikProvider.AUTH_MANAGER_CLASS is YouSeeAuthManager
    assert YouSeeMusikProvider.API_CLIENT_CLASS is YouSeeAPIClient
    assert YouSeeAPIClient.GRAPHQL_ENDPOINT == "https://graphql-1458.api.247e.com/graphql"
    assert YouSeeAPIClient.throttler is not Music247eAPIClient.throttler
    assert YouSeeAPIClient.throttler is not TelmoreAPIClient.throttler
