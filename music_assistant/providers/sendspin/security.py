"""Server identity persistence for the Sendspin provider."""

from __future__ import annotations

import os
from pathlib import Path

from aiosendspin.noise.keys import Identity, b64url_decode

IDENTITY_FILENAME = "identity.key"


def get_or_create_server_identity(storage_dir: Path) -> Identity:
    """
    Load the persistent Sendspin server identity, generating one only if absent.

    Raises on a corrupt or unreadable key file rather than minting a new identity
    over trusted state. Blocking (file I/O) - call via asyncio.to_thread.

    :param storage_dir: Directory holding the identity key file (created if needed).
    """
    storage_dir.mkdir(parents=True, exist_ok=True)
    key_path = storage_dir / IDENTITY_FILENAME
    try:
        return Identity.from_private_bytes(b64url_decode(key_path.read_text().strip()))
    except FileNotFoundError:
        pass
    identity = Identity.generate()
    try:
        fd = os.open(key_path, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
    except FileExistsError:
        return Identity.from_private_bytes(b64url_decode(key_path.read_text().strip()))
    with os.fdopen(fd, "w") as f:
        f.write(identity.private_b64u)
    return identity
