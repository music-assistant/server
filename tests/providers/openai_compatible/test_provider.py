"""Tests for the OpenAI Compatible plugin provider."""

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import ClientError
from music_assistant_models.errors import (
    InvalidDataError,
    LoginFailed,
    ProviderUnavailableError,
    RateLimited,
    SetupFailedError,
    UnsupportedFeaturedException,
)

from music_assistant.providers.openai_compatible import (
    SUPPORTED_FEATURES,
    OpenAICompatibleProvider,
    helpers,
    setup_flow,
)
from music_assistant.providers.openai_compatible.constants import CONF_MODELS

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession

BASE_URL = "https://api.example.com/v1"
INSTANCE_ID = "openai_compatible--test_instance"

# a minimal valid chat completion, for tests that only care about the request side
_CHAT_PAYLOAD = {"choices": [{"message": {"content": "42"}}]}


@pytest.fixture
def mass() -> AsyncMock:
    """Return a mocked MusicAssistant instance with a mockable HTTP session."""
    ma = AsyncMock()
    ma.http_session = MagicMock()
    return ma


@pytest.fixture
def provider(mass: AsyncMock) -> OpenAICompatibleProvider:
    """Return an OpenAICompatibleProvider with mocked dependencies and no models selected."""
    manifest = MagicMock()
    manifest.domain = "openai_compatible"
    config = MagicMock()
    config.values = {}
    config.instance_id = INSTANCE_ID
    config.get_value = MagicMock(return_value="GLOBAL")
    prov = OpenAICompatibleProvider(mass, manifest, config, SUPPORTED_FEATURES)
    prov._base_url = BASE_URL
    prov._api_key = ""
    return prov


async def test_list_models_returns_sorted_ids(mass: AsyncMock) -> None:
    """The discovered model ids come back sorted."""
    mass.http_session.request = MagicMock(
        return_value=_response_cm(
            response=_json_response(200, {"data": [{"id": "c"}, {"id": "a"}, {"id": "b"}]})
        )
    )

    assert await helpers.list_models(mass, BASE_URL, "", timeout=10) == ["a", "b", "c"]


async def test_list_models_skips_entries_without_a_usable_id(mass: AsyncMock) -> None:
    """Entries missing an id, or with a blank/non-string one, are dropped rather than raising."""
    mass.http_session.request = MagicMock(
        return_value=_response_cm(
            response=_json_response(
                200,
                {
                    "data": [
                        {"id": "good-model"},
                        {"id": ""},
                        {"id": 123},
                        {"no_id": "x"},
                        "not-a-dict",
                    ]
                },
            )
        )
    )

    assert await helpers.list_models(mass, BASE_URL, "", timeout=10) == ["good-model"]


async def test_list_models_returns_empty_list_when_endpoint_has_no_listing(
    mass: AsyncMock,
) -> None:
    """The listing endpoint is optional in the OpenAI API, so a 404 is not an error."""
    mass.http_session.request = MagicMock(
        return_value=_response_cm(response=_json_response(404, {}))
    )

    assert await helpers.list_models(mass, BASE_URL, "", timeout=10) == []


async def test_list_models_raises_login_failed_on_401(mass: AsyncMock) -> None:
    """Bad credentials must be reported, not swallowed into an empty list."""
    mass.http_session.request = MagicMock(
        return_value=_response_cm(response=_json_response(401, {}))
    )

    with pytest.raises(LoginFailed):
        await helpers.list_models(mass, BASE_URL, "bad-key", timeout=10)


async def test_login_failed_message_includes_the_upstream_error_detail(mass: AsyncMock) -> None:
    """The service's own error message reaches the user, not just its status code."""
    body = {"error": {"message": "Incorrect API key provided"}}
    mass.http_session.request = MagicMock(
        return_value=_response_cm(response=_json_response(401, body))
    )

    with pytest.raises(LoginFailed, match="Incorrect API key provided"):
        await helpers.list_models(mass, BASE_URL, "bad-key", timeout=10)


async def test_list_models_raises_provider_unavailable_on_connection_error(
    mass: AsyncMock,
) -> None:
    """An unreachable host must be reported, not swallowed into an empty list."""
    mass.http_session.request = MagicMock(
        return_value=_response_cm(exc=ClientError("connection refused"))
    )

    with pytest.raises(ProviderUnavailableError):
        await helpers.list_models(mass, BASE_URL, "", timeout=10)


