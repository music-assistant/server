"""Tests for the OpenAI Compatible plugin provider."""

import base64
import json
from collections.abc import Iterator
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import ClientConnectorDNSError, ClientError
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
from music_assistant.providers.openai_compatible.constants import (
    CONF_IMAGE_MODELS,
    CONF_MODELS,
    SERVICE_BASE_URLS,
    SERVICE_CUSTOM,
)

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


async def test_probe_reports_an_address_that_answers_with_an_error() -> None:
    """A wrong address must be caught during setup, not on the first AI query."""
    session = cast("SetupSession", SimpleNamespace(mass=MagicMock()))
    with patch.object(setup_flow, "list_models", AsyncMock(side_effect=InvalidDataError("404"))):
        result = await setup_flow._probe(session, {"base_url": BASE_URL})

    assert result == "cannot_connect"


async def test_run_setup_prefills_the_address_of_the_chosen_service() -> None:
    """Picking a known service fills in its address so it need not be looked up."""
    session = _FakeSession(setup_data={}, submissions=[{"service": "groq"}, {}])

    await _run_setup(session)

    assert _entry_value(session, "base_url") == SERVICE_BASE_URLS["groq"]


async def test_run_setup_leaves_the_address_empty_for_a_custom_service() -> None:
    """There is nothing to prefill for a service we do not know the address of."""
    session = _FakeSession(setup_data={}, submissions=[{"service": SERVICE_CUSTOM}, {}])

    await _run_setup(session)

    assert _entry_value(session, "base_url") == ""


async def test_run_setup_prefers_the_new_services_address_when_switching() -> None:
    """Moving an instance to another service must not keep the previous address."""
    session = _FakeSession(
        setup_data={"service": "openai", "base_url": SERVICE_BASE_URLS["openai"]},
        submissions=[{"service": "ollama"}, {}],
    )

    await _run_setup(session)

    assert _entry_value(session, "base_url") == SERVICE_BASE_URLS["ollama"]


async def test_run_setup_keeps_an_edited_address_for_the_same_service() -> None:
    """A hand-corrected address survives a reconfigure that keeps the same service."""
    edited = "http://nas.local:11434/v1"
    session = _FakeSession(
        setup_data={"service": "ollama", "base_url": edited},
        submissions=[{"service": "ollama"}, {}],
    )

    await _run_setup(session)

    assert _entry_value(session, "base_url") == edited


async def test_run_setup_keeps_the_stored_api_key_when_the_field_is_left_empty() -> None:
    """The stored key is never sent to the client, so an empty field must not wipe it."""
    session = _FakeSession(
        setup_data={"service": "openai", "base_url": BASE_URL, "api_key": "sk-existing"},
        submissions=[{"service": "openai"}, {"base_url": BASE_URL, "api_key": ""}],
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
        submissions=[{"service": "openai"}, {"base_url": BASE_URL, "api_key": "sk-new"}],
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
        ],
    )

    await _run_setup(session)

    assert session.finished == {"service": "ollama", "base_url": BASE_URL, "api_key": ""}


