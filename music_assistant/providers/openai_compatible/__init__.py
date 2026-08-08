"""OpenAI Compatible plugin provider for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType, ProviderFeature
from music_assistant_models.errors import (
    MusicAssistantError,
    SetupFailedError,
    UnsupportedFeaturedException,
)

from music_assistant.models.plugin import AIEngine, PluginProvider

from .constants import (
    CHAT_REQUEST_TIMEOUT,
    CONF_API_KEY,
    CONF_BASE_URL,
    CONF_MODELS,
    MODELS_REQUEST_TIMEOUT,
)
from .helpers import chat_completion, list_models

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


SUPPORTED_FEATURES = {ProviderFeature.AI_QUERY}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return OpenAICompatibleProvider(mass, manifest, config, SUPPORTED_FEATURES)


class OpenAICompatibleProvider(PluginProvider):
    """Exposes the models of an OpenAI-compatible API as selectable AI engines."""

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        if not self._base_url:
            msg = "No API base URL is configured"
            raise SetupFailedError(msg)

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return the (options) config entries to configure this provider instance."""
        selected = self._configured_models()
        # the listing is the natural source for the options, but it is optional in the
        # OpenAI API: with no options at all the frontend accepts freely typed names,
        # and keeping the current selection in the list survives a model disappearing
        models = sorted({*await self._discover_models(), *selected})
        return (
            ConfigEntry(
                key=CONF_MODELS,
                type=ConfigEntryType.STRING,
                multi_value=True,
                required=False,
                default_value=[],
                options=[ConfigValueOption(model, title=model) for model in models],
                category="features",
            ),
        )

    async def get_ai_engines(self) -> list[AIEngine]:
        """Return the AI engines this plugin exposes."""
        # the engine name carries no provider name: consumers label a picker option
        # as "<provider name> | <engine name>" themselves
        return [
            AIEngine(id=model, name=model, provider=self) for model in self._configured_models()
        ]

    async def ai_query(self, query: str, engine_id: str | None = None) -> str:
        """Handle an AI query."""
        model = engine_id or next(iter(self._configured_models()), None)
        if model is None:
            msg = "No model is selected for this provider"
            raise UnsupportedFeaturedException(msg)
        return await chat_completion(
            self.mass,
            self._base_url,
            self._api_key,
            model=model,
            prompt=query,
            timeout=CHAT_REQUEST_TIMEOUT,
        )

    @property
    def _base_url(self) -> str:
        """Return the configured API endpoint, without trailing slash."""
        # read on demand: the config entries are resolved before async init runs,
        # so nothing this provider needs may live on an attribute set there
        return str(self.get_setup_value(CONF_BASE_URL) or "").strip().rstrip("/")

    @property
    def _api_key(self) -> str:
        """Return the configured API key, empty for an endpoint without auth."""
        api_key = self.get_setup_value(CONF_API_KEY)
        return api_key.strip() if isinstance(api_key, str) else ""

    def _configured_models(self) -> list[str]:
        """Return the models the user selected, in a stable order."""
        value = self.get_config_value(CONF_MODELS)
        if not isinstance(value, list):
            return []
        return sorted(
            {model.strip() for model in value if isinstance(model, str) and model.strip()}
        )

    async def _discover_models(self) -> list[str]:
        """Return the models the endpoint advertises, empty when it cannot be asked."""
        try:
            return await list_models(
                self.mass, self._base_url, self._api_key, MODELS_REQUEST_TIMEOUT
            )
        except MusicAssistantError as err:
            self.logger.warning("Could not retrieve the list of available models: %s", err)
            return []
