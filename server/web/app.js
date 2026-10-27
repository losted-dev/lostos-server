/* lostOS panel: iOS-style PWA for the speaker's server. Vanilla JS, springs for every motion. */
(() => {
'use strict';
/* A household with several stations: the panel talks to the one the person picked (the gateway sends the request to that station's process); the choice is remembered here */
const _fetch = window.fetch.bind(window);
window.fetch = (u, o = {}) => {
  try { const sid = localStorage.getItem('lostos.station'); if (sid && typeof u === 'string' && u.startsWith('/')) o = { ...o, headers: { ...(o.headers || {}), 'X-Lostos-Station': sid } }; } catch (e) { /* no storage: the first station answers */ }
  return _fetch(u, o);
};
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const esc = (t) => String(t ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const raf = requestAnimationFrame;
const isDesktop = () => matchMedia('(min-width: 900px)').matches;      // wide window: the sidebar layout of the CSS
const isChromium = !!(navigator.userAgentData && navigator.userAgentData.brands.some((b) => /Chromium/.test(b.brand))) && !/iPhone|iPad/.test(navigator.userAgent);

/* ---------------------------------------------------------------- spring */
function spring({ from, to, v = 0, k = 380, c = 30, m = 1, update, rest = 0.002 }) {
  let x = from, vel = v, last = performance.now(), dead = false, done;
  const p = new Promise((res) => (done = res));
  const tick = (t) => {
    if (dead) return;
    let dt = Math.min(0.034, (t - last) / 1000); last = t;
    const n = Math.max(1, Math.ceil(dt / 0.004)), h = dt / n;
    for (let i = 0; i < n; i++) { vel += ((-k * (x - to) - c * vel) / m) * h; x += vel * h; }
    if (Math.abs(x - to) < rest * (Math.abs(to - from) || 1) + 0.0005 && Math.abs(vel) < 0.02 * (Math.abs(to - from) || 1) + 0.002) { update(to, 0); done(); return; }
    update(x, vel); raf(tick);
  };
  raf(tick);
  return { cancel() { dead = true; done(); }, get v() { return vel; }, get x() { return x; }, promise: p };
}

/* ---------------------------------------------------------------- state and API */
const S = { s: null, tab: 'home', volBusy: 0, liked: false, opt: {}, sig: {} };
/* the splash of index.html stays until the first state and the tiles of the home page are on the screen, but at least one second
   from the start of the page (a splash that flashes by looks like a glitch), and at most 6 seconds */
const SPLASH_MIN_MS = 1000, SPLASH_MAX_MS = 6000;
const ready = {};
function hideSplash() {
  const sp = document.getElementById('splash');
  if (!sp || sp.dataset.closing) return;
  sp.dataset.closing = '1';
  setTimeout(() => { sp.classList.add('gone'); setTimeout(() => sp.remove(), 600); }, Math.max(0, SPLASH_MIN_MS - performance.now()));   // performance.now() counts from the start of the page
}
function markReady(key) { ready[key] = true; if (ready.state && ready.tiles) hideSplash(); }
setTimeout(hideSplash, SPLASH_MAX_MS);
async function api(a, x = {}) {
  try {
    const r = await fetch('/api/do', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ a, ...x }) });
    const j = await r.json();
    if (j && j.ok === false && j.error) toast(j.error);
    setTimeout(load, 350);
    return j;
  } catch (e) { toast('Нет связи с сервером'); return null; }
}
/* A server on the internet works only with a confirmed speaker: until then it answers 423 and the panel shows the account and the wizard that connects the speaker. */
function setLocked(on) {
  if (!!S.locked === on) return;
  const was = S.locked;
  S.locked = on;
  document.body.classList.toggle('locked', on);
  $$('#p-settings .row.nav:not(#rowAccount)').forEach((r) => r.classList.toggle('lock-hide', on));
  $$('#p-settings .navlist').forEach((l) => l.classList.toggle('lock-hide', on && !l.contains($('#rowAccount'))));
  if (on) cwShow();                                                 // no speaker yet: the whole screen is the window that connects one
  else if (was) location.reload();                                  // the speaker is connected: the whole panel comes alive, and the first setup opens by itself
}
/* ---- the window of a server without a speaker */
const CW = { on: false, timer: 0, state: '', claimed: false };
/* The identifier of a station is 20 hex digits; the installer shows it in groups of five. Whatever is pasted (spaces, dashes, capitals) becomes the plain 20 digits. */
const normId = (v) => String(v || '').toLowerCase().replace(/[^0-9a-f]/g, '').slice(0, 20);
function cwShow() {
  if (CW.on) return;
  CW.on = true; $('#connect').hidden = false;
  cwRender('enter');
  CW.timer = setInterval(cwPoll, 1500); cwPoll();
}
function cwRender(state, data) {
  const body = $('#cwBody'); CW.state = state;
  const addr = location.origin;
  if (state === 'enter') {
    $('#cwTitle').textContent = 'Подключим станцию'; $('#cwSub').textContent = 'Введите идентификатор из установщика, это 20 знаков';
    body.innerHTML = `<div class="cw-card"><div class="cw-field"><input class="cw-in" id="cwId" type="text" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="20 знаков из установщика"><button class="cw-btn" id="cwGo">Подключить</button></div><div class="cw-err" id="cwErr"></div></div>
      <div class="cw-card"><h3>На колонке ещё нет lostOS</h3><p>Запустите установщик lostOS на компьютере в той же сети, что и колонка</p>
      <ol class="cw-ol"><li>Выберите свой сервер и впишите адрес <code id="cwAddr">${esc(addr)}</code></li><li>Дождитесь конца установки</li><li>Установщик покажет идентификатор колонки: впишите его выше</li></ol>
      <div style="margin-top:12px"><button class="cw-btn cw-ghost" id="cwCopy" style="height:42px;font-size:15px;padding:0 20px">Скопировать адрес</button></div></div>`;
    const inp = $('#cwId'), go = $('#cwGo');
    const sync = () => { go.disabled = inp.value.trim().length < 8; };
    inp.addEventListener('input', () => { const n = normId(inp.value); if (inp.value !== n) inp.value = n; sync(); }); sync();
    const send = async () => {
      const v = normId(inp.value); if (!v) return;
      if (v.length !== 20) { $('#cwErr').textContent = 'Идентификатор — 20 знаков, у вас ' + v.length; return; }
      go.disabled = true; $('#cwErr').textContent = '';
      const r = await fetch('/api/v1/stations/claim', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id: v }) }).then((x) => x.json()).catch(() => ({}));
      if (r.state === 'press') { CW.claimed = true; cwRender('press', { left: 60 }); }
      else if (r.state === 'ok') { cwRender('done'); loadAccount(); }
      else { $('#cwErr').textContent = r.error || 'Не получилось, проверьте идентификатор'; sync(); }
    };
    go.addEventListener('click', send); inp.addEventListener('keydown', (e) => { if (e.key === 'Enter') send(); });
    $('#cwCopy').addEventListener('click', async () => { try { await navigator.clipboard.writeText(addr); toast('Адрес скопирован'); } catch (e) { toast(addr); } });
  } else if (state === 'press') {
    $('#cwTitle').textContent = 'Нажмите кнопку Алисы'; $('#cwSub').textContent = 'Так колонка подтвердит, что вы рядом';
    body.innerHTML = `<div class="cw-card cw-press"><div class="cw-ring"><span id="cwLeft">${data && data.left != null ? data.left : ''}</span></div><p>Нажмите кнопку Алисы на колонке, пока идёт отсчёт</p><div style="margin-top:18px"><button class="cw-btn cw-ghost" id="cwBack" style="height:42px;font-size:15px;padding:0 20px">Ввести другой идентификатор</button></div></div>`;
    $('#cwBack').addEventListener('click', () => { CW.claimed = false; cwRender('enter'); });
  } else if (state === 'done') {
    $('#cwTitle').textContent = 'Станция подключена'; $('#cwSub').textContent = 'Сейчас откроется первая настройка';
    body.innerHTML = '<div class="cw-card cw-press"><div class="cw-ring"><span>✓</span></div><p>Станция в сети</p></div>';
  }
}
async function cwPoll() {
  if (CW.stage === 'setup') return;
  let d = null;
  try { const r = await rawFetch('/api/v1/stations'); d = r.ok ? await r.json() : null; } catch (e) { d = null; }
  if (!d) return;
  const mine = d.stations || [];
  if (mine.some((s) => s.confirmed)) {
    if (CW.state !== 'done') cwRender('done');
    if (!CW.reset) { CW.reset = true; try { await fetch('/api/v1/setup', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ done: false }) }); } catch (e) { /* the setup is offered anyway */ } }   // a new station: the first setup is shown once more
    setTimeout(cwSetupStart, 1200);                                           // the same window goes on with the first setup (this is the only place where it lives)
    return;
  }          // the account tells "not locked" and the page reloads
  const w = mine.find((s) => !s.confirmed && s.press);
  if (w && CW.state !== 'press') cwRender('press', { left: w.press });
  if (w) { const el = $('#cwLeft'); if (el) el.textContent = w.press; }
  else if (CW.state === 'press' && CW.claimed) { CW.claimed = false; cwRender('enter'); $('#cwErr').textContent = 'Время вышло, введите идентификатор ещё раз'; }
}
if (/[?&]connect(&|$)/.test(location.search)) { try { cwShow(); } catch (e) { console.error('connect window', e); } }                  // ?connect shows this window for a look (debug)
/* The first setup: it is the third step of the window of the connection, shown once, right after the station is connected. Its pages are the old wizard moved into this window. */
async function cwSetupStart() {
  if (CW.stage === 'setup') return;
  const d = await suLoad();
  if (!d || d.done) { if (CW.on) location.reload(); return; }
  CW.stage = 'setup'; CW.on = true; clearInterval(CW.timer);
  $('#connect').hidden = false;
  $('#cwStStation').className = 'past'; $('#cwStStation').firstChild.textContent = '✓'; $('#cwStSetup').className = 'on'; $('#cwStSetup').firstChild.textContent = '3';
  $('.cw-head').hidden = true; $('#cwBody').hidden = true;
  const box = $('#cwSetup'); box.hidden = false; box.appendChild($('#sheetSetup'));
  SU.i = Math.max(1, suFirstTodo(d)); SU.open = true;
  try {                                                                      // back from Spotify: the step the person left, not the first one
    const back = JSON.parse(localStorage.getItem('lostos.suStep') || 'null'); localStorage.removeItem('lostos.suStep');
    const k = back && Date.now() - back.t < 30 * 60 * 1000 ? SU_STEPS.findIndex((x) => x[0] === back.id) : -1;
    if (k > 0) SU.i = k;
  } catch (e) { /* ignore */ }
  suRender();
  clearInterval(SU.timer);
  SU.timer = setInterval(async () => { if (!SU.open || (document.activeElement && document.activeElement.closest && document.activeElement.closest('#suBody') && document.activeElement.tagName === 'INPUT')) return; if (await suLoad()) suRender(true); }, 3000);
}
$('#cwOut').addEventListener('click', async () => { await fetch('/auth/logout', { method: 'POST' }); location.reload(); });
const rawFetch = window.fetch.bind(window);
window.fetch = async (...a) => { const r = await rawFetch(...a); if (r.status === 423) setLocked(true); return r; };
let loading = false;
async function load() {
  if (loading || document.hidden) return;
  loading = true;
  try { const r = await fetch('/api/state', { cache: 'no-store' }); if (r.status === 423) { loading = false; markReady('state'); return; } S.s = await r.json(); render(); setOnline(true); }
  catch (e) { setOnline(false); }
  markReady('state');
  loading = false;
}
function setOnline(ok) {
  const d = $('#status .dot'), t = $('#statusText');
  if (!ok) { d.className = 'dot bad'; t.textContent = 'Сервер не отвечает'; $('#sideDot').className = 'dot bad'; $('#sideText').textContent = 'Сервер не отвечает'; return; }
  const s = S.s;
  d.className = 'dot' + (s.connected ? '' : ' off');
  t.textContent = s.connected ? 'Яндекс Станция Мини 3 · В сети' : 'Не в сети';
  $('#sideDot').className = 'dot' + (s.connected ? '' : ' off'); $('#sideText').textContent = s.connected ? 'В сети' : 'Не в сети';
}
const once = (key, val, fn) => { const sig = typeof val === 'string' ? val : JSON.stringify(val); if (S.sig[key] === sig) return; S.sig[key] = sig; fn(); };
const fmt = (ms) => { const s = Math.max(0, Math.round(ms / 1000)); return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0'); };
const bigArt = (u) => (u || '').replace(/(ab67616d0000)(4851|1e02)/, '$1b273');   // Spotify cdn: 64 / 300 / 640 px variants of one cover
const midArt = (u) => (u || '').replace(/(ab67616d0000)(4851|b273)/, '$11e02');

/* ---------------------------------------------------------------- render */
function render() {
  const s = S.s; if (!s) return;
  const n = s.now || {};
  // home: the now playing card
  const has = !!n.source;
  $('#nowSrc').textContent = has ? n.source + (n.playing ? '' : ' · пауза') : 'lostOS';
  $('#nowTitle').textContent = has ? (n.title || n.source) : 'Ничего не играет';
  $('#nowArtist').textContent = has ? (n.artist || '') : 'Включи музыку на телефоне или скажи «Алиса»';
  const art = midArt(n.art);
  once('art', art, () => { const a = $('#nowArt'); a.style.backgroundImage = art ? `url("${art}")` : ''; a.classList.toggle('empty', !art); });
  // mini player
  const mini = $('#mini');
  mini.classList.toggle('hidden', !has);
  mini.classList.toggle('playing', !!n.playing);
  $('#miniTitle').textContent = n.title || n.source || '';
  $('#miniSub').textContent = n.artist || n.source || '';
  once('miniArt', art, () => { $('#miniThumb').style.backgroundImage = art ? `url("${art}")` : ''; });
  // player sheet
  $('#pPlay').classList.toggle('playing', !!n.playing);
  $('#pTitle').textContent = n.title || n.source || '—';
  $('#pArtist').textContent = n.artist || '';
  $('#pSrc').textContent = n.source ? n.source + (n.playing ? '' : ' · пауза') : '';
  const big = bigArt(n.art);
  once('bigArt', big, () => { const c = $('#pCover'); c.style.backgroundImage = big ? `url("${big}")` : ''; c.classList.toggle('empty', !big); tintFrom(big); });
  const dur = n.duration || 0;
  if (!S.seekDrag) S.prog = { ms: n.progress || 0, dur, at: Date.now(), playing: !!n.playing, seek: n.source === 'Spotify' && dur > 0 };
  paintProgress();
  syncLyricsSwitch();
  syncEq(s);
  updateLyrics(n);
  $('#pLike').hidden = n.source !== 'Spotify';        // only Spotify has a heart: AirPlay / Bluetooth tracks are not in anybody's library
  if (Date.now() > (S.likeHold || 0)) S.liked = !!n.liked;
  $('#pLike').classList.toggle('on', S.liked);
  // sources
  const bt = s.bluetooth || {};
  enrollProgress(s.enroll);
  applyTileStatus(s.tile_status);                           // the live line of the AirPlay and Spotify tiles (they belong to their plugins)
  if (Date.now() > (S.airHold || 0)) { S.airOn = !!s.airplay_on; $$('[data-tswitch="airplay"]').forEach((el) => el.classList.toggle('on', S.airOn)); }
  setSwitch('bluetooth', S.opt.bluetooth ?? (bt.on === undefined || bt.on === null ? false : !!bt.on));
  const btText = bt.on === false ? 'Выключен' : bt.playing ? 'Играет' : bt.connected ? 'Телефон подключён' : bt.on ? 'Ждёт телефон' : '—';
  $('#srcBt').textContent = btText; $('#btState').textContent = 'Bluetooth: ' + btText.toLowerCase();
  // volume
  if (Date.now() > S.volBusy && s.volume != null) setVol(s.volume);
  // utilities: alarms, timers, reminders
  if (s.t) S.skew = s.t * 1000 - Date.now();
  S.mirror = { phase: s.phase || 'idle', text: s.clock && s.clock.text ? s.clock.text : '' };
  const rem = s.reminders || [];
  once('rem', rem.map((r) => [r.id, r.kind, r.label, r.text, r.time, r.repeat, r.at, r.enabled, r.snooze_min, r.total]), () => renderUtils(rem));
  once('dlgHome', s.dialogs, () => renderDialogs(s));
  once('sel', s.selects, () => renderSelects(s.selects));
  once('voices', [s.voices, s.session], () => renderVoices(s));
  once('wifi', s.wifi, () => renderWifi(s.wifi));
  once('engines', s.engines, () => renderEngines(s.engines));
  once('scen', s.scenarios, () => renderScenarios(s.scenarios));
  renderSleep(s.sleep);
  renderNotifs(s.notifs);
  renderHttps(s.https);
  once('sys', [s.last, s.dialogs, s.connected, s.volume, s.wifi && s.wifi.cur, s.voices], () => renderSystem(s));
  $('#ctx').textContent = s.context || '—';
  const lg = $('#log'), stick = lg.scrollTop + lg.clientHeight >= lg.scrollHeight - 24;
  once('log', (s.log || []).slice(-60).join('\n'), () => { lg.textContent = (s.log || []).slice(-60).join('\n'); if (stick) lg.scrollTop = lg.scrollHeight; });
  setSwitchEl($('#swBarge'), S.opt.barge ?? !!s.barge);
  setSwitchEl($('#swVolClick'), S.opt.volclick ?? !!(s.switches && s.switches.volume_sound));
}
function tintFrom(url) {   // the cover's colour tints the background (works when the image server allows it)
  const set = (c) => { document.documentElement.style.setProperty('--tint', c); };
  if (!url) return set('rgba(139,92,246,.10)');
  const img = new Image(); img.crossOrigin = 'anonymous';
  img.onload = () => { try { const c = document.createElement('canvas'); c.width = c.height = 8; const x = c.getContext('2d'); x.drawImage(img, 0, 0, 8, 8);
    const d = x.getImageData(0, 0, 8, 8).data; let r = 0, g = 0, b = 0; for (let i = 0; i < d.length; i += 4) { r += d[i]; g += d[i + 1]; b += d[i + 2]; }
    const k = d.length / 4; set(`rgba(${Math.round(r / k * 1.4)},${Math.round(g / k * 1.4)},${Math.round(b / k * 1.4)},.55)`); } catch (e) { set('rgba(139,92,246,.14)'); } };
  img.onerror = () => set('rgba(139,92,246,.14)');
  img.src = url;
}
/* ---- the progress bar of the player: runs on by itself between the polls; Spotify's can be dragged to seek */
function progNow() {
  const p = S.prog; if (!p) return 0;
  return clamp(p.ms + (p.playing ? Date.now() - p.at : 0), 0, p.dur || 0);
}
function paintProgress() {
  const p = S.prog, ms = p && p.dur ? progNow() : 0;
  $('#pProg').style.width = p && p.dur ? clamp(100 * ms / p.dur, 0, 100) + '%' : '0%';
  $('#pT0').textContent = p && p.dur ? fmt(ms) + ' / ' + fmt(p.dur) : '';
  $('.p-prog .track').classList.toggle('seekable', !!(p && p.seek));
}
setInterval(() => { if (!S.seekDrag) paintProgress(); }, 250);
(() => {
  const bar = document.querySelector('.p-prog .track');
  const at = (e) => { const r = bar.getBoundingClientRect(); return clamp((e.clientX - r.left) / r.width, 0, 1); };
  bar.addEventListener('pointerdown', (e) => {
    if (!S.prog || !S.prog.seek) return;
    S.seekDrag = true; bar.setPointerCapture(e.pointerId);
    const f = at(e); $('#pProg').style.width = 100 * f + '%'; $('#pT0').textContent = fmt(f * S.prog.dur) + ' / ' + fmt(S.prog.dur);
  });
  bar.addEventListener('pointermove', (e) => {
    if (!S.seekDrag) return;
    const f = at(e); $('#pProg').style.width = 100 * f + '%'; $('#pT0').textContent = fmt(f * S.prog.dur) + ' / ' + fmt(S.prog.dur);
  });
  const end = (e) => {
    if (!S.seekDrag) return;
    S.seekDrag = false;
    const ms = Math.round(at(e) * S.prog.dur);
    S.prog = Object.assign({}, S.prog, { ms, at: Date.now() });
    api('seek', { ms });
  };
  bar.addEventListener('pointerup', end);
  bar.addEventListener('pointercancel', () => { S.seekDrag = false; });
})();
/* ---- utilities: the alarms, timers and reminders of the speaker */
const DAYS = ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс'], DAYN = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'];
const same = (a, b) => a.length === b.length && a.every((v, i) => v === b[i]);
const repeatDays = (rep) => rep === 'daily' ? [0, 1, 2, 3, 4, 5, 6] : rep === 'weekdays' ? [0, 1, 2, 3, 4] : rep === 'weekends' ? [5, 6]
  : (rep || '').startsWith('days:') ? rep.slice(5).split(',').map((d) => DAYS.indexOf(d)).filter((i) => i >= 0).sort() : [];
