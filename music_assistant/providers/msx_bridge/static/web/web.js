/**
 * Music Assistant — Kiosk Web Player
 *
 * Renders MSX JSON content in a browser with sidebar + content layout.
 * Connects to the same /msx/* endpoints and /ws WebSocket as the MSX TV app.
 */
(function () {
    'use strict';

    // --- Constants ---
    var BASE = location.protocol + '//' + location.host;
    var WS_URL = (location.protocol === 'https:' ? 'wss:' : 'ws:') + '//' + location.host + '/ws';
    var DEVICE_KEY = 'ma_kiosk_device_id';
    var POS_INTERVAL = 3000;
    var SEARCH_DELAY = 400;

    // --- Device ID ---
    var deviceId = localStorage.getItem(DEVICE_KEY);
    if (!deviceId) {
        deviceId = crypto.randomUUID();
        localStorage.setItem(DEVICE_KEY, deviceId);
    }
    var deviceParam = 'device_id=' + encodeURIComponent(deviceId) + '&source=web';

    // --- State ---
    var menuItems = [];         // sidebar menu entries [{label, icon, url, isSearch}]
    var activeMenuIdx = -1;     // currently selected sidebar item
    var navStack = [];          // drill-down history within the content pane
    var playlist = [];
    var trackIdx = -1;
    var ws = null;
    var wsRetry = 1000;
    var posTimer = null;
    var searchTimer = null;
    var pausedByWS = false;
    var resumedByWS = false;

    // --- DOM ---
    var audio = document.getElementById('audio');

    // --- Helpers ---
    function addParam(url, param) {
        if (!param || url.indexOf('device_id=') >= 0) return url;
        return url + (url.indexOf('?') >= 0 ? '&' : '?') + param;
    }

    function resolveUrl(url) {
        if (!url) return '';
        if (url.indexOf('http://') === 0 || url.indexOf('https://') === 0) return url;
        if (url.charAt(0) === '/') return BASE + url;
        return url;
    }

    function parseMsx(text) {
        if (!text) return '';
        return text.replace(/\{txt:[^:}]+:([^}]*)\}/g, '$1')
                   .replace(/\{ico:[^}]*\}\s*/g, '')
                   .trim();
    }

    function fmtDur(sec) {
        if (!sec || !isFinite(sec) || sec < 0) return '';
        var m = Math.floor(sec / 60);
        var s = Math.floor(sec % 60);
        return m + ':' + (s < 10 ? '0' : '') + s;
    }

    function msxIcon(name) {
        if (!name) return '';
        var mapped = name.replace('msx-white-soft:', '').replace('msx-white:', '').replace(/-/g, '_');
        return '<span class="material-symbols-rounded">' + mapped + '</span>';
    }

    function esc(str) {
        if (!str) return '';
        var d = document.createElement('div');
        d.textContent = str;
        return d.innerHTML;
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

        // Auto-select first item
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

        // Clear drill-down stack when switching menu sections
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
                    // Update the current entry's title if we pushed
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
            // Return to menu-level content
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
            var imgHtml = item.image
                ? '<div class="card-img"><img src="' + esc(item.image) + '" alt="" loading="lazy"></div>'
                : '<div class="card-img card-img--empty">' + msxIcon(item.icon || 'music_note') + '</div>';

            card.innerHTML = imgHtml +
                '<div class="card-body">' +
                    '<div class="card-title">' + title + '</div>' +
                    (sub ? '<div class="card-sub">' + sub + '</div>' : '') +
                '</div>';

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
            var imgHtml = item.image
                ? '<img src="' + esc(item.image) + '" alt="" class="track-art" loading="lazy">'
                : '<div class="track-art track-art--empty">' + msxIcon('audiotrack') + '</div>';

            row.innerHTML =
                imgHtml +
                '<div class="track-info">' +
                    '<div class="track-title">' + title + '</div>' +
                    (sub ? '<div class="track-sub">' + sub + '</div>' : '') +
                '</div>';

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

    // --- Audio ---
    function playSingle(url, item) {
        playlist = [makeTrack(url, item)];
        trackIdx = 0;
        playCurrent();
    }

    function loadPlaylist(url) {
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
        if (trackIdx < 0 || trackIdx >= playlist.length) return;
        var track = playlist[trackIdx];
        audio.src = track.url;
        audio.play().catch(function (e) { console.warn('Autoplay blocked:', e); });
        updatePlayerBar(track);
        updateFullPlayer(track);
        startPosReport();
    }

    function updatePlayerBar(track) {
        document.getElementById('player-bar').classList.add('active');
        document.getElementById('bar-title').textContent = track.title;
        document.getElementById('bar-artist').textContent = track.artist;
        var art = document.getElementById('player-art');
        if (track.image) { art.src = track.image; art.style.display = ''; }
        else { art.style.display = 'none'; }
        document.getElementById('bar-dur').textContent = track.duration ? fmtDur(track.duration) : '';
        syncPlayBtn();
    }

    function updateFullPlayer(track) {
        var art = document.getElementById('full-art');
        if (track.image) { art.src = track.image; art.style.display = ''; }
        else { art.style.display = 'none'; }
        document.getElementById('full-title').textContent = track.title;
        document.getElementById('full-artist').textContent = track.artist;
        document.getElementById('full-dur').textContent = track.duration ? fmtDur(track.duration) : '';
    }

    function syncPlayBtn() {
        var icon = audio.paused ? 'play_arrow' : 'pause';
        var html = '<span class="material-symbols-rounded">' + icon + '</span>';
        document.getElementById('btn-play').innerHTML = html;
        document.getElementById('full-play').innerHTML = html;
    }

    function togglePlay() {
        if (audio.paused) audio.play();
        else audio.pause();
    }

    function nextTrack() {
        if (playlist.length <= 1) { stopPosReport(); return; }
        trackIdx = (trackIdx + 1) % playlist.length;
        playCurrent();
    }

    function prevTrack() {
        if (!playlist.length) return;
        if (audio.currentTime > 3) { audio.currentTime = 0; return; }
        trackIdx = (trackIdx - 1 + playlist.length) % playlist.length;
        playCurrent();
    }

    // --- Progress ---
    function updateProgress() {
        var cur = audio.currentTime;
        var dur = audio.duration || 0;
        document.getElementById('bar-time').textContent = fmtDur(cur);
        document.getElementById('full-time').textContent = fmtDur(cur);
        if (dur > 0) {
            var pct = (cur / dur) * 100;
            document.getElementById('bar-seek').value = pct;
            document.getElementById('full-seek').value = pct;
        }
    }

    function seekTo(pct) {
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
                    var track = {
                        url: BASE + msg.path + '.mp3',
                        title: msg.title || '',
                        artist: msg.artist || '',
                        image: msg.image_url || '',
                        duration: msg.duration || 0
                    };
                    playlist = [track];
                    trackIdx = 0;
                    playCurrent();
                }
                break;
            case 'stop':
                audio.pause();
                audio.removeAttribute('src');
                document.getElementById('player-bar').classList.remove('active');
                stopPosReport();
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
                if (msg.url) loadPlaylist(msg.url);
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
        // Deselect menu, clear stack, show search results
        activeMenuIdx = -1;
        highlightMenu(-1);
        navStack = [];
        updateContentHeader();
        loadContent('/msx/search-input.json?q=' + encodeURIComponent(q), 'Search: ' + q, false);
    }

    // --- Player Mode ---
    function toggleMode() {
        document.getElementById('player-full').classList.toggle('active');
    }

    // --- UI Helpers ---
    function showLoading(on) { document.getElementById('loading').classList.toggle('active', on); }
    function showError(msg) { document.getElementById('content').innerHTML = '<div class="empty-state">' + esc(msg) + '</div>'; }

    // --- Init ---
    function init() {
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
        document.getElementById('full-play').addEventListener('click', togglePlay);
        document.getElementById('full-prev').addEventListener('click', prevTrack);
        document.getElementById('full-next').addEventListener('click', nextTrack);
        document.getElementById('full-seek').addEventListener('input', function (e) { seekTo(e.target.value); });
        document.getElementById('full-browse').addEventListener('click', toggleMode);

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

        // WebSocket
        connectWS();

        // Load menu from /msx/menu.json, build sidebar, then auto-select first item
        var menuUrl = addParam('/msx/menu.json', deviceParam);
        fetch(resolveUrl(menuUrl))
            .then(function (r) { return r.json(); })
            .then(function (data) { buildMenu(data); })
            .catch(function (e) { console.error('Menu load failed:', e); });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
