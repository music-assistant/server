"""Tests for the OpenAI Compatible plugin provider."""

import base64
import json
from pathlib import Path
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

from music_assistant.helpers.plugin_engines import EngineKind, get_default_engine
from music_assistant.models.setup_flow import SetupFlowError
from music_assistant.providers.openai_compatible import (
    SUPPORTED_FEATURES,
    OpenAICompatibleProvider,
    helpers,
    setup_flow,
)
from music_assistant.providers.openai_compatible.constants import (
    CONF_IMAGE_MODELS,
    CONF_MODELS,
    SERVICE_BASE_URLS,
    SERVICE_CUSTOM,
)
from music_assistant.testing import full_mass_context

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
    ma.config = MagicMock()
    return ma


@pytest.fixture
def provider(mass: AsyncMock) -> OpenAICompatibleProvider:
    """Return an OpenAICompatibleProvider with mocked dependencies and no models selected."""
    return _build_provider(mass, {"base_url": BASE_URL})


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


async def test_list_models_raises_on_an_http_error(mass: AsyncMock) -> None:
    """A 404 usually means a wrong address, so it must not pass as "nothing to offer"."""
    mass.http_session.request = MagicMock(
        return_value=_response_cm(response=_json_response(404, {}))
    )

    with pytest.raises(InvalidDataError):
        await helpers.list_models(mass, BASE_URL, "", timeout=10)