async def test_list_models_raises_provider_unavailable_on_timeout(mass: AsyncMock) -> None:
    """A hung request must be reported, not swallowed into an empty list."""
    mass.http_session.request = MagicMock(return_value=_response_cm(exc=TimeoutError()))

    with pytest.raises(ProviderUnavailableError):
        await helpers.list_models(mass, BASE_URL, "", timeout=10)


async def test_chat_completion_returns_trimmed_content(mass: AsyncMock) -> None:
    """The answer is returned with surrounding whitespace trimmed."""
    mass.http_session.request = MagicMock(
        return_value=_response_cm(
            response=_json_response(
                200, {"choices": [{"message": {"content": "  The answer is 42.  "}}]}
            )
        )
    )

    result = await helpers.chat_completion(
        mass, BASE_URL, "", model="my-model", prompt="What is the answer?", timeout=30
    )

    assert result == "The answer is 42."


async def test_chat_completion_strips_leading_think_block(mass: AsyncMock) -> None:
    """A reasoning model's <think> preamble is removed, leaving only the answer."""
    content = "<think>Let me reason about this.</think>Paris is the capital of France."
    mass.http_session.request = MagicMock(
        return_value=_response_cm(
            response=_json_response(200, {"choices": [{"message": {"content": content}}]})
        )
    )

    result = await helpers.chat_completion(
        mass, BASE_URL, "", model="my-model", prompt="capital of France?", timeout=30
    )

    assert result == "Paris is the capital of France."


async def test_chat_completion_raises_on_think_only_content(mass: AsyncMock) -> None:
    """A reply that is only a think block leaves no answer, which is an error."""
    content = "<think>Still thinking about it...</think>"
    mass.http_session.request = MagicMock(
        return_value=_response_cm(
            response=_json_response(200, {"choices": [{"message": {"content": content}}]})
        )
    )

    with pytest.raises(InvalidDataError):
        await helpers.chat_completion(mass, BASE_URL, "", model="my-model", prompt="hi", timeout=30)


async def test_chat_completion_raises_on_blank_content(mass: AsyncMock) -> None:
    """Whitespace-only content is treated the same as no answer at all."""
    mass.http_session.request = MagicMock(
        return_value=_response_cm(
            response=_json_response(200, {"choices": [{"message": {"content": "   "}}]})
        )
    )

    with pytest.raises(InvalidDataError):
        await helpers.chat_completion(mass, BASE_URL, "", model="my-model", prompt="hi", timeout=30)


async def test_chat_completion_raises_when_choices_missing(mass: AsyncMock) -> None:
    """A response without a choices list is malformed, not a valid empty answer."""
    mass.http_session.request = MagicMock(
        return_value=_response_cm(response=_json_response(200, {}))
    )

    with pytest.raises(InvalidDataError):
        await helpers.chat_completion(mass, BASE_URL, "", model="my-model", prompt="hi", timeout=30)


async def test_chat_completion_raises_rate_limited_with_retry_after(mass: AsyncMock) -> None:
    """A 429 is raised as RateLimited, honouring a numeric Retry-After as the backoff time."""
    mass.http_session.request = MagicMock(
        return_value=_response_cm(response=_json_response(429, {}, retry_after="45"))
    )

    with pytest.raises(RateLimited) as excinfo:
        await helpers.chat_completion(mass, BASE_URL, "", model="my-model", prompt="hi", timeout=30)

    assert excinfo.value.backoff_time == 45


async def test_chat_completion_request_body_has_no_max_tokens(mass: AsyncMock) -> None:
    """The model and prompt are sent as a single user message, deliberately without max_tokens."""
    mass.http_session.request = MagicMock(
        return_value=_response_cm(response=_json_response(200, _CHAT_PAYLOAD))
    )

    await helpers.chat_completion(
        mass, BASE_URL, "", model="my-model", prompt="What is up?", timeout=30
    )

    sent = mass.http_session.request.call_args.kwargs["json"]
    assert sent["model"] == "my-model"
    assert sent["messages"] == [{"role": "user", "content": "What is up?"}]
    assert "max_tokens" not in sent


def test_error_detail_falls_back_to_raw_text_for_non_json_body() -> None:
    """A body that is not JSON at all still yields something readable."""
    assert helpers._error_detail(b"Service Unavailable") == "Service Unavailable"


