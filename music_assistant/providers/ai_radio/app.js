'use strict';

const TOKEN_STORAGE_KEY = 'ai_radio.plugin_token';
const TOUR_SEEN_STORAGE_KEY = 'ai_radio.web_tour_seen_v2';
const DEFAULT_UI_AUTO_REFRESH_SECONDS = 2;
const RECOMMENDED_SECTION_IDS = [
  'Song_Introduction_Start',
  'Song_Transition',
  'Song_Introduction_End',
  'Weather_Short',
];
const BUTTON_ICON_MAP = {
  auth_use_token: 'key',
  auth_clear: 'x',
  login_submit: 'log-in',
  control_create_station: 'plus',
  control_refresh_status: 'refresh-cw',
  station_new: 'plus-circle',
  station_new_template: 'file-plus',
  station_delete: 'trash-2',
  station_validate: 'check-circle',
  station_save: 'save',
  station_export: 'download',
  section_new: 'plus-circle',
  section_delete: 'trash-2',
  section_save: 'save',
  section_export: 'download',
  add_order_rule: 'plus',
  wizard_select_recommended_sections: 'sparkles',
  wizard_custom_section_add: 'plus',
  wizard_back: 'arrow-left',
  wizard_next: 'arrow-right',
  wizard_save: 'save',
  tour_back: 'arrow-left',
  tour_next: 'arrow-right',
  tour_done: 'check',
};

const TOUR_STEPS = [
  {
    title: 'What AI Radio Does',
    body: 'AI Radio turns a normal playlist into a radio-style program with spoken host segments.',
    bullets: [
      'Reads tracks from your source playlist.',
      'Selects sections by flow rules.',
      'Generates text, synthesizes speech, and inserts those audio segments into playback.',
    ],
  },
  {
    title: 'Stations and Sections',
    body: 'Stations and sections are separate on purpose.',
    bullets: [
      'Station: run preset (source playlist, selected sections, flow rules, host voice/config).',
      'Section: reusable prompt unit that can be shared across multiple stations.',
      'Edit sections once in Sections menu and reuse everywhere.',
      'Section type ai_text = normal spoken segment.',
      'Section type ai_meta = merge section, used to combine multiple same-slot between-song sections into one output.',
    ],
  },
  {
    title: 'Flow Rule Types',
    body: 'Flow rules decide how sections are selected at each slot.',
    bullets: [
      'MUST: always include that section.',
      'ALTERNATIVE: choose exactly one section from weighted choices.',
      'OPTIONAL: independently include one section only when chance and guard rules pass.',
      'If multiple sections are selected for one between-song slot, ai_meta merge logic can combine them into one spoken segment.',
    ],
  },
  {
    title: 'Run Modes',
    body: 'Choose run mode based on how you want output delivered.',
    bullets: [
      'Playlist Mode: creates a prepared playlist.',
      'Dynamic Mode: injects generated segments into the live queue in batches.',
    ],
  },
  {
    title: 'Recommended First Step',
    body: 'Start in the Run view with "Create Station (Guided)" for the easiest setup.',
    bullets: [
      'Use Create Station (Guided) in Run to set up a working station quickly.',
      'After creation, open Station Settings to edit flow rules and advanced options in more detail.',
      'You can change everything later.',
    ],
  },
];

const SESSION_PHASE_LABELS = {
  fetch_source_tracks: 'Loading Source Tracks',
  planning_sections: 'Planning Sections',
  generating_llm: 'Generating LLM',
  generating_tts: 'Generating TTS',
  publishing_playlist: 'Publishing Playlist',
  initializing_queue: 'Initializing Queue',
  queueing_batch: 'Queueing Batch',
  waiting_for_playback: 'Waiting For Playback',
  running: 'Running',
  completed: 'Completed',
  failed: 'Failed',
  stopped: 'Stopped',
};

const state = {
  token: '',
  stations: [],
  sections: [],
  players: [],
  playlists: [],
  sessions: [],
  uiAutoRefreshSeconds: DEFAULT_UI_AUTO_REFRESH_SECONDS,
  autoRefreshTimer: null,
  lastAutoRefreshError: '',
  loadedStationId: '',
  loadedSectionId: '',
  stationTemplate: null,
  sectionTemplate: null,
  wizardStep: 1,
  wizardIdTouched: false,
  tourStep: 0,
  tourShownThisSession: false,
};

const el = {};

window.addEventListener('DOMContentLoaded', () => {
  cacheElements();
  enhanceButtonIcons();
  bindHelpTips();
  bindEvents();
  bootstrapAuth();
});

function cacheElements() {
  const ids = [
    'nav_control',
    'nav_stations',
    'nav_sections',
    'nav_about',
    'tour_replay',
    'auth_toggle',
    'refresh_all',
    'auth_panel',
    'auth_token',
    'auth_use_token',
    'auth_clear',
    'login_provider',
    'login_username',
    'login_password',
    'login_submit',
    'control_view',
    'control_station_id',
    'control_source_playlist',
    'control_player_id',
    'control_player_hint',
    'control_dynamic_source_playtime_cap',
    'control_dynamic_batch_size',
    'control_start_playlist',
    'control_start_dynamic',
    'control_refresh_status',
    'control_create_station',
    'sessions_body',
    'stations_view',
    'sections_view',
    'about_view',
    'station_selector',
    'station_new',
    'station_new_template',
    'station_delete',
    'station_validate',
    'station_save',
    'station_export',
    'station_import',
    'station_id',
    'station_name',
    'station_source_playlist_id',
    'station_source_playlist_provider',
    'station_source_playlist_pick',
    'station_source_manual_mode',
    'station_target_playlist_provider',
    'station_default_player_id',
    'station_max_duration_minutes',
    'station_dynamic_batch_size',
    'station_dynamic_prefetch_remaining_tracks',
    'station_dynamic_poll_seconds',
    'station_clear_queue_on_start',
    'station_section_ids',
    'station_manage_sections_link',
    'general_timezone',
    'general_location_city',
    'general_location_country',
    'general_weather_provider',
    'general_weather_timeout_seconds',
    'general_model',
    'general_temperature',
    'general_max_tokens',
    'general_openai_base_url',
    'general_section_store_path',
    'general_tts_provider',
    'general_openai_tts_model',
    'general_openai_tts_voice',
    'general_elevenlabs_model',
    'general_elevenlabs_voice_id',
    'general_instructions',
    'general_openai_tts_instructions',
    'order_rules',
    'add_order_rule',
    'section_selector',
    'section_new',
    'section_delete',
    'section_save',
    'section_export',
    'section_import',
    'section_id',
    'section_name',
    'section_type',
    'section_web_search',
    'section_max_chars',
    'section_prompt',
    'wizard_modal',
    'wizard_close',
    'wizard_step_indicator',
    'wizard_error',
    'wizard_back',
    'wizard_next',
    'wizard_save',
    'wizard_name',
    'wizard_id',
    'wizard_source_playlist',
    'wizard_default_player',
    'wizard_max_duration_minutes',
    'wizard_section_ids',
    'wizard_select_recommended_sections',
    'wizard_manage_sections_link',
    'wizard_custom_section_id',
    'wizard_custom_section_name',
    'wizard_custom_section_type',
    'wizard_custom_section_web_search',
    'wizard_custom_section_max_chars',
    'wizard_custom_section_prompt',
    'wizard_custom_section_add',
    'wizard_model',
    'wizard_tts_provider',
    'wizard_openai_tts_voice',
    'wizard_elevenlabs_voice_id',
    'wizard_timezone',
    'wizard_location_city',
    'wizard_location_country',
    'wizard_order_mode',
    'wizard_instructions',
    'wizard_summary',
    'tour_modal',
    'tour_close',
    'tour_body',
    'tour_back',
    'tour_next',
    'tour_done',
    'messages',
  ];
  ids.forEach((id) => {
    el[id] = document.getElementById(id);
  });
}

function refreshLucideIcons() {
  if (window.lucide && typeof window.lucide.createIcons === 'function') {
    window.lucide.createIcons();
  }
}

function enhanceButtonIcons() {
  Object.entries(BUTTON_ICON_MAP).forEach(([buttonId, iconName]) => {
    const buttonEl = document.getElementById(buttonId);
    if (!buttonEl || buttonEl.querySelector('.btn-label')) return;
    const label = buttonEl.textContent ? buttonEl.textContent.trim() : '';
    buttonEl.innerHTML = `<i data-lucide="${escapeAttr(iconName)}" class="lucide" aria-hidden="true"></i><span class="btn-label">${escapeHtml(label)}</span>`;
  });
  refreshLucideIcons();
}

function bindHelpTips() {
  document.querySelectorAll('.help-tip').forEach((tipEl) => {
    tipEl.addEventListener('click', (event) => {
      event.preventDefault();
      event.stopPropagation();
      const helpText = String(tipEl.getAttribute('title') || '').trim();
      if (!helpText) return;
      setMessage(helpText, 'msg-info');
    });
  });
}

