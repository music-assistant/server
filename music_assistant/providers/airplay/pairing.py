"""
Pairing implementations for AirPlay devices.

This module provides pairing support for:
- AirPlay 2 (HAP - HomeKit Accessory Protocol) - for Apple TV 4+, HomePod, Mac.
  Delegated to the cliairplay binary (--pair-setup): the same HAP implementation
  performs pair-verify at stream time, so credentials and DACP identity always match.
- RAOP (AirPlay 1 legacy pairing) - for older devices, implemented natively.

Both produce credentials compatible with cliairplay.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import plistlib
import re

import aiohttp
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from music_assistant_models.errors import PlayerCommandFailed

from music_assistant.helpers.process import AsyncProcess
from music_assistant.helpers.util import format_ip_for_url

from .constants import StreamingProtocol
from .helpers import get_cli_binary

# Timeout for the binary to complete the SRP exchange after the PIN is entered
PAIR_SETUP_TIMEOUT = 60

# HAP error tag in the binary's pair-setup stderr lines ("... error tag: 3 (backoff ...)")
HAP_ERROR_TAG_RE = re.compile(r"error tag: (\d+)")

# ============================================================================
# RAOP Pairing constants (for AirPlay 1 legacy)
# ============================================================================

# SRP 2048-bit prime for RAOP (hex string format)
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


class AirPlayPairing:
    """
    Pairing session for an AirPlay device.

    Handles both HAP (AirPlay 2, via the cliairplay binary) and RAOP
    (AirPlay 1, native) pairing protocols.
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
        """
        Initialize AirPlay pairing.

        :param address: IP address of the device.
        :param name: Display name of the device.
        :param protocol: Streaming protocol (RAOP or AIRPLAY2).
        :param logger: Logger instance.
        :param port: Port number (default: 7000 for AirPlay 2, 5000 for RAOP).
        :param device_id: Device identifier (DACP ID) - must match what cliairplay
            uses at stream time (pair-verify signs with it).
        """
        self.address = address
        self.name = name
        self.protocol = protocol
        self.logger = logger
        self.port = port or (7000 if protocol == StreamingProtocol.AIRPLAY2 else 5000)
        self.device_id = device_id

        # cliairplay --pair-setup subprocess state (AirPlay 2)
        self._cli_binary: str | None = None
        self._pair_proc: AsyncProcess | None = None
        self._pair_proc_stderr: list[str] = []

        # HTTP session (RAOP)
        self._session: aiohttp.ClientSession | None = None
        self._base_url: str = f"http://{format_ip_for_url(address)}:{self.port}"

        # RAOP client identifier: 8 random bytes, the credentials are self-contained
        self._client_id = os.urandom(8)

    @property
    def protocol_name(self) -> str:
        """Return human-readable protocol name."""
        if self.protocol == StreamingProtocol.RAOP:
            return "RAOP (AirPlay 1)"
        return "AirPlay"

    async def start_pairing_session(self) -> None:
        """Prepare a new pairing session."""
        self.logger.info(
            "Starting %s pairing with %s at %s:%d",
            self.protocol_name,
            self.name,
            self.address,
            self.port,
        )
        if self.protocol == StreamingProtocol.AIRPLAY2:
            self._cli_binary = await get_cli_binary()
        else:
            self._session = aiohttp.ClientSession()

    async def start_pin_pairing(self) -> bool:
        """
        Start the pairing process, making the device display its PIN.

        :return: True if device provides PIN.
        :raises PlayerCommandFailed: If device connection fails.
        """
        if self.protocol == StreamingProtocol.AIRPLAY2:
            # the binary POSTs /pair-pin-start right after connecting,
            # then waits for the PIN on its stdin
            await self._start_pair_setup_process()
            self.logger.info("Device %s should now display its PIN", self.name)
            return True

        if not self._session:
            raise PlayerCommandFailed("Session not started")
        try:
            # Request PIN to be shown on device
            async with self._session.post(
                f"{self._base_url}/pair-pin-start",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    raise PlayerCommandFailed(f"Failed to start pairing: HTTP {resp.status}")

            self.logger.info("Device %s is displaying PIN", self.name)
            return True

        except aiohttp.ClientError as err:
            await self.close()
            raise PlayerCommandFailed(f"Connection failed: {err}") from err

    async def finish_pairing(self, pin: str) -> str:
        """
        Complete pairing with the provided PIN or password.

        :param pin: 4-digit PIN from device screen or device password.
        :return: Credentials string for cliairplay.
        :raises PlayerCommandFailed: If pairing fails.
        """
        try:
            if self.protocol == StreamingProtocol.AIRPLAY2:
                return await self._finish_cli_pair_setup(pin)
            if not self._session:
                raise PlayerCommandFailed("Pairing not started")
            return await self._finish_raop_pairing(pin)
        except PlayerCommandFailed:
            raise
        except Exception as err:
            self.logger.exception("Pairing failed")
            raise PlayerCommandFailed(f"Pairing failed: {err}") from err
        finally:
            await self.close()

    async def close(self) -> None:
        """Clean up resources."""
        if self._pair_proc and not self._pair_proc.closed:
            await self._pair_proc.kill()
        self._pair_proc = None
        if self._session:
            await self._session.close()
            self._session = None

    # ========================================================================
    # HAP (AirPlay 2) pairing via cliairplay --pair-setup
    # ========================================================================

    async def _start_pair_setup_process(self) -> None:
        """Spawn the cliairplay --pair-setup process (device shows its PIN)."""
        if self._pair_proc and not self._pair_proc.closed:
            return
        if not self._cli_binary:
            raise PlayerCommandFailed("Pairing not started")
        if not self.device_id:
            raise PlayerCommandFailed("Pairing requires a DACP id")
        args = [
            self._cli_binary,
            "--pair-setup",
            "--port",
            str(self.port),
            "--dacp",
            self.device_id,
            self.address,
        ]
        self._pair_proc_stderr = []
        self._pair_proc = AsyncProcess(
            args, stdin=True, stdout=True, stderr=True, name="cliairplay-pair-setup"
        )
        await self._pair_proc.start()
        self._pair_proc.attach_stderr_reader(
            asyncio.create_task(self._pair_setup_stderr_reader(self._pair_proc))
        )

    async def _pair_setup_stderr_reader(self, proc: AsyncProcess) -> None:
        """Collect (and debug-log) stderr output of the pair-setup process."""
        async for line in proc.iter_stderr():
            self._pair_proc_stderr.append(line)
            self.logger.debug("pair-setup: %s", line)

    async def _finish_cli_pair_setup(self, pin: str) -> str:
        """
        Complete HAP pairing by feeding the PIN to the cliairplay process.

        The binary performs the full SRP/HomeKit pair-setup exchange and
        prints ``CREDENTIALS: <192 hex chars>`` on stdout on success.

        :param pin: 4-digit PIN (or device password).
        """
        # password-only devices skip start_pin_pairing, spawn the process now
        await self._start_pair_setup_process()
        proc = self._pair_proc
        assert proc is not None  # type guard
        self.logger.info("Completing HAP pairing with PIN")
        try:
            await proc.write(f"{pin}\n".encode())
            credentials = await asyncio.wait_for(
                self._read_credentials(proc), timeout=PAIR_SETUP_TIMEOUT
            )
            returncode = await proc.wait_with_timeout(10)
        except (TimeoutError, BrokenPipeError, ConnectionResetError) as err:
            raise self._pair_setup_failure("Pairing failed") from err
        if not credentials or returncode != 0:
            raise self._pair_setup_failure(f"Pairing failed (exit code {returncode})")
        if len(credentials) != 192:
            raise PlayerCommandFailed(
                f"Pairing produced invalid credentials (length {len(credentials)})"
            )
        return credentials

    async def _read_credentials(self, proc: AsyncProcess) -> str | None:
        """Read the pair-setup process stdout until the CREDENTIALS line (or EOF)."""
        buffer = b""
        while chunk := await proc.read(1024):
            buffer += chunk
            while b"\n" in buffer:
                raw_line, buffer = buffer.split(b"\n", 1)
                line = raw_line.decode("utf-8", errors="ignore").strip()
                if line.startswith("CREDENTIALS:"):
                    return line.split(":", 1)[1].strip()
        return None

    def _pair_setup_failure(self, summary: str) -> PlayerCommandFailed:
        """
        Build the pairing failure carrying the most specific error detail available.

        :param summary: Short summary prefixed to the error detail.
        """
        detail = f"{summary}: {self._pair_setup_error()}"
        if translation_key := self._pair_setup_translation_key():
            return PlayerCommandFailed(detail, translation_key=translation_key)
        return PlayerCommandFailed(detail)

    def _pair_setup_translation_key(self) -> str | None:
        """Map the HAP error tag from the pair-setup stderr (if any) to an error translation."""
        for line in self._pair_proc_stderr:
            if match := HAP_ERROR_TAG_RE.search(line):
                tag = int(match.group(1))
                if tag == 2:
                    return "pairing_wrong_pin"
                if tag in (3, 5):
                    # backoff/max tries: the device rate-limits pairing attempts
                    return "pairing_backoff"
        return None

    def _pair_setup_error(self) -> str:
        """Return a short error description from the pair-setup stderr output."""
        # the binary reports failures as plain lines on stderr; the last specific
        # line wins, its generic "Pairing failed." trailer only as a last resort
        fallback = "no error details reported"
        for line in reversed(self._pair_proc_stderr):
            if not line or "Enter the PIN" in line:
                continue
            if line == "Pairing failed.":
                fallback = line
                continue
            # strip the binary's log prefix ("[time] func:line [HAP] message")
            return line.rsplit("] ", 1)[-1]
        return fallback

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
        """
        Compute RAOP SRP premaster secret S.

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
        r"""
        Compute RAOP session key K from premaster secret S.

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
        """
        Compute RAOP SRP M1 proof with padding for A and B (but not g).

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
        """
        Compute RAOP SRP client public key A = g^a mod N.

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
        """
        Complete RAOP pairing for AirPlay 1.

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
        auth_private_key = Ed25519PrivateKey.from_private_bytes(auth_secret)
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

        # Return credentials in raop credentials format: client_id:auth_secret
        return f"{self._client_id.hex()}:{auth_secret.hex()}"
