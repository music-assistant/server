/**
 * Persists the server-commanded static delay so it survives reboots and
 * reconnections, as the spec requires.
 */
import { clampSyncDelayMs } from "../sync-delay.js";
const STATIC_DELAY_STORAGE_KEY = "sendspin-static-delay-ms";
export class StaticDelayStore {
    constructor(storage) {
        this.storage = storage;
    }
    load() {
        if (!this.storage)
            return null;
        try {
            const stored = this.storage.getItem(STATIC_DELAY_STORAGE_KEY);
            if (stored === null)
                return null;
            const value = parseFloat(stored);
            if (isNaN(value))
                return null;
            return clampSyncDelayMs(value);
        }
        catch {
            return null;
        }
    }
    save(delayMs) {
        if (!this.storage)
            return;
        try {
            this.storage.setItem(STATIC_DELAY_STORAGE_KEY, delayMs.toString());
        }
        catch {
            // ignore
        }
    }
}
//# sourceMappingURL=static-delay-store.js.map
