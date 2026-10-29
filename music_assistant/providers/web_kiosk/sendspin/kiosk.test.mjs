import assert from 'node:assert/strict';
import { webcrypto, generateKeyPairSync, createPublicKey, diffieHellman, createHmac } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import { createContext, runInContext } from 'node:vm';

const source = await readFile(new URL('../../provider/static/web/web.js', import.meta.url), 'utf8');
const sdkSource = await readFile(new URL('../../provider/static/web/sendspin-js/index.js', import.meta.url), 'utf8');
const html = await readFile(new URL('../../provider/static/web/index.html', import.meta.url), 'utf8');
const elementIds = new Set([...html.matchAll(/\bid="([^"]+)"/g)].map(match => match[1]));

test('group Sendspin playback shows SDK metadata without a native kiosk play message', async () => {
    const result = await startKiosk('?controls=1', [], undefined, {}, { fakeSdk:true });
    result.socketInstances[0].onmessage({ data:JSON.stringify({ type:'welcome', player_id:'wk-group' }) });
    await new Promise(resolve => setImmediate(resolve));
    const player = result.context.__sendspinPlayer;
    player.core.onStreamStart();
    player.config.onStateChange({
        isPlaying:true, volume:65, serverState:{ metadata:{ title:'Group track', artist:'Group artist', artwork_url:'http://ma.local/cover', progress:{ track_duration:180000, track_progress:25000, playback_speed:1 } } },
        groupState:{ playback_state:'playing' },
    });
    assert.equal(result.elements.get('kiosk-title').textContent, 'Group track');
    assert.equal(result.elements.get('kiosk-artist').textContent, 'Group artist');
    assert.equal(result.elements.get('k-play').getAttribute('aria-label'), 'Pause');
    assert.equal(result.elements.get('kiosk-art').src, 'http://ma.local/cover');
});

test('Sendspin format updates do not restart the initial-audio fallback timer', async () => {
    const result = await startKiosk('', [], undefined, {}, { fakeSdk:true });
    result.socketInstances[0].onmessage({ data:JSON.stringify({ type:'welcome', player_id:'wk-format' }) });
    await new Promise(resolve => setImmediate(resolve));
    const player = result.context.__sendspinPlayer;
    player.core.onStreamStart({}, false);
    player.core.onAudioData({});
    player.core.onStreamStart({}, true);
    result.advance(11000);
    result.intervals.filter(interval => interval.delay === 1000).forEach(interval => interval.callback());
    assert.equal(player.disconnected, undefined);
    assert.equal(result.context.__sendspinPlayer, player);
});

test('group pause retains metadata and group stop clears it after the audio stream ends', async () => {
    const result = await startKiosk('?controls=1', [], undefined, {}, { fakeSdk:true });
    result.socketInstances[0].onmessage({ data:JSON.stringify({ type:'welcome', player_id:'wk-group' }) });
    await new Promise(resolve => setImmediate(resolve));
    const player = result.context.__sendspinPlayer;
    const metadata = { title:'Group track', artist:'Group artist' };
    player.core.onStreamStart({}, false);
    player.config.onStateChange({ isPlaying:true, serverState:{metadata} });
    player.config.onStateChange({ isPlaying:false, serverState:{metadata} });
    assert.equal(result.elements.get('k-play').getAttribute('aria-label'), 'Play');
    assert.equal(result.elements.get('kiosk-title').textContent, 'Group track');
    player.core.onStreamEnd();
    player.config.onStateChange({ isPlaying:false, serverState:{} });
    assert.equal(result.elements.get('kiosk-title').textContent, 'wk-group');
    assert.equal(result.elements.get('kiosk-art').src, '/web/sendspin.svg');
});

async function startKiosk(search, entries = [], randomUUID, partyState = {}, options = {}) {
    const storage = new Map(entries);
    const frames = [];
    let now = 1000;
    const sockets = [];
    const socketInstances = [];
    const apiCalls = [];
    const intervals = [];
    const documentEvents = new Map();
    const windowEvents = new Map();
    const timers = [];
    const wakeRequests = [];
    const fetchCalls = [];
    const audioInstances = [];
    const fullscreenCalls = [];
    let sdkLoads = 0;
    let activeElement = null;
    const node = () => {
        const classes = new Set();
        return {
            classList: {
                add(...names) { names.forEach(name => classes.add(name)); },
                remove(...names) { names.forEach(name => classes.delete(name)); },
                toggle(name, force) {
                    if (force ?? !classes.has(name)) classes.add(name);
                    else classes.delete(name);
                },
                contains(name) { return classes.has(name); },
            },
            style: { setProperty(name, value) { this[name] = value; } },
            dataset: {}, attributes: new Map(), textContent: '', value: '', children: [],
            appendChild(child) { child.offsetTop = this.children.length * 80; child.offsetHeight = 64; this.children.push(child); return child; },
            clientHeight: 400,
            replaceChildren(...children) { this.children = children; this._markup = ''; this.textContent = ''; },
            removeChild(child) { this.children.splice(this.children.indexOf(child), 1); },
            get firstChild() { return this.children[0] || null; },
            get childElementCount() { return this.children.length; },
            set innerHTML(value) { this._markup = value; this.children = []; },
            get innerHTML() { return this._markup || this.textContent.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); },
            addEventListener() {}, removeEventListener() {}, focus() { activeElement = this; },
            click() { if (this.onclick) this.onclick(); },
            getClientRects() { return this.classList.contains('hidden') ? [] : [this.getBoundingClientRect()]; },
            getBoundingClientRect() { return this.rect || { left:0, top:0, width:40, height:40 }; },
            setAttribute(name, value) { this.attributes.set(name, String(value)); },
            setAttributeNS(_namespace, name, value) { this.attributes.set(name, String(value)); },
            removeAttribute(name) { this.attributes.delete(name); if (name === 'src') delete this.src; },
            getAttribute(name) { return this.attributes.get(name) ?? this[name] ?? null; },
            getContext() {
                const element = this;
                return { clearRect() { element.clears = (element.clears || 0) + 1; }, fillRect() {} };
            },
        };
    };
    const elements = new Map();
    const config = options;
    const context = createContext({
        URL, URLSearchParams, Blob, TextEncoder, TextDecoder, atob, btoa, console,
        innerWidth: options.width || 390,
        CSS: { supports:() => !options.noGrid },
        location: { search, hash: options.hash || '', pathname: '/kiosk', protocol: options.protocol || 'http:', hostname:'192.0.2.10', host: '192.0.2.10:8098' },
        isSecureContext: options.secure === true,
        AudioContext: function () {},
        AudioDecoder: options.opusSupported !== undefined ? { isConfigSupported: async () => ({ supported:options.opusSupported }) } : undefined,
        OfflineAudioContext: class {
            decodeAudioData() { return options.flacSupported ? Promise.resolve({ length:960 }) : Promise.reject(new Error('FLAC unavailable')); }
        },
        navigator: { userAgent:'Kiosk Chrome/108', platform:'', maxTouchPoints:0, onLine:true,
            wakeLock: options.wakeLock ? { request: async type => {
                wakeRequests.push(type);
                if (options.wakeLock === 'reject') throw new Error('Wake lock denied');
                return { released:false, addEventListener() {}, release:async () => {} };
            } } : undefined },
        history: { replaceState(_state, _title, url) { const target = new URL(url, 'http://192.0.2.10:8098'); context.location.search = target.search; context.location.hash = target.hash; } },
        crypto: options.noCrypto ? undefined : { getRandomValues: webcrypto.getRandomValues.bind(webcrypto), randomUUID },
        localStorage: {
            getItem(key) { if (options.storageDenied) throw new Error('Storage denied'); return storage.get(key) ?? null; },
            setItem(key, value) { if (options.storageDenied) throw new Error('Storage denied'); storage.set(key, value); },
        },
        document: {
            documentElement: node(), body: node(),
            hidden:false,
            get activeElement() { return activeElement; },
            head: { appendChild(script) {
                sdkLoads++;
                try { runInContext(options.fakeSdk ? fakeSdk : sdkSource, context); script.onload(); }
                catch (error) { script.onerror(error); }
            } },
            getElementById(id) {
                if (!elementIds.has(id)) return null;
                if (!elements.has(id)) {
                    const element = node();
                    const tag = html.match(new RegExp(`<[^>]*\\bid="${id}"[^>]*>`))?.[0] || '';
                    const classes = tag.match(/\bclass="([^"]*)"/)?.[1] || '';
                    element.classList.add(...classes.split(/\s+/).filter(Boolean));
                    element.tagName = tag.match(/^<(\w+)/)?.[1].toUpperCase() || 'DIV';
                    element.id = id;
                    if (options.noReplaceChildren) element.replaceChildren = undefined;
                    elements.set(id, element);
                }
                return elements.get(id);
            },
            createElement: node,
            addEventListener(type, callback) {
                if (!documentEvents.has(type)) documentEvents.set(type, []);
                documentEvents.get(type).push(callback);
            },
            removeEventListener() {},
        },
        confirm: () => false,
        Audio: class {
            constructor() { audioInstances.push(this); }
            paused = true;
            _volume = 1;
            get volume() { return this._volume; }
            set volume(value) { if (!options.systemVolume) this._volume = value; }
            addEventListener() {}
            play() {
                if (options.playError?.()) return Promise.reject(options.playError());
                if (options.playAllowed?.()) { this.paused = false; return Promise.resolve(); }
                return Promise.reject(Object.assign(new Error('Gesture required'), { name: 'NotAllowedError' }));
            }
            pause() { this.paused = true; }
            removeAttribute(name) { delete this[name]; }
            getAttribute(name) { return this[name] ?? null; }
        },
        WebSocket: class {
            static OPEN = 1;
            readyState = 1;
            sent = [];
            constructor(url) { sockets.push(url); socketInstances.push(this); }
            send(data) { this.sent.push(JSON.parse(data)); }
            close() { this.readyState = 3; if(this.onclose) this.onclose(); }
        },
        fetch: async (url, options) => {
            fetchCalls.push(url);
            if (url === '/api/party') return { ok: partyState.ok !== false, json: async () => partyState.info || {} };
            if (url === '/web/flac-probe.flac') return { ok:true, arrayBuffer:async () => new ArrayBuffer(8) };
            if (url === '/api' && options?.body) {
                const request = JSON.parse(options.body);
                apiCalls.push(request);
                if (config.api) return { ok: true, json: async () => config.api(request) };
            }
            return { ok: true, json: async () => ({}) };
        },
        setInterval(callback, delay) { intervals.push({ callback, delay }); }, setTimeout(callback, delay) { timers.push({callback,delay}); return timers.length; }, clearTimeout() {},
        requestAnimationFrame(callback) { frames.push(callback); return frames.length; }, cancelAnimationFrame() {},
        performance: { now: () => now },
    });
    context.window = context;
    if (options.fullscreen) {
        const webkit = options.fullscreen === 'webkit';
        const change = webkit ? 'webkitfullscreenchange' : 'fullscreenchange';
        const element = webkit ? 'webkitFullscreenElement' : 'fullscreenElement';
        context.document[webkit ? 'webkitFullscreenEnabled' : 'fullscreenEnabled'] = true;
        context.document.documentElement[webkit ? 'webkitRequestFullscreen' : 'requestFullscreen'] = () => {
            fullscreenCalls.push('enter');
            if (options.fullscreen === 'reject') return Promise.reject(new Error('Fullscreen denied'));
            context.document[element] = context.document.documentElement;
            (documentEvents.get(change) || []).forEach(callback => callback());
            return webkit ? undefined : Promise.resolve();
        };
        context.document[webkit ? 'webkitExitFullscreen' : 'exitFullscreen'] = () => {
            fullscreenCalls.push('exit');
            context.document[element] = null;
            (documentEvents.get(change) || []).forEach(callback => callback());
            return webkit ? undefined : Promise.resolve();
        };
    }
    context.addEventListener = (type, callback) => {
        if (!windowEvents.has(type)) windowEvents.set(type, []);
        windowEvents.get(type).push(callback);
    };
    const fakeSdk = `
        window.WebKioskSendspin = {
        loadSendspinClientIdentity: function(storage) {
            var id = storage && storage.getItem('fake-identity');
            if (!id) { id = 'hSDwCYkwp1R0i33ctD73Wg2_Og0mOBr066SpjqqbTmo'; if(storage) storage.setItem('fake-identity', id); }
            return { clientId:id };
        },
        SendspinPlayer: class {
            constructor(config) { window.__sdkPlayers = (window.__sdkPlayers || 0) + 1; this.config = config; this.core = {}; this.clientId = config.storage && config.storage.getItem('fake-identity') || 'b'.repeat(43); }
            get trackProgress() { return this.core.trackProgress; }
            unlock() { this.unlocks = (this.unlocks || 0) + 1; return Promise.resolve(); }
            connect() { this.isConnected = true; return Promise.resolve(); }
            disconnect() { this.disconnected = true; }
            setVolume(value) { this.volume = value; }
            setMuted(value) { this.muted = value; }
        }};
    `;
    let startupError;
    try {
        runInContext(source, context);
    } catch (error) {
        startupError = error;
    }
    await new Promise(resolve => setImmediate(resolve));
    return { sockets, socketInstances, storage, startupError, elements, apiCalls, intervals, documentEvents, windowEvents, timers, wakeRequests, fullscreenCalls, fetchCalls, sdkLoads, root: context.document.documentElement, location: context.location, context, frames, audioInstances, advance: ms => { now += ms; }, resize: width => { context.innerWidth = width; (windowEvents.get('resize') || []).forEach(callback => callback()); } };
}