const repeatText = (d) => !d.length ? 'Никогда' : d.length === 7 ? 'Каждый день' : same(d, [0, 1, 2, 3, 4]) ? 'По будням' : same(d, [5, 6]) ? 'По выходным' : d.map((i) => DAYN[i]).join(', ');
const repeatParam = (d) => !d.length ? 'once' : d.length === 7 ? 'daily' : same(d, [0, 1, 2, 3, 4]) ? 'weekdays' : same(d, [5, 6]) ? 'weekends' : 'days:' + d.map((i) => DAYS[i]).join(',');
const nowSrv = () => (Date.now() + (S.skew || 0)) / 1000;
const pad2 = (n) => String(n).padStart(2, '0');
function inText(sec) {         // "через 9 ч 14 мин"
  const m = Math.max(0, Math.round(sec / 60)), d = Math.floor(m / 1440), h = Math.floor((m % 1440) / 60), mm = m % 60;
  return 'через ' + (d ? `${d} д ${h} ч` : h ? `${h} ч ${mm} мин` : `${mm || 1} мин`);
}
function whenText(at) {        // "сегодня в 21:30", "завтра в 07:00", "пт, 24 окт в 07:00"
  const d = new Date(at * 1000), t = new Date(), day = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = Math.round((day(d) - day(t)) / 86400000), hm = `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
  if (diff === 0) return 'сегодня в ' + hm; if (diff === 1) return 'завтра в ' + hm;
  return d.toLocaleDateString('ru-RU', { weekday: 'short', day: 'numeric', month: 'short' }) + ' в ' + hm;
}
const cdText = (sec) => { sec = Math.max(0, Math.ceil(sec)); const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60; return (h ? h + ':' + pad2(m) : m) + ':' + pad2(s); };
function renderUtils(rem) {
  const alarms = rem.filter((r) => r.kind === 'alarm' && !r.snooze).sort((a, b) => a.time.localeCompare(b.time));
  const timers = rem.filter((r) => r.kind === 'timer').sort((a, b) => a.at - b.at);
  const rems = rem.filter((r) => r.kind === 'reminder').sort((a, b) => a.at - b.at);
  S.alarms = alarms;
  const next = alarms.filter((a) => a.enabled).sort((a, b) => a.at - b.at)[0];
  // alarms
  $('#alarmList').innerHTML = alarms.length ? '<div class="inset">' + alarms.map((a) => {
    const d = repeatDays(a.repeat), sub = (a.label || 'Будильник') + (d.length ? ', ' + repeatText(d).replace(/, /g, ' ') : '');
    return `<div class="al${a.enabled ? '' : ' off'}" data-alarm="${esc(a.id)}"><div class="meta"><div class="tm">${esc(a.time)}</div><div class="sub">${esc(sub)}</div></div><button class="switch${a.enabled ? ' on' : ''}" data-altog="${esc(a.id)}" aria-label="Включить"><i></i></button></div>`;
  }).join('') + '</div>' : '<div class="inset"><div class="empty">Будильников нет. Нажми «+» или скажи: «Алиса, разбуди меня в семь»</div></div>';
  $('#alarmNextLine').textContent = next ? 'Следующий ' + inText(next.at - nowSrv()) : alarms.length ? 'Все будильники выключены' : '';
  // timers
  $('#timerList').innerHTML = timers.length ? '<div class="inset">' + timers.map((t) => `<div class="tmr" data-at="${t.at}" data-total="${t.total}"><div class="meta"><div class="cd" data-cd></div><div class="sub">${esc(t.label || 'Таймер')}</div><div class="bar"><i></i></div></div><button class="mini-btn red" data-rdel="${esc(t.id)}">Отменить</button></div>`).join('') + '</div>'
    : '<div class="inset"><div class="empty">Таймеров нет. Нажми «+» или скажи: «Алиса, поставь таймер на десять минут»</div></div>';
  tickTimers();
  // reminders
  $('#remList').innerHTML = rems.length ? '<div class="inset">' + rems.map((r) => {
    const d = repeatDays(r.repeat);
    return `<div class="rem-row"><div class="meta"><b>${esc(r.text || r.label)}</b><span>${whenText(r.at)}${d.length ? ' · ' + esc(repeatText(d).toLowerCase()) : ''}</span></div><button class="mini-btn red" data-rdel="${esc(r.id)}">Отменить</button></div>`;
  }).join('') + '</div>' : '<div class="inset"><div class="empty">Напоминаний нет. Нажми «+» или скажи: «Алиса, напомни через час позвонить маме»</div></div>';
  // the hub and the home card
  $('#uAlarmSub').textContent = next ? `${next.time} · ${inText(next.at - nowSrv())}` : alarms.length ? 'Выключены' : 'Нет';
  $('#uTimerSub').textContent = timers.length ? 'Идёт: ' + timers.length : 'Нет';
  $('#uRemSub').textContent = rems.length ? 'Стоит: ' + rems.length : 'Нет';
  const soon = [...(next ? [next] : []), ...timers, ...rems].sort((a, b) => a.at - b.at)[0];
  $('#alarmNext').textContent = !soon ? 'Ничего не стоит' : soon.kind === 'alarm' ? 'Будильник ' + soon.time : soon.kind === 'timer' ? 'Таймер ' + cdText(soon.at - nowSrv()) : 'Напоминание';
}
function tickTimers() {
  $$('.tmr').forEach((t) => {
    const left = +t.dataset.at - nowSrv(), total = +t.dataset.total || 1;
    $('[data-cd]', t).textContent = cdText(left);
    $('.bar i', t).style.width = clamp(100 * left / total, 0, 100) + '%';
  });
}
function tickAlarmText() {          // "через 9 ч 14 мин" ticks with the clock, not only when the list changes
  const next = (S.alarms || []).filter((a) => a.enabled && a.at > nowSrv() - 60).sort((a, b) => a.at - b.at)[0];
  if (!next) return;
  $('#alarmNextLine').textContent = 'Следующий ' + inText(next.at - nowSrv());
  $('#uAlarmSub').textContent = `${next.time} · ${inText(next.at - nowSrv())}`;
}
setInterval(() => { tickTimers(); tickAlarmText(); }, 1000);
function renderDialogs(s) {
  const d = (s.dialogs || []).slice().reverse();
  $('#homeDialogs').innerHTML = d.length ? d.slice(0, 3).map(dlg).join('') : '<div class="empty">Пока ничего</div>';
  $('#dialogs').innerHTML = d.length ? d.map(dlg).join('') : '<div class="empty">Пока пусто</div>';
}
const dlg = (x) => `<div class="dlg"><small>${esc(x.t)}</small><p>🗣 ${esc(x.heard)}</p><p class="re">🔊 ${esc(x.reply)}</p></div>`;
function renderSelects(sels) {
  const box = $('#selList'); box.innerHTML = '';
  for (const x of sels || []) {
    const row = document.createElement('div'); row.className = 'row action';
    row.innerHTML = `<span class="k">${esc(x.name.replace(/^Яндекс Станция Мини 3 /, ''))}</span><span class="v">${esc(S.opt['sel' + x.id] || x.current || '—')}<svg class="chev" style="display:inline-block;margin-left:8px" viewBox="0 0 8 14"><path d="M1 1l6 6-6 6"/></svg></span>`;
    row.onclick = () => menu(row, x.options.map((o) => ({ t: o, on: o === (S.opt['sel' + x.id] || x.current), v: o })), (o) => { S.opt['sel' + x.id] = o; S.sig.sel = ''; api('select', { id: x.id, option: o }).then(() => setTimeout(() => { delete S.opt['sel' + x.id]; }, 2500)); renderSelects(S.s.selects); });
    box.appendChild(row);
  }
  if (!box.children.length) box.innerHTML = '<div class="empty">Колонка не подключена</div>';
}
function renderVoices(s) {
  const v = s.voices || [];
  $('#voiceList').innerHTML = v.length ? '<div class="inset">' + v.map((p) => `<div class="item"><div class="ico"><svg viewBox="0 0 24 24"><circle cx="12" cy="8" r="4"/><path d="M4.5 20a7.5 7.5 0 0 1 15 0"/></svg></div><div class="t"><b>${esc(p.name)}</b><span>${esc(p.note || 'без заметок')} · ${p.n} образцов</span></div><div class="btns"><button class="mini-btn dots" data-vmenu="${esc(p.name)}" aria-label="Действия"><svg viewBox="0 0 24 24"><circle cx="5" cy="12" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="19" cy="12" r="1.6"/></svg></button></div></div>`).join('') + '</div>'
    : '<div class="inset"><div class="empty">Пока никого. Познакомь её с собой голосом</div></div>';
  const se = s.session || {}, el = $('#session');
  el.querySelector('.dot').className = 'dot' + (se.active ? '' : ' off');
  el.querySelector('span').textContent = se.active ? 'Сейчас разговор с: ' + (se.name || 'незнакомым человеком') : 'Сейчас никто не разговаривает';
}
const lockIc = '<svg class="ic" viewBox="0 0 24 24"><rect x="6" y="10.5" width="12" height="9" rx="2"/><path d="M8.5 10.5V8a3.5 3.5 0 0 1 7 0v2.5"/></svg>';
const infoIc = '<svg class="info" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 11v5.5M12 7.6v.2"/></svg>';
const checkIc = '<svg class="ck" viewBox="0 0 24 24"><path d="M5 12.5l4.5 4.5L19 7"/></svg>';
const sigIc = (dbm) => { const b = +dbm > -60 ? 3 : +dbm > -75 ? 2 : 1; return `<svg class="ic" viewBox="0 0 24 24"><path d="M2.5 9.5a14 14 0 0 1 19 0"${b < 3 ? ' class="dim"' : ''}/><path d="M5.5 13a9.5 9.5 0 0 1 13 0"${b < 2 ? ' class="dim"' : ''}/><path d="M8.7 16.3a5 5 0 0 1 6.6 0"/><circle cx="12" cy="19.5" r="1" fill="#fff"/></svg>`; };
function renderWifi(w) {
  w = w || { cur: {}, nets: [], aps: [] };
  const c = w.cur || {};
  $('#wifiCur').innerHTML = c.ssid ? `<div class="wrow">${checkIc}<span class="nm">${esc(c.ssid)}<small>${esc(c.ip || '?')} · ${esc(c.state || '')}${c.pending ? ' · проверка: ' + esc(c.pending) : ''}${c.last ? '<br>' + esc(c.last) : ''}</small></span>${lockIc}${sigIc(-50)}</div>` : '<div class="empty" style="padding-left:0">Нет данных (обнови lostOS на колонке)</div>';
  const nets = (w.nets || []).filter((n) => !n.flags.includes('CURRENT'));
  $('#wifiNets').style.display = nets.length ? '' : 'none'; $('#wifiNets').previousElementSibling.style.display = nets.length ? '' : 'none';
  $('#wifiNets').innerHTML = nets.map((n) => `<div class="wrow" data-wnet="${esc(n.ssid)}"><span class="nm">${esc(n.ssid)}<small>${esc(n.flags)}</small></span>${infoIc}</div>`).join('');
  const aps = (w.aps || []).filter((a) => a.ssid !== c.ssid);
  $('#wifiAps').innerHTML = aps.map((n) => `<div class="wrow" data-wpick="${esc(n.ssid)}"><span class="nm">${esc(n.ssid)}</span>${n.security === 'open' ? '' : lockIc}${sigIc(n.signal)}${infoIc}</div>`).join('') + '<div class="wrow" data-wother="1"><span class="nm">Другая…</span></div>';
  $('#setWifi').textContent = c.ssid || '—';
}
/* ---- recognition / speech engines: the cloud ones are always there, the local ones can be chosen only after they are installed */
function engOption(kind, value, title, sub, on, extra) {
  return `<div class="row eng${on ? ' sel' : ''}" data-eng="${kind}:${value}"><span class="k">${esc(title)}<small>${sub}</small></span>${extra || (on ? '<svg class="ck2" viewBox="0 0 24 24"><path d="M5 12.5l4.5 4.5L19 7"/></svg>' : '')}</div>`;
}
function renderEngines(e) {
  if (!e) return;
  const inst = (x, name) => x.busy ? `<span class="v">${esc(x.text || '…')}</span>` : x.installed ? null : x.blocked ? '<span class="v">запрещено</span>' : `<button class="mini-btn acc" data-einstall="${name}">Установить</button>`;
  const w = e.whisper, s = e.silero;
  const note = (x, okText, noText) => x.busy ? esc(x.text || 'Устанавливается…') : x.text ? esc(x.text) : x.installed ? okText : noText;
  $('#engStt').innerHTML =
    engOption('stt', 'yandex', 'Облако Яндекса', 'Как у Алисы: быстро, не занимает видеопамять', e.stt === 'yandex') +
    engOption('stt', 'whisper', 'Whisper (локально)', note(w, 'Установлен, работает без интернета', 'Не установлен: нужно скачать модель, около 1,6 ГБ'), e.stt === 'whisper', inst(w, 'whisper')) +
    ((e.plugins || {}).stt || []).map((p) => engOption('stt', 'plugin:' + p.id, p.name, 'Плагин', e.stt === 'plugin:' + p.id)).join('');
  $('#engTts').innerHTML =
    engOption('tts', 'alice', 'Алиса (облако Яндекса)', 'Голос Алисы, нужен интернет', e.tts === 'alice') +
    engOption('tts', 'silero', 'Silero (локально)', note(s, 'Установлен, работает без интернета', 'Не установлен: нужно скачать модель, около 100 МБ'), e.tts === 'silero', inst(s, 'silero')) +
    ((e.plugins || {}).tts || []).map((p) => engOption('tts', 'plugin:' + p.id, p.name, 'Плагин', e.tts === 'plugin:' + p.id)).join('') +
    `<div class="row action" id="rowVoice"><span class="k">Голос в облаке</span><span class="v acc">${esc((e.voices || {})[e.voice] || e.voice)}</span></div>`;
  const engName = (v, list, fallback) => v.startsWith('plugin:') ? ((list || []).find((p) => 'plugin:' + p.id === v) || { name: 'Плагин' }).name : fallback;
  $('#setEngines').textContent = engName(e.stt, (e.plugins || {}).stt, e.stt === 'yandex' ? 'Облако' : 'Whisper') + ' · ' + engName(e.tts, (e.plugins || {}).tts, e.tts === 'alice' ? 'Алиса' : 'Silero');
  const rv = $('#rowVoice'); if (rv) rv.onclick = () => menu(rv, Object.entries(e.voices || {}).map(([k, n]) => ({ t: n, on: k === e.voice, v: k })), (k) => api('engine_set', { kind: 'voice', value: k }));
}
document.addEventListener('click', async (ev) => {
  const ins = ev.target.closest('[data-einstall]');
  if (ins) { ev.stopPropagation(); const r = await api('engine_install', { name: ins.dataset.einstall }); if (r && r.ok !== false) toast('Установка началась'); return; }
  const row = ev.target.closest('[data-eng]');
  if (row) { const i = row.dataset.eng.indexOf(':'), kind = row.dataset.eng.slice(0, i), value = row.dataset.eng.slice(i + 1); await api('engine_set', { kind, value }); }
});
/* ---- the settings of the brain: which model thinks, how it answers, what it knows */
const AI = { d: null };
async function aiApi(body) {
  try {
    const r = await fetch('/api/v1/ai', body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {});
    const j = await r.json();
    if (j && j.ok === false && j.error) toast(j.error);
    return j && j.providers ? j : null;
  } catch (e) { toast('Нет связи с сервером'); return null; }
}
async function aiOpen() { const d = await aiApi(); if (d) { AI.d = d; aiRender(); } }
async function aiSet(body) { const d = await aiApi(body); if (d) { AI.d = d; aiRender(); } }
const STYLE_NAMES = { short: 'Коротко', normal: 'Обычно', detailed: 'Подробно' };
const TEMPS = [[0.2, 'Строго (0.2)'], [0.5, 'Ровно (0.5)'], [0.7, 'Живо (0.7, обычно)'], [1.0, 'Смело (1.0)'], [1.3, 'Очень смело (1.3)']];
const TOKENS = [[300, 'Короткие (300)'], [500, 'Средние (500)'], [700, 'Обычные (700)'], [1000, 'Длинные (1000)'], [1500, 'Очень длинные (1500)']];
function aiRender() {
  const d = AI.d; if (!d) return;
  const cur = d.providers.find((p) => p.id === d.provider);
  const modelNow = cur ? (cur.chosen || cur.default) : '';
  const mInfo = cur ? (cur.list.find((x) => x.id === modelNow) || {}) : {};
  $('#aiModel').innerHTML =
    `<div class="row action" id="aiProv"><span class="k">Сервис</span><span class="v acc">${esc(cur ? cur.title : 'нет ключа')}</span></div>` +
    (cur ? `<div class="row action" id="aiMod"><span class="k">Модель<small>${esc(mInfo.note || '')}</small></span><span class="v acc">${esc(mInfo.title || modelNow)}</span></div>` : '');
  const cu = d.custom || { base: '', model: '' }, cp = d.providers.find((p) => p.id === 'custom');
  $('#aiCustom').innerHTML =
    `<div class="row action" id="aiCuBase"><span class="k">Адрес<small>например, https://example.com/v1</small></span><span class="v acc">${esc(cu.base ? cu.base.replace(/^https?:\/\//, '') : 'не задан')}</span></div>` +
    `<div class="row action" id="aiCuModel"><span class="k">Model ID<small>ID модели из каталога сервиса</small></span><span class="v acc">${esc(cu.model || 'не задан')}</span></div>` +
    `<div class="row action" id="aiCuKey"><span class="k">Ключ</span><span class="v acc">${cp && cp.has_key ? 'сохранён' : 'нажмите и вставьте'}</span></div>` +
    `<div class="row action" id="aiCuTest"><span class="k">Проверить</span><span class="v acc">один запрос</span></div>`;
  $('#aiAnswers').innerHTML =
    `<div class="row action" id="aiStyle"><span class="k">Длина ответа</span><span class="v acc">${esc(STYLE_NAMES[d.style] || 'Обычно')}</span></div>` +
    `<div class="row action" id="aiTemp"><span class="k">Живость</span><span class="v acc">${esc((TEMPS.find((t) => t[0] === d.temperature) || [0, String(d.temperature)])[1])}</span></div>` +
    `<div class="row action" id="aiTok"><span class="k">Предел ответа</span><span class="v acc">${esc((TOKENS.find((t) => t[0] === d.max_tokens) || [0, String(d.max_tokens)])[1])}</span></div>`;
  const pa = $('#aiPersona'); if (document.activeElement !== pa) pa.value = d.persona || d.persona_default || '';
  $('#aiCtx').innerHTML = d.contexts.map((c) => `<div class="row"><span class="k">${esc(c.title)}<small>${esc(c.about)}${c.available ? '' : ' (не подключён)'}</small></span><button class="switch${c.on ? ' on' : ''}" data-aictx="${c.id}" aria-label="${esc(c.title)}"><i></i></button></div>`).join('');
  $$('[data-aictx]').forEach((el) => bindSwitch(el, (on) => aiSet({ context: { id: el.dataset.aictx, on } })));
  $('#aiPreview').textContent = d.preview || '—';
  $('#lAiSub').textContent = (cur ? cur.title : 'нет ключа') + (modelNow ? ' · ' + (mInfo.title || modelNow) : '');
  const mk = (id, items, pick) => { const el = $(id); if (el) el.onclick = () => menu(el, items, pick); };
  mk('#aiProv', d.providers.map((p) => ({ t: p.title + (p.has_key ? '' : ' (нет ключа)'), on: p.id === d.provider, v: p.id })), (v) => aiSet({ provider: v }));
  if (cur) mk('#aiMod', [...cur.list.map((x) => ({ t: x.title, on: x.id === modelNow, v: x.id })), { t: 'Другая…', v: '*' }, { t: 'По умолчанию', v: '' }],
    (v) => { if (v === '*') { const n = prompt('Название модели у ' + cur.title, cur.chosen || ''); if (n !== null) aiSet({ model: { provider: cur.id, id: n } }); } else aiSet({ model: { provider: cur.id, id: v } }); });
  const ask = (id, fn) => { const el = $(id); if (el) el.onclick = fn; };
  ask('#aiCuBase', () => { const v = prompt('Адрес сервиса (оканчивается на /v1)', cu.base); if (v !== null) aiSet({ custom: { base: v, model: cu.model } }); });
  ask('#aiCuModel', () => { const v = prompt('Model ID (ID модели из каталога сервиса)', cu.model); if (v !== null) aiSet({ custom: { base: cu.base, model: v } }); });
  ask('#aiCuKey', async () => {
    const v = prompt('Ключ сервиса (он хранится зашифрованно, обратно не показывается)', ''); if (!v) return;
    const r = await fetch('/api/v1/secrets', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: 'CUSTOM_API_KEY', value: v.trim() }) }).then((x) => x.json()).catch(() => ({}));
    if (!r.ok) toast(r.error || 'Не получилось сохранить'); else { toast('Ключ сохранён'); aiOpen(); }
  });
  ask('#aiCuTest', async () => {
    $('#aiCustomMsg').textContent = 'Проверяю…';
    const r = await fetch('/api/v1/setup/test', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ provider: 'custom' }) }).then((x) => x.json()).catch(() => ({}));
    $('#aiCustomMsg').textContent = r.ok ? 'Сервис ответил: всё работает. Выберите «Свой сервис» в строке «Сервис» выше' : 'Не получилось: ' + (r.error || 'нет ответа');
  });
  mk('#aiStyle', Object.entries(STYLE_NAMES).map(([k, t]) => ({ t, on: d.style === k, v: k })), (v) => aiSet({ style: v }));
  mk('#aiTemp', TEMPS.map(([v, t]) => ({ t, on: d.temperature === v, v })), (v) => aiSet({ temperature: v }));
  mk('#aiTok', TOKENS.map(([v, t]) => ({ t, on: d.max_tokens === v, v })), (v) => aiSet({ max_tokens: v }));
}
$('#aiPersonaSave').addEventListener('click', async () => { const t = $('#aiPersona').value.trim(); await aiSet({ persona: t === (AI.d && AI.d.persona_default) ? '' : t }); toast('Характер сохранён'); });
$('#aiPersonaReset').addEventListener('click', async () => { await aiSet({ persona: '' }); $('#aiPersona').value = AI.d.persona_default || ''; toast('Стандартный характер'); });
function wifiOpened() { api('wifi_scan'); const sp = $('#wifiSpin'); sp.hidden = false; setTimeout(() => { sp.hidden = true; }, 7000); }
function renderSystem(s) {
  const L = s.last || {}, c = (s.wifi && s.wifi.cur) || {};
  $('#lastBox').innerHTML = L.text ? `<div class="dlg"><small>${esc(L.t)} · ${esc(L.who || 'голос не определён')}${L.score ? ' (' + L.score + ')' : ''}</small><p>🎙 ${esc(L.text)}</p>${L.pre ? `<p class="re">до «Алиса»: ${esc(L.pre)}</p>` : ''}<p class="re">итог: ${esc(L.outcome)}</p></div>` : '<div class="empty">Пока ничего</div>';
  $('#sheetInfo').innerHTML = `<div class="row"><span class="k">Колонка</span><span class="v">${s.connected ? 'В сети' : 'Не в сети'}</span></div><div class="row"><span class="k">Громкость</span><span class="v">${s.volume != null ? Math.round(s.volume * 100) + '%' : '—'}</span></div>`;
  $('#setStatus').textContent = ((S.speaker && S.speaker.model) || 'Колонка') + ' · ' + (s.connected ? 'В сети' : 'Не в сети');
  $('#setVer').textContent = S.speaker && S.speaker.lostos ? 'lostOS ' + S.speaker.lostos.replace(/^v/, '').split('-')[0] + (S.speaker.firmware ? ' · прошивка ' + S.speaker.firmware.split('.').slice(0, 4).join('.') : '') : '';
  // what the speaker itself tells (its own inset under its name) and what belongs to the server (its own inset)
  const live = [['Состояние', s.connected ? 'В сети' : 'Не в сети'], ['Сеть Wi-Fi', c.ssid || '—'], ['IP-адрес', c.ip || '—'], ['Громкость', s.volume != null ? Math.round(s.volume * 100) + '%' : '—']];
  $('#spLive').innerHTML = live.map(([k, v]) => `<div class="row"><span class="k">${esc(k)}</span><span class="v">${esc(v)}</span></div>`).join('');
  const rows = [['Адрес сервера', location.host], ['Версия сервера', (s.app && s.app.version) || '—'], ...(s.app && s.app.newer ? [['Доступна версия', s.app.latest]] : s.app && s.app.repo ? [['Обновления', 'установлена последняя']] : []), ['Голосов в памяти', String((s.voices || []).length)]];
  $('#aboutList').innerHTML = rows.map(([k, v]) => `<div class="row"><span class="k">${esc(k)}</span><span class="v">${esc(v)}</span></div>`).join('');
  $('#aboutNotes').textContent = s.app && s.app.newer ? 'Что нового в ' + s.app.latest + ': ' + (s.app.notes || '') : '';
  $('#lVoiceSub').textContent = (s.voices || []).length ? 'Знакомых: ' + s.voices.length : 'Пока никого';
  const nd = (s.dialogs || []).length; $('#lDlgSub').textContent = nd ? 'Последних: ' + nd : 'Пока пусто';
}
/* ---------------------------------------------------------------- switches and sliders */
const swEls = { bluetooth: [] };
$$('[data-sw]').forEach((el) => swEls[el.dataset.sw].push(el));
function setSwitchEl(el, on) { if (on === undefined) return; el.classList.toggle('on', !!on); }
function setSwitch(name, on) { swEls[name].forEach((el) => setSwitchEl(el, on)); }
function bindSwitch(el, onToggle) {
  el.addEventListener('pointerdown', (e) => { e.stopPropagation(); el.classList.add('pressed'); });
  const up = () => el.classList.remove('pressed');
  el.addEventListener('pointerup', up); el.addEventListener('pointercancel', up); el.addEventListener('pointerleave', up);
  el.addEventListener('click', (e) => { e.stopPropagation(); const on = !el.classList.contains('on'); el.classList.toggle('on', on); onToggle(on); });
}
const hold = (key, ms = 3500) => { S.opt[key] = undefined; };
for (const name of ['bluetooth']) swEls[name].forEach((el) => bindSwitch(el, (on) => {
  S.opt[name] = on; setSwitch(name, on); setTimeout(() => { delete S.opt[name]; }, 4500);
  api(name, { on });
}));

bindSwitch($('#swVolClick'), (on) => { S.opt.volclick = on; setTimeout(() => { delete S.opt.volclick; }, 3000); api('switch', { id: 'volume_sound', on }); });
bindSwitch($('#swBarge'), (on) => { S.opt.barge = on; setTimeout(() => { delete S.opt.barge; }, 3000); api('barge', { on }); });

function bindSlider(el, { onInput, onCommit }) {
  let drag = false, last = 0;
  const ratio = (e) => { const r = el.getBoundingClientRect(); return clamp((e.clientX - r.left) / r.width, 0, 1); };
  const paint = (v) => { el.querySelector('.track i').style.width = (v * 100) + '%'; };
  el.addEventListener('pointerdown', (e) => { e.stopPropagation(); drag = true; el.setPointerCapture(e.pointerId); el.classList.add('drag'); S.volBusy = Date.now() + 2500; const v = ratio(e); paint(v); onInput(v); });
  el.addEventListener('pointermove', (e) => { if (!drag) return; const v = ratio(e); paint(v); onInput(v); S.volBusy = Date.now() + 2500; const t = Date.now(); if (t - last > 280) { last = t; onCommit(v); } });
  const end = (e) => { if (!drag) return; drag = false; el.classList.remove('drag'); const v = ratio(e); paint(v); onCommit(v); S.volBusy = Date.now() + 1800; };
  el.addEventListener('pointerup', end); el.addEventListener('pointercancel', () => { drag = false; el.classList.remove('drag'); });
  el.paint = paint;
}
const vols = [$('#volSlider'), $('#pVol')];
function setVol(v) { vols.forEach((el) => el.paint && el.paint(v)); $('#volVal').textContent = Math.round(v * 100) + '%'; }
vols.forEach((el) => bindSlider(el, { onInput: (v) => { vols.forEach((o) => o !== el && o.paint(v)); $('#volVal').textContent = Math.round(v * 100) + '%'; }, onCommit: (v) => api('volume', { level: Math.round(v * 100) }) }));

/* ---------------------------------------------------------------- tab bar and its lens */
const bar = $('#tabbar'), lens = $('.lens', bar), tabEls = $$('.tab', bar), TABS = tabEls.map((t) => t.dataset.tab);
const L = { x: 0, w: 60, v: 0, pressed: false, target: 0, ax: null, aw: null, loop: false, last: 0, moved: false, down: 0 };
const tabGeom = (i) => { const bb = bar.getBoundingClientRect(), tb = tabEls[i].getBoundingClientRect(); return { x: tb.left - bb.left - 1, w: tb.width + 2 }; };
function paintLens() {
  const st = Math.min(0.34, Math.abs(L.v) / 2400);
  const sx = 1 + st + (L.pressed ? 0.12 : 0), sy = 1 - st * 0.3 + (L.pressed ? 0.08 : 0);
  lens.style.width = L.w + 'px';
  lens.style.transform = `translate3d(${L.x}px,0,0) scale(${sx.toFixed(3)},${sy.toFixed(3)})`;
  const cx = L.x + L.w / 2;
  tabEls.forEach((t, i) => {
    let m = 1;
    if (L.pressed) { const g = tabGeom(i); m = 1 + 0.2 * Math.max(0, 1 - Math.abs(g.x + g.w / 2 - cx) / (g.w * 0.95)); }
    t.style.setProperty('--mag', m.toFixed(3));
  });
}
function lensTo(i, instant) {
  const g = tabGeom(i);
  L.ax && L.ax.cancel(); L.aw && L.aw.cancel();
  if (instant) { L.x = g.x; L.w = g.w; L.v = 0; paintLens(); return; }
  L.ax = spring({ from: L.x, to: g.x, v: L.v, k: 360, c: 22, update: (x, v) => { L.x = x; L.v = v; paintLens(); } });
  L.aw = spring({ from: L.w, to: g.w, k: 300, c: 26, update: (w) => { L.w = w; } });
}
function nearestTab(cx) { let b = 0, bd = 1e9; tabEls.forEach((t, i) => { const g = tabGeom(i); const d = Math.abs(g.x + g.w / 2 - cx); if (d < bd) { bd = d; b = i; } }); return b; }
function follow() {
  if (!L.pressed) { L.loop = false; return; }
  const now = performance.now(), dt = Math.max(1, now - L.last) / 1000; L.last = now;
  const nx = L.x + (L.target - L.x) * Math.min(1, dt * 22);
  L.v = (nx - L.x) / dt; L.x = nx; paintLens();
  tabEls.forEach((t, i) => t.classList.toggle('active', i === nearestTab(L.x + L.w / 2)));
  raf(follow);
}
bar.addEventListener('pointerdown', (e) => {
  if (e.button > 0 || isDesktop()) return;
  bar.setPointerCapture(e.pointerId); L.pressed = true; L.moved = false; L.down = e.clientX;
  L.ax && L.ax.cancel(); L.aw && L.aw.cancel();
  const g = tabGeom(TABS.indexOf(S.tab)); L.w = g.w;
  bar.classList.add('pressing'); lensGlassOn(true);
  setTarget(e); L.last = performance.now(); if (!L.loop) { L.loop = true; raf(follow); }
});
function setTarget(e) { const bb = bar.getBoundingClientRect(); L.target = clamp(e.clientX - bb.left - L.w / 2, 3, bb.width - L.w - 3); }
bar.addEventListener('pointermove', (e) => { if (!L.pressed) return; if (Math.abs(e.clientX - L.down) > 6) L.moved = true; setTarget(e); });
const lensEnd = (e) => {
  if (!L.pressed) return;
  L.pressed = false; bar.classList.remove('pressing'); lensGlassOn(false);
  if (e && e.clientX != null) setTarget(e);
  go(TABS[nearestTab(L.target + L.w / 2)]);      // where the finger is, not where the lens has got to (a quick tap)
};
bar.addEventListener('pointerup', lensEnd);
bar.addEventListener('pointercancel', () => { L.pressed = false; bar.classList.remove('pressing'); lensGlassOn(false); lensTo(TABS.indexOf(S.tab)); });
tabEls.forEach((t) => t.addEventListener('click', () => { if (isDesktop()) go(t.dataset.tab); }));
function lensGlassOn() { /* hook for the Chromium refraction layer */ }

/* ---------------------------------------------------------------- pages */
const pages = Object.fromEntries($$('.page:not(.sub)').map((p) => [p.id.slice(2), p]));
const subs = Object.fromEntries($$('.page.sub').map((p) => [p.id.slice(2), p]));
const allPages = () => [...Object.values(pages), ...Object.values(subs)];
S.stack = {};                      // every tab keeps its own navigation stack: the name of the sub-page that is open in it
const pageOf = (tab) => (S.stack[tab] ? subs[S.stack[tab]] : pages[tab]);
const ADDABLE = ['alarms', 'timers', 'reminders', 'scenarios'];     // the sub-pages with a "+" in the bar
let transitioning = null;
function navFor(tab) {          // the title and the buttons of the bar for what is on the screen
  const p = pageOf(tab), inSub = !!S.stack[tab];
  $('#navTitle').textContent = S.plugOpen && S.stack[tab] === 'plugins' ? S.plugOpen : S.accView && S.stack[tab] === 'account' ? ACC_VIEWS[S.accView] : p.dataset.title;
  $('#btnAdd').hidden = !(inSub && ADDABLE.includes(S.stack[tab])); $('#btnBack').hidden = !inSub;
  const phoneSub = inSub && !isDesktop();          // on the phone a sub-page keeps the bar for its title: no bell, and the avatar (the way to the account) only where there is no "+"
  $('#btnBell').hidden = phoneSub;
  $('#btnAvatar').hidden = phoneSub && (!$('#btnAdd').hidden || S.stack[tab] === 'account');
}
function go(name, instant) {
  if (S.locked && name !== 'settings') name = 'settings';              // no other page until the speaker is connected
  const prev = S.tab;
  if (S.editHome) setEditHome(false);                                         // leaving the page ends the edit mode
  if (name === prev && S.stack[name] && !instant) { popSub(); return; }       // the tab again = back to its root
  tabEls.forEach((t) => t.classList.toggle('active', t.dataset.tab === name));
  lensTo(TABS.indexOf(name), instant);
  try { localStorage.setItem('tab', name); } catch (e) {}
  if (name === prev && !instant) { const sc = $('.scroll', pageOf(name)); sc.scrollTo({ top: 0, behavior: 'smooth' }); return; }
  S.tab = name;
  const to = pageOf(name), from = pageOf(prev);
  navFor(name);
  if (instant || !from || from === to) { allPages().forEach((p) => p.classList.remove('active', 'leaving')); to.classList.add('active'); syncNav(); return; }
  const dir = TABS.indexOf(name) > TABS.indexOf(prev) ? 1 : -1;
  transitioning && transitioning.forEach((a) => a.cancel());
  allPages().forEach((p) => { if (p !== to && p !== from) p.classList.remove('active', 'leaving'); });
  to.classList.add('active'); from.classList.add('leaving');
  const opts = { duration: 520, easing: 'cubic-bezier(.32,.72,0,1)', fill: 'both' };
  const a1 = to.animate([{ transform: `translateX(${dir * 24}%)`, opacity: 0 }, { transform: 'none', opacity: 1 }], opts);
  const a2 = from.animate([{ transform: 'none', opacity: 1, filter: 'brightness(1)' }, { transform: `translateX(${-dir * 14}%)`, opacity: 0, filter: 'brightness(.55)' }], opts);
  transitioning = [a1, a2];
  a2.onfinish = () => { from.classList.remove('active', 'leaving'); a1.cancel(); a2.cancel(); transitioning = null; };
  syncNav();
}
function syncNav() { const sc = $('.scroll', pageOf(S.tab)); $('#nav').classList.toggle('scrolled', sc.scrollTop > 36); }
$$('.scroll').forEach((sc) => sc.addEventListener('scroll', () => { if (sc.closest('.page') === pageOf(S.tab)) syncNav(); }, { passive: true }));

