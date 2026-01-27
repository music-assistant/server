"""Native pairing implementations for AirPlay devices.

This module provides pairing support for:
- AirPlay 2 (HAP - HomeKit Accessory Protocol) - for Apple TV 4+, HomePod, Mac
- RAOP (AirPlay 1 legacy pairing) - for older devices

Both implementations produce credentials compatible with cliap2/cliraop.
"""

from __future__ import annotations

import binascii
import hashlib
import logging
import os
import plistlib
import uuid

import aiohttp
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from music_assistant_models.errors import PlayerCommandFailed
from srptools import SRPClientSession, SRPContext

from .constants import StreamingProtocol

# ============================================================================
# Common utilities
# ============================================================================


def hkdf_derive(
    input_key: bytes,
    salt: bytes,
    info: bytes,
    length: int = 32,
) -> bytes:
    """Derive key using HKDF-SHA512.

    :param input_key: Input keying material.
    :param salt: Salt value.
    :param info: Context info.
    :param length: Output key length.
    :return: Derived key bytes.
    """
    hkdf = HKDF(
        algorithm=hashes.SHA512(),
        length=length,
        salt=salt,
        info=info,
    )
    return hkdf.derive(input_key)


# ============================================================================
# TLV encoding/decoding for HAP
# ============================================================================

# TLV types for HAP pairing
TLV_METHOD = 0x00
TLV_IDENTIFIER = 0x01
TLV_SALT = 0x02
TLV_PUBLIC_KEY = 0x03
TLV_PROOF = 0x04
TLV_ENCRYPTED_DATA = 0x05
TLV_STATE = 0x06
TLV_ERROR = 0x07
TLV_SIGNATURE = 0x0A


def tlv_encode(items: list[tuple[int, bytes]]) -> bytes:
    """Encode items into TLV format.

    :param items: List of (type, value) tuples.
    :return: TLV-encoded bytes.
    """
    result = bytearray()
    for tlv_type, value in items:
        offset = 0
        while offset < len(value):
            chunk = value[offset : offset + 255]
            result.append(tlv_type)
            result.append(len(chunk))
            result.extend(chunk)
            offset += 255
        if len(value) == 0:
            result.append(tlv_type)
            result.append(0)
    return bytes(result)


def tlv_decode(data: bytes) -> dict[int, bytes]:
    """Decode TLV format into dictionary.

    :param data: TLV-encoded bytes.
    :return: Dictionary mapping type to concatenated value.
    """
    result: dict[int, bytearray] = {}
    offset = 0
    while offset < len(data):
        tlv_type = data[offset]
        length = data[offset + 1]
        value = data[offset + 2 : offset + 2 + length]
        if tlv_type in result:
            result[tlv_type].extend(value)
        else:
            result[tlv_type] = bytearray(value)
        offset += 2 + length
    return {k: bytes(v) for k, v in result.items()}


# ============================================================================
# HAP Pairing constants (for AirPlay 2)
# ============================================================================

# SRP 3072-bit prime for HAP (hex string format for srptools)
HAP_SRP_PRIME_3072 = (
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74"
    "020BBEA63B139B22514A08798E3404DDEF9519B3CD3A431B302B0A6DF25F1437"
    "4FE1356D6D51C245E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
    "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3DC2007CB8A163BF05"
    "98DA48361C55D39A69163FA8FD24CF5F83655D23DCA3AD961C62F356208552BB"
    "9ED529077096966D670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B"
    "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9DE2BCBF695581718"
    "3995497CEA956AE515D2261898FA051015728E5A8AAAC42DAD33170D04507A33"
    "A85521ABDF1CBA64ECFB850458DBEF0A8AEA71575D060C7DB3970F85A6E1E4C7"
    "ABF5AE8CDB0933D71E8C94E04A25619DCEE3D2261AD2EE6BF12FFA06D98A0864"
    "D87602733EC86A64521F2B18177B200CBBE117577A615D6C770988C0BAD946E2"
    "08E24FA074E5AB3143DB5BFCE0FD108E4B82D120A93AD2CAFFFFFFFFFFFFFFFF"
)
HAP_SRP_GENERATOR = "5"