def test_error_detail_returns_placeholder_for_empty_body() -> None:
    """An empty response body must not surface as an empty, unhelpful error message."""
    assert helpers._error_detail(b"") == "no details provided"


def test_error_detail_truncates_overlong_messages() -> None:
    """An unbounded upstream message is capped so it cannot bloat the raised error."""
    long_message = "x" * (helpers.MAX_ERROR_DETAIL_CHARS + 50)
    body = json.dumps({"error": {"message": long_message}}).encode()

    result = helpers._error_detail(body)

    assert result == long_message[: helpers.MAX_ERROR_DETAIL_CHARS]


async def test_handle_async_init_strips_trailing_slash_from_base_url() -> None:
    """Every request path is built by appending to the stored base URL."""
    provider = _provider_for_setup({"base_url": "https://api.example.com/v1/"})

    await provider.handle_async_init()

    assert provider._base_url == "https://api.example.com/v1"


async def test_handle_async_init_strips_whitespace_from_base_url() -> None:
    """Whitespace around a stored base URL is stripped."""
    provider = _provider_for_setup({"base_url": "  https://api.example.com/v1  "})

    await provider.handle_async_init()

    assert provider._base_url == "https://api.example.com/v1"


async def test_handle_async_init_raises_when_base_url_missing() -> None:
    """A never-configured base URL cannot be initialized into a working provider."""
    provider = _provider_for_setup({})

    with pytest.raises(SetupFailedError):
        await provider.handle_async_init()


async def test_handle_async_init_raises_when_base_url_blank() -> None:
    """A blank base URL is treated the same as a missing one."""
    provider = _provider_for_setup({"base_url": "   "})

    with pytest.raises(SetupFailedError):
        await provider.handle_async_init()


async def test_handle_async_init_defaults_missing_api_key_to_empty_string() -> None:
    """No stored key means the no-auth local-server case, not None or the text "None"."""
    provider = _provider_for_setup({"base_url": BASE_URL})

    await provider.handle_async_init()

    assert provider._api_key == ""


async def test_handle_async_init_keeps_the_stored_api_key() -> None:
    """A stored key is used once decrypted, with surrounding whitespace removed."""
    provider = _provider_for_setup({"base_url": BASE_URL, "api_key": "  sk-secret  "})

    await provider.handle_async_init()

    assert provider._api_key == "sk-secret"


async def test_probe_returns_invalid_api_key_slug_on_login_failed() -> None:
    """A rejected key must map to the frontend's invalid_api_key error slug."""
    session = cast("SetupSession", SimpleNamespace(mass=MagicMock()))
    with patch.object(setup_flow, "list_models", AsyncMock(side_effect=LoginFailed("bad key"))):
        result = await setup_flow._probe(session, {"base_url": BASE_URL, "api_key": "bad"})

    assert result == "invalid_api_key"


async def test_probe_returns_cannot_connect_slug_on_provider_unavailable() -> None:
    """An unreachable host must map to the frontend's cannot_connect error slug."""
    session = cast("SetupSession", SimpleNamespace(mass=MagicMock()))
    with patch.object(
        setup_flow, "list_models", AsyncMock(side_effect=ProviderUnavailableError("down"))
    ):
        result = await setup_flow._probe(session, {"base_url": BASE_URL})

    assert result == "cannot_connect"


async def test_probe_succeeds_when_endpoint_has_no_model_listing() -> None:
    """An empty model listing is a valid, working endpoint, not a probe failure."""
    session = cast("SetupSession", SimpleNamespace(mass=MagicMock()))
    with patch.object(setup_flow, "list_models", AsyncMock(return_value=[])):
        result = await setup_flow._probe(session, {"base_url": BASE_URL})

    assert result is None


async def test_run_setup_keeps_the_stored_api_key_when_the_field_is_left_empty() -> None:
    """The stored key is never sent to the client, so an empty field must not wipe it."""
    session = _FakeSession(
        setup_data={"base_url": BASE_URL, "api_key": "sk-existing"},
        submissions=[{"base_url": BASE_URL, "api_key": ""}],
    )

    await _run_setup(session)

    assert session.finished == {"base_url": BASE_URL, "api_key": "sk-existing"}