async def test_list_models_returns_empty_list_when_the_answer_carries_no_list(
    mass: AsyncMock,
) -> None:
    """An endpoint answering at the right address without a list has nothing to offer."""
    mass.http_session.request = MagicMock(
        return_value=_response_cm(response=_json_response(200, {}))
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


def test_base_url_strips_trailing_slash() -> None:
    """Every request path is built by appending to the stored base URL."""
    provider = _provider_for_setup({"base_url": "https://api.example.com/v1/"})

    assert provider._base_url == "https://api.example.com/v1"


def test_base_url_strips_whitespace() -> None:
    """Whitespace around a stored base URL is stripped."""
    provider = _provider_for_setup({"base_url": "  https://api.example.com/v1  "})

    assert provider._base_url == "https://api.example.com/v1"


async def test_handle_async_init_accepts_a_configured_base_url() -> None:
    """A usable endpoint must pass the init check, whatever shape the address has."""
    provider = _provider_for_setup({"base_url": "http://localhost:11434/v1"})

    await provider.handle_async_init()


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


def test_api_key_defaults_to_empty_string_when_not_stored() -> None:
    """No stored key means the no-auth local-server case, not None or the text "None"."""
    provider = _provider_for_setup({"base_url": BASE_URL})

    assert provider._api_key == ""


def test_api_key_comes_from_setup_data_and_is_stripped() -> None:
    """The key is only stored in setup_data, and surrounding whitespace is removed."""
    provider = _provider_for_setup({"base_url": BASE_URL, "api_key": "  sk-secret  "})

    assert provider._api_key == "sk-secret"


async def test_probe_raises_invalid_api_key_on_login_failed() -> None:
    """A rejected key must map to the invalid_api_key error."""
    session = cast("SetupSession", SimpleNamespace(mass=MagicMock()))
    with (
        patch.object(setup_flow, "list_models", AsyncMock(side_effect=LoginFailed("bad key"))),
        pytest.raises(SetupFlowError) as exc_info,
    ):
        await setup_flow._probe(session, {"base_url": BASE_URL, "api_key": "bad"})

    assert exc_info.value.translation_key == "invalid_api_key"


async def test_probe_raises_cannot_connect_on_provider_unavailable() -> None:
    """An unreachable host must map to the cannot_connect error."""
    session = cast("SetupSession", SimpleNamespace(mass=MagicMock()))
    with (
        patch.object(
            setup_flow, "list_models", AsyncMock(side_effect=ProviderUnavailableError("down"))
        ),
        pytest.raises(SetupFlowError) as exc_info,
    ):
        await setup_flow._probe(session, {"base_url": BASE_URL})

    assert exc_info.value.translation_key == "cannot_connect"


async def test_probe_succeeds_when_endpoint_has_no_model_listing() -> None:
    """An empty model listing is a valid, working endpoint, not a probe failure."""
    session = cast("SetupSession", SimpleNamespace(mass=MagicMock()))
    with patch.object(setup_flow, "list_models", AsyncMock(return_value=[])):
        result = await setup_flow._probe(session, {"base_url": BASE_URL})

    assert result == []


async def test_probe_reports_an_address_that_answers_with_an_error() -> None:
    """A wrong address must be caught during setup, not on the first AI query."""
    session = cast("SetupSession", SimpleNamespace(mass=MagicMock()))
    with (
        patch.object(setup_flow, "list_models", AsyncMock(side_effect=InvalidDataError("404"))),
        pytest.raises(SetupFlowError) as exc_info,
    ):
        await setup_flow._probe(session, {"base_url": BASE_URL})

    assert exc_info.value.translation_key == "cannot_connect"


async def test_run_setup_prefills_the_address_of_the_chosen_service() -> None:
    """Picking a known service fills in its address so it need not be looked up."""
    session = _FakeSession(setup_data={}, submissions=[{"service": "groq"}, {}, {}])

    await _run_setup(session)

    assert _entry_value(session, "base_url") == SERVICE_BASE_URLS["groq"]


async def test_run_setup_leaves_the_address_empty_for_a_custom_service() -> None:
    """There is nothing to prefill for a service we do not know the address of."""
    session = _FakeSession(setup_data={}, submissions=[{"service": SERVICE_CUSTOM}, {}, {}])

    await _run_setup(session)

    assert _entry_value(session, "base_url") == ""


async def test_run_setup_prefers_the_new_services_address_when_switching() -> None:
    """Moving an instance to another service must not keep the previous address."""
    session = _FakeSession(
        setup_data={"service": "openai", "base_url": SERVICE_BASE_URLS["openai"]},
        submissions=[{"service": "ollama"}, {}, {}],
    )

    await _run_setup(session)

    assert _entry_value(session, "base_url") == SERVICE_BASE_URLS["ollama"]


async def test_run_setup_keeps_an_edited_address_for_the_same_service() -> None:
    """A hand-corrected address survives a reconfigure that keeps the same service."""
    edited = "http://nas.local:11434/v1"
    session = _FakeSession(
        setup_data={"service": "ollama", "base_url": edited},
        submissions=[{"service": "ollama"}, {}, {}],
    )

    await _run_setup(session)

    assert _entry_value(session, "base_url") == edited


async def test_run_setup_keeps_the_stored_api_key_when_the_field_is_left_empty() -> None:
    """The stored key is never sent to the client, so an empty field must not wipe it."""
    session = _FakeSession(
        setup_data={"service": "openai", "base_url": BASE_URL, "api_key": "sk-existing"},
        submissions=[{"service": "openai"}, {"base_url": BASE_URL, "api_key": ""}, {}],
    )

    await _run_setup(session)

    assert session.finished == {
        "service": "openai",
        "base_url": BASE_URL,
        "api_key": "sk-existing",
    }


async def test_run_setup_replaces_the_stored_api_key_when_a_new_one_is_given() -> None:
    """A freshly typed key overwrites the stored one."""
    session = _FakeSession(
        setup_data={"service": "openai", "base_url": BASE_URL, "api_key": "sk-existing"},
        submissions=[{"service": "openai"}, {"base_url": BASE_URL, "api_key": "sk-new"}, {}],
    )

    await _run_setup(session)

    assert session.finished == {"service": "openai", "base_url": BASE_URL, "api_key": "sk-new"}


async def test_run_setup_clears_the_stored_api_key_on_request() -> None:
    """Ticking the clear option removes the key, for a move to a keyless server."""
    session = _FakeSession(
        setup_data={"service": "openai", "base_url": BASE_URL, "api_key": "sk-existing"},
        submissions=[
            {"service": "ollama"},
            {"base_url": BASE_URL, "api_key": "", "clear_api_key": True},
            {},
        ],
    )

    await _run_setup(session)

    assert session.finished == {"service": "ollama", "base_url": BASE_URL, "api_key": ""}


async def test_run_setup_offers_the_clear_option_only_when_a_key_is_stored() -> None:
    """A first-time setup has nothing to clear, so the option stays out of the form."""
    fresh = _FakeSession(setup_data={}, submissions=[{"service": "openai"}, {}, {}])
    stored = _FakeSession(
        setup_data={"service": "openai", "base_url": BASE_URL, "api_key": "sk-existing"},
        submissions=[{"service": "openai"}, {"base_url": BASE_URL, "api_key": ""}, {}],
    )

    await _run_setup(fresh)
    await _run_setup(stored)

    assert setup_flow.CONF_CLEAR_API_KEY not in [entry.key for entry in fresh.forms["connection"]]
    assert setup_flow.CONF_CLEAR_API_KEY in [entry.key for entry in stored.forms["connection"]]


async def test_run_setup_offers_the_listed_models_to_pick_from() -> None:
    """The models step offers what the endpoint lists, for text and image models alike."""
    session = _FakeSession(setup_data={}, submissions=[{"service": "openai"}, {}, {}])

    await _run_setup(session, AsyncMock(return_value=["gpt-a", "gpt-b"]))

    for key in (CONF_MODELS, CONF_IMAGE_MODELS):
        options = _entry(session, key, "models").options
        assert [option.value for option in options] == ["gpt-a", "gpt-b"]


async def test_run_setup_stores_the_picked_models_in_the_settings() -> None:
    """The picks land in the settings the provider reads its engines from, not in setup_data."""
    session = _FakeSession(
        setup_data={},
        submissions=[
            {"service": "openai"},
            {"base_url": BASE_URL, "api_key": "sk-new"},
            {CONF_MODELS: ["gpt-a"], CONF_IMAGE_MODELS: ["gpt-image"]},
        ],
    )

    await _run_setup(session, AsyncMock(return_value=["gpt-a", "gpt-image"]))

    assert session.finished_options == {CONF_MODELS: ["gpt-a"], CONF_IMAGE_MODELS: ["gpt-image"]}
    assert session.finished == {"service": "openai", "base_url": BASE_URL, "api_key": "sk-new"}


async def test_run_setup_prefills_the_current_models_on_a_reconfigure() -> None:
    """A reconfigure starts from the current picks, also when the endpoint no longer lists them."""
    session = _FakeSession(
        setup_data={"service": "openai", "base_url": BASE_URL},
        submissions=[{"service": "openai"}, {}, {}],
        values={CONF_MODELS: ["old-model"], CONF_IMAGE_MODELS: ["old-image"]},
    )

    await _run_setup(session, AsyncMock(return_value=["new-model"]))

    models = _entry(session, CONF_MODELS, "models")
    assert models.value == ["old-model"]
    assert [option.value for option in models.options] == ["new-model", "old-model"]
    assert _entry_value(session, CONF_IMAGE_MODELS, "models") == ["old-image"]


async def test_run_setup_accepts_typed_models_when_the_service_lists_none() -> None:
    """Without a listing the models step has no options, so the names can be typed in."""
    picks = {CONF_MODELS: ["typed-by-hand"], CONF_IMAGE_MODELS: []}
    session = _FakeSession(
        setup_data={}, submissions=[{"service": SERVICE_CUSTOM}, {"base_url": BASE_URL}, picks]
    )

    await _run_setup(session)

    assert _entry(session, CONF_MODELS, "models").options == []
    assert session.finished_options == picks


async def test_adding_the_provider_gives_the_picked_models_as_engines(tmp_path: Path) -> None:
    """Right after the setup flow the picks are engines, the default one included."""
    listing = AsyncMock(return_value=["gpt-a", "gpt-b"])
    with (
        patch.object(setup_flow, "list_models", listing),
        patch("music_assistant.providers.openai_compatible.list_models", listing),
    ):
        async with full_mass_context(tmp_path) as mass:
            step = await mass.config.setup_provider("openai_compatible")
            step = await mass.config.submit_setup_flow(step.flow_id, {"service": "openai"})
            step = await mass.config.submit_setup_flow(
                step.flow_id, {"base_url": BASE_URL, "api_key": "sk-test"}
            )
            assert step.step_id == "models"
            step = await mass.config.submit_setup_flow(
                step.flow_id, {CONF_MODELS: ["gpt-b"], CONF_IMAGE_MODELS: []}
            )
            assert step.result is not None
            instance_id = step.result["instance_id"]
            provider = mass.get_provider(instance_id, provider_type=OpenAICompatibleProvider)
            assert provider is not None
            assert [engine.id for engine in await provider.get_ai_engines()] == ["gpt-b"]
            default = await get_default_engine(mass, EngineKind.TEXT)
            assert default is not None
            assert default.id == "gpt-b"

            # a reconfigure starts from the current picks and replaces them
            step = await mass.config.reconfigure_provider(instance_id)
            step = await mass.config.submit_setup_flow(step.flow_id, {"service": "openai"})
            step = await mass.config.submit_setup_flow(step.flow_id, {"base_url": BASE_URL})
            assert next(e.value for e in step.entries if e.key == CONF_MODELS) == ["gpt-b"]
            await mass.config.submit_setup_flow(
                step.flow_id, {CONF_MODELS: ["gpt-a"], CONF_IMAGE_MODELS: ["gpt-b"]}
            )
            provider = mass.get_provider(instance_id, provider_type=OpenAICompatibleProvider)
            assert provider is not None
            assert [engine.id for engine in await provider.get_ai_engines()] == ["gpt-a"]
            assert [engine.id for engine in await provider.get_image_engines()] == ["gpt-b"]
            # the reconfigure keeps the stored key while its field stays empty
            assert provider._api_key == "sk-test"


async def test_run_setup_asks_for_the_connection_again_when_the_endpoint_fails() -> None:
    """A rejected key returns to the connection details before any model is asked for."""
    session = _FakeSession(
        setup_data={},
        submissions=[{"service": "openai"}, {"api_key": "bad"}, {"api_key": "good"}, {}],
    )

    await _run_setup(session, AsyncMock(side_effect=[LoginFailed("bad key"), ["gpt-a"]]))

    assert session.steps == ["service", "connection", "connection", "models"]
    assert session.errors["connection"]["base"].translation_key == "invalid_api_key"


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


async def test_get_config_entries_survives_an_endpoint_without_a_listing(
    provider: OpenAICompatibleProvider, mass: AsyncMock
) -> None:
    """Setup rejects a listing error, but an already-configured provider must still load."""
    _configure_models(provider, ["typed-by-hand"])
    mass.http_session.request = MagicMock(
        return_value=_response_cm(response=_json_response(404, {}))
    )

    entries = await provider.get_config_entries()

    models_entry = next(entry for entry in entries if entry.key == CONF_MODELS)
    assert [option.value for option in models_entry.options] == ["typed-by-hand"]


async def test_get_config_entries_reaches_the_endpoint_without_async_init(
    provider: OpenAICompatibleProvider, mass: AsyncMock
) -> None:
    """Loading resolves the config entries before async init, so they must not depend on it."""
    _configure_models(provider, [])
    mass.http_session.request = MagicMock(
        return_value=_response_cm(response=_json_response(200, {"data": [{"id": "fresh-model"}]}))
    )

    # deliberately no handle_async_init(): mass._load_provider only runs it afterwards
    entries = await provider.get_config_entries()

    assert mass.http_session.request.call_args.args[1] == f"{BASE_URL}/models"
    models_entry = next(entry for entry in entries if entry.key == CONF_MODELS)
    assert [option.value for option in models_entry.options] == ["fresh-model"]


async def test_image_generation_decodes_an_openrouter_style_inline_image(mass: AsyncMock) -> None:
    """OpenRouter returns the image as a data URL on the chat completion message."""
    encoded = base64.b64encode(b"png-bytes").decode()
    payload = {
        "choices": [
            {
                "message": {
                    "content": "",
                    "images": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{encoded}"},
                        }
                    ],
                }
            }
        ]
    }
    mass.http_session.request = MagicMock(
        return_value=_response_cm(response=_json_response(200, payload))
    )

    image, used_images_endpoint = await helpers.image_generation(
        mass, BASE_URL, "key", model="google/gemini-2.5-flash-image", prompt="A cat", timeout=10
    )

    assert image == b"png-bytes"
    assert used_images_endpoint is False
    method, url = mass.http_session.request.call_args.args
    assert (method, url) == ("post", f"{BASE_URL}/chat/completions")
    assert mass.http_session.request.call_args.kwargs["json"]["modalities"] == ["image", "text"]


