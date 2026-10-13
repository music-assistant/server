import { SUITES } from "./suites.js";
import { base64urlEncode, base64urlDecode } from "./base64url.js";
const SK_KEY = "sendspin-identity-sk";
export class Identity {
    constructor(privateKey, publicKey) {
        this.privateKey = privateKey;
        this.publicKey = publicKey;
        this.clientId = base64urlEncode(publicKey);
    }
    get keypair() {
        return { privateKey: this.privateKey, publicKey: this.publicKey };
    }
    static loadOrCreate(storage) {
        const kp = SUITES.chacha; // DH/curve is suite-independent (both 25519)
        if (storage) {
            const stored = storage.getItem(SK_KEY);
            if (stored) {
                try {
                    const sk = base64urlDecode(stored);
                    return new Identity(sk, kp.publicKey(sk));
                }
                catch {
                    // Corrupt persisted key: fail open with a fresh identity (a new
                    // client_id) rather than making the player unconstructable.
                    console.warn("Sendspin: stored identity key is invalid, generating a new one");
                }
            }
            const fresh = kp.generateKeypair();
            storage.setItem(SK_KEY, base64urlEncode(fresh.privateKey));
            return new Identity(fresh.privateKey, fresh.publicKey);
        }
        // No storage: ephemeral keypair, so client_id is unstable and pairing is disabled.
        console.warn("Sendspin: no storage provided, using an ephemeral identity (client_id changes each session, pairing unavailable)");
        const fresh = kp.generateKeypair();
        return new Identity(fresh.privateKey, fresh.publicKey);
    }
}
//# sourceMappingURL=identity.js.map
