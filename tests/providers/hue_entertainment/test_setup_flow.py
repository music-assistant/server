"""Tests for the Hue Lights Sync setup flow: the backend choice and the ZHA lamps."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock

import pytest
from hue_entertainment import ZigguratError
from music_assistant_models.config_entries import ConfigEntry, ConfigValueType

from music_assistant.models.setup_flow import AbortFlow
from music_assistant.providers.hue_entertainment import setup_flow
from music_assistant.providers.hue_entertainment.constants import (
    BACKEND_HUE_BRIDGE,
    BACKEND_ZHA,
    CONF_BACKEND,
    CONF_BRIDGE_HOST,
    CONF_ZIGBEE_LIGHTS,
    CONF_ZIGBEE_NAME,
)
from music_assistant.providers.hue_entertainment.setup_flow import run_setup

from .fakes import ZIGGURAT_URL, FakeHass, FakeMass, zha_device

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession

type Answer = dict[str, ConfigValueType] | Callable[[], dict[str, ConfigValueType]]


class FormsExhaustedError(Exception):
    """The flow asked for more forms than the test answers."""


class FakeSetupSession:
    """Drives a setup flow with prepared form answers and records what it shows."""

    def __init__(
        self,
        mass: Any,
        answers: list[Answer],
        *,
        kind: str = "setup",
        setup_data: dict[str, Any] | None = None,
    ) -> None:
        """
        Initialize the session.

        :param mass: The (fake) MusicAssistant instance.
        :param answers: The submitted values of each form in order, or a callable returning them.
        :param kind: "setup" or "reconfigure".
        :param setup_data: The stored setup data, for a reconfigure.
        """
        self.mass = mass
        self.context = SimpleNamespace(kind=kind, setup_data=setup_data or {})
        self.forms: list[tuple[str, list[ConfigEntry], dict[str, Any] | None]] = []
        self.finished: dict[str, ConfigValueType] | None = None
        self._answers = list(answers)

    async def form(
        self,
        entries: list[ConfigEntry],
        step_id: str = "user",
        errors: dict[str, Any] | None = None,
        **_kwargs: Any,
    ) -> dict[str, ConfigValueType]:
        """Record the form and return the next answer."""
        self.forms.append((step_id, entries, errors))
        if not self._answers:
            raise FormsExhaustedError(step_id)
        answer = self._answers.pop(0)
        return answer() if callable(answer) else answer

    async def progress_until(self, awaitable: Awaitable[Any], *_args: Any, **_kwargs: Any) -> Any:
        """Wait for the progress step's work."""
        return await awaitable

    async def finish(self, values: dict[str, ConfigValueType]) -> dict[str, str]:
        """Record the stored values."""
        self.finished = values
        return {}


async def _run(session: FakeSetupSession) -> None:
    await run_setup(cast("SetupSession", session))


def _entry(entries: list[ConfigEntry], key: str) -> ConfigEntry:
    return next(entry for entry in entries if entry.key == key)


def _zha_hass() -> FakeHass:
    """Return Home Assistant with ZHA on Ziggurat and three Hue lamps in two rooms."""
    return FakeHass(
        [
            zha_device("aa", "Signe", nwk="0x000a", area_id="living"),
            zha_device("bb", "Go", nwk="0x000b", area_id="kitchen"),
            zha_device("cc", "Bloom", nwk="0x000c", area_id="kitchen"),
        ],
        areas={"living": "Living room", "kitchen": "Kitchen"},
    )


