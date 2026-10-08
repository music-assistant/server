/* Smart DJ UI — injected by the smart_dj MA plugin.
 * Adds "Smart DJ" items to the queue overflow menu and playlist listing menu.
 */
(function () {
  'use strict';

  // ─────────────────────────── WebSocket API client ───────────────────────────

  const WS_PATH = '/ws';
  let _ws = null;
  let _msgId = 1;
  const _pending = new Map(); // msgId → {resolve, reject}

  function _getToken() {
    return localStorage.getItem('auth_token') || '';
  }

  function _rejectAllPending(reason) {
    _pending.forEach(function (cb) { cb.reject(new Error(reason)); });
    _pending.clear();
  }

  function _wsConnect() {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    _ws = new WebSocket(proto + '//' + location.host + WS_PATH);

    _ws.addEventListener('open', function () {
      _ws.send(JSON.stringify({
        command: 'auth',
        message_id: String(_msgId++),
        args: { token: _getToken() }
      }));
    });

    _ws.addEventListener('message', function (ev) {
      var msg;
      try { msg = JSON.parse(ev.data); } catch (_) { return; }
      var id = String(msg.message_id);
      var cb = _pending.get(id);
      if (cb) {
        _pending.delete(id);
        if (msg.error_code != null) {
          cb.reject(new Error(msg.details || 'MA error ' + msg.error_code));
        } else {
          cb.resolve(msg.result);
        }
      }
    });

    _ws.addEventListener('close', function () {
      _rejectAllPending('Smart DJ: WebSocket connection closed');
      setTimeout(_wsConnect, 5000);
    });
    _ws.addEventListener('error', function () { _ws.close(); });
  }

  function callAPI(command, args) {
    return new Promise(function (resolve, reject) {
      if (!_ws || _ws.readyState !== 1 /* OPEN */) {
        reject(new Error('Smart DJ: WebSocket not connected'));
        return;
      }
      var id = String(_msgId++);
      _pending.set(id, { resolve: resolve, reject: reject });
      _ws.send(JSON.stringify({ command: command, message_id: id, args: args || {} }));
    });
  }

  // ─────────────────────────── Context helpers ────────────────────────────────

  async function getActiveQueueId() {
    var players = await callAPI('players/all', {
      return_unavailable: false,
      return_disabled: false,
    });
    if (!players || !players.length) return null;
    // Skip analysis workers (their IDs contain "_worker_")
    var regular = players.filter(function (p) {
      return p.player_id.indexOf('_worker_') === -1;
    });
    // Prefer a powered, playing player
    var playing = regular.find(function (p) {
      return p.powered && p.state === 'playing';
    });
    if (playing) return playing.active_source || playing.player_id;
    // Fallback: any powered player that is not off
    var powered = regular.find(function (p) {
      return p.powered && p.state !== 'off';
    });
    if (powered) return powered.active_source || powered.player_id;
    // Last resort
    return regular.length ? regular[0].player_id : null;
  }

  function getPlaylistFromURL() {
    // Route: /#/playlists/{provider}/{itemId}
    var m = location.hash.match(/#\/playlists\/([^/?#]+)\/([^/?#]+)/);
    return m ? { provider: m[1], item_id: m[2] } : null;
  }

  // ─────────────────────────── Toast notification ─────────────────────────────

  function showToast(text, color) {
    var el = document.createElement('div');
    el.textContent = text;
    Object.assign(el.style, {
      position: 'fixed',
      bottom: '80px',
      left: '50%',
      transform: 'translateX(-50%)',
      background: color || '#1565C0',
      color: '#fff',
      padding: '10px 22px',
      borderRadius: '8px',
      zIndex: '99999',
      fontSize: '14px',
      pointerEvents: 'none',
      boxShadow: '0 3px 10px rgba(0,0,0,.45)',
      transition: 'opacity .4s ease',
      maxWidth: '90vw',
      textAlign: 'center',
    });
    document.body.appendChild(el);
    setTimeout(function () {
      el.style.opacity = '0';
      setTimeout(function () { el.remove(); }, 450);
    }, 4000);
  }

  // ─────────────────────────── Vuetify-compatible DOM elements ────────────────

  function makeListItem(label, icon, onClickAsync) {
    var item = document.createElement('div');
    item.className = [
      'v-list-item',
      'v-list-item--density-compact',
      'v-list-item--slim',
      'v-list-item--one-line',
    ].join(' ');
    item.style.cssText = 'cursor:pointer;user-select:none;';
    item.setAttribute('role', 'option');
    item.innerHTML =
      '<div class="v-list-item__prepend">' +
        '<i class="v-icon notranslate mdi ' + icon + '" aria-hidden="true"' +
        ' style="font-size:1.2rem;opacity:.85;"></i>' +
      '</div>' +
      '<div class="v-list-item__content">' +
        '<div class="v-list-item-title" style="font-size:.875rem;">' + label + '</div>' +
      '</div>';
    item.addEventListener('click', function (e) {
      e.stopPropagation();
      // Close the overlay before the async action starts
      document.body.dispatchEvent(new MouseEvent('click', { bubbles: true }));
      onClickAsync().catch(function (err) {
        showToast('Smart DJ error: ' + err.message, '#b71c1c');
      });
    });
    return item;
  }

  function makeDivider() {
    var hr = document.createElement('div');
    hr.className = 'v-divider';
    hr.setAttribute('role', 'separator');
    hr.style.cssText =
      'border-color:rgba(255,255,255,.12);border-top-width:thin;' +
      'border-top-style:solid;margin:4px 0;';
    return hr;
  }

  // ─────────────────────────── Queue menu injection ───────────────────────────

  function injectQueueMenu(listEl) {
    if (listEl.__smartDjInjected) return;
    listEl.__smartDjInjected = true;

    var sortItem = makeListItem('Smart DJ Queue', 'mdi-music-note-list', async function () {
      var queueId = await getActiveQueueId();
      if (!queueId) { showToast('No active player found', '#b71c1c'); return; }
      showToast('Smart DJ is reordering the queue…', '#1565C0');
      var res = await callAPI('smart_dj/rank_queue', {
        queue_id: queueId,
        controls: { apply: true },
      });
      var count = (res.tracks || []).length;
      showToast('Reordered ' + count + ' track(s)', '#1b5e20');
    });

    var analyzeItem = makeListItem('Analyze Queue (DJ)', 'mdi-waveform', async function () {
      var queueId = await getActiveQueueId();
      if (!queueId) { showToast('No active player found', '#b71c1c'); return; }
      showToast('Starting analysis…', '#1565C0');
      var res = await callAPI('smart_dj/analyze', { queue_id: queueId });
      var count = (res.tracks || []).length;
      showToast('Analyzed ' + count + ' track(s)', '#0d47a1');
    });

    listEl.appendChild(makeDivider());
    listEl.appendChild(sortItem);
    listEl.appendChild(analyzeItem);
  }

  // ─────────────────────────── Playlist menu injection ────────────────────────

  function injectPlaylistMenu(listEl) {
    if (listEl.__smartDjInjected) return;
    listEl.__smartDjInjected = true;

    var sortItem = makeListItem('Smart DJ Queue', 'mdi-music-note-list', async function () {
      var queueId = await getActiveQueueId();
      if (!queueId) { showToast('No active player found', '#b71c1c'); return; }
      showToast('Smart DJ is reordering the queue…', '#1565C0');
      var res = await callAPI('smart_dj/rank_queue', {
        queue_id: queueId,
        controls: { apply: true },
      });
      var count = (res.tracks || []).length;
      showToast('Reordered ' + count + ' track(s)', '#1b5e20');
    });

    var capsItem = makeListItem('Smart DJ capabilities', 'mdi-information-outline', async function () {
      var res = await callAPI('smart_dj/capabilities', {});
      var analysis = res.analysis || {};
      var parts = [];
      if (analysis.music_assistant) parts.push('MA analysis');
      if (analysis.musicae) parts.push('Musicae');
      showToast(parts.length ? 'Analysis available: ' + parts.join(', ') : 'No analysis provider configured', '#0d47a1');
    });

    listEl.appendChild(makeDivider());
    listEl.appendChild(sortItem);
    listEl.appendChild(capsItem);
  }

  // ─────────────────────────── MutationObserver ───────────────────────────────

  function tryInjectIntoOverlay(node) {
    var content = (node.matches && node.matches('.v-overlay__content'))
      ? node
      : (node.querySelector && node.querySelector('.v-overlay__content'));
    if (!content) return;

    var list = content.querySelector('.v-list');
    if (!list || list.__smartDjInjected) return;

    var titles = Array.from(list.querySelectorAll('.v-list-item-title')).map(function (el) {
      return el.textContent.trim();
    });

    if (titles.indexOf('Clear queue') !== -1) {
      injectQueueMenu(list);
    } else if (
      titles.indexOf('Sort options') !== -1 ||
      titles.indexOf('Refresh the items listing') !== -1 ||
      titles.indexOf('Select multiple items') !== -1
    ) {
      injectPlaylistMenu(list);
    }
  }

  var _observer = new MutationObserver(function (muts) {
    for (var i = 0; i < muts.length; i++) {
      var added = muts[i].addedNodes;
      for (var j = 0; j < added.length; j++) {
        if (added[j].nodeType === 1 /* ELEMENT_NODE */) {
          tryInjectIntoOverlay(added[j]);
        }
      }
    }
  });

  _observer.observe(document.body, { childList: true, subtree: true });
  _wsConnect();
})();