# ============================================================================
# RAOP Pairing constants (for AirPlay 1 legacy)
# ============================================================================

# SRP 2048-bit prime for RAOP (hex string format for srptools)
RAOP_SRP_PRIME_2048 = (
    "AC6BDB41324A9A9BF166DE5E1389582FAF72B6651987EE07FC319294"
    "3DB56050A37329CBB4A099ED8193E0757767A13DD52312AB4B03310D"
    "CD7F48A9DA04FD50E8083969EDB767B0CF6095179A163AB3661A05FB"
    "D5FAAAE82918A9962F0B93B855F97993EC975EEAA80D740ADBF4FF74"
    "7359D041D5C33EA71D281E446B14773BCA97B43A23FB801676BD207A"
    "436C6481F1D2B9078717461A5B9D32E688F87748544523B524B0D57D"
    "5EA77A2775D2ECFA032CFBDBF52FB3786160279004E57AE6AF874E73"
    "03CE53299CCC041C7BC308D82A5698F3A8D0C38271AE35F8E9DBFBB6"
    "94B5C803D89F7AE435DE236D525F54759B65E372FCD68EF20FA7111F"
    "9E4AFF73"
)
RAOP_SRP_GENERATOR = "02"  # RFC5054-2048bit uses generator 2


# ============================================================================
# Base Pairing class
# ============================================================================