function bindEvents() {
  el.nav_control.addEventListener('click', () => showView('control'));
  el.nav_stations.addEventListener('click', () => showView('stations'));
  el.nav_sections.addEventListener('click', () => showView('sections'));
  el.nav_about.addEventListener('click', () => showView('about'));
  el.tour_replay.addEventListener('click', () => openTour(false));

  el.auth_toggle.addEventListener('click', toggleAuthPanel);
  el.refresh_all.addEventListener('click', () => loadAllData());

  el.auth_use_token.addEventListener('click', () => useToken(el.auth_token.value));
  el.auth_clear.addEventListener('click', clearToken);
  el.login_submit.addEventListener('click', loginWithCredentials);

  el.control_start_playlist.addEventListener('click', startPlaylistRun);
  el.control_start_dynamic.addEventListener('click', startDynamicRun);
  el.control_refresh_status.addEventListener('click', refreshSessions);
  el.control_create_station.addEventListener('click', () => openStationWizard());
  el.control_station_id.addEventListener('change', renderPlayerSelectors);
  el.control_player_id.addEventListener('change', updateRunActionState);
  el.sessions_body.addEventListener('click', onSessionTableClick);

  el.station_selector.addEventListener('change', async () => {
    const stationId = el.station_selector.value;
    if (!stationId) return;
    await loadStation(stationId);
  });
  el.station_new.addEventListener('click', () => openStationWizard());
  el.station_new_template.addEventListener('click', loadTemplateStation);
  el.station_delete.addEventListener('click', deleteCurrentStation);
  el.station_validate.addEventListener('click', validateCurrentStation);
  el.station_save.addEventListener('click', saveCurrentStation);
  el.station_export.addEventListener('click', exportCurrentStation);
  el.station_import.addEventListener('change', importStationJson);

  el.station_source_playlist_pick.addEventListener('change', () => {
    if (el.station_source_manual_mode.checked) return;
    const value = el.station_source_playlist_pick.value;
    if (!value) return;
    const [provider, itemId] = splitComboValue(value);
    el.station_source_playlist_provider.value = provider;
    el.station_source_playlist_id.value = itemId;
  });

  el.station_source_manual_mode.addEventListener('change', syncSourceModeUi);
  el.general_tts_provider.addEventListener('change', syncEditorTtsUi);
  el.station_section_ids.addEventListener('change', refreshFlowSectionOptions);

  el.add_order_rule.addEventListener('click', () => {
    el.order_rules.append(createOrderRule());
  });

  el.section_selector.addEventListener('change', () => {
    const sectionId = el.section_selector.value;
    if (!sectionId) return;
    loadSectionIntoEditor(sectionId);
  });
  el.section_new.addEventListener('click', loadSectionTemplate);
  el.section_delete.addEventListener('click', deleteCurrentSection);
  el.section_save.addEventListener('click', saveCurrentSection);
  el.section_export.addEventListener('click', exportCurrentSection);
  el.section_import.addEventListener('change', importSectionJson);
  el.section_type.addEventListener('change', syncSectionEditorTypeUi);

  document.querySelectorAll('[data-action="close-wizard"]').forEach((node) => {
    node.addEventListener('click', closeStationWizard);
  });
  el.wizard_close.addEventListener('click', closeStationWizard);
  el.wizard_back.addEventListener('click', wizardBack);
  el.wizard_next.addEventListener('click', wizardNext);
  el.wizard_save.addEventListener('click', saveWizardStation);

  el.wizard_name.addEventListener('input', () => {
    if (!state.wizardIdTouched) {
      el.wizard_id.value = slugify(el.wizard_name.value);
    }
    refreshWizardSummary();
  });
  el.wizard_id.addEventListener('input', () => {
    state.wizardIdTouched = true;
    refreshWizardSummary();
  });

  [
    'wizard_source_playlist',
    'wizard_default_player',
    'wizard_max_duration_minutes',
    'wizard_section_ids',
    'wizard_model',
    'wizard_tts_provider',
    'wizard_openai_tts_voice',
    'wizard_elevenlabs_voice_id',
    'wizard_timezone',
    'wizard_location_city',
    'wizard_location_country',
    'wizard_order_mode',
    'wizard_instructions',
  ].forEach((id) => {
    el[id].addEventListener('input', refreshWizardSummary);
    el[id].addEventListener('change', refreshWizardSummary);
  });

  el.wizard_tts_provider.addEventListener('change', () => {
    syncWizardTtsUi();
    refreshWizardSummary();
  });

  el.wizard_select_recommended_sections.addEventListener('click', () => {
    selectRecommendedWizardSections();
  });
  el.wizard_custom_section_add.addEventListener('click', createWizardCustomSection);

  document.querySelectorAll('[data-action="close-tour"]').forEach((node) => {
    node.addEventListener('click', closeTour);
  });
  el.tour_close.addEventListener('click', closeTour);
  el.tour_back.addEventListener('click', tourBack);
  el.tour_next.addEventListener('click', tourNext);
  el.tour_done.addEventListener('click', completeTour);
}

function setAuthPanelVisible(visible) {
  el.auth_panel.classList.toggle('hidden', !visible);
  const labelEl = el.auth_toggle.querySelector('.btn-label');
  const label = visible ? 'Hide Auth' : 'Auth';
  if (labelEl) {
    labelEl.textContent = label;
  } else {
    el.auth_toggle.textContent = label;
  }
}

function toggleAuthPanel() {
  const visible = !el.auth_panel.classList.contains('hidden');
  setAuthPanelVisible(!visible);
}

function showView(view) {
  const active = view || 'control';
  el.control_view.classList.toggle('hidden', active !== 'control');
  el.stations_view.classList.toggle('hidden', active !== 'stations');
  el.sections_view.classList.toggle('hidden', active !== 'sections');
  el.about_view.classList.toggle('hidden', active !== 'about');

  el.nav_control.classList.toggle('active', active === 'control');
  el.nav_stations.classList.toggle('active', active === 'stations');
  el.nav_sections.classList.toggle('active', active === 'sections');
  el.nav_about.classList.toggle('active', active === 'about');
}

function applyInitialViewFromUrl() {
  const params = new URLSearchParams(window.location.search);
  const view = String(params.get('view') || '').trim().toLowerCase();
  if (view === 'sections') {
    showView('sections');
    return;
  }
  if (view === 'about') {
    showView('about');
    return;
  }
  if (view === 'stations') {
    showView('stations');
    return;
  }
  showView('control');
}

function setMessage(text, level = '') {
  el.messages.textContent = text || '';
  el.messages.classList.remove('msg-error', 'msg-ok', 'msg-warn', 'msg-info');
  if (level) {
    el.messages.classList.add(level);
  }
}

function setWizardError(text) {
  const message = String(text || '').trim();
  el.wizard_error.textContent = message;
  el.wizard_error.classList.toggle('hidden', !message);
}

function looksLikeToken(value) {
  if (!value || typeof value !== 'string') return false;
  const trimmed = value.trim();
  if (trimmed.length < 40) return false;
  return trimmed.split('.').length === 3;
}

function guessTokenCandidates() {
  const candidates = [];
  const params = new URLSearchParams(window.location.search);
  ['token', 'access_token'].forEach((key) => {
    const val = params.get(key);
    if (val && looksLikeToken(val)) {
      candidates.push(val);
    }
  });

  const remembered = localStorage.getItem(TOKEN_STORAGE_KEY);
  if (remembered && looksLikeToken(remembered)) {
    candidates.push(remembered);
  }
  return [...new Set(candidates)];
}

async function bootstrapAuth() {
  stopAutoRefresh();
  const candidates = guessTokenCandidates();
  for (const token of candidates) {
    const success = await useToken(token, false);
    if (success) {
      setAuthPanelVisible(false);
      return;
    }
  }
  setAuthPanelVisible(true);
  setMessage('No valid token detected. Use token field or login below.', 'msg-warn');
}

async function useToken(token, notify = true) {
  const trimmed = (token || '').trim();
  if (!looksLikeToken(trimmed)) {
    setMessage('Token format looks invalid.', 'msg-error');
    return false;
  }
  state.token = trimmed;
  el.auth_token.value = trimmed;
  try {
    const user = await rpc('auth/me');
    localStorage.setItem(TOKEN_STORAGE_KEY, trimmed);
    setAuthPanelVisible(false);
    setMessage(`Authenticated as ${user.display_name || user.username}.`, 'msg-ok');
    await loadAllData();
    applyInitialViewFromUrl();
    maybeStartTour();
    return true;
  } catch (err) {
    if (notify) {
      setMessage(`Token rejected: ${errorMessage(err)}`, 'msg-error');
    }
    stopAutoRefresh();
    state.token = '';
    return false;
  }
}

function clearToken() {
  stopAutoRefresh();
  state.token = '';
  el.auth_token.value = '';
  localStorage.removeItem(TOKEN_STORAGE_KEY);
  setAuthPanelVisible(true);
  setMessage('Token cleared.', 'msg-warn');
}

async function loginWithCredentials() {
  const providerId = (el.login_provider.value || 'builtin').trim();
  const username = (el.login_username.value || '').trim();
  const password = el.login_password.value || '';
  if (!username || !password) {
    setMessage('Username and password are required.', 'msg-error');
    return;
  }
  try {
    const response = await fetch('/auth/login', {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
      },
      body: JSON.stringify({
        provider_id: providerId,
        credentials: { username, password },
        device_name: 'AI Radio Plugin Page',
      }),
    });
    const payload = await response.json();
    if (!response.ok || !payload.success || !payload.token) {
      throw new Error(payload.error || `HTTP ${response.status}`);
    }
    await useToken(payload.token);
  } catch (err) {
    setMessage(`Login failed: ${errorMessage(err)}`, 'msg-error');
  }
}

async function rpc(command, args = {}) {
  if (!state.token) {
    throw new Error('No auth token set');
  }
  const response = await fetch('/api', {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      Authorization: `Bearer ${state.token}`,
    },
    body: JSON.stringify({
      message_id: makeMessageId(),
      command,
      args,
    }),
  });
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    const message = payload && payload.error ? payload.error : JSON.stringify(payload);
    throw new Error(`${response.status} ${response.statusText}: ${message}`);
  }
  return payload;
}

function makeMessageId() {
  if (window.crypto && window.crypto.randomUUID) {
    return window.crypto.randomUUID();
  }
  return `${Date.now()}_${Math.random().toString(16).slice(2)}`;
}

function errorMessage(err) {
  if (!err) return 'unknown error';
  if (typeof err === 'string') return err;
  if (err.message) return err.message;
  return String(err);
}

async function loadAllData() {
  try {
    const [stations, sections, players, playlists, status, uiSettings] = await Promise.all([
      rpc('ai_radio/stations/list'),
      rpc('ai_radio/sections/list'),
      rpc('players/all'),
      rpc('music/playlists/library_items', { limit: 1000, offset: 0, order_by: 'sort_name' }),
      rpc('ai_radio/status'),
      rpc('ai_radio/ui_settings'),
    ]);

    state.stations = Array.isArray(stations) ? stations : [];
    state.sections = Array.isArray(sections) ? sections : [];
    state.players = Array.isArray(players) ? players : [];
    state.playlists = Array.isArray(playlists) ? playlists : [];
    state.sessions = Array.isArray(status.sessions) ? status.sessions : [];
    state.uiAutoRefreshSeconds = normalizeAutoRefreshSeconds(uiSettings?.auto_refresh_seconds);

    renderStationSelectors();
    renderSectionSelectors();
    renderPlayerSelectors();
    renderPlaylistSelectors();
    renderSessions();
    renderWizardOptions();
    refreshFlowSectionOptions();

    if (!state.loadedStationId && state.stations.length) {
      await loadStation(state.stations[0].id);
    }
    if (!state.loadedSectionId && state.sections.length) {
      loadSectionIntoEditor(state.sections[0].id);
    }
    updateRunActionState();
    startAutoRefresh();

    setMessage('Data refreshed.', 'msg-ok');
  } catch (err) {
    stopAutoRefresh();
    setMessage(`Failed to load data: ${errorMessage(err)}`, 'msg-error');
  }
}

