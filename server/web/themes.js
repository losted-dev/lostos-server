/* lostOS panel themes: the look of the panel (iOS glass or Material Design 3), light/dark and the colour, kept per device.
   Material 3 colours are made from one seed colour as tonal palettes (tone = lightness L* of CIELAB, close to the HCT of Material). */
(function () {
  'use strict';
  const KEY = 'lostos.theme', DEFAULT = { style: 'ios', mode: 'dark', seed: '#8b5cf6', shape: '' };
  const PRESETS = [
    { id: 'ios', name: 'lostOS', hint: 'стекло, как в iOS', style: 'ios', mode: 'dark', seed: '#8b5cf6' },
    { id: 'ios-light', name: 'lostOS светлая', hint: 'стекло на светлом фоне', style: 'ios', mode: 'light', seed: '#7c3aed' },
    { id: 'm3-violet', name: 'Material 3', hint: 'фиолетовая, по системе', style: 'm3', mode: 'auto', seed: '#6750a4' },
    { id: 'm3-blue', name: 'Material 3 · Синяя', hint: 'по системе', style: 'm3', mode: 'auto', seed: '#1a73e8' },
    { id: 'm3-green', name: 'Material 3 · Зелёная', hint: 'по системе', style: 'm3', mode: 'auto', seed: '#2e7d32' },
    { id: 'm3-orange', name: 'Material 3 · Оранжевая', hint: 'по системе', style: 'm3', mode: 'auto', seed: '#e65100' },
    { id: 'm3-rose', name: 'Material 3 · Розовая', hint: 'по системе', style: 'm3', mode: 'auto', seed: '#c2185b' },
    { id: 'm3-night', name: 'Material 3 · Ночная', hint: 'тёмная, чёрный фон', style: 'm3', mode: 'dark', seed: '#6750a4', black: true },
  ];
  const SEEDS = ['#6750a4', '#1a73e8', '#00897b', '#2e7d32', '#f9a825', '#e65100', '#c2185b', '#8b5cf6'];
  const VARS = ['--shape-mask', '--shape-radius', '--shape-radius-s', '--bg', '--label', '--fg', '--sec', '--ter', '--sep', '--fill', '--group', '--glass', '--card', '--accent', '--on-accent', '--danger', '--green', '--sw-on',
    '--m3-primary-c', '--m3-on-primary-c', '--m3-secondary-c', '--m3-on-secondary-c', '--m3-surface-hi', '--m3-outline', '--m3-tertiary', '--m3-tertiary-c', '--m3-on-tertiary-c'];


  /* shapes of the Material shape library (m3.material.io/styles/shape): drawn as outlines in a 100 x 100 box, or as a rectangle with round corners */
  const TAU = Math.PI * 2;
  const radial = (fn, pts) => { pts = pts || 360; const o = []; for (let i = 0; i < pts; i++) { const a = TAU * i / pts, r = fn(a); o.push([50 + r * Math.cos(a), 50 + r * Math.sin(a)]); } return o; };
  const sgnpow = (c, p) => Math.sign(c) * Math.pow(Math.abs(c), p);
  const cookie = (n, a) => radial((t) => 47 * (1 + a * Math.cos(n * t + Math.PI)) / (1 + a));
  const star = (n, a, p) => radial((t) => 47 * (1 + a * sgnpow(Math.cos(n * t), p)) / (1 + a));
  const lobes = (n, k) => radial((t) => 47 * (k + (1 - k) * Math.pow(Math.abs(Math.cos(n * t / 2)), 0.65)));
  function rounded(v, rad) {                 // a polygon with rounded corners (quadratic curves)
    const o = [], n = v.length;
    for (let i = 0; i < n; i++) {
      const p = v[(i + n - 1) % n], c = v[i], q = v[(i + 1) % n], r = Array.isArray(rad) ? rad[i] : rad;
      const l1 = Math.hypot(c[0] - p[0], c[1] - p[1]), l2 = Math.hypot(q[0] - c[0], q[1] - c[1]);
      const d1 = Math.min(r, l1 / 2) / l1, d2 = Math.min(r, l2 / 2) / l2;
      const a = [c[0] + (p[0] - c[0]) * d1, c[1] + (p[1] - c[1]) * d1], b = [c[0] + (q[0] - c[0]) * d2, c[1] + (q[1] - c[1]) * d2];
      for (let s = 0; s <= 8; s++) { const u = s / 8, w = 1 - u; o.push([w * w * a[0] + 2 * w * u * c[0] + u * u * b[0], w * w * a[1] + 2 * w * u * c[1] + u * u * b[1]]); }
    }
    return o;
  }
  const regular = (n, R, rot, r) => rounded(Array.from({ length: n }, (_, i) => [50 + R * Math.cos(rot + TAU * i / n), 50 + R * Math.sin(rot + TAU * i / n)]), r);
  const SHAPES = [
    { id: 'circle', name: 'Circle', r: '50%', rs: '50%' },
    { id: 'square', name: 'Square', r: '28px', rs: '30%' },
    { id: 'slanted', name: 'Slanted', pts: () => rounded([[22, 14], [96, 14], [78, 86], [4, 86]], 12) },
    { id: 'arch', name: 'Arch', r: '9999px 9999px 22px 22px', rs: '50% 50% 14% 14%' },
    { id: 'oval', name: 'Oval', pts: () => radial((t) => { const x = Math.cos(t), y = Math.sin(t), c = Math.cos(-0.6), s = Math.sin(-0.6), u = x * c - y * s, w = x * s + y * c; return 47 / Math.hypot(u / 1, w / 0.68); }) },
    { id: 'pill', name: 'Pill', r: '9999px', rs: '50%' },
    { id: 'clamshell', name: 'Clamshell', pts: () => rounded([[26, 12], [74, 12], [98, 50], [74, 88], [26, 88], [2, 50]], 14) },
    { id: 'pentagon', name: 'Pentagon', pts: () => regular(5, 48, -Math.PI / 2, 12) },
    { id: 'gem', name: 'Gem', pts: () => regular(6, 47, -Math.PI / 2 + 0.2, 14) },
    { id: 'very-sunny', name: 'Very sunny', pts: () => star(8, .1, 0.5) },
    { id: 'sunny', name: 'Sunny', pts: () => star(8, .075, 0.4) },
    { id: 'cookie4', name: '4-sided cookie', pts: () => cookie(4, .2) },
    { id: 'cookie6', name: '6-sided cookie', pts: () => cookie(6, .09) },
    { id: 'cookie7', name: '7-sided cookie', pts: () => cookie(7, .075) },
    { id: 'cookie9', name: '9-sided cookie', pts: () => cookie(9, .06) },
    { id: 'cookie12', name: '12-sided cookie', pts: () => cookie(12, .045) },
    { id: 'clover4', name: '4-leaf clover', pts: () => lobes(8, .62) },
    { id: 'clover8', name: '8-leaf clover', pts: () => lobes(16, .8) },
    { id: 'soft-burst', name: 'Soft burst', pts: () => star(10, .16, 0.35) },
    { id: 'puffy', name: 'Puffy', pts: () => lobes(20, .84) },
  ];
  const shapeOf = (id) => SHAPES.find((s) => s.id === id);
  const maskCache = {};
  function maskOf(s) {
    if (!s.pts) return 'none';
    if (maskCache[s.id]) return maskCache[s.id];
    const d = 'M' + s.pts().map((p) => p[0].toFixed(1) + ',' + p[1].toFixed(1)).join('L') + 'Z';
    const svg = "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100' preserveAspectRatio='none'><path d='" + d + "'/></svg>";
    return (maskCache[s.id] = 'url("data:image/svg+xml,' + encodeURIComponent(svg).replace(/'/g, '%27') + '")');
  }

  const hexOk = (h) => /^#[0-9a-f]{6}$/i.test(h || '');
  const hex = (rgb) => '#' + rgb.map((v) => Math.round(Math.min(1, Math.max(0, v)) * 255).toString(16).padStart(2, '0')).join('');
  function lch2rgb(L, C, h) {
    const a = C * Math.cos(h * Math.PI / 180), b = C * Math.sin(h * Math.PI / 180);
    const fy = (L + 16) / 116, fx = fy + a / 500, fz = fy - b / 200;
    const f = (t) => (t * t * t > 0.008856 ? t * t * t : (t - 16 / 116) / 7.787);
    const X = 0.95047 * f(fx), Y = f(fy), Z = 1.08883 * f(fz);
    const lin = [3.2406 * X - 1.5372 * Y - 0.4986 * Z, -0.9689 * X + 1.8758 * Y + 0.0415 * Z, 0.0557 * X - 0.204 * Y + 1.057 * Z];
    return lin.map((c) => (c <= 0.0031308 ? 12.92 * c : 1.055 * Math.pow(Math.max(c, 0), 1 / 2.4) - 0.055));
  }
  const tone = (h, C, T) => {
    for (let c = C; c > 0; c -= 1) { const rgb = lch2rgb(T, c, h); if (rgb.every((v) => v >= -0.002 && v <= 1.002)) return hex(rgb); }
    return hex(lch2rgb(T, 0, h));
  };
  function hueOf(h) {                       // the hue and the chroma of a colour in CIELAB
    const c = [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16) / 255).map((v) => (v <= 0.04045 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4)));
    const X = (0.4124 * c[0] + 0.3576 * c[1] + 0.1805 * c[2]) / 0.95047, Y = 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2], Z = (0.0193 * c[0] + 0.1192 * c[1] + 0.9505 * c[2]) / 1.08883;
    const f = (t) => (t > 0.008856 ? Math.cbrt(t) : 7.787 * t + 16 / 116);
    const a = 500 * (f(X) - f(Y)), b = 200 * (f(Y) - f(Z));
    return { h: (Math.atan2(b, a) * 180 / Math.PI + 360) % 360, c: Math.hypot(a, b) };
  }
  const rgbOf = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16)).join(',');

  function m3(seed, dark, black) {          // Material 3 scheme (tonal spot) from the seed colour
    const s = hueOf(seed), gray = s.c < 8;
    const P = (T) => tone(s.h, gray ? 0 : 44, T), S = (T) => tone(s.h, gray ? 0 : 16, T), Tt = (T) => tone((s.h + 60) % 360, gray ? 0 : 26, T);
    const N = (T) => tone(s.h, gray ? 0 : 3, T), V = (T) => tone(s.h, gray ? 0 : 7, T), E = (T) => tone(25, 70, T);
    return dark ? {
      primary: P(80), onPrimary: P(20), primaryContainer: P(30), onPrimaryContainer: P(90), secondary: S(80), secondaryContainer: S(30), onSecondaryContainer: S(90), tertiary: Tt(80),
      surface: black ? '#000000' : N(6), container: black ? N(10) : N(12), containerHigh: black ? N(14) : N(17), containerHighest: black ? N(19) : N(22), onSurface: N(90), onSurfaceVariant: V(80),
      outline: V(60), outlineVariant: V(30), error: E(80),
      onSecondary: S(20), tertiaryContainer: Tt(30), onTertiaryContainer: Tt(90), onTertiary: Tt(20), onError: E(20), errorContainer: E(30), onErrorContainer: E(90),
      thirdC: tone(s.h, 30, 26), onThirdC: tone(s.h, 30, 90),
      low: black ? N(6) : N(10), lowest: black ? '#000000' : N(4), inverseSurface: N(90), inverseOnSurface: N(20), inversePrimary: P(40),
    } : {
      primary: P(40), onPrimary: '#ffffff', primaryContainer: P(90), onPrimaryContainer: P(10), secondary: S(40), secondaryContainer: S(90), onSecondaryContainer: S(10), tertiary: Tt(40),
      surface: N(98), container: N(94), containerHigh: N(92), containerHighest: N(90), onSurface: N(10), onSurfaceVariant: V(30), outline: V(50), outlineVariant: V(80), error: E(40),
      onSecondary: '#ffffff', tertiaryContainer: Tt(90), onTertiaryContainer: Tt(10), onTertiary: '#ffffff', onError: '#ffffff', errorContainer: E(90), onErrorContainer: E(10),
      thirdC: tone(s.h, 30, 86), onThirdC: tone(s.h, 30, 12),
      low: N(96), lowest: '#ffffff', inverseSurface: N(20), inverseOnSurface: N(95), inversePrimary: P(80),
    };
  }

  const MDV = ['primary', 'on-primary', 'primary-container', 'on-primary-container', 'secondary', 'on-secondary', 'secondary-container', 'on-secondary-container', 'tertiary', 'on-tertiary',
    'tertiary-container', 'on-tertiary-container', 'error', 'on-error', 'error-container', 'on-error-container', 'background', 'on-background', 'surface', 'on-surface', 'surface-variant',
    'on-surface-variant', 'outline', 'outline-variant', 'surface-container', 'surface-container-low', 'surface-container-high', 'surface-container-highest', 'surface-container-lowest',
    'inverse-surface', 'inverse-on-surface', 'inverse-primary', 'surface-tint', 'scrim', 'shadow', 'surface-dim', 'surface-bright'].map((n) => '--md-sys-color-' + n);
  function mdTokens(c) {                     // what the components of Material Web (the official library, mdweb.js) read
    const m = {
      primary: c.primary, 'on-primary': c.onPrimary, 'primary-container': c.primaryContainer, 'on-primary-container': c.onPrimaryContainer, secondary: c.secondary, 'on-secondary': c.onSecondary,
      'secondary-container': c.secondaryContainer, 'on-secondary-container': c.onSecondaryContainer, tertiary: c.tertiary, 'on-tertiary': c.onTertiary, 'tertiary-container': c.tertiaryContainer,
      'on-tertiary-container': c.onTertiaryContainer, error: c.error, 'on-error': c.onError, 'error-container': c.errorContainer, 'on-error-container': c.onErrorContainer, background: c.surface,
      'on-background': c.onSurface, surface: c.surface, 'on-surface': c.onSurface, 'surface-variant': c.containerHighest, 'on-surface-variant': c.onSurfaceVariant, outline: c.outline,
      'outline-variant': c.outlineVariant, 'surface-container': c.container, 'surface-container-low': c.low, 'surface-container-high': c.containerHigh, 'surface-container-highest': c.containerHighest,
      'surface-container-lowest': c.lowest, 'inverse-surface': c.inverseSurface, 'inverse-on-surface': c.inverseOnSurface, 'inverse-primary': c.inversePrimary, 'surface-tint': c.primary,
      scrim: '#000000', shadow: '#000000', 'surface-dim': c.surface, 'surface-bright': c.containerHigh,
    }, o = {};
    Object.keys(m).forEach((k) => { o['--md-sys-color-' + k] = m[k]; });
    return o;
  }

  function vars(t, dark) {                  // the CSS variables of the panel for a theme
    const seed = hexOk(t.seed) ? t.seed : DEFAULT.seed;
    if (t.style === 'm3') {
      const c = m3(seed, dark, !!t.black);
      return Object.assign(mdTokens(c), {
        '--bg': c.surface, '--label': c.onSurface, '--fg': rgbOf(c.onSurface), '--sec': c.onSurfaceVariant, '--ter': c.outline, '--sep': c.outlineVariant, '--fill': c.containerHighest,
        '--group': c.container, '--glass': c.containerHigh, '--card': c.containerHigh, '--accent': c.primary, '--on-accent': c.onPrimary, '--danger': c.error, '--green': c.tertiary, '--sw-on': c.primary,
        '--m3-primary-c': c.primaryContainer, '--m3-on-primary-c': c.onPrimaryContainer, '--m3-secondary-c': c.secondaryContainer, '--m3-on-secondary-c': c.onSecondaryContainer,
        '--m3-surface-hi': c.containerHighest, '--m3-outline': c.outline, '--m3-tertiary': c.tertiary, '--m3-tertiary-c': c.thirdC, '--m3-on-tertiary-c': c.onThirdC,
      });
    }
    const accent = seed.toLowerCase() === DEFAULT.seed ? null : seed;
    const v = dark ? {} : {
      '--bg': '#f2f2f7', '--label': '#000000', '--fg': '0,0,0', '--sec': 'rgba(60,60,67,.62)', '--ter': 'rgba(60,60,67,.32)', '--sep': 'rgba(60,60,67,.2)', '--fill': 'rgba(120,120,128,.16)',
      '--group': 'rgba(255,255,255,.92)', '--glass': 'rgba(255,255,255,.62)', '--card': '#ffffff', '--danger': '#ff3b30',
    };
    if (accent) v['--accent'] = accent;
    return v;
  }

  let theme = Object.assign({}, DEFAULT), mq = null;
  const isDark = (t) => (t.mode === 'dark' ? true : t.mode === 'light' ? false : !(mq && !mq.matches));

  function apply(t) {
    t = Object.assign({}, DEFAULT, t || {}); theme = t;
    const root = document.documentElement, dark = isDark(t), v = vars(t, dark);
    VARS.concat(MDV).forEach((k) => root.style.removeProperty(k));
    Object.keys(v).forEach((k) => root.style.setProperty(k, v[k]));
    root.dataset.style = t.style; const sh = t.shape && shapeOf(t.shape);
    if (sh) { root.dataset.shape = sh.id; root.style.setProperty('--shape-mask', maskOf(sh)); root.style.setProperty('--shape-radius', sh.pts ? '0' : sh.r); root.style.setProperty('--shape-radius-s', sh.pts ? '0' : sh.rs); } else delete root.dataset.shape; root.dataset.mode = dark ? 'dark' : 'light';
    root.style.colorScheme = dark ? 'dark' : 'light';
    const meta = document.querySelector('meta[name=theme-color]'); if (meta) meta.content = v['--bg'] || '#000000';
    try { window.dispatchEvent(new CustomEvent('lostos-theme', { detail: t })); } catch (e) { /* ignore */ }
  }
  const empty = () => ({ cur: null, id: 'ios', custom: [] });
  const read = () => { try { const j = JSON.parse(localStorage.getItem(KEY) || 'null'); return j && typeof j === 'object' ? j : empty(); } catch (e) { return empty(); } };
  const write = (st) => { try { localStorage.setItem(KEY, JSON.stringify(st)); } catch (e) { /* ignore */ } };
  function clean(t) {                       // what is allowed to come from an import
    const o = { style: t.style === 'm3' ? 'm3' : 'ios', mode: ['auto', 'light', 'dark'].includes(t.mode) ? t.mode : 'auto', seed: hexOk(t.seed) ? t.seed.toLowerCase() : DEFAULT.seed };
    o.shape = '';
    if (t.black) o.black = true;
    return o;
  }

  try { mq = matchMedia('(prefers-color-scheme: dark)'); mq.addEventListener('change', () => { if (theme.mode === 'auto') apply(theme); }); } catch (e) { /* ignore */ }
  apply(read().cur || DEFAULT);

  window.LTheme = {
    PRESETS, SEEDS, SHAPES, maskOf,
    state: () => { const st = read(); return { cur: Object.assign({}, DEFAULT, st.cur || {}), id: st.id || '', custom: st.custom || [] }; },
    set(t, id) { const st = read(); st.cur = clean(t); st.id = id || ''; write(st); apply(st.cur); },
    save(name, t) { const st = read(); st.custom = (st.custom || []).slice(0, 29); const id = 'u' + Date.now().toString(36); st.custom.push(Object.assign({ id, name: String(name).slice(0, 40) }, clean(t))); st.cur = clean(t); st.id = id; write(st); return id; },
    remove(id) { const st = read(); st.custom = (st.custom || []).filter((x) => x.id !== id); if (st.id === id) st.id = ''; write(st); },
    exportJSON: (t) => JSON.stringify(Object.assign({ lostos_theme: 1 }, clean(t))),
    parse(s) { try { const j = JSON.parse(s); return j && j.lostos_theme ? clean(j) : null; } catch (e) { return null; } },
    preview: (t) => vars(Object.assign({}, DEFAULT, t), isDark(Object.assign({}, DEFAULT, t))),
    isDark,
  };
})();
