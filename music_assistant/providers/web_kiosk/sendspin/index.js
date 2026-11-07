import { loadSendspinClientIdentity } from '@sendspin/sendspin-js';
import { x25519 } from '@noble/curves/ed25519';
import { hmac } from '@noble/hashes/hmac';
import { sha256 } from '@noble/hashes/sha256';
import { bytesToHex, utf8ToBytes } from '@noble/hashes/utils';

export { SendspinPlayer, loadSendspinClientIdentity } from '@sendspin/sendspin-js';

function decode(value) {
    const raw = atob(value.replace(/-/g, '+').replace(/_/g, '/'));
    return Uint8Array.from(raw, c => c.charCodeAt(0));
}
function encode(value) {
    return btoa(String.fromCharCode(...value)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

export function createSendspinClaimProof(storage, challenge) {
    let secret = null;
    function observe(value) {
        if (typeof value !== 'string') return;
        try {
            const candidate = decode(value);
            if (candidate.length === 32 && encode(x25519.getPublicKey(candidate)) === challenge.client_id) secret = candidate;
        } catch { /* SDK storage also contains non-key values. */ }
    }
    // Observe the identity through the SDK's public storage interface. No SDK
    // internals or persisted key names are assumed; match the actual public key.
    const identity = loadSendspinClientIdentity({
        getItem(key) { const value = storage.getItem(key); observe(value); return value; },
        setItem(key, value) { observe(value); storage.setItem(key, value); },
        removeItem(key) { storage.removeItem(key); },
    });
    if (identity.clientId !== challenge.client_id || !secret) throw new Error('Sendspin identity changed');
    const shared = x25519.getSharedSecret(secret, decode(challenge.server_key));
    return bytesToHex(hmac(sha256, shared, utf8ToBytes(challenge.message)));
}