class AirPlayPairing:
    """Base class for AirPlay pairing.

    Handles both HAP (AirPlay 2) and RAOP (AirPlay 1) pairing protocols.
    """

    def __init__(
        self,
        address: str,
        name: str,
        protocol: StreamingProtocol,
        logger: logging.Logger,
        port: int | None = None,
        device_id: str | None = None,
    ) -> None:
        """Initialize AirPlay pairing.

        :param address: IP address of the device.
        :param name: Display name of the device.
        :param protocol: Streaming protocol (RAOP or AIRPLAY2).
        :param logger: Logger instance.
        :param port: Port number (default: 7000 for AirPlay 2, 5000 for RAOP).
        :param device_id: Device identifier (DACP ID) - must match what cliap2 uses.
        """
        self.address = address
        self.name = name
        self.protocol = protocol
        self.logger = logger
        self.port = port or (7000 if protocol == StreamingProtocol.AIRPLAY2 else 5000)

        # HTTP session
        self._session: aiohttp.ClientSession | None = None
        self._base_url: str = f"http://{address}:{self.port}"

        # Common state
        self._is_pairing: bool = False
        self._srp_context: SRPContext | None = None
        self._srp_session: SRPClientSession | None = None
        self._session_key: bytes | None = None

        # Client identifier (device_id) handling depends on protocol:
        # - HAP (AirPlay 2): Uses DACP ID as string identifier (must match cliap2 pair-verify)
        # - RAOP: Uses 8 random bytes (not the DACP ID) - credentials are self-contained
        if protocol == StreamingProtocol.AIRPLAY2:
            # For HAP, use DACP ID as the identifier (must match pair-verify)
            if device_id:
                self._client_id: bytes = device_id.encode()
            else:
                self._client_id = str(uuid.uuid4()).encode()
        else:
            # For RAOP, generate 8 random bytes for client_id
            # The credentials format is client_id_hex:auth_secret_hex
            self._client_id = os.urandom(8)

        # Ed25519 keypair
        self._client_private_key: Ed25519PrivateKey | None = None
        self._client_public_key: bytes | None = None

        # Server's public key
        self._server_public_key: bytes | None = None

    @property
    def is_pairing(self) -> bool:
        """Return True if a pairing session is in progress."""
        return self._is_pairing

    @property
    def device_provides_pin(self) -> bool:
        """Return True if the device displays the PIN."""
        return True  # Both HAP and RAOP display PIN on device

    @property
    def protocol_name(self) -> str:
        """Return human-readable protocol name."""
        if self.protocol == StreamingProtocol.RAOP:
            return "RAOP (AirPlay 1)"
        return "AirPlay"

    async def start_pairing(self) -> bool:
        """Start the pairing process.

        :return: True if device provides PIN (always True for AirPlay).
        :raises PlayerCommandFailed: If device connection fails.
        """
        self.logger.info(
            "Starting %s pairing with %s at %s:%d",
            self.protocol_name,
            self.name,
            self.address,
            self.port,
        )

        # Generate Ed25519 keypair
        self._client_private_key = Ed25519PrivateKey.generate()
        self._client_public_key = self._client_private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )

        # Create HTTP session
        self._session = aiohttp.ClientSession()

        try:
            # Request PIN to be shown on device
            async with self._session.post(
                f"{self._base_url}/pair-pin-start",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    raise PlayerCommandFailed(f"Failed to start pairing: HTTP {resp.status}")

            self._is_pairing = True
            self.logger.info("Device %s is displaying PIN", self.name)

            # SRP context will be created in finish_pairing when we have the PIN
            return True

        except aiohttp.ClientError as err:
            await self.close()
            raise PlayerCommandFailed(f"Connection failed: {err}") from err

    async def finish_pairing(self, pin: str) -> str:
        """Complete pairing with the provided PIN.

        :param pin: 4-digit PIN from device screen.
        :return: Credentials string for cliap2/cliraop.
        :raises PlayerCommandFailed: If pairing fails.
        """
        if not self._session:
            raise PlayerCommandFailed("Pairing not started")

        try:
            if self.protocol == StreamingProtocol.AIRPLAY2:
                return await self._finish_hap_pairing(pin)
            return await self._finish_raop_pairing(pin)
        except PlayerCommandFailed:
            raise
        except Exception as err:
            self.logger.exception("Pairing failed")
            raise PlayerCommandFailed(f"Pairing failed: {err}") from err
        finally:
            await self.close()

    # ========================================================================
    # HAP (AirPlay 2) pairing implementation
    # ========================================================================

    async def _finish_hap_pairing(self, pin: str) -> str:
        """Complete HAP pairing for AirPlay 2.

        :param pin: 4-digit PIN.
        :return: Credentials (192 hex chars).
        """
        if not self._session:
            raise PlayerCommandFailed("Pairing not started")

        self.logger.info("Completing HAP pairing with PIN")

        # HAP headers required for pair-setup
        hap_headers = {
            "Content-Type": "application/octet-stream",
            "X-Apple-HKP": "3",
        }

        # M1: Send method request (state=1, method=0 for pair-setup)
        m1_data = tlv_encode(
            [
                (TLV_METHOD, bytes([0x00])),
                (TLV_STATE, bytes([0x01])),
            ]
        )

        async with self._session.post(
            f"{self._base_url}/pair-setup",
            data=m1_data,
            headers=hap_headers,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status != 200:
                raise PlayerCommandFailed(f"M1 failed: HTTP {resp.status}")
            m2_data = await resp.read()

        # Parse M2
        m2 = tlv_decode(m2_data)
        if TLV_ERROR in m2:
            raise PlayerCommandFailed(f"Device error in M2: {m2[TLV_ERROR].hex()}")

        salt = m2.get(TLV_SALT)
        server_pk_srp = m2.get(TLV_PUBLIC_KEY)
        if not salt or not server_pk_srp:
            raise PlayerCommandFailed("Invalid M2: missing salt or public key")

        # M3: SRP authentication - create context with password
        # PIN is passed directly as string (not "Pair-Setup:PIN")
        # Note: pyatv doesn't specify bits_random, uses default
        self._srp_context = SRPContext(
            username="Pair-Setup",
            password=pin,
            prime=HAP_SRP_PRIME_3072,
            generator=HAP_SRP_GENERATOR,
            hash_func=hashlib.sha512,
        )
        # Pass Ed25519 private key bytes as the SRP "a" value (random private exponent)
        # This is what pyatv does - use the client's Ed25519 private key as the SRP private value
        if not self._client_private_key:
            raise PlayerCommandFailed("Client private key not initialized")
        auth_private = self._client_private_key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        self._srp_session = SRPClientSession(
            self._srp_context, binascii.hexlify(auth_private).decode()
        )

        # Process with server's public key and salt (as hex strings)
        self._srp_session.process(server_pk_srp.hex(), salt.hex())

        # Get client's public key and proof
        client_pk_srp = bytes.fromhex(self._srp_session.public)
        client_proof = bytes.fromhex(self._srp_session.key_proof.decode("ascii"))

        m3_data = tlv_encode(
            [
                (TLV_STATE, bytes([0x03])),
                (TLV_PUBLIC_KEY, client_pk_srp),
                (TLV_PROOF, client_proof),
            ]
        )

        async with self._session.post(
            f"{self._base_url}/pair-setup",
            data=m3_data,
            headers=hap_headers,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status != 200:
                raise PlayerCommandFailed(f"M3 failed: HTTP {resp.status}")
            m4_data = await resp.read()

        # Parse M4
        m4 = tlv_decode(m4_data)
        if TLV_ERROR in m4:
            raise PlayerCommandFailed(f"Device error in M4: {m4[TLV_ERROR].hex()}")

        server_proof = m4.get(TLV_PROOF)
        if not server_proof:
            raise PlayerCommandFailed("Invalid M4: missing proof")

        # Verify server proof
        if not self._srp_session.verify_proof(server_proof.hex().encode("ascii")):
            raise PlayerCommandFailed("Server proof verification failed")

        # Get session key
        self._session_key = bytes.fromhex(self._srp_session.key.decode("ascii"))

        # M5: Send encrypted client info
        await self._send_hap_m5()

        # Generate credentials
        return self._generate_hap_credentials()

    async def _send_hap_m5(self) -> None:
        """Send M5 with encrypted client info and receive M6."""
        if (
            not self._session_key
            or not self._client_private_key
            or not self._client_public_key
            or not self._session
        ):
            raise PlayerCommandFailed("Invalid state for M5")

        # HAP headers required for pair-setup
        hap_headers = {
            "Content-Type": "application/octet-stream",
            "X-Apple-HKP": "3",
        }

        # Derive keys
        enc_key = hkdf_derive(
            self._session_key,
            b"Pair-Setup-Encrypt-Salt",
            b"Pair-Setup-Encrypt-Info",
            32,
        )
        sign_key = hkdf_derive(
            self._session_key,
            b"Pair-Setup-Controller-Sign-Salt",
            b"Pair-Setup-Controller-Sign-Info",
            32,
        )

        # Sign device info
        device_info = sign_key + self._client_id + self._client_public_key
        signature = self._client_private_key.sign(device_info)

        # Create and encrypt inner TLV
        inner_tlv = tlv_encode(
            [
                (TLV_IDENTIFIER, self._client_id),
                (TLV_PUBLIC_KEY, self._client_public_key),
                (TLV_SIGNATURE, signature),
            ]
        )

        cipher = ChaCha20Poly1305(enc_key)
        # Nonce format: 4 zero bytes + 8-byte message identifier = 12 bytes
        nonce = b"\x00\x00\x00\x00PS-Msg05"
        encrypted = cipher.encrypt(nonce, inner_tlv, None)

        # Send M5
        m5_data = tlv_encode(
            [
                (TLV_STATE, bytes([0x05])),
                (TLV_ENCRYPTED_DATA, encrypted),
            ]
        )

        async with self._session.post(
            f"{self._base_url}/pair-setup",
            data=m5_data,
            headers=hap_headers,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status != 200:
                raise PlayerCommandFailed(f"M5 failed: HTTP {resp.status}")
            m6_data = await resp.read()

        # Parse M6
        m6 = tlv_decode(m6_data)
        if TLV_ERROR in m6:
            raise PlayerCommandFailed(f"Device error in M6: {m6[TLV_ERROR].hex()}")

        encrypted_data = m6.get(TLV_ENCRYPTED_DATA)
        if not encrypted_data:
            raise PlayerCommandFailed("Invalid M6: missing encrypted data")

        # Decrypt M6
        # Nonce format: 4 zero bytes + 8-byte message identifier = 12 bytes
        nonce = b"\x00\x00\x00\x00PS-Msg06"
        decrypted = cipher.decrypt(nonce, encrypted_data, None)

        # Extract server's public key
        inner = tlv_decode(decrypted)
        self._server_public_key = inner.get(TLV_PUBLIC_KEY)
        if not self._server_public_key:
            raise PlayerCommandFailed("Invalid M6: missing server public key")

    def _generate_hap_credentials(self) -> str:
        """Generate HAP credentials for cliap2.

        Format: client_private_key(128 hex) + server_public_key(64 hex) = 192 hex chars

        :return: Credentials string.
        """
        if (
            not self._client_private_key
            or not self._server_public_key
            or not self._client_public_key
        ):
            raise PlayerCommandFailed("Missing keys for credential generation")

        # Get raw private key (32 bytes seed)
        private_key_bytes = self._client_private_key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )

        # Expand to 64-byte Ed25519 secret key format (seed + public_key)
        if len(private_key_bytes) == 32:
            private_key_bytes = private_key_bytes + self._client_public_key

        if len(private_key_bytes) != 64 or len(self._server_public_key) != 32:
            raise PlayerCommandFailed("Invalid key lengths")

        return binascii.hexlify(private_key_bytes).decode("ascii") + binascii.hexlify(
            self._server_public_key
        ).decode("ascii")

    # ========================================================================
    # RAOP (AirPlay 1 legacy) pairing implementation
    # ========================================================================

    def _compute_raop_premaster_secret(
        self,
        user_id: str,
        password: str,
        salt: bytes,
        client_private: bytes,
        client_public: bytes,
        server_public: bytes,
    ) -> bytes:
        """Compute RAOP SRP premaster secret S.

        S = (B - k*v)^(a + u*x) mod N

        :param user_id: Username (hex-encoded client_id).
        :param password: PIN code.
        :param salt: Salt from server.
        :param client_private: Client private key (a) as bytes.
        :param client_public: Client public key (A) as bytes.
        :param server_public: Server public key (B) as bytes.
        :return: Premaster secret S as bytes (padded to N length).
        """
        # Convert values to integers
        n_bytes = bytes.fromhex(RAOP_SRP_PRIME_2048)
        n_len = len(n_bytes)
        n = int.from_bytes(n_bytes, "big")
        g = int.from_bytes(bytes.fromhex(RAOP_SRP_GENERATOR), "big")

        a = int.from_bytes(client_private, "big")
        b_pub = int.from_bytes(server_public, "big")

        # x = H(s | H(I : P))
        inner_hash = hashlib.sha1(f"{user_id}:{password}".encode()).digest()
        x = int.from_bytes(hashlib.sha1(salt + inner_hash).digest(), "big")

        # k = H(N | PAD(g))
        g_padded = bytes.fromhex(RAOP_SRP_GENERATOR).rjust(n_len, b"\x00")
        k = int.from_bytes(hashlib.sha1(n_bytes + g_padded).digest(), "big")

        # u = H(PAD(A) | PAD(B))
        a_padded = client_public.rjust(n_len, b"\x00")
        b_padded = server_public.rjust(n_len, b"\x00")
        u = int.from_bytes(hashlib.sha1(a_padded + b_padded).digest(), "big")

        # v = g^x mod N
        v = pow(g, x, n)

        # S = (B - k*v)^(a + u*x) mod N
        s_int = pow(b_pub - k * v, a + u * x, n)

        # Convert to bytes and pad to N length
        s_bytes = s_int.to_bytes((s_int.bit_length() + 7) // 8, "big")
        return s_bytes.rjust(n_len, b"\x00")

    def _compute_raop_session_key(self, premaster_secret: bytes) -> bytes:
        r"""Compute RAOP session key K from premaster secret S.

        K = SHA1(S | \x00\x00\x00\x00) | SHA1(S | \x00\x00\x00\x01)

        This produces a 40-byte key (two SHA1 hashes concatenated).

        :param premaster_secret: The SRP premaster secret S.
        :return: 40-byte session key K.
        """
        k1 = hashlib.sha1(premaster_secret + b"\x00\x00\x00\x00").digest()
        k2 = hashlib.sha1(premaster_secret + b"\x00\x00\x00\x01").digest()
        return k1 + k2

    def _compute_raop_m1(
        self, user_id: str, salt: bytes, client_pk: bytes, server_pk: bytes, session_key: bytes
    ) -> bytes:
        """Compute RAOP SRP M1 proof with padding for A and B (but not g).

        M1 = H(H(N) XOR H(g) | H(I) | s | PAD(A) | PAD(B) | K)

        Note: g is NOT padded, but A and B ARE padded to N length.
        K is 40 bytes (from _compute_raop_session_key).

        :param user_id: Username (hex-encoded client_id).
        :param salt: Salt bytes from server.
        :param client_pk: Client public key (A).
        :param server_pk: Server public key (B).
        :param session_key: Session key (K) - 40 bytes.
        :return: M1 proof bytes (20 bytes for SHA-1).
        """
        n_bytes = bytes.fromhex(RAOP_SRP_PRIME_2048)
        n_len = len(n_bytes)
        g_bytes = bytes.fromhex(RAOP_SRP_GENERATOR)

        # H(N) XOR H(g) - g is NOT padded
        h_n = hashlib.sha1(n_bytes).digest()
        h_g = hashlib.sha1(g_bytes).digest()
        h_n_xor_h_g = bytes(a ^ b for a, b in zip(h_n, h_g, strict=True))

        # H(I) - hash of username
        h_i = hashlib.sha1(user_id.encode("ascii")).digest()

        # PAD A and B to N length
        a_padded = client_pk.rjust(n_len, b"\x00")
        b_padded = server_pk.rjust(n_len, b"\x00")

        # M1 = H(H(N) XOR H(g) | H(I) | s | PAD(A) | PAD(B) | K)
        m1_data = h_n_xor_h_g + h_i + salt + a_padded + b_padded + session_key
        return hashlib.sha1(m1_data).digest()

    def _compute_raop_client_public(self, auth_secret: bytes) -> bytes:
        """Compute RAOP SRP client public key A = g^a mod N.

        :param auth_secret: 32-byte random secret (used as SRP private key a).
        :return: Client public key A as bytes.
        """
        n_bytes = bytes.fromhex(RAOP_SRP_PRIME_2048)
        n = int.from_bytes(n_bytes, "big")
        g = int.from_bytes(bytes.fromhex(RAOP_SRP_GENERATOR), "big")
        a = int.from_bytes(auth_secret, "big")
        a_pub = pow(g, a, n)
        return a_pub.to_bytes((a_pub.bit_length() + 7) // 8, "big")

    async def _finish_raop_pairing(self, pin: str) -> str:
        """Complete RAOP pairing for AirPlay 1.

        :param pin: 4-digit PIN.
        :return: Credentials (client_id:auth_secret format).
        """
        if not self._session:
            raise PlayerCommandFailed("Pairing not started")

        self.logger.info("Completing RAOP pairing with PIN")

        # Generate 32-byte auth secret
        auth_secret = os.urandom(32)

        # Derive Ed25519 public key from auth secret
        # For RAOP, we use the auth_secret as the Ed25519 seed
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: PLC0415
            Ed25519PrivateKey as Ed25519Key,
        )

        auth_private_key = Ed25519Key.from_private_bytes(auth_secret)
        auth_public_key = auth_private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )

        # Step 1: Send device ID and method
        user_id = self._client_id.hex().upper()
        step1_plist = {
            "method": "pin",
            "user": user_id,
        }

        async with self._session.post(
            f"{self._base_url}/pair-setup-pin",
            data=plistlib.dumps(step1_plist, fmt=plistlib.FMT_BINARY),
            headers={"Content-Type": "application/x-apple-binary-plist"},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status != 200:
                raise PlayerCommandFailed(f"RAOP step 1 failed: HTTP {resp.status}")
            step1_response = plistlib.loads(await resp.read())

        # Get salt and server public key
        salt, server_pk = step1_response.get("salt"), step1_response.get("pk")
        if not salt or not server_pk:
            raise PlayerCommandFailed("Invalid RAOP step 1 response")

        # Step 2: SRP authentication
        # Apple uses a custom K formula: K = SHA1(S|0000) | SHA1(S|0001) (40 bytes)
        client_pk = self._compute_raop_client_public(auth_secret)
        premaster_secret = self._compute_raop_premaster_secret(
            user_id, pin, salt, auth_secret, client_pk, server_pk
        )
        session_key = self._compute_raop_session_key(premaster_secret)
        client_proof = self._compute_raop_m1(user_id, salt, client_pk, server_pk, session_key)

        step2_plist = {
            "pk": client_pk,
            "proof": client_proof,
        }

        async with self._session.post(
            f"{self._base_url}/pair-setup-pin",
            data=plistlib.dumps(step2_plist, fmt=plistlib.FMT_BINARY),
            headers={"Content-Type": "application/x-apple-binary-plist"},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status != 200:
                raise PlayerCommandFailed(f"RAOP step 2 failed: HTTP {resp.status}")
            step2_response = plistlib.loads(await resp.read())

        # Verify server proof M2 exists (verification optional)
        server_proof = step2_response.get("proof")
        if not server_proof:
            raise PlayerCommandFailed("RAOP server did not return proof")
        self._session_key = session_key

        # Step 3: Encrypt and send auth public key using AES-GCM
        # Derive AES key and IV from session key K (40 bytes)
        aes_key = hashlib.sha512(b"Pair-Setup-AES-Key" + session_key).digest()[:16]
        aes_iv = bytearray(hashlib.sha512(b"Pair-Setup-AES-IV" + session_key).digest()[:16])
        aes_iv[-1] = (aes_iv[-1] + 1) % 256  # Increment last byte

        # Encrypt auth public key with AES-GCM
        cipher = Cipher(algorithms.AES(aes_key), modes.GCM(bytes(aes_iv)))
        encryptor = cipher.encryptor()
        encrypted_pk = encryptor.update(auth_public_key) + encryptor.finalize()
        tag = encryptor.tag

        step3_plist = {
            "epk": encrypted_pk,
            "authTag": tag,
        }

        async with self._session.post(
            f"{self._base_url}/pair-setup-pin",
            data=plistlib.dumps(step3_plist, fmt=plistlib.FMT_BINARY),
            headers={"Content-Type": "application/x-apple-binary-plist"},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status != 200:
                raise PlayerCommandFailed(f"RAOP step 3 failed: HTTP {resp.status}")

        # Return credentials in cliraop format: client_id:auth_secret
        return f"{self._client_id.hex()}:{auth_secret.hex()}"

    # ========================================================================
    # Cleanup
    # ========================================================================

    async def close(self) -> None:
        """Clean up resources."""
        self._is_pairing = False
        if self._session:
            await self._session.close()
            self._session = None
        self._srp_context = None
        self._srp_session = None
        self._session_key = None
