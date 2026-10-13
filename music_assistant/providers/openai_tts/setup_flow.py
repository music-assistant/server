"""Setup flow for the OpenAI Text-to-speech provider."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiohttp import ClientError, ClientPayloadError, ClientTimeout
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType

from music_assistant.models.setup_flow import SetupFlowError

from . import (
    CONF_API_KEY,
    CONF_BASE_URL,
    CONF_MODEL,
    CONF_RESPONSE_FORMAT,
    CONF_VOICES,
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    DEFAULT_RESPONSE_FORMAT,
    DEFAULT_VOICES,
    RESPONSE_FORMATS,
    fetch_backend_voices,
    normalize_base_url,
    parse_voices,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType

    from music_assistant.models.setup_flow import SetupSession

# short throwaway phrase, only rendered to verify the endpoint and credentials
VALIDATION_MESSAGE = "Hello"
# the user is waiting on the form, so do not sit on an unresponsive endpoint
VALIDATION_TIMEOUT = ClientTimeout(total=30)
# the errors of a test render the server accepted but could not complete
RENDER_ERRORS = ("render_failed", "render_incomplete")


async def run_setup(session: SetupSession) -> None:
    """
    Run the OpenAI Text-to-speech setup flow.

    Collects the API endpoint, the (optional) API key, the model and the response format
    to use, then verifies them by rendering a short phrase on the speech endpoint. When
    that fails and the server does not list its voices, the user is asked to name them.

    :param session: The setup session driving the flow.
    """
    setup_data = dict(session.context.setup_data)
    errors: dict[str, str | SetupFlowError] | None = None
    ask_voices = bool(setup_data.get(CONF_VOICES))
    while True:
        values = await session.form(
            [
                ConfigEntry(
                    key=CONF_BASE_URL,
                    type=ConfigEntryType.STRING,
                    required=True,
                    default_value=DEFAULT_BASE_URL,
                    value=setup_data.get(CONF_BASE_URL),
                ),
                ConfigEntry(
                    key=CONF_API_KEY,
                    type=ConfigEntryType.SECURE_STRING,
                    required=False,
                    value=setup_data.get(CONF_API_KEY),
                ),
                # free text: self-hosted backends serve arbitrary model names
                ConfigEntry(
                    key=CONF_MODEL,
                    type=ConfigEntryType.STRING,
                    required=True,
                    default_value=DEFAULT_MODEL,
                    value=setup_data.get(CONF_MODEL),
                ),
                ConfigEntry(
                    key=CONF_RESPONSE_FORMAT,
                    type=ConfigEntryType.STRING,
                    required=True,
                    options=[ConfigValueOption(fmt) for fmt in RESPONSE_FORMATS],
                    default_value=DEFAULT_RESPONSE_FORMAT,
                    value=setup_data.get(CONF_RESPONSE_FORMAT),
                ),
                # only asked for once the server turned out to need voices it does not list
                *(
                    [
                        ConfigEntry(
                            key=CONF_VOICES,
                            type=ConfigEntryType.STRING,
                            multi_value=True,
                            required=False,
                            default_value=[],
                            value=setup_data.get(CONF_VOICES),
                        )
                    ]
                    if ask_voices
                    else []
                ),
            ],
            step_id="user",
            errors=errors,
            last_step=True,
        )
        setup_data.update(values)
        base_url = normalize_base_url(str(values[CONF_BASE_URL]))
        api_key = str(values.get(CONF_API_KEY) or "").strip()
        model = str(values[CONF_MODEL]).strip()
        response_format = str(values[CONF_RESPONSE_FORMAT])
        voices = parse_voices(values.get(CONF_VOICES))

        # self-hosted backends serve their own voice names, so a standard voice may not exist
        listed = await fetch_backend_voices(session.mass.http_session, base_url, api_key, model)
        voice = (voices or listed or DEFAULT_VOICES)[0]
        try:
            await _validate_credentials(session, base_url, api_key, model, response_format, voice)
        except SetupFlowError as err:
            errors = {"base": err}
            if err.translation_key in RENDER_ERRORS and not listed:
                # the voice may not exist on this server, and only the user can name its voices
                ask_voices = True
                errors[CONF_VOICES] = SetupFlowError("Voices not listed", "voices_not_listed")
            continue

        finish_values: dict[str, ConfigValueType] = {
            CONF_BASE_URL: base_url,
            CONF_MODEL: model,
            CONF_RESPONSE_FORMAT: response_format,
            # always stored, so a reconfigure that no longer needs them clears earlier voices
            CONF_VOICES: voices,
        }
        if api_key:
            finish_values[CONF_API_KEY] = api_key
        try:
            await session.finish(finish_values)
            return
        except SetupFlowError as err:
            errors = {"base": err}


async def _validate_credentials(
    session: SetupSession,
    base_url: str,
    api_key: str,
    model: str,
    response_format: str,
    voice: str,
) -> None:
    """
    Render a short throwaway phrase to verify the endpoint accepts the given credentials.

    :param session: The setup session driving the flow.
    :param base_url: The API endpoint to validate, without trailing slash.
    :param api_key: The API key to authenticate with, empty for backends without auth.
    :param model: The speech model to validate.
    :param response_format: The audio format to request the speech in.
    :param voice: The voice to render the phrase with.
    """
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    payload = {
        "model": model,
        "voice": voice,
        "input": VALIDATION_MESSAGE,
        "response_format": response_format,
    }
    try:
        async with session.mass.http_session.post(
            f"{base_url}/audio/speech",
            headers=headers,
            json=payload,
            timeout=VALIDATION_TIMEOUT,
        ) as response:
            if response.status in (401, 403):
                raise SetupFlowError("Authentication failed", "invalid_auth")
            if response.status == 404:
                raise SetupFlowError("Speech endpoint not found", "endpoint_not_found")
            if response.status != 200:
                detail = (await response.text()).strip()[:200]
                detail = detail or f"HTTP {response.status}"
                raise SetupFlowError(f"Speech request failed: {detail}", "render_failed", [detail])
            audio_data = await response.read()
    except ClientPayloadError as err:
        # some servers (mlx-audio) answer 200 OK and then abort the body when rendering fails
        raise SetupFlowError(f"Speech request aborted: {err}", "render_incomplete") from err
    except (ClientError, TimeoutError) as err:
        raise SetupFlowError(f"Connection failed: {err}", "cannot_connect") from err
    if not audio_data:
        raise SetupFlowError("Speech request returned no audio", "render_incomplete")