function normalizeAutoRefreshSeconds(value) {
  const parsed = parseInt(String(value ?? DEFAULT_UI_AUTO_REFRESH_SECONDS), 10);
  if (!Number.isFinite(parsed) || Number.isNaN(parsed)) {
    return DEFAULT_UI_AUTO_REFRESH_SECONDS;
  }
  return Math.max(1, Math.min(30, parsed));
}

function stopAutoRefresh() {
  if (state.autoRefreshTimer !== null) {
    window.clearInterval(state.autoRefreshTimer);
    state.autoRefreshTimer = null;
  }
}

function startAutoRefresh() {
  stopAutoRefresh();
  const intervalSeconds = normalizeAutoRefreshSeconds(state.uiAutoRefreshSeconds);
  state.uiAutoRefreshSeconds = intervalSeconds;
  state.autoRefreshTimer = window.setInterval(() => {
    void refreshRuntimeState(false);
  }, intervalSeconds * 1000);
}

function renderStationSelectors() {
  fillSelect(
    el.control_station_id,
    state.stations.map((station) => ({
      value: station.id,
      label: `${station.name} (${station.id})`,
    }))
  );
  fillSelect(
    el.station_selector,
    state.stations.map((station) => ({
      value: station.id,
      label: `${station.name} (${station.id})`,
    }))
  );

  if (state.loadedStationId) {
    el.control_station_id.value = state.loadedStationId;
    el.station_selector.value = state.loadedStationId;
  }
  updateRunActionState();
}

function renderSectionSelectors() {
  const options = state.sections
    .map((section) => ({
      value: section.id,
      label: `${section.id} (${section.name || section.id})`,
    }))
    .sort((a, b) => a.label.localeCompare(b.label));

  fillSelect(el.section_selector, options);

  const selectedStationSectionIds = getSelectedMultiValues(el.station_section_ids);
  fillMultiSelect(el.station_section_ids, options, selectedStationSectionIds);
}

function renderPlayerSelectors() {
  const previousOverride = String(el.control_player_id.value || '').trim();
  const selectedStationId = String(el.control_station_id.value || '').trim();
  const station = state.stations.find((item) => item.id === selectedStationId) || null;
  const stationDefaultPlayerId = String(station?.default_player_id || '').trim();
  const stationDefaultPlayer = stationDefaultPlayerId ? findPlayer(stationDefaultPlayerId) : null;
  const stationDefaultPlayerLabel = stationDefaultPlayer
    ? (stationDefaultPlayer.display_name || stationDefaultPlayer.name || stationDefaultPlayerId)
    : stationDefaultPlayerId;
  let stationDefaultOption = null;
  if (stationDefaultPlayerId) {
    if (stationDefaultPlayer && isPlayerAvailable(stationDefaultPlayer)) {
      stationDefaultOption = {
        value: '',
        label: `Station Default - ${stationDefaultPlayerLabel}`,
        disabled: false,
      };
    } else {
      stationDefaultOption = {
        value: '',
        label: `Station Default - ${stationDefaultPlayerLabel} (Not available)`,
        disabled: true,
      };
    }
  }
  const runOptions = state.players
    .map((player) => ({
      value: player.player_id,
      label: `${player.display_name || player.name || player.player_id} (${player.player_id})${isPlayerAvailable(player) ? '' : ' (Not available)'}`,
      disabled: !isPlayerAvailable(player),
    }))
    .sort((a, b) => a.label.localeCompare(b.label));
  const stationOptions = state.players
    .map((player) => ({
      value: player.player_id,
      label: `${player.display_name || player.name || player.player_id} (${player.player_id})`,
    }))
    .sort((a, b) => a.label.localeCompare(b.label));

  const controlPlayerOptions = stationDefaultOption ? [stationDefaultOption, ...runOptions] : runOptions;
  fillSelect(el.control_player_id, controlPlayerOptions);
  fillSelect(el.station_default_player_id, [{ value: '', label: '-- None --' }, ...stationOptions]);
  if (previousOverride && runOptions.some((item) => item.value === previousOverride && !item.disabled)) {
    el.control_player_id.value = previousOverride;
  } else {
    const defaultPlayerId = chooseLastPlayedAvailablePlayerId();
    el.control_player_id.value = defaultPlayerId || '';
  }
  updateRunActionState();
}

function renderPlaylistSelectors() {
  const options = state.playlists
    .map((playlist) => {
      const provider = playlist.provider || 'library';
      const itemId = String(playlist.item_id || '');
      return {
        value: comboValue(provider, itemId),
        label: `${playlist.name || itemId} (${provider}:${itemId})`,
      };
    })
    .sort((a, b) => a.label.localeCompare(b.label));

  fillSelect(el.control_source_playlist, [{ value: '', label: 'Use station default' }, ...options]);
  fillSelect(el.station_source_playlist_pick, [{ value: '', label: '-- Select --' }, ...options]);
}

function renderWizardOptions() {
  const playerOptions = state.players
    .map((player) => ({
      value: player.player_id,
      label: `${player.display_name || player.name || player.player_id} (${player.player_id})`,
    }))
    .sort((a, b) => a.label.localeCompare(b.label));

  const playlistOptions = state.playlists
    .map((playlist) => {
      const provider = playlist.provider || 'library';
      const itemId = String(playlist.item_id || '');
      return {
        value: comboValue(provider, itemId),
        label: `${playlist.name || itemId} (${provider}:${itemId})`,
      };
    })
    .sort((a, b) => a.label.localeCompare(b.label));

  const sectionOptions = state.sections
    .map((section) => ({
      value: section.id,
      label: `${section.id} (${section.name || section.id})`,
    }))
    .sort((a, b) => a.label.localeCompare(b.label));

  fillSelect(el.wizard_default_player, [{ value: '', label: '-- None --' }, ...playerOptions]);
  fillSelect(el.wizard_source_playlist, [{ value: '', label: '-- Select --' }, ...playlistOptions]);
  const selected = getSelectedMultiValues(el.wizard_section_ids);
  fillMultiSelect(el.wizard_section_ids, sectionOptions, selected);
}

function syncSourceModeUi() {
  const manual = Boolean(el.station_source_manual_mode.checked);
  document.querySelectorAll('.manual-source-field').forEach((item) => {
    item.classList.toggle('hidden', !manual);
  });
  el.station_source_playlist_provider.disabled = !manual;
  el.station_source_playlist_id.disabled = !manual;
  el.station_source_playlist_pick.disabled = manual;

  if (!manual) {
    const value = el.station_source_playlist_pick.value;
    if (value) {
      const [provider, itemId] = splitComboValue(value);
      el.station_source_playlist_provider.value = provider;
      el.station_source_playlist_id.value = itemId;
    } else {
      el.station_source_playlist_provider.value = 'library';
      el.station_source_playlist_id.value = '';
    }
  }
}

function syncEditorTtsUi() {
  const provider = String(el.general_tts_provider.value || 'openai').toLowerCase();
  const openai = provider === 'openai';

  document.querySelectorAll('.openai-tts-field').forEach((item) => {
    item.classList.toggle('hidden', !openai);
  });

  document.querySelectorAll('.elevenlabs-tts-field').forEach((item) => {
    item.classList.toggle('hidden', openai);
  });
}

function syncWizardTtsUi() {
  const provider = String(el.wizard_tts_provider.value || 'openai').toLowerCase();
  const openai = provider === 'openai';

  document.querySelectorAll('.wizard-openai-tts-field').forEach((item) => {
    item.classList.toggle('hidden', !openai);
  });

  document.querySelectorAll('.wizard-elevenlabs-tts-field').forEach((item) => {
    item.classList.toggle('hidden', openai);
  });
}

function syncSectionEditorTypeUi() {
  const isMeta = el.section_type.value === 'ai_meta';
  el.section_web_search.disabled = isMeta;
  el.section_max_chars.disabled = isMeta;
}

function fillSelect(selectEl, options) {
  if (!selectEl) return;
  const currentValue = selectEl.value;
  selectEl.innerHTML = '';
  options.forEach((option) => {
    const opt = document.createElement('option');
    opt.value = option.value;
    opt.textContent = option.label;
    opt.disabled = Boolean(option.disabled);
    selectEl.appendChild(opt);
  });
  if (options.some((option) => option.value === currentValue && !option.disabled)) {
    selectEl.value = currentValue;
  }
}

function fillMultiSelect(selectEl, options, selectedValues) {
  if (!selectEl) return;
  const selected = new Set(selectedValues || []);
  selectEl.innerHTML = '';
  options.forEach((option) => {
    const opt = document.createElement('option');
    opt.value = option.value;
    opt.textContent = option.label;
    opt.selected = selected.has(option.value);
    selectEl.appendChild(opt);
  });
}

function getSelectedMultiValues(selectEl) {
  if (!selectEl) return [];
  return Array.from(selectEl.selectedOptions).map((opt) => opt.value).filter(Boolean);
}

function comboValue(provider, itemId) {
  return `${provider}:::${itemId}`;
}

function splitComboValue(value) {
  const [provider, ...rest] = String(value || '').split(':::');
  return [provider || 'library', rest.join(':::')];
}

function isPlayerAvailable(player) {
  if (!player || typeof player !== 'object') return false;
  if (player.available === false) return false;
  if (player.enabled === false) return false;
  return true;
}

function findPlayer(playerId) {
  if (!playerId) return null;
  return state.players.find((player) => player.player_id === playerId) || null;
}

function playbackStateRank(player) {
  const stateValue = String(player?.playback_state || '').trim().toLowerCase();
  if (stateValue === 'playing') return 3;
  if (stateValue === 'paused') return 2;
  if (stateValue === 'buffering') return 1;
  return 0;
}

function chooseLastPlayedAvailablePlayerId() {
  const availablePlayers = state.players.filter((player) => isPlayerAvailable(player));
  if (!availablePlayers.length) {
    return '';
  }
  const sorted = [...availablePlayers].sort((a, b) => {
    const rankDiff = playbackStateRank(b) - playbackStateRank(a);
    if (rankDiff !== 0) return rankDiff;
    const updatedDiff = Number(b.elapsed_time_last_updated || 0) - Number(a.elapsed_time_last_updated || 0);
    if (updatedDiff !== 0) return updatedDiff;
    return Number(b.elapsed_time || 0) - Number(a.elapsed_time || 0);
  });
  return String(sorted[0]?.player_id || '');
}