@pytest.fixture
def segments(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Stand in for reading the lamps' gradient segments from Ziggurat."""
    mock = AsyncMock(return_value={0x000A: 7, 0x000B: 1})
    monkeypatch.setattr(setup_flow, "read_segment_counts", mock)
    return mock


@pytest.mark.parametrize(
    ("hass", "zha_disabled"),
    [
        (None, True),
        (FakeHass(ziggurat_path="/dev/ttyUSB0"), True),
        (FakeHass(), False),
    ],
    ids=["no_hass", "no_ziggurat", "zha_on_ziggurat"],
)
async def test_zha_option_needs_hass_with_ziggurat(
    hass: FakeHass | None, zha_disabled: bool
) -> None:
    """The ZHA option is greyed out unless the Home Assistant plugin runs ZHA on Ziggurat."""
    session = FakeSetupSession(FakeMass(hass), [])

    with pytest.raises(FormsExhaustedError):
        await _run(session)

    step_id, entries, _errors = session.forms[0]
    assert step_id == "backend"
    options = {option.value: option for option in _entry(entries, CONF_BACKEND).options}
    assert not options[BACKEND_HUE_BRIDGE].disabled
    assert options[BACKEND_ZHA].disabled is zha_disabled


async def test_disabled_zha_choice_is_refused() -> None:
    """Submitting the greyed out ZHA option shows the backend step again with an error."""
    session = FakeSetupSession(
        FakeMass(None), [{CONF_BACKEND: BACKEND_ZHA}, {CONF_BACKEND: BACKEND_HUE_BRIDGE}]
    )

    with pytest.raises(FormsExhaustedError):
        await _run(session)

    assert [(step_id, errors) for step_id, _entries, errors in session.forms] == [
        ("backend", None),
        ("backend", {"base": "zha_unavailable"}),
        ("user", None),
    ]


async def test_zha_lamps_step_stores_name_and_lamps(segments: AsyncMock) -> None:
    """The chosen lamps are stored in option order with their zones, next to the name."""
    session = FakeSetupSession(
        FakeMass(_zha_hass()),
        [
            {CONF_BACKEND: BACKEND_ZHA},
            {CONF_ZIGBEE_NAME: " Party ", CONF_ZIGBEE_LIGHTS: ["aa", "cc", "bb"]},
        ],
    )

    await _run(session)

    _step_id, entries, _errors = session.forms[1]
    assert [option.title for option in _entry(entries, CONF_ZIGBEE_LIGHTS).options] == [
        "Kitchen · Bloom",
        "Kitchen · Go",
        "Living room · Signe",
    ]
    # Bloom (cc) does not answer the segment read and gets one zone; Signe is capped at three
    segments.assert_awaited_once_with(ZIGGURAT_URL, [0x000C, 0x000B, 0x000A])
    assert session.finished == {
        CONF_BACKEND: BACKEND_ZHA,
        CONF_ZIGBEE_NAME: "Party",
        CONF_ZIGBEE_LIGHTS: ["cc/1", "bb/1", "aa/3"],
    }


@pytest.mark.usefixtures("segments")
async def test_empty_name_follows_the_rooms() -> None:
    """Without a name the player is named after the rooms of the chosen lamps."""
    session = FakeSetupSession(
        FakeMass(_zha_hass()),
        [
            {CONF_BACKEND: BACKEND_ZHA},
            {CONF_ZIGBEE_NAME: "", CONF_ZIGBEE_LIGHTS: ["aa", "bb", "cc"]},
        ],
    )

    await _run(session)

    assert session.finished is not None
    assert session.finished[CONF_ZIGBEE_NAME] == "Kitchen & Living room"


@pytest.mark.usefixtures("segments")
async def test_empty_lamp_choice_is_refused() -> None:
    """Choosing no lamps shows the lamps step again with an error."""
    session = FakeSetupSession(
        FakeMass(_zha_hass()),
        [
            {CONF_BACKEND: BACKEND_ZHA},
            {CONF_ZIGBEE_NAME: "", CONF_ZIGBEE_LIGHTS: []},
            {CONF_ZIGBEE_NAME: "", CONF_ZIGBEE_LIGHTS: ["bb"]},
        ],
    )

    await _run(session)

    assert session.forms[2][2] == {CONF_ZIGBEE_LIGHTS: "required"}
    assert session.finished is not None
    assert session.finished[CONF_ZIGBEE_LIGHTS] == ["bb/1"]


async def test_unreachable_ziggurat_returns_to_the_lamps_step(segments: AsyncMock) -> None:
    """When Ziggurat can not be reached, the lamps step shows again with the entered values."""
    segments.side_effect = [ZigguratError("connection refused"), {0x000B: 1}]
    session = FakeSetupSession(
        FakeMass(_zha_hass()),
        [
            {CONF_BACKEND: BACKEND_ZHA},
            {CONF_ZIGBEE_NAME: "", CONF_ZIGBEE_LIGHTS: ["bb"]},
            {CONF_ZIGBEE_NAME: "", CONF_ZIGBEE_LIGHTS: ["aa", "bb"]},
        ],
    )

    await _run(session)

    _step_id, entries, errors = session.forms[2]
    assert errors == {"base": "ziggurat_unreachable"}
    assert _entry(entries, CONF_ZIGBEE_NAME).value is None
    assert _entry(entries, CONF_ZIGBEE_LIGHTS).value == ["bb"]
    assert session.finished is not None
    assert session.finished[CONF_ZIGBEE_NAME] == "Kitchen & Living room"


async def test_lamp_gone_from_zha_returns_to_the_lamps_step(segments: AsyncMock) -> None:
    """A chosen lamp that left ZHA before the check shows the lamps step again."""
    hass = _zha_hass()

    def _choose_aa_after_it_left() -> dict[str, ConfigValueType]:
        hass.commands["zha/devices"] = [zha_device("bb", "Go", nwk="0x000b")]
        return {CONF_ZIGBEE_NAME: "", CONF_ZIGBEE_LIGHTS: ["aa"]}

    session = FakeSetupSession(
        FakeMass(hass), [{CONF_BACKEND: BACKEND_ZHA}, _choose_aa_after_it_left]
    )

    with pytest.raises(FormsExhaustedError):
        await _run(session)

    assert session.forms[2][2] == {CONF_ZIGBEE_LIGHTS: "lights_not_found"}
    segments.assert_not_awaited()


@pytest.mark.usefixtures("segments")
async def test_reconfigure_skips_the_backend_step_and_prefills() -> None:
    """A ZHA reconfigure starts at the lamps step, prefilled without lamps that left ZHA."""
    setup_data = {
        CONF_BACKEND: BACKEND_ZHA,
        CONF_ZIGBEE_NAME: "Downstairs",
        CONF_ZIGBEE_LIGHTS: ["aa/3", "gone/1", "bb/1"],
    }
    session = FakeSetupSession(
        FakeMass(_zha_hass()),
        [{CONF_ZIGBEE_NAME: "Downstairs", CONF_ZIGBEE_LIGHTS: ["bb", "aa"]}],
        kind="reconfigure",
        setup_data=setup_data,
    )

    await _run(session)

    step_id, entries, _errors = session.forms[0]
    assert step_id == "zigbee_lights"
    assert _entry(entries, CONF_ZIGBEE_NAME).value == "Downstairs"
    assert _entry(entries, CONF_ZIGBEE_LIGHTS).value == ["bb", "aa"]
    assert session.finished == {
        CONF_BACKEND: BACKEND_ZHA,
        CONF_ZIGBEE_NAME: "Downstairs",
        CONF_ZIGBEE_LIGHTS: ["bb/1", "aa/3"],
    }


async def test_reconfigure_without_ha_aborts() -> None:
    """A ZHA reconfigure without the Home Assistant plugin aborts the flow."""
    session = FakeSetupSession(
        FakeMass(None), [], kind="reconfigure", setup_data={CONF_BACKEND: BACKEND_ZHA}
    )

    with pytest.raises(AbortFlow, match="zha_unavailable"):
        await _run(session)


async def test_reconfigure_of_a_hue_bridge_instance_pairs_again() -> None:
    """An instance stored without a backend is a Hue bridge one: it goes straight to pairing."""
    session = FakeSetupSession(
        FakeMass(_zha_hass()),
        [],
        kind="reconfigure",
        setup_data={CONF_BRIDGE_HOST: "192.168.1.20"},
    )

    with pytest.raises(FormsExhaustedError):
        await _run(session)

    step_id, entries, _errors = session.forms[0]
    assert step_id == "user"
    assert _entry(entries, CONF_BRIDGE_HOST).value == "192.168.1.20"
