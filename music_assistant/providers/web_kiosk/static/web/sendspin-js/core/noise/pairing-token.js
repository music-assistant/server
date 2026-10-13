import { base64urlDecode } from "./base64url.js";
const BASE32_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
const KEY_SIZE = 32;
function encodeBase32(bytes) {
    let bits = 0;
    let value = 0;
    let encoded = "";
    for (const byte of bytes) {
        value = (value << 8) | byte;
        bits += 8;
        while (bits >= 5) {
            encoded += BASE32_ALPHABET[(value >>> (bits - 5)) & 31];
            bits -= 5;
        }
        value &= (1 << bits) - 1;
    }
    if (bits > 0) {
        encoded += BASE32_ALPHABET[(value << (5 - bits)) & 31];
    }
    return encoded;
}
export function encodePairingToken(clientId, pairingPsk) {
    const clientKey = base64urlDecode(clientId);
    const psk = base64urlDecode(pairingPsk);
    if (clientKey.length !== KEY_SIZE) {
        throw new Error(`clientId must decode to ${KEY_SIZE} bytes`);
    }
    if (psk.length !== KEY_SIZE) {
        throw new Error(`pairingPsk must decode to ${KEY_SIZE} bytes`);
    }
    const payload = new Uint8Array(clientKey.length + psk.length);
    payload.set(clientKey);
    payload.set(psk, clientKey.length);
    const body = encodeBase32(payload).replace(/2/g, "9");
    return `SP:0${body}`;
}
//# sourceMappingURL=pairing-token.js.map