function updateRunActionState() {
  const selectedStationId = String(el.control_station_id.value || '').trim();
  const station = state.stations.find((item) => item.id === selectedStationId) || null;
  const overridePlayerId = String(el.control_player_id.value || '').trim();
  const stationDefaultPlayerId = String(station?.default_player_id || '').trim();
  const effectivePlayerId = overridePlayerId || stationDefaultPlayerId;
  const effectivePlayer = findPlayer(effectivePlayerId);
  const availablePlayers = state.players.filter((player) => isPlayerAvailable(player));

  let hint = '';
  let hintLevel = '';
  let disablePlaylistStart = false;
  let disableDynamicStart = false;
  if (!selectedStationId) {
    hint = 'Select a station first.';
    disablePlaylistStart = true;
    disableDynamicStart = true;
  } else if (!state.players.length || !availablePlayers.length) {
    hint = 'No available playback device. Start playback once in the main MA UI, then click Refresh.';
    hintLevel = 'warn';
    disableDynamicStart = true;
  } else if (overridePlayerId) {
    if (!effectivePlayer) {
      hint = `Selected override player '${overridePlayerId}' was not found.`;
      disableDynamicStart = true;
    } else if (!isPlayerAvailable(effectivePlayer)) {
      hint = `Override player '${overridePlayerId}' is currently unavailable.`;
      disableDynamicStart = true;
    } else {
      hint = `Override player '${overridePlayerId}' is available.`;
    }
  } else if (stationDefaultPlayerId) {
    if (!effectivePlayer) {
      hint = `Station default player '${stationDefaultPlayerId}' was not found.`;
      disableDynamicStart = true;
    } else if (!isPlayerAvailable(effectivePlayer)) {
      hint = `Station default player '${stationDefaultPlayerId}' is currently unavailable.`;
      disableDynamicStart = true;
    } else {
      hint = `Using station default player '${stationDefaultPlayerId}'.`;
    }
  } else {
    hint = 'No player selected. A dynamic run requires a player.';
    disableDynamicStart = true;
  }

  if (el.control_player_hint) {
    el.control_player_hint.textContent = hint;
    el.control_player_hint.classList.toggle('field-help-warn', hintLevel === 'warn');
  }
  el.control_start_playlist.disabled = disablePlaylistStart;
  el.control_start_dynamic.disabled = disableDynamicStart;
}

async function refreshSessions() {
  await refreshRuntimeState(true);
}

async function refreshRuntimeState(showMessage = false) {
  try {
    const [status, players] = await Promise.all([rpc('ai_radio/status'), rpc('players/all')]);
    state.sessions = Array.isArray(status.sessions) ? status.sessions : [];
    state.players = Array.isArray(players) ? players : [];
    renderSessions();
    if (!el.control_view.classList.contains('hidden')) {
      renderPlayerSelectors();
      updateRunActionState();
    }
    state.lastAutoRefreshError = '';
    if (showMessage) {
      setMessage('Session status refreshed.', 'msg-ok');
    }
  } catch (err) {
    const msg = `Failed to refresh status: ${errorMessage(err)}`;
    if (showMessage) {
      setMessage(msg, 'msg-error');
      return;
    }
    if (state.lastAutoRefreshError !== msg) {
      state.lastAutoRefreshError = msg;
      setMessage(`Auto refresh warning: ${errorMessage(err)}`, 'msg-warn');
    }
  }
}

function renderSessions() {
  const byStationId = Object.fromEntries(state.stations.map((station) => [station.id, station.name]));
  el.sessions_body.innerHTML = '';
  if (!state.sessions.length) {
    const row = document.createElement('tr');
    row.innerHTML = '<td colspan="7">No sessions</td>';
    el.sessions_body.appendChild(row);
    return;
  }
  state.sessions.forEach((session) => {
    const row = document.createElement('tr');
    const info = renderSessionInfo(session);
    const status = String(session.status || '');
    const statusClass = status.toLowerCase().replace(/[^a-z0-9_-]+/g, '-') || 'unknown';
    const stationLabel = byStationId[session.station_id] || session.station_id || '';
    const stopButton = session.status === 'running'
      ? `<button type="button" data-action="stop-session" data-session-id="${escapeAttr(session.session_id)}">Stop</button>`
      : '';
    row.innerHTML = `
      <td>${escapeHtml(session.session_id || '')}</td>
      <td>${escapeHtml(stationLabel)}</td>
      <td>${escapeHtml(session.mode || '')}</td>
      <td><span class="session-status session-status-${escapeAttr(statusClass)}">${escapeHtml(status)}</span></td>
      <td>${escapeHtml(session.created_at || '')}</td>
      <td class="session-info-cell">${info}</td>
      <td>${stopButton}</td>
    `;
    el.sessions_body.appendChild(row);
  });
}

function renderSessionInfo(session) {
  const progress = (session && typeof session.progress === 'object' && session.progress) || {};
  const result = (session && typeof session.result === 'object' && session.result) || {};
  const status = String(session.status || '').toLowerCase();
  const payload = status === 'completed'
    ? (Object.keys(result).length ? result : progress)
    : progress;
  const phaseKey = String(payload.phase || payload.step || status || '').trim();

  const chips = [];
  if (phaseKey) {
    chips.push(`<span class="session-chip">${escapeHtml(getSessionPhaseLabel(phaseKey))}</span>`);
  }
  if (session.error) {
    chips.push('<span class="session-chip session-chip-error">Error</span>');
  }

  const lines = [];
  if (payload.queued_tracks !== undefined && payload.total_tracks !== undefined) {
    lines.push(`Tracks queued: ${payload.queued_tracks}/${payload.total_tracks}`);
  } else if (payload.source_tracks !== undefined) {
    lines.push(`Source tracks: ${payload.source_tracks}`);
  }
  if (payload.sections_planned !== undefined) {
    lines.push(`Sections planned: ${payload.sections_planned}`);
  }
  if (payload.sections !== undefined) {
    lines.push(`Sections generated: ${payload.sections}`);
  }
  if (payload.generated_sections !== undefined) {
    lines.push(`Audio sections: ${payload.generated_sections}`);
  }
  if (payload.entries !== undefined) {
    lines.push(`Playlist entries: ${payload.entries}`);
  }
  if (payload.entries_added !== undefined) {
    lines.push(`Playlist entries added: ${payload.entries_added}`);
  }
  if (payload.batch_index !== undefined) {
    lines.push(`Batch: ${payload.batch_index}`);
  }
  if (payload.batch_size !== undefined) {
    lines.push(`Batch size: ${payload.batch_size}`);
  }
  if (payload.prefetch_remaining_tracks !== undefined) {
    lines.push(`Prefetch trigger: ${payload.prefetch_remaining_tracks} remaining track(s)`);
  }
  if (payload.batch_entries !== undefined) {
    lines.push(`Batch queue entries: ${payload.batch_entries}`);
  }
  if (payload.queue_entries !== undefined) {
    lines.push(`Queue entries: ${payload.queue_entries}`);
  }
  if (payload.wait_trigger_index !== undefined) {
    lines.push(`Wait trigger index: ${payload.wait_trigger_index}`);
  }
  if (payload.queue_id) {
    lines.push(`Queue: ${payload.queue_id}`);
  }
  if (payload.source_playlist_name) {
    lines.push(`Source playlist: ${payload.source_playlist_name}`);
  }
  if (payload.target_playlist_name) {
    lines.push(`Target playlist: ${payload.target_playlist_name}`);
  }

  if (session.error) {
    lines.unshift(session.error);
  }
  if (!lines.length) {
    lines.push('No details yet.');
  }

  const rawData = session.error
    ? { error: session.error, progress }
    : (status === 'completed' ? result : payload);
  const rawJson = escapeHtml(JSON.stringify(rawData || {}, null, 2));

  return `
    <div class="session-info">
      ${chips.length ? `<div class="session-chips">${chips.join('')}</div>` : ''}
      <div class="session-lines">${lines.map((line) => `<div>${escapeHtml(String(line))}</div>`).join('')}</div>
      <details class="session-raw">
        <summary>Raw details</summary>
        <pre>${rawJson}</pre>
      </details>
    </div>
  `;
}

function getSessionPhaseLabel(phase) {
  const key = String(phase || '').trim().toLowerCase();
  if (!key) return '';
  return SESSION_PHASE_LABELS[key] || key.replace(/[_-]+/g, ' ').replace(/\b\w/g, (m) => m.toUpperCase());
}

async function onSessionTableClick(event) {
  const button = event.target.closest('button[data-action="stop-session"]');
  if (!button) return;
  const sessionId = button.dataset.sessionId;
  if (!sessionId) return;
  try {
    await rpc('ai_radio/stop', { session_id: sessionId });
    await refreshSessions();
    setMessage(`Stopped session ${sessionId}.`, 'msg-warn');
  } catch (err) {
    setMessage(`Failed to stop session: ${errorMessage(err)}`, 'msg-error');
  }
}

function startPlaylistRun() {
  return startRun('playlist');
}

function startDynamicRun() {
  return startRun('dynamic');
}

async function startRun(mode) {
  const stationId = el.control_station_id.value;
  if (!stationId) {
    setMessage('Select a station first.', 'msg-error');
    return;
  }
  updateRunActionState();
  const normalizedMode = String(mode || 'playlist').trim().toLowerCase();
  const startButton = normalizedMode === 'dynamic' ? el.control_start_dynamic : el.control_start_playlist;
  if (startButton.disabled) {
    setMessage(
      normalizedMode === 'dynamic'
        ? 'Cannot start run: select an available player for dynamic mode.'
        : 'Cannot start run: select a station first.',
      'msg-error'
    );
    return;
  }

  const args = {
    station_id: stationId,
    mode: normalizedMode,
  };

  const playlistOverride = el.control_source_playlist.value;
  if (playlistOverride) {
    const [provider, itemId] = splitComboValue(playlistOverride);
    args.source_playlist_provider_override = provider;
    args.source_playlist_id_override = itemId;
  }

  if (normalizedMode === 'playlist') {
    const sourcePlaytimeCap = Number.parseFloat(el.control_dynamic_source_playtime_cap.value || '');
    if (!Number.isNaN(sourcePlaytimeCap) && sourcePlaytimeCap >= 0) {
      args.dynamic_source_playtime_cap_override = sourcePlaytimeCap;
    }
  }

  if (normalizedMode === 'dynamic') {
    const playerOverride = el.control_player_id.value;
    if (playerOverride) {
      args.player_id_override = playerOverride;
    }
    const batchSize = parseInt(el.control_dynamic_batch_size.value || '', 10);
    if (!Number.isNaN(batchSize) && batchSize > 0) {
      args.dynamic_batch_size_override = batchSize;
    }
  }

  try {
    const result = await rpc('ai_radio/start', args);
    await refreshSessions();
    setMessage(`Started session ${result.session_id}.`, 'msg-ok');
  } catch (err) {
    setMessage(`Failed to start session: ${errorMessage(err)}`, 'msg-error');
  }
}

