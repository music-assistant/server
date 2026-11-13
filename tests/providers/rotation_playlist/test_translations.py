"""Tests that the Rotation provider sends its labels and errors in the language of the client."""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from music_assistant_models.api import ErrorResultMessage, SuccessResultMessage
from music_assistant_models.errors import InvalidDataError, MediaNotFoundError
from music_assistant_models.translations import TRANSLATION_RESOLVER

from music_assistant.controllers.translations import SOURCE_LANGUAGE, TranslationController
from music_assistant.providers import rotation_playlist
from music_assistant.providers.rotation_playlist.constants import TRANSLATION_OWNER
from music_assistant.providers.rotation_playlist.fields import FIELDS, describe
from music_assistant.providers.rotation_playlist.models import (
    ItemKind,
    SortKey,
    SortStep,
    StepError,
    StepsError,
    parse_steps,
)
from music_assistant.providers.rotation_playlist.validation import validate_steps
from scripts.build_translations import build_translations_source

from .test_provider import _loaded, _steps

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

PREFIX = f"{TRANSLATION_OWNER}."
GERMAN = {
    f"{PREFIX}fields.year": "Jahr",
    f"{PREFIX}fields.play_count": "Wiedergaben insgesamt",
    f"{PREFIX}recipes.random_tracks.name": "Zufällige Lieder",
    f"{PREFIX}errors.invalid_steps": "Ungültige Schritte: {0}",
    f"{PREFIX}errors.step_error": "Schritt {0}: {1}",
    f"{PREFIX}errors.expand_albums": "Alben werden nur zu Liedern; nimm track",
    f"{PREFIX}errors.condition_value": "'{0}' {1}: {2}",
    f"{PREFIX}errors.value_not_number": "der Wert muss eine Zahl sein",
    f"{PREFIX}errors.name_missing": "Eine Rotation braucht einen Namen",
    f"{PREFIX}errors.steps_without_source": "Die Schritte beginnen nicht mit einer Quelle",
}

STRINGS = Path(rotation_playlist.__file__).parent / "strings.json"
# the error strings that the step errors of a steps document never use
RAISED_ONLY = {
    "invalid_steps",
    "step_error",
    "steps_without_source",
    "sort_too_many_items",
    "rotation_not_found",
    "preview_limit_not_number",
    "preview_limit_out_of_range",
    "unknown_media_type",
    "name_missing",
}
EXPAND_ALBUMS = [{"type": "source", "media_type": "album"}, {"type": "expand", "to": "artist"}]

# steps documents that each have at least one problem, covering every kind of message
BAD_STEPS: list[Any] = [
    "not a list",
    [],
    [{"type": "source", "media_type": "album"}] * 31,
    [{"type": "filter", "mode": "keep", "match": "all"}],
    [{"type": "source", "media_type": "album"}, {"type": "source", "media_type": "track"}],
    [42],
    [{"type": "shuffle"}],
    [{"type": "source", "media_type": "album", "colour": "red"}],
    [{"type": "source", "media_type": "album", "from": "spotify"}],
    [{"type": "source"}],
    [{"type": "source", "media_type": "song"}],
    [
        {"type": "source", "media_type": "album"},
        {
            "type": "filter",
            "mode": "keep",
            "match": "all",
            "conditions": [{"field": "year", "op": "is", "value": 1}] * 21,
        },
    ],
    [{"type": "source", "media_type": "album"}, {"type": "sort", "keys": []}],
    [{"type": "source", "media_type": "album"}, {"type": "sort", "keys": [{"field": "y"}] * 6}],
    [{"type": "source", "media_type": "album"}, {"type": "limit"}],
    [{"type": "source", "media_type": "album"}, {"type": "limit", "count": 0}],
    [{"type": "expand", "to": "track"}],
    [{"type": "source", "media_type": "album"}, {"type": "expand", "to": "artist"}],
    [{"type": "source", "media_type": "artist"}, {"type": "expand", "to": "artist"}],
    [{"type": "source", "media_type": "track"}, {"type": "expand", "to": "track"}],
]


