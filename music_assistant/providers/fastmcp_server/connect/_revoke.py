"""Best-effort token revocation helper for the Connect Wizard.

Lives in its own module so both :mod:`provider.connect.handlers` and
:mod:`provider.connect.actions` can import it without coupling them to each
other.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

LOGGER = logging.getLogger(__name__)


async def revoke_token_by_id(
    mass: MusicAssistant, token_id: str, *, user_id: str | None = None
) -> bool:
    """Delete a token row and drop any WS bound to it. Best-effort, never raises.

    :param mass: MusicAssistant instance.
    :param token_id: ``jti`` of the token to revoke.
    :param user_id: When set, verify the row belongs to this user before
        deleting. Required when ``token_id`` comes from client input —
        prevents a cross-user revoke when the caller can name a foreign
        token id. Leave ``None`` only when the token id is intrinsically
        scoped to the calling user (e.g. came from a just-verified JWT or
        from a row already filtered by user_id).
    :return: ``True`` if the delete actually ran, ``False`` if the call
        no-opped (ownership mismatch, lookup raise, delete raise). Callers
        that need to know whether to mark this id as "already handled"
        downstream should consult this return value.
    """
    if user_id is not None:
        try:
            owned = await mass.webserver.auth.database.get_rows(
                "auth_tokens", {"token_id": token_id, "user_id": user_id}, limit=1
            )
        except Exception:
            LOGGER.exception(
                "Connect Wizard: ownership check failed (token_id=%s, user_id=%s)",
                token_id,
                user_id,
            )
            return False
        if not owned:
            return False

    try:
        await mass.webserver.auth.database.delete("auth_tokens", {"token_id": token_id})
    except Exception:
        LOGGER.exception("Connect Wizard: token revoke failed (token_id=%s)", token_id)
        return False
    try:
        mass.webserver.disconnect_websockets_for_token(token_id)
    except Exception:
        LOGGER.exception(
            "Connect Wizard: WS disconnect after revoke failed (token_id=%s)", token_id
        )
    return True