/* the navigation stack: a sub-page slides over its hub (it carries the finger when swiped from the left edge) */
const SL = { p: 0, anim: null, tab: '', name: '', id: 0, busy: false, to: 1 };       // id: only the newest animation finishes the job (a cancelled one must not)
function cancelSlide() { SL.id++; SL.busy = false; SL.anim && SL.anim.cancel(); }
function paintSlide(p) {
  const sub = subs[SL.name], hub = pages[SL.tab]; if (!sub) return;
  SL.p = p;
  sub.style.transform = `translateX(${((1 - p) * 100).toFixed(2)}%)`;
  sub.style.boxShadow = `-18px 0 40px rgba(0,0,0,${(0.55 * p).toFixed(2)})`;
  hub.style.transform = `translateX(${(-p * 26).toFixed(2)}%)`;
  hub.style.filter = `brightness(${(1 - 0.5 * p).toFixed(3)})`;
}
function settleSlide(to, v = 0) {
  cancelSlide();
  const sub = subs[SL.name], hub = pages[SL.tab], tab = SL.tab, my = SL.id;
  SL.busy = true; SL.to = to;
  SL.anim = spring({ from: SL.p, to, v, k: 300, c: 34, update: (p) => paintSlide(p) });
  return SL.anim.promise.then(() => {
    if (my !== SL.id) return;                                  // another animation took over: it cleans up
    SL.busy = false;
    [sub, hub].forEach((el) => { el.style.transform = ''; el.style.filter = ''; el.style.boxShadow = ''; });
    if (to === 1) hub.classList.remove('active', 'leaving'); else { sub.classList.remove('active'); S.stack[tab] = null; navFor(S.tab); }
    syncNav();
  });
}
function pushSub(name) {
  const tab = subs[name].dataset.tab;
  if (S.tab !== tab) { S.stack[tab] = name; go(tab); return; }      // from another tab: the tab opens with the sub-page already on top
  if (S.stack[tab]) return;
  S.stack[tab] = name; SL.tab = tab; SL.name = name; navFor(tab);
  subs[name].classList.add('active'); pages[tab].classList.add('active');
  paintSlide(0); $('.scroll', subs[name]).scrollTop = 0;
  settleSlide(1);
  if (name === 'wifi') wifiOpened();
  if (name === 'ai') aiOpen();
}
function navRoot(tab) { $('#navTitle').textContent = pages[tab].dataset.title; $('#btnBack').hidden = true; $('#btnAdd').hidden = true; $('#btnBell').hidden = false; $('#btnAvatar').hidden = tab === 'settings' && !isDesktop(); }     // on the phone the account card of Settings replaces the small avatar
function popSub() {
  const tab = S.tab, name = S.stack[tab]; if (!name) return;
  if (name === 'account' && S.accView) { closeAccView(); return; }
  if (name === 'plugins' && S.plugOpen) { closePlugin(); return; }        // the page of a plugin is a level inside the plugin list
  if (SL.busy && SL.to === 0 && SL.name === name) return;                 // already on its way back: a second tap on the dock must not restart it
  const moving = SL.busy && SL.name === name;                             // an opening (or a swipe) in flight: go back from where it is, not from the open page
  SL.tab = tab; SL.name = name;
  navRoot(tab); pages[tab].classList.add('active'); subs[name].classList.add('active');   // the hub may be hidden (the sub-page was opened from another tab)
  if (!moving) paintSlide(1);
  settleSlide(0, -0.4);
}
$$('[data-sub]').forEach((b) => b.addEventListener('click', () => pushSub(b.dataset.sub)));
$('#btnBack').addEventListener('click', popSub);
(() => {      // swipe back from the left edge of the screen
  const root = $('#app'); let e0 = null;
  root.addEventListener('pointerdown', (e) => {
    if (isDesktop() || !S.stack[S.tab] || $('#sheetLayer.open') || e.clientX - root.getBoundingClientRect().left > 24 || (e.pointerType === 'mouse' && e.button !== 0)) return;
    e0 = { x: e.clientX, id: e.pointerId, lx: e.clientX, lt: performance.now(), v: 0, on: false, tab: S.tab, name: S.stack[S.tab] };
  });
  root.addEventListener('pointermove', (e) => {
    if (!e0 || e.pointerId !== e0.id) return;
    const dx = e.clientX - e0.x, w = root.clientWidth;
    if (!e0.on && dx > 8) { e0.on = true; SL.tab = e0.tab; SL.name = e0.name; cancelSlide(); pages[e0.tab].classList.add('active'); try { root.setPointerCapture(e.pointerId); } catch (_) {} }
    if (!e0.on) return;
    const now = performance.now(); e0.v = 0.7 * e0.v + 0.3 * ((e.clientX - e0.lx) / Math.max(1, now - e0.lt)) * 1000; e0.lx = e.clientX; e0.lt = now;
    paintSlide(1 - clamp(dx / w, 0, 1));
  });
  const end = () => {
    if (!e0) return; const d = e0; e0 = null; if (!d.on) return;
    const back = SL.p < 0.62 || d.v > 500;
    if (back && d.name === 'account' && S.accView) { settleSlide(1, 0); closeAccView(); return; }
    if (back && d.name === 'plugins' && S.plugOpen) { settleSlide(1, 0); closePlugin(); return; }
    if (back) { navRoot(d.tab); settleSlide(0, -d.v / root.clientWidth); }
    else settleSlide(1, 0);
  };
  root.addEventListener('pointerup', end); root.addEventListener('pointercancel', end);
})();

/* ---------------------------------------------------------------- shelves: drag with a mouse / pen (a finger scrolls natively); the content eases after the pointer, then flies on with inertia and snaps to a card */
$$('.shelf').forEach((sh) => {
  let d = null, anim = null, swallow = false;
  const maxX = () => sh.scrollWidth - sh.clientWidth;
  const snaps = () => { const r0 = sh.getBoundingClientRect().left; return $$('.card', sh).map((c) => clamp(c.getBoundingClientRect().left - r0 + sh.scrollLeft - 20, 0, maxX())); };
  const frame = (t) => {                                   // the drag follows the pointer through a low-pass filter: smooth even when the mouse reports rarely
    if (!d || !d.loop) return;
    const dt = clamp((t - d.t) / 1000, 0.001, 0.05); d.t = t;
    d.pos += (d.tgt - d.pos) * (1 - Math.exp(-dt * 24)); sh.scrollLeft = d.pos;
    raf(frame);
  };
  sh.addEventListener('pointerdown', (e) => {
    if (e.pointerType === 'touch' || e.button !== 0) return;
    if (S.editHome && e.target.closest('[data-tile]')) return;          // in the edit mode a held tile moves, the shelf does not
    anim && anim.cancel(); sh.style.scrollSnapType = 'none';
    d = { x: e.clientX, s: sh.scrollLeft, pos: sh.scrollLeft, tgt: sh.scrollLeft, moved: false, lx: e.clientX, lt: performance.now(), v: 0, id: e.pointerId, loop: false, t: 0 };
  });
  sh.addEventListener('pointermove', (e) => {
    if (!d || e.pointerId !== d.id) return;
    const dx = e.clientX - d.x;
    if (!d.moved && Math.abs(dx) > 4) { d.moved = true; try { sh.setPointerCapture(e.pointerId); } catch (_) {} sh.classList.add('grabbing'); d.loop = true; d.t = performance.now(); raf(frame); }
    if (!d.moved) return;
    const now = performance.now(); d.v = 0.75 * d.v + 0.25 * ((d.lx - e.clientX) / Math.max(1, now - d.lt)) * 1000; d.lx = e.clientX; d.lt = now;
    d.tgt = clamp(d.s - dx, 0, maxX());
  });
  const end = () => {
    if (!d) return; const dd = d; d = null; dd.loop = false; sh.classList.remove('grabbing');
    if (!dd.moved) { sh.style.scrollSnapType = ''; return; }
    swallow = true; setTimeout(() => { swallow = false; }, 0);
    const v = performance.now() - dd.lt > 90 ? 0 : dd.v;     // a pointer that rested before release does not fling
    const pts = snaps(), aim = dd.tgt + v * 0.28;
    const to = pts.reduce((b, p) => (Math.abs(p - aim) < Math.abs(b - aim) ? p : b), pts[0] || 0);
    anim = spring({ from: dd.pos, to, v, k: 190, c: 27, update: (x) => { sh.scrollLeft = x; } });
    anim.promise.then(() => { sh.style.scrollSnapType = ''; });
  };
  sh.addEventListener('pointerup', end); sh.addEventListener('pointercancel', end);
  sh.addEventListener('click', (e) => { if (swallow) { e.stopPropagation(); e.preventDefault(); } }, true);
  sh.addEventListener('wheel', (e) => { if (e.shiftKey && !e.deltaX) { e.preventDefault(); sh.scrollBy({ left: e.deltaY, behavior: 'smooth' }); } }, { passive: false });
});

