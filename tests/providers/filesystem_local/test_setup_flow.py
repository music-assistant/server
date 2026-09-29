"""Tests for the Local files setup flow: which folders a music source may read from."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncGenerator, Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from music_assistant_models.enums import ConfigEntryType, FlowStepType

from music_assistant.controllers.storage import StorageController, StorageKind, StorageUsage
from music_assistant.mass import MusicAssistant
from music_assistant.models.setup_flow import SetupFlowContext, SetupSession
from music_assistant.providers.filesystem_local.constants import CONF_CONTENT_TYPE
from music_assistant.providers.filesystem_local.setup_flow import run_setup
from tests.controllers.storage.conftest import make_location, set_locations

if TYPE_CHECKING:
    from mashumaro import DataClassDictMixin
    from music_assistant_models.config_entries import ConfigValueType
    from music_assistant_models.setup_flow import SetupFlowStep

MEMBER_AND_ADMIN = pytest.mark.parametrize(
    "manages_all_sources", [False, True], ids=["member", "admin"]
)


class _Flow:
    """A running Local files setup flow, driven the way a client drives it."""

    def __init__(
        self, mass: MusicAssistant, manages_all_sources: bool, setup_data: dict[str, Any]
    ) -> None:
        """
        Start the flow.

        :param mass: The server the flow runs on.
        :param manages_all_sources: Whether the caller manages every music source.
        :param setup_data: The stored setup data of the source to reconfigure, empty for a
            new source.
        """
        self.finished_with: dict[str, Any] | None = None
        context = SetupFlowContext(
            kind="reconfigure" if setup_data else "setup",
            reason="user",
            domain="filesystem_local",
            setup_data=setup_data,
            manages_all_sources=manages_all_sources,
        )
        self.session = SetupSession(mass, "flow", context, self._finish)
        self.task = asyncio.create_task(run_setup(self.session))

    async def form(self) -> SetupFlowStep:
        """Return the form the flow shows."""
        if self.session.current_step is None:
            await self.session.wait_for_step_change(5)
        step = self.session.current_step
        assert step is not None
        assert step.type == FlowStepType.FORM
        return step

    async def submit(self, path: Path | str, content_type: str = "music") -> SetupFlowStep:
        """
        Submit the form and return the step the flow shows next.

        :param path: The folder to submit.
        :param content_type: The content type to submit.
        """
        await self.form()
        values: dict[str, ConfigValueType] = {CONF_CONTENT_TYPE: content_type, "path": str(path)}
        assert self.session.handle_submit(values) is None
        await self.session.wait_for_step_change(5)
        step = self.session.current_step
        assert step is not None
        return step

    async def _finish(self, _session: SetupSession, values: dict[str, Any]) -> dict[str, str]:
        """Keep the values the flow finished with, in place of creating the source."""
        self.finished_with = dict(values)
        return {"instance_id": "filesystem_local--test"}


@pytest.fixture
async def start_flow(
    storage: StorageController,
) -> AsyncGenerator[Callable[..., Awaitable[_Flow]]]:
    """
    Provide a starter for Local files setup flows, cancelling the ones left running.

    :param storage: The storage controller of the server the flows run on.
    """
    flows: list[_Flow] = []

    async def _start(
        manages_all_sources: bool = True, setup_data: dict[str, Any] | None = None
    ) -> _Flow:
        flow = _Flow(storage.mass, manages_all_sources, setup_data or {})
        flows.append(flow)
        await flow.form()
        return flow

    try:
        yield _start
    finally:
        for flow in flows:
            flow.task.cancel()
            await asyncio.gather(flow.task, return_exceptions=True)


@pytest.fixture
def tree(tmp_path: Path, storage: StorageController) -> Path:
    """
    Provide folders in and around the storage locations, and hold those locations.

    ``media`` is a location every caller may use, ``admin_disk`` and ``media/private_disk`` are
    ones only admins see, and ``data`` is the data folder of the server (created by the minimal
    server).

    :param tmp_path: Temporary directory for the tree.
    :param storage: The storage controller that holds the locations.
    """
    media = tmp_path / "media"
    for folder in (
        media / "Music",
        media / "private_disk" / "Music",
        tmp_path / "outside" / "Music",
        tmp_path / "media-evil",
        tmp_path / "admin_disk" / "Music",
        tmp_path / "legacy",
    ):
        folder.mkdir(parents=True)
    (media / "track.mp3").write_bytes(b"")
    (media / "escape").symlink_to(tmp_path / "outside", target_is_directory=True)
    (media / "to_data").symlink_to(tmp_path / "data", target_is_directory=True)
    (media / "shortcut").symlink_to(media / "Music", target_is_directory=True)
    (media / "to_admin_disk").symlink_to(
        tmp_path / "admin_disk" / "Music", target_is_directory=True
    )
    (tmp_path / "outside" / "into_media").symlink_to(media / "Music", target_is_directory=True)
    set_locations(
        storage,
        make_location(media, kind=StorageKind.MANUAL),
        make_location(media / "private_disk", kind=StorageKind.LOCAL_DISK),
        make_location(tmp_path / "admin_disk", kind=StorageKind.LOCAL_DISK),
        make_location(tmp_path / "data", kind=StorageKind.LOCAL_DISK, usage=StorageUsage.DATA),
    )
    return tmp_path


def _error_key(step: SetupFlowStep) -> str:
    """Return the translation key of the error on the folder of a re-rendered form."""
    assert step.type == FlowStepType.FORM
    return step.error_translations["path"].key


def _record_calls(monkeypatch: pytest.MonkeyPatch, name: str) -> list[str]:
    """
    Record the paths a function of os.path is called with, and return that record.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param name: The name of the function in os.path.
    """
    calls: list[str] = []
    original = getattr(os.path, name)

    def _record(path: str, *args: Any, **kwargs: Any) -> Any:
        calls.append(os.fspath(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os.path, name, _record)
    return calls


@MEMBER_AND_ADMIN
@pytest.mark.parametrize("folder", ["media/Music", "media/shortcut"], ids=["folder", "symlink"])
async def test_a_folder_in_a_location_finishes(
    start_flow: Callable[..., Awaitable[_Flow]],
    tree: Path,
    manages_all_sources: bool,
    folder: str,
) -> None:
    """
    A folder in a location the caller may use finishes with the content type and the folder.

    :param manages_all_sources: Whether the caller manages every music source.
    :param folder: The picked folder, relative to the tree; a symlink that stays inside the
        location is kept as picked.
    """
    flow = await start_flow(manages_all_sources)

    step = await flow.submit(tree / folder, content_type="audiobooks")

    assert step.type == FlowStepType.FINISH
    assert flow.finished_with == {CONF_CONTENT_TYPE: "audiobooks", "path": str(tree / folder)}


@MEMBER_AND_ADMIN
@pytest.mark.parametrize(
    "path",
    [
        "{tmp}/outside/Music",
        "{tmp}/media-evil",
        "{tmp}/media/../outside/Music",
        "{tmp}",
        "/",
        "media/Music",
        "{tmp}/media/Music\0",
    ],
    ids=["outside", "look_alike", "traversal", "parent", "root", "relative", "nul"],
)
async def test_a_folder_outside_every_location_is_refused(
    start_flow: Callable[..., Awaitable[_Flow]],
    tree: Path,
    manages_all_sources: bool,
    path: str,
) -> None:
    """
    A folder outside every location is refused for every caller, and nothing is created.

    :param manages_all_sources: Whether the caller manages every music source.
    :param path: The submitted folder.
    """
    flow = await start_flow(manages_all_sources)

    step = await flow.submit(path.format(tmp=tree))

    assert _error_key(step) == "folder_not_allowed"
    assert flow.finished_with is None


@pytest.mark.parametrize("path", ["", "  "], ids=["empty", "blank"])
async def test_an_empty_folder_is_a_missing_value(
    start_flow: Callable[..., Awaitable[_Flow]], path: str
) -> None:
    """
    A folder left empty reads as a required value that is missing, like any required field.

    :param path: The submitted folder.
    """
    flow = await start_flow()

    step = await flow.submit(path)

    assert step.type == FlowStepType.FORM
    assert step.errors == {"path": "required"}
    assert flow.finished_with is None


async def test_a_refused_folder_can_be_corrected(
    start_flow: Callable[..., Awaitable[_Flow]], tree: Path
) -> None:
    """The form shows the refused folder again, and a folder that may be used then finishes."""
    flow = await start_flow(manages_all_sources=False)

    refused = await flow.submit(tree / "outside" / "Music")
    assert refused.errors.keys() == {"path"}
    assert next(e for e in refused.entries if e.key == "path").value == str(
        tree / "outside" / "Music"
    )
    finished = await flow.submit(tree / "media" / "Music")

    assert finished.type == FlowStepType.FINISH
    assert flow.finished_with == {CONF_CONTENT_TYPE: "music", "path": str(tree / "media" / "Music")}


@pytest.mark.parametrize(
    ("manages_all_sources", "allowed"), [(False, False), (True, True)], ids=["member", "admin"]
)
@pytest.mark.parametrize(
    "folder",
    ["admin_disk/Music", "media/private_disk/Music", "media/to_admin_disk"],
    ids=["folder", "nested_in_a_shared_location", "link_from_a_shared_location"],
)
async def test_a_location_only_admins_see_is_refused_to_a_member(
    start_flow: Callable[..., Awaitable[_Flow]],
    tree: Path,
    manages_all_sources: bool,
    allowed: bool,
    folder: str,
) -> None:
    """
    A member may not put a source on a location that was not made available to it.

    Also not when that location lies inside one the member may use, nor through a symlink in
    such a location: the member's rule also holds for the folder the link leads to.

    :param manages_all_sources: Whether the caller manages every music source.
    :param allowed: Whether the caller may use the location.
    :param folder: The picked folder, relative to the tree.
    """
    flow = await start_flow(manages_all_sources)

    step = await flow.submit(tree / folder)

    if allowed:
        assert step.type == FlowStepType.FINISH
    else:
        assert _error_key(step) == "folder_not_allowed"
        assert flow.finished_with is None


@MEMBER_AND_ADMIN
@pytest.mark.parametrize(
    "folder",
    [
        "media/escape",
        "media/escape/Music",
        "media/escape/missing",
        "media/to_data",
        "outside/into_media",
    ],
    ids=[
        "out_of_the_location",
        "below_the_link",
        "missing_below_the_link",
        "into_the_data_folder",
        "link_from_outside",
    ],
)
async def test_a_symlink_across_the_location_boundary_is_refused(
    start_flow: Callable[..., Awaitable[_Flow]],
    tree: Path,
    manages_all_sources: bool,
    folder: str,
) -> None:
    """
    A symlink is refused when it leads out of the locations or into the server's own folders.

    So is a symlink outside every location that leads into one: the folder is taken as given.
    A missing folder behind such a link is refused the same way, so the answer does not tell
    what exists outside the locations.

    :param manages_all_sources: Whether the caller manages every music source.
    :param folder: The picked folder, relative to the tree.
    """
    flow = await start_flow(manages_all_sources)

    step = await flow.submit(tree / folder)

    assert _error_key(step) == "folder_not_allowed"
    assert flow.finished_with is None


@pytest.mark.parametrize("folder", ["data", "to_data"], ids=["folder", "symlink"])
async def test_the_data_folder_is_refused_also_inside_a_media_location(
    start_flow: Callable[..., Awaitable[_Flow]],
    tmp_path: Path,
    storage: StorageController,
    folder: str,
) -> None:
    """
    The data folder of the server never holds a source, even inside a media location.

    :param folder: The picked folder: the data folder or a symlink to it, both in the location.
    """
    (tmp_path / "Music").mkdir()
    (tmp_path / "to_data").symlink_to(tmp_path / "data", target_is_directory=True)
    set_locations(
        storage,
        make_location(tmp_path, kind=StorageKind.MANUAL),
        make_location(tmp_path / "data", kind=StorageKind.LOCAL_DISK, usage=StorageUsage.DATA),
    )
    flow = await start_flow()

    refused = await flow.submit(tmp_path / folder)
    assert _error_key(refused) == "folder_not_allowed"
    finished = await flow.submit(tmp_path / "Music")

    assert finished.type == FlowStepType.FINISH


@pytest.mark.parametrize("folder", ["media/missing", "media/track.mp3"], ids=["missing", "file"])
async def test_what_is_no_folder_is_reported_as_missing(
    start_flow: Callable[..., Awaitable[_Flow]], tree: Path, folder: str
) -> None:
    """
    A path in a location that is no existing folder is reported as a missing folder.

    :param folder: The submitted path, relative to the tree.
    """
    flow = await start_flow()

    step = await flow.submit(tree / folder)

    assert _error_key(step) == "music_directory_not_found"
    assert step.error_translations["path"].args == [str(tree / folder)]
    assert flow.finished_with is None


@MEMBER_AND_ADMIN
async def test_a_folder_on_an_unavailable_location_is_not_touched(
    start_flow: Callable[..., Awaitable[_Flow]],
    tmp_path: Path,
    storage: StorageController,
    monkeypatch: pytest.MonkeyPatch,
    manages_all_sources: bool,
) -> None:
    """
    A folder on a location that is away is refused as such, without touching the folder.

    A share whose server is gone could hold up whatever touches it for as long as its mount
    waits for an answer.

    :param manages_all_sources: Whether the caller manages every music source.
    """
    nas = tmp_path / "nas"
    (nas / "Music").mkdir(parents=True)
    set_locations(
        storage,
        make_location(nas, kind=StorageKind.NETWORK_SHARE, managed=True, available=False),
    )
    touched = [_record_calls(monkeypatch, "realpath"), _record_calls(monkeypatch, "isdir")]
    flow = await start_flow(manages_all_sources)

    step = await flow.submit(nas / "Music")

    assert _error_key(step) == "storage_location_unavailable"
    assert step.error_translations["path"].args == [str(nas)]
    assert not [path for calls in touched for path in calls if path.startswith(str(nas))]
    assert flow.finished_with is None


@MEMBER_AND_ADMIN
async def test_reconfigure_keeps_the_stored_folder(
    start_flow: Callable[..., Awaitable[_Flow]], tree: Path, manages_all_sources: bool
) -> None:
    """
    A source on a folder outside every location keeps it and can still change what it holds.

    :param manages_all_sources: Whether the caller manages every music source.
    """
    legacy = str(tree / "legacy")
    flow = await start_flow(
        manages_all_sources, setup_data={CONF_CONTENT_TYPE: "music", "path": legacy}
    )

    form = await flow.form()
    assert next(e for e in form.entries if e.key == "path").value == legacy
    step = await flow.submit(legacy, content_type="podcasts")

    assert step.type == FlowStepType.FINISH
    assert flow.finished_with == {CONF_CONTENT_TYPE: "podcasts", "path": legacy}


@pytest.mark.parametrize(
    ("folder", "error_key"),
    [("outside/Music", "folder_not_allowed"), ("media/Music", None)],
    ids=["outside", "in_a_location"],
)
async def test_reconfigure_checks_a_changed_folder(
    start_flow: Callable[..., Awaitable[_Flow]],
    tree: Path,
    folder: str,
    error_key: str | None,
) -> None:
    """
    A changed folder is checked like a new one, also when the stored one lies outside.

    :param folder: The new folder, relative to the tree.
    :param error_key: The expected error, None when the folder may be used.
    """
    flow = await start_flow(
        manages_all_sources=False,
        setup_data={CONF_CONTENT_TYPE: "music", "path": str(tree / "legacy")},
    )

    step = await flow.submit(tree / folder)

    if error_key is None:
        assert step.type == FlowStepType.FINISH
        assert flow.finished_with == {CONF_CONTENT_TYPE: "music", "path": str(tree / folder)}
    else:
        assert _error_key(step) == error_key
        assert flow.finished_with is None


@pytest.mark.parametrize(
    ("locations", "manages_all_sources", "in_container", "default"),
    [
        (["/media"], True, False, "/media"),
        (["/media"], False, True, "/media"),
        (["/media"], False, False, None),
        (["/media/music"], True, False, None),
        ([], True, False, None),
    ],
    ids=[
        "admin",
        "member_in_a_container",
        "member_on_a_host",
        "only_a_folder_below",
        "no_location",
    ],
)
async def test_the_folder_defaults_to_media_only_where_it_may_be_used(
    start_flow: Callable[..., Awaitable[_Flow]],
    storage: StorageController,
    locations: list[str],
    manages_all_sources: bool,
    in_container: bool,
    default: str | None,
) -> None:
    """
    The folder is a folder picker that offers /media when the caller may use it.

    :param locations: The paths of the media locations.
    :param manages_all_sources: Whether the caller manages every music source.
    :param in_container: Whether the server runs in a container, which makes every media
        location available to members.
    :param default: The expected default folder.
    """
    storage._in_container = in_container
    set_locations(
        storage, *(make_location(path, kind=StorageKind.BUILTIN_MEDIA) for path in locations)
    )

    form = await (await start_flow(manages_all_sources)).form()

    path_entry = next(entry for entry in form.entries if entry.key == "path")
    assert path_entry.type == ConfigEntryType.FOLDER
    assert path_entry.default_value == default


async def test_the_content_type_is_a_question_with_a_button_per_type(
    start_flow: Callable[..., Awaitable[_Flow]],
    localize: Callable[[DataClassDictMixin], dict[str, Any]],
) -> None:
    """The setup asks what to add, one button per type, starting on music without a default."""
    form = await (await start_flow()).form()

    shown = localize(next(entry for entry in form.entries if entry.key == CONF_CONTENT_TYPE))

    assert shown["label"] == "What do you want to add?"
    assert shown["expanded_options"] is True
    assert shown["default_value"] is None
    assert shown["value"] == "music"
    assert [option["title"] for option in shown["options"]] == [
        "Music",
        "Audiobooks",
        "Podcasts",
        "Sound Effects",
    ]


async def test_a_content_type_left_out_is_a_missing_value(
    start_flow: Callable[..., Awaitable[_Flow]], tree: Path
) -> None:
    """A submitted content type of null is refused rather than stored without a type."""
    flow = await start_flow()

    step = flow.session.handle_submit({CONF_CONTENT_TYPE: None, "path": str(tree / "media")})

    assert step is not None
    assert step.errors == {CONF_CONTENT_TYPE: "required"}
    assert flow.finished_with is None