async def test_image_generation_falls_back_to_the_images_endpoint(mass: AsyncMock) -> None:
    """A service rejecting image output on chat completions is asked on /images/generations."""
    encoded = base64.b64encode(b"jpeg-bytes").decode()
    mass.http_session.request = MagicMock(
        side_effect=[
            _response_cm(response=_json_response(400, {"error": {"message": "bad modalities"}})),
            _response_cm(response=_json_response(200, {"data": [{"b64_json": encoded}]})),
        ]
    )

    image, used_images_endpoint = await helpers.image_generation(
        mass, BASE_URL, "key", model="gpt-image-1", prompt="A cat", timeout=10
    )

    assert image == b"jpeg-bytes"
    assert used_images_endpoint is True
    method, url = mass.http_session.request.call_args.args
    assert (method, url) == ("post", f"{BASE_URL}/images/generations")
    assert mass.http_session.request.call_args.kwargs["json"]["prompt"] == "A cat"


async def test_image_generation_goes_straight_to_a_known_images_endpoint(mass: AsyncMock) -> None:
    """A model known to need the images endpoint skips the chat completion attempt."""
    encoded = base64.b64encode(b"jpeg-bytes").decode()
    mass.http_session.request = MagicMock(
        return_value=_response_cm(response=_json_response(200, {"data": [{"b64_json": encoded}]}))
    )

    await helpers.image_generation(
        mass,
        BASE_URL,
        "",
        model="gpt-image-1",
        prompt="A cat",
        timeout=10,
        use_images_endpoint=True,
    )

    assert mass.http_session.request.call_count == 1
    assert mass.http_session.request.call_args.args[1] == f"{BASE_URL}/images/generations"


