const EMPTY = new Uint8Array(0);
export class CipherState {
    constructor(suite) {
        this.suite = suite;
        this.k = null;
        this.n = 0n;
    }
    initializeKey(key) {
        this.k = key;
        this.n = 0n;
    }
    hasKey() {
        return this.k !== null;
    }
    encryptWithAd(ad, plaintext) {
        if (this.k === null)
            return plaintext;
        const ct = this.suite.aeadEncrypt(this.k, this.n, ad, plaintext);
        this.n += 1n;
        return ct;
    }
    decryptWithAd(ad, ciphertext) {
        if (this.k === null)
            return ciphertext;
        const pt = this.suite.aeadDecrypt(this.k, this.n, ad, ciphertext);
        this.n += 1n;
        return pt;
    }
}
export { EMPTY };
//# sourceMappingURL=cipher-state.js.map
