# ruff: noqa: D107, RUF001
"""
Effective skill manifest with file-based override + UI status reporting.

Single class :class:`SkillManifestProvider` is the only entry point for
the rest of the provider to read / write / validate the skill TOML
manifest. It encapsulates:

* Loading the **effective** manifest (override file if present and
  valid, else the package-bundled default at
  ``provider/data/skill.toml``).
* Status reporting to UI: ``bundled`` / ``override_valid`` /
  ``override_invalid`` (with parser error message for the latter)
  via :class:`ManifestStatus.source`.
* File operations exposed as user-facing actions: export current
  effective manifest to override path; import paste from UI; reset
  (delete override); validate override locally.
* Runtime dispatch of an NLU intent block (``request.nlu.intents``)
  into a :class:`ParsedControl` / :class:`ParsedCommand` via the
  manifest's ``runtime:`` blocks (provider-side ``parse_intent``).

Override file path: ``<storage_root>/yandex_alice/skill.toml`` where
``storage_root`` is ``mass.storage_path`` if MA exposes it, falling
back to ``$HOME/.musicassistant`` (matching the path convention the
provider already documents in dialogs.py for log files).
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import dataclasses
import importlib.resources
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from ya_dialogs_api import (
    EntityDraft,
    IntentDraft,
    SkillManifest,
    SkillManifestError,
    apply_runtime_mapping,
    iter_intent_matches,
    parse_manifest_text,
)

from .dialogs_control import ParsedControl
from .dialogs_nlu import ParsedCommand

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

__all__ = [
    "ManifestStatus",
    "SkillManifestProvider",
]


_LOGGER = logging.getLogger(__name__)

_BASE64_PASTE_PREFIX = "data:base64,"


@dataclasses.dataclass(frozen=True, slots=True)
class ManifestStatus:
    """
    User-facing snapshot of the effective manifest.

    :param source: ``"bundled"`` when no override file exists,
        ``"override_valid"`` when the override file parses cleanly,
        ``"override_invalid"`` when the override file is present but
        unusable (the bundled default is loaded as fallback).
    :param override_path: path where the override file lives or
        would live (always reportable to UI even when absent).
    :param intent_count: number of intents in the effective manifest.
    :param entity_count: number of entities in the effective manifest.
    :param error: parser error message when ``source ==
        "override_invalid"``, ``None`` otherwise.
    """

    source: Literal["bundled", "override_valid", "override_invalid"]
    override_path: Path
    intent_count: int
    entity_count: int
    error: str | None = None


_ResolvedSource = Literal["bundled", "override_valid", "override_invalid"]


@dataclasses.dataclass(frozen=True, slots=True)
class _Resolved:
    """Cached snapshot of one ``stat()`` worth of effective manifest state."""

    manifest: SkillManifest
    source: _ResolvedSource
    error: str | None


class SkillManifestProvider:
    """
    Effective skill manifest gateway for the rest of the provider.

    Cheap to construct (no I/O until the first call). Subsequent calls
    that hit the override path use a stat-keyed cache: the override is
    re-parsed only when the file's ``(existence, mtime_ns)`` pair
    changes. External edits take effect on the next call without a
    restart, but unchanged files do not trigger redundant disk reads
    or TOML parses on every webhook hit / UI render.
    """

    def __init__(self, mass: MusicAssistant) -> None:
        self._mass = mass
        self._last_import_success = False
        self._bundled_cache: SkillManifest | None = None
        self._resolved_cache_key: tuple[bool, int | None] | None = None
        self._resolved_cache: _Resolved | None = None

    # -----------------------------------------------------------------------
    # Path & status
    # -----------------------------------------------------------------------

    @property
    def override_path(self) -> Path:
        """Where the user override TOML lives (or would live)."""
        return self._storage_root() / "yandex_alice" / "skill.toml"

    @property
    def last_import_success(self) -> bool:
        """``True`` if the most recent ``import_from_paste`` call succeeded."""
        return self._last_import_success

    def status(self) -> ManifestStatus:
        """Diagnostic snapshot for UI display."""
        resolved = self._resolve()
        path = self.override_path
        return ManifestStatus(
            source=resolved.source,
            override_path=path,
            intent_count=len(resolved.manifest.intents),
            entity_count=len(resolved.manifest.to_entity_drafts()),
            error=resolved.error,
        )

    # -----------------------------------------------------------------------
    # Effective manifest loading
    # -----------------------------------------------------------------------

    def manifest(self) -> SkillManifest:
        """Return the effective manifest — override if valid, else bundled."""
        return self._resolve().manifest

    def grammar(self) -> list[IntentDraft]:
        """Effective intents as ``IntentDraft`` for ``set_intents``."""
        return self.manifest().to_intent_drafts()

    def entities(self) -> list[EntityDraft]:
        """Effective entities as ``EntityDraft`` for ``set_entities``."""
        return self.manifest().to_entity_drafts()

    # -----------------------------------------------------------------------
    # Runtime dispatch — NLU intent block → ParsedControl/ParsedCommand
    # -----------------------------------------------------------------------

    def parse_intent(
        self,
        nlu_intents: dict[str, Any] | None,
    ) -> ParsedControl | ParsedCommand | None:
        """
        Map an NLU intent block to the dispatcher's dataclass.

        Walks ``request.nlu.intents`` matches against the effective
        manifest's ``runtime`` blocks; the first matched intent with a
        valid runtime mapping yields a :class:`ParsedControl` (kind
        ``"control"``) or :class:`ParsedCommand` (kind ``"play"``).
        Returns ``None`` when no intent has a runtime mapping that
        applies (slot missing without default, value rejected by
        cap / reject_if_below, or matched intent has no runtime block).
        """
        manifest = self.manifest()
        by_form = {i.form_name: i for i in manifest.intents}
        for match in iter_intent_matches(nlu_intents):
            intent = by_form.get(match.form_name)
            if intent is None or intent.runtime is None:
                continue
            fields = apply_runtime_mapping(match, intent.runtime)
            if fields is None:
                continue
            if intent.runtime.kind == "control":
                return ParsedControl(
                    action=intent.runtime.action,  # type: ignore[arg-type]
                    **fields,
                )
            if intent.runtime.kind == "play":
                # Play-side ParsedCommand has fixed fields; manifest
                # may declare no mapping (my_wave) or future query
                # mappings. Defaults match v1.5.0 hardcoded behaviour.
                return ParsedCommand(
                    kind=intent.runtime.action,  # type: ignore[arg-type]
                    query=fields.get("query", ""),
                    radio_mode=bool(fields.get("radio_mode", True)),
                )
            # Unknown kind — silently skip (consumer-side configuration
            # error; production logs would catch repeat offenders).
        return None

    # -----------------------------------------------------------------------
    # User-facing actions
    # -----------------------------------------------------------------------

    def export_to_override(self) -> str:
        """
        Copy bundled default into the override path (if not already there).

        First-time export bootstraps the override file from the
        manifest the user is currently effectively running. Subsequent
        invocations are a no-op — user edits aren't trampled.
        """
        path = self.override_path
        if path.exists():
            return f"Override уже существует: {path}"
        try:
            self._atomic_write_text(path, self._bundled_manifest_text())
        except OSError as exc:
            return f"Экспорт не удался — не могу записать {path}: {exc}"
        self._invalidate_cache()
        return f"Манифест экспортирован в {path}. Откройте файл во внешнем редакторе."

    def import_from_paste(self, paste: str) -> str:
        """
        Validate and write a TOML paste into the override file.

        ``data:base64,<b64>`` prefix triggers base64 decoding (fallback
        for MA UI clients that strip newlines from STRING fields).
        Sets :attr:`last_import_success` so the dispatcher in
        ``__init__.py`` can clear the paste field on success.
        """
        self._last_import_success = False
        if not paste or not paste.strip():
            return "Поле для вставки пусто, нечего импортировать"

        text, decode_error = self._decode_paste(paste)
        if decode_error is not None:
            return f"Импорт не удался: {decode_error}"

        try:
            manifest = parse_manifest_text(text)
        except SkillManifestError as exc:
            return f"Импорт не удался — манифест невалиден: {exc}"

        path = self.override_path
        try:
            self._atomic_write_text(path, text)
        except OSError as exc:
            return f"Импорт не удался — не могу записать {path}: {exc}"

        self._invalidate_cache()
        self._last_import_success = True
        return (
            f"Манифест импортирован в {path} — "
            f"{len(manifest.intents)} intents, "
            f"{len(manifest.to_entity_drafts())} entities"
        )

    def reset_override(self) -> str:
        """
        Delete the override file so the bundled default takes effect.

        Idempotent in effect — calling on an already-clean state is
        a no-op. The returned message differs (``удалён`` vs
        ``отсутствует``) so the UI can confirm what actually happened.
        """
        path = self.override_path
        if not path.exists():
            return "Override отсутствует, используется bundled default"
        try:
            path.unlink()
        except OSError as exc:
            return f"Сброс не удался — не могу удалить {path}: {exc}"
        self._invalidate_cache()
        return f"Override удалён ({path}), используется bundled default"

    def validate_override_message(self) -> str:
        """
        Local-only validation of the override file.

        TOML parse + manifest schema check. Granet (Yandex) validation
        runs at "Apply skill changes" time; this method is a quick
        sanity check before that.
        """
        path = self.override_path
        if not path.exists():
            return "Override отсутствует, нечего валидировать"
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            return f"Не могу прочитать {path}: {exc}"
        try:
            manifest = parse_manifest_text(text)
        except SkillManifestError as exc:
            return f"✗ Override невалиден: {exc}"
        return (
            f"✓ Override валиден ({len(manifest.intents)} intents, "
            f"{len(manifest.to_entity_drafts())} entities)"
        )

    # -----------------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------------

    def _storage_root(self) -> Path:
        """
        MA storage root, with a documented fallback.

        Tries ``mass.storage_path`` first (the path MA-core uses for
        its own state). Falls back to ``$HOME/.musicassistant`` —
        matches the path the provider already documents for log files
        in ``dialogs.py``.
        """
        attr = getattr(self._mass, "storage_path", None)
        if attr:
            return Path(attr)
        return Path.home() / ".musicassistant"

    def _resolve(self) -> _Resolved:
        """
        Stat-keyed effective-manifest cache.

        Cache key is ``(override_exists, override_mtime_ns)``. Returns
        the cached snapshot when the key matches; otherwise re-reads
        the override file (or bundled fallback) and refreshes the
        cache. Mutating actions (export / import / reset) must call
        :meth:`_invalidate_cache` so the next read picks up the change
        even when the new mtime collides with the old (rare on most
        filesystems but possible on coarse-resolution clocks).
        """
        path = self.override_path
        try:
            stat = path.stat()
        except FileNotFoundError:
            key: tuple[bool, int | None] = (False, None)
        else:
            key = (True, stat.st_mtime_ns)

        if self._resolved_cache_key == key and self._resolved_cache is not None:
            return self._resolved_cache

        if not key[0]:
            resolved = _Resolved(
                manifest=self._bundled_manifest(),
                source="bundled",
                error=None,
            )
        else:
            try:
                override = parse_manifest_text(path.read_text(encoding="utf-8"))
            except (OSError, SkillManifestError) as exc:
                _LOGGER.warning(
                    "skill manifest override at %s is invalid (%s); "
                    "falling back to bundled default",
                    path,
                    exc,
                )
                resolved = _Resolved(
                    manifest=self._bundled_manifest(),
                    source="override_invalid",
                    error=str(exc),
                )
            else:
                resolved = _Resolved(
                    manifest=override,
                    source="override_valid",
                    error=None,
                )

        self._resolved_cache_key = key
        self._resolved_cache = resolved
        return resolved

    def _invalidate_cache(self) -> None:
        """Drop the resolved-manifest cache (called by mutating actions)."""
        self._resolved_cache_key = None
        self._resolved_cache = None

    def _bundled_manifest(self) -> SkillManifest:
        if self._bundled_cache is None:
            self._bundled_cache = parse_manifest_text(self._bundled_manifest_text())
        return self._bundled_cache

    @staticmethod
    def _bundled_manifest_text() -> str:
        # Resolve via this module's own package so the lookup keeps
        # working after the upstream sync renames the package from
        # ``provider`` to ``music_assistant.providers.yandex_alice``.
        ref = importlib.resources.files(__package__).joinpath("data/skill.toml")
        return ref.read_text(encoding="utf-8")

    @staticmethod
    def _atomic_write_text(path: Path, text: str) -> None:
        """
        Write ``text`` to ``path`` atomically (tmp file + ``os.replace``).

        Replaces the target in-place on POSIX/NT — readers either see
        the old content or the fully-written new content, never a
        partially-flushed file. Parent directories are created if
        missing. Bubbles up :class:`OSError` so callers can surface a
        useful message.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        try:
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(path)
        except OSError:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise

    @staticmethod
    def _decode_paste(paste: str) -> tuple[str, str | None]:
        """
        Return ``(decoded_text, error_message)``.

        On success ``error_message`` is ``None``. On base64 decode
        failure the original paste is returned alongside an
        explanation.

        Whitespace inside the base64 payload is stripped before
        decoding — most ``base64`` CLI tools wrap output at 76 columns
        by default, so a copy-paste from ``base64 -i skill.toml``
        contains newlines that ``validate=True`` would otherwise
        reject.
        """
        if not paste.startswith(_BASE64_PASTE_PREFIX):
            return paste, None
        raw = paste.removeprefix(_BASE64_PASTE_PREFIX)
        encoded = "".join(raw.split())
        try:
            decoded_bytes = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            return paste, f"base64 декодирование не удалось: {exc}"
        try:
            return decoded_bytes.decode("utf-8"), None
        except UnicodeDecodeError as exc:
            return paste, f"декодированные данные не UTF-8: {exc}"
