// CPACE-X25519-SHA512 (draft-irtf-cfrg-cpace-21) in initiator-responder mode
// with the explicit mutual-confirmation flow (MCF) of §10.4. The Sendspin
// server is role A (initiator) and the client is role B (responder).
import { x25519 } from "../../vendor/noble-curves/ed25519.js";
import { sha512 } from "../../vendor/noble-hashes/sha2.js";
import { hmac } from "../../vendor/noble-hashes/hmac.js";
// Curve25519 field and Elligator2 parameters (draft G_X25519).
const Q = 2n ** 255n - 19n;
const A = 486662n;
const Z = 2n; // the non-square used by Elligator2 on Curve25519
const FIELD_BYTES = 32;
export const SHARE_SIZE = 32;
export const TAG_SIZE = 64;
const DSI = utf8("CPace255");
const DSI_ISK = utf8("CPace255_ISK");
const MAC_LABEL = utf8("CPaceMac");
const SHA512_BLOCK_BYTES = 128;
export class CPaceError extends Error {
}
function utf8(s) {
    return new TextEncoder().encode(s);
}
function concat(...parts) {
    const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
    let off = 0;
    for (const p of parts) {
        out.set(p, off);
        off += p.length;
    }
    return out;
}
/** LEB128 length prefix per the CPace draft's prepend_len. */
export function prependLen(data) {
    let length = data.length;
    const prefix = [];
    for (;;) {
        prefix.push(length < 128 ? length : (length & 0x7f) | 0x80);
        length >>= 7;
        if (length === 0)
            break;
    }
    return concat(new Uint8Array(prefix), data);
}
export function lvCat(...parts) {
    return concat(...parts.map(prependLen));
}
export function generatorString(prs, ci, sid) {
    const lenZpad = Math.max(0, SHA512_BLOCK_BYTES - 1 - prependLen(prs).length - prependLen(DSI).length);
    return lvCat(DSI, prs, new Uint8Array(lenZpad), ci, sid);
}
function mod(n, m) {
    const r = n % m;
    return r < 0n ? r + m : r;
}
function modPow(base, exp, m) {
    let result = 1n;
    let b = mod(base, m);
    let e = exp;
    while (e > 0n) {
        if (e & 1n)
            result = (result * b) % m;
        b = (b * b) % m;
        e >>= 1n;
    }
    return result;
}
function modInv(n, m) {
    return modPow(n, m - 2n, m); // m prime
}
function decodeU(value) {
    const u = value.slice();
    u[u.length - 1] &= 0x7f; // 255-bit field: ignore the unused top bit (RFC 7748)
    let n = 0n;
    for (let i = u.length - 1; i >= 0; i--)
        n = (n << 8n) | BigInt(u[i]);
    return n;
}
function encodeU(x) {
    const out = new Uint8Array(FIELD_BYTES);
    let n = x;
    for (let i = 0; i < FIELD_BYTES; i++) {
        out[i] = Number(n & 0xffn);
        n >>= 8n;
    }
    return out;
}
/** Elligator2 map onto Curve25519 (B = 1), returning the u-coordinate bytes. */
export function elligator2(r) {
    const rq = mod(r, Q);
    const v = mod(-A * modInv(mod(1n + Z * rq * rq, Q), Q), Q);
    const eps = modPow(mod(v * v * v + A * v * v + v, Q), (Q - 1n) / 2n, Q);
    const x = mod(eps * v - mod(1n - eps, Q) * A * modInv(2n, Q), Q);
    return encodeU(x);
}
export function calculateGenerator(prs, ci, sid) {
    const genHash = sha512(generatorString(prs, ci, sid)).slice(0, FIELD_BYTES);
    return elligator2(decodeU(genHash));
}
/** X25519 scalar mult that rejects a result encoding the identity (low order). */
function scalarMultVfy(scalar, point, what) {
    let shared;
    try {
        shared = x25519.scalarMult(scalar, point);
    }
    catch {
        throw new CPaceError(`${what} encodes a low-order point`);
    }
    if (shared.every((b) => b === 0)) {
        throw new CPaceError(`${what} encodes a low-order point`);
    }
    return shared;
}
function constantTimeEqual(a, b) {
    if (a.length !== b.length)
        return false;
    let diff = 0;
    for (let i = 0; i < a.length; i++)
        diff |= a[i] ^ b[i];
    return diff === 0;
}
/**
 * One side of a CPACE-X25519-SHA512 exchange with mutual confirmation.
 * Sendspin uses empty CI, ADa = "server", ADb = "client", and
 * sid = "sendspin-pair-pake-v1" || h || counter.
 */