async def test_run_setup_offers_the_clear_option_only_when_a_key_is_stored() -> None:
    """A first-time setup has nothing to clear, so the option stays out of the form."""
    fresh = _FakeSession(setup_data={}, submissions=[{"service": "openai"}, {}])
    stored = _FakeSession(
        setup_data={"service": "openai", "base_url": BASE_URL, "api_key": "sk-existing"},
        submissions=[{"service": "openai"}, {"base_url": BASE_URL, "api_key": ""}],
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


@pytest.fixture(autouse=True)
def _image_session_on_mass() -> Iterator[None]:
    """Download generated images through the mocked shared session, unless a test says not."""

    def session(mass: AsyncMock, _endpoint_host: str) -> MagicMock:
        return _response_cm(response=mass.http_session)

    with (
        patch(
            "music_assistant.providers.openai_compatible.helpers._image_session",
            side_effect=session,
        ),
        # no proxy, whatever the environment the tests run in has
        patch("music_assistant.providers.openai_compatible.helpers.getproxies", return_value={}),
    ):
        yield


def _download_response(
    chunks: list[bytes],
    content_length: int | None = None,
    *,
    status: int = 200,
    location: str = "",
) -> MagicMock:
    """Build a fake aiohttp response streaming a downloaded image in the given chunks."""
    response = MagicMock()
    response.status = status
    response.headers = {"Location": location} if location else {}
    response.content_length = content_length
    response.content.read = AsyncMock(side_effect=[*chunks, b""])
    return response


async def _generate_by_url(mass: AsyncMock, url: str) -> bytes:
    """Run an images endpoint generation whose reply hands out the image by URL."""
    mass.http_session.request = MagicMock(
        return_value=_response_cm(response=_json_response(200, {"data": [{"url": url}]}))
    )
    image, _ = await helpers.image_generation(
        mass, BASE_URL, "", model="dall-e-3", prompt="A cat", timeout=10, use_images_endpoint=True
    )
    return image


async def test_image_generation_downloads_an_image_handed_out_by_url(mass: AsyncMock) -> None:
    """An images endpoint answering with a URL has the image downloaded from it."""
    mass.http_session.get = MagicMock(
        return_value=_response_cm(response=_download_response([b"png-", b"bytes"]))
    )

    assert await _generate_by_url(mass, "https://cdn.example.com/x.png") == b"png-bytes"
    assert mass.http_session.get.call_args.args[0] == "https://cdn.example.com/x.png"
    assert mass.http_session.get.call_args.kwargs["allow_redirects"] is False


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
        await _generate_by_url(mass, "https://cdn.example.com/x.png")


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/x.png",
        "http://10.0.1.5:8080/x.png",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/x.png",
    ],
)
async def test_image_links_to_local_addresses_are_refused(mass: AsyncMock, url: str) -> None:
    """A link naming a local address is refused without a request."""
    mass.http_session.get = MagicMock()

    with pytest.raises(InvalidDataError, match="local network"):
        await _generate_by_url(mass, url)

    mass.http_session.get.assert_not_called()


async def test_image_links_to_hosts_on_a_local_network_are_refused(mass: AsyncMock) -> None:
    """A host name the resolver refuses for a local address fails as a refused link."""
    refused = ClientConnectorDNSError(
        MagicMock(), helpers.LocalNetworkHostError("intranet.example.com")
    )
    mass.http_session.get = MagicMock(return_value=_response_cm(exc=refused))

    with pytest.raises(InvalidDataError, match=r"local network: intranet\.example\.com"):
        await _generate_by_url(mass, "https://intranet.example.com/x.png")


async def test_image_links_on_the_configured_endpoint_are_allowed(mass: AsyncMock) -> None:
    """A local service handing out links to itself still works, also by address."""
    mass.http_session.get = MagicMock(
        return_value=_response_cm(response=_download_response([b"png"]))
    )
    mass.http_session.request = MagicMock(
        return_value=_response_cm(
            response=_json_response(
                200, {"data": [{"url": "http://192.168.1.20:8080/generated/x.png"}]}
            )
        )
    )

    image, _ = await helpers.image_generation(
        mass,
        "http://192.168.1.20:8080/v1",
        "",
        model="dall-e-3",
        prompt="A cat",
        timeout=10,
        use_images_endpoint=True,
    )

    assert image == b"png"


async def test_other_services_on_the_endpoints_host_are_refused(mass: AsyncMock) -> None:
    """Only the endpoint itself is trusted, not another port or scheme on its address."""
    mass.http_session.get = MagicMock()
    for url in ("http://192.168.1.20:8123/api/states", "https://192.168.1.20:8080/x.png"):
        mass.http_session.request = MagicMock(
            return_value=_response_cm(response=_json_response(200, {"data": [{"url": url}]}))
        )
        with pytest.raises(InvalidDataError, match="local network"):
            await helpers.image_generation(
                mass,
                "http://192.168.1.20:8080/v1",
                "",
                model="dall-e-3",
                prompt="A cat",
                timeout=10,
                use_images_endpoint=True,
            )

    mass.http_session.get.assert_not_called()