async def test_run_setup_replaces_the_stored_api_key_when_a_new_one_is_given() -> None:
    """A freshly typed key overwrites the stored one."""
    session = _FakeSession(
        setup_data={"base_url": BASE_URL, "api_key": "sk-existing"},
        submissions=[{"base_url": BASE_URL, "api_key": "sk-new"}],
    )

    await _run_setup(session)

    assert session.finished == {"base_url": BASE_URL, "api_key": "sk-new"}


async def test_run_setup_clears_the_stored_api_key_on_request() -> None:
    """Ticking the clear option removes the key, for a move to a keyless server."""
    session = _FakeSession(
        setup_data={"base_url": BASE_URL, "api_key": "sk-existing"},
        submissions=[{"base_url": BASE_URL, "api_key": "", "clear_api_key": True}],
    )

    await _run_setup(session)

    assert session.finished == {"base_url": BASE_URL, "api_key": ""}


async def test_run_setup_offers_the_clear_option_only_when_a_key_is_stored() -> None:
    """A first-time setup has nothing to clear, so the option stays out of the form."""
    fresh = _FakeSession(setup_data={}, submissions=[{"base_url": BASE_URL, "api_key": "sk-new"}])
    stored = _FakeSession(
        setup_data={"base_url": BASE_URL, "api_key": "sk-existing"},
        submissions=[{"base_url": BASE_URL, "api_key": ""}],
    )

    await _run_setup(fresh)
    await _run_setup(stored)

    assert setup_flow.CONF_CLEAR_API_KEY not in [entry.key for entry in fresh.entries]
    assert setup_flow.CONF_CLEAR_API_KEY in [entry.key for entry in stored.entries]


async def test_get_ai_engines_returns_one_engine_per_configured_model(
    provider: OpenAICompatibleProvider,
) -> None:
    """Each configured model becomes its own engine, keyed by its bare model id."""
    _configure_models(provider, ["model-a", "model-b"])

    engines = await provider.get_ai_engines()

    assert [engine.id for engine in engines] == ["model-a", "model-b"]
    assert all(engine.id == engine.name for engine in engines)
    assert [engine.uid for engine in engines] == [
        f"{INSTANCE_ID}/model-a",
        f"{INSTANCE_ID}/model-b",
    ]


async def test_get_ai_engines_empty_when_no_models_configured(
    provider: OpenAICompatibleProvider,
) -> None:
    """No selection yields no engines rather than a default model."""
    _configure_models(provider, [])

    assert await provider.get_ai_engines() == []


async def test_get_ai_engines_empty_when_config_value_absent(
    provider: OpenAICompatibleProvider,
) -> None:
    """A provider whose options form was never saved yields no engines instead of raising."""
    provider.config.get_value = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda _key, default=None: default
    )

    assert await provider.get_ai_engines() == []


async def test_get_ai_engines_ignores_non_string_junk(
    provider: OpenAICompatibleProvider,
) -> None:
    """Malformed stored entries are dropped instead of raising or producing bad engines."""
    _configure_models(provider, ["model-a", 123, None, "   ", ""])

    engines = await provider.get_ai_engines()

    assert [engine.id for engine in engines] == ["model-a"]


async def test_ai_query_uses_the_given_engine_id(
    provider: OpenAICompatibleProvider, mass: AsyncMock
) -> None:
    """An explicit engine id is the model sent in the request, regardless of the selection."""
    _configure_models(provider, ["model-a", "model-b"])
    mass.http_session.request = MagicMock(
        return_value=_response_cm(response=_json_response(200, _CHAT_PAYLOAD))
    )

    await provider.ai_query("hello", engine_id="model-b")

    assert mass.http_session.request.call_args.kwargs["json"]["model"] == "model-b"


async def test_ai_query_falls_back_to_first_configured_model(
    provider: OpenAICompatibleProvider, mass: AsyncMock
) -> None:
    """No engine id falls back to the first configured model, in sorted order."""
    _configure_models(provider, ["model-b", "model-a"])
    mass.http_session.request = MagicMock(
        return_value=_response_cm(response=_json_response(200, _CHAT_PAYLOAD))
    )

    await provider.ai_query("hello", engine_id=None)

    assert mass.http_session.request.call_args.kwargs["json"]["model"] == "model-a"