def _download_response(
    chunks: list[bytes],
    content_length: int | None = None,
    *,
    status: int = 200,
    retry_after: str = "",
) -> MagicMock:
    """Build a fake aiohttp response streaming a downloaded image in the given chunks."""
    response = MagicMock()
    response.status = status
    response.headers = {"Retry-After": retry_after} if retry_after else {}
    response.content_length = content_length
    response.content.read = AsyncMock(side_effect=[*chunks, b""])
    return response


LOCAL_BASE_URL = "http://192.168.1.20:8080/v1"


async def _generate_by_url(mass: AsyncMock, url: str, base_url: str = LOCAL_BASE_URL) -> bytes:
    """Run an images endpoint generation whose reply hands out the image by URL."""
    mass.http_session.request = MagicMock(
        return_value=_response_cm(response=_json_response(200, {"data": [{"url": url}]}))
    )
    image, _ = await helpers.image_generation(
        mass, base_url, "", model="dall-e-3", prompt="A cat", timeout=10, use_images_endpoint=True
    )
    return image


async def test_the_images_endpoint_is_asked_for_an_inline_image(mass: AsyncMock) -> None:
    """The image is asked for inline, so there is nothing to download."""
    encoded = base64.b64encode(b"png").decode()
    mass.http_session.request = MagicMock(
        return_value=_response_cm(response=_json_response(200, {"data": [{"b64_json": encoded}]}))
    )

    image, _ = await helpers.image_generation(
        mass, BASE_URL, "", model="dall-e-3", prompt="A cat", timeout=10, use_images_endpoint=True
    )

    assert image == b"png"
    assert mass.http_session.request.call_args.kwargs["json"]["response_format"] == "b64_json"