def _filter(*conditions: dict[str, Any], kind: str = "album") -> list[dict[str, Any]]:
    return [
        {"type": "source", "media_type": kind},
        {"type": "filter", "mode": "keep", "match": "all", "conditions": list(conditions)},
    ]


def _sort(field: str, kind: str = "album") -> list[dict[str, Any]]:
    return [{"type": "source", "media_type": kind}, {"type": "sort", "keys": [{"field": field}]}]


# steps documents with the right shape whose fields, operators or values do not fit
INVALID_FIELDS: list[list[dict[str, Any]]] = [
    _filter({"field": "random", "op": "is", "value": 1}),
    _sort("genre"),
    _sort("title"),
    _sort("nothing", kind="artist"),
    _sort("nothing", kind="track"),
    _filter({"field": "title", "op": "is", "value": "x"}),
    _filter({"field": "nothing", "op": "is", "value": 1}),
    _filter({"field": "year", "op": "is", "value": 1}, kind="artist"),
    _filter({"field": "title", "op": "is", "value": "x"}, kind="artist"),
    _filter({"field": "year", "op": "contains", "value": 1}),
    *(
        _filter({"field": name, "op": "is", "value": None}, kind=kind)
        for kind, name in (
            ("album", "artist"),
            ("track", "album"),
            ("album", "genre"),
            ("album", "provider"),
            ("album", "album_type"),
            ("album", "audio_quality"),
        )
    ),
    _filter({"field": "year", "op": "is", "value": None}),
    _filter({"field": "date_added", "op": "in_last_days", "value": 0}),
    _filter({"field": "year", "op": "between", "value": [1]}),
    _filter({"field": "year", "op": "between", "value": [1, "x"]}),
    _filter({"field": "genre", "op": "one_of", "value": []}),
    _filter({"field": "audio_quality", "op": "is", "value": "great"}),
    _filter({"field": "year", "op": "is", "value": "x"}),
    _filter({"field": "favorite", "op": "is", "value": "x"}),
    _filter({"field": "date_added", "op": "is", "value": "x"}),
    _filter({"field": "name", "op": "is", "value": ""}),
    [
        {"type": "source", "media_type": "artist"},
        {"type": "expand", "to": "track", "scope": "providers"},
    ],
]


def _english_errors() -> list[StepError]:
    """Return the errors of every bad steps document."""
    errors: list[StepError] = []
    for steps in BAD_STEPS:
        with pytest.raises(StepsError) as exc:
            parse_steps(steps)
        errors.extend(exc.value.errors)
    for steps in INVALID_FIELDS:
        found = validate_steps(parse_steps(steps))
        assert found, steps
        errors.extend(found)
    return errors


def _controller(locales: dict[str, dict[str, str]] | None = None) -> TranslationController:
    """Return a translations controller with the real English strings and test locales."""
    ctrl = TranslationController.__new__(TranslationController)
    ctrl.logger = logging.getLogger("test.rotation_playlist.translations")
    ctrl._source = build_translations_source()
    ctrl._locales = locales or {}
    ctrl._locale_files = {}
    ctrl._locale_locks = {}
    ctrl._available_locales = {SOURCE_LANGUAGE, *(locales or {})}
    return ctrl


@contextmanager
def _resolver(resolve: Callable[..., str | None]) -> Iterator[None]:
    """Bind a translation resolver like the WebSocket API does while it sends a message."""
    token = TRANSLATION_RESOLVER.set(resolve)
    try:
        yield
    finally:
        TRANSLATION_RESOLVER.reset(token)


def _sent(result: Any, locale: str | None, ctrl: TranslationController | None = None) -> Any:
    """Return a command result as a client with the locale receives it."""
    ctrl = ctrl or _controller({"de": GERMAN})
    with _resolver(partial(ctrl.get_translation, locale=locale)):
        return json.loads(SuccessResultMessage("1", result).to_json())["result"]


