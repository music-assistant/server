/**
 * Music Assistant — Web Player
 *
 * Renders MSX JSON content in a browser with sidebar + content layout.
 * Connects to the same /msx/* endpoints and /ws WebSocket as the MSX TV app.
 *
 * Audio modes:
 * - Normal mode: HTML5 streaming with WebSocket sync
 * - Kiosk mode (?kiosk=1): Fullscreen HTML5 player with WebSocket push
 * - Kiosk + Sendspin (?kiosk=1&sendspin=1): Sendspin synchronized audio
 *
 * URL Parameters:
 * - kiosk: Enable kiosk mode (e.g., ?kiosk=1)
 * - sendspin: Use Sendspin SDK in kiosk mode (e.g., ?kiosk=1&sendspin=1)
 * - sendspin_url: Custom Sendspin server URL (e.g., ?sendspin_url=http://ma:8927)
 * - controls / party / viz / lyrics: kiosk display toggles, on by default,
 *   "=0" disables (e.g., ?kiosk=1&controls=0&lyrics=0)
 */

// --- URL Parameters ---
const urlParams = new URLSearchParams(window.location.search);
const KIOSK_MODE = urlParams.get('kiosk') === '1';
const SENDSPIN_MODE = KIOSK_MODE && urlParams.get('sendspin') === '1';
const SENDSPIN_URL_PARAM = urlParams.get('sendspin_url') || '';

// Kiosk display toggles: every feature is on unless explicitly "=0"
function kioskFlag(name) { return urlParams.get(name) !== '0'; }
const KIOSK_SHOW_CONTROLS = kioskFlag('controls');
const KIOSK_SHOW_PARTY = kioskFlag('party');
const KIOSK_SHOW_VIZ = kioskFlag('viz');
const KIOSK_SHOW_LYRICS = kioskFlag('lyrics');

