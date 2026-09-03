"""
Pure helpers for assembling Yandex Dialogs skill metadata.

Side-effect free: no MA / aiohttp / network access. Lives in its own module
so the orchestrator (auto_create.py / auto_update.py) can be tested without
threading these strings through every fixture.
"""

from __future__ import annotations

from typing import Any

from .constants import DIALOG_NAME_MAX_LEN, DIALOG_NAME_MIN_LEN, DIALOG_WEBHOOK_BASE_PATH
from .url_helpers import is_public_https_url


def validate_skill_name(value: object) -> bool:
    """
    Pre-flight check for the Yandex Dialogs skill name field.

    Yandex enforces three constraints when a skill is created:

    1. **At least two whitespace-separated words.** Single-word names are
       rejected by the dev-console form (Russian: "Название должно содержать
       минимум два слова"). The auto-create pipeline would surface this only
       after Device Flow + ``create_app`` round-trip, so we check up front.
    2. **2 to 64 characters** (after stripping leading/trailing whitespace).
    3. Globally unique across all Yandex skills — only checkable at
       ``create_app`` time, not here.

    Returns ``True`` when the value passes 1+2 (uniqueness is server-side).
    Designed for use as ``ConfigEntry(validate=validate_skill_name)`` —
    accepts ``object`` because that's the type the MA frontend hands us.
    """
    if not isinstance(value, str):
        return False
    stripped = value.strip()
    if len(stripped.split()) < 2:
        return False
    return DIALOG_NAME_MIN_LEN <= len(stripped) <= DIALOG_NAME_MAX_LEN


def validate_activation_phrase(value: object) -> bool:
    """
    Pre-flight check for an *optional* extra activation phrase.

    Same word/length rules as :func:`validate_skill_name`, but an
    **empty / whitespace-only** value is accepted — the slot is
    optional. The dispatcher drops empty slots before assembling the
    list sent to Yandex.
    """
    if not isinstance(value, str):
        return False
    stripped = value.strip()
    if not stripped:
        return True  # empty slot — optional
    if len(stripped.split()) < 2:
        return False
    return DIALOG_NAME_MIN_LEN <= len(stripped) <= DIALOG_NAME_MAX_LEN


def build_backend_uri(base_url: str, webhook_secret: str) -> str:
    """
    Compose the public webhook URL Yandex must call.

    Yandex sends voice phrases directly from its cloud to the URL we
    register, so the host must be reachable from the public internet.
    We require HTTPS *and* reject private / loopback / link-local hosts
    up front — otherwise auto-create would happily register a URL Yandex
    can't reach (e.g. ``https://192.168.1.10`` or ``https://localhost``)
    and the user would only discover the failure once moderation finishes.

    :raises ValueError: ``base_url`` is empty / not a public HTTPS URL,
        or ``webhook_secret`` is empty.
    """
    base = (base_url or "").strip().rstrip("/")
    if not base:
        msg = "External base URL is empty — set a public HTTPS URL for Yandex first"
        raise ValueError(msg)
    if not is_public_https_url(base):
        msg = (
            f"External base URL must be a public HTTPS endpoint Yandex can "
            f"reach over the internet (got: {base!r}). Private IPs, loopback "
            f"and link-local addresses are rejected."
        )
        raise ValueError(msg)
    secret = (webhook_secret or "").strip()
    if not secret:
        msg = "Webhook secret is empty — open the form once to auto-generate one"
        raise ValueError(msg)
    return f"{base}{DIALOG_WEBHOOK_BASE_PATH}/{secret}"


def build_skill_description(skill_name: str) -> str:
    """
    Default Russian description shown in the Alice catalog and to moderators.

    Yandex rejects empty descriptions for ``aliceSkill`` skills, so we always
    return a non-empty string. Embeds the skill name so the catalog listing
    is self-contained.
    """
    name = (skill_name or "").strip() or "Music Assistant"
    return (
        f"Голосовое управление Music Assistant через навык «{name}». "
        "Включение треков, управление воспроизведением и громкостью, "
        "перемещение очереди между колонками."
    )


def build_activation_phrases(skill_name: str) -> list[str]:
    """Default activation phrase list — single entry: the skill name itself."""
    name = (skill_name or "").strip() or "Music Assistant"
    return [name]


def build_structured_examples(skill_name: str) -> list[dict[str, Any]]:
    """
    Default structured examples shown to moderators.

    Shape captured from a successful PATCH issued by the dev console after a
    manual form fill (see ya_dialogs_api.api_client.build_dialog_draft_payload
    docstring). Three concrete commands so reviewers can see playback,
    transport, and multi-room intents without us guessing what passes review.
    """
    name = (skill_name or "").strip() or "Music Assistant"
    return [
        {
            "marker": "попроси",
            "activationPhrase": name,
            "request": "включи джаз",
            "is_valid": True,
        },
        {
            "marker": "попроси",
            "activationPhrase": name,
            "request": "поставь на паузу",
            "is_valid": True,
        },
        {
            "marker": "попроси",
            "activationPhrase": name,
            "request": "переведи музыку на кухню",
            "is_valid": True,
        },
    ]
