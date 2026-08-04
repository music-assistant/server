"""Setup flow for the OpenAI Text-to-speech provider."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiohttp import ClientError, ClientTimeout
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from music_assistant.models.setup_flow import SetupFlowError

from . import (
    CONF_API_KEY,
    CONF_BASE_URL,
    CONF_MODEL,
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    DEFAULT_VOICES,
    RESPONSE_FORMAT,
    fetch_backend_voices,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType

    from music_assistant.models.setup_flow import SetupSession

# short throwaway phrase, only rendered to verify the endpoint and credentials
VALIDATION_MESSAGE = "Hello"
# the user is waiting on the form, so do not sit on an unresponsive endpoint
VALIDATION_TIMEOUT = ClientTimeout(total=30)


async def run_setup(session: SetupSession) -> None:
    """
    Run the OpenAI Text-to-speech setup flow.

    Collects the API endpoint, the (optional) API key and the model to use, then verifies
    them by rendering a short phrase on the speech endpoint.

    :param session: The setup session driving the flow.
    """
    setup_data = dict(session.context.setup_data)
    errors: dict[str, str] | None = None
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
            ],
            step_id="user",
            errors=errors,
            last_step=True,
        )
        setup_data.update(values)
        base_url = str(values[CONF_BASE_URL]).strip().rstrip("/")
        api_key = str(values.get(CONF_API_KEY) or "").strip()
        model = str(values[CONF_MODEL]).strip()

        try:
            await _validate_credentials(session, base_url, api_key, model)
        except SetupFlowError as err:
            errors = {"base": err.translation_key or str(err)}
            continue

        finish_values: dict[str, ConfigValueType] = {
            CONF_BASE_URL: base_url,
            CONF_MODEL: model,
        }
        if api_key:
            finish_values[CONF_API_KEY] = api_key
        try:
            await session.finish(finish_values)
            return
        except SetupFlowError as err:
            errors = {"base": err.translation_key or str(err)}


async def _validate_credentials(
    session: SetupSession, base_url: str, api_key: str, model: str
) -> None:
    """
    Render a short throwaway phrase to verify the endpoint accepts the given credentials.

    :param session: The setup session driving the flow.
    :param base_url: The API endpoint to validate, without trailing slash.
    :param api_key: The API key to authenticate with, empty for backends without auth.
    :param model: The speech model to validate.
    """
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    # self-hosted backends serve their own voice names, so a standard voice may not exist
    advertised = await fetch_backend_voices(session.mass.http_session, base_url, api_key)
    payload = {
        "model": model,
        "voice": advertised[0] if advertised else DEFAULT_VOICES[0],
        "input": VALIDATION_MESSAGE,
        "response_format": RESPONSE_FORMAT,
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
                detail = (await response.text())[:200]
                raise SetupFlowError(f"Unexpected response: {detail}", "cannot_connect")
            await response.read()
    except (ClientError, TimeoutError) as err:
        raise SetupFlowError(f"Connection failed: {err}", "cannot_connect") from err