async def test_a_service_rejecting_the_format_is_asked_again_without_it(mass: AsyncMock) -> None:
    """A service that does not take response_format still gets to answer."""
    encoded = base64.b64encode(b"png").decode()
    mass.http_session.request = MagicMock(
        side_effect=[
            _response_cm(
                response=_json_response(
                    400, {"error": {"message": "Unknown parameter: 'response_format'."}}
                )
            ),
            _response_cm(response=_json_response(200, {"data": [{"b64_json": encoded}]})),
        ]
    )

    image, _ = await helpers.image_generation(
        mass,
        BASE_URL,
        "",
        model="gpt-image-1",
        prompt="A cat",
        timeout=10,
        use_images_endpoint=True,
    )

    assert image == b"png"
    assert "response_format" not in mass.http_session.request.call_args.kwargs["json"]


async def test_a_model_refusing_the_format_is_asked_plainly_from_then_on(
    mass: AsyncMock,
) -> None:
    """Once a model refused response_format, later requests skip it without trying first."""
    encoded = base64.b64encode(b"png").decode()
    refused = {"error": {"message": "Unknown parameter: 'response_format'."}}
    ok = {"data": [{"b64_json": encoded}]}
    mass.http_session.request = MagicMock(
        side_effect=[
            _response_cm(response=_json_response(400, refused)),
            _response_cm(response=_json_response(200, ok)),
            _response_cm(response=_json_response(200, ok)),
        ]
    )
    no_format_models: set[str] = set()

    for _ in range(2):
        await helpers.image_generation(
            mass,
            BASE_URL,
            "",
            model="gpt-image-1",
            prompt="A cat",
            timeout=10,
            use_images_endpoint=True,
            no_format_models=no_format_models,
        )

    assert no_format_models == {"gpt-image-1"}
    # the refused request once, then plain requests only
    assert mass.http_session.request.call_count == 3
    assert "response_format" not in mass.http_session.request.call_args.kwargs["json"]