for (const api of ['standard', 'webkit']) {
    test(`optional fullscreen enters, exits and follows browser Escape (${api})`, async () => {
        const result = await startKiosk('?sendspin=0&fullscreen=1', [], undefined, {}, { fullscreen:api });
        const button = result.context.document.getElementById('k-fullscreen');
        assert.ok(button);
        assert.equal(button.classList.contains('hidden'), false);
        assert.equal(button.getAttribute('aria-label'), 'Enter fullscreen');
        button.click();
        await new Promise(resolve => setImmediate(resolve));
        assert.deepEqual(result.fullscreenCalls, ['enter']);
        assert.equal(button.getAttribute('aria-label'), 'Exit fullscreen');
        button.click();
        await new Promise(resolve => setImmediate(resolve));
        assert.deepEqual(result.fullscreenCalls, ['enter', 'exit']);
        assert.equal(button.getAttribute('aria-label'), 'Enter fullscreen');
        button.click();
        result.context.document[api === 'webkit' ? 'webkitExitFullscreen' : 'exitFullscreen']();
        assert.equal(button.getAttribute('aria-label'), 'Enter fullscreen');
        assert.equal(button.getAttribute('aria-pressed'), 'false');
    });
}

test('fullscreen is hidden by default and when the browser has no fullscreen API', async () => {
    for (const [search, options] of [['?sendspin=0', {fullscreen:'standard'}], ['?sendspin=0&fullscreen=1', {}]]) {
        const result = await startKiosk(search, [], undefined, {}, options);
        assert.equal(result.context.document.getElementById('k-fullscreen').classList.contains('hidden'), true);
        assert.equal(result.fullscreenCalls.length, 0);
    }
});

test('a rejected fullscreen request leaves playback controls usable', async () => {
    const result = await startKiosk('?sendspin=0&fullscreen=1&controls=1', [], undefined, {}, {fullscreen:'reject'});
    const button = result.context.document.getElementById('k-fullscreen');
    button.click();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(button.getAttribute('aria-label'), 'Enter fullscreen');
    assert.equal(result.elements.get('kiosk-status').textContent, 'Fullscreen unavailable');
    assert.equal(result.root.classList.contains('controls'), true);
});

test('the URL builder enables the optional fullscreen button without changing default Sendspin', async () => {
    const builder = await readFile(new URL('../../provider/static/web/url-builder.js', import.meta.url), 'utf8');
    const nodes = new Map(['url', 'copy', 'open', 'controls', 'party', 'viz', 'lyrics', 'queue', 'show_player_id', 'remote', 'lite', 'wake_lock', 'fullscreen', 'token', 'device_id', 'sendspin_url'].map(id => [id, {value:'', checked:false, dataset:{}, select(){}}]));
    nodes.get('url').dataset.base = 'http://192.168.10.10:8098';
    const events = new Map();
    runInContext(builder, createContext({
        URL, navigator:{}, window:{open(){}},
        document:{getElementById:id => nodes.get(id), querySelector:() => ({value:'sendspin'}), addEventListener:(name, cb) => events.set(name, cb)},
    }));
    assert.equal(nodes.get('url').value, 'http://192.168.10.10:8098/kiosk');
    nodes.get('fullscreen').checked = true;
    events.get('change')();
    assert.equal(nodes.get('url').value, 'http://192.168.10.10:8098/kiosk?fullscreen=1');
});

test('Sendspin transport navigation preserves the fullscreen option', async () => {
    const result = await startKiosk('?sendspin=0&fullscreen=1');
    result.socketInstances[0].onmessage({ data:JSON.stringify({type:'sendspin', url:'/kiosk?sendspin=1'}) });
    assert.equal(new URL(result.location.href, 'http://192.168.10.10:8098').searchParams.get('fullscreen'), '1');
});

