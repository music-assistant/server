"""Data model for the steps document of a Rotation playlist."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field, fields
from enum import StrEnum
from typing import Any, Literal, Self

from mashumaro import DataClassDictMixin, field_options
from mashumaro.codecs import BasicDecoder, BasicEncoder
from mashumaro.dialect import Dialect
from mashumaro.exceptions import MissingField
from mashumaro.jsonschema import build_json_schema
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.translations import TranslatableText, resolve_translation

from .constants import MAX_CONDITIONS, MAX_SORT_KEYS, MAX_STEPS, TRANSLATION_OWNER


class ItemKind(StrEnum):
    """The kind of items a step works on (equals the media type name)."""

    ARTIST = "artist"
    ALBUM = "album"
    TRACK = "track"


@dataclass
class Condition:
    """One condition of a filter step."""

    field: str
    op: str
    value: Any = None


@dataclass
class SourceStep:
    """Pick the media type the Rotation starts from."""

    media_type: ItemKind
    origin: Literal["library"] = field(default="library", metadata={"alias": "from"})
    type: Literal["source"] = "source"


@dataclass
class FilterStep:
    """Keep or remove items by conditions."""

    mode: Literal["keep", "remove"]
    match: Literal["all", "any"]
    conditions: list[Condition] = field(default_factory=list)
    type: Literal["filter"] = "filter"


@dataclass
class SortKey:
    """One sort key of a sort step."""

    field: str
    descending: bool = False


@dataclass
class SortStep:
    """Order the items by one or more keys."""

    keys: list[SortKey] = field(default_factory=list)
    type: Literal["sort"] = "sort"


@dataclass
class LimitStep:
    """Cut the list to a number of items or a total duration in minutes."""

    count: int | None = None
    minutes: int | None = None
    type: Literal["limit"] = "limit"


@dataclass
class ExpandStep:
    """Replace each item by its children, kept grouped by their parent."""

    to: ItemKind
    # "providers" also lists the albums of an artist on its providers; artist tracks always
    # come from the library
    scope: Literal["library", "providers"] = "library"
    type: Literal["expand"] = "expand"


@dataclass
class FlattenStep:
    """Drop all grouping so later steps work on one list again."""

    type: Literal["flatten"] = "flatten"


Step = SourceStep | FilterStep | SortStep | LimitStep | ExpandStep | FlattenStep


@dataclass
class StepError(DataClassDictMixin):
    """
    A problem found in a steps document.

    Return it from an API command at the top level, in a list or in a dataclass field, never
    inside a plain dict: orjson writes a dataclass in a dict itself and skips the hook that
    translates the message.
    """

    step: int
    field: str | None
    # in English; an API result carries it in the language of the client
    message: str
    # the key of the message in the errors strings of this provider, never sent itself
    # (the field attribute above hides the field function in this class body)
    translation_key: str | None = dataclasses.field(
        default=None, metadata=field_options(serialize="omit"), repr=False, compare=False
    )
    translation_args: list[Any] = dataclasses.field(
        default_factory=list,
        metadata=field_options(serialize="omit"),
        repr=False,
        compare=False,
    )

    def __post_serialize__(self, d: dict[str, Any]) -> dict[str, Any]:
        """Localize the message when a resolver for the language of the client is active."""
        d["message"] = self.localized_message()
        return d

    def localized_message(self) -> str:
        """Return the message in the language of the current API client, or in English."""
        return _localized(self.translation_key, self.translation_args) or self.message

    def __str__(self) -> str:
        """Return the error as a readable sentence, in the language of the current API client."""
        return _localized("step_error", [self.step, self.localized_message()]) or (
            f"step {self.step}: {self.message}"
        )


class StepsError(InvalidDataError):
    """Raised when a steps document is not valid."""

    translation_key = "invalid_steps"
    translation_owner = TRANSLATION_OWNER

    def __init__(self, errors: list[StepError]) -> None:
        """
        Initialize the error.

        :param errors: The problems found, one entry per problem.
        """
        self.errors = errors
        joined = _StepErrors(errors)
        super().__init__(f"Invalid steps: {joined:s}", translation_args=[joined])


@dataclass
class RotationDetails(DataClassDictMixin):
    """A Rotation with its steps and the round status of the current user, as the API sends it."""

    id: str
    name: str
    steps: list[dict[str, Any]]
    playlist: dict[str, Any]
    # when the steps cannot run, it holds the error in English; an API result carries the
    # error in the language of the client
    round_status: dict[str, Any]
    # the translation of the error of the round status in the errors strings of this provider,
    # never sent itself
    round_error_key: str | None = dataclasses.field(
        default=None, metadata=field_options(serialize="omit"), repr=False, compare=False
    )
    round_error_args: list[Any] = dataclasses.field(
        default_factory=list,
        metadata=field_options(serialize="omit"),
        repr=False,
        compare=False,
    )

    def __post_serialize__(self, d: dict[str, Any]) -> dict[str, Any]:
        """Localize the error of the round status when a resolver for the client is active."""
        if (text := _localized(self.round_error_key, self.round_error_args)) is not None:
            d["round_status"] = {**d["round_status"], "error": text}
        return d


def step_text(key: str, fallback: str, *args: Any) -> TranslatableText:
    """
    Return a part of a step error that reads in the language of the API client.

    :param key: The key of the text in the errors strings of this provider.
    :param fallback: The text in English.
    :param args: The values for the {0}/{1} placeholders of the text.
    """
    return TranslatableText(
        f"errors.{key}",
        fallback=fallback,
        owner=TRANSLATION_OWNER,
        params=[str(arg) for arg in args] or None,
    )


def parse_steps(data: Any) -> list[Step]:
    """
    Parse and shape-check a steps document.

    :param data: The decoded JSON list of steps.
    :raises StepsError: When the document does not have the right shape.
    """
    if not isinstance(data, list):
        raise StepsError([StepError(0, None, "the steps must be a list", "steps_not_list")])
    errors: list[StepError] = []
    if not data:
        errors.append(StepError(1, None, "add at least a source step", "no_steps"))
    if len(data) > MAX_STEPS:
        errors.append(
            StepError(
                MAX_STEPS + 1,
                None,
                f"at most {MAX_STEPS} steps are allowed",
                "too_many_steps",
                [MAX_STEPS],
            )
        )
        data = data[:MAX_STEPS]
    steps: list[Step] = []
    kind: ItemKind | None = None
    for number, raw in enumerate(data, start=1):
        step = _parse_step(number, raw, errors)
        if step is None:
            continue
        steps.append(step)
        if number == 1 and not isinstance(step, SourceStep):
            errors.append(
                StepError(
                    number,
                    "type",
                    "the first step must be a source; add a source step",
                    "first_step_not_source",
                )
            )
        elif number > 1 and isinstance(step, SourceStep):
            errors.append(
                StepError(
                    number,
                    "type",
                    "only the first step can be a source; remove this one",
                    "second_source",
                )
            )
        kind = _check_kind(number, step, kind, errors)
    if errors:
        raise StepsError(errors)
    return steps


def steps_to_dict(steps: list[Step]) -> list[dict[str, Any]]:
    """
    Convert steps to plain JSON-ready dicts.

    :param steps: The steps to convert.
    """
    return [_STEP_ENCODER.encode(step) for step in steps]


def steps_json_schema() -> dict[str, Any]:
    """Return the JSON Schema of the steps list; every step must name its type."""
    schema = build_json_schema(list[Step]).to_dict()
    items = schema["items"]
    # the step classes default their type, but a steps document must always name it
    for alternative in items.get("anyOf") or items.get("oneOf") or ():
        alternative["properties"]["type"].pop("default", None)
        required = alternative.setdefault("required", [])
        if "type" not in required:
            required.append("type")
    return schema


def item_kinds(steps: list[Step]) -> list[tuple[ItemKind, int]]:
    """
    Return the item kind and group depth of the list each step ends with.

    Steps before a valid source are reported as albums at depth 0.

    :param steps: The steps to walk.
    """
    result: list[tuple[ItemKind, int]] = []
    kind = ItemKind.ALBUM
    depth = 0
    for step in steps:
        if isinstance(step, SourceStep):
            kind, depth = step.media_type, 0
        elif isinstance(step, ExpandStep):
            kind, depth = step.to, depth + 1
        elif isinstance(step, FlattenStep):
            depth = 0
        result.append((kind, depth))
    return result


def _parse_step(number: int, raw: Any, errors: list[StepError]) -> Step | None:
    """Decode one step, appending shape problems to errors."""
    if not isinstance(raw, dict):
        errors.append(
            StepError(number, None, "a step must be an object with a type", "step_not_object")
        )
        return None
    step_type = raw.get("type")
    if step_type not in _DECODERS:
        errors.append(
            StepError(
                number,
                "type",
                f"unknown step type {step_type!r}; use one of: {', '.join(_DECODERS)}",
                "unknown_step_type",
                [repr(step_type), ", ".join(_DECODERS)],
            )
        )
        return None
    known = {item.metadata.get("alias", item.name) for item in fields(_STEP_CLASSES[step_type])}
    unknown = sorted(str(key) for key in raw if key not in known)
    if unknown:
        errors.append(
            StepError(
                number,
                unknown[0],
                f"unknown field(s): {', '.join(unknown)}; remove them",
                "unknown_fields",
                [", ".join(unknown)],
            )
        )
        return None
    if step_type == "source" and raw.get("from", "library") != "library":
        errors.append(
            StepError(
                number,
                "from",
                "only the library is supported as a source for now",
                "source_not_library",
            )
        )
        return None
    try:
        step: Step = _DECODERS[step_type].decode(raw)
    except MissingField as err:
        errors.append(
            StepError(
                number,
                err.field_name,
                f"missing field {err.field_name!r}; add it",
                "missing_field",
                [repr(err.field_name)],
            )
        )
        return None
    except (ValueError, TypeError, KeyError) as err:
        cause = err.__cause__ or err
        errors.append(
            StepError(
                number,
                getattr(err, "field_name", None),
                f"invalid value ({cause}); check the field type",
                "invalid_value",
                [cause],
            )
        )
        return None
    _check_limits(number, step, errors)
    return step


def _check_limits(number: int, step: Step, errors: list[StepError]) -> None:
    """Check counts and sizes of one decoded step."""
    if isinstance(step, FilterStep) and len(step.conditions) > MAX_CONDITIONS:
        errors.append(
            StepError(
                number,
                "conditions",
                f"at most {MAX_CONDITIONS} conditions are allowed per filter",
                "too_many_conditions",
                [MAX_CONDITIONS],
            )
        )
    elif isinstance(step, SortStep):
        if not step.keys:
            errors.append(
                StepError(number, "keys", "a sort step needs at least one key", "sort_without_keys")
            )
        elif len(step.keys) > MAX_SORT_KEYS:
            errors.append(
                StepError(
                    number,
                    "keys",
                    f"at most {MAX_SORT_KEYS} sort keys are allowed",
                    "too_many_sort_keys",
                    [MAX_SORT_KEYS],
                )
            )
    elif isinstance(step, LimitStep):
        if (step.count is None) == (step.minutes is None):
            errors.append(
                StepError(
                    number,
                    None,
                    "a limit step needs exactly one of count or minutes",
                    "limit_count_or_minutes",
                )
            )
        elif (step.count or step.minutes or 0) < 1:
            errors.append(
                StepError(number, None, "the limit must be at least 1", "limit_too_small")
            )


def _check_kind(
    number: int, step: Step, kind: ItemKind | None, errors: list[StepError]
) -> ItemKind | None:
    """Return the item kind after the step and report expand steps that cannot work."""
    if isinstance(step, SourceStep):
        return step.media_type
    if not isinstance(step, ExpandStep):
        return kind
    if kind is None:
        errors.append(
            StepError(
                number,
                "to",
                "an expand step needs albums or artists before it; add a source of albums",
                "expand_without_source",
            )
        )
        return None
    if step.to in _EXPAND_TARGETS[kind]:
        return step.to
    if kind == ItemKind.ALBUM:
        message, key = "albums can only become tracks; use track", "expand_albums"
    elif kind == ItemKind.ARTIST:
        message = "artists can only become albums or tracks; use album or track"
        key = "expand_artists"
    else:
        message, key = "tracks cannot be expanded; remove this step", "expand_tracks"
    errors.append(StepError(number, "to", message, key))
    return kind


def _localized(key: str | None, args: list[Any]) -> str | None:
    """Return a message of this provider in the language of the current API client, or None."""
    if key is None:
        return None
    return resolve_translation(
        f"errors.{key}", owner=TRANSLATION_OWNER, params=[str(arg) for arg in args] or None
    )


class _StepErrors(str):
    """
    The step errors of a StepsError as one text, in the language of the API client.

    Its value is the English text, which JSON gets when no API client language is bound.
    """

    __slots__ = ("errors",)
    errors: list[StepError]

    def __new__(cls, errors: list[StepError]) -> Self:
        """
        Create the text.

        :param errors: The step errors to join.
        """
        text = super().__new__(cls, "; ".join(f"step {err.step}: {err.message}" for err in errors))
        text.errors = errors
        return text

    def __str__(self) -> str:
        """Return the step errors joined, in the language of the current API client."""
        return "; ".join(str(err) for err in self.errors)


class _AliasDialect(Dialect):
    """Dialect that writes fields under their alias."""

    serialize_by_alias = True


_STEP_CLASSES: dict[str, type[Step]] = {
    "source": SourceStep,
    "filter": FilterStep,
    "sort": SortStep,
    "limit": LimitStep,
    "expand": ExpandStep,
    "flatten": FlattenStep,
}
_DECODERS: dict[str, BasicDecoder[Any]] = {
    name: BasicDecoder(cls) for name, cls in _STEP_CLASSES.items()
}
_STEP_ENCODER: BasicEncoder[Any] = BasicEncoder(Step, default_dialect=_AliasDialect)
_EXPAND_TARGETS: dict[ItemKind, tuple[ItemKind, ...]] = {
    ItemKind.ARTIST: (ItemKind.ALBUM, ItemKind.TRACK),
    ItemKind.ALBUM: (ItemKind.TRACK,),
    ItemKind.TRACK: (),
}