async def test_another_rejection_is_not_retried(mass: AsyncMock) -> None:
    """Only a rejected response_format is asked again, not any refused request."""
    mass.http_session.request = MagicMock(
        return_value=_response_cm(
            response=_json_response(400, {"error": {"message": "content policy violation"}})
        )
    )

    with pytest.raises(InvalidDataError, match="content policy"):
        await helpers.image_generation(
            mass, BASE_URL, "", model="m", prompt="A cat", timeout=10, use_images_endpoint=True
        )

    assert mass.http_session.request.call_count == 1


async def test_a_link_to_the_endpoint_itself_is_downloaded(mass: AsyncMock) -> None:
    """A self-hosted server linking to itself is downloaded through the shared session."""
    mass.http_session.get = MagicMock(
        return_value=_response_cm(response=_download_response([b"png-", b"bytes"]))
    )

    assert await _generate_by_url(mass, "http://192.168.1.20:8080/generated/x.png") == b"png-bytes"
    assert mass.http_session.get.call_args.args[0] == "http://192.168.1.20:8080/generated/x.png"
    assert mass.http_session.get.call_args.kwargs["allow_redirects"] is False


@pytest.mark.parametrize(
    "url",
    [
        # another host
        "https://cdn.example.com/x.png",
        # another service on the endpoint's host
        "http://192.168.1.20:8123/api/states",
        # another scheme
        "https://192.168.1.20:8080/x.png",
        "file:///etc/passwd",
    ],
)
async def test_links_outside_the_endpoint_are_refused(mass: AsyncMock, url: str) -> None:
    """The service picks the link, so only one to the endpoint itself is followed."""
    mass.http_session.get = MagicMock()

    with pytest.raises(InvalidDataError):
        await _generate_by_url(mass, url)

    mass.http_session.get.assert_not_called()


async def test_a_redirect_of_an_image_link_is_not_followed(mass: AsyncMock) -> None:
    """A redirect could lead anywhere, so it fails instead of being followed."""
    mass.http_session.get = MagicMock(
        return_value=_response_cm(response=_download_response([], status=302))
    )

    with pytest.raises(InvalidDataError, match="302"):
        await _generate_by_url(mass, "http://192.168.1.20:8080/generated/x.png")


@pytest.mark.parametrize("status", [408, 500, 503])
async def test_a_failing_image_download_is_reported_as_unavailable(
    mass: AsyncMock, status: int
) -> None:
    """The endpoint failing to hand out the image is an outage, not a failed generation."""
    mass.http_session.get = MagicMock(
        return_value=_response_cm(response=_download_response([b"busy"], status=status))
    )

    with pytest.raises(ProviderUnavailableError, match=str(status)):
        await _generate_by_url(mass, "http://192.168.1.20:8080/generated/x.png")


async def test_a_rate_limited_image_download_asks_for_the_backoff(mass: AsyncMock) -> None:
    """A 429 on the image download is a rate limit, for as long as the endpoint asks."""
    mass.http_session.get = MagicMock(
        return_value=_response_cm(
            response=_download_response([b"slow down"], status=429, retry_after="120")
        )
    )

    with pytest.raises(RateLimited) as limited:
        await _generate_by_url(mass, "http://192.168.1.20:8080/generated/x.png")

    assert limited.value.backoff_time == 120


async def test_an_ipv6_endpoint_links_to_itself(mass: AsyncMock) -> None:
    """An endpoint by IPv6 address is matched to its own links."""
    mass.http_session.get = MagicMock(
        return_value=_response_cm(response=_download_response([b"png"]))
    )

    assert (
        await _generate_by_url(
            mass, "http://[fd00::5]:8080/x.png", base_url="http://[fd00::5]:8080/v1"
        )
        == b"png"
    )


@pytest.mark.parametrize(
    "response",
    [
        # declared too large up front
        _download_response([b"x"], content_length=helpers.MAX_IMAGE_BYTES + 1),
        # no declared length, but more arrives than the limit
        _download_response([b"x" * helpers.MAX_IMAGE_BYTES, b"x"]),
    ],
)
async def test_image_generation_refuses_an_oversized_download(
    mass: AsyncMock, response: MagicMock
) -> None:
    """A downloaded image over the size limit is refused instead of read into memory."""
    mass.http_session.get = MagicMock(return_value=_response_cm(response=response))

    with pytest.raises(InvalidDataError, match="larger than the download limit"):
        await _generate_by_url(mass, "http://192.168.1.20:8080/generated/x.png")