@pytest.mark.parametrize(
    ("addresses", "allowed"),
    [
        (["93.184.215.14"], True),
        (["10.0.0.7"], False),
        # a name that does not resolve here is left to the proxy, as it is the one connecting
        ([], True),
    ],
)
async def test_behind_a_proxy_hosts_are_checked_before_the_download(
    mass: AsyncMock, addresses: list[str], allowed: bool
) -> None:
    """Through a proxy the host is resolved up front, and the proxy is used to download it."""
    mass.http_session.get = MagicMock(
        return_value=_response_cm(response=_download_response([b"png"]))
    )
    with (
        patch(
            "music_assistant.providers.openai_compatible.helpers.getproxies",
            return_value={"https": "http://proxy.lan:3128"},
        ),
        patch(
            "music_assistant.providers.openai_compatible.helpers.proxy_bypass", return_value=False
        ),
        patch(
            "music_assistant.providers.openai_compatible.helpers._host_addresses",
            AsyncMock(return_value=addresses),
        ),
    ):
        if allowed:
            assert await _generate_by_url(mass, "https://cdn.example.com/x.png") == b"png"
        else:
            with pytest.raises(InvalidDataError, match="local network"):
                await _generate_by_url(mass, "https://cdn.example.com/x.png")

    assert mass.http_session.get.called is allowed
    if allowed:
        assert mass.http_session.get.call_args.kwargs["proxy"] == "http://proxy.lan:3128"


@pytest.mark.parametrize(
    ("proxies", "bypassed", "url"),
    [
        # a proxy for https only, so an http link goes direct
        ({"https": "http://proxy.lan:3128"}, False, "http://cdn.example.com/x.png"),
        # a host the proxy is bypassed for
        ({"https": "http://proxy.lan:3128"}, True, "https://cdn.example.com/x.png"),
    ],
)
async def test_links_that_go_direct_are_checked_as_they_connect(
    mass: AsyncMock, proxies: dict[str, str], bypassed: bool, url: str
) -> None:
    """A link the proxy is not used for goes through the checked session, without a proxy."""
    mass.http_session.get = MagicMock(
        return_value=_response_cm(response=_download_response([b"png"]))
    )
    with (
        patch(
            "music_assistant.providers.openai_compatible.helpers.getproxies",
            return_value=proxies,
        ),
        patch(
            "music_assistant.providers.openai_compatible.helpers.proxy_bypass",
            return_value=bypassed,
        ),
        patch(
            "music_assistant.providers.openai_compatible.helpers._host_addresses", AsyncMock()
        ) as host_addresses,
    ):
        assert await _generate_by_url(mass, url) == b"png"

    assert mass.http_session.get.call_args.kwargs["proxy"] is None
    # resolved by the session's resolver as it connects, not up front
    host_addresses.assert_not_awaited()


async def test_the_session_trusts_the_endpoint_and_the_proxies(mass: AsyncMock) -> None:
    """The proxy is often on the local network, so the session's resolver lets it through."""
    mass.http_session.get = MagicMock(
        return_value=_response_cm(response=_download_response([b"png"]))
    )
    with (
        patch(
            "music_assistant.providers.openai_compatible.helpers.getproxies",
            return_value={"https": "http://proxy.lan:3128", "socks": "socks5://s.lan:1080"},
        ),
        patch(
            "music_assistant.providers.openai_compatible.helpers.proxy_bypass", return_value=False
        ),
        patch(
            "music_assistant.providers.openai_compatible.helpers._host_addresses",
            AsyncMock(return_value=["93.184.215.14"]),
        ),
    ):
        await _generate_by_url(mass, "https://cdn.example.com/x.png")

    cast("MagicMock", helpers._image_session).assert_called_once_with(
        mass, [("https", "api.example.com", 443), ("http", "proxy.lan", 3128)]
    )


async def test_an_ipv6_endpoint_links_to_itself(mass: AsyncMock) -> None:
    """An endpoint by IPv6 address is matched to its own links."""
    mass.http_session.get = MagicMock(
        return_value=_response_cm(response=_download_response([b"png"]))
    )
    mass.http_session.request = MagicMock(
        return_value=_response_cm(
            response=_json_response(200, {"data": [{"url": "http://[fd00::5]:8080/x.png"}]})
        )
    )

    image, _ = await helpers.image_generation(
        mass,
        "http://[fd00::5]:8080/v1",
        "",
        model="dall-e-3",
        prompt="A cat",
        timeout=10,
        use_images_endpoint=True,
    )

    assert image == b"png"


