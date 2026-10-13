// Dynamic-PIN derivation and commitment (Sendspin pairing spec).
import { sha256 } from "../../vendor/noble-hashes/sha2.js";
const PIN_DERIVE_LABEL = new TextEncoder().encode("sendspin-pin-derive-v1");
const PAIR_COMMIT_LABEL = new TextEncoder().encode("sendspin-pair-commit-v1");
export const NONCE_SIZE = 32;
export const MIN_PIN_DIGITS = 4;
export const MAX_PIN_DIGITS = 12;
export const DEFAULT_MIN_PIN_DIGITS = 6;
export const STATIC_PIN_DIGITS = 8;
const STATIC_PIN_RE = new RegExp(`^[0-9]{${STATIC_PIN_DIGITS}}$`);
/** Whether pin is exactly 8 decimal digits, as the static-PIN method requires. */
export function isValidStaticPin(pin) {
    return STATIC_PIN_RE.test(pin);
}
/** Fresh 32-byte CSPRNG nonce (nonce_A or nonce_B). */
export function generateNonce() {
    return crypto.getRandomValues(new Uint8Array(NONCE_SIZE));
}
/** SHA-256("sendspin-pair-commit-v1" || nonce), the commitment commit_B to nonce_B. */
export function commitNonce(nonce) {
    const input = new Uint8Array(PAIR_COMMIT_LABEL.length + nonce.length);
    input.set(PAIR_COMMIT_LABEL, 0);
    input.set(nonce, PAIR_COMMIT_LABEL.length);
    return sha256(input);
}
/** Derive the pinLength-digit dynamic PIN from the handshake hash and both nonces. */
export function derivePin(handshakeHash, nonceA, nonceB, pinLength) {
    const input = new Uint8Array(PIN_DERIVE_LABEL.length +
        handshakeHash.length +
        nonceA.length +
        nonceB.length);
    input.set(PIN_DERIVE_LABEL, 0);
    input.set(handshakeHash, PIN_DERIVE_LABEL.length);
    input.set(nonceA, PIN_DERIVE_LABEL.length + handshakeHash.length);
    input.set(nonceB, PIN_DERIVE_LABEL.length + handshakeHash.length + nonceA.length);
    const digest = sha256(input);
    let n = 0n;
    for (const b of digest)
        n = (n << 8n) | BigInt(b);
    const pin = n % 10n ** BigInt(pinLength);
    return pin.toString().padStart(pinLength, "0");
}
//# sourceMappingURL=pin.js.map
