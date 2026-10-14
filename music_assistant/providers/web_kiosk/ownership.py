"""Connection-bound proof of ownership for browser Sendspin identities."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey


@dataclass(frozen=True)
class VerifiedSendspinClaim:
    """A Sendspin identity verified for one native kiosk player."""

    player_id: str
    client_id: str


class SendspinClaimChallenge:
    """Verify knowledge of the private key behind a Sendspin client ID."""

    def __init__(self, player_id: str, client_id: str) -> None:
        """Create a fresh, connection-bound challenge."""
        self.player_id = player_id
        self.client_id = client_id
        self._key = X25519PrivateKey.generate()
        nonce = secrets.token_urlsafe(32)
        self.message = f"web-kiosk-sendspin-claim-v1:{nonce}:{player_id}:{client_id}"

    def payload(self) -> dict[str, str]:
        """Return the public challenge for the browser SDK adapter."""
        return {
            "type": "sendspin_challenge",
            "client_id": self.client_id,
            "server_key": base64.urlsafe_b64encode(self._key.public_key().public_bytes_raw())
            .decode()
            .rstrip("="),
            "message": self.message,
        }

    def verify(self, proof: object) -> VerifiedSendspinClaim | None:
        """Verify a response without transmitting the browser private key."""
        if not isinstance(proof, str) or len(proof) != 64 or not proof.isascii():
            return None
        try:
            public = X25519PublicKey.from_public_bytes(
                base64.urlsafe_b64decode(self.client_id + "=")
            )
            shared = self._key.exchange(public)
            expected = hmac.new(shared, self.message.encode(), hashlib.sha256).hexdigest()
            if hmac.compare_digest(expected, proof):
                return VerifiedSendspinClaim(self.player_id, self.client_id)
        except ValueError:
            pass
        return None