/* ---------------------------------------------------------------- sheets */
const layer = $('#sheetLayer'), appEl = $('#app');
let openSheetEl = null, sheetAnim = null;
function setProgress(p) {
  if (isDesktop()) { layer.style.setProperty('--scrim', clamp(p, 0, 1)); return; }
  appEl.style.transform = '';      // the page behind a sheet is not pushed away any more: it only gets darker (the scrim)
  appEl.style.filter = '';
  layer.style.setProperty('--scrim', clamp(p, 0, 1));
}
function sheetY(el, y) { el.style.transform = `translate(-50%, ${y}px)`; setProgress(clamp(1 - y / (el.offsetHeight + 40), 0, 1)); }
function openSheet(el, fromMini) {
  if (openSheetEl) return;
  openSheetEl = el; el.classList.add('show'); layer.classList.add('open'); document.body.classList.add('sheeted');
  const H = el.offsetHeight + 40; sheetY(el, H);
  if (el.id === 'sheetPlayer') { const c = $('#pCover'); c.classList.add('small'); setTimeout(() => c.classList.remove('small'), 40); }
  sheetAnim && sheetAnim.cancel();
  sheetAnim = spring({ from: H, to: 0, k: 330, c: 36, update: (y) => sheetY(el, y) });
}
function closeSheet(v = 0) {
  const el = openSheetEl; if (!el) return Promise.resolve();
  openSheetEl = null; sheetAnim && sheetAnim.cancel();
  const H = el.offsetHeight + 40, y0 = parseFloat((/translate\(-50%, ([-\d.]+)px/.exec(el.style.transform) || [0, 0])[1]);
  sheetAnim = spring({ from: y0, to: H, v, k: 280, c: 34, update: (y) => sheetY(el, y) });
  return sheetAnim.promise.then(() => { el.classList.remove('show'); layer.classList.remove('open'); document.body.classList.remove('sheeted'); setProgress(0); });
}
$$('[data-close]').forEach((b) => b.addEventListener('click', () => closeSheet()));
$('#scrim').addEventListener('click', () => closeSheet());
// drag a sheet down to dismiss (from its top area; the content scrolls normally)
$$('.sheet').forEach((el) => {
  let st = null;
  el.addEventListener('pointerdown', (e) => {
    if (e.target.closest('input,select,button,.slider,.seg,.wheel,.days,.bri,.swatches,a')) { if (!e.target.closest('.close')) return; }
    const body = el.querySelector('.sheet-body'), top = e.clientY - el.getBoundingClientRect().top;
    st = { y: e.clientY, t: performance.now(), id: e.pointerId, dragging: false, fromTop: top < 84, scroll: body ? body.scrollTop : 0, vy: 0, ly: e.clientY, lt: performance.now() };
  });
  el.addEventListener('pointermove', (e) => {
    if (!st || e.pointerId !== st.id) return;
    const dy = e.clientY - st.y, body = el.querySelector('.sheet-body');
    if (!st.dragging && dy > 8 && (st.fromTop || (body && body.scrollTop <= 0 && st.scroll <= 0) || el.id === 'sheetPlayer')) { st.dragging = true; el.setPointerCapture(e.pointerId); sheetAnim && sheetAnim.cancel(); }
    if (!st.dragging) return;
    const now = performance.now(); st.vy = (e.clientY - st.ly) / Math.max(1, now - st.lt); st.ly = e.clientY; st.lt = now;
    sheetY(el, dy > 0 ? dy : dy * 0.25);
  });
  const end = (e) => {
    if (!st || !st.dragging) { st = null; return; }
    const dy = e.clientY - st.y, vy = st.vy; st = null;
    if (dy > el.offsetHeight * 0.22 || vy > 0.6) closeSheet(vy * 1000);
    else { sheetAnim && sheetAnim.cancel(); sheetAnim = spring({ from: dy, to: 0, v: vy * 1000, k: 380, c: 30, update: (y) => sheetY(el, y) }); }
  };
  el.addEventListener('pointerup', end); el.addEventListener('pointercancel', end);
});
// On the PC the account card of the sidebar opens the account page of a server with accounts, on a home server the Settings; on the phone the small sheet with the app's own settings
$('#btnAvatar').addEventListener('click', () => {
  if (!S.account) { openSheet($('#sheetSettings')); return; }
  const open = S.stack[S.tab];
  if (open && open !== 'account') {          // from another sub-page (plugins, Wi-Fi...): it closes at once, the account takes its place
    cancelSlide(); S.plugOpen = ''; S.accView = '';
    Object.values(subs).forEach((el) => { el.classList.remove('active'); el.style.transform = ''; el.style.boxShadow = ''; });
    Object.values(pages).forEach((el) => { el.style.transform = ''; el.style.filter = ''; });
    S.stack[S.tab] = null;
  }
  pushSub('account'); loadAccount();
});
$('#mini').addEventListener('click', (e) => { if (e.target.closest('.mbtn')) return; openSheet($('#sheetPlayer'), true); });
$('#cardNow').addEventListener('click', () => { if (S.s && S.s.now && S.s.now.source) openSheet($('#sheetPlayer')); });
$('#btnAdd').addEventListener('click', () => { const s = S.stack[S.tab]; if (s === 'alarms') openAlarm(null); else if (s === 'timers') openTimer(); else if (s === 'reminders') openReminder(); else if (s === 'scenarios') openScenario(null); });
$('#cardAlarm').addEventListener('click', () => pushSub('alarms'));

/* ---------------------------------------------------------------- transport buttons */
const flip = (on) => { const n = S.s && S.s.now; if (n) { n.playing = on; n.paused = !on; render(); } };
const toggle = () => { const n = S.s && S.s.now; flip(!(n && n.playing)); api('toggle'); };
$('#miniPlay').addEventListener('click', (e) => { e.stopPropagation(); toggle(); });
$('#pPlay').addEventListener('click', toggle);
const skip = (a) => () => { api(a); };
$('#miniNext').addEventListener('click', (e) => { e.stopPropagation(); skip('next')(); });
$('#pNext').addEventListener('click', skip('next')); $('#pPrev').addEventListener('click', skip('previous'));
$('#pLike').addEventListener('click', async () => {
  if (S.liked && !(await confirmAsk('Убрать из «Любимых»?', 'Трек «' + ((S.s && S.s.now && S.s.now.title) || 'этот') + '» удалится из лайков в Spotify.', 'Убрать', 'danger'))) return;
  S.liked = !S.liked; S.likeHold = Date.now() + 8000; render(); api(S.liked ? 'like' : 'unlike'); toast(S.liked ? 'Добавлено в «Любимые»' : 'Убрано из «Любимых»'); });
$('#rowReload').addEventListener('click', () => { load(); toast('Обновлено'); closeSheet(); });

/* ---------------------------------------------------------------- alerts, menus, toast */
const alertLayer = $('#alertLayer'), alertBox = $('#alertBox');
function ask({ title, text, fields = [], actions }) {
  return new Promise((res) => {
    alertBox.innerHTML = `<h3>${esc(title)}</h3>${text ? `<p>${esc(text)}</p>` : ''}` +
      fields.map((f, i) => `<input data-f="${i}" type="${f.type || 'text'}" placeholder="${esc(f.placeholder || '')}" value="${esc(f.value || '')}" autocomplete="off">`).join('') +
      `<div class="acts ${actions.length === 2 ? 'two' : ''}">${actions.map((a, i) => `<button data-a="${i}" class="${a.role || ''}">${esc(a.t)}</button>`).join('')}</div>`;
    const done = (a) => { alertLayer.classList.remove('open'); res({ a, v: $$('input', alertBox).map((x) => x.value) }); };
    $$('button', alertBox).forEach((b) => b.addEventListener('click', () => done(actions[+b.dataset.a])));
    alertLayer.classList.add('open');
    const f = $('input', alertBox); if (f) setTimeout(() => f.focus(), 250);
  });
}
const confirmAsk = async (title, text, ok = 'Да', role = 'pri') => (await ask({ title, text, actions: [{ t: 'Отмена' }, { t: ok, role }] })).a.t === ok;
const promptAsk = async (title, text, value = '') => { const r = await ask({ title, text, fields: [{ value }], actions: [{ t: 'Отмена' }, { t: 'Готово', role: 'pri' }] }); return r.a.t === 'Готово' ? r.v[0] : null; };
const menuLayer = $('#menuLayer');
function menu(anchor, items, pick) {
  const m = document.createElement('div'); m.className = 'menu glass';
  m.innerHTML = items.map((it, i) => `<div class="mi" data-i="${i}"><span>${esc(it.t)}</span>${it.on ? '<svg viewBox="0 0 24 24"><path d="M5 12.5l4.5 4.5L19 7"/></svg>' : ''}</div>`).join('');
  menuLayer.innerHTML = ''; menuLayer.appendChild(m); menuLayer.classList.add('open');
  const r = anchor.getBoundingClientRect(), mw = Math.min(280, innerWidth * 0.86);
  m.style.width = mw + 'px'; m.style.right = Math.max(10, innerWidth - r.right - 4) + 'px'; m.style.top = Math.min(r.bottom + 6, innerHeight - 40 - items.length * 46) + 'px';
  if (anchor.closest && anchor.closest('.side-foot')) {          // the block at the bottom of the left column: the menu opens above it, from its left edge
    m.style.right = 'auto'; m.style.left = Math.max(10, r.left) + 'px'; m.style.top = Math.max(10, r.top - 12 - items.length * 46) + 'px';
  }
  requestAnimationFrame(() => m.classList.add('in'));
  const close = () => { m.classList.remove('in'); menuLayer.classList.remove('open'); setTimeout(() => { menuLayer.innerHTML = ''; }, 250); };
  m.addEventListener('click', (e) => { const mi = e.target.closest('.mi'); if (mi) { pick(items[+mi.dataset.i].v); close(); } });
  menuLayer.onclick = (e) => { if (e.target === menuLayer) close(); };
}
let toastT = 0;
function toast(t) { const el = $('#toast'); el.textContent = t; el.classList.add('show'); clearTimeout(toastT); toastT = setTimeout(() => el.classList.remove('show'), 2400); }

/* ---------------------------------------------------------------- lists: reminders, voices, wifi */
document.addEventListener('click', async (e) => {
  const t = e.target.closest('[data-rdel],[data-vmenu],[data-wnet],[data-wpick],[data-wother]'); if (!t) return;
  const d = t.dataset;
  if (d.vmenu) {
    const name = d.vmenu;
    menu(t, [{ t: 'Заметка', v: 'note' }, { t: 'Переименовать', v: 'ren' }, { t: 'Забыть голос', v: 'del' }], async (act) => {
      if (act === 'del') { if (await confirmAsk('Забыть голос «' + name + '»?', 'Колонка перестанет узнавать этого человека', 'Забыть', 'danger')) api('voice_delete', { name }); }
      else if (act === 'ren') { const v = await promptAsk('Как называть человека?', '', name); if (v && v !== name) api('voice_rename', { name, new: v }); }
      else { const v = await promptAsk('Заметка о «' + name + '»', 'Как обращаться, что любит. Пусто = стереть'); if (v !== null) api('voice_note', { name, note: v }); }
    });
  }
  else if (d.rdel) { await api('rem_cancel', { id: d.rdel }); toast('Отменено'); }
  else if (d.wnet) menu(t, [{ t: 'Забыть сеть', v: 'forget' }], async () => { if (await confirmAsk('Забыть сеть «' + d.wnet + '»?', '', 'Забыть', 'danger')) api('wifi_forget', { ssid: d.wnet }); });
  else if (d.wpick) wifiAdd(d.wpick);
  else if (d.wother) wifiAdd('');
});
async function wifiAdd(ssid) {
  const r = await ask({ title: 'Добавить сеть', text: 'Без «Сразу» колонка только запомнит сеть и подключится к ней, когда окажется в её зоне', fields: [{ placeholder: 'Название сети', value: ssid }, { placeholder: 'Пароль (пусто = открытая)', type: 'password' }],
    actions: [{ t: 'Отмена' }, { t: 'Запомнить', role: '' }, { t: 'Сразу', role: 'pri' }] });
  const [name, pass] = r.v; if (!name || r.a.t === 'Отмена') return;
  const now = r.a.t === 'Сразу';
  if (now && !(await confirmAsk('Переключиться на «' + name + '»?', 'Если не получится, колонка вернётся к прежней сети через 45 секунд'))) return;
  api('wifi_add', { ssid: name, password: pass, now }); toast('Отправлено');
}
$('#btnEnroll').addEventListener('click', async () => {
  const n = await promptAsk('Запомнить голос', 'Как тебя зовут?');
  if (!n || !n.trim()) return;
  S.enrollWatch = Date.now();
  const r = await api('voice_enroll', { name: n.trim() });
  if (r && r.ok === false) { S.enrollWatch = 0; return; }
  toast('Колонка сейчас заговорит: слушайте и повторяйте фразы');
});
function enrollProgress(e) {              // she greets and gives three phrases; the panel shows where it is and any error
  const btn = $('#btnEnroll');
  if (!e || !e.state) return;
  if (e.state === 'waiting' || e.state === 'sample') { btn.textContent = e.state === 'sample' ? `Запоминаю: ${e.step} из ${e.need}` : 'Слушаю вас…'; S.enrollSeen = e.t; return; }
  btn.textContent = 'Запомнить мой голос';
  if (S.enrollWatch && e.t * 1000 > S.enrollWatch - 5000 && S.enrollSeen !== 'x' + e.t) {
    S.enrollSeen = 'x' + e.t; S.enrollWatch = 0;
    toast(e.state === 'done' ? 'Голос запомнен' : e.error || 'Не получилось, повторите');
  }
}

/* ---- a wheel like the iOS picker: a cylinder with 24° between the rows, drag, inertia, mouse wheel, snap to a value */
function makeWheel(el, n) {
  const ROW = 54, STEP = 24 * Math.PI / 180, R = ROW / Math.sin(STEP), HALF = 4;
  const rows = []; for (let i = -HALF; i <= HALF; i++) { const r = document.createElement('div'); r.className = 'wi'; el.appendChild(r); rows.push(r); }
  let pos = 0, target = 0, anim = null, drag = null, acc = 0;
  const mod = (v) => ((v % n) + n) % n;
  function paint() {
    const base = Math.round(pos), frac = pos - base;
    rows.forEach((r, i) => {
      const k = i - HALF, th = (k - frac) * STEP;
      if (Math.abs(th) > 1.4) { r.style.opacity = 0; return; }
      const c = Math.cos(th);
      r.textContent = pad2(mod(base + k));
      r.style.transform = `translateY(${(R * Math.sin(th)).toFixed(2)}px) scale(${(0.8 + 0.2 * c).toFixed(3)},${c.toFixed(3)})`;
      r.style.opacity = Math.pow(c, 2.4).toFixed(3);
    });
  }
  function settle(to, v = 0) {
    target = to; anim && anim.cancel();
    anim = spring({ from: pos, to, v, k: 150, c: 24, update: (x) => { pos = x; paint(); } });
  }
  el.addEventListener('pointerdown', (e) => {
    if (e.pointerType === 'mouse' && e.button !== 0) return;
    anim && anim.cancel(); try { el.setPointerCapture(e.pointerId); } catch (_) {}
    drag = { y: e.clientY, p0: pos, id: e.pointerId, ly: e.clientY, lt: performance.now(), v: 0 };
  });
  el.addEventListener('pointermove', (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    const now = performance.now(), dt = Math.max(12, now - drag.lt) / 1000;
    drag.v = clamp(0.7 * drag.v + 0.3 * (-(e.clientY - drag.ly) / ROW / dt), -40, 40); drag.ly = e.clientY; drag.lt = now;
    pos = drag.p0 - (e.clientY - drag.y) / ROW; paint();
  });
  const end = () => { if (!drag) return; const d = drag; drag = null; const v = performance.now() - d.lt > 80 ? 0 : d.v; settle(Math.round(pos + v * 0.22), v); };
  el.addEventListener('pointerup', end); el.addEventListener('pointercancel', end);
  el.addEventListener('wheel', (e) => { e.preventDefault(); acc += e.deltaY; if (Math.abs(acc) >= 28) { const s = Math.trunc(acc / 28); acc -= s * 28; settle((anim ? target : Math.round(pos)) + s); } }, { passive: false });
  paint();
  return { set(v) { anim && anim.cancel(); pos = target = v; paint(); }, get value() { return mod(Math.round(pos)); } };
}

/* ---- the alarm editor (as in iOS Clock) */
const AL = { id: '', days: [], snooze: true, snoozeMin: 9 };
const wH = makeWheel($('#wH'), 24), wM = makeWheel($('#wM'), 60);
function paintDays() { $$('#days button').forEach((b) => b.classList.toggle('on', AL.days.includes(+b.dataset.d))); $('#repVal').textContent = repeatText(AL.days); }
$$('#days button').forEach((b) => b.addEventListener('click', () => { const d = +b.dataset.d; AL.days = AL.days.includes(d) ? AL.days.filter((x) => x !== d) : [...AL.days, d].sort(); paintDays(); }));
function paintSnooze() { setSwitchEl($('#swSnooze'), AL.snooze); $('#rowSnoozeLen').hidden = !AL.snooze; $('#snoozeVal').textContent = AL.snoozeMin + ' мин'; }
bindSwitch($('#swSnooze'), (on) => { AL.snooze = on; paintSnooze(); });
$('#rowSnoozeLen').addEventListener('click', () => menu($('#rowSnoozeLen'), [5, 9, 10, 15, 20, 30].map((m) => ({ t: m + ' мин', on: m === AL.snoozeMin, v: m })), (m) => { AL.snoozeMin = m; paintSnooze(); }));
function openAlarm(a) {
  AL.id = a ? a.id : '';
  const now = new Date(), [h, m] = a ? a.time.split(':').map(Number) : [now.getHours(), now.getMinutes()];
  wH.set(h); wM.set(m);
  AL.days = a ? repeatDays(a.repeat) : []; AL.snooze = a ? a.snooze_min > 0 : true; AL.snoozeMin = a && a.snooze_min > 0 ? a.snooze_min : 9;
  $('#alName').value = a ? a.label || '' : '';
  $('#alarmTitle').textContent = a ? 'Будильник' : 'Новый будильник'; $('#btnAlarmDelete').hidden = !a;
  paintDays(); paintSnooze(); openSheet($('#sheetAlarm'));
}
$('#btnAlarmSave').addEventListener('click', async () => {
  const time = pad2(wH.value) + ':' + pad2(wM.value);
  const r = await api('rem_add', { kind: 'alarm', time, text: $('#alName').value.trim(), repeat: repeatParam(AL.days), snooze_min: AL.snooze ? AL.snoozeMin : 0, replace_id: AL.id });
  if (!r || r.ok === false) return;
  closeSheet(); toast(AL.id ? 'Будильник изменён' : 'Будильник поставлен на ' + time);
});
$('#btnAlarmDelete').addEventListener('click', async () => {
  if (!(await confirmAsk('Удалить будильник?', '', 'Удалить', 'danger'))) return;
  await api('rem_cancel', { id: AL.id }); closeSheet(); toast('Будильник удалён');
});
// the list: a tap opens the editor, the switch turns the alarm off / on
document.addEventListener('pointerdown', (e) => { const sw = e.target.closest('[data-altog]'); if (sw) sw.classList.add('pressed'); });
['pointerup', 'pointercancel'].forEach((t) => document.addEventListener(t, () => $$('[data-altog].pressed').forEach((s) => s.classList.remove('pressed'))));
document.addEventListener('click', (e) => {
  const sw = e.target.closest('[data-altog]');
  if (sw) { const on = !sw.classList.contains('on'); sw.classList.toggle('on', on); sw.closest('.al').classList.toggle('off', !on); api('alarm_toggle', { id: sw.dataset.altog, on }); return; }
  const row = e.target.closest('[data-alarm]');
  if (row) { const a = (S.alarms || []).find((x) => x.id === row.dataset.alarm); if (a) openAlarm(a); }
});

/* ---- the timer */
const tH = makeWheel($('#tH'), 24), tM = makeWheel($('#tM'), 60), tS = makeWheel($('#tS'), 60);
function openTimer() { tH.set(0); tM.set(5); tS.set(0); $('#tmName').value = ''; openSheet($('#sheetTimer')); }
$('#btnTimerSave').addEventListener('click', async () => {
  const total = tH.value * 3600 + tM.value * 60 + tS.value;
  if (!total) { toast('Выбери время'); return; }
  const r = await api('rem_add', { kind: 'timer', minutes: total / 60, text: $('#tmName').value.trim() });
  if (!r || r.ok === false) return;
  closeSheet(); toast('Таймер запущен');
});

/* ---- the reminder */
function bindSeg(seg, onChange) {
  const lens = $('.seg-lens', seg), btns = $$('button', seg); let cur = btns[0].dataset.k, lx = null;
  function set(k, instant) {
    cur = k; btns.forEach((b) => b.classList.toggle('on', b.dataset.k === k));
    const b = btns.find((x) => x.dataset.k === k), x = b.offsetLeft - 3;
    lens.style.width = b.offsetWidth + 'px';
    if (instant || lx === null) { lens.style.transform = `translateX(${x}px)`; lx = x; }
    else spring({ from: lx, to: x, k: 380, c: 24, update: (v) => { lens.style.transform = `translateX(${v}px)`; lx = v; } });
    onChange(k);
  }
  btns.forEach((b) => b.addEventListener('click', () => set(b.dataset.k)));
  return { set, get value() { return cur; } };
}
const rmSeg = bindSeg($('#rmKind'), (k) => { $('#rmInRow').hidden = k !== 'in'; $('#rmAtRow').hidden = $('#rmRepRow').hidden = k !== 'at'; });
function openReminder() { $('#rmText').value = ''; $('#rmMin').value = ''; openSheet($('#sheetReminder')); rmSeg.set('in', true); }
$('#btnRemSave').addEventListener('click', async () => {
  const text = $('#rmText').value.trim(), inMode = rmSeg.value === 'in', minutes = +$('#rmMin').value || 0;
  if (!text) { toast('Напиши, что напомнить'); return; }
  if (inMode && minutes <= 0) { toast('Через сколько минут?'); return; }
  const r = await api('rem_add', inMode ? { kind: 'reminder', text, minutes } : { kind: 'reminder', text, time: $('#rmTime').value, repeat: $('#rmRep').value });
  if (!r || r.ok === false) return;
  closeSheet(); toast('Напоминание поставлено');
});

/* ---------------------------------------------------------------- smart home (Home Assistant): rooms, tiles, the device sheet */
const DEV_IC = {
  light: '<path d="M9 18h6M10 21h4M12 3a6 6 0 0 0-3.5 10.9c.6.5 1 1.2 1 2.1h5c0-.9.4-1.6 1-2.1A6 6 0 0 0 12 3z"/>',
  switch: '<rect x="3.5" y="8" width="17" height="8" rx="4"/><circle cx="16" cy="12" r="2.2"/>',
  input_boolean: '<rect x="3.5" y="8" width="17" height="8" rx="4"/><circle cx="16" cy="12" r="2.2"/>',
  fan: '<circle cx="12" cy="12" r="1.6"/><path d="M12 10.4C12 6 14 3.5 16.5 4.5S17 9 12 10.4zM13.6 12c4.4 0 6.9 2 5.9 4.5S15 17 13.6 12zM12 13.6C12 18 10 20.5 7.5 19.5S7 15 12 13.6zM10.4 12C6 12 3.5 10 4.5 7.5S9 7 10.4 12z"/>',
  cover: '<path d="M4 4h16v4H4zM6 8v10M12 8v10M18 8v10"/>',
  climate: '<path d="M12 3v11.2a3.8 3.8 0 1 1-3 0V3a1.5 1.5 0 0 1 3 0z"/>',
};
const SWATCHES = ['#ff5a5a', '#ff7f6e', '#ff9a45', '#ffc247', '#c5ff0f', '#62e862', '#2de3a6', '#2ae6e6', '#62e4ff', '#6688e8', '#e6e6fb', '#8a62e8', '#a864e8', '#e63de6', '#e676dc', '#e84672', '#e87fa0'];
const hexRgb = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
const HM = { devices: [], configured: true, busy: 0, id: '' };
const devById = (id) => HM.devices.find((d) => d.id === id);
const devOn = (d) => !['off', 'closed', 'unavailable', 'unknown', 'idle'].includes(d.state);
function devColor(d) {         // the colour of a lit tile
  let c = d.rgb || [255, 190, 110];
  if (Math.min(...c) > 205) c = [170, 150, 255];
  const k = 0.55 + 0.45 * ((d.bri_pct ?? 100) / 100);
  return `rgb(${c.map((v) => Math.round(v * 0.85 * k)).join(',')})`;
}
function devState(d) {
  if (d.state === 'unavailable') return 'Недоступно';
  if (!devOn(d)) return d.id.startsWith('cover.') ? 'Закрыто' : 'Выключено';
  if (d.bri_pct != null) return `Включено · ${d.bri_pct}%`;
  if (d.id.startsWith('cover.')) return d.pos != null ? `Открыто · ${d.pos}%` : 'Открыто';
  if (d.id.startsWith('climate.')) return d.cur != null ? `${Math.round(d.cur)}° → ${d.temp ?? '—'}°` : d.state;
  return 'Включено';
}
function renderHome() {
  const box = $('#homeBox');
  if (!HM.configured) { box.innerHTML = '<div class="inset"><div class="empty">Home Assistant не подключён: пропиши HA_URL и HA_TOKEN в server\\.env и перезапусти сервер</div></div>'; $('#uHomeSub').textContent = 'Не подключён'; return; }
  const rooms = {};
  HM.devices.forEach((d) => { (rooms[d.area || 'Без комнаты'] = rooms[d.area || 'Без комнаты'] || []).push(d); });
  const names = Object.keys(rooms).sort((a, b) => (a === 'Без комнаты') - (b === 'Без комнаты') || a.localeCompare(b, 'ru'));
  $('#uHomeSub').textContent = HM.devices.length ? `${HM.devices.length} устройств · включено ${HM.devices.filter(devOn).length}` : 'Устройств нет';
  box.innerHTML = names.length ? names.map((n) => `<div class="cap">${esc(n)}</div><div class="devgrid">` + rooms[n].map((d) => {
    const on = devOn(d), ic = DEV_IC[d.id.split('.')[0]] || DEV_IC.switch;
    return `<div class="dev${on ? ' on' : ''}" data-dev="${esc(d.id)}" style="--c:${devColor(d)}"><svg class="ic" viewBox="0 0 24 24">${ic}</svg>` +
      `<button class="more" data-devpower="${esc(d.id)}" aria-label="Включить / выключить"><svg viewBox="0 0 24 24"><path d="M12 3v8.5M7 6.5a7.5 7.5 0 1 0 10 0" fill="none" stroke="#fff" stroke-width="2.2" stroke-linecap="round"/></svg></button>` +
      `<div><b>${esc(d.name)}</b><span class="st">${esc(devState(d))}</span></div></div>`;
  }).join('') + '</div>').join('') : '<div class="inset"><div class="empty">Устройств нет. В Home Assistant должны быть лампы, розетки, шторы или климат</div></div>';
  if (HM.id) paintDevice();
}
async function homeLoad() {
  if (document.hidden) return;
  try {
    const j = await (await fetch('/api/home', { cache: 'no-store' })).json();
    HM.configured = j.configured;
    HM.scenes = j.scenes || [];
    if (Date.now() < HM.busy) return;                // the finger is on a control: the answer would jerk it back
    HM.devices = j.devices || []; renderHome();
  } catch (e) { /* the server is away: the status shows it */ }
}
async function homeSet(id, body) {      // an optimistic change at once, then the real one
  const d = devById(id); HM.busy = Date.now() + 2500;
  if (d) {
    if (body.service === 'turn_off') d.state = 'off'; else if (body.service === 'toggle') d.state = devOn(d) ? 'off' : 'on'; else d.state = 'on';
    if (body.brightness_pct != null) d.bri_pct = body.brightness_pct; else if (d.bri_pct == null && d.dim && devOn(d)) d.bri_pct = 100;
    if (body.rgb) d.rgb = body.rgb;
    renderHome();
  }
  const r = await api('home_set', { id, ...body });
  setTimeout(() => { HM.busy = 0; homeLoad(); }, 700);
  return r;
}
document.addEventListener('click', (e) => {
  const pw = e.target.closest('[data-devpower]');                    // the small power button of a tile: switch at once
  if (pw) { e.stopPropagation(); homeSet(pw.dataset.devpower, { service: 'toggle' }); return; }
  const tile = e.target.closest('[data-dev]');                       // the tile itself opens the device
  if (tile) openDevice(tile.dataset.dev);
});
setInterval(() => { if (S.stack[S.tab] === 'home' || HM.id) homeLoad(); }, 3000);
setTimeout(homeLoad, 1500);

/* the device sheet */
function bindBar(el, onPct) {
  const fill = $('i', el), lab = $('span', el); let drag = false, last = 0;
  const pct = (e) => { const r = el.getBoundingClientRect(); return clamp(Math.round(100 * (e.clientX - r.left) / r.width), 1, 100); };
  const paint = (p) => { fill.style.width = p + '%'; lab.textContent = p + '%'; };
  el.addEventListener('pointerdown', (e) => { drag = true; try { el.setPointerCapture(e.pointerId); } catch (_) {} const p = pct(e); paint(p); onPct(p, false); last = Date.now(); });
  el.addEventListener('pointermove', (e) => { if (!drag) return; const p = pct(e); paint(p); if (Date.now() - last > 250) { last = Date.now(); onPct(p, false); } });
  const end = (e) => { if (!drag) return; drag = false; const p = pct(e); paint(p); onPct(p, true); };
  el.addEventListener('pointerup', end); el.addEventListener('pointercancel', () => { drag = false; });
  return paint;
}
const setBri = (p) => { if (HM.id) homeSet(HM.id, { service: 'turn_on', brightness_pct: p }); };
const paintBri = [bindBar($('#devBri'), setBri), bindBar($('#devBri2'), setBri)];
$('#devSw').innerHTML = SWATCHES.map((c) => `<button style="--c:${c}" data-sw="${c}" aria-label="${c}"></button>`).join('') + '<button class="plus" id="devPlus" aria-label="Свой цвет"><svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg></button>';
$('#devSw').addEventListener('click', (e) => { const b = e.target.closest('[data-sw]'); if (b && HM.id) homeSet(HM.id, { service: 'turn_on', rgb: hexRgb(b.dataset.sw) }); });
$('#devSw').addEventListener('click', (e) => { if (e.target.closest('#devPlus')) $('#devPick').click(); });
$('#devPick').addEventListener('change', (e) => { if (HM.id) homeSet(HM.id, { service: 'turn_on', rgb: hexRgb(e.target.value) }); });
$('#devPower').addEventListener('click', () => { if (HM.id) homeSet(HM.id, { service: 'toggle' }); });
function paintDevice() {
  const d = devById(HM.id); if (!d) return;
  const on = devOn(d), body = $('#sheetDevice .dev-body');
  body.classList.toggle('on', on); body.style.setProperty('--c', devColor(d));
  body.style.setProperty('--cc', d.rgb && on ? `rgb(${d.rgb.join(',')})` : '#fff');        // the brightness bar takes the colour of the light
  $('#devName').textContent = d.name; $('#devRoom').textContent = d.area || 'Без комнаты';
  $('#devIcon').innerHTML = DEV_IC[d.id.split('.')[0]] || DEV_IC.switch;
  $('#devColor').hidden = !(d.color && on); $('#devDim').hidden = !(d.dim && !d.color && on); $('#devCt').hidden = !(d.ct && on);
  const p = d.bri_pct ?? 100; paintBri.forEach((f) => f(p));
  $$('#devSw [data-sw]').forEach((b) => { const c = hexRgb(b.dataset.sw); b.classList.toggle('sel', !!d.rgb && c.every((v, i) => Math.abs(v - d.rgb[i]) < 6)); });
  if (d.ct) {
    const a = d.kmin || 2200, z = d.kmax || 6500;
    $('#devCtSw').innerHTML = [0, 1 / 3, 2 / 3, 1].map((t) => { const k = Math.round(a + (z - a) * t); const w = k < 3200 ? '#ffb46b' : k < 4500 ? '#ffe2b8' : k < 5600 ? '#fff6ea' : '#d6e8ff'; return `<button style="--c:${w}" data-k="${k}" aria-label="${k} K"></button>`; }).join('');
  }
}
$('#devCtSw').addEventListener('click', (e) => { const b = e.target.closest('[data-k]'); if (b && HM.id) homeSet(HM.id, { service: 'turn_on', kelvin: +b.dataset.k }); });
function openDevice(id) { HM.id = id; paintDevice(); openSheet($('#sheetDevice')); }
new MutationObserver(() => { if (!$('#sheetDevice').classList.contains('show')) HM.id = ''; }).observe($('#sheetDevice'), { attributes: true, attributeFilter: ['class'] });

/* ---------------------------------------------------------------- scenarios: a list, a run button, and the editor with its actions */
const ACT = {
  light: { t: 'Свет', ic: '<path d="M9 18h6M10 21h4M12 3a6 6 0 0 0-3.5 10.9c.6.5 1 1.2 1 2.1h5c0-.9.4-1.6 1-2.1A6 6 0 0 0 12 3z"/>', def: () => ({ type: 'light', entity: '', on: true, brightness_pct: 60 }) },
  ha: { t: 'Сцена или скрипт Home Assistant', ic: '<path d="M13 3L5 13.5h6L10 21l8-10.5h-6z"/>', def: () => ({ type: 'ha', entity: '', service: 'turn_on' }) },
  say: { t: 'Сказать', ic: '<path d="M4 5.5h16v10H10l-4.5 3.5v-3.5H4z"/>', def: () => ({ type: 'say', text: '' }) },
  music: { t: 'Музыка', ic: '<path d="M9 18V5l11-2v13"/><circle cx="6.5" cy="18" r="2.5"/><circle cx="17.5" cy="16" r="2.5"/>', def: () => ({ type: 'music', action: 'pause' }) },
  volume: { t: 'Громкость', ic: '<path d="M4 9.5h3.2L12 5.5v13l-4.8-4H4z"/><path d="M15.2 9a4.2 4.2 0 0 1 0 6"/>', def: () => ({ type: 'volume', level: 20 }) },
  fade: { t: 'Плавно приглушить музыку и остановить', ic: '<path d="M20.5 14.5A8.5 8.5 0 1 1 9.5 3.5a6.5 6.5 0 0 0 11 11z"/>', def: () => ({ type: 'fade', minutes: 30, smooth: true }) },
  wait: { t: 'Подождать', ic: '<circle cx="12" cy="13" r="7.5"/><path d="M12 9v4l2.5 2M9.5 3h5"/>', def: () => ({ type: 'wait', seconds: 5 }) },
};
const MUSIC_L = { pause: 'Пауза', play: 'Продолжить', next: 'Следующий трек', previous: 'Предыдущий трек' };
const SC = { id: '', actions: [], days: [0, 1, 2, 3, 4, 5, 6], sched: false, enabled: true };
const SC_ICON = '<path d="M13 3L5 13.5h6L10 21l8-10.5h-6z"/>';
function scSub(sc) {
  const p = [];
  if (sc.phrases && sc.phrases.length) p.push('«' + sc.phrases[0] + '»');
  if (sc.schedule) p.push(sc.schedule.time + ' · ' + (sc.schedule.days.length === 7 ? 'каждый день' : repeatText(sc.schedule.days).toLowerCase()));
  p.push(`${sc.actions.length} ${sc.actions.length === 1 ? 'действие' : sc.actions.length < 5 ? 'действия' : 'действий'}`);
  return p.join(' · ');
}
function renderScenarios(list) {
  list = list || [];
  S.scenarios = list;
  $('#uScSub').textContent = list.length ? `${list.length} · включено ${list.filter((x) => x.enabled).length}` : 'Нет';
  $('#scList').innerHTML = list.length ? '<div class="inset">' + list.map((sc) =>
    `<div class="scn${sc.enabled ? '' : ' off'}" data-scn="${esc(sc.id)}"><span class="ico30 c-violet"><svg viewBox="0 0 24 24">${SC_ICON}</svg></span><div class="meta"><b>${esc(sc.name)}</b><span>${esc(scSub(sc))}</span></div>` +
    `<button class="mini-btn acc" data-scrun="${esc(sc.id)}" aria-label="Запустить"><svg viewBox="0 0 24 24" class="pl"><path d="M7 4.5v15l12-7.5z"/></svg></button><button class="switch${sc.enabled ? ' on' : ''}" data-sctog="${esc(sc.id)}" aria-label="Включён"><i></i></button></div>`).join('') + '</div>'
    : '<div class="inset"><div class="empty">Сценариев нет. Нажми «+»: например «Спокойной ночи» (выключить свет, сказать фразу, плавно приглушить музыку)</div></div>';
}
function devChoices(kinds) { return HM.devices.filter((d) => kinds.some((k) => d.id.startsWith(k + '.'))).map((d) => ({ t: d.name + (d.area ? ' · ' + d.area : ''), v: d.id })); }
const entName = (id) => { const d = HM.devices.find((x) => x.id === id) || (HM.scenes || []).find((x) => x.id === id); return d ? d.name : id || 'Выбрать…'; };
function renderActs() {
  $('#scActions').innerHTML = SC.actions.length ? SC.actions.map((a, i) => {
    const def = ACT[a.type]; let body = '';
    if (a.type === 'light') {
      body = `<div class="row action" data-amenu="entity" data-i="${i}"><span class="k">Устройство</span><span class="v acc">${esc(entName(a.entity))}</span></div>` +
        `<div class="row"><span class="k">Включить</span><button class="switch${a.on ? ' on' : ''}" data-aon="${i}"><i></i></button></div>` +
        (a.on ? `<div class="row"><span class="k">Яркость <b class="vv">${a.brightness_pct || 100}%</b></span><input type="range" min="1" max="100" value="${a.brightness_pct || 100}" data-aprop="brightness_pct" data-i="${i}" class="rng"></div>` +
          `<div class="row"><span class="k">Цвет</span><span class="rgbpick"><input type="color" value="${a.rgb ? '#' + a.rgb.map((v) => v.toString(16).padStart(2, '0')).join('') : '#ffffff'}" data-acolor="${i}">${a.rgb ? `<button class="mini-btn" data-aclear="${i}">Сбросить</button>` : ''}</span></div>` : '');
    } else if (a.type === 'ha') body = `<div class="row action" data-amenu="entity" data-i="${i}"><span class="k">Что запустить</span><span class="v acc">${esc(entName(a.entity))}</span></div>`;
    else if (a.type === 'say') body = `<div class="row"><input type="text" class="plain left" placeholder="Что сказать колонке" value="${esc(a.text)}" data-aprop="text" data-i="${i}"></div>`;
    else if (a.type === 'music') body = `<div class="row action" data-amenu="action" data-i="${i}"><span class="k">Действие</span><span class="v acc">${MUSIC_L[a.action]}</span></div>`;
    else if (a.type === 'volume') body = `<div class="row"><span class="k">Громкость <b class="vv">${a.level}%</b></span><input type="range" min="0" max="100" value="${a.level}" data-aprop="level" data-i="${i}" class="rng"></div>`;
    else if (a.type === 'fade') body = `<div class="row"><span class="k">Минут</span><input type="number" min="1" max="180" value="${a.minutes}" class="pill-in" data-aprop="minutes" data-i="${i}"></div><div class="row"><span class="k">Плавно</span><button class="switch${a.smooth ? ' on' : ''}" data-asmooth="${i}"><i></i></button></div>`;
    else if (a.type === 'wait') body = `<div class="row"><span class="k">Секунд</span><input type="number" min="1" max="600" value="${a.seconds}" class="pill-in" data-aprop="seconds" data-i="${i}"></div>`;
    return `<div class="act"><div class="act-h"><span class="ico30 c-violet"><svg viewBox="0 0 24 24">${def.ic}</svg></span><b>${def.t}</b><button class="mini-btn red" data-adel="${i}">Убрать</button></div>${body}</div>`;
  }).join('') : '<div class="empty">Пока нет действий. Добавь первое</div>';
}
function paintSched() {
  setSwitchEl($('#swScSched'), SC.sched);
  $('#scTimeRow').hidden = !SC.sched; $('#scDays').hidden = !SC.sched;
  $$('#scDays button').forEach((b) => b.classList.toggle('on', SC.days.includes(+b.dataset.d)));
}
async function openScenario(sc) {
  await homeLoad();                                 // the devices and scenes for the pickers
  SC.id = sc ? sc.id : ''; SC.enabled = sc ? sc.enabled : true;
  SC.actions = sc ? JSON.parse(JSON.stringify(sc.actions)) : [];
  SC.sched = !!(sc && sc.schedule); SC.days = sc && sc.schedule ? sc.schedule.days.slice() : [0, 1, 2, 3, 4, 5, 6];
  $('#scName').value = sc ? sc.name : ''; $('#scPhrases').value = sc ? sc.phrases.join(', ') : ''; $('#scTime').value = sc && sc.schedule ? sc.schedule.time : '23:00';
  $('#scTitle').textContent = sc ? 'Сценарий' : 'Новый сценарий'; $('#btnScDelete').hidden = !sc;
  paintSched(); renderActs(); openSheet($('#sheetScenario'));
}
bindSwitch($('#swScSched'), (on) => { SC.sched = on; paintSched(); });
$$('#scDays button').forEach((b) => b.addEventListener('click', () => { const d = +b.dataset.d; SC.days = SC.days.includes(d) ? SC.days.filter((x) => x !== d) : [...SC.days, d].sort(); paintSched(); }));
$('#scAddAction').addEventListener('click', () => menu($('#scAddAction'), Object.entries(ACT).map(([k, v]) => ({ t: v.t, v: k })), (k) => { SC.actions.push(ACT[k].def()); renderActs(); }));
$('#scActions').addEventListener('input', (e) => {
  const el = e.target.closest('[data-aprop]'); if (!el) return;
  const a = SC.actions[+el.dataset.i], k = el.dataset.aprop;
  a[k] = el.type === 'text' ? el.value : +el.value;
  const vv = el.closest('.row').querySelector('.vv'); if (vv) vv.textContent = el.value + '%';
});
$('#scActions').addEventListener('change', (e) => { const c = e.target.closest('[data-acolor]'); if (c) { SC.actions[+c.dataset.acolor].rgb = hexRgb(c.value); renderActs(); } });
$('#scActions').addEventListener('click', (e) => {
  const t = e.target;
  const del = t.closest('[data-adel]'); if (del) { SC.actions.splice(+del.dataset.adel, 1); renderActs(); return; }
  const on = t.closest('[data-aon]'); if (on) { const a = SC.actions[+on.dataset.aon]; a.on = !a.on; renderActs(); return; }
  const sm = t.closest('[data-asmooth]'); if (sm) { const a = SC.actions[+sm.dataset.asmooth]; a.smooth = !a.smooth; renderActs(); return; }
  const cl = t.closest('[data-aclear]'); if (cl) { delete SC.actions[+cl.dataset.aclear].rgb; renderActs(); return; }
  const m = t.closest('[data-amenu]');
  if (m) {
    const a = SC.actions[+m.dataset.i];
    if (m.dataset.amenu === 'action') return menu(m, Object.entries(MUSIC_L).map(([k, n]) => ({ t: n, on: a.action === k, v: k })), (k) => { a.action = k; renderActs(); });
    const opts = a.type === 'light' ? devChoices(['light', 'switch', 'fan', 'input_boolean']) : (HM.scenes || []).map((s) => ({ t: s.name + (s.area ? ' · ' + s.area : ''), v: s.id }));
    if (!opts.length) return toast(a.type === 'light' ? 'Устройств нет: проверь Home Assistant' : 'В Home Assistant нет сцен и скриптов');
    menu(m, opts.map((o) => ({ ...o, on: o.v === a.entity })), (id) => { a.entity = id; renderActs(); });
  }
});
$('#btnScSave').addEventListener('click', async () => {
  const name = $('#scName').value.trim();
  if (!name) { toast('Назови сценарий'); return; }
  if (!SC.actions.length) { toast('Добавь хотя бы одно действие'); return; }
  const bad = SC.actions.find((a) => (a.type === 'light' || a.type === 'ha') && !a.entity);
  if (bad) { toast('Выбери устройство в действии'); return; }
  const r = await api('sc_save', { scenario: { id: SC.id, name, enabled: SC.enabled, phrases: $('#scPhrases').value.split(',').map((x) => x.trim()).filter(Boolean),
    schedule: SC.sched ? { time: $('#scTime').value, days: SC.days } : null, actions: SC.actions } });
  if (!r || r.ok === false) return;
  closeSheet(); toast('Сценарий сохранён');
});
$('#btnScDelete').addEventListener('click', async () => {
  if (!(await confirmAsk('Удалить сценарий?', '', 'Удалить', 'danger'))) return;
  await api('sc_delete', { id: SC.id }); closeSheet(); toast('Сценарий удалён');
});
document.addEventListener('click', (e) => {
  const run = e.target.closest('[data-scrun]');
  if (run) { e.stopPropagation(); api('sc_run', { id: run.dataset.scrun }); toast('Запускаю'); return; }
  const tog = e.target.closest('[data-sctog]');
  if (tog) { e.stopPropagation(); const on = !tog.classList.contains('on'); tog.classList.toggle('on', on); tog.closest('.scn').classList.toggle('off', !on); api('sc_toggle', { id: tog.dataset.sctog, on }); return; }
  const row = e.target.closest('[data-scn]');
  if (row) { const sc = (S.scenarios || []).find((x) => x.id === row.dataset.scn); if (sc) openScenario(sc); }
});

/* ---- the sleep timer of the Sound page */
let smooth = true; try { smooth = localStorage.getItem('smooth') !== '0'; } catch (e) {}
setSwitchEl($('#swSmooth'), smooth);
bindSwitch($('#swSmooth'), (on) => { smooth = on; try { localStorage.setItem('smooth', on ? '1' : '0'); } catch (e) {} });
function renderSleep(sl) {
  const left = sl && sl.until ? sl.until - nowSrv() : 0;
  $('#sleepVal').textContent = left > 0 ? 'ещё ' + (left >= 90 ? Math.ceil(left / 60) + ' мин' : Math.ceil(left) + ' с') : 'Выкл';
  $('#sleepSub').textContent = left > 0 ? (sl.smooth ? 'громкость плавно падает' : 'музыка играет до конца таймера') : 'через заданное время';
  S.sleepOn = left > 0;
}
$('#rowSleep').addEventListener('click', () => {
  const items = S.sleepOn ? [{ t: 'Отменить таймер сна', v: 0 }] : [15, 30, 45, 60, 90].map((m) => ({ t: m + ' мин', v: m }));
  menu($('#rowSleep'), items, (m) => { if (!m) { api('sleep_cancel'); toast('Таймер сна отменён'); } else { api('sleep_start', { minutes: m, smooth }); toast('Музыка остановится через ' + m + ' мин'); } });
});

/* ---------------------------------------------------------------- service health, backup, notification centre, scenario templates */
async function healthLoad(force) {
  $('#healthList').innerHTML = '<div class="inset"><div class="empty">Проверяю…</div></div>';
  try {
    const j = await (await fetch('/api/health' + (force ? '?force=1' : ''), { cache: 'no-store' })).json();
    $('#healthList').innerHTML = '<div class="inset">' + j.items.map((x) =>
      `<div class="item"><i class="dot ${x.ok ? '' : x.optional ? 'off' : 'bad'}"></i><div class="t"><b>${esc(x.name)}</b><span>${esc(x.detail)}${x.ms ? ' · ' + x.ms + ' мс' : ''}</span></div></div>`).join('') + '</div>';
  } catch (e) { $('#healthList').innerHTML = '<div class="inset"><div class="empty">Сервер не ответил</div></div>'; }
}
$('#btnHealth').addEventListener('click', () => healthLoad(true));
$$('[data-sub="health"]').forEach((b) => b.addEventListener('click', () => healthLoad(true)));

$('#btnRestore').addEventListener('click', () => $('#restoreFile').click());
$('#restoreFile').addEventListener('change', async (e) => {
  const f = e.target.files[0]; e.target.value = ''; if (!f) return;
  if (!(await confirmAsk('Восстановить из «' + f.name + '»?', 'Сценарии с теми же именами заменятся, недостающие будильники добавятся', 'Восстановить'))) return;
  try {
    const r = await (await fetch('/api/restore', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: await f.text() })).json();
    if (r.ok) { const n = r.result; toast(`Готово: сценариев ${n.scenarios}, будильников ${n.alarms}, заметок ${n.notes}`); setTimeout(load, 400); } else toast(r.error || 'Не получилось');
  } catch (err) { toast('Не получилось прочитать файл'); }
});