async function loadTemplateStation() {
  try {
    const template = await rpc('ai_radio/stations/template');
    populateStationEditor(template);
    state.loadedStationId = '';
    el.station_selector.value = '';
    showView('stations');
    setMessage('Template loaded into editor.', 'msg-ok');
  } catch (err) {
    setMessage(`Failed to load template: ${errorMessage(err)}`, 'msg-error');
  }
}

async function loadStation(stationId) {
  try {
    const station = await rpc('ai_radio/stations/get', { station_id: stationId });
    populateStationEditor(station);
    state.loadedStationId = station.id;
    el.station_selector.value = station.id;
    el.control_station_id.value = station.id;
    setMessage(`Loaded station ${station.name}.`, 'msg-ok');
  } catch (err) {
    setMessage(`Failed to load station: ${errorMessage(err)}`, 'msg-error');
  }
}

function populateStationEditor(station) {
  el.station_id.value = station.id || '';
  el.station_name.value = station.name || '';
  el.station_source_playlist_id.value = station.source_playlist_id || '';
  el.station_source_playlist_provider.value = station.source_playlist_provider || 'library';
  el.station_target_playlist_provider.value = station.target_playlist_provider || 'builtin';
  el.station_default_player_id.value = station.default_player_id || '';
  el.station_max_duration_minutes.value = String(station.max_duration_minutes || 0);
  el.station_dynamic_batch_size.value = String(station.dynamic_batch_size || 1);
  el.station_dynamic_prefetch_remaining_tracks.value = String(
    station.dynamic_prefetch_remaining_tracks || 2
  );
  el.station_dynamic_poll_seconds.value = String(station.dynamic_poll_seconds || 5);
  el.station_clear_queue_on_start.checked = station.clear_queue_on_start !== false;

  const sectionIds = Array.isArray(station.section_ids) && station.section_ids.length
    ? station.section_ids
    : (Array.isArray(station.sections) ? station.sections.map((item) => item.id).filter(Boolean) : []);

  const sectionOptions = state.sections
    .map((section) => ({ value: section.id, label: `${section.id} (${section.name || section.id})` }))
    .sort((a, b) => a.label.localeCompare(b.label));

  const missing = sectionIds
    .filter((id) => !sectionOptions.some((item) => item.value === id))
    .map((id) => ({ value: id, label: `${id} (missing)` }));

  fillMultiSelect(el.station_section_ids, [...sectionOptions, ...missing], sectionIds);

  const general = station.general || {};
  el.general_timezone.value = general.timezone || 'UTC';
  const location = general.location || {};
  el.general_location_city.value = location.city || '';
  el.general_location_country.value = location.country || '';
  const weatherProvider = String(general.weather_provider || 'open_meteo').replace('-', '_');
  el.general_weather_provider.value = weatherProvider;
  el.general_weather_timeout_seconds.value = String(general.weather_timeout_seconds ?? 20);
  el.general_model.value = general.model || 'gpt-4o-mini';
  el.general_temperature.value = String(general.temperature ?? 0.7);
  el.general_max_tokens.value = String(general.max_tokens ?? 900);
  el.general_openai_base_url.value = general.openai_base_url || 'https://api.openai.com/v1';
  el.general_section_store_path.value = general.section_store_path || 'ai_radio_sections';
  el.general_tts_provider.value = general.tts_provider || 'openai';
  el.general_openai_tts_model.value = general.openai_tts_model || 'gpt-4o-mini-tts';
  el.general_openai_tts_voice.value = general.openai_tts_voice || 'ballad';
  el.general_elevenlabs_model.value = general.elevenlabs_model || 'eleven_multilingual_v2';
  el.general_elevenlabs_voice_id.value = general.elevenlabs_voice_id || '';
  el.general_instructions.value = general.instructions || '';
  el.general_openai_tts_instructions.value = general.openai_tts_instructions || '';

  renderSectionOrder(station.section_order || []);

  const combo = comboValue(
    station.source_playlist_provider || 'library',
    station.source_playlist_id || ''
  );
  const hasPlaylistOption = Array.from(el.station_source_playlist_pick.options).some(
    (opt) => opt.value === combo
  );
  const useManualSource = (station.source_playlist_provider || 'library') !== 'library'
    || (Boolean(station.source_playlist_id) && !hasPlaylistOption);
  el.station_source_manual_mode.checked = useManualSource;
  if (Array.from(el.station_source_playlist_pick.options).some((opt) => opt.value === combo)) {
    el.station_source_playlist_pick.value = combo;
  } else {
    el.station_source_playlist_pick.value = '';
  }
  syncSourceModeUi();
  syncEditorTtsUi();
  refreshFlowSectionOptions();
  updateRunActionState();
}

function collectStationFromEditor() {
  let sourcePlaylistProvider = (el.station_source_playlist_provider.value || 'library').trim() || 'library';
  let sourcePlaylistId = (el.station_source_playlist_id.value || '').trim();
  if (!el.station_source_manual_mode.checked && el.station_source_playlist_pick.value) {
    const [provider, itemId] = splitComboValue(el.station_source_playlist_pick.value);
    sourcePlaylistProvider = provider;
    sourcePlaylistId = itemId;
  }

  return {
    id: (el.station_id.value || '').trim(),
    name: (el.station_name.value || '').trim(),
    source_playlist_id: sourcePlaylistId,
    source_playlist_provider: sourcePlaylistProvider,
    target_playlist_provider: (el.station_target_playlist_provider.value || 'builtin').trim() || 'builtin',
    default_player_id: (el.station_default_player_id.value || '').trim(),
    max_duration_minutes: parseFloat(el.station_max_duration_minutes.value || '0') || 0,
    dynamic_batch_size: parseInt(el.station_dynamic_batch_size.value || '1', 10) || 1,
    dynamic_prefetch_remaining_tracks:
      parseInt(el.station_dynamic_prefetch_remaining_tracks.value || '2', 10) || 2,
    dynamic_poll_seconds: parseInt(el.station_dynamic_poll_seconds.value || '5', 10) || 5,
    clear_queue_on_start: Boolean(el.station_clear_queue_on_start.checked),
    section_ids: getSelectedMultiValues(el.station_section_ids),
    general: {
      timezone: (el.general_timezone.value || 'UTC').trim() || 'UTC',
      location: {
        city: (el.general_location_city.value || '').trim(),
        country: (el.general_location_country.value || '').trim(),
      },
      weather_provider: (el.general_weather_provider.value || 'open_meteo').trim() || 'open_meteo',
      weather_timeout_seconds: parseInt(el.general_weather_timeout_seconds.value || '20', 10) || 20,
      model: (el.general_model.value || 'gpt-4o-mini').trim() || 'gpt-4o-mini',
      temperature: parseFloat(el.general_temperature.value || '0.7') || 0.7,
      max_tokens: parseInt(el.general_max_tokens.value || '900', 10) || 900,
      openai_base_url: (el.general_openai_base_url.value || 'https://api.openai.com/v1').trim() || 'https://api.openai.com/v1',
      section_store_path: (el.general_section_store_path.value || 'ai_radio_sections').trim() || 'ai_radio_sections',
      instructions: el.general_instructions.value || '',
      tts_provider: el.general_tts_provider.value || 'openai',
      openai_tts_model: el.general_openai_tts_model.value || '',
      openai_tts_voice: el.general_openai_tts_voice.value || '',
      openai_tts_instructions: el.general_openai_tts_instructions.value || '',
      elevenlabs_model: el.general_elevenlabs_model.value || '',
      elevenlabs_voice_id: el.general_elevenlabs_voice_id.value || '',
    },
    section_order: collectSectionOrder(),
  };
}

async function saveCurrentStation() {
  const station = collectStationFromEditor();
  if (!station.name) {
    setMessage('Station name is required.', 'msg-error');
    return;
  }
  if (!station.source_playlist_id) {
    setMessage('Source playlist is required.', 'msg-error');
    return;
  }
  if (!station.section_ids.length) {
    setMessage('Select at least one section for this station.', 'msg-error');
    return;
  }

  try {
    const saved = await rpc('ai_radio/stations/save', { station });
    state.loadedStationId = saved.id;
    await loadAllData();
    await loadStation(saved.id);
    setMessage(`Saved station ${saved.name}.`, 'msg-ok');
  } catch (err) {
    setMessage(`Save failed: ${errorMessage(err)}`, 'msg-error');
  }
}

async function validateCurrentStation() {
  const station = collectStationFromEditor();
  try {
    await rpc('ai_radio/stations/validate', { station });
    setMessage('Station config is valid.', 'msg-ok');
  } catch (err) {
    setMessage(`Validation failed: ${errorMessage(err)}`, 'msg-error');
  }
}

async function deleteCurrentStation() {
  const stationId = (el.station_id.value || '').trim() || state.loadedStationId;
  if (!stationId) {
    setMessage('No station selected for deletion.', 'msg-error');
    return;
  }
  if (!window.confirm(`Delete station ${stationId}?`)) {
    return;
  }
  try {
    await rpc('ai_radio/stations/delete', { station_id: stationId });
    state.loadedStationId = '';
    await loadAllData();
    if (state.stations.length) {
      await loadStation(state.stations[0].id);
    } else {
      await loadTemplateStation();
    }
    setMessage(`Deleted station ${stationId}.`, 'msg-warn');
  } catch (err) {
    setMessage(`Delete failed: ${errorMessage(err)}`, 'msg-error');
  }
}