(function () {
    'use strict';

    // --- Constants ---
    var BASE = location.protocol + '//' + location.host;
    var WS_URL = (location.protocol === 'https:' ? 'wss:' : 'ws:') + '//' + location.host + '/ws';
    var DEVICE_KEY = 'ma_kiosk_device_id';
    var POS_INTERVAL = 3000;
    var SEARCH_DELAY = 400;

    // Sendspin only in kiosk mode
    var sendspinUrl = '';

    function getDefaultSendspinUrl() {
        var hostname = location.hostname;
        return 'http://' + hostname + ':8927';
    }

    function isSendspinMode() {
        return SENDSPIN_MODE;
    }

    function isKioskHtml5Mode() {
        return KIOSK_MODE && !SENDSPIN_MODE;
    }

    // --- Storage helpers (guard against SecurityError in private browsing / TV environments) ---
    function storageGet(key, fallback) {
        try { return localStorage.getItem(key) || fallback; } catch (e) { return fallback; }
    }
    function storageSet(key, val) {
        try { localStorage.setItem(key, val); } catch (e) {}
    }

    // --- Device ID ---
    function generateUUID() {
        if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
            return crypto.randomUUID();
        }
        if (typeof crypto !== 'undefined' && typeof crypto.getRandomValues === 'function') {
            return ([1e7]+-1e3+-4e3+-8e3+-1e11).replace(/[018]/g, function(c) {
                return (c ^ crypto.getRandomValues(new Uint8Array(1))[0] & 15 >> c / 4).toString(16);
            });
        }
        return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function(c) {
            var r = Math.random() * 16 | 0;
            return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
        });
    }

    var deviceId = storageGet(DEVICE_KEY, '');
    if (!deviceId) {
        deviceId = generateUUID();
        storageSet(DEVICE_KEY, deviceId);
    }
    var deviceParam = 'device_id=' + encodeURIComponent(deviceId) + '&source=web';

    // --- State ---
    var menuItems = [];
    var activeMenuIdx = -1;
    var navStack = [];
    var playlist = [];
    var trackIdx = -1;
    var ws = null;
    var wsRetry = 1000;
    var posTimer = null;
    var searchTimer = null;
    var pausedByWS = false;
    var resumedByWS = false;

    // Sendspin state (kiosk mode only)
    var sendspinPlayer = null;
    var sendspinReady = false;
    var progressInterval = null;

    // Kiosk auto-hide state
    var kioskHideTimer = null;
    var KIOSK_HIDE_DELAY = 3500;

    // Karaoke / lyrics state
    var lrcLines = [];          // [{time: float, text: string}]
    var currentLyricIdx = -1;
    var lyricsMode = 'none';    // 'none' | 'lrc' | 'plain'
    var lyricsFetchTimer = null;
    var currentPlayerId = '';   // player_id from last WS play/playlist message

    // Kiosk queue state
    var kioskQueueTimer = null;

    // --- DOM ---
    var audio = document.getElementById('audio');

    // --- Helpers ---
    function addParam(url, param) {
        if (!param || url.indexOf('device_id=') >= 0) return url;
        return url + (url.indexOf('?') >= 0 ? '&' : '?') + param;
    }

    function isHttpUrl(url) {
        return url.indexOf('http://') === 0 || url.indexOf('https://') === 0;
    }

    // Resolve a content/audio URL. The bridge only ever hands out URLs pointing
    // at itself, so anything targeting another host is rejected here.
    function resolveUrl(url) {
        if (!url) return '';
        if (isHttpUrl(url)) {
            try {
                var parsed = new URL(url);
                if (parsed.host !== location.host) return '';
                // rebuild from parsed parts so a "//evil.com/x" pathname can't change the host
                return BASE + parsed.pathname + parsed.search;
            } catch (e) {
                return '';
            }
        }
        if (url.charAt(0) === '/') return BASE + url;
        return '';
    }

    // Image URLs may legitimately point at remote CDNs (album art), so any
    // http(s) URL is allowed here — but never other schemes like javascript:.
    function safeImageUrl(url) {
        if (!url) return '';
        if (isHttpUrl(url)) return url;
        if (url.charAt(0) === '/') return BASE + url;
        return '';
    }

    function parseMsx(text) {
        if (!text) return '';
        return text.replace(/\{txt:[^:}]+:([^}]*)\}/g, '$1')
                   .replace(/\{ico:[^}]*\}\s*/g, '')
                   .trim();
    }

    function fmtDur(sec) {
        if (!sec || !isFinite(sec) || sec < 0) return '0:00';
        var m = Math.floor(sec / 60);
        var s = Math.floor(sec % 60);
        return m + ':' + (s < 10 ? '0' : '') + s;
    }

    function msxIcon(name) {
        if (!name) return '';
        var mapped = name.replace('msx-white-soft:', '').replace('msx-white:', '').replace(/-/g, '_');
        return '<span class="material-symbols-rounded">' + esc(mapped) + '</span>';
    }

    function esc(str) {
        if (!str) return '';
        var d = document.createElement('div');
        d.textContent = str;
        return d.innerHTML;
    }

    // --- Sendspin Integration (Kiosk Mode Only) ---
    async function initSendspin() {
        if (!KIOSK_MODE) return;

        sendspinUrl = SENDSPIN_URL_PARAM || getDefaultSendspinUrl();

        try {
            // Vendored @sendspin/sendspin-js (see scripts/vendor-sendspin-js.sh):
            // TVs on LAN-only setups have no CDN access. web.js is loaded as a
            // module, so the relative specifier resolves against this file's
            // URL (/web/web.js) and survives reverse-proxy path prefixes.
            var module = await import('./sendspin-js/index.js');
            var SendspinPlayer = module.SendspinPlayer;

            console.log('[Sendspin] SDK loaded, connecting to:', sendspinUrl);

            // When opened by the Sendspin bridge, connect with the bridge's
            // client id so the server upgrades the pre-registered client
            // instead of creating a new player.
            var bridgeClientId = urlParams.get('sendspin_client_id');
            var playerConfig = {
                playerId: bridgeClientId || ('web-kiosk-' + deviceId.substring(0, 8)),
                baseUrl: sendspinUrl,
                clientName: bridgeClientId ? 'MSX TV (Sendspin)' : 'Web Kiosk Player',
                correctionMode: 'sync',
                onStateChange: onSendspinStateChange
            };
            // Without WebCodecs the SDK's opus fallback needs the opus-encdec
            // package, which is not vendored (bare dynamic import, browser-
            // unloadable). Don't advertise opus so the server picks FLAC/PCM.
            if (typeof AudioDecoder === 'undefined') {
                playerConfig.codecs = ['flac', 'pcm'];
            }
            sendspinPlayer = new SendspinPlayer(playerConfig);

            await sendspinPlayer.connect();
            sendspinReady = true;
            console.log('[Sendspin] Connected successfully');

            updateSendspinStatus('connected');
            progressInterval = setInterval(updateSendspinProgress, 500);

        } catch (e) {
            console.error('[Sendspin] Failed to initialize:', e);
            updateSendspinStatus('error', e.message);
        }
    }

    function onSendspinStateChange(state) {
        console.log('[Sendspin] State changed:', state);

        syncPlayBtn();

        if (state.serverState && state.serverState.metadata) {
            var meta = state.serverState.metadata;
            updateKioskPlayer({
                title: meta.title || '',
                artist: meta.artist || '',
                image: meta.artwork_url || '',
                duration: meta.progress && meta.progress.track_duration
                    ? meta.progress.track_duration / 1000 : 0
            });
        }

        var syncInfo = sendspinPlayer.timeSyncInfo;
        updateSendspinStatus(syncInfo && syncInfo.synced ? 'synced' : 'syncing');
    }

    function updateSendspinProgress() {
        if (!sendspinPlayer || !sendspinReady) return;

        var progress = sendspinPlayer.trackProgress;
        if (progress) {
            var cur = progress.positionMs / 1000;
            var dur = progress.durationMs / 1000;

            var timeEl = document.getElementById('kiosk-time');
            var durEl = document.getElementById('kiosk-dur');
            var seekEl = document.getElementById('kiosk-seek');

            if (timeEl) timeEl.textContent = fmtDur(cur);
            if (durEl) durEl.textContent = fmtDur(dur);
            if (seekEl && dur > 0) seekEl.value = (cur / dur) * 100;

            // Also update bar/full player elements if they exist
            var barTime = document.getElementById('bar-time');
            var fullTime = document.getElementById('full-time');
            if (barTime) barTime.textContent = fmtDur(cur);
            if (fullTime) fullTime.textContent = fmtDur(cur);
        }
    }

    function updateSendspinStatus(status, msg) {
        var statusEl = document.getElementById('kiosk-sync-status');
        if (!statusEl) return;

        statusEl.className = 'sync-indicator';
        if (status === 'connected' || status === 'synced') {
            statusEl.classList.add('synced');
            statusEl.textContent = 'SYNC';
        } else if (status === 'syncing') {
            statusEl.classList.add('syncing');
            statusEl.textContent = 'SYNCING...';
        } else if (status === 'error') {
            statusEl.classList.add('error');
            statusEl.textContent = 'ERROR: ' + (msg || 'Connection failed');
        } else {
            statusEl.textContent = 'CONNECTING...';
        }
    }

    function updateKioskPlayer(track) {
        var bgImg = document.getElementById('kiosk-bg-img');
        var artCenter = document.getElementById('kiosk-art-center');
        var titleEl = document.getElementById('kiosk-title');
        var artistEl = document.getElementById('kiosk-artist');
        var durEl = document.getElementById('kiosk-dur');

        var imgUrl = safeImageUrl(track.image);
        if (bgImg) {
            if (imgUrl) {
                bgImg.src = imgUrl;
                bgImg.style.opacity = '1';
            } else {
                bgImg.style.opacity = '0';
            }
        }
        if (artCenter) {
            if (imgUrl) {
                artCenter.src = imgUrl;
                artCenter.style.display = '';
            } else {
                artCenter.style.display = 'none';
            }
        }
        if (titleEl) titleEl.textContent = track.title || '';
        if (artistEl) artistEl.textContent = track.artist || '';
        if (durEl) durEl.textContent = track.duration ? fmtDur(track.duration) : '';

        setKioskPlaying(true);
        resetKioskHideTimer();

        // Fetch lyrics for kiosk HTML5/Sendspin modes
        var pid = track.player_id || currentPlayerId;
        if ((isKioskHtml5Mode() || isSendspinMode()) && pid) {
            fetchLyrics(pid);
            fetchKioskQueue(pid);
        }

        // Also update full player
        updateFullPlayer(track);
    }

    // --- Sidebar Menu ---
    function buildMenu(data) {
        if (!data.items) return;
        var ul = document.getElementById('menu');
        ul.innerHTML = '';
        menuItems = [];

        data.items.forEach(function (item, idx) {
            var label = parseMsx(item.label || item.title || '');
            var icon = item.icon || '';
            var url = item.content || '';
            var isSearch = !!(
                (item.action && item.action.indexOf('search') >= 0) ||
                (url && url.indexOf('search') >= 0)
            );

            menuItems.push({ label: label, icon: icon, url: url, isSearch: isSearch });

            var li = document.createElement('li');
            li.className = 'menu-item';
            li.innerHTML = msxIcon(icon) + '<span>' + esc(label) + '</span>';
            li.addEventListener('click', function () { onMenuClick(idx); });
            ul.appendChild(li);
        });

        if (menuItems.length > 0) {
            onMenuClick(0);
        }
    }

    function onMenuClick(idx) {
        var item = menuItems[idx];
        if (!item) return;

        if (item.isSearch) {
            showSearch();
            return;
        }

        navStack = [];
        activeMenuIdx = idx;
        highlightMenu(idx);
        loadContent(item.url, item.label);
    }

    function highlightMenu(idx) {
        var items = document.querySelectorAll('.menu-item');
        items.forEach(function (el, i) {
            el.classList.toggle('active', i === idx);
        });
    }

    // --- Content Loading ---
    function loadContent(url, title, push) {
        if (push) {
            var ct = document.getElementById('content');
            navStack.push({ url: url, title: title || '', scrollY: ct ? ct.scrollTop : 0 });
        }
        updateContentHeader();
        showLoading(true);

        var fullUrl = addParam(resolveUrl(url), deviceParam);
        fetch(fullUrl)
            .then(function (r) { return r.json(); })
            .then(function (data) {
                var headline = parseMsx(data.headline) || title || '';
                if (navStack.length > 0) {
                    document.getElementById('content-title').textContent = headline;
                }
                renderContent(data);
                showLoading(false);
            })
            .catch(function (e) {
                console.error('Load failed:', e);
                showError('Failed to load content');
                showLoading(false);
            });
    }

    function drillDown(url, title) {
        loadContent(url, title, true);
    }

    function goBack() {
        if (!navStack.length) return;
        navStack.pop();
        if (navStack.length > 0) {
            var prev = navStack[navStack.length - 1];
            loadContent(prev.url, prev.title, false);
        } else {
            var item = menuItems[activeMenuIdx];
            if (item) loadContent(item.url, item.label, false);
        }
    }

    function updateContentHeader() {
        var hdr = document.getElementById('content-header');
        hdr.classList.toggle('visible', navStack.length > 0);
    }

    // --- Rendering ---
    function renderContent(data) {
        var el = document.getElementById('content');
        el.innerHTML = '';
        if (!data.items || !data.items.length) {
            el.innerHTML = '<div class="empty-state">Nothing here yet</div>';
            return;
        }
        var tpl = data.template || {};
        var layout = (tpl.layout || '0,0,3,4').split(',');
        var colSpan = parseInt(layout[2], 10) || 3;
        var rowSpan = parseInt(layout[3], 10) || 4;
        var isList = rowSpan <= 1 || (tpl.type === 'default' && colSpan >= 6);

        if (isList) {
            renderTrackList(el, data.items);
        } else {
            renderGrid(el, data.items, colSpan);
        }
        el.scrollTop = 0;
    }

    function renderGrid(container, items, colSpan) {
        var cols = Math.max(2, Math.floor(12 / colSpan));
        var grid = document.createElement('div');
        grid.className = 'content-grid';
        grid.style.setProperty('--cols', cols);

        items.forEach(function (item, i) {
            var card = document.createElement('div');
            card.className = 'card';
            card.style.animationDelay = (i * 25) + 'ms';

            var title = esc(parseMsx(item.titleHeader || item.title || item.label || ''));
            var sub = esc(item.titleFooter || '');

            // Build img container via DOM to safely assign src without innerHTML injection
            var imgContainer = document.createElement('div');
            var cardImgUrl = safeImageUrl(item.image);
            if (cardImgUrl) {
                imgContainer.className = 'card-img';
                var img = document.createElement('img');
                img.src = cardImgUrl;
                img.alt = '';
                img.loading = 'lazy';
                imgContainer.appendChild(img);
            } else {
                imgContainer.className = 'card-img card-img--empty';
                imgContainer.innerHTML = msxIcon(item.icon || 'music_note');
            }
            card.appendChild(imgContainer);
            card.insertAdjacentHTML('beforeend',
                '<div class="card-body">' +
                    '<div class="card-title">' + title + '</div>' +
                    (sub ? '<div class="card-sub">' + sub + '</div>' : '') +
                '</div>');

            card.addEventListener('click', function () { handleAction(item); });
            grid.appendChild(card);
        });
        container.appendChild(grid);
    }

    function renderTrackList(container, items) {
        var list = document.createElement('div');
        list.className = 'track-list';

        items.forEach(function (item, i) {
            var row = document.createElement('div');
            row.className = 'track-row';
            row.style.animationDelay = (i * 15) + 'ms';

            var title = esc(parseMsx(item.titleHeader || item.title || item.playerLabel || ''));
            var sub = esc(item.titleFooter || item.label || '');

            // Build art element via DOM to safely assign src without innerHTML injection
            var trackArtUrl = safeImageUrl(item.image);
            if (trackArtUrl) {
                var img = document.createElement('img');
                img.src = trackArtUrl;
                img.alt = '';
                img.className = 'track-art';
                img.loading = 'lazy';
                row.appendChild(img);
            } else {
                var emptyArt = document.createElement('div');
                emptyArt.className = 'track-art track-art--empty';
                emptyArt.innerHTML = msxIcon('audiotrack');
                row.appendChild(emptyArt);
            }
            row.insertAdjacentHTML('beforeend',
                '<div class="track-info">' +
                    '<div class="track-title">' + title + '</div>' +
                    (sub ? '<div class="track-sub">' + sub + '</div>' : '') +
                '</div>');

            row.addEventListener('click', function () { handleAction(item); });
            list.appendChild(row);
        });
        container.appendChild(list);
    }

    // --- Actions ---
    function handleAction(item) {
        var action = item.action || (item.content ? 'content:' + item.content : '');
        if (!action) return;

        if (action.indexOf('request:interaction:') >= 0 || action.indexOf('search-page') >= 0) {
            showSearch();
            return;
        }
        if (action.indexOf('content:') === 0) {
            var url = action.substring(8);
            if (url.indexOf('request:') === 0) return;
            var title = parseMsx(item.titleHeader || item.title || item.label || '');
            drillDown(url, title);
        } else if (action.indexOf('playlist:') === 0) {
            loadPlaylist(action.substring(9));
        } else if (action.indexOf('audio:') === 0) {
            playSingle(action.substring(6), item);
        }
    }

    // --- Audio (HTTP Stream mode - normal mode only) ---
    function playSingle(url, item) {
        if (isSendspinMode()) return;
        playlist = [makeTrack(url, item)];
        trackIdx = 0;
        playCurrent();
    }

    function loadPlaylist(url) {
        if (isSendspinMode()) return;
        var fullUrl = addParam(resolveUrl(url), deviceParam);
        fetch(fullUrl)
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (!data.items || !data.items.length) return;
                playlist = data.items.map(function (item) {
                    var aUrl = item.action ? item.action.replace(/^audio:/, '') : '';
                    return makeTrack(aUrl, item);
                });
                trackIdx = 0;
                playCurrent();
            })
            .catch(function (e) { console.error('Playlist load failed:', e); });
    }

    function makeTrack(url, item) {
        return {
            url: resolveUrl(url),
            title: parseMsx(item.titleHeader || item.title || item.playerLabel || ''),
            artist: item.titleFooter || item.label || '',
            image: item.image || item.background || '',
            duration: item.duration || 0
        };
    }

    function playCurrent() {
        if (isSendspinMode()) return;
        if (trackIdx < 0 || trackIdx >= playlist.length) return;
        var track = playlist[trackIdx];
        audio.src = track.url;
        // Muted autoplay is always allowed; unmute immediately after play starts.
        // This bypasses browser autoplay restrictions without requiring user interaction.
        audio.muted = true;
        audio.play().then(function () {
            audio.muted = false;
        }).catch(function (e) {
            console.warn('Autoplay blocked even when muted:', e);
            audio.muted = false;
            if (isKioskHtml5Mode()) {
                showKioskControls();
                cancelKioskHideTimer();
            }
        });

        if (isKioskHtml5Mode()) {
            updateKioskPlayer(track);
            var seekEl = document.getElementById('kiosk-seek');
            if (seekEl) seekEl.disabled = false;
        } else {
            updatePlayerBar(track);
        }
        updateFullPlayer(track);
        startPosReport();
        refreshQueueIfVisible();
    }

    function updatePlayerBar(track) {
        document.getElementById('player-bar').classList.add('active');
        document.getElementById('bar-title').textContent = track.title;
        document.getElementById('bar-artist').textContent = track.artist;
        var art = document.getElementById('player-art');
        var imgUrl = safeImageUrl(track.image);
        if (imgUrl) { art.src = imgUrl; art.style.display = ''; }
        else { art.style.display = 'none'; }
        document.getElementById('bar-dur').textContent = track.duration ? fmtDur(track.duration) : '';
        syncPlayBtn();
    }

    function updateFullPlayer(track) {
        var art = document.getElementById('full-art');
        var imgUrl = safeImageUrl(track.image);
        if (art) {
            if (imgUrl) { art.src = imgUrl; art.style.display = ''; }
            else { art.style.display = 'none'; }
        }
        var titleEl = document.getElementById('full-title');
        var artistEl = document.getElementById('full-artist');
        var durEl = document.getElementById('full-dur');
        if (titleEl) titleEl.textContent = track.title;
        if (artistEl) artistEl.textContent = track.artist;
        if (durEl) durEl.textContent = track.duration ? fmtDur(track.duration) : '';
    }

    function syncPlayBtn() {
        var isPlaying;
        if (isSendspinMode() && sendspinPlayer) {
            isPlaying = sendspinPlayer.isPlaying;
        } else {
            isPlaying = !audio.paused;
        }
        var icon = isPlaying ? 'pause' : 'play_arrow';
        var html = '<span class="material-symbols-rounded">' + icon + '</span>';

        var btnPlay = document.getElementById('btn-play');
        var fullPlay = document.getElementById('full-play');
        var kioskPlay = document.getElementById('kiosk-play');

        if (btnPlay) btnPlay.innerHTML = html;
        if (fullPlay) fullPlay.innerHTML = html;
        if (kioskPlay) kioskPlay.innerHTML = html;
    }

    // sendspin-js 3.x throws when the server's supported_commands list
    // excludes the command (1.x sent it unconditionally) — don't let that
    // kill the button handler.
    function sendspinCommand(cmd) {
        try {
            sendspinPlayer.sendCommand(cmd);
        } catch (e) {
            console.warn('[Sendspin] Command rejected:', cmd, e.message);
        }
    }

    function togglePlay() {
        if (isSendspinMode() && sendspinPlayer) {
            if (sendspinPlayer.isPlaying) {
                sendspinCommand('pause');
            } else {
                sendspinCommand('play');
            }
        } else {
            if (audio.paused) audio.play();
            else audio.pause();
        }
    }

    function nextTrack() {
        if (isSendspinMode() && sendspinPlayer) {
            sendspinCommand('next');
            return;
        }
        if (playlist.length <= 1) { stopPosReport(); return; }
        trackIdx = (trackIdx + 1) % playlist.length;
        playCurrent();
    }

    function prevTrack() {
        if (isSendspinMode() && sendspinPlayer) {
            sendspinCommand('previous');
            return;
        }
        if (!playlist.length) return;
        if (audio.currentTime > 3) { audio.currentTime = 0; return; }
        trackIdx = (trackIdx - 1 + playlist.length) % playlist.length;
        playCurrent();
    }

    // --- Progress ---
    function updateProgress() {
        if (isSendspinMode()) return;
        var cur = audio.currentTime;
        var dur = audio.duration || 0;

        if (isKioskHtml5Mode()) {
            var kioskTime = document.getElementById('kiosk-time');
            var kioskDur = document.getElementById('kiosk-dur');
            var kioskSeek = document.getElementById('kiosk-seek');
            if (kioskTime) kioskTime.textContent = fmtDur(cur);
            if (kioskDur && dur > 0) kioskDur.textContent = fmtDur(dur);
            if (kioskSeek && dur > 0) kioskSeek.value = (cur / dur) * 100;
            return;
        }

        document.getElementById('bar-time').textContent = fmtDur(cur);
        var fullTime = document.getElementById('full-time');
        if (fullTime) fullTime.textContent = fmtDur(cur);
        if (dur > 0) {
            var pct = (cur / dur) * 100;
            document.getElementById('bar-seek').value = pct;
            var fullSeek = document.getElementById('full-seek');
            if (fullSeek) fullSeek.value = pct;
        }
    }

    function seekTo(pct) {
        if (isSendspinMode()) return;
        var dur = audio.duration;
        if (dur && isFinite(dur)) audio.currentTime = (pct / 100) * dur;
    }

    // --- WebSocket ---
    function connectWS() {
        if (!window.WebSocket) return;

        var url = WS_URL + '?device_id=' + encodeURIComponent(deviceId) + '&source=web';
        ws = new WebSocket(url);
        var thisWs = ws;

        ws.onopen = function () { wsRetry = 1000; };

        ws.onmessage = function (ev) {
            try { handleWSMsg(JSON.parse(ev.data)); }
            catch (e) { console.warn('WS message error:', e); }
        };

        ws.onclose = function (ev) {
            if (ws !== thisWs) return;
            ws = null;
            if (ev.code !== 1000 && ev.code !== 1001) {
                var jitter = Math.random() * 1000;
                setTimeout(connectWS, wsRetry + jitter);
                wsRetry = Math.min(wsRetry * 2, 10000);
            }
        };
    }

    function handleWSMsg(msg) {
        switch (msg.type) {
            case 'play':
                if (msg.path) {
                    if (msg.player_id) currentPlayerId = msg.player_id;
                    var track = {
                        url: BASE + msg.path,
                        title: msg.title || '',
                        artist: msg.artist || '',
                        image: msg.image_url || '',
                        duration: msg.duration || 0,
                        player_id: msg.player_id || ''
                    };
                    playlist = [track];
                    trackIdx = 0;
                    playCurrent();
                }
                break;
            case 'stop':
                audio.pause();
                audio.removeAttribute('src');
                if (isKioskHtml5Mode()) {
                    document.getElementById('kiosk-title').textContent = '';
                    document.getElementById('kiosk-artist').textContent = '';
                    document.getElementById('kiosk-time').textContent = '0:00';
                    document.getElementById('kiosk-dur').textContent = '0:00';
                    document.getElementById('kiosk-seek').value = 0;
                    document.getElementById('kiosk-seek').disabled = true;
                    var bgImg = document.getElementById('kiosk-bg-img');
                    if (bgImg) bgImg.style.opacity = '0';
                    var artCenter = document.getElementById('kiosk-art-center');
                    if (artCenter) artCenter.src = '';
                    clearLyrics();
                    setKioskPlaying(false);
                    cancelKioskHideTimer();
                    hideKioskControls();
                    // Clear kiosk queue
                    var kql = document.getElementById('kiosk-queue-list');
                    if (kql) kql.innerHTML = '<div class="kiosk-queue-empty">No tracks in queue</div>';
                } else {
                    document.getElementById('player-bar').classList.remove('active');
                }
                stopPosReport();
                syncPlayBtn();
                break;
            case 'pause':
                pausedByWS = true;
                audio.pause();
                break;
            case 'resume':
                resumedByWS = true;
                audio.play();
                break;
            case 'playlist':
                if (msg.url) {
                    if (msg.player_id) currentPlayerId = msg.player_id;
                    loadPlaylist(msg.url);
                }
                break;
            case 'goto_index':
                if (msg.index != null && msg.index < playlist.length) {
                    trackIdx = msg.index;
                    playCurrent();
                }
                break;
        }
    }

    function sendWS(obj) {
        if (ws && ws.readyState === WebSocket.OPEN) {
            try { ws.send(JSON.stringify(obj)); } catch (e) { /* noop */ }
        }
    }

    function startPosReport() {
        if (isSendspinMode()) return;
        stopPosReport();
        posTimer = setInterval(function () {
            sendWS({ type: 'position', position: audio.currentTime });
        }, POS_INTERVAL);
    }

    function stopPosReport() {
        if (posTimer) { clearInterval(posTimer); posTimer = null; }
    }

    // --- Search ---
    function showSearch() {
        document.getElementById('search-overlay').classList.add('active');
        document.getElementById('search-input').focus();
    }

    function hideSearch() {
        document.getElementById('search-overlay').classList.remove('active');
        document.getElementById('search-input').value = '';
    }

    function doSearch(q) {
        if (!q) return;
        hideSearch();
        activeMenuIdx = -1;
        highlightMenu(-1);
        navStack = [];
        updateContentHeader();
        loadContent('/msx/search-input.json?q=' + encodeURIComponent(q), 'Search: ' + q, false);
    }

    // --- Lyrics offset: keep lyrics above the controls panel using measured panel height ---
    function updateLyricsOffset() {
        var panel = document.getElementById('kiosk-controls-panel');
        var panelH = panel ? panel.offsetHeight : 140;
        document.documentElement.style.setProperty('--lyrics-bottom-offset', panelH + 'px');
    }

    // --- Kiosk display toggles (URL params) ---
    function applyKioskDisplayFlags() {
        var kp = document.getElementById('kiosk-player');
        if (!kp) return;
        if (!KIOSK_SHOW_CONTROLS) kp.classList.add('kiosk-hide-controls');
        if (!KIOSK_SHOW_PARTY) kp.classList.add('kiosk-hide-party');
        if (!KIOSK_SHOW_VIZ) kp.classList.add('kiosk-hide-viz');
        if (!KIOSK_SHOW_LYRICS) kp.classList.add('kiosk-hide-lyrics');
    }

    // --- Kiosk Auto-Hide Controls ---
    function showKioskControls() {
        if (!KIOSK_SHOW_CONTROLS) return;
        var kp = document.getElementById('kiosk-player');
        if (kp) kp.classList.remove('controls-hidden');
        document.body.classList.add('controls-visible');
        updateLyricsOffset();
    }
    function hideKioskControls() {
        var kp = document.getElementById('kiosk-player');
        if (kp) kp.classList.add('controls-hidden');
        document.body.classList.remove('controls-visible');
        updateLyricsOffset();
    }
    function resetKioskHideTimer() {
        showKioskControls();
        clearTimeout(kioskHideTimer);
        kioskHideTimer = setTimeout(hideKioskControls, KIOSK_HIDE_DELAY);
    }
    function cancelKioskHideTimer() {
        clearTimeout(kioskHideTimer);
        kioskHideTimer = null;
    }
    function setKioskPlaying(on) {
        var kp = document.getElementById('kiosk-player');
        if (kp) kp.classList.toggle('playing', on);
    }

    // --- Karaoke Lyrics ---
    function parseLrc(text) {
        var lines = text.split('\n');
        var result = [];
        lines.forEach(function(line) {
            var m = line.match(/^\[(\d{1,2}):(\d{2}(?:\.\d+)?)\](.*)/);
            if (m) {
                var t = parseInt(m[1], 10) * 60 + parseFloat(m[2]);
                var txt = m[3].trim();
                if (txt) result.push({ time: t, text: txt });
            }
        });
        return result.sort(function(a, b) { return a.time - b.time; });
    }

    function renderLyrics(data) {
        var inner = document.getElementById('kiosk-lyrics-inner');
        var kp = document.getElementById('kiosk-player');
        if (!inner || !kp) return;

        inner.innerHTML = '';
        inner.style.transform = '';
        lrcLines = [];
        currentLyricIdx = -1;

        if (data.lrc_lyrics) {
            lyricsMode = 'lrc';
            lrcLines = parseLrc(data.lrc_lyrics);
            lrcLines.forEach(function(l) {
                var div = document.createElement('div');
                div.className = 'lyric-line';
                div.textContent = l.text;
                inner.appendChild(div);
            });
            kp.classList.add('has-lyrics');
            kp.classList.remove('plain-lyrics');
        } else if (data.lyrics) {
            lyricsMode = 'plain';
            data.lyrics.split('\n').forEach(function(l) {
                var div = document.createElement('div');
                div.className = 'lyric-line';
                div.textContent = l;
                inner.appendChild(div);
            });
            kp.classList.add('has-lyrics', 'plain-lyrics');
        } else {
            lyricsMode = 'none';
            kp.classList.remove('has-lyrics', 'plain-lyrics');
        }
    }

    function syncLyrics(currentTime) {
        if (lyricsMode !== 'lrc' || !lrcLines.length) return;

        var idx = -1;
        for (var i = 0; i < lrcLines.length; i++) {
            if (lrcLines[i].time <= currentTime) idx = i;
            else break;
        }
        if (idx === currentLyricIdx) return;
        currentLyricIdx = idx;

        var inner = document.getElementById('kiosk-lyrics-inner');
        var scroll = document.getElementById('kiosk-lyrics-scroll');
        if (!inner || !scroll) return;

        var lines = inner.querySelectorAll('.lyric-line');
        lines.forEach(function(el, i) {
            el.classList.remove('active', 'lp1', 'ln1');
            if (i === idx) el.classList.add('active');
            else if (i === idx - 1) el.classList.add('lp1');
            else if (i === idx + 1) el.classList.add('ln1');
        });

        if (idx >= 0 && lines[idx]) {
            var lineTop = lines[idx].offsetTop;
            var lineHeight = lines[idx].offsetHeight;
            var scrollH = scroll.offsetHeight;
            var target = lineTop - (scrollH / 2) + (lineHeight / 2);
            inner.style.transform = 'translateY(' + (-target) + 'px)';
        }
    }

    function clearLyrics() {
        clearTimeout(lyricsFetchTimer);
        lyricsFetchTimer = null;
        lrcLines = [];
        currentLyricIdx = -1;
        lyricsMode = 'none';
        var inner = document.getElementById('kiosk-lyrics-inner');
        if (inner) { inner.innerHTML = ''; inner.style.transform = ''; }
        var kp = document.getElementById('kiosk-player');
        if (kp) kp.classList.remove('has-lyrics', 'plain-lyrics');
    }

    function fetchLyrics(playerId) {
        if (KIOSK_MODE && !KIOSK_SHOW_LYRICS) return;
        clearTimeout(lyricsFetchTimer);
        lyricsFetchTimer = setTimeout(function() {
            fetch('/api/lyrics/' + encodeURIComponent(playerId))
                .then(function(r) { return r.json(); })
                .then(function(data) { renderLyrics(data); })
                .catch(function() { /* no lyrics available */ });
        }, 400);
    }

    // --- CSS Text Equalizer ---
    var EQ_BAR_COUNT = 32;

    function buildEqualizer() {
        var container = document.getElementById('eq-bars');
        if (!container || container.children.length > 0) return;
        for (var i = 0; i < EQ_BAR_COUNT; i++) {
            var bar = document.createElement('div');
            bar.className = 'eq-bar';
            // Randomize animation parameters for organic look (CSS fallback)
            var dur = (0.6 + Math.random() * 0.9).toFixed(2);
            var delay = (Math.random() * 0.8).toFixed(2);
            var minH = (4 + Math.random() * 8).toFixed(0);
            var maxH = (40 + Math.random() * 160).toFixed(0);
            bar.style.setProperty('--eq-dur', dur + 's');
            bar.style.setProperty('--eq-delay', delay + 's');
            bar.style.setProperty('--eq-min', minH + 'px');
            bar.style.setProperty('--eq-max', maxH + 'px');
            container.appendChild(bar);
        }
    }

    // --- Real spectrum visualizer (HTTP kiosk only) ---
    // Sendspin decodes/schedules audio inside its SDK with no exposed audio
    // graph, so a live analyzer is only possible in HTTP mode where playback
    // goes through our own <audio> element. Any Web Audio failure leaves the
    // decorative CSS animation in place.
    var audioAnalyser = null;
    var audioAnalyserData = null;
    var audioSourceNode = null;
    var vizRaf = null;

    function setupVisualizer() {
        if (!isKioskHtml5Mode() || audioAnalyser) return;
        var Ctx = window.AudioContext || window.webkitAudioContext;
        if (!Ctx || !audio) return;
        try {
            var ctx = new Ctx();
            // MediaElementSource may be created only once per element.
            audioSourceNode = ctx.createMediaElementSource(audio);
            audioAnalyser = ctx.createAnalyser();
            audioAnalyser.fftSize = 128;
            audioAnalyser.smoothingTimeConstant = 0.8;
            // Keep the output audible — the analyzer is a tap, not a sink.
            audioSourceNode.connect(audioAnalyser);
            audioAnalyser.connect(ctx.destination);
            audioAnalyserData = new Uint8Array(audioAnalyser.frequencyBinCount);
            audioAnalyser._ctx = ctx;
        } catch (e) {
            console.warn('[Visualizer] Web Audio unavailable, using CSS fallback:', e);
            audioAnalyser = null;
        }
    }

    function startVisualizer() {
        if (!KIOSK_SHOW_VIZ) return;
        setupVisualizer();
        if (!audioAnalyser) return;
        // A suspended context (autoplay policy) never produces data until resumed.
        if (audioAnalyser._ctx && audioAnalyser._ctx.state === 'suspended') {
            audioAnalyser._ctx.resume().catch(function () {});
        }
        var container = document.getElementById('eq-bars');
        if (!container) return;
        // Real data drives inline heights; disable the keyframe animation.
        container.classList.add('eq-live');
        if (vizRaf) return;
        var bars = container.children;
        var bins = audioAnalyserData.length;

        function frame() {
            vizRaf = requestAnimationFrame(frame);
            audioAnalyser.getByteFrequencyData(audioAnalyserData);
            for (var i = 0; i < bars.length; i++) {
                // Log-ish bin mapping: low bars from low bins, spread the rest.
                var idx = Math.min(bins - 1, Math.floor((i / bars.length) * bins));
                var mag = audioAnalyserData[idx] / 255; // 0..1
                var h = Math.max(4, Math.round(mag * mag * 320));
                bars[i].style.height = h + 'px';
            }
        }
        frame();
    }

    function stopVisualizer() {
        if (vizRaf) {
            cancelAnimationFrame(vizRaf);
            vizRaf = null;
        }
    }

    // --- Party Mode (kiosk QR overlay) ---
    var PARTY_POLL_INTERVAL = 30000;

    function updatePartyOverlay() {
        fetch('/api/party')
            .then(function (r) {
                if (!r.ok) throw new Error('HTTP ' + r.status);
                return r.json();
            })
            .then(function (data) {
                var panel = document.getElementById('kiosk-party');
                if (!panel) return;
                var qrUrl = data && data.active ? resolveUrl(data.qr_url) : '';
                if (!qrUrl) { panel.hidden = true; return; }
                // the version param changes only when the join code rotates, so the
                // TV refetches the image exactly then (no flicker on idle polls)
                var src = addParam(qrUrl, 'v=' + encodeURIComponent(data.qr_version || ''));
                var img = document.getElementById('kiosk-party-qr');
                if (img.src !== src) img.src = src;
                document.getElementById('kiosk-party-name').textContent = data.name || '';
                document.getElementById('kiosk-party-text').textContent =
                    data.qr_text || 'Scan to join the party';
                panel.hidden = false;
            })
            .catch(function (e) {
                // keep the last shown state on transient network errors
                console.warn('Party status fetch failed:', e);
            });
    }

    function startPartyPolling() {
        updatePartyOverlay();
        setInterval(updatePartyOverlay, PARTY_POLL_INTERVAL);
    }

    // --- Kiosk Queue ---
    function fetchKioskQueue(playerId) {
        clearTimeout(kioskQueueTimer);
        kioskQueueTimer = setTimeout(function() {
            fetch('/api/queue/' + encodeURIComponent(playerId))
                .then(function(r) { return r.json(); })
                .then(function(data) { renderKioskQueue(data); })
                .catch(function(e) { console.warn('Kiosk queue fetch failed:', e); });
        }, 250);
    }

    function renderKioskQueue(data) {
        var container = document.getElementById('kiosk-queue-list');
        if (!container) return;
        container.innerHTML = '';

        if (!data.items || !data.items.length) {
            container.innerHTML = '<div class="kiosk-queue-empty">No tracks in queue</div>';
            return;
        }

        var currentIdx = data.current_index;

        data.items.forEach(function(item, i) {
            var row = document.createElement('div');
            row.className = 'kiosk-queue-item' + (i === currentIdx ? ' active' : '');

            var numEl = '<div class="kiosk-queue-num">';
            if (i === currentIdx) {
                numEl += '<span class="material-symbols-rounded" style="font-size:14px">play_arrow</span>';
            } else {
                numEl += (i + 1);
            }
            numEl += '</div>';

            var durStr = item.duration ? fmtDur(item.duration) : '';

            // Set static HTML first (no user-data), then append img via DOM
            row.innerHTML =
                numEl +
                (item.image ? '<span class="kiosk-queue-art-slot"></span>' : '<div class="kiosk-queue-art--empty"><span class="material-symbols-rounded" style="font-size:16px">audiotrack</span></div>') +
                '<div class="kiosk-queue-info">' +
                    '<div class="kiosk-queue-title">' + esc(item.title || '') + '</div>' +
                    '<div class="kiosk-queue-sub">' + esc(item.artist || '') + '</div>' +
                '</div>' +
                '<div class="kiosk-queue-dur">' + durStr + '</div>';

            var kioskArtUrl = safeImageUrl(item.image);
            if (kioskArtUrl) {
                var img = document.createElement('img');
                img.src = kioskArtUrl;
                img.alt = '';
                img.className = 'kiosk-queue-art';
                img.loading = 'lazy';
                var slot = row.querySelector('.kiosk-queue-art-slot');
                slot.parentNode.replaceChild(img, slot);
            }

            container.appendChild(row);
        });

        // Scroll current track into view
        if (currentIdx >= 0) {
            var activeEl = container.querySelector('.kiosk-queue-item.active');
            if (activeEl) {
                activeEl.scrollIntoView({ block: 'center', behavior: 'smooth' });
            }
        }
    }

    // --- Player Mode ---
    function toggleMode() {
        var full = document.getElementById('player-full');
        full.classList.toggle('active');
        if (full.classList.contains('active') && currentPlayerId) {
            fetchQueue(currentPlayerId);
        }
    }

    // --- Queue Panel ---
    var queueFetchTimer = null;

    function fetchQueue(playerId) {
        clearTimeout(queueFetchTimer);
        queueFetchTimer = setTimeout(function() {
            fetch('/api/queue/' + encodeURIComponent(playerId))
                .then(function(r) { return r.json(); })
                .then(function(data) { renderQueue(data); })
                .catch(function(e) { console.warn('Queue fetch failed:', e); });
        }, 200);
    }

    function renderQueue(data) {
        var container = document.getElementById('queue-list');
        if (!container) return;
        container.innerHTML = '';

        if (!data.items || !data.items.length) {
            container.innerHTML = '<div class="queue-empty">No tracks in queue</div>';
            return;
        }

        var currentIdx = data.current_index;

        data.items.forEach(function(item, i) {
            var row = document.createElement('div');
            row.className = 'queue-item' + (i === currentIdx ? ' active' : '');

            var numEl = '<div class="queue-item-num">';
            if (i === currentIdx) {
                numEl += '<span class="material-symbols-rounded" style="font-size:16px">play_arrow</span>';
            } else {
                numEl += (i + 1);
            }
            numEl += '</div>';

            var durStr = item.duration ? fmtDur(item.duration) : '';

            row.innerHTML =
                numEl +
                '<div class="queue-item-info">' +
                    '<div class="queue-item-title">' + esc(item.title || '') + '</div>' +
                    '<div class="queue-item-sub">' + esc(item.artist || '') + '</div>' +
                '</div>' +
                '<div class="queue-item-dur">' + durStr + '</div>';

            // Build img element via DOM to safely assign src without innerHTML injection
            var artEl;
            var artUrl = safeImageUrl(item.image);
            if (artUrl) {
                artEl = document.createElement('img');
                artEl.src = artUrl;
                artEl.alt = '';
                artEl.className = 'queue-item-art';
                artEl.loading = 'lazy';
            } else {
                artEl = document.createElement('div');
                artEl.className = 'queue-item-art--empty';
                artEl.innerHTML = '<span class="material-symbols-rounded" style="font-size:18px">audiotrack</span>';
            }
            row.insertBefore(artEl, row.querySelector('.queue-item-info'));

            container.appendChild(row);
        });

        // Scroll current track into view
        if (currentIdx >= 0) {
            var activeEl = container.querySelector('.queue-item.active');
            if (activeEl) {
                activeEl.scrollIntoView({ block: 'center', behavior: 'smooth' });
            }
        }
    }

    function refreshQueueIfVisible() {
        var full = document.getElementById('player-full');
        if (full && full.classList.contains('active') && currentPlayerId) {
            fetchQueue(currentPlayerId);
        }
        // Also refresh kiosk queue if in kiosk mode
        if (KIOSK_MODE && currentPlayerId) {
            fetchKioskQueue(currentPlayerId);
        }
    }

    // --- UI Helpers ---
    function showLoading(on) { document.getElementById('loading').classList.toggle('active', on); }
    function showError(msg) { document.getElementById('content').innerHTML = '<div class="empty-state">' + esc(msg) + '</div>'; }

    // --- Init ---
    async function init() {
        if (SENDSPIN_MODE) {
            // Kiosk + Sendspin mode: synchronized audio via Sendspin SDK
            document.body.classList.add('kiosk-mode');
            applyKioskDisplayFlags();

            // Build CSS equalizer bars
            buildEqualizer();

            await initSendspin();

            // Setup kiosk controls
            var kioskPlay = document.getElementById('kiosk-play');
            var kioskPrev = document.getElementById('kiosk-prev');
            var kioskNext = document.getElementById('kiosk-next');

            if (kioskPlay) kioskPlay.addEventListener('click', togglePlay);
            if (kioskPrev) kioskPrev.addEventListener('click', prevTrack);
            if (kioskNext) kioskNext.addEventListener('click', nextTrack);

            updateLyricsOffset();
            window.addEventListener('resize', updateLyricsOffset);

            if (KIOSK_SHOW_PARTY) startPartyPolling();

            console.log('[WebPlayer] Kiosk mode initialized with Sendspin');
        } else if (KIOSK_MODE) {
            // Kiosk HTML5 mode: fullscreen player with WebSocket push + HTML5 Audio
            document.body.classList.add('kiosk-mode');
            applyKioskDisplayFlags();

            // Build CSS equalizer bars
            buildEqualizer();

            // Hide sync indicator (not used in HTML5 mode)
            var syncStatus = document.getElementById('kiosk-sync-status');
            if (syncStatus) syncStatus.style.display = 'none';

            connectWS();

            // HTML5 Audio events → update kiosk UI
            audio.addEventListener('timeupdate', function() {
                updateProgress();
                syncLyrics(audio.currentTime);
            });
            audio.addEventListener('ended', nextTrack);
            audio.addEventListener('pause', function () {
                syncPlayBtn();
                stopVisualizer();
                if (pausedByWS) { pausedByWS = false; return; }
                sendWS({ type: 'pause', position: audio.currentTime });
                stopPosReport();
            });
            audio.addEventListener('play', function () {
                syncPlayBtn();
                startVisualizer();
                if (resumedByWS) { resumedByWS = false; return; }
                sendWS({ type: 'resume' });
                startPosReport();
            });

            // Kiosk controls → HTML5 Audio
            var kioskPlay = document.getElementById('kiosk-play');
            var kioskPrev = document.getElementById('kiosk-prev');
            var kioskNext = document.getElementById('kiosk-next');
            var kioskSeek = document.getElementById('kiosk-seek');

            if (kioskPlay) kioskPlay.addEventListener('click', togglePlay);
            if (kioskPrev) kioskPrev.addEventListener('click', prevTrack);
            if (kioskNext) kioskNext.addEventListener('click', nextTrack);
            if (kioskSeek) {
                kioskSeek.addEventListener('input', function (e) { seekTo(e.target.value); });
            }

            // Auto-hide controls on inactivity
            var kc = document.getElementById('kiosk-player');
            if (kc) {
                kc.addEventListener('mousemove', resetKioskHideTimer);
                kc.addEventListener('touchstart', resetKioskHideTimer, { passive: true });
                kc.addEventListener('click', resetKioskHideTimer);
            }
            hideKioskControls(); // start in hidden state
            updateLyricsOffset();
            window.addEventListener('resize', updateLyricsOffset);

            if (KIOSK_SHOW_PARTY) startPartyPolling();

            console.log('[WebPlayer] Kiosk mode initialized with HTML5 streaming');
        } else {
            // Normal mode: HTML5 streaming with WebSocket
            connectWS();

            // Audio events
            audio.addEventListener('timeupdate', updateProgress);
            audio.addEventListener('ended', nextTrack);
            audio.addEventListener('pause', function () {
                syncPlayBtn();
                if (pausedByWS) { pausedByWS = false; return; }
                sendWS({ type: 'pause', position: audio.currentTime });
                stopPosReport();
            });
            audio.addEventListener('play', function () {
                syncPlayBtn();
                if (resumedByWS) { resumedByWS = false; return; }
                sendWS({ type: 'resume' });
                startPosReport();
            });

            // Bar controls
            document.getElementById('btn-play').addEventListener('click', togglePlay);
            document.getElementById('btn-prev').addEventListener('click', prevTrack);
            document.getElementById('btn-next').addEventListener('click', nextTrack);
            document.getElementById('bar-seek').addEventListener('input', function (e) { seekTo(e.target.value); });
            document.getElementById('bar-info').addEventListener('click', function () {
                if (playlist.length) toggleMode();
            });

            // Full player controls
            var fullPlay = document.getElementById('full-play');
            var fullPrev = document.getElementById('full-prev');
            var fullNext = document.getElementById('full-next');
            var fullSeek = document.getElementById('full-seek');
            var fullBrowse = document.getElementById('full-browse');

            if (fullPlay) fullPlay.addEventListener('click', togglePlay);
            if (fullPrev) fullPrev.addEventListener('click', prevTrack);
            if (fullNext) fullNext.addEventListener('click', nextTrack);
            if (fullSeek) fullSeek.addEventListener('input', function (e) { seekTo(e.target.value); });
            if (fullBrowse) fullBrowse.addEventListener('click', toggleMode);

            // Back button
            document.getElementById('btn-back').addEventListener('click', goBack);

            // Search
            document.getElementById('search-close').addEventListener('click', hideSearch);
            document.getElementById('search-input').addEventListener('input', function (e) {
                clearTimeout(searchTimer);
                var val = e.target.value;
                searchTimer = setTimeout(function () { doSearch(val); }, SEARCH_DELAY);
            });
            document.getElementById('search-input').addEventListener('keydown', function (e) {
                if (e.key === 'Enter') { clearTimeout(searchTimer); doSearch(e.target.value); }
                if (e.key === 'Escape') hideSearch();
            });

            // Load menu
            var menuUrl = addParam('/msx/menu.json', deviceParam);
            fetch(resolveUrl(menuUrl))
                .then(function (r) { return r.json(); })
                .then(function (data) { buildMenu(data); })
                .catch(function (e) { console.error('Menu load failed:', e); });

            console.log('[WebPlayer] Normal mode initialized with HTML5 streaming');
        }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