/* notification centre */
const NK = { alarm: ['Будильник', '<circle cx="12" cy="13" r="7.5"/><path d="M12 9v4l2.5 2M5 4.5L2.5 7M19 4.5L21.5 7"/>', 'c-violet'], timer: ['Таймер', '<circle cx="12" cy="13.5" r="7.5"/><path d="M12 9.5v4l2.5 1.5M9.5 3h5"/>', 'c-orange'],
  reminder: ['Напоминание', '<path d="M6 16V11a6 6 0 0 1 12 0v5l1.5 2h-15zM10 20a2 2 0 0 0 4 0"/>', 'c-blue'], scenario: ['Сценарий', '<path d="M13 3L5 13.5h6L10 21l8-10.5h-6z"/>', 'c-violet'],
  warn: ['Сбой', '<path d="M12 4l9 16H3z"/><path d="M12 10v4.5M12 17.5v.2"/>', 'c-orange'] };
let nSeen = 0, nFirst = true; try { nSeen = +localStorage.getItem('nseen') || 0; } catch (e) {}
function renderNotifs(list) {
  list = list || [];
  const top = list.length ? list[list.length - 1].id : 0;
  if (nFirst) { nFirst = false; if (top < nSeen) nSeen = 0; }
  else list.filter((n) => n.id > S.nTop).forEach((n) => { if (!$('#sheetNotifs').classList.contains('show')) toast(n.title); });
  S.nTop = top;
  const unread = list.filter((n) => n.id > nSeen).length;
  $('#bellBadge').hidden = !unread; $('#bellBadge').textContent = unread > 9 ? '9+' : unread;
  $('#notifList').innerHTML = list.length ? list.slice().reverse().map((n) => {
    const k = NK[n.kind] || NK.warn, d = new Date(n.t * 1000);
    return `<div class="item"><span class="ico30 ${k[2]}"><svg viewBox="0 0 24 24">${k[1]}</svg></span><div class="t"><b>${esc(n.title)}</b><span>${esc(n.text || k[0])} · ${pad2(d.getHours())}:${pad2(d.getMinutes())}</span></div></div>`;
  }).join('') : '<div class="empty">Пока ничего</div>';
}
$('#btnBell').addEventListener('click', () => {
  openSheet($('#sheetNotifs'));
  if (S.nTop) { nSeen = S.nTop; try { localStorage.setItem('nseen', String(nSeen)); } catch (e) {} $('#bellBadge').hidden = true; }
});

/* scenario templates: opens the editor filled in, nothing is saved before the tick */
const SC_TPL = [
  { name: 'Спокойной ночи', phrases: ['спокойной ночи', 'ложусь спать'], actions: [{ type: 'say', text: 'Спокойной ночи' }, { type: 'fade', minutes: 30, smooth: true }] },
  { name: 'Ночной режим', phrases: [], schedule: { time: '23:00', days: [0, 1, 2, 3, 4, 5, 6] }, actions: [{ type: 'volume', level: 15 }] },
  { name: 'Дневной режим', phrases: [], schedule: { time: '07:30', days: [0, 1, 2, 3, 4, 5, 6] }, actions: [{ type: 'volume', level: 40 }] },
  { name: 'Я дома', phrases: ['я дома', 'я вернулся'], actions: [{ type: 'say', text: 'С возвращением' }, { type: 'volume', level: 35 }] },
  { name: 'Доброе утро', phrases: ['доброе утро', 'я проснулся'], schedule: { time: '07:30', days: [0, 1, 2, 3, 4] }, actions: [{ type: 'say', text: 'Доброе утро' }, { type: 'music', action: 'play' }] },
];
$('#btnScTpl').addEventListener('click', () => menu($('#btnScTpl'), SC_TPL.map((t, i) => ({ t: t.name, v: i })), (i) => openScenario({ ...JSON.parse(JSON.stringify(SC_TPL[i])), id: '', enabled: true })));

/* ---- HTTPS: the instructions with this server's own addresses */
function renderHttps(h) {
  const host = location.hostname, secure = location.protocol === 'https:';
  const on = !!(h && h.on), port = h && h.port;
  $('#httpsStatus').innerHTML = `<i class="dot ${on ? '' : 'off'}"></i><span>${secure ? 'Сейчас открыто по HTTPS' : on ? 'Защищённый порт включён: ' + port : 'Защищённый порт выключен'}</span>`;
  const httpUrl = `http://${host}:${location.port || 80}/lostos-ca.crt`, httpsUrl = `https://${host}:${port || 8443}/`;
  $('#stepCa').textContent = httpUrl; $('#stepHttps').textContent = httpsUrl;
  $('#btnHttps').href = httpsUrl; $('#btnHttps').style.display = on ? '' : 'none';
  $('#btnCa').href = `http://${host}:${location.port || 80}/lostos-ca.crt`;
}
/* ---- push on this device */
const b64u = (s) => Uint8Array.from(atob((s + '='.repeat((4 - s.length % 4) % 4)).replace(/-/g, '+').replace(/_/g, '/')), (c) => c.charCodeAt(0));
const standalone = () => navigator.standalone === true || matchMedia('(display-mode: standalone)').matches;
async function pushInfo() {
  if (!window.isSecureContext) return { ok: false, text: 'Нужен HTTPS', hint: 'Открой панель по https:// (Настройки → «Защищённое соединение»)' };
  if (!('serviceWorker' in navigator) || !('PushManager' in window))
    return { ok: false, text: 'Недоступно', sub: /iPhone|iPad/.test(navigator.userAgent) && !standalone() ? 'На iPhone: «Поделиться» → «На экран Домой», потом открой оттуда' : 'Этот браузер не умеет пуши' };
  if (Notification.permission === 'denied') return { ok: false, text: 'Запрещено', hint: 'Разреши уведомления для lostOS в настройках телефона или браузера' };
  const reg = await Promise.race([navigator.serviceWorker.ready, new Promise((r) => setTimeout(() => r(null), 4000))]);
  if (!reg) return { ok: false, text: 'Нет воркера', hint: 'Сервис-воркер ещё не запустился: обнови страницу' };
  const sub = await reg.pushManager.getSubscription();
  return { ok: true, reg, sub, text: sub ? 'Включены' : 'Выключены', hint: sub ? 'Это устройство получает события колонки' : 'Нажми, чтобы получать события, даже когда приложение закрыто' };
}
async function renderPush() {
  const i = await pushInfo();
  if (i.ok && i.sub) {          // the phone has a subscription: (re)register it, the server may have lost it (a restart in the middle of a request)
    try { await fetch('/api/push/subscribe', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(i.sub.toJSON()) }); } catch (e) { /* the server is away */ }
  }
  $('#pushVal').textContent = i.text; $('#pushSub').textContent = (S.pushErr ? 'Ошибка: ' + S.pushErr + '. ' : '') + i.hint + (S.s && S.s.push ? ' · на сервере подписок: ' + S.s.push.subs : ''); $('#rowPushTest').hidden = !(i.ok && i.sub);
  S.pushInfo = i;
}
$('#rowPush').addEventListener('click', () => {
  // iOS opens the permission question only from the tap itself, before any await: ask first, look around afterwards
  if (S.pushBusy) return;                    // a second tap while the first one works would switch the subscription off again
  S.pushBusy = true; $('#pushVal').textContent = '…'; S.pushErr = '';
  const asking = ('Notification' in window && window.isSecureContext && Notification.permission === 'default') ? Notification.requestPermission() : null;
  (async () => {
    try { await pushToggle(asking); } finally { S.pushBusy = false; }
  })();
});
const within = (p, ms, step) => Promise.race([p, new Promise((_, rej) => setTimeout(() => rej(new Error('завис на шаге «' + step + '» (больше ' + ms / 1000 + ' с)')), ms))]);
async function pushToggle(asking) {
  const step = (t) => { $('#pushSub').textContent = 'Шаг: ' + t + '…'; };
  try {
    step('проверка'); const i = await within(pushInfo(), 8000, 'проверка');
    if (!i.ok) { S.pushErr = ''; toast(i.hint); await renderPush(); return; }
    if (i.sub) {                                   // a subscription exists: this tap switches it off
      step('отключение'); await within(fetch('/api/push/unsubscribe', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ endpoint: i.sub.endpoint }) }), 8000, 'сообщить серверу');
      await within(i.sub.unsubscribe(), 8000, 'отписка в телефоне'); toast('Уведомления на телефон выключены'); await renderPush(); return;
    }
    step('разрешение'); const perm = asking ? await within(asking, 15000, 'разрешение') : Notification.permission;
    if (perm !== 'granted') { S.pushErr = 'разрешение не дано (' + perm + ')'; toast('Без разрешения уведомления не включить'); await renderPush(); return; }
    step('ключ сервера'); const key = (await (await within(fetch('/api/push/key'), 8000, 'ключ сервера')).json()).key;
    step('подписка в телефоне'); const sub = await within(i.reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64u(key) }), 20000, 'подписка в телефоне');
    step('сообщаю серверу'); const r = await (await within(fetch('/api/push/subscribe', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(sub.toJSON()) }), 8000, 'сообщить серверу')).json();
    toast(r.ok ? 'Уведомления включены' : (r.error || 'Не получилось'));
  } catch (e) { S.pushErr = (e.name && e.name !== 'Error' ? e.name + ': ' : '') + (e.message || e).toString().slice(0, 100); toast('Не получилось: ' + S.pushErr); }
  await renderPush();
}
$('#rowPushTest').addEventListener('click', async () => { await fetch('/api/push/test', { method: 'POST' }); toast('Отправил, жди на экране'); });
$('#btnBell').addEventListener('click', () => { renderPush(); });
/* ---------------------------------------------------------------- PWA */
let installEvt = null;
window.addEventListener('beforeinstallprompt', (e) => { e.preventDefault(); installEvt = e; });
$('#rowInstall').addEventListener('click', async () => {
  if (installEvt) { installEvt.prompt(); installEvt = null; return; }
  if (matchMedia('(display-mode: standalone)').matches || navigator.standalone) { toast('Уже установлено'); return; }
  ask({ title: 'Установить lostOS', text: 'iPhone: нажми «Поделиться» → «На экран Домой». Android: меню браузера → «Установить приложение»', actions: [{ t: 'Понятно', role: 'pri' }] });
});
if ('serviceWorker' in navigator && (location.protocol === 'https:' || location.hostname === 'localhost')) navigator.serviceWorker.register('/sw.js').catch(() => {});

/* Chromium: real refraction (SVG displacement) on the glass, as in AirBridge. Safari keeps the blurred glass of the CSS. */
function glassUpgrade() {
  if (isDesktop() || !isChromium || !window.LiquidGlass || !CSS.supports('backdrop-filter', 'url(#a)')) return;
  const o = { thickness: 60, n: 1.45, scale: 0.9, specular: 0.8, blur: 8, saturate: 1.6 };
  [$('#tabbar'), $('#mini')].forEach((el) => window.LiquidGlass.apply(el, o));
}

/* A home-screen app without viewport-fit=cover reports no safe-area inset at the bottom. Face ID iPhones (a status bar of 40+ pt above the window) have the 34 pt home indicator. */
(() => {
  if (!(navigator.standalone === true || matchMedia('(display-mode: standalone)').matches) || innerWidth > innerHeight) return;
  const p = document.createElement('div'); p.style.cssText = 'position:fixed;visibility:hidden;padding-bottom:env(safe-area-inset-bottom)'; document.body.appendChild(p);
  const env = parseFloat(getComputedStyle(p).paddingBottom) || 0; p.remove();
  if (!env && screen.height - innerHeight >= 40) document.documentElement.style.setProperty('--sab', '34px');
})();

/* ?debug=1: a small overlay with the real window sizes and safe-area insets (for finding layout problems on a phone) */
let debugTimer = 0, debugBox = null, debugProbe = null;
function setDebug(on) {
  clearInterval(debugTimer); debugBox && debugBox.remove(); debugProbe && debugProbe.remove(); debugBox = debugProbe = null;
  try { localStorage.setItem('debug', on ? '1' : ''); } catch (e) {}
  if (!on) return;
  const box = document.createElement('pre');
  box.style.cssText = 'position:fixed;z-index:999;left:8px;right:8px;top:calc(env(safe-area-inset-top) + 62px);margin:0;padding:8px 10px;border-radius:12px;background:rgba(0,0,0,.82);color:#7CFC8A;font:11px/1.35 ui-monospace,Menlo,monospace;white-space:pre-wrap;pointer-events:none';
  const probe = document.createElement('div');
  probe.style.cssText = 'position:fixed;left:0;top:0;width:0;height:0;visibility:hidden;padding-top:env(safe-area-inset-top);padding-bottom:env(safe-area-inset-bottom)';
  document.body.append(probe, box);
  const upd = () => {
    const vv = window.visualViewport, cs = getComputedStyle(probe), a = $('#app').getBoundingClientRect(), t = $('#tabbar').getBoundingClientRect();
    box.textContent = [
      `inner ${innerWidth}x${innerHeight}  outer ${outerWidth}x${outerHeight}  screen ${screen.width}x${screen.height}`,
      `visualViewport ${vv ? vv.width.toFixed(0) + 'x' + vv.height.toFixed(1) + ' off ' + vv.offsetTop.toFixed(1) : '-'}  dpr ${devicePixelRatio}`,
      `safe top ${cs.paddingTop} bottom ${cs.paddingBottom}`,
      `standalone: navigator ${navigator.standalone} / display-mode ${matchMedia('(display-mode: standalone)').matches}`,
      `#app top ${a.top.toFixed(0)} h ${a.height.toFixed(0)}   tabbar bottom ${t.bottom.toFixed(0)}   viewport-fit ${/viewport-fit=cover/.test(document.querySelector('meta[name=viewport]').content) ? 'cover' : 'auto'}`,
      `100dvh ${(() => { const e = document.createElement('div'); e.style.cssText = 'position:fixed;height:100dvh;width:0'; document.body.appendChild(e); const h = e.offsetHeight; e.remove(); return h; })()}  100lvh ${(() => { const e = document.createElement('div'); e.style.cssText = 'position:fixed;height:100lvh;width:0'; document.body.appendChild(e); const h = e.offsetHeight; e.remove(); return h; })()}`,
    ].join('\n');
  };
  upd(); debugTimer = setInterval(upd, 800); debugBox = box; debugProbe = probe;
}
bindSwitch($('#swDebug'), (on) => setDebug(on));
try {
  const dbg = /[?&]debug\b/.test(location.search) || localStorage.getItem('debug') === '1';
  setSwitchEl($('#swDebug'), dbg);
  if (dbg) setDebug(true);
} catch (e) {}

/* ---------------------------------------------------------------- lyrics (synced only) */
const lyricsEnabled = () => !!(S.opt.lyrics ?? (S.s && S.s.switches && S.s.switches.lyrics));      // a switch of the speaker itself (its config), the same for every panel
const wideScreen = () => matchMedia('(min-width: 900px)').matches;
S.ly = { key: '', lines: null, cur: -1, show: null };      // show: null = not chosen yet (the PC shows the lyrics, the phone does not), true / false = the button
async function loadLyrics(n) {
  const key = n.artist + '|' + n.title + '|' + n.duration;
  if (S.ly.key === key) return;
  S.ly = { key, lines: null, cur: -1, show: S.ly.show };
  drawLyrics();
  if (!lyricsEnabled() || !n.duration) return;                // needs the length of the track: Spotify, AirPlay and Bluetooth tell it
  try {
    const r = await fetch('/api/v1/lyrics?' + new URLSearchParams({ artist: n.artist, title: n.title, duration: n.duration })).then((x) => x.json());
    if (S.ly.key === key) { S.ly.lines = r.lines; drawLyrics(); }
  } catch (e) { /* no lyrics this time */ }
}
function drawLyrics() {
  const box = $('#pLyIn'), lines = S.ly.lines;
  box.innerHTML = lines ? lines.map((l, i) => `<div class="ly-line${l.text ? '' : ' gap'}" data-i="${i}">${esc(l.text)}</div>`).join('') : '';
  S.ly.cur = -1;
  $('#pLyBtn').hidden = !lines;
  applyLyricsMode();
}
function applyLyricsMode() {
  const on = !!S.ly.lines && lyricsEnabled() && (S.ly.show ?? wideScreen());
  $('.player').classList.toggle('lyr', on);
  $('#sheetPlayer').classList.toggle('wide', wideScreen());                       // the PC player is always the full window
  $('#pLyBtn').classList.toggle('on', on);
  if (on) { S.ly.cur = -1; tickLyrics(); }
}
function tickLyrics() {
  const lines = S.ly.lines;
  if (!lines || !$('.player').classList.contains('lyr') || !S.prog) return;
  const ms = progNow() + 250;                 // the line is lit a moment before it is sung
  let cur = -1;
  for (let i = 0; i < lines.length && lines[i].t <= ms; i++) cur = i;
  if (cur === S.ly.cur) return;
  S.ly.cur = cur;
  const els = $$('#pLyIn .ly-line');
  els.forEach((el, i) => { el.classList.toggle('cur', i === cur); el.classList.toggle('past', i < cur); el.classList.toggle('d1', i === cur + 1); });
  // the line that has just been sung stays on top, the one that sounds now is the second; the very first line sits a little lower
  const box = $('#pLyrics'), prev = els[cur - 1], first = els[0];
  const posOf = (el) => el.getBoundingClientRect().top - box.getBoundingClientRect().top + box.scrollTop;   // offsetTop would count the box's own place in the sheet
  const top = prev ? posOf(prev) - 6 : first ? posOf(first) - box.clientHeight * 0.1 : 0;
  box.scrollTo({ top: Math.max(0, top), behavior: 'smooth' });
}
function updateLyrics(n) {
  $('#pThumb').style.backgroundImage = $('#pCover').style.backgroundImage;
  if (!lyricsEnabled()) { if (S.ly.lines) { S.ly = { key: '', lines: null, cur: -1, show: null }; drawLyrics(); } return; }
  loadLyrics(n);
}
setInterval(tickLyrics, 300);
$('#pLyBtn').addEventListener('click', () => { S.ly.show = !$('.player').classList.contains('lyr'); applyLyricsMode(); });
$('#pLyIn').addEventListener('click', (e) => {            // a tap on a line jumps to it (Spotify)
  const el = e.target.closest('.ly-line');
  if (!el || !S.prog || !S.prog.seek) return;
  const ms = S.ly.lines[+el.dataset.i].t;
  S.prog = Object.assign({}, S.prog, { ms, at: Date.now() });
  api('seek', { ms });
});
addEventListener('resize', applyLyricsMode);
applyLyricsMode();
const swLy = $('#swLyrics');
bindSwitch(swLy, (on) => {
  S.opt.lyrics = on; setTimeout(() => { delete S.opt.lyrics; }, 3000);
  api('switch', { id: 'lyrics', on });
  $('#uLyricsSub').textContent = on ? 'тексты включены' : 'тексты выключены';
  S.ly = { key: '', lines: null, cur: -1, show: null };
  if (!on) drawLyrics(); else if (S.s && S.s.now) loadLyrics(S.s.now);
});
bindSwitch($('#swCovers'), (on) => { S.opt.covers = on; setTimeout(() => { delete S.opt.covers; }, 3000); api('switch', { id: 'cover_lookup', on }); });
function syncLyricsSwitch() {      // the state of the speaker's switch into the page
  const on = lyricsEnabled();
  setSwitchEl(swLy, on);
  $('#uLyricsSub').textContent = on ? 'тексты включены' : 'тексты выключены';
  setSwitchEl($('#swCovers'), S.opt.covers ?? !!(S.s && S.s.switches && S.s.switches.cover_lookup));
}

/* ---------------------------------------------------------------- equalizer (ten bands, kept in the speaker's eq.conf) */
const EQ_HZ = ['31', '62', '125', '250', '500', '1k', '2k', '4k', '8k', '16k'];
const EQ_PRESETS = {
  'Плоский': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0], 'Бас': [6, 5, 4, 2, 0, 0, 0, 0, 0, 0], 'Вокал': [-2, -1, 0, 2, 3, 3, 2, 0, -1, -2],
  'Высокие': [0, 0, 0, 0, 0, 1, 2, 4, 5, 6], 'Рок': [4, 3, 2, 0, -1, -1, 0, 2, 3, 4], 'Кино': [5, 4, 2, 0, 0, 0, 1, 2, 3, 3],
  'Речь': [-4, -3, -1, 1, 3, 4, 3, 1, -2, -4], 'Вечер': [3, 2, 1, 0, 0, 0, 0, 1, 2, 3],
};
S.eq = { on: false, gains: new Array(10).fill(0), preamp: 0, hold: 0 };
const eqPreamp = (g) => -Math.round(Math.max(0, ...g) * 0.8);           // headroom: boosted bands lower the whole signal
function eqSend() {
  S.eq.hold = Date.now() + 2500;
  api('eq', { on: S.eq.on, preamp: S.eq.preamp, gains: S.eq.gains });
}
function eqPaint() {
  $('#swEq').classList.toggle('on', S.eq.on);
  $('#eqCard').classList.toggle('off', !S.eq.on);
  const name = Object.keys(EQ_PRESETS).find((n) => EQ_PRESETS[n].every((v, i) => Math.abs(v - S.eq.gains[i]) < 0.6));
  $('#eqPreset').textContent = name || 'Свой';
  $$('#eqBands .eqb').forEach((el, i) => {
    const g = S.eq.gains[i];
    $('.db', el).textContent = (g > 0 ? '+' : '') + Math.round(g);
    const knob = $('.knob', el); knob.style.top = (50 - g / 15 * 46) + '%'; knob.classList.toggle('on', S.eq.on);
  });
}
(() => {
  const host = $('#eqBands');
  host.innerHTML = EQ_HZ.map((f) => `<div class="eqb"><div class="db">0</div><div class="trk"><i class="mid"></i><i class="knob"></i></div><div class="hz">${f}</div></div>`).join('');
  $$('.eqb', host).forEach((el, i) => {
    const trk = $('.trk', el); let drag = false;
    const set = (e) => {
      const r = trk.getBoundingClientRect(), f = clamp((e.clientY - r.top) / r.height, 0, 1);
      S.eq.gains[i] = Math.round((0.5 - f) * 2 * 15 * 10 / 10); S.eq.preamp = eqPreamp(S.eq.gains); eqPaint();
    };
    trk.addEventListener('pointerdown', (e) => { drag = true; trk.setPointerCapture(e.pointerId); S.eq.hold = Date.now() + 60000; set(e); });
    trk.addEventListener('pointermove', (e) => { if (drag) set(e); });
    const end = () => { if (drag) { drag = false; eqSend(); } };
    trk.addEventListener('pointerup', end); trk.addEventListener('pointercancel', end);
  });
  bindSwitch($('#swEq'), (on) => { S.eq.on = on; eqPaint(); eqSend(); });
  $('#rowEqPreset').addEventListener('click', () => menu($('#rowEqPreset'), Object.keys(EQ_PRESETS).map((n) => ({ t: n, v: n })), (n) => {
    if (!n) return; S.eq.gains = EQ_PRESETS[n].slice(); S.eq.preamp = eqPreamp(S.eq.gains); S.eq.on = n !== 'Плоский' ? true : S.eq.on; eqPaint(); eqSend();
  }));
})();
function syncEq(s) {            // the speaker's own state into the page (not while a finger is on a slider or just after a change)
  if (!s.eq || !s.eq.known || Date.now() < S.eq.hold) return;
  S.eq.on = !!s.eq.on; S.eq.gains = s.eq.gains.slice(); S.eq.preamp = s.eq.preamp; eqPaint();
}

/* ---------------------------------------------------------------- plugins: the tiles of "Сейчас" and the plugin list */
const TILE_ICONS = { clock: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>', sun: '<circle cx="12" cy="12" r="4"/><path d="M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6l1.4 1.4M17 17l1.4 1.4M5.6 18.4L7 17M17 7l1.4-1.4"/>',
  cloud: '<path d="M7 18a4 4 0 0 1-.6-8A5.5 5.5 0 0 1 17 8.7 4.7 4.7 0 0 1 17 18z"/>', music: '<path d="M9 18V5l10-2v13"/><circle cx="6.5" cy="18" r="2.5"/><circle cx="16.5" cy="16" r="2.5"/>',
  chart: '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>', bolt: '<path d="M13 2L5 14h6l-1 8 8-12h-6z"/>', bell: '<path d="M6 17V11a6 6 0 0 1 12 0v6l1.5 2h-15zM10 21h4"/>',
  home: '<path d="M4 11l8-7 8 7v9H4z"/>', star: '<path d="M12 3l2.7 5.6 6.1.8-4.5 4.2 1.1 6-5.4-3-5.4 3 1.1-6-4.5-4.2 6.1-.8z"/>', clock: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7v5l3 2"/>',
  note: '<path d="M6 3h9l4 4v14H6zM9 12h6M9 16h6"/>',
  airplay: '<path d="M5.5 17.5A8.5 8.5 0 1 1 18.5 17.5M8.6 14.6A4.6 4.6 0 1 1 15.4 14.6"/><path d="M12 14.5l4 5.5H8z" class="fill"/>',
  cpu: '<rect x="7" y="7" width="10" height="10" rx="2"/><path d="M10 3v4M14 3v4M10 17v4M14 17v4M3 10h4M3 14h4M17 10h4M17 14h4"/>',
  memory: '<rect x="3" y="7.5" width="18" height="9" rx="1.5"/><path d="M7 7.5v9M11 7.5v9M15 7.5v9M6 16.5V19M10 16.5V19M14 16.5V19M18 16.5V19"/>',
  disk: '<ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
  spotify: '<circle cx="12" cy="12" r="9.5"/><path d="M7 9.2c3.4-1 7.2-.7 10.2 1M7.6 12.4c2.8-.8 5.8-.5 8.3.9M8.2 15.4c2.2-.6 4.4-.4 6.4.6"/>' };
const tileSvg = (n) => `<svg viewBox="0 0 24 24">${TILE_ICONS[n] || TILE_ICONS.star}</svg>`;
/* The tiles of the home page belong to plugins: a plugin that is on shows its tile, one that is off or removed shows none. What lostOS draws by
   itself (the player, the alarm, Bluetooth, "Что она слышала") are tiles of the system and can be put away and moved in the same way.
   The edit mode is in the manner of the widgets of iOS: a tile is picked up by HOLDING it (a swipe over it only scrolls), the tiles shake, can be put
   away (−) and moved by dragging, and a tile that used to be there stays as a dashed place holder that says whose it was and why it is gone. */
const TILE_SHELVES = { now: '#shelfNow', sources: '#shelfSrc' };
const TILE_TITLES = { now: '#titleNow', sources: '#titleSrc' };
const tileIc = (name, cls) => `<svg${cls ? ` class="${cls}"` : ''} viewBox="0 0 24 24">${TILE_ICONS[name] || TILE_ICONS.star}</svg>`;
const tileEl = (id) => document.querySelector(`[data-tile="${CSS.escape(id)}"]`);
/* The weather widget: the sky of the weather behind the numbers (weather_fx.js), the weather now and the next three parts of the day.
   The signs of the weather are drawn here, in colour. */
