export class WebSocketManager {
    constructor(config) {
        this.ws = null;
        this.reconnectTimeout = null;
        this.shouldReconnect = false;
        this.isReconnecting = false;
        this.reconnectAttempt = 0;
        this.baseDelayMs = Math.max(0, config?.baseDelayMs ?? 1000);
        this.maxDelayMs = Math.max(this.baseDelayMs, config?.maxDelayMs ?? 15000);
        this.maxAttempts =
            config?.maxAttempts === undefined
                ? Infinity
                : Math.max(0, config.maxAttempts);
        this.onReconnecting = config?.onReconnecting;
        this.onReconnected = config?.onReconnected;
        this.onExhausted = config?.onExhausted;
    }
    /**
     * Adopt an existing WebSocket connection.
     * The caller is responsible for having already opened the socket.
     * Reconnection is disabled for adopted sockets.
     *
     * Returns a Promise that resolves once the adopted socket is open. Throws
     * synchronously if the socket is already CLOSING or CLOSED.
     */
    adopt(ws, onOpen, onMessage, onError, onClose) {
        if (ws.readyState !== WebSocket.OPEN &&
            ws.readyState !== WebSocket.CONNECTING) {
            throw new Error(`Sendspin: Cannot adopt WebSocket in readyState ${ws.readyState} (must be OPEN or CONNECTING)`);
        }
        // Store handlers
        this.onOpenHandler = onOpen;
        this.onMessageHandler = onMessage;
        this.onErrorHandler = onError;
        this.onCloseHandler = onClose;
        // Detach handlers from any existing socket so its async close event
        // cannot fire into the newly-adopted session.
        if (this.ws) {
            const old = this.ws;
            old.onopen = null;
            old.onmessage = null;
            old.onerror = null;
            old.onclose = null;
            old.close();
            this.ws = null;
        }
        this.ws = ws;
        this.ws.binaryType = "arraybuffer";
        // No auto-reconnect for externally-managed sockets
        this.shouldReconnect = false;
        this.clearReconnectState();
        this.ws.onmessage = (event) => {
            if (this.onMessageHandler) {
                this.onMessageHandler(event);
            }
        };
        this.ws.onerror = (error) => {
            console.error("Sendspin: WebSocket error", error);
            if (this.onErrorHandler) {
                this.onErrorHandler(error);
            }
        };
        this.ws.onclose = () => {
            console.log("Sendspin: WebSocket disconnected");
            if (this.onCloseHandler) {
                this.onCloseHandler();
            }
        };
        return new Promise((resolve, reject) => {
            const fireOpen = () => {
                if (this.onOpenHandler) {
                    this.onOpenHandler();
                }
                resolve();
            };
            if (ws.readyState === WebSocket.OPEN) {
                console.log("Sendspin: Adopted open WebSocket");
                fireOpen();
                return;
            }
            // CONNECTING: wait for open or early close.
            const prevOnClose = this.ws.onclose;
            this.ws.onopen = () => {
                console.log("Sendspin: Adopted WebSocket connected");
                fireOpen();
            };
            this.ws.onclose = (event) => {
                if (prevOnClose) {
                    prevOnClose.call(this.ws, event);
                }
                reject(new Error("Sendspin: Adopted WebSocket closed before opening"));
            };
        });
    }
    // Connect to WebSocket server
    async connect(url, onOpen, onMessage, onError, onClose) {
        // Store handlers
        this.onOpenHandler = onOpen;
        this.onMessageHandler = onMessage;
        this.onErrorHandler = onError;
        this.onCloseHandler = onClose;
        // Detach the old socket before replacing it: its async onclose would
        // otherwise re-enter scheduleReconnect once openSocket re-arms retry.
        this.shouldReconnect = false;
        this.clearReconnectState();
        if (this.ws) {
            const old = this.ws;
            old.onopen = null;
            old.onmessage = null;
            old.onerror = null;
            old.onclose = null;
            old.close();
            this.ws = null;
        }
        return this.openSocket(url);
    }
    openSocket(url) {
        return new Promise((resolve, reject) => {
            try {
                console.log("Sendspin: Connecting to", url);
                this.ws = new WebSocket(url);
                this.ws.binaryType = "arraybuffer";
                this.shouldReconnect = true;
                this.ws.onopen = () => {
                    console.log("Sendspin: WebSocket connected");
                    const wasReconnecting = this.isReconnecting;
                    this.isReconnecting = false;
                    this.reconnectAttempt = 0;
                    if (this.onOpenHandler) {
                        this.onOpenHandler();
                    }
                    if (wasReconnecting) {
                        this.onReconnected?.();
                    }
                    resolve();
                };
                this.ws.onmessage = (event) => {
                    if (this.onMessageHandler) {
                        this.onMessageHandler(event);
                    }
                };
                this.ws.onerror = (error) => {
                    console.error("Sendspin: WebSocket error", error);
                    if (this.onErrorHandler) {
                        this.onErrorHandler(error);
                    }
                    reject(error);
                };
                this.ws.onclose = () => {
                    console.log("Sendspin: WebSocket disconnected");
                    if (this.onCloseHandler) {
                        this.onCloseHandler();
                    }
                    // Try to reconnect after a delay if we should reconnect
                    if (this.shouldReconnect) {
                        this.scheduleReconnect(url);
                    }
                };
            }
            catch (error) {
                console.error("Sendspin: Failed to connect", error);
                reject(error);
            }
        });
    }
    getReconnectDelayMs(attempt) {
        const exponential = this.baseDelayMs * 2 ** (attempt - 1);
        return Math.min(exponential, this.maxDelayMs);
    }
    // Schedule reconnection attempt
    scheduleReconnect(url) {
        if (this.reconnectTimeout !== null) {
            clearTimeout(this.reconnectTimeout);
            this.reconnectTimeout = null;
        }
        const attempt = this.reconnectAttempt + 1;
        if (attempt > this.maxAttempts) {
            console.warn(`Sendspin: Reconnect exhausted after ${this.maxAttempts} attempt(s)`);
            this.shouldReconnect = false;
            this.isReconnecting = false;
            this.reconnectAttempt = 0;
            this.onExhausted?.();
            return;
        }
        this.reconnectAttempt = attempt;
        this.isReconnecting = true;
        const delayMs = this.getReconnectDelayMs(attempt);
        this.reconnectTimeout = globalThis.setTimeout(() => {
            this.reconnectTimeout = null;
            if (!this.shouldReconnect) {
                return;
            }
            this.onReconnecting?.(attempt);
            console.log(`Sendspin: Attempting to reconnect (attempt ${attempt}${this.maxAttempts === Infinity ? "" : `/${this.maxAttempts}`})...`);
            this.openSocket(url).catch((error) => {
                console.error("Sendspin: Reconnection failed", error);
            });
        }, delayMs);
    }
    clearReconnectState() {
        if (this.reconnectTimeout !== null) {
            clearTimeout(this.reconnectTimeout);
            this.reconnectTimeout = null;
        }
        this.isReconnecting = false;
        this.reconnectAttempt = 0;
    }
    // Disconnect from WebSocket server
    disconnect() {
        this.shouldReconnect = false;
        this.clearReconnectState();
        if (this.ws) {
            this.ws.close();
            this.ws = null;
        }
    }
    // Send message to server (JSON)
    send(message) {
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            this.ws.send(JSON.stringify(message));
        }
        else {
            console.warn("Sendspin: Cannot send message, WebSocket not connected");
        }
    }
    // Check if WebSocket is connected
    isConnected() {
        return this.ws !== null && this.ws.readyState === WebSocket.OPEN;
    }
    // Get current ready state
    getReadyState() {
        return this.ws ? this.ws.readyState : WebSocket.CLOSED;
    }
}
//# sourceMappingURL=websocket-manager.js.map