def _sent_error(err: Exception, locale: str | None) -> str:
    """Return the error message a client with the locale receives for a raised error."""
    assert isinstance(err, InvalidDataError | MediaNotFoundError)
    # built like the WebSocket API does: the English message first, then sent with a resolver
    message = _error_message(err)
    ctrl = _controller({"de": GERMAN})
    with _resolver(partial(ctrl.get_translation, locale=locale)):
        sent = json.loads(message.to_json())
    assert "translation_key" not in sent
    details: str = sent["details"]
    return details


def _error_message(err: InvalidDataError | MediaNotFoundError) -> ErrorResultMessage:
    """Return the error message the WebSocket API builds for a raised error."""
    return ErrorResultMessage(
        "1",
        err.error_code,
        str(err),
        translation_key=err.translation_key,
        translation_args=err.translation_args,
        translation_owner=err.translation_owner,
    )


def test_english_strings_match_the_messages() -> None:
    """Every step error reads the same from the English strings as from the code."""
    errors = _english_errors()
    used: set[str] = set()
    for err in errors:
        assert err.translation_key, err.message
        used.add(err.translation_key)
        used.update(
            arg.key.removeprefix("errors.") for arg in err.translation_args if hasattr(arg, "key")
        )
        assert _sent([err], "en")[0]["message"] == err.message
    strings = json.loads(STRINGS.read_text(encoding="utf-8"))["errors"]
    assert used <= set(strings)
    assert set(strings) - used == RAISED_ONLY
    source = build_translations_source()
    sent = _sent(describe(None, []), "en")
    for kind, described in sent["media_types"].items():
        for field in described["fields"]:
            assert f"{PREFIX}fields.{field['name']}" in source, field["name"]
            assert field["label"] == FIELDS[ItemKind(kind)][field["name"]].label


def test_steps_error_reads_the_same_in_english() -> None:
    """A raised steps error reaches an English client with its own text, not a generic one."""
    for steps in BAD_STEPS:
        with pytest.raises(StepsError) as exc:
            parse_steps(steps)
        assert _sent_error(exc.value, "en") == str(exc.value)
        assert _sent_error(exc.value, None) == str(exc.value)


def test_raised_errors_serialize_without_a_resolver() -> None:
    """An error message also serializes when no client language is bound, in English."""
    with pytest.raises(StepsError) as exc:
        parse_steps(EXPAND_ALBUMS)
    sent = json.loads(_error_message(exc.value).to_json())
    assert sent["details"] == str(exc.value)
    assert sent["translation_args"] == [str(exc.value).removeprefix("Invalid steps: ")]


def test_step_errors_compare_by_their_content() -> None:
    """Two step errors with the same text are equal, whatever their translation details."""
    assert StepError(1, None, "x", "no_steps") == StepError(1, None, "x")


def test_field_labels_follow_the_client_language() -> None:
    """Field labels arrive in the language of the client and in English when it lacks them."""
    fields = {
        field["name"]: field
        for field in _sent(describe(ItemKind.ALBUM, []), "de_DE")["media_types"]["album"]["fields"]
    }
    assert fields["year"]["label"] == "Jahr"
    assert fields["play_count"]["label"] == "Wiedergaben insgesamt"
    assert fields["genre"]["label"] == "Genre"
    assert set(fields["year"]) == {"name", "type", "label", "operators", "sortable"}
    assert fields["audio_quality"]["values"] == ["lossy", "lossless", "hi_res"]


def test_step_errors_follow_the_client_language() -> None:
    """Step errors arrive in the language of the client, also the parts they are made of."""
    with pytest.raises(StepsError) as exc:
        parse_steps(EXPAND_ALBUMS)
    errors = exc.value.errors
    errors += validate_steps(parse_steps(_filter({"field": "year", "op": "is", "value": "x"})))
    sent = _sent(errors, "de")
    assert sent[0] == {
        "step": 2,
        "field": "to",
        "message": "Alben werden nur zu Liedern; nimm track",
    }
    assert sent[-1]["message"] == "'year' is: der Wert muss eine Zahl sein"
    # nothing the client lacks a translation for is lost
    assert _sent(errors, "fr")[0]["message"] == errors[0].message