test('HTML5 registers without crypto or loading Sendspin', async () => {
    const result = await startKiosk('?sendspin=0', [], undefined, {}, { fakeSdk: true, noCrypto: true });
    assert.equal(result.startupError, undefined);
    assert.equal(result.sockets.length, 1);
    assert.equal(result.sdkLoads, 0);
    assert.match(result.storage.get('wk_device_id'), /^wk-/);
});

for (const type of ['touchstart', 'mousedown']) {
    test(`${type} activates HTML5 audio without Pointer Events`, async () => {
        let allowed = false;
        const result = await startKiosk('?sendspin=0', [], undefined, {}, { playAllowed: () => allowed });
        allowed = true;
        for (const callback of result.documentEvents.get(type) || []) callback({ type });
        await new Promise(resolve => setImmediate(resolve));
        assert.equal(result.audioInstances[0].paused, false);
        assert.equal(result.elements.get('kiosk-status').classList.contains('audio-prompt'), false);
    });
}

test('remote arrows move focus, Enter clicks and legacy Back closes QR', async () => {
    const result = await startKiosk('?sendspin=0&controls=1&remote=1&party=1&token=test', [], undefined,
        { info: { active: true, version: 'one' } });
    result.socketInstances[0].onmessage({ data: JSON.stringify({ type: 'welcome', player_id: 'wk-remote' }) });
    const prev = result.elements.get('k-prev');
    const play = result.elements.get('k-play');
    const next = result.elements.get('k-next');
    prev.disabled = play.disabled = next.disabled = false;
    prev.rect = { left:0, top:100, width:40, height:40 };
    play.rect = { left:100, top:100, width:40, height:40 };
    next.rect = { left:200, top:100, width:40, height:40 };
    play.focus();
    const key = (key, keyCode) => {
        const event = { key, keyCode, target: result.context.document.activeElement, preventDefault() {}, stopPropagation() {} };
        for (const callback of result.documentEvents.get('keydown')) callback(event);
    };
    key('ArrowRight');
    assert.equal(result.context.document.activeElement, next);
    assert.equal(result.elements.get('kiosk-controls').classList.contains('visible'), true);
    key('Enter');
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(result.apiCalls.at(-1).command, 'players/cmd/next');
    const party = result.elements.get('kiosk-party');
    party.onclick();
    key('Unidentified', 10009);
    assert.equal(party.attributes.get('aria-expanded'), 'false');
});

test('denied persistent storage keeps one identity for registration and Sendspin', async () => {
    const result = await startKiosk('', [], undefined, {}, { fakeSdk:true, storageDenied:true });
    result.socketInstances[0].onmessage({ data:JSON.stringify({ type:'welcome', player_id:'wk-storage' }) });
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(result.context.__sendspinPlayer.clientId,
        new URL(result.sockets[0]).searchParams.get('sendspin_client_id'));
});

for (const [opusSupported, flacSupported, expected] of [
    [false, false, ['pcm']], [true, false, ['opus', 'pcm']], [false, true, ['flac', 'pcm']],
]) {
    test(`Sendspin advertises only verified codecs (${expected.join(',')})`, async () => {
        const result = await startKiosk('', [], undefined, {}, { fakeSdk:true, secure:true, opusSupported, flacSupported });
        result.socketInstances[0].onmessage({ data:JSON.stringify({ type:'welcome', player_id:'wk-codecs' }) });
        await new Promise(resolve => setImmediate(resolve));
        assert.deepEqual(Array.from(result.context.__sendspinPlayer.config.codecs), expected);
    });
}

test('a welcome during a pending Sendspin start does not create a second SDK player', async () => {
    const result = await startKiosk('', [], undefined, {}, { fakeSdk:true, secure:true, opusSupported:true, flacSupported:true });
    const welcome = { data:JSON.stringify({ type:'welcome', player_id:'wk-once' }) };
    result.socketInstances[0].onmessage(welcome);
    result.socketInstances[0].onmessage(welcome);
    for (let i = 0; i < 5; i++) await new Promise(resolve => setImmediate(resolve));
    assert.equal(result.context.__sdkPlayers, 1);
    assert.ok(result.context.__sendspinPlayer);
});

test('missing Sendspin capabilities selects HTML5 before loading the SDK', async () => {
    const result = await startKiosk('', [], undefined, {}, { noCrypto:true, fakeSdk:true });
    assert.equal(result.sockets.length, 1);
    assert.equal(result.sdkLoads, 0);
    assert.equal(result.elements.get('kiosk-art').src, '/web/html5.svg');
});

test('HTTPS without a secure Sendspin endpoint selects HTML5', async () => {
    const result = await startKiosk('', [], undefined, {}, { protocol:'https:', fakeSdk:true });
    assert.equal(result.sdkLoads, 0);
    assert.equal(new URL(result.sockets[0]).protocol, 'wss:');
    assert.equal(result.elements.get('kiosk-art').src, '/web/html5.svg');
});

test('HTML5 exposes device volume when media-element volume is not writable', async () => {
    const result = await startKiosk('?sendspin=0&controls=1&token=test', [], undefined, {}, { systemVolume:true });
    assert.equal(result.elements.get('kiosk-volume').disabled, true);
    assert.equal(result.elements.get('k-volume-level').textContent, 'Device');
});

for (const receivedAudio of [false, true]) test(`initial Sendspin audio watchdog respects decoded buffers (${receivedAudio})`, async () => {
    const result = await startKiosk('', [], undefined, {}, { fakeSdk:true });
    result.socketInstances[0].onmessage({ data:JSON.stringify({ type:'welcome', player_id:'wk-audio' }) });
    await new Promise(resolve => setImmediate(resolve));
    const player = result.context.__sendspinPlayer;
    player.core.onStreamStart();
    if (receivedAudio) player.core.onAudioData({ samples:[] });
    result.advance(11000);
    for (const interval of result.intervals.filter(item => item.delay === 1000)) interval.callback();
    assert.equal(player.disconnected === true, !receivedAudio);
    assert.equal(result.elements.get('kiosk-art').src, receivedAudio ? '/web/sendspin.svg' : '/web/html5.svg');
});

test('Sendspin receives volume through its own public player API', async () => {
    const result = await startKiosk('', [], undefined, {}, { fakeSdk:true, systemVolume:true });
    const message = data => result.socketInstances[0].onmessage({ data:JSON.stringify(data) });
    message({ type:'welcome', player_id:'wk-volume' });
    await new Promise(resolve => setImmediate(resolve));
    result.context.__sendspinPlayer.core.onStreamStart();
    message({ type:'volume', level:42 });
    assert.equal(result.context.__sendspinPlayer.volume, 42);
});

test('online recovery reconnects once and ignores stale socket close callbacks', async () => {
    const result = await startKiosk('?sendspin=0');
    const previous = result.socketInstances[0];
    previous.close();
    for (const callback of result.windowEvents.get('online') || []) callback({type:'online'});
    assert.equal(result.socketInstances.length, 2);
    previous.onclose();
    for (const timer of result.timers.filter(timer => timer.delay === 2000)) timer.callback();
    assert.equal(result.socketInstances.length, 2);
});

test('optional wake lock reacquires after visibility changes without breaking startup', async () => {
    const result = await startKiosk('?sendspin=0&wake_lock=1', [], undefined, {}, { wakeLock:true });
    assert.deepEqual(result.wakeRequests, ['screen']);
    result.context.document.hidden = true;
    for (const callback of result.documentEvents.get('visibilitychange') || []) callback();
    result.context.document.hidden = false;
    for (const callback of result.documentEvents.get('visibilitychange') || []) callback();
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(result.wakeRequests, ['screen', 'screen']);
    assert.equal(result.socketInstances.length, 1);
});

test('denied wake lock still registers the player', async () => {
    const result = await startKiosk('?sendspin=0&wake_lock=1', [], undefined, {}, { wakeLock:'reject' });
    assert.equal(result.socketInstances.length, 1);
    assert.equal(result.startupError, undefined);
    assert.deepEqual(result.wakeRequests, ['screen']);
});

test('visible page resumes the Sendspin audio context through the public SDK', async () => {
    const result = await startKiosk('', [], undefined, {}, { fakeSdk:true });
    result.socketInstances[0].onmessage({ data:JSON.stringify({ type:'welcome', player_id:'wk-resume' }) });
    await new Promise(resolve => setImmediate(resolve));
    const player = result.context.__sendspinPlayer;
    player.core.onStreamStart();
    const before = player.unlocks;
    for (const callback of result.documentEvents.get('visibilitychange')) callback();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(player.unlocks, before + 1);
});