function exportCurrentStation() {
  try {
    const station = collectStationFromEditor();
    const stationName = station.id || station.name || 'station';
    const blob = new Blob([JSON.stringify(station, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `${stationName}.json`;
    document.body.append(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    setMessage('Exported station JSON.', 'msg-ok');
  } catch (err) {
    setMessage(`Export failed: ${errorMessage(err)}`, 'msg-error');
  }
}

async function importStationJson(event) {
  const input = event.target;
  const file = input.files && input.files[0];
  if (!file) return;
  try {
    const text = await file.text();
    const data = JSON.parse(text);
    let station = data;
    if (Array.isArray(data.stations) && data.stations.length) {
      station = data.stations[0];
      setMessage('Imported first station from stations array.', 'msg-warn');
    }
    if (!station || typeof station !== 'object') {
      throw new Error('JSON does not contain a station object');
    }
    populateStationEditor(station);
    state.loadedStationId = station.id || '';
    showView('stations');
    setMessage('Imported station JSON into editor. Click Save to persist.', 'msg-ok');
  } catch (err) {
    setMessage(`Import failed: ${errorMessage(err)}`, 'msg-error');
  } finally {
    input.value = '';
  }
}

function loadSectionIntoEditor(sectionId) {
  const section = state.sections.find((item) => item.id === sectionId);
  if (!section) return;
  state.loadedSectionId = sectionId;
  el.section_selector.value = sectionId;
  el.section_id.value = section.id || '';
  el.section_name.value = section.name || '';
  el.section_type.value = section.type || 'ai_text';
  el.section_web_search.value = section.web_search || 'disabled';
  const constraints = section.constraints || {};
  el.section_max_chars.value = String(constraints.max_chars || 0);
  el.section_prompt.value = section.prompt || '';
  syncSectionEditorTypeUi();
}

async function loadSectionTemplate() {
  try {
    const template = await rpc('ai_radio/sections/template');
    state.loadedSectionId = '';
    el.section_selector.value = '';
    el.section_id.value = template.id || '';
    el.section_name.value = template.name || '';
    el.section_type.value = template.type || 'ai_text';
    el.section_web_search.value = template.web_search || 'disabled';
    el.section_max_chars.value = String(template.constraints?.max_chars || 0);
    el.section_prompt.value = template.prompt || '';
    syncSectionEditorTypeUi();
    showView('sections');
    setMessage('Section template loaded.', 'msg-ok');
  } catch (err) {
    setMessage(`Failed to load section template: ${errorMessage(err)}`, 'msg-error');
  }
}

function collectSectionFromEditor() {
  const sectionType = (el.section_type.value || 'ai_text').trim();
  const section = {
    id: (el.section_id.value || '').trim(),
    name: (el.section_name.value || '').trim(),
    type: sectionType,
    prompt: el.section_prompt.value || '',
  };
  if (sectionType === 'ai_text') {
    section.web_search = el.section_web_search.value || 'disabled';
    const maxChars = parseInt(el.section_max_chars.value || '0', 10) || 0;
    section.constraints = { max_chars: maxChars };
  }
  return section;
}

async function saveCurrentSection() {
  const section = collectSectionFromEditor();
  if (!section.id) {
    setMessage('Section ID is required.', 'msg-error');
    return;
  }
  if (!section.name) {
    setMessage('Section name is required.', 'msg-error');
    return;
  }
  if (!String(section.prompt || '').trim()) {
    setMessage('Section prompt is required.', 'msg-error');
    return;
  }
  try {
    const saved = await rpc('ai_radio/sections/save', { section });
    state.loadedSectionId = saved.id;
    await loadAllData();
    loadSectionIntoEditor(saved.id);
    setMessage(`Saved section ${saved.id}.`, 'msg-ok');
  } catch (err) {
    setMessage(`Section save failed: ${errorMessage(err)}`, 'msg-error');
  }
}

async function deleteCurrentSection() {
  const sectionId = (el.section_id.value || '').trim() || state.loadedSectionId;
  if (!sectionId) {
    setMessage('No section selected for deletion.', 'msg-error');
    return;
  }
  if (!window.confirm(`Delete section ${sectionId}?`)) {
    return;
  }
  try {
    await rpc('ai_radio/sections/delete', { section_id: sectionId });
    state.loadedSectionId = '';
    await loadAllData();
    if (state.sections.length) {
      loadSectionIntoEditor(state.sections[0].id);
    } else {
      await loadSectionTemplate();
    }
    setMessage(`Deleted section ${sectionId}.`, 'msg-warn');
  } catch (err) {
    setMessage(`Section delete failed: ${errorMessage(err)}`, 'msg-error');
  }
}

function exportCurrentSection() {
  try {
    const section = collectSectionFromEditor();
    const sectionName = section.id || 'section';
    const blob = new Blob([JSON.stringify(section, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `${sectionName}.json`;
    document.body.append(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    setMessage('Exported section JSON.', 'msg-ok');
  } catch (err) {
    setMessage(`Section export failed: ${errorMessage(err)}`, 'msg-error');
  }
}

async function importSectionJson(event) {
  const input = event.target;
  const file = input.files && input.files[0];
  if (!file) return;
  try {
    const text = await file.text();
    const data = JSON.parse(text);
    if (!data || typeof data !== 'object') {
      throw new Error('JSON does not contain a section object');
    }
    el.section_id.value = data.id || '';
    el.section_name.value = data.name || '';
    el.section_type.value = data.type || 'ai_text';
    el.section_web_search.value = data.web_search || 'disabled';
    el.section_max_chars.value = String(data.constraints?.max_chars || 0);
    el.section_prompt.value = data.prompt || '';
    syncSectionEditorTypeUi();
    showView('sections');
    setMessage('Imported section JSON into editor. Click Save to persist.', 'msg-ok');
  } catch (err) {
    setMessage(`Section import failed: ${errorMessage(err)}`, 'msg-error');
  } finally {
    input.value = '';
  }
}

function getStationSectionOptions(includeEmpty = false) {
  const selectedIds = getSelectedMultiValues(el.station_section_ids);
  const options = selectedIds.map((id) => ({ value: id, label: id }));
  if (includeEmpty) {
    options.push({ value: 'EMPTY_SECTION', label: 'EMPTY_SECTION (no spoken segment)' });
  }
  return options;
}

function populateFlowSectionSelect(selectEl, value, includeEmpty = false) {
  const options = getStationSectionOptions(includeEmpty);
  fillSelect(selectEl, options);
  if (options.some((item) => item.value === value)) {
    selectEl.value = value;
  }
}

function refreshFlowSectionOptions() {
  document.querySelectorAll('select[data-role="flow-section"]').forEach((selectEl) => {
    const currentValue = selectEl.value;
    const includeEmpty = selectEl.dataset.allowEmpty === '1';
    populateFlowSectionSelect(selectEl, currentValue, includeEmpty);
  });
}

function renderSectionOrder(sectionOrder) {
  el.order_rules.innerHTML = '';
  if (!Array.isArray(sectionOrder) || !sectionOrder.length) {
    el.order_rules.append(createOrderRule());
    return;
  }
  sectionOrder.forEach((rule) => el.order_rules.append(createOrderRule(rule)));
}

function createOrderRule(rule = {}) {
  const item = document.createElement('div');
  item.className = 'order-rule';
  item.innerHTML = `
    <div class="order-rule-head">
      <label>Placement
        <select class="rule-when">
          <option value="start_of_playlist">Start of Playlist</option>
          <option value="between_songs">Between Songs</option>
          <option value="end_of_playlist">End of Playlist</option>
        </select>
      </label>
      <button type="button" class="rule-add-flow">Add Flow Item</button>
      <button type="button" class="rule-up">Up</button>
      <button type="button" class="rule-down">Down</button>
      <button type="button" class="rule-remove">Remove Rule</button>
    </div>
    <div class="rule-flows"></div>
  `;

  const flowContainer = item.querySelector('.rule-flows');
  item.querySelector('.rule-when').value = rule.when || 'between_songs';

  const flows = Array.isArray(rule.flow) && rule.flow.length ? rule.flow : [{ MUST: '' }];
  flows.forEach((flow) => flowContainer.append(createFlowItem(flow)));

  item.querySelector('.rule-add-flow').addEventListener('click', () => {
    flowContainer.append(createFlowItem({ MUST: '' }));
  });
  item.querySelector('.rule-remove').addEventListener('click', () => item.remove());
  item.querySelector('.rule-up').addEventListener('click', () => moveElement(item, -1));
  item.querySelector('.rule-down').addEventListener('click', () => moveElement(item, 1));

  return item;
}

function createFlowItem(flow = {}) {
  const item = document.createElement('div');
  item.className = 'flow-item';

  let flowType = 'MUST';
  let flowData = { section: '' };
  if (flow.MUST !== undefined) {
    flowType = 'MUST';
    flowData = { section: flow.MUST || '' };
  } else if (flow.ALTERNATIVE !== undefined) {
    flowType = 'ALTERNATIVE';
    flowData = flow.ALTERNATIVE || { choices: [] };
  } else if (flow.OPTIONAL !== undefined) {
    flowType = 'OPTIONAL';
    flowData = flow.OPTIONAL || { section: '', chance: 0.5, guards: {} };
  }

  item.innerHTML = `
    <div class="flow-item-header">
      <label>Flow Type
        <select class="flow-type">
          <option value="MUST">MUST (Always Include)</option>
          <option value="ALTERNATIVE">ALTERNATIVE (Pick One Weighted Choice)</option>
          <option value="OPTIONAL">OPTIONAL (Independent Chance + Guards)</option>
        </select>
      </label>
      <button type="button" class="flow-remove">Remove</button>
    </div>
    <div class="flow-body"></div>
  `;

  item.querySelector('.flow-type').value = flowType;
  renderFlowBody(item, flowType, flowData);

  item.querySelector('.flow-type').addEventListener('change', () => {
    const nextType = item.querySelector('.flow-type').value;
    renderFlowBody(item, nextType, {});
  });
  item.querySelector('.flow-remove').addEventListener('click', () => item.remove());

  return item;
}

function renderFlowBody(item, flowType, flowData) {
  const body = item.querySelector('.flow-body');
  body.innerHTML = '';

  if (flowType === 'MUST') {
    const select = document.createElement('select');
    select.className = 'must-section-select';
    select.dataset.role = 'flow-section';
    select.dataset.allowEmpty = '0';
    populateFlowSectionSelect(select, flowData.section || '', false);
    body.append(select);
    return;
  }

  if (flowType === 'ALTERNATIVE') {
    const choicesWrap = document.createElement('div');
    choicesWrap.className = 'alt-choices';
    const choices = Array.isArray(flowData.choices) && flowData.choices.length
      ? flowData.choices
      : [{ section: '', weight: 100 }];
    choices.forEach((choice) => choicesWrap.append(createAlternativeChoice(choice)));

    const addButton = document.createElement('button');
    addButton.type = 'button';
    addButton.textContent = 'Add Choice';
    addButton.addEventListener('click', () => {
      choicesWrap.append(createAlternativeChoice({ section: '', weight: 100 }));
    });

    body.append(choicesWrap, addButton);
    return;
  }

  const optional = flowData || {};
  const guards = optional.guards || {};
  const placeholders = Array.isArray(guards.require_placeholders_present)
    ? guards.require_placeholders_present.join(', ')
    : '';

  const sectionWrap = document.createElement('label');
  sectionWrap.textContent = 'Section Key';
  const sectionSelect = document.createElement('select');
  sectionSelect.className = 'optional-section-select';
  sectionSelect.dataset.role = 'flow-section';
  sectionSelect.dataset.allowEmpty = '0';
  populateFlowSectionSelect(sectionSelect, optional.section || '', false);
  sectionWrap.append(sectionSelect);

  const grid = document.createElement('div');
  grid.className = 'grid two-col';
  grid.innerHTML = `
    <label>Chance (0..1 or 0..100)
      <input class="optional-chance" type="number" min="0" step="0.01" value="${escapeAttr(String(optional.chance ?? 0.5))}">
    </label>
    <label>Guard: Minimum Song Gap
      <input class="optional-min-gap" type="number" min="0" step="1" value="${escapeAttr(String(guards.min_gap_songs || 0))}">
    </label>
    <label>Guard: Max Per 60 Minutes
      <input class="optional-max-per" type="number" min="0" step="1" value="${escapeAttr(String(guards.max_per_60min || 0))}">
    </label>
    <label>Guard: Required Placeholders (comma-separated)
      <input class="optional-placeholders" type="text" value="${escapeAttr(placeholders)}">
    </label>
  `;

  body.append(sectionWrap, grid);
}

function createAlternativeChoice(choice = {}) {
  const row = document.createElement('div');
  row.className = 'alt-choice';

  const sectionSelect = document.createElement('select');
  sectionSelect.className = 'alt-section-select';
  sectionSelect.dataset.role = 'flow-section';
  sectionSelect.dataset.allowEmpty = '1';
  populateFlowSectionSelect(sectionSelect, choice.section || '', true);

  const weightInput = document.createElement('input');
  weightInput.className = 'alt-weight';
  weightInput.type = 'number';
  weightInput.min = '0';
  weightInput.step = '1';
  weightInput.value = String(choice.weight ?? 100);

  const removeButton = document.createElement('button');
  removeButton.type = 'button';
  removeButton.className = 'alt-remove';
  removeButton.textContent = 'Remove';
  removeButton.addEventListener('click', () => row.remove());

  row.append(sectionSelect, weightInput, removeButton);
  return row;
}

function moveElement(element, direction) {
  const sibling = direction < 0 ? element.previousElementSibling : element.nextElementSibling;
  if (!sibling) return;
  if (direction < 0) {
    element.parentNode.insertBefore(element, sibling);
  } else {
    element.parentNode.insertBefore(sibling, element);
  }
}

function collectSectionOrder() {
  const output = [];
  el.order_rules.querySelectorAll('.order-rule').forEach((ruleElem) => {
    const when = ruleElem.querySelector('.rule-when').value;
    const flow = [];
    ruleElem.querySelectorAll('.flow-item').forEach((flowElem) => {
      const type = flowElem.querySelector('.flow-type').value;
      if (type === 'MUST') {
        const section = (flowElem.querySelector('.must-section-select').value || '').trim();
        if (section) {
          flow.push({ MUST: section });
        }
        return;
      }
      if (type === 'ALTERNATIVE') {
        const choices = [];
        flowElem.querySelectorAll('.alt-choice').forEach((choiceElem) => {
          const section = (choiceElem.querySelector('.alt-section-select').value || '').trim();
          const weight = parseFloat(choiceElem.querySelector('.alt-weight').value || '0') || 0;
          if (section) {
            choices.push({ section, weight });
          }
        });
        if (choices.length) {
          flow.push({ ALTERNATIVE: { choices } });
        }
        return;
      }

      const optionalSection = (flowElem.querySelector('.optional-section-select').value || '').trim();
      if (!optionalSection) {
        return;
      }
      const chance = parseFloat(flowElem.querySelector('.optional-chance').value || '0');
      const minGap = parseInt(flowElem.querySelector('.optional-min-gap').value || '0', 10) || 0;
      const maxPer = parseInt(flowElem.querySelector('.optional-max-per').value || '0', 10) || 0;
      const placeholdersRaw = flowElem.querySelector('.optional-placeholders').value || '';
      const placeholderList = placeholdersRaw
        .split(',')
        .map((item) => item.trim())
        .filter(Boolean);
      const guards = {};
      if (minGap > 0) guards.min_gap_songs = minGap;
      if (maxPer > 0) guards.max_per_60min = maxPer;
      if (placeholderList.length) guards.require_placeholders_present = placeholderList;
      flow.push({ OPTIONAL: { section: optionalSection, chance: Number.isFinite(chance) ? chance : 0.5, guards } });
    });

    if (flow.length) {
      output.push({ when, flow });
    }
  });
  return output;
}

async function ensureStationTemplate() {
  if (state.stationTemplate) return;
  state.stationTemplate = await rpc('ai_radio/stations/template');
}

async function ensureSectionTemplate() {
  if (state.sectionTemplate) return;
  state.sectionTemplate = await rpc('ai_radio/sections/template');
}

async function openStationWizard() {
  try {
    await ensureStationTemplate();
    await ensureSectionTemplate();

    state.wizardStep = 1;
    state.wizardIdTouched = false;
    setWizardError('');

    const template = deepClone(state.stationTemplate);
    const suggestedName = `My AI Radio ${state.stations.length + 1}`;
    el.wizard_name.value = suggestedName;
    el.wizard_id.value = slugify(suggestedName);

    const sourceCombo = comboValue(
      template.source_playlist_provider || 'library',
      template.source_playlist_id || ''
    );
    el.wizard_source_playlist.value = Array.from(el.wizard_source_playlist.options).some((opt) => opt.value === sourceCombo)
      ? sourceCombo
      : '';

    el.wizard_default_player.value = template.default_player_id || '';
    el.wizard_max_duration_minutes.value = String(template.max_duration_minutes || 0);

    const templateSectionIds = Array.isArray(template.section_ids) ? template.section_ids : RECOMMENDED_SECTION_IDS;
    const wizardSectionOptions = state.sections
      .map((section) => ({ value: section.id, label: `${section.id} (${section.name || section.id})` }))
      .sort((a, b) => a.label.localeCompare(b.label));
    fillMultiSelect(el.wizard_section_ids, wizardSectionOptions, templateSectionIds);

    el.wizard_custom_section_id.value = '';
    el.wizard_custom_section_name.value = '';
    el.wizard_custom_section_type.value = 'ai_text';
    el.wizard_custom_section_web_search.value = 'disabled';
    el.wizard_custom_section_max_chars.value = '0';
    el.wizard_custom_section_prompt.value = '';

    el.wizard_model.value = template.general?.model || 'gpt-4o-mini';
    el.wizard_tts_provider.value = template.general?.tts_provider || 'openai';
    el.wizard_openai_tts_voice.value = template.general?.openai_tts_voice || 'ballad';
    el.wizard_elevenlabs_voice_id.value = template.general?.elevenlabs_voice_id || '';
    el.wizard_timezone.value = template.general?.timezone || 'UTC';
    el.wizard_location_city.value = template.general?.location?.city || '';
    el.wizard_location_country.value = template.general?.location?.country || '';
    el.wizard_order_mode.value = inferWizardOrderMode(template);
    el.wizard_instructions.value = template.general?.instructions || '';

    renderWizardStep();
    syncWizardTtsUi();
    refreshWizardSummary();
    el.wizard_modal.classList.remove('hidden');
  } catch (err) {
    setMessage(`Failed to open wizard: ${errorMessage(err)}`, 'msg-error');
  }
}

function closeStationWizard() {
  setWizardError('');
  el.wizard_modal.classList.add('hidden');
}

function wizardBack() {
  state.wizardStep = Math.max(1, state.wizardStep - 1);
  setWizardError('');
  renderWizardStep();
}

function wizardNext() {
  if (!validateWizardStep(state.wizardStep)) {
    return;
  }
  state.wizardStep = Math.min(5, state.wizardStep + 1);
  setWizardError('');
  renderWizardStep();
  refreshWizardSummary();
}

function validateWizardStep(step) {
  if (step === 1) {
    if (!el.wizard_name.value.trim()) {
      setWizardError('Station name is required.');
      return false;
    }
    if (!el.wizard_id.value.trim()) {
      setWizardError('Station ID is required.');
      return false;
    }
  }

  if (step === 2) {
    if (!el.wizard_source_playlist.value) {
      setWizardError('Select a source playlist.');
      return false;
    }
  }

  if (step === 3) {
    const selectedSections = getSelectedMultiValues(el.wizard_section_ids);
    if (!selectedSections.length) {
      setWizardError('Select at least one section.');
      return false;
    }
    const hasCore = selectedSections.includes('Song_Introduction_Start')
      && selectedSections.includes('Song_Transition')
      && selectedSections.includes('Song_Introduction_End');
    if (!hasCore) {
      setWizardError(
        'Recommended core sections are required for the starter flow. Click "Select Recommended Sections" first.'
      );
      return false;
    }
  }

  setWizardError('');
  return true;
}

function renderWizardStep() {
  for (let i = 1; i <= 5; i += 1) {
    const node = document.getElementById(`wizard_step_${i}`);
    if (!node) continue;
    node.classList.toggle('hidden', i !== state.wizardStep);
  }
  el.wizard_step_indicator.textContent = `Step ${state.wizardStep} of 5`;
  el.wizard_back.disabled = state.wizardStep === 1;
  const onLast = state.wizardStep === 5;
  el.wizard_next.classList.toggle('hidden', onLast);
  el.wizard_save.classList.toggle('hidden', !onLast);
}

function selectRecommendedWizardSections() {
  const selected = new Set(getSelectedMultiValues(el.wizard_section_ids));
  RECOMMENDED_SECTION_IDS.forEach((id) => {
    if (state.sections.some((section) => section.id === id)) {
      selected.add(id);
    }
  });
  Array.from(el.wizard_section_ids.options).forEach((opt) => {
    opt.selected = selected.has(opt.value);
  });
  refreshWizardSummary();
}

async function createWizardCustomSection() {
  const sectionType = (el.wizard_custom_section_type.value || 'ai_text').trim();
  const section = {
    id: (el.wizard_custom_section_id.value || '').trim(),
    name: (el.wizard_custom_section_name.value || '').trim(),
    type: sectionType,
    prompt: (el.wizard_custom_section_prompt.value || '').trim(),
  };
  if (!section.id || !section.name || !section.prompt) {
    setWizardError('Custom section requires ID, name, and prompt.');
    return;
  }
  if (sectionType === 'ai_text') {
    section.web_search = el.wizard_custom_section_web_search.value || 'disabled';
    section.constraints = {
      max_chars: parseInt(el.wizard_custom_section_max_chars.value || '0', 10) || 0,
    };
  }

  try {
    const saved = await rpc('ai_radio/sections/save', { section });
    await loadAllData();

    Array.from(el.wizard_section_ids.options).forEach((opt) => {
      if (opt.value === saved.id) {
        opt.selected = true;
      }
    });

    el.wizard_custom_section_id.value = '';
    el.wizard_custom_section_name.value = '';
    el.wizard_custom_section_prompt.value = '';
    setWizardError('');
    refreshWizardSummary();
    setMessage(`Created section ${saved.id} and added it to wizard selection.`, 'msg-ok');
  } catch (err) {
    setWizardError(`Failed to create section: ${errorMessage(err)}`);
  }
}

function inferWizardOrderMode(station) {
  const betweenRule = Array.isArray(station?.section_order)
    ? station.section_order.find((rule) => rule?.when === 'between_songs')
    : null;
  if (!betweenRule || !Array.isArray(betweenRule.flow)) {
    return 'balanced';
  }
  const hasMustTransition = betweenRule.flow.some(
    (item) => item && typeof item === 'object' && item.MUST === 'Song_Transition'
  );
  if (hasMustTransition) return 'every_song';

  const alt = betweenRule.flow.find((item) => item && item.ALTERNATIVE);
  const choices = alt && alt.ALTERNATIVE && Array.isArray(alt.ALTERNATIVE.choices)
    ? alt.ALTERNATIVE.choices
    : [];
  const hasEmpty = choices.some((choice) => choice && choice.section === 'EMPTY_SECTION');
  const transitionChoice = choices.find((choice) => choice && choice.section === 'Song_Transition');
  const transitionWeight = Number(transitionChoice?.weight || 0);
  if (hasEmpty && transitionWeight <= 60) return 'light';
  return 'balanced';
}

function applyWizardOrderMode(station, mode) {
  const sectionIds = Array.isArray(station.section_ids) ? station.section_ids : [];
  const startSectionId = sectionIds.includes('Song_Introduction_Start')
    ? 'Song_Introduction_Start'
    : (sectionIds[0] || 'Song_Introduction_Start');
  const transitionSectionId = sectionIds.includes('Song_Transition')
    ? 'Song_Transition'
    : (sectionIds.find((id) => id !== startSectionId) || startSectionId);
  const endSectionId = sectionIds.includes('Song_Introduction_End')
    ? 'Song_Introduction_End'
    : (sectionIds[sectionIds.length - 1] || transitionSectionId);
  const weatherSectionId = sectionIds.includes('Weather_Short') ? 'Weather_Short' : null;

  const flow = [];

  if (mode === 'every_song') {
    flow.push({ MUST: transitionSectionId });
  } else if (mode === 'light') {
    flow.push({
      ALTERNATIVE: {
        choices: [
          { section: transitionSectionId, weight: 55 },
          { section: 'EMPTY_SECTION', weight: 45 },
        ],
      },
    });
  } else {
    flow.push({
      ALTERNATIVE: {
        choices: [
          { section: transitionSectionId, weight: 80 },
          { section: 'EMPTY_SECTION', weight: 20 },
        ],
      },
    });
  }

  if (weatherSectionId) {
    flow.push({
      OPTIONAL: {
        section: weatherSectionId,
        chance: mode === 'light' ? 0.08 : 0.15,
        guards: {
          min_gap_songs: 3,
          max_per_60min: 1,
          require_placeholders_present: ['<weather_hourly>', '<timestamp>'],
        },
      },
    });
  }

  station.section_order = [
    {
      when: 'start_of_playlist',
      flow: [{ MUST: startSectionId }],
    },
    {
      when: 'between_songs',
      flow,
    },
    {
      when: 'end_of_playlist',
      flow: [{ MUST: endSectionId }],
    },
  ];
}

function refreshWizardSummary() {
  const [sourceProvider, sourcePlaylistId] = splitComboValue(el.wizard_source_playlist.value || 'library:::');
  const providerLabel = el.wizard_tts_provider.value === 'elevenlabs' ? 'ElevenLabs' : 'OpenAI';
  const voiceLabel = el.wizard_tts_provider.value === 'elevenlabs'
    ? (el.wizard_elevenlabs_voice_id.value || '(not set)')
    : (el.wizard_openai_tts_voice.value || '(not set)');

  const orderLabelMap = {
    balanced: 'Balanced',
    every_song: 'Every transition',
    light: 'Lighter talk, more music',
  };
  const selectedSectionIds = getSelectedMultiValues(el.wizard_section_ids);

  el.wizard_summary.innerHTML = `
    <ul class="guide-list wizard-summary-list">
      <li><strong>Name:</strong> ${escapeHtml(el.wizard_name.value || '-')}</li>
      <li><strong>ID:</strong> ${escapeHtml(el.wizard_id.value || '-')}</li>
      <li><strong>Source Playlist:</strong> ${escapeHtml(sourceProvider)}:${escapeHtml(sourcePlaylistId || '(none)')}</li>
      <li><strong>Default Player:</strong> ${escapeHtml(el.wizard_default_player.value || '(none)')}</li>
      <li><strong>Selected Sections:</strong> ${escapeHtml(selectedSectionIds.join(', ') || '(none)')}</li>
      <li><strong>Model:</strong> ${escapeHtml(el.wizard_model.value || '-')}</li>
      <li><strong>TTS:</strong> ${escapeHtml(providerLabel)} / ${escapeHtml(voiceLabel)}</li>
      <li><strong>Timezone:</strong> ${escapeHtml(el.wizard_timezone.value || 'UTC')}</li>
      <li><strong>Weather Location:</strong> ${escapeHtml(el.wizard_location_city.value || '-')} ${escapeHtml(el.wizard_location_country.value || '-')}</li>
      <li><strong>Flow Style:</strong> ${escapeHtml(orderLabelMap[el.wizard_order_mode.value] || el.wizard_order_mode.value)}</li>
    </ul>
  `;
}

function buildStationFromWizard() {
  const template = deepClone(state.stationTemplate || {});
  const station = template;

  const [sourceProvider, sourcePlaylistId] = splitComboValue(el.wizard_source_playlist.value);

  station.id = (el.wizard_id.value || '').trim();
  station.name = (el.wizard_name.value || '').trim();
  station.source_playlist_provider = sourceProvider || 'library';
  station.source_playlist_id = sourcePlaylistId || '';
  station.default_player_id = (el.wizard_default_player.value || '').trim();
  station.max_duration_minutes = parseFloat(el.wizard_max_duration_minutes.value || '0') || 0;
  station.section_ids = getSelectedMultiValues(el.wizard_section_ids);

  if (!station.general || typeof station.general !== 'object') {
    station.general = {};
  }

  station.general.model = (el.wizard_model.value || 'gpt-4o-mini').trim() || 'gpt-4o-mini';
  station.general.timezone = (el.wizard_timezone.value || 'UTC').trim() || 'UTC';
  station.general.instructions = el.wizard_instructions.value || '';
  station.general.tts_provider = el.wizard_tts_provider.value || 'openai';

  if (!station.general.location || typeof station.general.location !== 'object') {
    station.general.location = {};
  }
  station.general.location.city = (el.wizard_location_city.value || '').trim();
  station.general.location.country = (el.wizard_location_country.value || '').trim();

  if (station.general.tts_provider === 'openai') {
    station.general.openai_tts_voice = (el.wizard_openai_tts_voice.value || 'ballad').trim() || 'ballad';
  } else {
    station.general.elevenlabs_voice_id = (el.wizard_elevenlabs_voice_id.value || '').trim();
  }

  applyWizardOrderMode(station, el.wizard_order_mode.value || 'balanced');

  return station;
}

async function saveWizardStation() {
  if (!validateWizardStep(1) || !validateWizardStep(2) || !validateWizardStep(3)) {
    return;
  }

  try {
    const station = buildStationFromWizard();
    const saved = await rpc('ai_radio/stations/save', { station });
    state.loadedStationId = saved.id;
    closeStationWizard();
    await loadAllData();
    await loadStation(saved.id);
    showView('stations');
    setMessage(`Created station ${saved.name}.`, 'msg-ok');
  } catch (err) {
    setMessage(`Wizard save failed: ${errorMessage(err)}`, 'msg-error');
  }
}

function maybeStartTour() {
  if (state.tourShownThisSession) return;
  const seen = localStorage.getItem(TOUR_SEEN_STORAGE_KEY) === '1';
  if (seen) return;
  state.tourShownThisSession = true;
  openTour(true);
}

function openTour(resetSeenState = false) {
  if (resetSeenState) {
    localStorage.removeItem(TOUR_SEEN_STORAGE_KEY);
  }
  state.tourStep = 0;
  renderTourStep();
  el.tour_modal.classList.remove('hidden');
}

function renderTourStep() {
  const step = TOUR_STEPS[state.tourStep] || TOUR_STEPS[0];
  const bulletItems = Array.isArray(step.bullets)
    ? step.bullets.map((item) => `<li>${escapeHtml(item)}</li>`).join('')
    : '';
  el.tour_body.innerHTML = `
    <h3>${escapeHtml(step.title)}</h3>
    <p>${escapeHtml(step.body)}</p>
    ${bulletItems ? `<ul class="guide-list">${bulletItems}</ul>` : ''}
    <p class="hint compact">${state.tourStep + 1} / ${TOUR_STEPS.length}</p>
  `;

  el.tour_back.disabled = state.tourStep === 0;
  const last = state.tourStep === TOUR_STEPS.length - 1;
  el.tour_next.classList.toggle('hidden', last);
  el.tour_done.classList.toggle('hidden', !last);
}

function tourBack() {
  state.tourStep = Math.max(0, state.tourStep - 1);
  renderTourStep();
}

function tourNext() {
  state.tourStep = Math.min(TOUR_STEPS.length - 1, state.tourStep + 1);
  renderTourStep();
}

function closeTour() {
  localStorage.setItem(TOUR_SEEN_STORAGE_KEY, '1');
  el.tour_modal.classList.add('hidden');
}

function completeTour() {
  closeTour();
}

function slugify(value) {
  return String(value || '')
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '');
}

function deepClone(value) {
  if (value === null || value === undefined) return value;
  if (typeof structuredClone === 'function') {
    return structuredClone(value);
  }
  return JSON.parse(JSON.stringify(value));
}

function escapeAttr(value) {
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/"/g, '&quot;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}
