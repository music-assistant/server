"""Tests for the AI Radio setup flow."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import FlowStepType, ProviderFeature

from music_assistant.models.plugin import AIEngine, PluginProvider, TTSEngine
from music_assistant.models.setup_flow import (
    AbortFlow,
    SetupFlowContext,
    SetupSession,
)
from music_assistant.providers.ai_radio import setup_flow


def _create_plugin(instance_id: str, ai_ids: list[str], tts_ids: list[str]) -> MagicMock:
    """Create a mock plugin provider exposing the given AI and TTS engines."""
    provider = MagicMock(spec=PluginProvider)
    provider.instance_id = instance_id
    provider.get_ai_engines = AsyncMock(
        return_value=[
            AIEngine(id=engine_id, name=engine_id, provider=provider) for engine_id in ai_ids
        ]
    )
    provider.get_tts_engines = AsyncMock(
        return_value=[
            TTSEngine(id=engine_id, name=engine_id, provider=provider) for engine_id in tts_ids
        ]
    )
    return provider


def _create_mass(*plugins: MagicMock, locale: str = "en_US") -> MagicMock:
    """Create a mock MusicAssistant serving the given plugins for the engine features."""
    mass = MagicMock()
    mass.metadata = SimpleNamespace(locale=locale)

    def _providers(feature: ProviderFeature, priority: Any = ()) -> list[MagicMock]:  # noqa: ARG001
        if feature == ProviderFeature.AI_QUERY:
            return [plugin for plugin in plugins if plugin.get_ai_engines.return_value]
        return [plugin for plugin in plugins if plugin.get_tts_engines.return_value]

    mass.get_providers_supporting_feature.side_effect = _providers
    return mass


def _create_session(
    mass: MagicMock,
    collected: dict[str, Any],
    setup_data: dict[str, Any] | None = None,
) -> SetupSession:
    """Create a setup session recording the values handed to finish()."""

    async def finish(_session: SetupSession, values: dict[str, Any]) -> dict[str, str]:
        collected.update(values)
        return {"instance_id": "ai_radio--1"}

    return SetupSession(
        mass,
        "flow1",
        SetupFlowContext(
            kind="setup",
            reason="user",
            domain="ai_radio",
            setup_data=setup_data or {},
        ),
        finish,
    )


async def _wait_for_form(session: SetupSession) -> Any:
    """Wait until the flow published its form step."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        step = session.current_step
        if step is not None and step.type == FlowStepType.FORM:
            return step
        await asyncio.sleep(0.01)
    raise AssertionError("setup flow did not publish a form step")


async def test_setup_collects_both_engines() -> None:
    """The single form offers an AI and a TTS picker and persists the selection."""
    mass = _create_mass(_create_plugin("hass--1", ["ai_task.a"], ["tts.a"]))
    collected: dict[str, Any] = {}
    session = _create_session(mass, collected)

    task = asyncio.create_task(setup_flow.run_setup(session))
    step = await _wait_for_form(session)
    assert [entry.key for entry in step.entries] == [
        "ai_engine",
        "tts_engine",
        "weather_city",
        "weather_country",
    ]
    assert [option.value for option in step.entries[0].options] == ["hass--1/ai_task.a"]
    assert (
        session.handle_submit({"ai_engine": "hass--1/ai_task.a", "tts_engine": "hass--1/tts.a"})
        is None
    )
    await task

    assert collected == {
        "ai_engine": "hass--1/ai_task.a",
        "tts_engine": "hass--1/tts.a",
        "weather_city": "",
        "weather_country": "US",
    }


async def test_setup_offers_the_weather_location() -> None:
    """A city/country submitted at setup is persisted alongside the engine picks."""
    mass = _create_mass(_create_plugin("hass--1", ["ai_task.a"], ["tts.a"]))
    collected: dict[str, Any] = {}
    session = _create_session(mass, collected)

    task = asyncio.create_task(setup_flow.run_setup(session))
    await _wait_for_form(session)
    session.handle_submit(
        {
            "ai_engine": "hass--1/ai_task.a",
            "tts_engine": "hass--1/tts.a",
            "weather_city": "Amsterdam",
            "weather_country": "NL",
        }
    )
    await task

    assert collected["weather_city"] == "Amsterdam"
    assert collected["weather_country"] == "NL"


async def test_setup_rejects_a_submission_without_a_choice() -> None:
    """Both engines are mandatory, so an empty submission re-shows the form with an error."""
    mass = _create_mass(_create_plugin("hass--1", ["ai_task.a"], ["tts.a"]))
    collected: dict[str, Any] = {}
    session = _create_session(mass, collected)

    task = asyncio.create_task(setup_flow.run_setup(session))
    await _wait_for_form(session)
    step = session.handle_submit({})
    assert step is not None
    assert step.errors
    assert not collected

    session.handle_submit({"ai_engine": "hass--1/ai_task.a", "tts_engine": "hass--1/tts.a"})
    await task


async def test_setup_prefills_the_previous_selection() -> None:
    """A reconfigure run starts from the values collected by the previous run."""
    mass = _create_mass(_create_plugin("hass--1", ["ai_task.a"], ["tts.a"]))
    session = _create_session(mass, {}, setup_data={"tts_engine": "hass--1/tts.a"})

    task = asyncio.create_task(setup_flow.run_setup(session))
    step = await _wait_for_form(session)
    assert step.entries[0].value is None
    assert step.entries[1].value == "hass--1/tts.a"
    session.handle_submit({"ai_engine": "hass--1/ai_task.a", "tts_engine": "hass--1/tts.a"})
    await task


async def test_setup_aborts_without_an_ai_engine() -> None:
    """No AI engine means the user has to set up a plugin providing AI first."""
    mass = _create_mass(_create_plugin("hass--1", [], ["tts.a"]))

    with pytest.raises(AbortFlow) as error:
        await setup_flow.run_setup(_create_session(mass, {}))

    assert error.value.reason == "no_ai_engine"


async def test_setup_aborts_without_a_tts_engine() -> None:
    """No TTS engine means the user has to set up a plugin providing speech first."""
    mass = _create_mass(_create_plugin("hass--1", ["ai_task.a"], []))

    with pytest.raises(AbortFlow) as error:
        await setup_flow.run_setup(_create_session(mass, {}))

    assert error.value.reason == "no_tts_engine"