export class CPace {
    constructor(role, scalar, sid, ada, adb, generator) {
        this.role = role;
        this.sid = sid;
        this.ada = ada;
        this.adb = adb;
        this.macKey = null;
        this.initiatorShare = null;
        this.responderShare = null;
        this.iskValue = null;
        this.scalar = scalar;
        this.publicShare = scalarMultVfy(scalar, generator, "generator");
    }
    /** Begin a CPace run, sampling a scalar and computing the public share. */
    static start(opts) {
        const scalar = opts.scalar ?? crypto.getRandomValues(new Uint8Array(FIELD_BYTES));
        const generator = calculateGenerator(opts.prs, opts.ci ?? new Uint8Array(0), opts.sid);
        return new CPace(opts.role, scalar, opts.sid, opts.ada ?? new Uint8Array(0), opts.adb ?? new Uint8Array(0), generator);
    }
    /** Ingest the peer's public share, deriving the confirmation MAC key. */
    derive(peerShare) {
        if (!this.scalar) {
            throw new CPaceError("derive() may only be called once");
        }
        const scalar = this.scalar;
        this.scalar = null;
        if (peerShare.length !== SHARE_SIZE) {
            throw new CPaceError(`peer share must be ${SHARE_SIZE} bytes, got ${peerShare.length}`);
        }
        const shared = scalarMultVfy(scalar, peerShare, "peer share");
        if (this.role === "initiator") {
            this.initiatorShare = this.publicShare;
            this.responderShare = peerShare;
        }
        else {
            this.initiatorShare = peerShare;
            this.responderShare = this.publicShare;
        }
        const transcript = concat(lvCat(this.initiatorShare, this.ada), lvCat(this.responderShare, this.adb));
        this.iskValue = sha512(concat(lvCat(DSI_ISK, this.sid, shared), transcript));
        this.macKey = sha512(concat(MAC_LABEL, this.sid, this.iskValue));
    }
    /** The 64-byte CPace intermediate session key (ISK). derive() must run first. */
    get isk() {
        if (!this.iskValue) {
            throw new CPaceError("derive() must be called before reading the ISK");
        }
        return this.iskValue;
    }
    /** This side's confirmation tag (Ta for the initiator, Tb for the responder). */
    tag() {
        return this.mac(true);
    }
    /** Whether peerTag matches the peer's expected confirmation tag. */
    verify(peerTag) {
        const expected = this.mac(false);
        // With the same (Y, AD) on both sides the tag this side would accept is the
        // tag it published, which an attacker can echo back without knowing the PRS.
        if (constantTimeEqual(expected, this.mac(true)))
            return false;
        return constantTimeEqual(peerTag, expected);
    }
    mac(own) {
        if (!this.macKey || !this.initiatorShare || !this.responderShare) {
            throw new CPaceError("derive() must be called before confirmation tags");
        }
        // Ta authenticates (Ya, ADa) and Tb authenticates (Yb, ADb).
        const useInitiator = own === (this.role === "initiator");
        const share = useInitiator ? this.initiatorShare : this.responderShare;
        const ad = useInitiator ? this.ada : this.adb;
        return hmac(sha512, this.macKey, lvCat(share, ad));
    }
}
//# sourceMappingURL=cpace.js.map