const WX = {
  sun: '<g stroke="#ffb020" stroke-width="1.9" stroke-linecap="round"><path d="M12 2.4v2.2M12 19.4v2.2M2.4 12h2.2M19.4 12h2.2M5.2 5.2l1.6 1.6M17.2 17.2l1.6 1.6M5.2 18.8l1.6-1.6M17.2 6.8l1.6-1.6"/></g><circle cx="12" cy="12" r="4.6" fill="#ffc233"/>',
  moon: '<path d="M20.2 14.6A8.6 8.6 0 1 1 9.4 3.8a6.9 6.9 0 0 0 10.8 10.8z" fill="#ffc233"/>',
  cloud: '<path d="M7.4 19a4.6 4.6 0 0 1-.6-9.2 6.1 6.1 0 0 1 11.7 1.7A3.8 3.8 0 0 1 18 19z" fill="#f4f7fb"/>',
  cloudGrey: '<path d="M7.4 19a4.6 4.6 0 0 1-.6-9.2 6.1 6.1 0 0 1 11.7 1.7A3.8 3.8 0 0 1 18 19z" fill="#c3cddc"/>',
  cloudDark: '<path d="M7.4 19a4.6 4.6 0 0 1-.6-9.2 6.1 6.1 0 0 1 11.7 1.7A3.8 3.8 0 0 1 18 19z" fill="#8f9bb3"/>',
};
const wxG = (inner, tr) => `<g transform="${tr}">${inner}</g>`;
WX.partly = wxG(WX.sun, 'translate(0 0) scale(.6)') + wxG(WX.cloud, 'translate(8.2 6.4) scale(.68)');
WX.partlynight = wxG(WX.moon, 'translate(.4 .2) scale(.56)') + wxG(WX.cloud, 'translate(8.2 6.4) scale(.68)');
WX.rain = wxG(WX.cloudGrey, 'translate(0 -3.4) scale(.92)') + '<path d="M8.4 19.2l-1 2.4M12.4 19.2l-1 2.4M16.4 19.2l-1 2.4" fill="none" stroke="#7cc4ff" stroke-width="1.9" stroke-linecap="round"/>';
WX.snow = wxG(WX.cloudGrey, 'translate(0 -3.4) scale(.92)') + '<g fill="#fff"><circle cx="8" cy="20" r="1.4"/><circle cx="12" cy="21.6" r="1.4"/><circle cx="16" cy="20" r="1.4"/></g>';
WX.storm = wxG(WX.cloudDark, 'translate(0 -3.4) scale(.92)') + '<path d="M12.9 15l-3.2 4.4h2.7l-1.1 3.2 4.3-5.1h-2.7z" fill="#ffd23f"/>';
WX.fog = '<path d="M4 9h16M3 13h18M5 17h14" fill="none" stroke="#e8e6e1" stroke-width="2.2" stroke-linecap="round"/>';
const wxIcon = (kind, night, cls) => `<svg class="${cls}" viewBox="0 0 24 24">${kind === 'clear' ? (night ? WX.moon : WX.sun) : kind === 'partly' ? (night ? WX.partlynight : WX.partly) : WX[kind] || WX.cloud}</svg>`;
function weatherHtml(t, sig) {
  const w = t.weather, sk = window.lostWeatherFx.sky(w.kind, w.night);
  const parts = (w.parts || []).map((p) => `<div class="wx-part"><span>${esc(p.name)}</span>${wxIcon(p.kind, p.night, 'wx-pi')}<b>${p.temp > 0 ? '+' : ''}${p.temp}°</b></div>`).join('');
  return `<article class="card wx-card${t.hidden ? ' hidden-tile' : ''}" data-tile="${esc(t.id)}" data-shelf="${esc(t.shelf)}" data-sig="${sig}" style="--sky1:${sk[0]};--sky2:${sk[1]}">
    <div class="wx-bg" data-kind="${esc(w.kind)}" data-night="${w.night ? 1 : 0}"><canvas></canvas></div>
    <div class="wx-top"><span class="wx-city">${esc(w.city)}<svg viewBox="0 0 24 24" fill="currentColor"><path d="M20.5 3.5L3.8 10.6l6.5 2.6 2.6 6.5z"/></svg></span></div>
    <div class="wx-main"><span class="wx-temp">${w.temp > 0 ? '+' : ''}${w.temp}°</span>${wxIcon(w.kind, w.night, 'wx-ic')}</div>
    <div class="wx-parts">${parts}</div></article>`;
}
/* ---- the clock and the 3D model of the speaker: they repeat what the speaker shows (the time or its text, the light of the assistant) */
const SEG7 = { 0: 'abcdef', 1: 'bc', 2: 'abdeg', 3: 'abcdg', 4: 'bcfg', 5: 'acdfg', 6: 'acdefg', 7: 'abc', 8: 'abcdefg', 9: 'abcdfg', '-': 'g', '_': 'd', ' ': '',
  A: 'abcefg', B: 'cdefg', C: 'adef', D: 'bcdeg', E: 'adefg', F: 'aefg', H: 'bcefg', L: 'def', N: 'ceg', O: 'abcdef', P: 'abefg', R: 'eg', S: 'acdfg', T: 'defg', U: 'bcdef', '°': 'abfg' };
/* The widget repeats the real display: the speaker tells how every segment is lit (the sensor display_frame: 28 hex digits for 4 digits x segments a..g, then the colon and the
   degree/alarm LED). Without it (an older speaker) the time or the text of the server is drawn instead. */
const SEG_R = { a: [7, 0, 30, 7], b: [37, 7, 7, 30], c: [37, 43, 7, 30], d: [7, 73, 30, 7], e: [0, 43, 7, 30], f: [0, 7, 7, 30], g: [7, 36.5, 30, 7] };
function clockSvg() {
  const X = [0, 58, 138, 196];
  const rects = X.map((x) => Object.values(SEG_R).map(([rx, ry, w, h]) => `<rect x="${x + rx}" y="${ry}" width="${w}" height="${h}" rx="3.5"/>`).join('')).join('');
  return `<svg class="clk" viewBox="0 0 252 80" aria-hidden="true">${rects}<circle cx="124" cy="26" r="4.4"/><circle cx="124" cy="54" r="4.4"/><circle cx="246" cy="6" r="3.2"/></svg>`;
}
function frameFromText(text) {                     // the same 30 digits for a text: lit segments full, the others dark
  const digits = [], src = String(text || '').toUpperCase(); let colon = false;
  for (const ch of src) { if (ch === ':') { colon = true; continue; } if (digits.length < 4) digits.push(SEG7[ch] ?? ''); }
  while (digits.length < 4) digits.push('');
  return digits.map((d) => 'abcdefg'.split('').map((k) => (d.includes(k) ? 'f' : '0')).join('')).join('') + (colon ? 'f' : '0') + '0';
}
/* The speaker's frames come a few times faster than the eye needs but not evenly: every segment glides to its new level (exponential smoothing, about 60 ms) on every screen frame. */
const CK = { cur: new Array(31).fill(0), tgt: new Array(31).fill(0), raf: 0, last: 0 };
function clockPaint(svg, f) {                       // sets the targets; the glide does the drawing
  for (let i = 0; i < 31; i++) CK.tgt[i] = parseInt(f[i === 28 || i === 29 ? 28 : i === 30 ? 29 : i] || '0', 16) / 15;
  clockKick();
}
function clockKick() { if (!CK.raf) CK.raf = requestAnimationFrame(clockGlide); }
function clockGlide(now) {
  CK.raf = 0;
  const dt = Math.min(0.1, (now - (CK.last || now)) / 1000); CK.last = now;
  const k = 1 - Math.exp(-dt / 0.06);
  let moving = false;
  for (let i = 0; i < 31; i++) {
    const d = CK.tgt[i] - CK.cur[i];
    if (Math.abs(d) > 0.004) { CK.cur[i] += d * k; moving = true; } else CK.cur[i] = CK.tgt[i];
  }
  $$('.clk-card .clk').forEach((svg) => {
    const els = svg._els || (svg._els = [...svg.children]);
    for (let i = 0; i < els.length && i < 31; i++) {
      const v = CK.cur[i];
      els[i].style.opacity = (0.07 + 0.93 * v).toFixed(3);
      els[i].style.filter = v > 0.4 ? 'drop-shadow(0 0 4px rgba(255,255,255,' + (0.55 * v).toFixed(2) + '))' : '';
    }
  });
  if (moving) CK.raf = requestAnimationFrame(clockGlide); else CK.last = 0;
}
const clockNow = () => { const d = new Date(Date.now() + (S.skew || 0)); return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0'); };   // a steady colon, like the speaker's own
function clockFrame() { return S.disp && S.disp.f ? S.disp.f : frameFromText((S.mirror && S.mirror.text) || clockNow()); }
function clockHtml(t, sig) {
  return `<article class="card clk-card${t.hidden ? ' hidden-tile' : ''}" data-tile="${esc(t.id)}" data-shelf="${esc(t.shelf)}" data-sig="${sig}"><div class="clk-box">${clockSvg()}</div></article>`;
}
function mirrorUpdate() {
  const f = clockFrame();
  $$('.clk-card .clk').forEach((svg) => clockPaint(svg, f));
}
let dispAbort = null;
async function dispStream() {                       // frames pushed by the server; when the stream breaks it is opened again
  if (dispAbort) return;
  dispAbort = new AbortController();
  const mine = dispAbort;
  try {
    const r = await fetch('/api/v1/display/stream', { signal: mine.signal });
    if (!r.ok || !r.body) throw new Error('no stream');
    const rd = r.body.getReader(), dec = new TextDecoder(); let buf = '';
    for (;;) {
      const { value, done } = await rd.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let k;
      while ((k = buf.indexOf('\n\n')) >= 0) {
        const line = buf.slice(0, k); buf = buf.slice(k + 2);
        if (line.startsWith('data:')) { const f = line.slice(5).trim(); S.disp = f ? { f, on: true } : null; mirrorUpdate(); }
      }
    }
  } catch (e) { /* closed or offline: tried again below */ }
  if (dispAbort === mine) dispAbort = null;
}
function dispWatch() {                              // the stream runs only while a clock widget is on the page and the tab is open
  const want = !document.hidden && !!document.querySelector('.clk-card');
  if (want && !dispAbort) dispStream();
  else if (!want && dispAbort) { dispAbort.abort(); dispAbort = null; }
  if (document.querySelector('.clk-card')) mirrorUpdate();            // the time itself moves on even without frames (an older speaker)
}
setInterval(dispWatch, 1000);
document.addEventListener('visibilitychange', dispWatch);
function hwHtml(t, sig) {
  const h = t.hw, mb = (kb) => Math.round(kb / 1024);
  const row = (icon, name, frac, text) => `<div class="hw-row">${tileIc(icon)}<div class="hw-m"><span>${name}</span><div class="tc-bar"><i style="width:${Math.round(Math.max(0, Math.min(1, frac)) * 100)}%"></i></div></div><b>${text}</b></div>`;
  return `<article class="card hw-card${t.hidden ? ' hidden-tile' : ''}" data-tile="${esc(t.id)}" data-shelf="${esc(t.shelf)}" data-sig="${sig}">
    <div class="hw-top">Железо колонки</div>
    ${row('cpu', 'Процессор', h.cpu / 100, Math.round(h.cpu) + '%' + (h.temp != null ? ' · ' + Math.round(h.temp) + '°' : ''))}
    ${row('memory', 'Память', h.mem_used / h.mem_total, mb(h.mem_used) + ' / ' + mb(h.mem_total) + ' МБ')}
    ${row('disk', 'Накопитель', 1 - h.disk_free / h.disk_total, mb(h.disk_free) + ' МБ свободно')}</article>`;
}
function tileHtml(t, sig) {
  if (t.kind === 'weather' && t.weather) return weatherHtml(t, sig);
  if (t.kind === 'hw' && t.hw) return hwHtml(t, sig);
  if (t.kind === 'clock') return clockHtml(t, sig);
  const accent = esc(/^#[0-9a-f]{3,8}$/i.test(t.accent) ? t.accent : '#3b82f6');
  const live = t.live ? (S.tileStatus || {})[t.id] || '' : '';
  if (t.shelf === 'sources') return `<article class="card src-card tint-${esc(t.tint || 'air')}" data-tile="${esc(t.id)}" data-shelf="${esc(t.shelf)}" data-sig="${sig}">${tileIc(t.icon, 'big-ic')}<div class="card-text"><b>${esc(t.title)}</b><span class="tile-live">${esc(live)}</span></div>${t.switch ? `<button class="switch${S.airOn ? ' on' : ''}" data-tswitch="${esc(t.switch)}" aria-label="${esc(t.title)}: приёмник"><i></i></button>` : ''}</article>`;
  return `<article class="card tile-card" data-tile="${esc(t.id)}" data-shelf="${esc(t.shelf)}" data-sig="${sig}" style="--tile:${accent}"><div class="tc-top">${tileIc(t.icon)}<span>${esc(t.title)}</span></div>
    <div class="tc-val">${esc(t.value || '')}</div>${t.bar != null ? `<div class="tc-bar"><i style="width:${Math.round(Math.max(0, Math.min(1, t.bar)) * 100)}%"></i></div>` : ''}<div class="tc-sub">${esc(t.subtitle || '')}</div></article>`;
}
function ghostHtml(g) {
  return `<article class="card ghost${g.shelf === 'sources' ? ' src-card' : ''}" data-ghost="${esc(g.id)}" data-shelf="${esc(g.shelf)}">${tileIc(g.icon, 'big-ic')}
    <div class="card-text"><b>${esc(g.title)}</b><span>Виджет плагина «${esc(g.plugin)}»</span><span class="why">${esc(g.reason)}</span></div>
    <button class="tbadge minus" data-tforget="${esc(g.id)}" aria-label="Забыть">×</button></article>`;
}
/* On the PC a widget that is cut off at the edge of a shelf shows a light blur on that side; where nothing is hidden there is none. */
function shelfEdges() {
  for (const sel of Object.values(TILE_SHELVES)) {
    const shelf = $(sel);
    let wrap = shelf.parentElement;
    if (!wrap.classList.contains('shelf-wrap')) {
      wrap = document.createElement('div'); wrap.className = 'shelf-wrap';
      shelf.parentElement.insertBefore(wrap, shelf); wrap.appendChild(shelf);
      wrap.insertAdjacentHTML('beforeend', '<i class="edge l"></i><i class="edge r"></i>');
      shelf.addEventListener('scroll', () => paintEdges(shelf), { passive: true });
      new ResizeObserver(() => paintEdges(shelf)).observe(shelf);
    }
    paintEdges(shelf);
  }
}
function paintEdges(shelf) {
  // the blur is only over the margin of the shelf (where a widget that is cut off shows), and only while a widget really stands in it
  const wrap = shelf.parentElement, box = shelf.getBoundingClientRect(), pad = parseFloat(getComputedStyle(shelf).paddingLeft) || 0;
  let l = false, r = false;
  for (const c of shelf.children) {
    const t = c.getBoundingClientRect();
    if (t.width && t.right > box.left + 1 && t.left < box.left + pad - 1) l = true;
    if (t.width && t.left < box.right - 1 && t.right > box.right - pad + 1) r = true;
  }
  wrap.classList.toggle('cut-l', l);
  wrap.classList.toggle('cut-r', r);
}
function renderTiles() {
  const d = S.tilesData || { tiles: [], ghosts: [] }, edit = !!S.editHome;
  // A tile whose content has not changed stays as it is (the weather keeps its moving sky); the others and the place holders are drawn anew.
  const sigOf = (t) => { const s = JSON.stringify({ ...t, hidden: 0 }); let h = 5381; for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) | 0; return String(h >>> 0); };
  const kept = new Map($$('.shelf [data-tile]:not([data-static])').map((e) => [e.dataset.tile, e]));
  $$('[data-ghost]').forEach((c) => c.remove());
  for (const [shelf, sel] of Object.entries(TILE_SHELVES)) {
    const box = $(sel), own = d.tiles.filter((t) => t.shelf === shelf);
    for (const t of own.filter((x) => !x.static)) {
      const sig = sigOf(t), old = kept.get(t.id);
      if (old && old.dataset.sig === sig) { kept.delete(t.id); box.appendChild(old); continue; }
      if (old) { old.remove(); kept.delete(t.id); }
      box.insertAdjacentHTML('beforeend', tileHtml(t, sig));
      if (t.kind === 'weather') window.lostWeatherFx.start(box.lastElementChild);
    }
    if (edit) box.insertAdjacentHTML('beforeend', d.ghosts.filter((g) => g.shelf === shelf).map(ghostHtml).join(''));
    const rank = (el) => { const i = own.findIndex((t) => t.id === el.dataset.tile); return i < 0 ? 1e6 : i; };
    $$(':scope > [data-tile]', box).sort((a, b) => rank(a) - rank(b)).forEach((el) => box.appendChild(el));       // the order the owner chose
    $$(':scope > [data-ghost]', box).forEach((g) => box.appendChild(g));
  }
  kept.forEach((e) => e.remove());                                                          // the tiles that are gone
  for (const t of d.tiles) { const el = tileEl(t.id); if (el) el.classList.toggle('hidden-tile', !!t.hidden); }
  for (const shelf of Object.keys(TILE_SHELVES)) {                                          // a shelf with nothing left on it goes away with its title
    const box = $(TILE_SHELVES[shelf]), empty = !$$(':scope > [data-tile]:not(.hidden-tile), :scope > [data-ghost]', box).length;
    box.classList.toggle('empty-shelf', empty); $(TILE_TITLES[shelf]).classList.toggle('empty-shelf', empty);
  }
  $$('[data-tile] > .tbadge, [data-tile] > .tile-label').forEach((e) => e.remove());
  $$('[data-tile]').forEach((el) => {                                                        // the caption under each widget; in the edit mode −/+ and whose widget it is
    const t = d.tiles.find((x) => x.id === el.dataset.tile);
    if (!t) return;
    if (edit) el.insertAdjacentHTML('beforeend', `<button class="tbadge minus" data-tbadge="${esc(t.id)}" aria-label="Убрать">−</button>`);
    if (t.shelf !== 'page') el.insertAdjacentHTML('beforeend', `<span class="tile-label">${esc(t.label || t.title)}${edit && t.plugin ? `<small> · ${esc(t.plugin)}</small>` : ''}</span>`);
  });
  shelfEdges();
  $('#p-home').classList.toggle('editing', edit);
  $('#btnHomeEdit').textContent = edit ? 'Готово' : 'Изменить';
  $('#btnHomeAdd').hidden = !edit;
}
async function loadTiles(force) {
  if (S.editHome && !force) return;                         // the page does not change under the finger of the owner
  let d = null;
  try { d = await fetch('/api/v1/tiles' + (location.search.includes('wx=') ? '?' + new URLSearchParams(location.search).toString() : '')).then((r) => r.json()); } catch (e) { /* the server is not there: the page stays as it is */ }
  if (d && d.tiles) {
    const sig = JSON.stringify(d);
    if (S.tilesSig !== sig || force) { S.tilesSig = sig; S.tilesData = d; renderTiles(); }
  }
  markReady('tiles');
}
function applyTileStatus(st) {        // the live line of the tiles that plugins keep up to date: only the text changes
  S.tileStatus = st || {};
  $$('[data-tile] .tile-live').forEach((el) => { el.textContent = S.tileStatus[el.closest('[data-tile]').dataset.tile] || ''; });
}
function saveLayout() {
  const d = S.tilesData, order = {};
  if (!d) return;
  for (const shelf of Object.keys(TILE_SHELVES)) order[shelf] = d.tiles.filter((t) => t.shelf === shelf).map((t) => t.id);
  fetch('/api/v1/tiles/layout', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ hidden: d.tiles.filter((t) => t.hidden).map((t) => t.id), order }) });
}
function setEditHome(on) {
  S.editHome = on;
  if (!on) saveLayout();
  renderTiles();
  if (!on) { S.tilesSig = ''; loadTiles(true); }
}
$('#btnHomeEdit').addEventListener('click', () => setEditHome(!S.editHome));
// a tap on an empty place of the home page ends the edit mode
$('#p-home').addEventListener('click', (ev) => {
  if (S.editHome && !S.tileSwallow && !ev.target.closest('[data-tile], [data-ghost], .home-edit-row')) setEditHome(false);
});

/* ---------------------------------------------------------------- the gallery of widgets (the panel of all widgets, in the manner of iOS) */
const WG_GROUPS = [['info', 'Информативные виджеты'], ['control', 'Пункт управления']];
function previewHtml(t) {
  if (t.kind === 'weather' || t.kind === 'hw' || !t.static) return tileHtml({ ...t, hidden: false }, 'p').replace(/ data-(tile|sig|shelf)="[^"]*"/g, '');   // a preview is no tile of the page
  const generic = { 'sys.now': ['now-card', 'music'], 'sys.alarm': ['tint-alarm', 'clock'], 'sys.bluetooth': ['src-card tint-bt', 'note'], 'sys.dialogs': ['tile-card', 'note'] }[t.id] || ['tile-card', 'star'];
  return `<article class="card ${generic[0]} wg-generic">${tileIc(generic[1], 'big-ic')}<b>${esc(t.label || t.title)}</b></article>`;
}
function renderWidgetGallery() {
  const q = ($('#wgSearch').value || '').trim().toLowerCase(), d = S.tilesData || { tiles: [] };
  const list = d.tiles.filter((t) => !q || ((t.label || '') + ' ' + (t.title || '') + ' ' + (t.about || '') + ' ' + (t.plugin || '')).toLowerCase().includes(q));
  $('#wgList').innerHTML = WG_GROUPS.map(([g, title]) => {
    const own = list.filter((t) => (t.group || 'info') === g);
    if (!own.length) return '';
    return `<div class="cap">${title}</div><div class="wg-grid">${own.map((t) => `<div class="wg-item${t.shelf === 'sources' && !t.static ? ' small' : ''}" data-wg="${esc(t.id)}">
      <div class="wg-prev ${t.shelf === 'sources' && t.id !== 'sys.bluetooth' ? 'src' : ''}">${previewHtml(t)}</div>
      <b>${esc(t.label || t.title)}</b><span>${esc(t.about || '')}</span>
      <button class="mini-btn ${t.hidden ? 'acc' : ''}" data-wgadd="${esc(t.id)}">${t.hidden ? 'Добавить' : 'На главной ✓'}</button></div>`).join('')}</div>`;
  }).join('') || '<div class="empty">Ничего не найдено</div>';
}
$('#btnHomeAdd').addEventListener('click', () => { $('#wgSearch').value = ''; renderWidgetGallery(); openSheet($('#sheetWidgets')); });
$('#wgSearch').addEventListener('input', renderWidgetGallery);
$('#wgList').addEventListener('click', (ev) => {
  const b = ev.target.closest('[data-wgadd]');
  if (!b) return;
  const t = S.tilesData.tiles.find((x) => x.id === b.dataset.wgadd);
  if (!t) return;
  t.hidden = !t.hidden;                                                                      // add or take away; the layout is saved when the edit mode ends
  if (!t.hidden) { S.tilesData.tiles = S.tilesData.tiles.filter((x) => x !== t).concat(t); }   // a widget that is added goes to the end of its shelf
  renderTiles(); renderWidgetGallery();
});
document.addEventListener('click', async (ev) => {
  const sw = ev.target.closest('[data-tswitch]');
  if (sw && !S.editHome) {                                          // the switch of the receiver on a tile: the plugin stays on, the receiver stops or starts
    ev.stopPropagation();
    const on = !sw.classList.contains('on');
    sw.classList.toggle('on', on); S.airOn = on; S.airHold = Date.now() + 4500;
    api('airplay', { on });
    return;
  }
  const b = ev.target.closest('[data-tbadge]'), f = ev.target.closest('[data-tforget]'), card = ev.target.closest('[data-tile]');
  if (b) {
    const t = S.tilesData.tiles.find((x) => x.id === b.dataset.tbadge);
    if (t) { t.hidden = !t.hidden; renderTiles(); }
  } else if (f) {
    await fetch('/api/v1/tiles/' + encodeURIComponent(f.dataset.tforget), { method: 'DELETE' });
    S.tilesData.ghosts = S.tilesData.ghosts.filter((g) => g.id !== f.dataset.tforget); renderTiles();
  } else if (card && !S.editHome) {                                // a tap on the tile of a plugin opens the page of that plugin; the system ones and the weather open nothing
    const t = (S.tilesData && S.tilesData.tiles || []).find((x) => x.id === card.dataset.tile);
    if (!t || t.static || t.plugin_id === 'lostos') return;
    pushSub('plugins'); await loadPlugins(); openPlugin(t.plugin_id);
  }
});
// in the edit mode (and just after the hold) a tile does nothing of its own: no player opens, no switch flips
document.addEventListener('click', (ev) => {
  if ((S.editHome || S.tileSwallow) && ev.target.closest('[data-tile]') && !ev.target.closest('[data-tbadge]')) { ev.stopPropagation(); ev.preventDefault(); }
}, true);
document.addEventListener('contextmenu', (ev) => { if (ev.target.closest('[data-tile]')) ev.preventDefault(); });
/* A tile is picked up by holding it: a finger that moves before the hold is a scroll and nothing else happens (no highlight, no shaking)
   The hold opens the edit mode (if it is not open yet) with this tile lifted; then it follows the finger along its shelf. */