async def test_a_malformed_inline_image_falls_back_to_the_images_endpoint(
    mass: AsyncMock,
) -> None:
    """An inline image that does not decode is no image, so the images endpoint is asked."""
    encoded = base64.b64encode(b"jpeg-bytes").decode()
    chat_payload = {
        "choices": [
            {
                "message": {
                    "images": [{"image_url": {"url": "data:image/png;base64,!!!!"}}],
                }
            }
        ]
    }
    mass.http_session.request = MagicMock(
        side_effect=[
            _response_cm(response=_json_response(200, chat_payload)),
            _response_cm(response=_json_response(200, {"data": [{"b64_json": encoded}]})),
        ]
    )

    image, used_images_endpoint = await helpers.image_generation(
        mass, BASE_URL, "", model="gpt-image-1", prompt="A cat", timeout=10
    )

    assert (image, used_images_endpoint) == (b"jpeg-bytes", True)


@pytest.mark.parametrize("status", [408, 500, 502, 503])
async def test_an_unavailable_service_is_reported_as_unavailable(
    mass: AsyncMock, status: int
) -> None:
    """A service that is down answers with an outage, not with a refused request."""
    mass.http_session.request = MagicMock(
        return_value=_response_cm(response=_json_response(status, {"error": {"message": "down"}}))
    )

    with pytest.raises(ProviderUnavailableError, match="unavailable"):
        await helpers.image_generation(mass, BASE_URL, "", model="m", prompt="A cat", timeout=10)

    # the images endpoint is not asked as well, the service is down
    assert mass.http_session.request.call_count == 1


async def test_an_oversized_api_reply_is_refused(mass: AsyncMock) -> None:
    """A reply over the size limit is refused instead of read into memory."""
    response = _json_response(200, _CHAT_PAYLOAD)
    response.content_length = helpers.MAX_RESPONSE_BYTES + 1
    mass.http_session.request = MagicMock(return_value=_response_cm(response=response))

    with pytest.raises(InvalidDataError, match="larger than the download limit"):
        await helpers.chat_completion(mass, BASE_URL, "", model="m", prompt="Hi", timeout=10)


async def test_an_inline_image_wrapped_over_lines_decodes() -> None:
    """Line breaks in a base64 payload are allowed."""
    encoded = base64.encodebytes(b"x" * 100).decode()
    assert "\n" in encoded

    assert helpers._decode_base64(encoded) == b"x" * 100


async def test_image_generation_raises_when_neither_endpoint_returns_an_image(
    mass: AsyncMock,
) -> None:
    """A text-only answer on both endpoints is an error naming both failures."""
    mass.http_session.request = MagicMock(
        side_effect=[
            _response_cm(response=_json_response(200, _CHAT_PAYLOAD)),
            _response_cm(response=_json_response(404, {"error": {"message": "no such route"}})),
        ]
    )

    with pytest.raises(InvalidDataError, match="no such route"):
        await helpers.image_generation(
            mass, BASE_URL, "", model="text-model", prompt="A cat", timeout=10
        )


async def test_image_generation_does_not_retry_a_rejected_api_key(mass: AsyncMock) -> None:
    """Bad credentials fail at once instead of trying the second endpoint."""
    mass.http_session.request = MagicMock(
        return_value=_response_cm(response=_json_response(401, {}))
    )

    with pytest.raises(LoginFailed):
        await helpers.image_generation(mass, BASE_URL, "bad", model="m", prompt="A cat", timeout=10)
    assert mass.http_session.request.call_count == 1


async def test_get_image_engines_returns_the_configured_image_models(
    provider: OpenAICompatibleProvider,
) -> None:
    """Image models are configured apart from the text models."""
    provider.config.get_value = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda key, default=None: {
            CONF_MODELS: ["text-model"],
            CONF_IMAGE_MODELS: ["image-b", "image-a"],
        }.get(key, default)
    )

    assert [engine.id for engine in await provider.get_image_engines()] == ["image-a", "image-b"]
    assert [engine.id for engine in await provider.get_ai_engines()] == ["text-model"]