test('lightweight visualizer limits canvas redraw work during rapid animation frames', async () => {
    const result = await startKiosk('?sendspin=0&viz=1&lite=1');
    const message = data => result.socketInstances[0].onmessage({ data:JSON.stringify(data) });
    message({ type:'welcome', player_id:'wk-lite' });
    message({ type:'play', path:'/track', duration:180 });
    message({ type:'wave', bins:[1, 1, 1] });
    const canvas = result.elements.get('kiosk-viz');
    const before = canvas.clears;
    for (let i = 0; i < 100; i++) { result.advance(10); result.frames.at(-1)(); }
    assert.ok(canvas.clears - before <= 16);
    assert.ok(canvas.clears - before >= 10);
    assert.equal(result.root.classList.contains('lite'), true);
});

for (const search of ['?kiosk=1', '?kiosk=1&sendspin=1']) {
    test(`fresh kiosk registers over HTTP without crypto.randomUUID (${search})`, async () => {
        const result = await startKiosk(search);
        assert.equal(result.sockets.length, 1, `No registration WebSocket: ${result.startupError}`);
        const url = new URL(result.sockets[0]);
        assert.equal(url.pathname, '/ws');
        assert.equal(url.searchParams.get('device_id'), result.storage.get('wk_device_id'));
        assert.match(url.searchParams.get('device_id'), /^wk-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
        assert.match(url.searchParams.get('sendspin_client_id'), /^[A-Za-z0-9_-]{43}$/);
    });
}

test('explicit device id reaches registration on HTTP', async () => {
    const result = await startKiosk('?kiosk=1&device_id=wk-existing');
    assert.equal(result.sockets.length, 1, `No registration WebSocket: ${result.startupError}`);
    assert.equal(new URL(result.sockets[0]).searchParams.get('device_id'), 'wk-existing');
});

test('reload preserves the device id generated on HTTP', async () => {
    const first = await startKiosk('?kiosk=1');
    const reloaded = await startKiosk('?kiosk=1', [...first.storage]);
    assert.equal(new URL(reloaded.sockets[0]).searchParams.get('device_id'), first.storage.get('wk_device_id'));
});

test('native UUID generation remains supported', async () => {
    const uuid = 'a23e4567-e89b-42d3-a456-426614174000';
    let calls = 0;
    const result = await startKiosk('?kiosk=1', [], () => { calls++; return uuid; });
    assert.equal(calls, 1);
    assert.equal(result.storage.get('wk_device_id'), `wk-${uuid}`);
    assert.equal(new URL(result.sockets[0]).searchParams.get('device_id'), `wk-${uuid}`);
});

test('transport icons follow server state and retain their command bindings', async () => {
    const result = await startKiosk('?kiosk=1&token=test-token');
    const message = data => result.socketInstances[0].onmessage({ data: JSON.stringify(data) });
    message({ type: 'welcome', player_id: 'test-player' });
    message({ type: 'resume' });
    assert.equal(result.elements.get('k-play-icon').attributes.get('href'), '#k-icon-pause');
    assert.equal(result.elements.get('k-play').attributes.get('aria-label'), 'Pause');
    result.elements.get('k-play').onclick();
    assert.equal(result.apiCalls.at(-1).command, 'players/cmd/pause');
    message({ type: 'pause' });
    assert.equal(result.elements.get('k-play-icon').attributes.get('href'), '#k-icon-play');
    result.elements.get('k-play').onclick();
    assert.equal(result.apiCalls.at(-1).command, 'players/cmd/play');
    result.elements.get('k-next').onclick();
    assert.equal(result.apiCalls.at(-1).command, 'players/cmd/next');
    result.elements.get('k-prev').onclick();
    assert.equal(result.apiCalls.at(-1).command, 'players/cmd/previous');
});

test('server volume updates the slider, percentage and speaker icon together', async () => {
    const result = await startKiosk('?kiosk=1');
    const message = data => result.socketInstances[0].onmessage({ data: JSON.stringify(data) });
    message({ type: 'volume', level: 42 });
    assert.equal(result.elements.get('kiosk-volume').value, '42');
    assert.equal(result.elements.get('k-volume-level').textContent, '42%');
    assert.equal(result.elements.get('k-volume-panel-level').textContent, '42%');
    assert.equal(result.elements.get('k-volume-icon').attributes.get('href'), '#k-icon-volume-1');
    message({ type: 'volume', level: 0 });
    assert.equal(result.elements.get('k-volume-icon').attributes.get('href'), '#k-icon-volume-x');
});

for (const search of ['?kiosk=1', '?kiosk=1&sendspin=1']) {
    test(`registered player identifier is displayed and refreshed (${search})`, async () => {
        const result = await startKiosk(search);
        const message = data => result.socketInstances[0].onmessage({ data: JSON.stringify(data) });
        message({ type: 'welcome', player_id: 'wk-test-player' });
        assert.equal(result.elements.get('kiosk-player-id')?.textContent, 'Player ID: wk-test-player');
        message({ type: 'welcome', player_id: 'wk-reconnected-player' });
        assert.equal(result.elements.get('kiosk-player-id').textContent, 'Player ID: wk-reconnected-player');
    });
}

for (const [search, type, logo] of [
    ['?kiosk=1&sendspin=0', 'HTML5', '/web/html5.svg'],
    ['?kiosk=1&sendspin=1', 'Sendspin', '/web/sendspin.svg'],
]) {
    test(`idle ${type} player shows its ID, project artwork and the MA version`, async () => {
        const result = await startKiosk(search);
        const message = data => result.socketInstances[0].onmessage({ data: JSON.stringify(data) });
        message({ type: 'welcome', player_id: 'wk-idle', server_version: '2.17.0.dev42' });
        assert.equal(result.elements.get('kiosk-title').textContent, 'wk-idle');
        assert.equal(result.elements.get('kiosk-artist').textContent, 'Music Assistant 2.17.0.dev42');
        assert.equal(result.elements.get('kiosk-bar-title').textContent, 'wk-idle');
        assert.equal(result.elements.get('kiosk-bar-artist').textContent, 'Music Assistant 2.17.0.dev42');
        assert.equal(result.elements.get('kiosk-art').src, logo);
        message({ type: 'play', path: '/track.wav', title: 'Track', artist: 'Artist', image_url: '/cover.jpg', duration: 180 });
        assert.equal(result.elements.get('kiosk-title').textContent, 'Track');
        assert.equal(result.elements.get('kiosk-artist').textContent, 'Artist');
        message({ type: 'stop' });
        assert.equal(result.elements.get('kiosk-title').textContent, 'wk-idle');
        assert.equal(result.elements.get('kiosk-art').src, logo);
    });
}

for (const [search, logo] of [['?sendspin=0', '/web/html5.svg'], ['', '/web/sendspin.svg']]) {
    test(`bottom-left artwork stays a project emblem without Party (${search})`, async () => {
        const result = await startKiosk(search);
        const message = data => result.socketInstances[0].onmessage({ data: JSON.stringify(data) });
        message({ type: 'welcome', player_id: 'wk-logo' });
        message({ type: 'play', path: '/track', title: 'Track', image_url: '/album.jpg', duration: 180 });
        assert.equal(result.elements.get('kiosk-bar-art').src, logo);
        assert.equal(result.elements.get('kiosk-party').classList.contains('hidden'), true);
        assert.equal(result.elements.get('kiosk-art').src.includes('album.jpg'), true);
    });
}

test('Party thumbnail falls back to the emblem on a failed QR and recovers on the next poll', async () => {
    const state = { info: { active: true, version: 'one', name: 'Room' } };
    const result = await startKiosk('?sendspin=0&party=1', [], undefined, state);
    const thumbnail = result.elements.get('kiosk-party-thumbnail');
    const party = result.elements.get('kiosk-party');
    assert.equal(thumbnail.src, '/api/party/qr.svg?v=one');
    assert.equal(party.classList.contains('hidden'), false);
    assert.equal(result.elements.get('kiosk-bar-art').src, '/web/html5.svg');
    party.onclick();
    thumbnail.onerror();
    assert.equal(party.classList.contains('hidden'), true);
    assert.equal(party.attributes.get('aria-expanded'), 'false');
    const poll = result.intervals.find(interval => interval.delay === 10000).callback;
    await poll();
    assert.equal(party.classList.contains('hidden'), false);
    state.info = { active: false };
    await poll();
    assert.equal(party.classList.contains('hidden'), true);
    assert.equal(result.elements.get('kiosk-bar-art').src, '/web/html5.svg');
});

test('Party QR toggles the large artwork without losing current track updates', async () => {
    const partyState = { info: { active: true, version: 'one', name: 'Party' } };
    const result = await startKiosk('?kiosk=1&token=test&party=1', [], undefined, partyState);
    const message = data => result.socketInstances[0].onmessage({ data: JSON.stringify(data) });
    message({ type: 'welcome', player_id: 'wk-party', server_version: '2.17.0' });
    await new Promise(resolve => setImmediate(resolve));
    const party = result.elements.get('kiosk-party');
    party.onclick();
    assert.equal(party.attributes.get('aria-expanded'), 'true');
    assert.equal(result.elements.get('kiosk-party-art').src, '/api/party/qr.svg?v=one');
    message({ type: 'play', path: '/track.wav', title: 'New track', image_url: '/new-cover.jpg', duration: 180 });
    assert.equal(result.elements.get('kiosk-title').textContent, 'New track');
    assert.equal(party.attributes.get('aria-expanded'), 'true');
    party.onclick();
    assert.equal(party.attributes.get('aria-expanded'), 'false');
    assert.equal(result.elements.get('kiosk-art').src, '/new-cover.jpg');
    party.onclick();
    result.elements.get('kiosk-party-expanded').onclick();
    assert.equal(party.attributes.get('aria-expanded'), 'false');
    party.onclick();
    for (const callback of result.documentEvents.get('keydown')) callback({ key: 'Escape', target: { tagName: 'BUTTON' } });
    assert.equal(party.attributes.get('aria-expanded'), 'false');
});

test('enlarged Party QR follows version changes and closes when Party disappears', async () => {
    const partyState = { info: { active: true, version: 'one' } };
    const result = await startKiosk('?kiosk=1&party=1', [], undefined, partyState);
    const poll = result.intervals.find(item => item.delay === 10000).callback;
    const party = result.elements.get('kiosk-party');
    party.onclick();
    partyState.info = { active: true, version: 'two' };
    await poll();
    assert.equal(result.elements.get('kiosk-party-art').src, '/api/party/qr.svg?v=two');
    assert.equal(party.attributes.get('aria-expanded'), 'true');
    partyState.info = { active: false };
    await poll();
    assert.equal(party.attributes.get('aria-expanded'), 'false');
    assert.equal(result.elements.get('kiosk-party-expanded').classList.contains('hidden'), true);
    partyState.info = { active: true, version: 'three' };
    await poll();
    party.onclick();
    partyState.ok = false;
    await poll();
    assert.equal(party.attributes.get('aria-expanded'), 'false');
});

test('a missing server version never exposes undefined in the idle labels', async () => {
    const result = await startKiosk('?kiosk=1');
    result.socketInstances[0].onmessage({ data: JSON.stringify({ type: 'welcome', player_id: 'wk-old' }) });
    assert.equal(result.elements.get('kiosk-artist').textContent, 'Music Assistant');
});

test('bare kiosk defaults to Sendspin without optional additions', async () => {
    const result = await startKiosk('');
    assert.equal(result.root.classList.contains('sendspin'), true);
    result.socketInstances[0].onmessage({ data: JSON.stringify({ type: 'welcome', player_id: 'wk-default' }) });
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(result.elements.get('kiosk-player-id').classList.contains('hidden'), true);
    assert.equal(result.root.classList.contains('controls'), false);
    assert.equal(result.fetchCalls.includes('/api/party'), false);
    assert.equal(result.intervals.some(item => item.delay === 10000 || item.delay === 15000), false);
});

test('idle project artwork supplies the same background image as the album artwork', async () => {
    const result = await startKiosk('?sendspin=0');
    assert.equal(result.elements.get('kiosk-bg-img').src, '/web/html5.svg');
    assert.equal(result.elements.get('kiosk-bg-img').style.opacity, '1');
});

test('URL builder emits a clean kiosk URL and only selected additions', async () => {
    const builder = await readFile(new URL('../../provider/static/web/url-builder.js', import.meta.url), 'utf8');
    const nodes = new Map(['url', 'copy', 'open', 'controls', 'party', 'viz', 'lyrics', 'queue', 'show_player_id', 'remote', 'lite', 'wake_lock', 'ma_url', 'token', 'device_id', 'sendspin_url'].map(id => [id, { value: '', checked: false, dataset: {}, select() {} }]));
    nodes.get('url').dataset.base = 'http://192.168.10.10:8098';
    let mode = 'sendspin';
    const events = new Map();
    runInContext(builder, createContext({
        URL, navigator: {}, window: { open() {} },
        document: {
            getElementById: id => nodes.get(id),
            querySelector: () => ({ value: mode }),
            addEventListener: (name, callback) => events.set(name, callback),
        },
    }));
    assert.equal(nodes.get('url').value, 'http://192.168.10.10:8098/kiosk');
    mode = 'html5';
    nodes.get('show_player_id').checked = true;
    nodes.get('party').checked = true;
    events.get('change')();
    const url = new URL(nodes.get('url').value);
    assert.equal(url.searchParams.get('sendspin'), '0');
    assert.equal(url.searchParams.get('show_player_id'), '1');
    assert.equal(url.searchParams.get('party'), '1');
    assert.equal(url.searchParams.has('controls'), false);
});

test('player ID is visible only when explicitly enabled', async () => {
    const result = await startKiosk('?show_player_id=1');
    result.socketInstances[0].onmessage({ data: JSON.stringify({ type: 'welcome', player_id: 'wk-visible' }) });
    assert.equal(result.elements.get('kiosk-player-id').classList.contains('hidden'), false);
    assert.equal(result.elements.get('kiosk-player-id').textContent, 'Player ID: wk-visible');
});

test('Sendspin navigation retains selected kiosk additions', async () => {
    const result = await startKiosk('?sendspin=0&controls=1&party=1&show_player_id=1&device_id=chosen');
    result.socketInstances[0].onmessage({ data: JSON.stringify({ type: 'sendspin', url: '/kiosk?sendspin=1&sendspin_client_id=server-identity' }) });
    const url = new URL(result.location.href);
    assert.equal(url.pathname, '/kiosk');
    assert.equal(url.searchParams.get('sendspin'), '1');
    assert.equal(url.searchParams.get('sendspin_client_id'), 'server-identity');
    assert.equal(url.searchParams.get('controls'), '1');
    assert.equal(url.searchParams.get('party'), '1');
    assert.equal(url.searchParams.get('show_player_id'), '1');
    assert.equal(url.searchParams.get('device_id'), 'chosen');
});


test('fragment token is stored and removed from the visible player URL', async () => {
    const result = await startKiosk('?sendspin=0&controls=1', [], undefined, {}, { hash: '#token=secret-token' });
    assert.equal(result.storage.get('wk_token'), 'secret-token');
    assert.equal(result.location.hash, '');
    const message = data => result.socketInstances[0].onmessage({ data: JSON.stringify(data) });
    message({ type: 'welcome', player_id: 'wk-fragment' });
    result.elements.get('k-next').onclick();
    assert.equal(result.apiCalls.at(-1).command, 'players/cmd/next');
    message({ type: 'sendspin', url: '/kiosk?sendspin=1' });
    assert.equal(new URL(result.location.href).searchParams.has('token'), false);
});


test('builder puts credentials in the fragment instead of the request query', async () => {
    const builder = await readFile(new URL('../../provider/static/web/url-builder.js', import.meta.url), 'utf8');
    const nodes = new Map(['url', 'copy', 'open', 'controls', 'party', 'viz', 'lyrics', 'queue', 'show_player_id', 'remote', 'lite', 'wake_lock', 'ma_url', 'token', 'device_id', 'sendspin_url'].map(id => [id, { value: '', checked: false, dataset: {}, select() {} }]));
    nodes.get('url').dataset.base = 'http://192.0.2.10:8098';
    nodes.get('token').value = 'secret & token';
    runInContext(builder, createContext({
        URL, URLSearchParams, navigator: {}, window: { open() {} },
        document: {
            getElementById: id => nodes.get(id),
            querySelector: () => ({ value: 'sendspin' }),
            addEventListener() {},
        },
    }));
    const url = new URL(nodes.get('url').value);
    assert.equal(url.searchParams.has('token'), false);
    assert.equal(new URLSearchParams(url.hash.slice(1)).get('token'), 'secret & token');
});


test('queue artwork is an inert DOM attribute even with quotes in metadata', async () => {
    const image = '/cover.jpg" onerror="alert(1)';
    const result = await startKiosk('?sendspin=0&queue=1&token=test', [], undefined, {}, {
        api: request => request.command === 'player_queues/get_active_queue'
            ? { queue_id: 'wk-queue', current_index: 0 }
            : [{ media_item: { name: 'Track', image } }],
    });
    result.socketInstances[0].onmessage({ data: JSON.stringify({ type: 'welcome', player_id: 'wk-queue' }) });
    await new Promise(resolve => setImmediate(resolve));
    const queue = result.elements.get('kiosk-queue');
    assert.equal(queue.innerHTML.includes('onerror='), false);
    assert.equal(queue.children[0].children[0].src, image);
    assert.equal(queue.children[0].children[0].attributes.has('onerror'), false);
});


test('long queue keeps the current item visible beyond the first 200 entries', async () => {
    const items = Array.from({ length: 450 }, (_, i) => ({ name: 'Track ' + i }));
    const result = await startKiosk('?sendspin=0&queue=1&token=test', [], undefined, {}, {
        api: request => request.command === 'player_queues/get_active_queue'
            ? { queue_id: 'wk-long', current_index: 350 }
            : items.slice(request.args.offset || 0, (request.args.offset || 0) + request.args.limit),
    });
    result.socketInstances[0].onmessage({ data: JSON.stringify({ type: 'welcome', player_id: 'wk-long' }) });
    await new Promise(resolve => setImmediate(resolve));
    const active = result.elements.get('kiosk-queue').children.find(row => row.classList.contains('active'));
    assert.ok(active, 'Current item is absent from the displayed queue');
    assert.equal(active.children[0].children[0].textContent, 'Track 350');
});


test('wide lyrics show the full song without replaceChildren and follow viewport changes', async () => {
    const result = await startKiosk('?lyrics=1', [], undefined, {}, { fakeSdk: true, width: 1280, noReplaceChildren: true });
    const message = data => result.socketInstances[0].onmessage({ data: JSON.stringify(data) });
    message({ type: 'welcome', player_id: 'wk-lyrics' });
    await new Promise(resolve => setImmediate(resolve));
    message({ type: 'play', path: '/track', title: 'Track', duration: 180 });
    const core = result.context.__sendspinPlayer.core;
    core.onStreamStart();
    core.trackProgress = { positionMs: 65000, durationMs: 180000, playbackSpeed: 1 };
    message({ type: 'lyrics', lines: [{ t: 0, text: 'Opening' }, { t: 20, text: 'Verse' }, { t: 60, text: 'Middle' }, { t: 90, text: 'Ending' }, { t: 120, text: '<Outro>' }] });
    const lyrics = result.elements.get('kiosk-lyrics');
    assert.equal(lyrics.dataset.layout, 'wide');
    assert.deepEqual(Array.from(lyrics.children[0].children, line => line.textContent), ['Opening', 'Verse', 'Middle', 'Ending', '<Outro>']);
    assert.equal(lyrics.children[0].children[2].classList.contains('current'), true);
    assert.equal(lyrics.children[0].style.transform, 'translateY(8px)');
    core.trackProgress.positionMs = 95000;
    result.frames.at(-1)();
    assert.equal(lyrics.children[0].children[3].classList.contains('current'), true);
    assert.equal(lyrics.children[0].style.transform, 'translateY(-72px)');
    result.resize(768);
    assert.equal(lyrics.dataset.layout, 'compact');
    assert.match(lyrics.innerHTML, /^<div class="l">Middle<\/div><div class="l current">Ending<\/div><div class="l">&lt;Outro&gt;<\/div>$/);
    result.resize(769);
    assert.equal(lyrics.dataset.layout, 'wide');
    assert.equal(lyrics.children[0].children[3].classList.contains('current'), true);
    message({ type: 'lyrics', lines: [] });
    assert.equal(lyrics.classList.contains('hidden'), true);
    assert.equal(result.root.classList.contains('lyrics-visible'), false);
});

test('a legacy browser without Grid keeps compact lyrics even on a wide screen', async () => {
    const result = await startKiosk('?sendspin=0&lyrics=1', [], undefined, {}, { width:1280, noGrid:true });
    result.socketInstances[0].onmessage({ data:JSON.stringify({ type:'lyrics', lines:[{ t:0, text:'Legacy line' }] }) });
    assert.equal(result.elements.get('kiosk-lyrics').dataset.layout, 'compact');
    assert.match(result.elements.get('kiosk-lyrics').innerHTML, /Legacy line/);
});

for (const search of ['?lyrics=1', '?lyrics=1&token=test']) {
    test(`Sendspin lyrics follow SDK progress without a visible queue (${search})`, async () => {
        const result = await startKiosk(search, [], undefined, {}, { fakeSdk: true });
        const message = data => result.socketInstances[0].onmessage({ data: JSON.stringify(data) });
        message({ type: 'welcome', player_id: 'wk-clock' });
        await new Promise(resolve => setImmediate(resolve));
        message({ type: 'play', path: '/track', title: 'Track', duration: 180 });
        const core = result.context.__sendspinPlayer.core;
        core.onStreamStart();
        core.trackProgress = { positionMs: 65000, durationMs: 180000, playbackSpeed: 1 };
        message({ type: 'lyrics', lines: [{ t: 0, text: 'Opening' }, { t: 60, text: 'Middle' }, { t: 90, text: 'Ending' }] });
        result.frames.at(-1)();
        assert.match(result.elements.get('kiosk-lyrics').innerHTML, /class="l current">Middle</);
        core.trackProgress = { positionMs: 95000, durationMs: 180000, playbackSpeed: 1 };
        result.advance(100);
        result.frames.at(-1)();
        assert.match(result.elements.get('kiosk-lyrics').innerHTML, /class="l current">Ending</);
        assert.equal(result.apiCalls.some(call => call.command === 'player_queues/get_active_queue'), false);
    });
}


test('Sendspin clock follows SDK progress again after seeking without a queue', async () => {
    const result = await startKiosk('?controls=1&token=test', [], undefined, {}, { fakeSdk: true });
    const message = data => result.socketInstances[0].onmessage({ data: JSON.stringify(data) });
    message({ type: 'welcome', player_id: 'wk-seek' });
    await new Promise(resolve => setImmediate(resolve));
    message({ type: 'play', path: '/track', duration: 180 });
    const core = result.context.__sendspinPlayer.core;
    core.onStreamStart();
    const slider = result.elements.get('kiosk-seek');
    slider.onchange({ target: { value: '500' } });
    assert.equal(result.elements.get('kiosk-time').textContent, '1:30');
    core.trackProgress = { positionMs: 95000, durationMs: 180000, playbackSpeed: 1 };
        result.advance(100);
    result.advance(2500);
    result.intervals.find(interval => interval.delay === 500).callback();
    assert.equal(result.elements.get('kiosk-time').textContent, '1:35');
});


test('server stop is applied even when a confirmation would be dismissed', async () => {
    const result = await startKiosk('?sendspin=0');
    const message = data => result.socketInstances[0].onmessage({ data: JSON.stringify(data) });
    message({ type: 'welcome', player_id: 'wk-stop' });
    message({ type: 'play', path: '/track', title: 'Playing', duration: 180 });
    await new Promise(resolve => setImmediate(resolve));
    const audio = result.audioInstances[0];
    audio.paused = false;
    message({ type: 'stop', showNotification: true });
    assert.equal(audio.paused, true);
    assert.equal(audio.getAttribute('src'), null);
    assert.equal(result.elements.get('kiosk-title').textContent, 'wk-stop');
    assert.equal(result.elements.get('kiosk-status').textContent, 'Playback stopped');
    assert.equal(result.elements.get('k-play-icon').attributes.get('href'), '#k-icon-play');
});


test('audio completion echoes its play generation once and resets for a new play', async () => {
    const result = await startKiosk('?sendspin=0');
    const socket = result.socketInstances[0];
    const message = data => socket.onmessage({ data: JSON.stringify(data) });
    message({ type: 'welcome', player_id: 'wk-ended' });
    const audio = result.audioInstances[0];
    message({ type: 'play', path: '/old', duration: 5, playback_generation: 'old-generation' });
    audio.ended = true;
    audio.onended();
    audio.onended();
    assert.equal(socket.sent.filter(item => item.type === 'ended').length, 1);
    assert.equal(socket.sent.at(-1).playback_generation, 'old-generation');
    message({ type: 'play', path: '/new', duration: 5, playback_generation: 'new-generation' });
    audio.ended = true;
    audio.onended();
    assert.equal(socket.sent.filter(item => item.type === 'ended').length, 2);
    assert.equal(socket.sent.at(-1).playback_generation, 'new-generation');
});


test("browser reports HTML5 and active Sendspin volume capabilities", async () => {
    const result = await startKiosk("", [], undefined, {}, { fakeSdk:true, systemVolume:true });
    const socket = result.socketInstances[0];
    assert.equal(new URL(result.sockets[0]).searchParams.get("volume_control"), "0");
    socket.onmessage({ data:JSON.stringify({ type:"welcome", player_id:"wk-volume-caps" }) });
    await new Promise(resolve => setImmediate(resolve));
    const player = result.context.__sendspinPlayer;
    player.core.onStreamStart();
    assert.deepEqual(socket.sent.at(-1), { type:"capabilities", volume_control:true });
    player.core.onStreamEnd();
    assert.deepEqual(socket.sent.at(-1), { type:"capabilities", volume_control:false });
});


test("Sendspin identifies the same browser model reported by MA", async () => {
    const result = await startKiosk("", [], undefined, {}, { fakeSdk:true });
    result.socketInstances[0].onmessage({ data:JSON.stringify({ type:"welcome", player_id:"wk-browser",
        device_info:{ model:"Chrome 129 (Linux)" } }) });
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(result.context.__sendspinPlayer.config.productName, "Web Kiosk / Chrome 129 (Linux)");
});


test('Sendspin media generations display lyrics without starting HTML5 audio', async () => {
    const result = await startKiosk('?lyrics=1', [], undefined, {}, { fakeSdk:true, width:1280 });
    const message = data => result.socketInstances[0].onmessage({data:JSON.stringify(data)});
    message({type:'welcome', player_id:'wk-group'});
    await new Promise(resolve => setImmediate(resolve));
    result.context.__sendspinPlayer.core.onStreamStart();
    const audio = result.audioInstances[0];
    const source = audio.src;
    message({type:'media', wave_seq:1, title:'Group track', duration:180});
    message({type:'lyrics', seq:1, lines:[{t:0, text:'Group lyrics'}]});
    assert.equal(result.elements.get('kiosk-lyrics').classList.contains('hidden'), false);
    assert.equal(result.elements.get('kiosk-lyrics').children[0].children[0].textContent, 'Group lyrics');
    message({type:'media', wave_seq:2, title:'Next track', duration:180});
    assert.equal(result.elements.get('kiosk-lyrics').classList.contains('hidden'), true);
    message({type:'lyrics', seq:1, lines:[{t:0, text:'Obsolete'}]});
    assert.equal(result.elements.get('kiosk-lyrics').classList.contains('hidden'), true);
    assert.equal(audio.src, source);
});

test('optional group label follows membership and uses plain text', async () => {
    for (const search of ['?show_group=1', '']) {
        const result = await startKiosk(search);
        const label = result.context.document.getElementById('kiosk-group');
        const message = data => result.socketInstances[0].onmessage({data:JSON.stringify(data)});
        message({type:'group', group_id:'group', group_name:'Living room'});
        assert.equal(label.classList.contains('hidden'), !search);
        assert.equal(label.textContent, 'Living room');
        message({type:'group', group_id:'group', group_name:'<Renamed>'});
        assert.equal(label.textContent, '<Renamed>');
        message({type:'group', group_id:null, group_name:null});
        assert.equal(label.classList.contains('hidden'), true);
    }
});


test('builder and Sendspin navigation retain optional group display', async () => {
    const builder = await readFile(new URL('../../provider/static/web/url-builder.js', import.meta.url), 'utf8');
    const nodes = new Map(['url','copy','open','controls','party','viz','lyrics','queue','show_player_id','show_group','remote','lite','wake_lock','fullscreen','token','device_id','sendspin_url'].map(id => [id,{value:'',checked:false,dataset:{},select(){}}]));
    nodes.get('url').dataset.base = 'http://192.168.10.10:8098';
    const events = new Map();
    runInContext(builder, createContext({URL,navigator:{},window:{open(){}},document:{getElementById:id=>nodes.get(id),querySelector:()=>({value:'sendspin'}),addEventListener:(name,cb)=>events.set(name,cb)}}));
    nodes.get('show_group').checked = true;
    events.get('change')();
    assert.equal(new URL(nodes.get('url').value).searchParams.get('show_group'), '1');
    const result = await startKiosk('?sendspin=0&show_group=1');
    result.socketInstances[0].onmessage({data:JSON.stringify({type:'sendspin',url:'/kiosk?sendspin=1'})});
    assert.equal(new URL(result.location.href,'http://192.168.10.10:8098').searchParams.get('show_group'), '1');
});


for (const mode of ['0', '1']) test(`custom player name replaces idle ID in HTML5/Sendspin (${mode})`, async () => {
    const result = await startKiosk('?sendspin=' + mode + '&show_player_id=1');
    const message = data => result.socketInstances[0].onmessage({data:JSON.stringify(data)});
    message({type:'welcome', player_id:'wk-test', custom_name:'Kitchen'});
    assert.equal(result.elements.get('kiosk-title').textContent, 'Kitchen');
    assert.equal(result.elements.get('kiosk-bar-title').textContent, 'Kitchen');
    assert.equal(result.elements.get('kiosk-player-id').textContent, 'Player ID: wk-test');
    message({type:'player', custom_name:'<Living room>'});
    assert.equal(result.elements.get('kiosk-title').textContent, '<Living room>');
    message({type:'play', path:'/track', title:'Song', duration:180});
    message({type:'player', custom_name:'Bedroom'});
    assert.equal(result.elements.get('kiosk-title').textContent, 'Song');
    message({type:'stop'});
    assert.equal(result.elements.get('kiosk-title').textContent, 'Bedroom');
    message({type:'player', custom_name:null});
    assert.equal(result.elements.get('kiosk-title').textContent, 'wk-test');
});


test('no-audio fallback asks MA for native playback over the kiosk socket', async () => {
    const result = await startKiosk('?sendspin=1', [], undefined, {}, {fakeSdk:true, playAllowed:()=>true});
    const message = data => result.socketInstances[0].onmessage({data:JSON.stringify(data)});
    message({type:'welcome', player_id:'wk-fallback'});
    await new Promise(resolve => setImmediate(resolve));
    const player = result.context.__sendspinPlayer;
    player.core.onStreamStart({}, false);
    player.config.onStateChange({isPlaying:true,serverState:{metadata:{title:'Track'}}});
    result.advance(11000);
    result.intervals.find(timer => timer.delay === 1000).callback();
    assert.equal(result.socketInstances[0].sent.some(value => value.type === 'sendspin_fallback'), true);
    message({type:'play',path:'/native-track',title:'Track',duration:180});
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(result.audioInstances[0].src.includes('/native-track'),true);
    assert.equal(result.audioInstances[0].paused,false);
});


test('HTML5 clock and lyrics use media time for a sped-up stream', async () => {
    const result = await startKiosk('?sendspin=0&lyrics=1&controls=1', [], undefined, {}, {width:1280, playAllowed:()=>true});
    const message = data => result.socketInstances[0].onmessage({data:JSON.stringify(data)});
    message({type:'welcome',player_id:'wk-speed'});
    message({type:'play',path:'/podcast',title:'Podcast',duration:300,start:100,playback_speed:2,wave_seq:1});
    result.audioInstances[0].currentTime = 40;
    message({type:'lyrics',seq:1,lines:[{t:0,text:'Opening'},{t:170,text:'Current'}]});
    result.audioInstances[0].ontimeupdate();
    result.frames.at(-1)();
    assert.equal(result.elements.get('kiosk-time').textContent,'3:00');
    assert.equal(result.elements.get('kiosk-lyrics').children[0].children[1].classList.contains('current'),true);
});


test('an HTML5 media message moves the song clock without reloading audio', async () => {
    const result = await startKiosk('?sendspin=0&controls=1', [], undefined, {}, {playAllowed:()=>true});
    const message = data => result.socketInstances[0].onmessage({data:JSON.stringify(data)});
    message({type:'welcome',player_id:'wk-flow'});
    message({type:'play',path:'/flow',title:'First',duration:200,start:0,playback_speed:1,wave_seq:1,playback_generation:'g'});
    const audio = result.audioInstances[0];
    const source = audio.src;
    audio.currentTime = 230;
    // The queue moved on to the second song, which started at stream second 200.
    message({type:'media',wave_seq:2,title:'Second',artist:'B',duration:180,start:-200,playback_speed:1});
    audio.ontimeupdate();
    assert.equal(result.elements.get('kiosk-time').textContent,'0:30');
    assert.equal(result.elements.get('kiosk-dur').textContent,'3:00');
    assert.equal(audio.src, source);
    assert.equal(audio.paused, false);
});

test('the song clock never passes the song duration', async () => {
    const result = await startKiosk('?sendspin=0&controls=1', [], undefined, {}, {playAllowed:()=>true});
    const message = data => result.socketInstances[0].onmessage({data:JSON.stringify(data)});
    message({type:'welcome',player_id:'wk-flow-end'});
    message({type:'play',path:'/flow',title:'First',duration:200,start:0,playback_speed:1,wave_seq:1,playback_generation:'g'});
    // The next song is already playing; its metadata has not arrived yet.
    result.audioInstances[0].currentTime = 203;
    result.audioInstances[0].ontimeupdate();
    assert.equal(result.elements.get('kiosk-time').textContent,'3:20');
});

test('seeks at the end of a song leave the end margin the server names', async () => {
    const result = await startKiosk('?sendspin=0&controls=1&token=test-token', [], undefined, {}, {playAllowed:()=>true});
    const message = data => result.socketInstances[0].onmessage({data:JSON.stringify(data)});
    message({type:'welcome',player_id:'wk-end',end_margin:3});
    message({type:'play',path:'/flow',title:'Song',duration:200.6,start:0,playback_speed:1,wave_seq:1,playback_generation:'g'});
    const seeks = () => result.apiCalls.filter(call => call.command === 'players/cmd/seek').map(call => call.args.position);
    result.elements.get('kiosk-seek').onchange({target:{value:'1000', style:{setProperty(){}}}});
    result.audioInstances[0].currentTime = 195;
    for (const callback of result.documentEvents.get('keydown')) callback({key:'ArrowRight', target:{tagName:'BODY'}, preventDefault(){}});
    result.elements.get('kiosk-seek').onchange({target:{value:'500', style:{setProperty(){}}}});
    assert.deepEqual(seeks(), [197, 197, 100]);
});

test('every page seek goes through the one clamped seek function', () => {
    const calls = source.match(/players\/cmd\/seek/g) || [];
    assert.equal(calls.length, 1);
    assert.match(source, /function seekToSong\(seconds\) \{[\s\S]*?players\/cmd\/seek/);
});

test('a stopped kiosk shows 0:00, not where its stream ended', async () => {
    const result = await startKiosk('?sendspin=0&controls=1', [], undefined, {}, {playAllowed:()=>true});
    const message = data => result.socketInstances[0].onmessage({data:JSON.stringify(data)});
    message({type:'welcome',player_id:'wk-stop'});
    message({type:'play',path:'/flow',title:'Song',duration:25,start:-75,playback_speed:1,wave_seq:1,playback_generation:'g'});
    const audio = result.audioInstances[0];
    audio.currentTime = 100;
    message({type:'stop'});
    audio.ontimeupdate();
    assert.equal(result.elements.get('kiosk-time').textContent,'0:00');
});

test('kiosk proves its SDK identity before receiving registration welcome', async () => {
    const result = await startKiosk('');
    const socket = result.socketInstances[0];
    const clientId = new URL(result.sockets[0]).searchParams.get('sendspin_client_id');
    const server = generateKeyPairSync('x25519');
    const challenge = {type:'sendspin_challenge',client_id:clientId,server_key:server.publicKey.export({format:'der',type:'spki'}).subarray(-32).toString('base64url'),message:'fresh:ws:wk-test:' + clientId};
    socket.onmessage({data:JSON.stringify(challenge)});
    await new Promise(resolve => setImmediate(resolve));
    const publicKey = createPublicKey({key:Buffer.concat([Buffer.from('302a300506032b656e032100','hex'),Buffer.from(clientId,'base64url')]),format:'der',type:'spki'});
    const shared = diffieHellman({privateKey:server.privateKey,publicKey});
    assert.equal(socket.sent.find(item => item.type === 'sendspin_proof').proof, createHmac('sha256',shared).update(challenge.message).digest('hex'));
    assert.equal(result.context.__sendspinPlayer, undefined);
});


test('HTML5 load failures report the playback generation once', async () => {
    let failure = null;
    const result = await startKiosk('?sendspin=0', [], undefined, {}, {
        playAllowed: () => true,
        playError: () => failure,
    });
    const socket = result.socketInstances[0];
    const message = data => socket.onmessage({ data: JSON.stringify(data) });
    const settle = () => new Promise(resolve => setImmediate(resolve));
    message({ type: 'welcome', player_id: 'wk-fail' });
    const audio = result.audioInstances[0];
    const reports = () => socket.sent.filter(item => item.type === 'playback_error');

    failure = Object.assign(new Error('Unsupported'), { name: 'NotSupportedError' });
    message({ type: 'play', path: '/broken', duration: 5, playback_generation: 'gen-1' });
    await settle();
    assert.equal(reports().length, 1);
    assert.equal(reports()[0].playback_generation, 'gen-1');
    audio.error = { code: 4 };
    audio.onerror();
    assert.equal(reports().length, 1);

    failure = null;
    message({ type: 'play', path: '/works', duration: 5, playback_generation: 'gen-2' });
    await settle();
    assert.equal(reports().length, 1);
    audio.error = { code: 2 };
    audio.onerror();
    assert.equal(reports().length, 2);
    assert.equal(reports()[1].playback_generation, 'gen-2');
});

test('autoplay denial and replaced streams are not reported as playback failures', async () => {
    let failure = null;
    const result = await startKiosk('?sendspin=0', [], undefined, {}, {
        playAllowed: () => true,
        playError: () => failure,
    });
    const socket = result.socketInstances[0];
    const message = data => socket.onmessage({ data: JSON.stringify(data) });
    const settle = () => new Promise(resolve => setImmediate(resolve));
    message({ type: 'welcome', player_id: 'wk-denied' });
    const audio = result.audioInstances[0];

    failure = Object.assign(new Error('Gesture required'), { name: 'NotAllowedError' });
    message({ type: 'play', path: '/denied', duration: 5, playback_generation: 'gen-1' });
    await settle();
    failure = Object.assign(new Error('Unsupported'), { name: 'NotSupportedError' });
    message({ type: 'play', path: '/old', duration: 5, playback_generation: 'gen-2' });
    failure = null;
    message({ type: 'play', path: '/new', duration: 5, playback_generation: 'gen-3' });
    await settle();
    audio.error = { code: 1 };
    audio.onerror();
    assert.equal(socket.sent.filter(item => item.type === 'playback_error').length, 0);
});

test('kiosk socket asks for lyrics only when the lyrics addition is enabled', async () => {
    const plain = await startKiosk('?sendspin=0');
    assert.ok(!plain.sockets[0].includes('lyrics=1'));
    const withLyrics = await startKiosk('?sendspin=0&lyrics=1');
    assert.ok(withLyrics.sockets[0].includes('&lyrics=1'));
});

test('position heartbeats name the playback generation they describe', async () => {
    const result = await startKiosk('?sendspin=0', [], undefined, {}, { playAllowed: () => true });
    const socket = result.socketInstances[0];
    const message = data => socket.onmessage({ data: JSON.stringify(data) });
    message({ type: 'welcome', player_id: 'wk-pos' });
    message({ type: 'play', path: '/track', duration: 60, playback_generation: 'gen-7' });
    result.audioInstances[0].currentTime = 12;
    result.intervals.find(interval => interval.delay === 2000).callback();
    const report = socket.sent.filter(item => item.type === 'position').at(-1);
    assert.equal(report.position, 12);
    assert.equal(report.playback_generation, 'gen-7');
});

test('mute messages mute the output without changing the volume', async () => {
    const result = await startKiosk('?sendspin=0&controls=1', [], undefined, {}, { playAllowed: () => true });
    const socket = result.socketInstances[0];
    const message = data => socket.onmessage({ data: JSON.stringify(data) });
    message({ type: 'welcome', player_id: 'wk-mute' });
    const audio = result.audioInstances[0];
    message({ type: 'volume', level: 40 });
    message({ type: 'mute', muted: true });
    assert.equal(audio.muted, true);
    assert.equal(audio.volume, 0.4);
    message({ type: 'mute', muted: false });
    assert.equal(audio.muted, false);
    assert.equal(audio.volume, 0.4);
});

test('mute messages also mute an active Sendspin client', async () => {
    const result = await startKiosk('?controls=1', [], undefined, {}, { fakeSdk:true });
    result.socketInstances[0].onmessage({ data:JSON.stringify({ type:'welcome', player_id:'wk-ss-mute' }) });
    await new Promise(resolve => setImmediate(resolve));
    const player = result.context.__sendspinPlayer;
    result.socketInstances[0].onmessage({ data:JSON.stringify({ type:'mute', muted:true }) });
    assert.equal(player.muted, true);
    result.socketInstances[0].onmessage({ data:JSON.stringify({ type:'mute', muted:false }) });
    assert.equal(player.muted, false);
});