def test_raised_errors_follow_the_client_language() -> None:
    """Errors raised by the commands reach the client in its language."""
    with pytest.raises(StepsError) as exc:
        parse_steps(EXPAND_ALBUMS)
    assert _sent_error(exc.value, "de") == (
        "Ungültige Schritte: Schritt 2: Alben werden nur zu Liedern; nimm track"
    )
    assert str(exc.value) == ("Invalid steps: step 2: albums can only become tracks; use track")


async def test_round_status_error_follows_the_client_language(tmp_path: Path) -> None:
    """The round status of steps that no longer run reaches the client in its language."""
    provider = await _loaded(tmp_path)
    rotation_id = (await provider.create("Die drei ???", _steps())).item_id
    provider._steps[rotation_id] = [SortStep([SortKey("random")])]
    english = "Invalid steps: step 1: the steps must start with a source step"
    rotation = await provider.get(rotation_id)
    assert rotation.to_dict()["round_status"] == {"round": 1, "error": english}
    sent = _sent(rotation, "de")
    assert sent["round_status"] == {
        "round": 1,
        "error": "Ungültige Schritte: Schritt 1: Die Schritte beginnen nicht mit einer Quelle",
    }
    assert set(sent) == {"id", "name", "steps", "playlist", "round_status"}
    assert _sent(rotation, "fr")["round_status"]["error"] == english
    assert _sent(await provider.get(rotation_id), None)["round_status"]["error"] == english


async def test_commands_send_translated_data(tmp_path: Path) -> None:
    """The commands return data that the API translates for the client and errors with keys."""
    provider = await _loaded(tmp_path)
    errors = _sent(await provider.validate(EXPAND_ALBUMS), "de")
    assert errors[0]["message"] == "Alben werden nur zu Liedern; nimm track"
    described = _sent(await provider.describe_steps("album"), "de")
    assert "schema" in described
    recipes = _sent(await provider.recipes(), "de")
    assert recipes[0]["name"] == "Zufällige Lieder"
    assert recipes[0]["goal"] == "Play all tracks of your library in random order."
    assert set(recipes[0]) == {"id", "name", "goal", "steps"}
    with pytest.raises(InvalidDataError) as name_error:
        await provider.create(" ", _steps())
    assert _sent_error(name_error.value, "de") == "Eine Rotation braucht einen Namen"
    for limit in ("ten", 500):
        with pytest.raises(InvalidDataError) as limit_error:
            await provider.preview(_steps(), limit)  # type: ignore[arg-type]
        assert _sent_error(limit_error.value, "en") == str(limit_error.value)
    with pytest.raises(InvalidDataError) as kind_error:
        await provider.describe_steps("song")
    assert _sent_error(kind_error.value, "en") == str(kind_error.value)
    with pytest.raises(MediaNotFoundError) as missing:
        await provider.get_playlist("nothing")
    assert _sent_error(missing.value, "en") == str(missing.value)


def test_results_keep_english_without_a_resolver() -> None:
    """Without a client to answer, as for the MCP tools, the data stays in English."""
    with pytest.raises(StepsError) as exc:
        parse_steps(EXPAND_ALBUMS)
    assert exc.value.errors[0].to_dict()["message"] == exc.value.errors[0].message
    assert set(exc.value.errors[0].to_dict()) == {"step", "field", "message"}
    asked: list[tuple[str, str | None, list[str] | None]] = []

    def unknown(key: str, owner: str | None = None, params: list[str] | None = None) -> None:
        asked.append((key, owner, params))

    with _resolver(unknown):
        sent = json.loads(SuccessResultMessage("1", describe(ItemKind.TRACK, [])).to_json())
    labels = {
        field["name"]: field["label"] for field in sent["result"]["media_types"]["track"]["fields"]
    }
    assert labels["title"] == "Title"
    assert ("fields.title", TRANSLATION_OWNER, None) in asked