async def test_ai_generate_image_remembers_a_model_that_needs_the_images_endpoint(
    provider: OpenAICompatibleProvider,
) -> None:
    """After one fallback, later requests for that model go to the images endpoint directly."""
    await provider.handle_async_init()
    with patch(
        "music_assistant.providers.openai_compatible.image_generation",
        AsyncMock(return_value=(b"img", True)),
    ) as generate:
        assert await provider.ai_generate_image("A cat", engine_id="gpt-image-1") == b"img"
        await provider.ai_generate_image("A dog", engine_id="gpt-image-1")

    assert generate.call_args_list[0].kwargs["use_images_endpoint"] is False
    assert generate.call_args_list[1].kwargs["use_images_endpoint"] is True


async def test_ai_generate_image_raises_without_image_models(
    provider: OpenAICompatibleProvider,
) -> None:
    """Refuse to generate when no image model is selected."""
    await provider.handle_async_init()

    with pytest.raises(UnsupportedFeaturedException):
        await provider.ai_generate_image("A cat")


async def test_get_config_entries_offers_the_listing_for_image_models(
    provider: OpenAICompatibleProvider, mass: AsyncMock
) -> None:
    """The image model picker is filled from the same listing as the text models."""
    _configure_models(provider, [])
    mass.http_session.request = MagicMock(
        return_value=_response_cm(response=_json_response(200, {"data": [{"id": "flux"}]}))
    )

    entries = await provider.get_config_entries()

    image_entry = next(entry for entry in entries if entry.key == CONF_IMAGE_MODELS)
    assert image_entry.multi_value is True
    assert [option.value for option in image_entry.options] == ["flux"]


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
    response.content_length = None
    body = json.dumps(payload).encode()
    sent = False

    async def read(_size: int = -1) -> bytes:
        # the body once, then the end of it from then on, as an exhausted stream does
        nonlocal sent
        if sent:
            return b""
        sent = True
        return body

    response.content.read = read
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
    return _build_provider(mass, setup_data)


def _build_provider(mass: AsyncMock, setup_data: dict[str, Any]) -> OpenAICompatibleProvider:
    """Build a provider on the given mass, backed by the given setup_data."""
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


def _entry(session: _FakeSession, key: str, step_id: str = "connection") -> Any:
    """Return an entry of the given step as the flow last rendered it."""
    return next(entry for entry in session.forms[step_id] if entry.key == key)


def _entry_value(session: _FakeSession, key: str, step_id: str = "connection") -> Any:
    """Return the prefilled value of an entry of the given step."""
    return _entry(session, key, step_id).value


async def _run_setup(session: _FakeSession, models: AsyncMock | None = None) -> None:
    """
    Drive the setup flow against a fake session.

    :param session: The fake session replaying the submissions.
    :param models: Stand-in for the model listing, a reachable endpoint without models if omitted.
    """
    with patch.object(setup_flow, "list_models", models or AsyncMock(return_value=[])):
        await setup_flow.run_setup(cast("SetupSession", session))


class _FakeSession:
    """Minimal stand-in for SetupSession that replays scripted form submissions."""

    def __init__(
        self,
        setup_data: dict[str, Any],
        submissions: list[dict[str, Any]],
        values: dict[str, Any] | None = None,
    ) -> None:
        """Initialize with the stored setup_data, options and the submissions to replay."""
        self.context = SimpleNamespace(setup_data=setup_data, values=values or {})
        self.mass = MagicMock()
        # the entries and errors each step was last rendered with, by step id
        self.forms: dict[str, list[Any]] = {}
        self.errors: dict[str, Any] = {}
        self.steps: list[str] = []
        self.finished: dict[str, Any] | None = None
        self.finished_options: dict[str, Any] | None = None
        self._submissions = list(submissions)

    async def form(
        self, entries: list[Any], step_id: str = "user", **kwargs: Any
    ) -> dict[str, Any]:
        """Record the rendered entries and return the next scripted submission."""
        self.forms[step_id] = entries
        self.errors[step_id] = kwargs.get("errors")
        self.steps.append(step_id)
        return dict(self._submissions.pop(0))

    async def finish(
        self, values: dict[str, Any], options: dict[str, Any] | None = None
    ) -> dict[str, str]:
        """Record the values the flow decided to persist."""
        self.finished = dict(values)
        self.finished_options = dict(options or {})
        return {"instance_id": "openai_compatible--new"}
