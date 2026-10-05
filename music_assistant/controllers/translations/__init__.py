"""
Controller that owns translation strings for server-provided objects.

Covers config entries, config option titles, provider manifests and localizable media item names.
English source strings are authored in ``strings.json`` files (a shared
``music_assistant/strings.json`` plus a per-provider/per-controller ``strings.json``);
``build_translations.py`` compiles them into ``translations/en.json``, which Lokalise syncs against,
and translated languages are downloaded to ``translations/<lang>.json``.

At runtime ``translations/en.json`` is loaded eagerly (the final fallback) and each translated
locale lazily on first use, so a lookup is always an in-memory dict get.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from music_assistant_models.helpers import create_safe_string

from music_assistant.helpers.api import api_command
from music_assistant.helpers.json import load_json_dict
from music_assistant.models.core_controller import CoreController

if TYPE_CHECKING:
    from music_assistant_models.config_entries import CoreConfig

# package paths (this file lives at music_assistant/controllers/translations/__init__.py)
PACKAGE_ROOT = str(Path(__file__).resolve().parents[2])
# translations/ holds the flat locale files: the generated en.json (English source, pushed to
# Lokalise) and the downloaded per-language <lang>.json files. The hand-authored sources live in
# strings.json files (music_assistant/strings.json + per-provider/per-controller strings.json).
TRANSLATIONS_PATH = os.path.join(PACKAGE_ROOT, "translations")

# the language the in-repo source strings are authored in
SOURCE_LANGUAGE = "en"
SOURCE_FILE = os.path.join(TRANSLATIONS_PATH, f"{SOURCE_LANGUAGE}.json")


class TranslationController(CoreController):
    """Loads and resolves translation strings for server-provided objects."""

    domain: str = "translations"

    def __init__(self, mass: Any) -> None:
        """Initialize the translations controller."""
        super().__init__(mass)
        self.manifest.name = "Translations"
        self.manifest.description = "Translation strings for server-provided objects."
        # FQ key -> English source string (eager, always loaded)
        self._source: dict[str, str] = {}
        # locale -> {FQ key -> translated string} (lazy, populated on first use)
        self._locales: dict[str, dict[str, str]] = {}
        # locale -> file path, discovered at startup (no parsing)
        self._locale_files: dict[str, str] = {}
        # de-duplicate concurrent cold loads of the same locale
        self._locale_locks: dict[str, asyncio.Lock] = {}
        self._available_locales: set[str] = {SOURCE_LANGUAGE}

    @property
    def available_locales(self) -> set[str]:
        """Return the set of locales the server can serve."""
        return self._available_locales

    @api_command("translations/locales")
    async def get_available_locales(self) -> list[str]:
        """Return the list of available UI locales (sorted)."""
        return sorted(self._available_locales)

    async def setup(self, config: CoreConfig) -> None:
        """Load the translation strings."""
        self.config = config
        self._locale_files = await asyncio.to_thread(_discover_locale_files)
        self._available_locales = {SOURCE_LANGUAGE, *self._locale_files}
        self._source = await self._load_flat(SOURCE_FILE)
        self.logger.debug(
            "Loaded %s source strings across %s locale(s)",
            len(self._source),
            len(self._available_locales),
        )

    def get_translation(
        self,
        key: str,
        locale: str | None = None,
        owner: str | None = None,
        params: list[str] | None = None,
    ) -> str | None:
        """
        Resolve a translation key for the given locale.

        Accepts either a fully-qualified key (e.g. "provider.ytmusic.manifest.name") or a
        relative key (e.g. "settings.cookie.label") plus an optional ``owner`` hint
        (provider domain/instance) used to build the owner-namespaced candidate. Returns the
        resolved string, or None when nothing matches so the caller keeps its existing value.
        Never raises.

        :param key: Fully-qualified or relative translation key.
        :param locale: Requested locale (e.g. "nl" or "de_DE"); None falls back to the source.
        :param owner: Optional owner hint (provider domain/instance) for relative keys.
        :param params: Optional positional arguments for ``{0}``/``{1}`` placeholders.
        """
        owner_prefix = _owner_prefix(owner) if owner else None
        # Candidates are ordered owner-specific -> common -> bare (see _candidate_keys); each
        # is probed in the requested locale, its base language and finally the English source.
        for candidate in _candidate_keys(key, owner_prefix):
            value = self._lookup(candidate, locale)
            if value is not None:
                return _format(value, params)
        return None

    async def ensure_locale_loaded(self, locale: str | None) -> None:
        """
        Load (and cache) the bundle for a locale and its base language if not already loaded.

        Call this when a connection declares/changes its locale so subsequent lookups never
        have to read from disk.

        :param locale: The locale to warm up (e.g. "nl" or "de_DE").
        """
        if not locale:
            return
        for candidate in _locale_candidates(locale):
            if (
                candidate == SOURCE_LANGUAGE
                or candidate in self._locales
                or candidate not in self._locale_files
            ):
                continue
            lock = self._locale_locks.setdefault(candidate, asyncio.Lock())
            async with lock:
                if candidate in self._locales:
                    continue
                self._locales[candidate] = await self._load_flat(self._locale_files[candidate])

    async def reverse_lookup_media_names(self, query: str) -> set[str]:
        """
        Return the canonical (English) media names whose localized value matches ``query``.

        Lets localized item names be found by the name the user sees: a text search that returns
        nothing literally can be retried against these canonical names (which equal the items'
        stored ``search_name``). Only genre and playlist names (``*.media.genre.*`` /
        ``*.media.playlist.*`` under any owner — ``common.`` or a provider, e.g.
        ``provider.builtin.media.playlist.*``) are considered — the searchable library media
        types; browse and recommendation folder titles are display-only and never library items.

        The reverse-translation always uses the metadata controller's configured language
        (``CONF_LANGUAGE``), which doubles as the fallback search locale; an English, unknown or
        untranslatable language yields an empty set (the literal search already covers English).

        :param query: The (possibly localized) search query.
        """
        locale = self.mass.metadata.locale
        normalized = create_safe_string(query, True, True)
        if not normalized or not locale or locale.split("_")[0] == SOURCE_LANGUAGE:
            return set()
        await self.ensure_locale_loaded(locale)
        bundle = self._locales.get(locale) or self._locales.get(locale.split("_")[0])
        if not bundle:
            return set()
        matches: set[str] = set()
        for key, value in bundle.items():
            if not key.endswith(".name"):
                continue
            if ".media.genre." not in key and ".media.playlist." not in key:
                continue
            if normalized in create_safe_string(value, True, True):
                if english := self._source.get(key):
                    matches.add(english)
        return matches

    def _lookup(self, key: str, locale: str | None) -> str | None:
        """Look up a single candidate key, locale bundle first then English source."""
        if locale:
            for candidate in _locale_candidates(locale):
                if candidate == SOURCE_LANGUAGE:
                    break
                if (bundle := self._locales.get(candidate)) and key in bundle:
                    return bundle[key]
        return self._source.get(key)

    async def _load_flat(self, path: str) -> dict[str, str]:
        """Load a flat {fq_key: str} translations file, tolerating a missing file or errors."""
        if not Path(path).is_file():
            return {}
        try:
            data = await load_json_dict(path)
        except Exception as err:
            self.logger.warning("Failed to load translations file %s: %s", path, err)
            return {}
        return {key: value for key, value in data.items() if isinstance(value, str)}


def _discover_locale_files() -> dict[str, str]:
    """
    Discover the locale files in translations/ (blocking).

    Returns a map of language -> file path for every ``translations/<lang>.json`` except the
    English source ``en.json``. Each file is a flat, fully-qualified key->string map.
    """
    locale_files: dict[str, str] = {}
    if not Path(TRANSLATIONS_PATH).is_dir():
        return locale_files
    for filename in os.listdir(TRANSLATIONS_PATH):  # noqa: PTH208, RUF100
        if not filename.endswith(".json"):
            continue
        lang = filename[: -len(".json")]
        if lang == SOURCE_LANGUAGE:
            continue
        locale_files[lang] = os.path.join(TRANSLATIONS_PATH, filename)
    return locale_files


def _owner_prefix(owner: str) -> str:
    """Map an owner hint (provider domain/instance, or an already-rooted prefix) to a key prefix."""
    if owner.startswith(("provider.", "core.", "common.")):
        return owner
    return f"provider.{owner}"


def _candidate_keys(key: str, owner_prefix: str | None = None) -> list[str]:
    """
    Build the ordered list of translation keys to try for a requested key.

    A fully-qualified key (starting with ``provider.``/``core.``/``common.``) is tried
    as-is plus a ``common.`` rewrite that drops the owner segment. A relative key is tried
    under the owner prefix (if any), then ``common.``, then bare. Multi-instance providers carry
    an ``<domain>--<id>`` instance id, so the domain-only prefix is also tried before ``common.``.
    Any candidate ending in ``.name`` also gets a bare fallback (dropping ``.name``).
    """
    roots = ("provider.", "core.", "common.")
    base_candidates: list[str] = []
    if key.startswith(roots):
        base_candidates.append(key)
        for prefix in ("provider.", "core."):
            if key.startswith(prefix):
                rest = key[len(prefix) :]
                if "." in rest:
                    base_candidates.append(f"common.{rest.split('.', 1)[1]}")
                break
    else:
        if owner_prefix:
            base_candidates.append(f"{owner_prefix}.{key}")
            # a multi-instance owner is "provider.<domain>--<id>"; also try the bare domain
            domain_prefix = owner_prefix.split("--", 1)[0]
            if domain_prefix != owner_prefix:
                base_candidates.append(f"{domain_prefix}.{key}")
        base_candidates.append(f"common.{key}")
        base_candidates.append(key)
    candidates: list[str] = []
    seen: set[str] = set()
    for candidate in base_candidates:
        variants = (
            (candidate, candidate[: -len(".name")]) if candidate.endswith(".name") else (candidate,)
        )
        for variant in variants:
            if variant not in seen:
                seen.add(variant)
                candidates.append(variant)
    return candidates


def _locale_candidates(locale: str) -> list[str]:
    """Return [normalized locale, base language] (deduplicated, order preserved)."""
    normalized = locale.replace("-", "_")
    base = normalized.split("_", 1)[0]
    return [normalized] if normalized == base else [normalized, base]


def _format(template: str, params: list[str] | None) -> str:
    """Substitute positional ``{0}``/``{1}`` placeholders, leaving the template intact on error."""
    if not params:
        return template
    try:
        return template.format(*params)
    except IndexError, KeyError, ValueError:
        return template