async def test_image_link_redirects_are_checked_at_every_hop(mass: AsyncMock) -> None:
    """A redirect is followed by hand, so its target is checked like the link itself."""
    mass.http_session.get = MagicMock(
        return_value=_response_cm(
            response=_download_response([], status=302, location="http://10.0.0.7/x.png")
        )
    )

    with pytest.raises(InvalidDataError, match="local network"):
        await _generate_by_url(mass, "https://images.example.com/x.png")

    assert mass.http_session.get.call_count == 1


async def test_image_link_redirects_to_public_hosts_are_followed(mass: AsyncMock) -> None:
    """A redirect to another public location is followed to the image."""
    mass.http_session.get = MagicMock(
        side_effect=[
            _response_cm(response=_download_response([], status=302, location="/final.png")),
            _response_cm(response=_download_response([b"png"])),
        ]
    )

    assert await _generate_by_url(mass, "https://images.example.com/x.png") == b"png"
    assert mass.http_session.get.call_args.args[0] == "https://images.example.com/final.png"


def _resolved(*addresses: str) -> list[dict[str, Any]]:
    """Return resolver results for the given addresses."""
    return [
        {"hostname": "h", "host": address, "port": 443, "family": 0, "proto": 0, "flags": 0}
        for address in addresses
    ]


@pytest.mark.parametrize(
    "addresses",
    [("10.0.0.7",), ("93.184.215.14", "192.168.1.20"), ("fe80::1%eth0",), ("::1",), ()],
)
async def test_the_resolver_refuses_hosts_with_a_local_address(
    addresses: tuple[str, ...],
) -> None:
    """A host with any local address, or none at all, is refused as it is connected to."""
    resolver = helpers.PublicHostResolver([("https", "api.example.com", 443)])
    with (
        patch.object(
            helpers.ThreadedResolver, "resolve", AsyncMock(return_value=_resolved(*addresses))
        ),
        pytest.raises(helpers.LocalNetworkHostError),
    ):
        await resolver.resolve("images.example.com", 443)


async def test_the_resolver_passes_public_hosts_and_the_endpoint() -> None:
    """Public hosts resolve as usual, and so does the endpoint itself, but not its neighbours."""
    resolver = helpers.PublicHostResolver([("http", "localai.lan", 8080)])
    with patch.object(
        helpers.ThreadedResolver,
        "resolve",
        AsyncMock(
            side_effect=[
                _resolved("93.184.215.14"),
                _resolved("192.168.1.20"),
                _resolved("192.168.1.20"),
            ]
        ),
    ):
        assert await resolver.resolve("images.example.com", 443) == _resolved("93.184.215.14")
        assert await resolver.resolve("LocalAI.lan", 8080) == _resolved("192.168.1.20")
        # another service on the endpoint's host is not the endpoint
        with pytest.raises(helpers.LocalNetworkHostError):
            await resolver.resolve("localai.lan", 8123)


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


@pytest.mark.parametrize(
    ("address", "public"),
    [
        ("93.184.215.14", True),
        ("2606:2800:21f:cb07:6820:80da:af6b:8b2c", True),
        ("::ffff:10.0.0.7", False),
        ("2002:a00:7::1", False),
        ("64:ff9b::a00:7", False),
        ("64:ff9b::5db8:d70e", True),
        ("::a00:7", False),
        ("fe80::1%eth0", False),
    ],
)
def test_addresses_carrying_a_local_ipv4_address_are_not_public(address: str, public: bool) -> None:
    """An IPv6 address that routes to a local IPv4 address counts as local."""
    assert helpers._is_public(address) is public


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
        # the body, then the end of it, every time the response is read
        nonlocal sent
        sent = not sent
        return body if sent else b""

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


def _entry_value(session: _FakeSession, key: str) -> Any:
    """Return the prefilled value of an entry on the last form the flow rendered."""
    return next(entry.value for entry in session.entries if entry.key == key)


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
