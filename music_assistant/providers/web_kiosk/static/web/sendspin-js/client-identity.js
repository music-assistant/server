import { Identity } from "./core/noise/identity.js";
import { PskStore } from "./core/noise/psk.js";
import { base64urlEncode } from "./core/noise/base64url.js";
import { encodePairingToken } from "./core/noise/pairing-token.js";
/**
 * Read the persisted client identity, creating it if absent.
 *
 * Apps that key their own state on the client id need it before a player
 * exists. A SendspinPlayer built afterwards with the same storage adopts this
 * identity rather than minting another.
 */
export function loadSendspinClientIdentity(storage) {
    let resolved = null;
    if (storage !== undefined) {
        resolved = storage;
    }
    else if (typeof localStorage !== "undefined") {
        resolved = localStorage;
    }
    const identity = Identity.loadOrCreate(resolved);
    const pairingPsk = resolved
        ? base64urlEncode(new PskStore(resolved).getOrCreatePairingPsk())
        : null;
    return {
        clientId: identity.clientId,
        pairingPsk,
        pairingToken: pairingPsk
            ? encodePairingToken(identity.clientId, pairingPsk)
            : null,
    };
}
//# sourceMappingURL=client-identity.js.map
