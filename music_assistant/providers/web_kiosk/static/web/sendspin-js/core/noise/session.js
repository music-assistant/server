const EMPTY = new Uint8Array(0);
export class NoiseSession {
    constructor(role, split) {
        const [c1, c2] = split;
        // c1: initiator->responder, c2: responder->initiator.
        if (role === "initiator") {
            this.sendCs = c1;
            this.recvCs = c2;
        }
        else {
            this.sendCs = c2;
            this.recvCs = c1;
        }
    }
    encrypt(plaintext) {
        return this.sendCs.encryptWithAd(EMPTY, plaintext);
    }
    decrypt(ciphertext) {
        return this.recvCs.decryptWithAd(EMPTY, ciphertext);
    }
}
//# sourceMappingURL=session.js.map