async def test_ai_query_raises_when_no_models_configured(
    provider: OpenAICompatibleProvider,
) -> None:
    """No selected model at all is a hard failure, not a silent default."""
    _configure_models(provider, [])

    with pytest.raises(UnsupportedFeaturedException):
        await provider.ai_query("hello")


async def test_get_config_entries_models_option_union_survives_vanished_selection(
    provider: OpenAICompatibleProvider, mass: AsyncMock
) -> None:
    """A selected model missing from the endpoint's current listing is still offered."""
    _configure_models(provider, ["stale-model"])
    mass.http_session.request = MagicMock(
        return_value=_response_cm(response=_json_response(200, {"data": [{"id": "fresh-model"}]}))
    )

    entries = await provider.get_config_entries()

    models_entry = next(entry for entry in entries if entry.key == CONF_MODELS)
    assert models_entry.multi_value is True
    assert {option.value for option in models_entry.options} == {"stale-model", "fresh-model"}


async def test_get_config_entries_options_empty_when_discovery_fails(
    provider: OpenAICompatibleProvider, mass: AsyncMock
) -> None:
    """A failed listing leaves no options, which is what lets the frontend accept free text."""
    _configure_models(provider, [])
    mass.http_session.request = MagicMock(
        return_value=_response_cm(exc=ClientError("connection refused"))
    )

    entries = await provider.get_config_entries()

    models_entry = next(entry for entry in entries if entry.key == CONF_MODELS)
    assert models_entry.options == []


def _response_cm(*, response: MagicMock | None = None, exc: Exception | None = None) -> MagicMock:
    """Build a fake async context manager mimicking aiohttp's session.request()."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=response, side_effect=exc)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _json_response(status: int, payload: dict[str, Any], *, retry_after: str = "") -> MagicMock:
    """Build a fake aiohttp response exposing the attributes _request reads."""
    response = MagicMock()
    response.status = status
    response.headers = {"Retry-After": retry_after} if retry_after else {}
    response.read = AsyncMock(return_value=json.dumps(payload).encode())
    return response


def _configure_models(provider: OpenAICompatibleProvider, models: list[Any]) -> None:
    """Make the provider report the given stored value for the models config entry."""
    provider.config.get_value = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda key, default=None: models if key == CONF_MODELS else default
    )


def _provider_for_setup(setup_data: dict[str, Any]) -> OpenAICompatibleProvider:
    """Build a provider backed by the given setup_data, with nothing stored in its options."""
    mass = AsyncMock()
    mass.http_session = MagicMock()
    mass.config = MagicMock()
    mass.config.get = MagicMock(return_value=dict(setup_data))
    mass.config.decrypt_string = MagicMock(side_effect=lambda value: value)
    manifest = MagicMock()
    manifest.domain = "openai_compatible"
    config = MagicMock()
    config.values = {}
    config.instance_id = INSTANCE_ID
    config.get_value = MagicMock(return_value="GLOBAL")
    provider = OpenAICompatibleProvider(mass, manifest, config, SUPPORTED_FEATURES)
    # the log level is read during construction; past that nothing is stored in the options
    config.get_value = MagicMock(side_effect=lambda _key, default=None: default)
    return provider


async def _run_setup(session: _FakeSession) -> None:
    """Drive the setup flow against a fake session, with a reachable endpoint."""
    with patch.object(setup_flow, "list_models", AsyncMock(return_value=[])):
        await setup_flow.run_setup(cast("SetupSession", session))


class _FakeSession:
    """Minimal stand-in for SetupSession that replays scripted form submissions."""

    def __init__(self, setup_data: dict[str, Any], submissions: list[dict[str, Any]]) -> None:
        """Initialize with the stored setup_data and the submissions to replay in order."""
        self.context = SimpleNamespace(setup_data=setup_data)
        self.mass = MagicMock()
        self.entries: list[Any] = []
        self.finished: dict[str, Any] | None = None
        self._submissions = list(submissions)

    async def form(self, entries: list[Any], **kwargs: Any) -> dict[str, Any]:
        """Record the rendered entries and return the next scripted submission."""
        self.entries = entries
        return dict(self._submissions.pop(0))

    async def finish(self, values: dict[str, Any]) -> dict[str, str]:
        """Record the values the flow decided to persist."""
        self.finished = dict(values)
        return {"instance_id": "openai_compatible--new"}
