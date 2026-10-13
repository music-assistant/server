import { SUITES, SUITE_WIRE_NAME } from "./noise/suites.js";
import { HandshakeState, MSG1, MSG2 } from "./noise/handshake.js";
import { NoiseSession } from "./noise/session.js";
import { base64urlEncode, base64urlDecode } from "./noise/base64url.js";
import { authorizeActivate } from "./activate-authorization.js";
const utf8 = new TextEncoder();
const dutf8 = new TextDecoder();
const HANDSHAKE_TIMEOUT_MS = 30000;
const MAX_TRANSPORT_PLAINTEXT = 65519; // 65535 - 16 (tag); includes the type byte
const MAX_TRANSPORT_CIPHERTEXT = MAX_TRANSPORT_PLAINTEXT + 16; // Noise transport message cap
// Cap total reassembled size so an endless run of fragment-more frames can't exhaust memory.
const MAX_REASSEMBLY_BYTES = 4 * 1024 * 1024;
function concat(a, b) {
    const out = new Uint8Array(a.length + b.length);
    out.set(a, 0);
    out.set(b, a.length);
    return out;
}
export class SendspinTransport {
    constructor(wsManager, deps, cb) {
        this.wsManager = wsManager;
        this.deps = deps;
        this.cb = cb;
        this.state = "idle";
        this.hs = null;
        this.session = null;
        this.matched = null;
        this.serverId = "";
        this.rawClientInit = new Uint8Array(0);
        this.rawServerInit = new Uint8Array(0);
        this.timeout = null;
        this.frag = null;
        this.lastHandshakeHash = new Uint8Array(0);
        this.quiesced = false;
        this.outboundQueue = [];
        this.seenActivate = false;
        this.effectiveActiveRoles = undefined;
    }
    get suite() {
        return SUITES[this.deps.suiteId];
    }
    /** True once transport mode is established. */
    get ready() {
        return this.state === "transport";
    }
    get handshakeInfo() {
        if (!this.matched)
            return null;
        return {
            trustLevel: this.matched.category === "long_term" ? "user" : "none",
            category: this.matched.category,
            serverId: this.serverId,
            entry: this.matched,
        };
    }
    /** The Noise handshake hash h of the current session (PIN pairing binds to it). */
    get handshakeHash() {
        return this.lastHandshakeHash;
    }
    start() {
        // Reset per-connection state so a reconnect does not inherit a stale session.
        this.resetSession();
        const initStr = JSON.stringify({
            type: "client/init",
            payload: {
                client_id: this.deps.identity.clientId,
                version: 1,
                suite: SUITE_WIRE_NAME[this.deps.suiteId],
            },
        });
        this.rawClientInit = utf8.encode(initStr);
        this.wsManager.sendText(initStr);
        this.state = "await_server_init";
        this.armTimeout();
    }
    /** Reset per-connection handshake and session state. */
    resetSession() {
        this.hs = null;
        this.session = null;
        this.matched = null;
        this.frag = null;
        this.serverId = "";
        this.rawServerInit = new Uint8Array(0);
        this.quiesced = false;
        this.outboundQueue = [];
        this.lastHandshakeHash = new Uint8Array(0);
        this.seenActivate = false;
        this.effectiveActiveRoles = undefined;
    }
    /**
     * The socket closed. Drop the handshake timer and session so a reconnect
     * starts clean and no send targets the dead session's keys.
     */
    onSocketClosed() {
        this.clearTimeout();
        this.resetSession();
        this.state = "idle";
    }
    /** Public close: pair/abort and other flows need to tear down the socket. */
    close() {
        this.clearTimeout();
        this.wsManager.disconnect();
    }
    handleRaw(event) {
        if (this.state === "transport") {
            if (typeof event.data === "string")
                return this.fail(); // unexpected cleartext
            let plain;
            try {
                const bytes = new Uint8Array(event.data);
                if (bytes.length > MAX_TRANSPORT_CIPHERTEXT)
                    return this.fail();
                plain = this.session.decrypt(bytes); // AEAD failure is a real transport failure
            }
            catch {
                return this.fail();
            }
            if (plain.length < 1)
                return this.fail();
            // A malformed payload or a throwing app callback must not close the socket
            // or disable reconnect. Log and keep the connection.
            try {
                this.dispatchPlain(plain);
            }
            catch (e) {
                console.warn("Sendspin: dropped malformed transport message", e);
            }
            return;
        }
        try {
            if (typeof event.data !== "string")
                return this.fail(); // handshake is text only
            this.handleHandshakeText(event.data);
        }
        catch {
            this.fail();
        }
    }
    handleHandshakeText(raw) {
        const msg = JSON.parse(raw);
        if (this.state === "await_server_init" && msg.type === "server/init") {
            if (msg.payload.version !== 1)
                return this.fail();
            this.serverId = String(msg.payload.server_id);
            this.rawServerInit = utf8.encode(raw);
            this.hs = new HandshakeState({
                suite: this.suite,
                role: "responder",
                prologue: concat(this.rawClientInit, this.rawServerInit),
                s: this.deps.identity.keypair,
                rs: base64urlDecode(this.serverId),
            });
            this.state = "await_noise1";
            this.armTimeout();
            return;
        }
        if (this.state === "await_noise1" && msg.type === "noise/handshake") {
            this.processNoise1(base64urlDecode(String(msg.payload.data)));
            return;
        }
        this.fail();
    }
    processNoise1(data) {
        const payload1 = this.hs.readMessage(MSG1, data); // static-DH only; throws => fail
        const { psk_id } = JSON.parse(dutf8.decode(payload1));
        const entry = this.deps.pskStore.lookup(psk_id);
        if (!entry)
            return this.fail();
        if (entry.category === "long_term" &&
            entry.serverId !== undefined &&
            entry.serverId !== this.serverId) {
            return this.fail();
        }
        this.hs.setPsk(entry.psk);
        const m2 = this.hs.writeMessage(MSG2, utf8.encode("{}"));
        this.wsManager.sendText(JSON.stringify({
            type: "noise/handshake",
            payload: { data: base64urlEncode(m2) },
        }));
        this.session = new NoiseSession("responder", this.hs.split());
        this.matched = entry;
        this.state = "transport";
        this.clearTimeout();
        this.lastHandshakeHash = this.hs.handshakeHash;
        this.cb.onHandshakeComplete(this.handshakeInfo);
    }
    /** Route one decrypted plaintext frame on its leading message-type byte. */
    dispatchPlain(full) {
        const type = full[0];
        // The body view is built per branch: the binary path below is the hot one
        // and reads only `full`.
        if (type === 0) {
            this.handleControl(JSON.parse(dutf8.decode(full.subarray(1))));
        }
        else if (type === 2 || type === 3) {
            this.handleFragment(type, full.subarray(1));
        }
        else {
            // full is the decrypt output: exclusively owned, offset 0, exact length,
            // type byte intact. Hand it over without re-copying.
            this.cb.onBinaryMessage(full);
        }
    }
    handleFragment(type, body) {
        if (type === 2 && this.frag === null) {
            if (body.length < 1)
                return this.fail();
            // Reject a fragment whose inner type is itself a fragment marker.
            if (body[0] === 2 || body[0] === 3)
                return this.fail();
            const first = body.subarray(1);
            this.frag = { origType: body[0], parts: [first], size: first.length };
            return;
        }
        if (type === 2) {
            this.frag.parts.push(body);
            this.frag.size += body.length;
            if (this.frag.size > MAX_REASSEMBLY_BYTES) {
                this.frag = null;
                return this.fail();
            }
            return;
        }
        // type === 3: closing frame
        if (this.frag === null)
            return this.fail();
        this.frag.parts.push(body);
        this.frag.size += body.length;
        if (this.frag.size > MAX_REASSEMBLY_BYTES) {
            this.frag = null;
            return this.fail();
        }
        const origType = this.frag.origType;
        // Assemble into a size+1 buffer with the type byte at offset 0, so the
        // binary path can hand it over without a second copy to prepend the type.
        const assembled = new Uint8Array(this.frag.size + 1);
        assembled[0] = origType;
        let off = 1;
        for (const p of this.frag.parts) {
            assembled.set(p, off);
            off += p.length;
        }
        this.frag = null;
        if (origType === 0) {
            this.handleControl(JSON.parse(dutf8.decode(assembled.subarray(1))));
        }
        else {
            this.cb.onBinaryMessage(assembled);
        }
    }
    handleControl(msg) {
        if (msg.type === "noise/handshake") {
            this.handleRehandshake(msg);
            return;
        }
        if (msg.type === "server/activate") {
            this.handleActivate(msg);
            return;
        }
        if (msg.type === "server/unpair") {
            this.handleUnpair();
            return;
        }
        this.cb.onControlMessage(msg);
    }
    handleRehandshake(msg) {
        const newHs = new HandshakeState({
            suite: this.suite,
            role: "responder",
            prologue: this.lastHandshakeHash,
            s: this.deps.identity.keypair,
            rs: base64urlDecode(this.serverId),
        });
        const payload1 = newHs.readMessage(MSG1, base64urlDecode(msg.payload.data));
        const { psk_id } = JSON.parse(dutf8.decode(payload1));
        const entry = this.deps.pskStore.lookup(psk_id);
        if (!entry)
            return this.fail();
        if (entry.category === "long_term" &&
            entry.serverId !== undefined &&
            entry.serverId !== this.serverId) {
            return this.fail();
        }
        newHs.setPsk(entry.psk);
        const m2 = newHs.writeMessage(MSG2, utf8.encode("{}"));
        // Hold periodic outbound traffic until the post-re-handshake server/activate.
        this.quiesced = true;
        this.armTimeout();
        // Send msg 2 under the CURRENT keys, then swap.
        this.encryptSend({
            type: "noise/handshake",
            payload: { data: base64urlEncode(m2) },
        });
        this.session = new NoiseSession("responder", newHs.split());
        this.matched = entry;
        this.lastHandshakeHash = newHs.handshakeHash;
        // The re-handshake re-runs the activate sequence, so the next activate is a fresh first.
        this.seenActivate = false;
        this.effectiveActiveRoles = undefined;
        this.cb.onHandshakeComplete(this.handshakeInfo);
    }
    handleActivate(msg) {
        const payloadRoles = msg.payload.active_roles;
        // active_roles is required on the first activate and persists when later ones omit it.
        if (!this.seenActivate && payloadRoles === undefined) {
            this.sendGoodbyeAndClose("unauthorized");
            return;
        }
        if (payloadRoles !== undefined)
            this.effectiveActiveRoles = payloadRoles;
        this.seenActivate = true;
        const result = authorizeActivate(this.matched.category, msg.payload.activities, this.effectiveActiveRoles, this.deps.unpairedAccess);
        if (!result.ok) {
            this.sendGoodbyeAndClose(result.goodbye);
            return;
        }
        this.clearTimeout();
        const queuedCommands = !msg.payload.activities.includes("pairing") &&
            this.effectiveActiveRoles?.includes("controller@v1")
            ? this.outboundQueue.filter((queued) => queued.type === "client/command")
            : [];
        this.quiesced = false;
        this.outboundQueue = [];
        this.cb.onControlMessage(msg);
        for (const command of queuedCommands)
            this.encryptSend(command);
    }
    handleUnpair() {
        // trust_level none (Sentinel or in-flight pairing): ignore.
        if (this.matched?.category !== "long_term")
            return;
        this.deps.pskStore.removeByPskId(this.matched.pskId);
        this.sendGoodbyeAndClose("unpaired");
    }
    sendGoodbyeAndClose(reason) {
        try {
            this.encryptSend({ type: "client/goodbye", payload: { reason } });
        }
        catch {
            /* best effort */
        }
        this.close();
    }
    sendControl(msg) {
        if (this.state !== "transport" || !this.session) {
            console.warn("Sendspin: sendControl before transport ready");
            return;
        }
        const type = msg.type;
        if (this.quiesced &&
            type !== undefined &&
            SendspinTransport.QUIESCED_TYPES.has(type)) {
            this.outboundQueue.push(msg);
            return;
        }
        this.encryptSend(msg);
    }
    encryptSend(msg) {
        const json = utf8.encode(JSON.stringify(msg));
        const pt = concat(Uint8Array.of(0), json);
        if (pt.length > MAX_TRANSPORT_PLAINTEXT) {
            throw new Error("Sendspin: control message exceeds single-frame limit");
        }
        this.wsManager.sendBinary(this.session.encrypt(pt));
    }
    armTimeout() {
        this.clearTimeout();
        this.timeout = globalThis.setTimeout(() => this.fail(), HANDSHAKE_TIMEOUT_MS);
    }
    clearTimeout() {
        if (this.timeout !== null) {
            clearTimeout(this.timeout);
            this.timeout = null;
        }
    }
    /** Any handshake or transport failure: close the socket with no app-level error. */
    fail() {
        this.clearTimeout();
        this.wsManager.disconnect();
    }
}
// Hold normal traffic during a re-handshake. client/hello must still flow or
// the post-re-handshake server/activate would deadlock.
SendspinTransport.QUIESCED_TYPES = new Set([
    "client/command",
    "client/time",
    "client/state",
]);
//# sourceMappingURL=transport.js.map
