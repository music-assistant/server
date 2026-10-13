import { x25519 } from "../../vendor/noble-curves/ed25519.js";
import { chacha20poly1305 } from "../../vendor/noble-ciphers/chacha.js";
import { gcm } from "../../vendor/noble-ciphers/aes.js";
import { sha256 } from "../../vendor/noble-hashes/sha2.js";
// Shared across calls because every aead* below consumes it synchronously.
const NONCE = new Uint8Array(12);
const NONCE_VIEW = new DataView(NONCE.buffer);
function nonceLE(n) {
    NONCE_VIEW.setBigUint64(4, n, true);
    return NONCE;
}
function nonceBE(n) {
    NONCE_VIEW.setBigUint64(4, n, false);
    return NONCE;
}
const dh = (priv, pub) => x25519.getSharedSecret(priv, pub);
const publicKey = (priv) => x25519.getPublicKey(priv);
const generateKeypair = () => {
    const privateKey = x25519.utils.randomSecretKey();
    return { privateKey, publicKey: x25519.getPublicKey(privateKey) };
};
export const SUITES = {
    chacha: {
        name: "ChaChaPoly",
        dhLen: 32,
        dh,
        generateKeypair,
        publicKey,
        hash: sha256,
        aeadEncrypt: (k, n, ad, pt) => chacha20poly1305(k, nonceLE(n), ad).encrypt(pt),
        aeadDecrypt: (k, n, ad, ct) => chacha20poly1305(k, nonceLE(n), ad).decrypt(ct),
    },
    aesgcm: {
        name: "AESGCM",
        dhLen: 32,
        dh,
        generateKeypair,
        publicKey,
        hash: sha256,
        aeadEncrypt: (k, n, ad, pt) => gcm(k, nonceBE(n), ad).encrypt(pt),
        aeadDecrypt: (k, n, ad, ct) => gcm(k, nonceBE(n), ad).decrypt(ct),
    },
};
/** Maps the config suite id to the wire suite string in client/init. */
export const SUITE_WIRE_NAME = {
    chacha: "25519_ChaChaPoly_SHA256",
    aesgcm: "25519_AESGCM_SHA256",
};
//# sourceMappingURL=suites.js.map