(() => {
  const HOLD_MS = 380, SLOP = 9;
  let press = null, drag = null;
  const cancelPress = () => { if (press) { clearTimeout(press.timer); press = null; } };
  const lift = () => {
    const p = press; press = null;
    S.tileSwallow = true;
    if (navigator.vibrate) navigator.vibrate(12);
    if (!S.editHome) setEditHome(true);
    const card = tileEl(p.id);
    if (!card) return;
    card.classList.add('lifted');
    drag = { card, shelf: card.parentElement.classList.contains('shelf') ? card.parentElement : null, x: p.x, grab: p.x - card.getBoundingClientRect().left, live: false };
  };
  document.addEventListener('pointerdown', (e) => {
    const card = e.target.closest('[data-tile]');
    if (!card || e.target.closest('[data-tbadge]') || e.target.closest('.switch') && !S.editHome) return;
    if (e.pointerType === 'mouse' && e.button !== 0) return;
    cancelPress();
    press = { id: card.dataset.tile, x: e.clientX, y: e.clientY, timer: setTimeout(lift, HOLD_MS) };
  });
  document.addEventListener('pointermove', (e) => {
    if (press && Math.hypot(e.clientX - press.x, e.clientY - press.y) > SLOP) cancelPress();      // it is a swipe: the page scrolls
    if (!drag || !drag.shelf) return;
    const { card, shelf } = drag;
    if (!drag.live && Math.abs(e.clientX - drag.x) < 6) return;
    if (!drag.live) { drag.live = true; card.classList.add('dragging'); try { card.setPointerCapture(e.pointerId); } catch (err) { /* a finger always has a pointer to hold */ } }
    const sr = shelf.getBoundingClientRect();
    if (e.clientX < sr.left + 40) shelf.scrollLeft -= 14; else if (e.clientX > sr.right - 40) shelf.scrollLeft += 14;
    const follow = () => { card.style.transform = ''; card.style.transform = `translateX(${e.clientX - drag.grab - card.getBoundingClientRect().left}px) scale(1.06)`; };
    follow();
    const over = $$(':scope > [data-tile]', shelf).find((o) => { if (o === card) return false; const r = o.getBoundingClientRect(); return e.clientX > r.left && e.clientX < r.right; });
    if (over) {                                              // it moves to the place of the tile under the finger
      const r = over.getBoundingClientRect();
      shelf.insertBefore(card, e.clientX > r.left + r.width / 2 ? over.nextSibling : over);
      follow();
    }
  });
  // once a tile is held the finger belongs to it: the page must not scroll under it
  document.addEventListener('touchmove', (e) => { if (drag) e.preventDefault(); }, { passive: false });
  const end = () => {
    cancelPress();
    if (drag) {
      const { card, shelf } = drag;
      card.classList.remove('lifted', 'dragging'); card.style.transform = '';
      if (drag.live && shelf) {                                // the new order of this shelf
        const d = S.tilesData, name = card.dataset.shelf, own = $$(':scope > [data-tile]', shelf).map((c) => d.tiles.find((t) => t.id === c.dataset.tile)).filter(Boolean);
        d.tiles = d.tiles.filter((t) => t.shelf !== name).concat(own);
        saveLayout();
      }
      drag = null;
    }
    if (S.tileSwallow) setTimeout(() => { S.tileSwallow = false; }, 350);                // the click that ends the hold is not a tap
  };
  document.addEventListener('pointerup', end); document.addEventListener('pointercancel', end);
})();
loadTiles(); setInterval(loadTiles, 10000);
/* the system plugins can be folded away in the list or hidden (the setting is kept on this device) */
let sysShow = true, sysFold = false;
try { sysShow = localStorage.getItem('sysShow') !== '0'; sysFold = localStorage.getItem('sysFold') === '1'; } catch (e) {}
function applySysView() {
  $('#capSys').hidden = !sysShow;
  $('#plugSys').hidden = !sysShow || sysFold;
  $('#capSys').classList.toggle('folded', sysFold);
}
$('#capSys').addEventListener('click', () => { sysFold = !sysFold; try { localStorage.setItem('sysFold', sysFold ? '1' : ''); } catch (e) {} applySysView(); });
setSwitchEl($('#swSysShow'), sysShow);
bindSwitch($('#swSysShow'), (on) => { sysShow = on; try { localStorage.setItem('sysShow', on ? '1' : '0'); } catch (e) {} applySysView(); });
const SYS_OFF = {
  'ru.lostos.llm': ['Выключить мозг lostOS?', 'Вопросы перестанут уходить к Gemini и Claude. Ассистент ответит, что мозг выключен, пока вы его не включите или не поставите свой мозг'],
  'ru.lostos.airplay': ['Выключить AirPlay 2?', 'Колонка пропадёт из списка AirPlay на iPhone, iPad и Mac, а то, что играет по AirPlay, остановится. Включить обратно можно здесь же'],
  'ru.lostos.spotify': ['Выключить Spotify Connect?', 'Колонка пропадёт из списка устройств в приложении Spotify, а музыка из него остановится. Включить обратно можно здесь же'],
  'ru.lostos.alice-api': ['Выключить Alice API?', 'Облачные озвучка и распознавание Яндекса перестанут работать. Колонка услышит вас и заговорит, только если установлены Silero и Whisper или плагины с голосом и распознаванием'],
};
async function switchSystemPlugin(p, after) {
  if (!p) return;
  const on = !p.enabled, warn = SYS_OFF[p.id];
  if (!on && warn && !(await confirmAsk(warn[0], warn[1], 'Выключить', 'danger'))) { loadPlugins(); return; }
  const r = await fetch('/api/v1/plugins/' + encodeURIComponent(p.id), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled: on }) }).then((x) => x.json()).catch(() => ({}));
  if (!r.ok) toast(r.error || 'Не получилось');
  await loadPlugins();
  S.tilesSig = ''; loadTiles();                              // the tile of a plugin is there while the plugin is on
  if (after) after();
}
/* the repository of plugins: a signed list that the server checks; the owner installs and updates from it */
async function loadRepo(force) {
  const box = $('#plugRepo'); if (!box) return;
  let d = null;
  try { d = await fetch('/api/v1/registry' + (force ? '?refresh=1' : '')).then((r) => r.json()); } catch (e) { d = null; }
  if (!d || !d.ok) { box.innerHTML = '<div class="empty">Репозиторий недоступен</div>'; return; }
  const list = d.plugins || [];
  box.innerHTML = list.length
    ? list.map((p) => `<div class="row"><span class="k"><b>${esc(p.name)}</b><small>${esc(p.version)} · ${esc(p.publisher || '')}${p.about ? ' · ' + esc(p.about) : ''}</small></span>${p.installed && !p.update ? '<span class="v">установлен</span>' : `<button class="mini-btn acc" data-repoinst="${esc(p.id)}">${p.update ? 'Обновить' : 'Установить'}</button>`}</div>`).join('')
    : `<div class="empty">${d.error ? 'Пока недоступен: ' + esc(d.error) : 'В репозитории пока ничего нет'}</div>`;
}
document.addEventListener('click', async (ev) => {
  const b = ev.target.closest('[data-repoinst]'); if (!b) return;
  b.disabled = true; b.textContent = 'Ставлю…';
  const r = await fetch('/api/v1/registry/install', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id: b.dataset.repoinst }) }).then((x) => x.json()).catch(() => ({}));
  toast(r.ok ? 'Установлено, включите его на странице плагина' : r.error || 'Не получилось');
  loadPlugins();
});
async function loadPlugins() {
  let d;
  try { d = await fetch('/api/v1/plugins').then((r) => r.json()); } catch (e) { return; }
  const sysState = (p) => (p.builtin === 'speaker' ? (p.running ? 'работает на колонке' : 'колонка не в сети') : p.enabled ? 'включён' : 'выключен');
  const row = (p) => `<div class="plug" data-pid="${esc(p.id)}"><div class="pi">${p.logo ? `<img src="${esc(p.logo)}" alt="" referrerpolicy="no-referrer">` : tileSvg(p.icon)}</div><div class="pm"><b>${esc(p.name)}</b>
    ${p.system ? '' : '<small>' + esc(p.version) + ' · ' + esc(p.author) + ' · ' + (p.signature && p.signature.state === 'trusted' ? '<span class="ok">подписан: ' + esc(p.signature.publisher) + '</span>' : p.signature && p.signature.state === 'known' ? '<span class="warn">подпись верна, автор неизвестен</span>' : '<span class="warn">не подписан</span>') + '</small>'}
    ${p.error ? `<small class="warn">${esc(p.error)}</small>` : ''}${p.violations ? `<small class="warn">нарушений: ${p.violations}</small>` : ''}</div>
    ${p.system && !p.switchable ? '<small>всегда</small>' : `<button class="switch${p.enabled ? ' on' : ''}" data-plug="${esc(p.id)}" aria-label="${esc(p.name)}"><i></i></button>`}</div>`;
  const sys = d.plugins.filter((p) => p.system), usr = d.plugins.filter((p) => !p.system);
  $('#plugSys').innerHTML = sys.length ? sys.map(row).join('') : '<div class="empty">Пока нет</div>';
  $('#sysCount').textContent = sys.length ? sys.filter((p) => p.enabled).length + ' из ' + sys.length : '';
  applySysView();
  $('#plugUser').innerHTML = usr.length ? usr.map(row).join('') : '<div class="empty">Нет установленных плагинов</div>';
  $('#plugSub').textContent = usr.filter((p) => p.enabled).length ? 'включено: ' + usr.filter((p) => p.enabled).length : 'виджеты и расширения';
  S.plugs = d.plugins;
  loadRepo();
  $$('#plugSys [data-plug]').forEach((b) => b.addEventListener('click', () => switchSystemPlugin(S.plugs.find((x) => x.id === b.dataset.plug))));
  $$('#plugUser [data-plug]').forEach((b) => b.addEventListener('click', async () => {
    const p = S.plugs.find((x) => x.id === b.dataset.plug), on = !p.enabled;
    if (on) {
      const risky = p.risky.length ? ' Особо осторожно: ' + p.risky.map((k) => p.permission_names[k]).join(', ') + '.' : '';
      if (!(await confirmAsk('Включить «' + p.name + '»?', 'Автор: ' + p.author + '. Разработчик не подтверждён: lostOS не отвечает за утечку данных через этот плагин. Он получит право: ' +
        (p.permissions.map((k) => p.permission_names[k]).join('; ') || 'ничего особенного') + '.' + risky, 'Включить'))) return;
    }
    const rr = await fetch('/api/v1/plugins/' + encodeURIComponent(p.id), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled: on }) }).then((x) => x.json()).catch(() => ({}));
    if (rr.ok === false) toast(rr.error || 'Не получилось');
    setTimeout(() => { loadPlugins(); loadTiles(); }, 1500);
  }));
}
/* ---------------------------------------------------------------- keys of the owner (the encrypted vault) are a section of the account page */
const KEY_TEXT = ['SPEAKER_ROOM', 'SPOTIFY_CLIENT_ID'];
/* the keys are a view of their own inside the account page: the row "Ключи" opens it, back returns to the account */
const ACC_VIEWS = { keys: 'Ключи', stations: 'Станции' };
function openAccView(name) { S.accView = name; $('#acMain').hidden = true; $('#acKeysView').hidden = name !== 'keys'; $('#acStationsView').hidden = name !== 'stations'; navFor(S.tab); $('#s-account .scroll').scrollTop = 0; if (name === 'keys') loadKeys(); else loadStations(); }
function closeAccView() { S.accView = ''; $('#acMain').hidden = false; $('#acKeysView').hidden = true; $('#acStationsView').hidden = true; navFor(S.tab); }
document.addEventListener('click', (ev) => { if (ev.target.closest('#acKeysRow')) openAccView('keys'); else if (ev.target.closest('#acStationsRow')) openAccView('stations'); });
async function loadKeys() {
  let d = null, sp = null;
  try { const r = await fetch('/api/v1/secrets'); d = r.ok ? await r.json() : null; } catch (e) { d = null; }
  try { const r = await fetch('/api/v1/spotify'); sp = r.ok ? await r.json() : null; } catch (e) { sp = null; }
  $('#acKeysNav').hidden = !d;
  if (!d) { if (S.accView === 'keys') closeAccView(); return; }
  if (document.activeElement && document.activeElement.closest && document.activeElement.closest('#acKeysView') && /INPUT/.test(document.activeElement.tagName)) return;
  $('#acKeysLocked').hidden = !d.locked;
  $('#acSpotify').hidden = !sp;
  if (sp) $('#acSpotify').innerHTML = `<div class="row"><span class="k">Spotify</span><span class="v">${sp.logged_in ? 'подключён' : 'не подключён'}</span>
    ${sp.logged_in ? '<button class="mini-btn" id="spOut">Выйти</button>' : `<button class="mini-btn acc" id="spIn" ${sp.locked || !sp.client_id ? 'disabled' : ''}>Войти</button>`}</div>`;
  $('#acKeys').innerHTML = d.keys.map((k) => `<div class="row action su-field" data-ackey="${esc(k.name)}"><span class="k">${esc(k.title)}</span>
    <span class="su-val">${k.saved ? '<span class="chip ok">сохранён</span>' : '<span class="chip">нет</span>'}</span>${k.saved ? `<button class="mini-btn" data-vkdel="${esc(k.name)}" aria-label="Удалить">×</button>` : ''}</div>`).join('');
}
document.addEventListener('click', async (ev) => {
  if (ev.target.closest('#spIn')) {
    const r = await fetch('/api/v1/spotify/login', { method: 'POST' }).then((x) => x.json());
    if (r.ok) location.href = r.url; else toast(r.error || 'Не получилось');
    return;
  }
  if (ev.target.closest('#spOut')) {
    if (!(await confirmAsk('Выйти из Spotify?', 'Токен будет стёрт из хранилища сервера', 'Выйти', 'danger'))) return;
    await fetch('/api/v1/spotify/logout', { method: 'POST' });
    loadKeys();
    return;
  }
  const dl = ev.target.closest('[data-vkdel]');
  if (dl) {
    if (!(await confirmAsk('Удалить ключ?', 'Он будет стёрт из хранилища сервера', 'Удалить', 'danger'))) return;
    await fetch('/api/v1/secrets/' + encodeURIComponent(dl.dataset.vkdel), { method: 'DELETE' });
    loadKeys();
    return;
  }
  const row = ev.target.closest('[data-ackey]');
  if (row && !row.querySelector('input') && $('#acKeysLocked').hidden) {        // the chip turns into the input in the same line
    const name = row.dataset.ackey, slot = row.querySelector('.su-val');
    slot.innerHTML = `<input class="pill-in su-in" type="${KEY_TEXT.includes(name) ? 'text' : 'password'}" autocomplete="off" placeholder="вставьте значение">`;
    const input = slot.firstChild; input.focus();
    let done = false;
    const finish = async (save) => {
      if (done) return; done = true;
      const value = input.value.trim();
      if (save && value) {
        const r = await fetch('/api/v1/secrets', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name, value }) }).then((x) => x.json()).catch(() => ({}));
        toast(r.ok ? 'Сохранено' : r.error || 'Не сохранено');
      }
      input.blur();                                  // the list is redrawn only when no field has the focus
      loadKeys();
    };
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') finish(true); else if (e.key === 'Escape') finish(false); });
    input.addEventListener('blur', () => finish(true));
  }
});

/* ---------------------------------------------------------------- stations linked to a server on the internet (only a lite server has them) */
async function loadStations() {
  let d = null;
  try { const r = await fetch('/api/v1/stations'); d = r.ok ? await r.json() : null; } catch (e) { d = null; }
  $('#acStationsNav').hidden = !d;
  if (!d) { if (S.accView === 'stations') closeAccView(); return; }
  if (document.activeElement && document.activeElement.closest && document.activeElement.closest('#stList') && /INPUT/.test(document.activeElement.tagName)) return;
  const st = d.stations;
  $('#acStationsCount').textContent = st.length ? String(st.length) : '';
  const mine = st.filter((s) => s.confirmed);
  let cur = ''; try { cur = localStorage.getItem('lostos.station') || ''; } catch (e) { cur = ''; }
  if (!mine.some((s) => s.id === cur)) cur = '';
  const now = mine.find((s) => s.id === cur) || mine.find((s) => s.online) || mine[0];
  $('#stationSwitch').hidden = mine.length < 2;
  if (now) { $('#stationNow').textContent = now.name || 'Колонка'; const ss = $('#sideSub'); if (ss) ss.textContent = now.name || 'Колонка'; }
  const pickStation = (anchor) => menu(anchor, mine.map((s) => ({ t: (s.name || 'Колонка') + (s.online ? '' : ' (не в сети)'), on: now && s.id === now.id, v: s.id })),
    (v) => { try { localStorage.setItem('lostos.station', v); } catch (e) { /* ignore */ } location.reload(); });
  const rs = $('#rowStation'); if (rs) rs.onclick = () => pickStation(rs);
  const sf = document.querySelector('.side-foot');          // on the desktop the switch is in the left block, under the tabs
  if (sf) { sf.classList.toggle('pick', mine.length > 1); sf.onclick = mine.length > 1 ? () => pickStation(sf) : null; }
  const stText = (s) => (s.confirmed ? (s.online ? 'В сети' : 'Не в сети') : s.press ? `нажмите кнопку Алисы на колонке, осталось ${s.press} с` : 'ждёт нажатия кнопки Алисы');
  $('#stList').innerHTML = `<div class="row action su-field" data-stclaim><span class="k">Подключить колонку<small>идентификатор из установщика</small></span><span class="su-val"><span class="chip">ввести</span></span></div>`
    + st.map((s) => `<div class="row"><span class="k"><b>${esc(s.name || 'Колонка')}</b><small>${esc(s.id)} · ${stText(s)}</small></span><button class="mini-btn" data-stdel="${esc(s.id)}">Убрать</button></div>`).join('');
}
/* The person types the identifier of HIS speaker (the installer shows it): only then the station becomes his and asks for the Alice button. Waiting stations are never listed. */
async function claimStation(ident) {
  ident = normId(ident);
  const r = await fetch('/api/v1/stations/claim', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id: ident }) }).then((x) => x.json()).catch(() => ({}));
  toast(r.state === 'press' ? 'Нажмите кнопку Алисы на колонке' : r.state === 'ok' ? 'Колонка подключена' : r.error || 'Не получилось');
  return r;
}
document.addEventListener('click', async (ev) => {
  const claim = ev.target.closest('[data-stclaim]'), x = ev.target.closest('[data-stdel]');
  if (claim && !claim.querySelector('input')) {
    const slot = claim.querySelector('.su-val');
    slot.innerHTML = '<input class="pill-in su-in" type="text" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="20 знаков">';
    const input = slot.firstChild; input.focus();
    input.addEventListener('input', () => { const n = normId(input.value); if (input.value !== n) input.value = n; });
    let done = false;
    const finish = async (save) => {
      if (done) return; done = true;
      const v = input.value.trim();
      input.blur();
      if (save && v) await claimStation(v);
      loadStations();
    };
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') finish(true); else if (e.key === 'Escape') finish(false); });
    input.addEventListener('blur', () => finish(true));
  } else if (x) {
    if (!(await confirmAsk('Убрать колонку?', 'Она отключится от сервера. Чтобы подключить снова, введите её идентификатор и нажмите кнопку Алисы', 'Убрать', 'danger'))) return;
    await fetch('/api/v1/stations/' + encodeURIComponent(x.dataset.stdel), { method: 'DELETE' });
    loadStations();
  }
});
loadStations(); setInterval(() => { if (S.tab === 'settings') loadStations(); }, 8000);

/* ---------------------------------------------------------------- about the speaker: its name for AirPlay and Spotify, the model (a tap shows the codename), the load and the versions */
async function loadSpeaker() {
  let d = null;
  try { d = await fetch('/api/v1/speaker').then((r) => r.json()); } catch (e) { return; }
  S.speaker = d;
  const mb = (kb) => (kb == null ? '—' : Math.round(kb / 1024) + ' МБ');
  if (document.activeElement !== $('#spName')) $('#spName').value = d.name || '';
  const num = (v) => { const m = /\d+(\.\d+)+/.exec(v || ''); return m ? m[0] : '—'; };          // only the number of the version: 0.3.0, not the build text
  const model = (d.model || '').replace(/^Яндекс\s+/, '') || '—';                                  // "Станция Мини 3"
  const mem = (used, total) => (total ? Math.round(used / 1024) + ' / ' + Math.round(total / 1024) + ' МБ' : '—');
  const rows = [['Модель', `<button class="v link" id="spModel">${esc(S.spCode ? d.codename || '—' : model)}</button>`],
    ['lostOS', esc(num(d.lostos))], ['Прошивка колонки', esc(d.firmware || '—')], ['Процессор', esc(d.chip || '—')],
    ['Память', mem(d.mem_used, d.mem_total)], ['Накопитель', d.disk_total ? mem(d.disk_total - d.disk_free, d.disk_total) : '—']];
  $$('#spInfo > .row:not(:first-child)').forEach((r) => r.remove());
  $('#spInfo').insertAdjacentHTML('beforeend', rows.map(([k, v]) => `<div class="row"><span class="k">${esc(k)}</span>${v.startsWith('<button') ? v : `<span class="v">${v}</span>`}</div>`).join(''));
}
$('#spInfo').addEventListener('click', (ev) => { if (ev.target.closest('#spModel')) { S.spCode = !S.spCode; loadSpeaker(); } });   // a tap on the model shows its codename
async function saveSpeakerName(name) {
  const r = await fetch('/api/v1/speaker', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) }).then((x) => x.json()).catch(() => ({}));
  toast(r.ok ? 'Имя сохранено: AirPlay и Spotify перезапустятся' : r.error || 'Не получилось');
  setTimeout(loadSpeaker, 4000);
}
// The name is kept when the field is left (or Done is pressed); an empty field means the name of the model.
$('#spName').addEventListener('change', () => { const v = $('#spName').value.trim(); if (v !== (S.speaker && S.speaker.name)) saveSpeakerName(v); });
$('#spName').addEventListener('keydown', (e) => { if (e.key === 'Enter') e.target.blur(); });
$('[data-sub="about"]').addEventListener('click', loadSpeaker);
loadSpeaker(); setInterval(() => { if (S.tab === 'settings') loadSpeaker(); }, 8000);

/* the page of a plugin: who made it, where it came from, what it may do, and its README (drawn by md.js without any HTML from the text) */
const LEVELS = { normal: 'Безопасное: только смотрит или хранит своё', sensitive: 'Управляет колонкой: может сам менять музыку, звук, подсветку', dangerous: 'Опасное: видит личное или выходит в сеть' };
function permBlock(title, rows, plug) {
  if (!rows.length) return '';
  const chip = (r) => (r.granted ? '<span class="chip ok">выдано</span>' : plug.enabled ? '<span class="chip">не выдано</span>' : '<span class="chip">спросим при включении</span>');
  return `<div class="cap">${esc(title)}</div><div class="inset">${rows.map((r) => `<div class="row pperm ${esc(r.level)}"><i class="lvl"></i><span class="k"><b>${esc(r.title)}</b>
    <small>${esc(r.what)}</small>${r.risk ? `<small class="risk">Чем рискуете: ${esc(r.risk)}</small>` : ''}<small class="lv">${esc(LEVELS[r.level] || '')}</small></span>${chip(r)}</div>`).join('')}</div>`;
}
async function openPlugin(id) {
  let pg;
  try { pg = await fetch('/api/v1/plugins/' + encodeURIComponent(id) + '/page').then((r) => (r.ok ? r.json() : null)); } catch (e) { pg = null; }
  if (!pg) { toast('Не получилось открыть страницу плагина'); return; }
  const box = $('#plugDetail');
  const logo = pg.logo ? `<img src="${esc(pg.logo)}" alt="" referrerpolicy="no-referrer">` : tileSvg(pg.icon);
  const state = pg.system && !pg.switchable ? (pg.builtin === 'speaker' ? (pg.running ? 'работает' : 'Не в сети') : 'встроенный') : pg.enabled ? (pg.running ? 'работает' : 'включён') : 'выключен';
  box.innerHTML = `<div class="pd-head"><div class="pd-logo">${logo}</div><div class="pd-meta"><h1>${esc(pg.name)}</h1>
      <div class="pd-by">${esc(pg.author)} · версия ${esc(pg.version)}</div>
      <div class="pd-badges"><span class="chip ${pg.enabled ? 'ok' : ''}">${esc(state)}</span>${pg.verified ? '' : '<span class="chip warn">разработчик не подтверждён</span>'}</div></div></div>
    ${pg.description ? `<p class="pd-desc">${esc(pg.description)}</p>` : ''}
    <div class="inset"><div class="row"><span class="k">Источник установки<small>${esc(pg.source.title)}${pg.source.detail ? '<br>' + esc(pg.source.detail) : ''}</small></span></div>
      <div class="row"><span class="k">Идентификатор<small>${esc(pg.id)}</small></span></div>
      ${pg.min_firmware ? `<div class="row"><span class="k">Нужна прошивка lostOS<small>${esc(pg.min_firmware)} или новее${pg.firmware ? ', на колонке ' + esc(pg.firmware) : ''}</small>${pg.firmware_problem ? `<small class="warn">${esc(pg.firmware_problem)}</small>` : ''}</span></div>` : ''}
      ${pg.builtin ? `<div class="row"><span class="k">Где работает<small>${pg.side === 'speaker' ? 'на самой колонке' : 'на сервере'}</small></span></div>` : ''}
      ${(pg.requires || []).map((q) => `<div class="row"><span class="k">Использует<small>${esc(q.name)}${q.enabled ? '' : ' (выключен)'}</small></span></div>`).join('')}
      ${(pg.used_by || []).map((q) => `<div class="row"><span class="k">Им пользуется<small>${esc(q.name)}${q.enabled ? '' : ' (выключен)'}</small></span></div>`).join('')}</div>
    ${pg.error ? `<p class="hint warn">${esc(pg.error)}</p>` : ''}${pg.violations ? `<p class="hint warn">Нарушений прав: ${pg.violations}</p>` : ''}
    ${pg.system ? '' : permBlock('Разрешения на сервере', pg.permissions, pg) + permBlock('Разрешения на колонке (в песочнице)' + (pg.device_state ? ': ' + pg.device_state : ''), pg.device_permissions, pg)}
    ${pg.system || pg.permissions.length + pg.device_permissions.length ? '' : '<div class="cap">Разрешения</div><div class="inset"><div class="empty">Плагин не просит ничего особенного</div></div>'}
    ${pg.system && pg.device_state ? `<div class="inset"><div class="row"><span class="k">Состояние на колонке<small>${esc(pg.device_state)}</small></span></div></div>` : ''}
    <div class="cap">Описание</div><div class="inset md-box"><article class="md" id="plugMd"></article></div>
    ${pg.system && pg.switchable ? `<div class="inset gap"><div class="row"><span class="k">Включён</span><button class="switch${pg.enabled ? ' on' : ''}" id="pdSysSwitch" aria-label="Включить"><i></i></button></div></div>` : ''}
    ${pg.system ? '' : `<div class="inset gap"><div class="row"><span class="k">Включён</span><button class="switch${pg.enabled ? ' on' : ''}" id="pdSwitch" aria-label="Включить"><i></i></button></div></div>
      <button class="pill-btn glass" id="pdDelete" style="color:var(--danger)">Удалить плагин</button>`}`;
  const md = $('#plugMd');
  if (pg.readme.trim()) md.appendChild(window.lostMd.render(pg.readme, pg.assets)); else md.innerHTML = '<p class="md-empty">У плагина нет описания</p>';
  $('#plugListView').hidden = true; box.hidden = false;
  S.plugOpen = pg.name; S.plugId = pg.id; navFor(S.tab);
  $('.scroll', subs.plugins).scrollTop = 0;
  const ssw = $('#pdSysSwitch');
  if (ssw) ssw.addEventListener('click', () => switchSystemPlugin({ id: pg.id, enabled: pg.enabled }, () => { if (S.plugId === pg.id && S.plugOpen) openPlugin(pg.id); }));
  const sw = $('#pdSwitch');
  if (sw) sw.addEventListener('click', async () => {
    const p = { ...pg }, on = !pg.enabled;
    if (on && !(await confirmAsk('Включить «' + p.name + '»?', 'Разработчик не подтверждён: lostOS не отвечает за утечку данных через этот плагин. Проверьте разрешения на странице', 'Включить'))) return;
    const rr = await fetch('/api/v1/plugins/' + encodeURIComponent(p.id), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled: on }) }).then((x) => x.json()).catch(() => ({}));
    if (rr.ok === false) toast(rr.error || 'Не получилось');
    setTimeout(() => { if (S.plugId === p.id && S.plugOpen) openPlugin(p.id); loadTiles(); }, 1500);
  });
  const del = $('#pdDelete');
  if (del) del.addEventListener('click', async () => {
    if (!(await confirmAsk('Удалить «' + pg.name + '»?', 'Плагин и его данные будут удалены с сервера', 'Удалить', 'danger'))) return;
    await fetch('/api/v1/plugins/' + encodeURIComponent(pg.id), { method: 'DELETE' });
    closePlugin(); loadTiles();
  });
}
function closePlugin() {
  S.plugOpen = ''; S.plugId = '';
  $('#plugDetail').hidden = true; $('#plugDetail').innerHTML = ''; $('#plugListView').hidden = false;
  navFor(S.tab); loadPlugins();
}
document.addEventListener('click', (ev) => {
  const row = ev.target.closest('#plugSys .plug, #plugUser .plug');
  if (row && !ev.target.closest('button')) openPlugin(row.dataset.pid);
});
$('[data-sub="plugins"]').addEventListener('click', loadPlugins);
$('#rowPlugInstall').addEventListener('click', () => $('#filePlug').click());
$('#filePlug').addEventListener('change', async (e) => {
  const f = e.target.files[0]; e.target.value = '';
  if (!f) return;
  try {
    const r = await fetch('/api/v1/plugins/install', { method: 'POST', body: f, headers: { 'X-File-Name': encodeURIComponent(f.name) } }).then((x) => x.json());
    toast(r.ok ? 'Установлен: ' + r.plugin.name + ' (выключен)' : 'Не установлен: ' + r.error);
  } catch (err) { toast('Не получилось отправить файл'); }
  loadPlugins();
});
loadPlugins();

/* the bell and the avatar: in the bar on the phone, in the account card of the sidebar on the desktop */
(() => {
  const mq = window.matchMedia('(min-width: 900px)'), right = $('.nav-right');
  const place = () => {
    if (mq.matches) { $('#saAvatar').appendChild($('#btnAvatar')); $('#saBell').appendChild($('#btnBell')); }
    else { right.insertBefore($('#btnBell'), right.firstChild); right.appendChild($('#btnAvatar')); }
    if (S.tab) navFor(S.tab);
  };
  $('#saTxt').addEventListener('click', () => $('#btnAvatar').click());
  mq.addEventListener('change', place); place();
})();

/* ---------------------------------------------------------------- start */
let start = 'home'; try { start = localStorage.getItem('tab') || 'home'; } catch (e) {}
start = { timers: 'utils', voices: 'llm', system: 'settings' }[start] || start;
if (!TABS.includes(start)) start = 'home';
S.tab = start; Object.values(pages).forEach((p) => p.classList.remove('active')); pages[start].classList.add('active');
$('#navTitle').textContent = pages[start].dataset.title; $('#btnAdd').hidden = true; $('#btnBack').hidden = true;
tabEls.forEach((t) => t.classList.toggle('active', t.dataset.tab === start));
const place = () => { lensTo(TABS.indexOf(S.tab), true); };
place(); addEventListener('resize', place); document.fonts && document.fonts.ready.then(place);
load(); setInterval(load, 1600); document.addEventListener('visibilitychange', () => { if (!document.hidden) load(); });
setTimeout(glassUpgrade, 400);

/* ---------------------------------------------------------------- the account of a server open to the internet: who I am, the people, the sessions */
const shortAgent = (a) => { const os = /iPhone|iPad|Android|Windows|Mac OS|Linux/.exec(a || ''), br = /Edg\/|OPR\/|Firefox|CriOS|Chrome|Safari/.exec(a || ''); return [br && ({ 'Edg/': 'Edge', 'OPR/': 'Opera', CriOS: 'Chrome' }[br[0]] || br[0]), os && os[0].replace('Mac OS', 'Mac')].filter(Boolean).join(' · ') || 'устройство'; };
const whenStr = (t) => (t ? new Date(t * 1000).toLocaleString('ru-RU', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }) : '—');
async function loadAccount() {
  let d = null;
  try { const r = await fetch('/api/v1/account'); d = r.ok ? await r.json() : null; } catch (e) { d = null; }
  $('#rowAccount').hidden = !d;
  if (!d) return;
  S.account = d;
  setLocked(!!d.locked);
  $('#acSessBox').hidden = !!d.local; $('#acLocal').hidden = !d.local; if (d.local) $('#acKeysNav').hidden = true; else if (d.me.role === 'owner') loadKeys();
  if (d.local) {                                                      // a home server: nobody logs in, the page only says so and points to the server on the internet
    $('#acSub').textContent = 'вход не нужен, вы дома'; $('#lkName').textContent = 'Домашняя сеть'; $('#lkSub').textContent = 'Вход не нужен'; $('#lkAv').innerHTML = '<span>L</span>';
    $('#saTxt').innerHTML = '<b>Домашняя сеть</b><small>вход не нужен</small>';
    $('#acMe').innerHTML = '<div class="row"><span class="k">Доступ<small>только из вашей сети</small></span><span class="v">без входа</span></div>';
    $('#acOwner').hidden = true;
    $('#acRemote').hidden = !d.remote; if (d.remote) $('#acRemote').href = d.remote;
    return;
  }
  const owner = d.me.role === 'owner';
  $('#lkName').textContent = d.me.name || 'Аккаунт'; $('#lkSub').textContent = 'Ваш аккаунт в lostID';
  $('#cwName').textContent = d.me.name || 'Аккаунт'; $('#cwAv').innerHTML = d.me.photo ? `<img src="${esc(d.me.photo)}" alt="" referrerpolicy="no-referrer">` : esc((d.me.name || 'L').trim().charAt(0).toUpperCase());
  $('#lkAv').innerHTML = d.me.photo ? `<img src="${esc(d.me.photo)}" alt="" referrerpolicy="no-referrer">` : `<span>${esc((d.me.name || 'L').trim().charAt(0).toUpperCase())}</span>`;
  const av = $('#btnAvatar');
  if (d.me.photo && av.dataset.photo !== d.me.photo) { av.dataset.photo = d.me.photo; av.innerHTML = `<img src="${esc(d.me.photo)}" alt="" referrerpolicy="no-referrer" style="width:100%;height:100%;border-radius:50%;object-fit:cover;display:block">`; }
  $('#saTxt').innerHTML = `<b>${esc(d.me.name || 'Аккаунт')}</b><small>${owner ? 'владелец' : 'участник'}</small>`;
  $('#acSub').textContent = owner ? (d.bound ? 'владелец · Telegram привязан' : 'привяжите Telegram') : 'участник';
  const tg = d.me.username ? '@' + d.me.username : d.bound || !owner ? 'привязан' : 'не привязан';
  $('#acMe').innerHTML = `<div class="row"><span class="k">Вы<small>${owner ? 'владелец сервера' : 'участник'}</small></span><span class="v">${esc(d.me.name || '—')}</span></div>
    <div class="row"><span class="k">Telegram</span><span class="v">${esc(tg)}</span>${owner && !d.bound ? (d.oidc ? '<a class="mini-btn acc" href="/auth/telegram/start?bind=1">Привязать</a>' : '<button class="mini-btn acc" id="acBind">Привязать</button>') : ''}</div>
    <div class="row"><span class="k">Бот</span><span class="v">${d.bot.username ? '@' + esc(d.bot.username) : d.bot.configured ? 'запускается…' : 'не настроен'}</span></div>`;
  $('#acOwner').hidden = true;          // the people and the invitations live in the admin panel now; the account page keeps only what is about this person
  if (owner) {
    $('#acUsers').innerHTML = d.users.length ? d.users.map((u) => `<div class="row"><span class="k"><b>${esc(u.name || u.tg_id)}</b><small>${u.role === 'owner' ? 'владелец' : 'участник'}${u.username ? ' · @' + esc(u.username) : ''} · вход: ${esc(whenStr(u.last_login))}</small></span>
      ${u.role === 'owner' ? '' : `<button class="mini-btn" data-acdel="${esc(u.tg_id)}">Убрать</button>`}</div>`).join('') : '<div class="empty">Пока никого</div>';
  }
  $('#acSessions').innerHTML = d.sessions.length ? d.sessions.map((s) => `<div class="row"><span class="k"><b>${esc(shortAgent(s.agent))}</b><small>${owner ? esc(s.who) + ' · ' : ''}${esc(s.ip || '')} · ${esc(whenStr(s.last || s.created))}</small></span>
    <button class="mini-btn" data-acend="${esc(s.id)}">Завершить</button></div>`).join('') : '<div class="empty">Нет</div>';
}
document.addEventListener('click', async (ev) => {
  const bind = ev.target.closest('#acBind'), del = ev.target.closest('[data-acdel]'), end = ev.target.closest('[data-acend]');
  if (bind) {
    const r = await fetch('/api/v1/account/bind', { method: 'POST' }).then((x) => x.json()).catch(() => ({}));
    if (!r.ok) return toast(r.error || 'Не получилось');
    $('#acBindOut').hidden = false; $('#acBindOut').innerHTML = `Откройте бота и нажмите «Да, это я»: <a href="${esc(r.url)}" target="_blank" rel="noopener">открыть бота в Telegram</a>`;
    window.open(r.url, '_blank', 'noopener');
  } else if (del) {
    if (!(await confirmAsk('Убрать человека?', 'Он больше не сможет войти, его сессии завершатся', 'Убрать', 'danger'))) return;
    await fetch('/api/v1/account/users/' + encodeURIComponent(del.dataset.acdel), { method: 'DELETE' });
    loadAccount();
  } else if (end) {
    await fetch('/api/v1/account/sessions/' + encodeURIComponent(end.dataset.acend), { method: 'DELETE' });
    loadAccount();
  }
});
$('#acInvite').addEventListener('click', async () => {
  const r = await fetch('/api/v1/account/invite', { method: 'POST' }).then((x) => x.json()).catch(() => ({}));
  if (!r.ok) return toast(r.error || 'Не получилось');
  const out = $('#acInviteOut'); out.hidden = false;
  out.innerHTML = `Отправьте человеку эту ссылку (она работает один раз, ${r.hours} ч): <a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.url)}</a> <button class="mini-btn" id="acCopy">Скопировать</button>`;
  $('#acCopy').addEventListener('click', () => { try { navigator.clipboard.writeText(r.url); toast('Скопировано'); } catch (e) { toast('Скопируйте ссылку выше'); } });
});
$('#acLogout').addEventListener('click', async () => { await fetch('/auth/logout', { method: 'POST' }); location.reload(); });
$('#acLogoutAll').addEventListener('click', async () => {
  if (!(await confirmAsk('Выйти везде?', 'Все остальные устройства придётся входить заново. Это устройство останется', 'Выйти', 'danger'))) return;
  const r = await fetch('/api/v1/account/logout-all', { method: 'POST' }).then((x) => x.json()).catch(() => ({}));
  toast(r.ok ? 'Завершено сессий: ' + r.ended : 'Не получилось'); loadAccount();
});
$('[data-sub="account"]').addEventListener('click', loadAccount);
$('#lkCard').addEventListener('click', () => { if (S.account) { pushSub('account'); loadAccount(); } });
loadAccount(); setInterval(() => { if (S.tab === 'settings') loadAccount(); }, 6000);

/* ---------------------------------------------------------------- the first-run wizard: the speaker, the model, search, the assistant, Spotify, Home Assistant, the city, the voice */
const SU = { i: 0, d: null, open: false, key: '', timer: 0 };
const SU_STEPS = [['station', 'Колонка'], ['brain', 'Нейросеть'], ['search', 'Поиск'], ['agent', 'Помощник'], ['spotify', 'Spotify'], ['ha', 'Home Assistant'], ['city', 'Город'], ['voice', 'Голос'], ['done', 'Готово']];
const suApi = async (path, body) => fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).then(async (r) => ({ status: r.status, ...(await r.json().catch(() => ({}))) })).catch(() => ({ ok: false, error: 'нет связи с сервером' }));
async function suLoad() {
  let d = null;
  try { const r = await fetch('/api/v1/setup'); d = r.ok ? await r.json() : null; } catch (e) { d = null; }
  SU.d = d && d.available ? d : null;
  $('#rowSetup').hidden = true;                                        // there is no row for it in the panel: it is shown once, in the window of the connection
  return SU.d;
}
function suFirstTodo(d) { return !d.station || !d.station.confirmed ? 0 : !d.brain.selected ? 1 : 0; }
async function suOpen() {
  const d = await suLoad();
  if (!d) return;
  SU.i = suFirstTodo(d); SU.open = true;
  suRender();
  openSheet($('#sheetSetup'));
  clearInterval(SU.timer); SU.timer = setInterval(async () => { if (!$('#sheetSetup').classList.contains('show')) { SU.open = false; clearInterval(SU.timer); return; } if (!SU.open || document.activeElement.closest && document.activeElement.closest('#suBody') && document.activeElement.tagName === 'INPUT') return; if (await suLoad()) suRender(true); }, 3000);
}
/* A saved value is shown as a chip; a tap on the line turns the chip into the input (Enter or leaving the field saves, Escape cancels). */
const SU_FIELDS = { claim: ['text', '20 знаков'], SPOTIFY_CLIENT_ID: ['text', 'Client ID'], TAVILY_API_KEY: ['password', 'ключ Tavily'], HA_TOKEN: ['password', 'долгосрочный токен'], SPEAKER_ROOM: ['text', 'например, Кухня'], haurl: ['text', 'homeassistant.local:8123'] };
const suField = (name, label, saved, hint, shown) => `<div class="row action su-field" data-sufield="${name}"><span class="k">${label}${hint ? `<small>${hint}</small>` : ''}</span><span class="su-val">${shown ? `<span class="su-addr">${esc(shown)}</span>` : saved ? '<span class="chip ok">сохранён</span>' : '<span class="chip warn">нет</span>'}</span></div>`;
const suChip = (ok, yes, no) => `<span class="chip ${ok ? 'ok' : 'warn'}">${ok ? yes : no}</span>`;
function suCityTime(tz) {
  try { return tz ? '. Там сейчас ' + new Date().toLocaleTimeString('ru-RU', { timeZone: tz, hour: '2-digit', minute: '2-digit' }) + ' (' + tz + ')' : ''; } catch (e) { return ''; }
}
function suStepHtml(id, d) {
  const url = d.public_url;
  if (id === 'station') {
    const s = d.station;
    if (s && s.confirmed) return `<h2>Колонка подключена</h2><div class="inset"><div class="row"><span class="k"><b>${esc(s.name || 'Колонка')}</b><small>${esc(s.id)}</small></span>${suChip(s.online, 'В сети', 'Не в сети')}</div></div>
      <p class="hint">Колонка и сервер связаны, ключи хранятся в зашифрованном виде, а ключ от них лежит у колонки. Дальше настроим, чем она будет думать</p>`;
    if (s) return `<h2>Нажмите кнопку Алисы</h2><div class="inset"><div class="row"><span class="k"><b>${esc(s.name || 'Колонка')}</b><small>${esc(s.id)}</small></span><span class="chip warn">${s.press ? `осталось ${s.press} с` : 'ждёт нажатия'}</span></div></div>
      <p class="hint">Колонка сказала, что нужно нажать её кнопку Алисы, и показывает на часах первые знаки идентификатора. Нажмите, и колонка станет вашей</p>`;
    return `<h2>Подключите колонку</h2><p class="hint"><b>Пока колонка не подключена, панель закрыта</b>: доступен только аккаунт</p>
      <div class="inset"><div class="row steps"><span class="k"><small>1. Установите lostOS на колонку установщиком и укажите этот сервер: <code>lostos-install --server ${esc(url)}</code><br>2. Колонка придёт сюда сама и скажет «Подключение успешно, ожидаю привязки к вашему аккаунту»<br>3. Установщик покажет её идентификатор из 20 знаков (он же лежит на колонке в <code>/data/lostos/link.id</code>)<br>4. Нажмите на строку ниже и введите его</small></span></div>
      ${suField('claim', 'Идентификатор колонки', false, 'нажмите и введите', 'ввести')}</div>
      <p class="hint">Чужие колонки здесь не видны: сервер показывает только ту, идентификатор которой вы ввели сами</p>`;
  }
  if (id === 'brain') {
    const rows = d.brain.providers.map((p) => `<div class="su-prov${SU.key === p.id ? ' open' : ''}" data-suprov="${esc(p.id)}"><div class="row action"><span class="k"><b>${esc(p.title)}</b><small>${esc(p.hint)}</small></span>
      ${p.selected ? '<span class="chip ok">отвечает</span>' : p.has_key ? '<span class="chip">ключ есть</span>' : ''}</div>
      ${SU.key === p.id ? `<div class="su-key"><input class="pill-in" id="suKey" type="password" autocomplete="off" placeholder="вставьте ключ" style="max-width:none;text-align:left;width:100%">
      <div class="row-btns"><button class="mini-btn acc" id="suKeySave" data-env="${esc(p.env)}" data-prov="${esc(p.id)}">Проверить и сохранить</button><a class="mini-btn" href="${esc(p.url)}" target="_blank" rel="noopener">Где взять ключ</a>${p.has_key && !p.selected ? `<button class="mini-btn" data-suuse="${esc(p.id)}">Сделать основной</button>` : ''}</div>
      <p class="hint" id="suKeyMsg"></p></div>` : ''}</div>`).join('');
    return `<h2>Чем она будет думать</h2><p class="hint">Выберите нейросеть и вставьте свой ключ. Он сохранится в зашифрованном виде, а панель его больше не покажет. Можно добавить несколько и выбрать главную</p>
      ${d.vault_locked ? '<p class="hint warn-text">Хранилище закрыто: колонка сейчас не в сети, а ключи открывает только она</p>' : ''}<div class="inset">${rows}</div>`;
  }
  if (id === 'search') {
    return `<h2>Поиск в интернете</h2><p class="hint">Когда колонка не знает ответа, она может поискать в сети. Это можно выключить</p>
      <div class="inset"><div class="row"><span class="k">Искать в интернете<small>команда «поиск»</small></span><button class="switch${d.search.on ? ' on' : ''}" data-sutool="search_web" aria-label="Поиск"><i></i></button></div>
      ${suField('TAVILY_API_KEY', 'Ключ Tavily', d.search.tavily, 'необязательно: с ним поиск точнее')}</div>
      <p class="hint">Ключ бесплатный (1000 запросов в месяц), его выдают на <a href="https://tavily.com" target="_blank" rel="noopener">tavily.com</a>. Нажмите на строку и вставьте</p>`;
  }
  if (id === 'agent') {
    const st = (k, t) => `<button class="mini-btn${d.style === k ? ' acc' : ''}" data-sustyle="${k}">${t}</button>`;
    return `<h2>Помощник</h2><p class="hint">Как длинно отвечать и что она умеет. Команды плагинов тоже здесь: выключенную команда не увидит и не выполнит</p>
      <div class="row-btns" style="margin:0 0 14px">${st('short', 'Коротко')}${st('normal', 'Обычно')}${st('detailed', 'Подробно')}</div>
      <div class="inset">${d.commands.map((c) => `<div class="row"><span class="k"><b>${esc(c.title)}</b><small>${c.plugin ? 'плагин «' + esc(c.plugin) + '» · ' : ''}${esc(c.about)}</small></span><button class="switch${c.on ? ' on' : ''}" data-sutool="${esc(c.name)}" aria-label="${esc(c.title)}"><i></i></button></div>`).join('')}</div>`;
  }
  if (id === 'spotify') {
    const sp = d.spotify;
    return `<h2>Spotify</h2><p class="hint"><b>Нужен Spotify Premium.</b> Ключ приложения личный: никому его не передавайте. Так колонка сможет включать музыку по просьбе и лайкать треки</p>
      <div class="inset"><div class="row steps"><span class="k"><small>1. На developer.spotify.com/dashboard создайте приложение (Web API)</small></span><a class="mini-btn acc" href="https://developer.spotify.com/dashboard" target="_blank" rel="noopener">Открыть Spotify Dashboard</a></div>
      <div class="row"><span class="k"><small>2. В Redirect URIs добавьте адрес этого сервера:</small><b style="word-break:break-all;font-size:15px">${esc(sp.redirect)}</b></span><button class="mini-btn" id="suCopy" data-copy="${esc(sp.redirect)}">Копировать</button></div>
      ${suField('SPOTIFY_CLIENT_ID', '3. Client ID приложения', sp.client_id, 'нажмите и вставьте')}
      <div class="row"><span class="k"><small>4. Вход в Spotify</small></span>${sp.logged_in ? '<span class="chip ok">вы вошли</span>' : `<button class="mini-btn acc" id="suSpLogin"${sp.client_id ? '' : ' disabled'}>Войти через Spotify</button>`}</div></div><p class="hint" id="suSpMsg"></p>
      <p class="hint">Шаг можно пропустить: Spotify Connect (колонка в списке устройств приложения) работает и без этого</p>`;
  }
  if (id === 'ha') {
    const ha = d.ha;
    let haUrl = ha.addr || 'homeassistant.local:8123';      // the speaker tells the address that the owner gave to the installer
    if (!ha.addr) { try { haUrl = localStorage.getItem('haurl') || haUrl; } catch (e) {} }
    return `<h2>Home Assistant</h2><p class="hint">Необязательно. С ним колонка управляет умным домом. Нужен «долгосрочный токен»: это пароль для приложений, он покажется только один раз. Токен хранится в зашифрованном виде. Адрес Home Assistant колонка берёт из своего конфига (<code>forward=ha=…</code>), сервер туда сам не ходит</p>
      <div class="inset"><div class="row steps"><span class="k"><small>1. Откройте Home Assistant у себя дома (с телефона или компьютера в вашей сети)<br>2. Внизу слева нажмите на своё имя (профиль), вкладка «Безопасность»<br>3. Прокрутите вниз до «Долгосрочные токены доступа» и нажмите «Создать токен»<br>4. Назовите его lostOS, нажмите «ОК» и скопируйте длинную строку<br>5. Нажмите на строку ниже и вставьте её</small></span></div>
      <div class="row"><span class="k"><small>Ссылка на профиль<br><b style="font-size:15px;word-break:break-all">http://${esc(haUrl)}/profile/security</b></small></span><a class="mini-btn acc" href="http://${esc(haUrl)}/profile/security" target="_blank" rel="noopener">Открыть</a></div>
      ${ha.addr ? '' : suField('haurl', 'Адрес вашего Home Assistant', true, 'если не открывается, впишите свой', haUrl)}
      ${suField('HA_TOKEN', 'Токен', ha.token, 'нажмите и вставьте')}
      ${suField('SPEAKER_ROOM', 'Комната колонки', ha.room, 'где она стоит, как в Home Assistant')}
      <div class="row"><span class="k">Связь с Home Assistant<small>${ha.reachable ? 'колонка его видит' : 'колонка пока его не предлагает'}</small></span>${suChip(ha.configured && !ha.error, 'работает', ha.token ? (ha.error ? 'ошибка' : 'ждёт') : 'не настроен')}</div></div>
      <p class="hint" id="suHaMsg">${esc(ha.error)}</p>`;
  }
  if (id === 'city') {
    return `<h2>Город</h2><p class="hint">Для погоды и чтобы нейросеть понимала, где вы. Напишите город</p>
      <div class="inset"><div class="row"><input class="pill-in" id="suCity" type="text" autocomplete="off" placeholder="например, Москва" value="${esc(d.city ? d.city.name.split(',')[0] : '')}" style="max-width:none;text-align:left;flex:1"><button class="mini-btn acc" id="suCitySave">Найти</button></div></div>
      <p class="hint" id="suCityMsg">${d.city ? 'Сейчас: ' + esc(d.city.name) + suCityTime(d.city.tz) : 'Город пока не выбран'}</p>
      <p class="hint">По городу колонка и сервер узнают часовой пояс: будильники, напоминания и часы на колонке пойдут по местному времени</p>`;
  }
  if (id === 'voice') {
    return `<h2>Ваш голос</h2><p class="hint">Чтобы колонка узнавала вас по голосу, добавьте его. Скажите ей: «Алиса, запомни мой голос, меня зовут …». Сейчас голосов: <b>${d.voices}</b>. Остальное можно сделать позже на странице «Голоса»</p>
      <div class="row-btns"><button class="mini-btn acc" id="suVoices">Открыть «Голоса»</button></div>`;
  }
  const todo = [[d.station && d.station.confirmed, d.station && d.station.confirmed ? 'Колонка подключена' : 'Колонка не подключена'], [!!d.brain.selected, 'Нейросеть: ' + (d.brain.providers.find((p) => p.selected) || { title: 'не выбрана' }).title], [d.spotify.logged_in, 'Spotify'], [d.ha.configured, 'Home Assistant'], [!!d.city, 'Город'], [d.voices > 0, 'Голос']];
  return `<h2>Готово</h2><div class="inset">${todo.map(([ok, t]) => `<div class="row"><span class="k">${esc(t)}</span>${suChip(ok, 'готово', 'позже')}</div>`).join('')}</div>
    <p class="hint">Всё, что отмечено «позже», можно настроить в любое время: Настройки, «Первая настройка», «Ключи», «Плагины»</p>`;
}
function suRender(keepInput) {
  const d = SU.d;
  if (!d) return;
  if (keepInput && document.activeElement && document.activeElement.closest && document.activeElement.closest('#suBody') && /INPUT/.test(document.activeElement.tagName)) return;
  const [id, title] = SU_STEPS[SU.i];
  $('#suTitle').textContent = title;
  $('#suBody').innerHTML = suStepHtml(id, d);
  $('#suDots').innerHTML = SU_STEPS.map((_, k) => `<i class="${k === SU.i ? 'on' : k < SU.i ? 'past' : ''}"></i>`).join('');
  $('#suBack').hidden = SU.i <= (CW.stage === 'setup' ? 1 : 0);
  const last = SU.i === SU_STEPS.length - 1, linked = !!(d.station && d.station.confirmed);
  $('#suNext').textContent = last ? 'Готово' : 'Дальше';
  $('#suNext').disabled = !linked;                                    // the speaker is the one step that cannot be skipped
  $('#suNext').style.opacity = linked ? '' : '.35';
  $('#suSkip').hidden = !linked;
  if (!linked && SU.i !== 0) { SU.i = 0; suRender(); }
}
async function suFinish() {
  await suApi('/api/v1/setup', { done: true });
  SU.open = false; clearInterval(SU.timer);
  if (CW.stage === 'setup') { location.reload(); return; }              // the window closes: the panel opens
  closeSheet(); suLoad();
}
$('#suNext').addEventListener('click', () => { if (SU.i === SU_STEPS.length - 1) return suFinish(); SU.i++; SU.key = ''; suRender(); $('#sheetSetup .sheet-body').scrollTop = 0; });
$('#suBack').addEventListener('click', () => { if (SU.i > 0) { SU.i--; SU.key = ''; suRender(); $('#sheetSetup .sheet-body').scrollTop = 0; } });
$('#suSkip').addEventListener('click', async () => { if (await confirmAsk('Пропустить настройку?', 'Ключи, Spotify, умный дом и город можно задать позже в настройках панели', 'Пропустить')) suFinish(); });
$('#rowSetup').addEventListener('click', suOpen);
$('#sheetSetup').addEventListener('click', async (ev) => {
  const t = ev.target, d = SU.d, msg = (id, text, bad) => { const el = $(id); if (el) { el.textContent = text; el.className = 'hint' + (bad ? ' warn-text' : ''); } };
  const prov = t.closest('[data-suprov]');
  if (prov && !t.closest('.su-key')) { SU.key = SU.key === prov.dataset.suprov ? '' : prov.dataset.suprov; suRender(); return; }
  const save = t.closest('#suKeySave');
  if (save) {
    const value = $('#suKey').value.trim();
    if (!value) return msg('#suKeyMsg', 'Вставьте ключ', true);
    save.disabled = true; msg('#suKeyMsg', 'Сохраняю и проверяю…');
    const s = await suApi('/api/v1/secrets', { name: save.dataset.env, value });
    if (!s.ok) { save.disabled = false; return msg('#suKeyMsg', s.error || 'Не получилось сохранить', true); }
    const tst = await suApi('/api/v1/setup/test', { provider: save.dataset.prov });
    save.disabled = false;
    if (!tst.ok) { await suLoad(); return msg('#suKeyMsg', 'Ключ сохранён, но не работает: ' + (tst.error || 'нет ответа') + '. Проверьте его и вставьте заново', true); }
    await suApi('/api/v1/setup', { brain: save.dataset.prov });
    await suLoad(); SU.key = ''; suRender(); toast('Нейросеть работает'); return;
  }
  const use = t.closest('[data-suuse]');
  if (use) { await suApi('/api/v1/setup', { brain: use.dataset.suuse }); await suLoad(); SU.key = ''; suRender(); return; }
  const tool = t.closest('[data-sutool]');
  if (tool) {
    const on = !tool.classList.contains('on'); tool.classList.toggle('on', on);
    const r = await suApi('/api/v1/setup', { tool: tool.dataset.sutool, on });
    if (!r.ok) { tool.classList.toggle('on', !on); toast(r.error || 'Не получилось'); }
    return;
  }
  const sty = t.closest('[data-sustyle]');
  if (sty) { await suApi('/api/v1/setup', { style: sty.dataset.sustyle }); await suLoad(); suRender(); return; }
  const fld = t.closest('[data-sufield]');
  if (fld && !fld.querySelector('input')) {                            // the chip becomes the input, in the same line
    const [type, ph] = SU_FIELDS[fld.dataset.sufield] || ['text', ''];
    const slot = fld.querySelector('.su-val');
    slot.innerHTML = `<input class="pill-in su-in" type="${type}" autocomplete="off" placeholder="${esc(ph)}">`;
    const input = slot.firstChild; input.focus();
    let done = false;
    const finish = async (save) => {
      if (done) return; done = true;
      const v = input.value.trim(), name = fld.dataset.sufield;
      if (save && v) {
        if (name === 'claim') { await claimStation(v); }
        else if (name === 'haurl') { try { localStorage.setItem('haurl', v.replace(/^https?:\/\//, '').replace(/\/.*$/, '')); } catch (e) {} }
        else { const r = await suApi('/api/v1/secrets', { name, value: v }); if (!r.ok) toast(r.error || 'Не получилось сохранить'); else toast('Сохранено'); }
        await suLoad();
      }
      suRender();
    };
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') finish(true); else if (e.key === 'Escape') finish(false); });
    input.addEventListener('blur', () => finish(true));
    return;
  }
  if (t.closest('#suCopy')) { try { await navigator.clipboard.writeText(t.closest('#suCopy').dataset.copy); toast('Скопировано'); } catch (e) { toast('Скопируйте адрес вручную'); } return; }
  if (t.closest('#suSpLogin')) {
    const r = await suApi('/api/v1/spotify/login', {});
    if (r.ok && r.url) { try { localStorage.setItem('lostos.suStep', JSON.stringify({ id: SU_STEPS[SU.i][0], t: Date.now() })); } catch (e) { /* the first step is offered then */ } location.href = r.url; } else msg('#suSpMsg', r.error || 'Не получилось', true);
    return;
  }
  if (t.closest('#suCitySave')) {
    const v = $('#suCity').value.trim(); if (!v) return msg('#suCityMsg', 'Напишите город', true);
    msg('#suCityMsg', 'Ищу…'); const r = await suApi('/api/v1/setup', { city: v });
    if (r.ok) { await suLoad(); msg('#suCityMsg', 'Выбрано: ' + r.city.name + suCityTime(r.city.tz) + '. Часы колонки обновятся через несколько секунд'); S.tilesSig = ''; loadTiles(); } else msg('#suCityMsg', r.error || 'Не получилось', true);
    return;
  }
  if (t.closest('#suVoices')) {
    SU.open = false; clearInterval(SU.timer);
    if (CW.stage === 'setup') { await suApi('/api/v1/setup', { done: true }); try { sessionStorage.setItem('openVoices', '1'); } catch (e) { /* ignore */ } location.reload(); return; }
    await closeSheet(); go('llm'); pushSub('voices');
  }
});
// it opens by itself for the owner until it is finished (once per page load)
suLoad().then((d) => { if (d && !d.done && d.station && d.station.confirmed) cwSetupStart(); });          // not finished: the window of the first setup opens (only once in the life of a house)
try { if (sessionStorage.getItem('openVoices')) { sessionStorage.removeItem('openVoices'); setTimeout(() => { go('llm'); pushSub('voices'); }, 900); } } catch (e) { /* ignore */ }

})();
