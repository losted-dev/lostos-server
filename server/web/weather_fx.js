"use strict";
/* The sky of the weather widget: the weather itself is drawn behind the temperature, like the weather of Apple does it.
   clear day: the sun glows in a blue sky; clear night: stars twinkle; clouds drift; rain falls in slanting streaks; a thunderstorm flashes;
   snow sways down; fog drifts in layers. Only the card that is on the page is animated, and nothing moves while the page is hidden. */
window.lostWeatherFx = (() => {
  const rnd = (a, b) => a + Math.random() * (b - a);
  const SKY = {
    'clear-day': ['#5bb3ff', '#1d6fdc'], 'clear-night': ['#0a1747', '#34509e'],
    'partly-day': ['#69adf4', '#2b69c2'], 'partly-night': ['#0d1a49', '#2c4385'],
    'cloud-day': ['#92a3bb', '#566580'], 'cloud-night': ['#2a3347', '#131a29'],
    'rain-day': ['#6a7a92', '#2b3546'], 'rain-night': ['#1d2636', '#0b101b'],
    'storm-day': ['#41396a', '#14112a'], 'storm-night': ['#2d2750', '#0c0a1b'],
    'snow-day': ['#b6c6db', '#7186a3'], 'snow-night': ['#3c4b68', '#1b2338'],
    'fog-day': ['#b0aba1', '#716e66'], 'fog-night': ['#41434b', '#1c1d23'],
  };
  const sky = (kind, night) => SKY[kind + '-' + (night ? 'night' : 'day')] || SKY['cloud-day'];

  // the colour of the clouds of this weather (r,g,b): white on a clear day, grey in the rain, dark in a thunderstorm, dim at night
  const cloudRgb = (kind, night) => (kind === 'storm' ? (night ? '86,80,128' : '108,102,148') : kind === 'rain' ? (night ? '98,112,142' : '156,168,188')
    : kind === 'snow' ? (night ? '156,170,200' : '246,249,255') : kind === 'fog' ? (night ? '146,148,156' : '238,236,230')
    : night ? '150,164,198' : kind === 'cloud' ? '236,241,248' : '255,255,255');
  function cloud(bg, x, y, w, h, alpha, dur, delay) {   // a soft cloud: blurred blobs that drift across the card and come round again
    const c = document.createElement('div');
    c.className = 'wx-cloud';
    c.style.cssText = `left:${x}%;top:${y}%;width:${w}px;height:${h}px;opacity:${alpha};animation-duration:${dur}s;animation-delay:${-delay}s`;
    bg.appendChild(c);
  }
  function addClouds(bg, kind, night) {
    bg.style.setProperty('--cloud', cloudRgb(kind, night));
    const dim = night ? 0.5 : 1;
    if (kind === 'partly') { cloud(bg, 5, 12, 150, 46, 0.55 * dim, 46, 8); cloud(bg, 40, 30, 190, 56, 0.4 * dim, 62, 30); }
    else if (kind === 'cloud') { for (let i = 0; i < 5; i++) cloud(bg, rnd(-10, 70), rnd(0, 55), rnd(150, 230), rnd(46, 70), (night ? 0.28 : 0.5) + rnd(0, 0.2), rnd(50, 90), rnd(0, 60)); }
    else if (kind === 'rain' || kind === 'storm') { for (let i = 0; i < 4; i++) cloud(bg, rnd(-10, 70), rnd(-8, 25), rnd(170, 250), rnd(60, 90), (kind === 'storm' ? 0.42 : 0.34) + rnd(0, 0.15), rnd(55, 95), rnd(0, 60)); }
    else if (kind === 'snow') { for (let i = 0; i < 3; i++) cloud(bg, rnd(-10, 70), rnd(-6, 25), rnd(170, 240), rnd(56, 80), 0.4 + rnd(0, 0.15), rnd(60, 100), rnd(0, 60)); }
    else if (kind === 'fog') { for (let i = 0; i < 4; i++) { const f = document.createElement('div'); f.className = 'wx-fog'; f.style.cssText = `top:${15 + i * 21}%;animation-duration:${34 + i * 13}s;animation-delay:${-i * 9}s;opacity:${0.22 + i * 0.05}`; bg.appendChild(f); } }
    if ((kind === 'clear' || kind === 'partly') && !night) { const g = document.createElement('div'); g.className = 'wx-sun'; bg.appendChild(g); }
  }

  function start(card) {
    const bg = card.querySelector('.wx-bg');
    if (!bg || bg.dataset.fx) return;
    bg.dataset.fx = '1';
    const kind = bg.dataset.kind, night = bg.dataset.night === '1';
    addClouds(bg, kind, night);
    const cv = bg.querySelector('canvas'), ctx = cv.getContext('2d');
    const still = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    let W = 0, H = 0, dpr = 1, items = [];
    const bolt = { next: performance.now() + rnd(2500, 6000), flash: 0, until: 0, pts: [] };

    const make = () => {   // the particles of this weather
      items = [];
      const n = kind === 'clear' ? (night ? 80 : 0) : kind === 'partly' ? (night ? 45 : 0) : kind === 'cloud' ? (night ? 16 : 0) : kind === 'rain' ? 90 : kind === 'storm' ? 150 : kind === 'snow' ? 60 : 0;
      for (let i = 0; i < n; i++) {
        if (kind === 'rain' || kind === 'storm') items.push({ x: rnd(-20, W + 20), y: rnd(0, H), len: rnd(9, 22), v: rnd(520, 900), a: rnd(0.22, 0.55) });
        else if (kind === 'snow') items.push({ x: rnd(0, W), y: rnd(0, H), r: rnd(1, 3.4), v: rnd(18, 55), ph: rnd(0, 6.3), sw: rnd(8, 22), a: rnd(0.55, 0.95) });
        else items.push({ x: rnd(0, W), y: rnd(0, H * 0.8), r: rnd(0.4, 1.25), ph: rnd(0, 6.3), sp: rnd(0.6, 2.2), a: rnd(0.35, 0.95) });   // a star
      }
    };
    const size = () => {
      const r = card.getBoundingClientRect();
      if (!r.width) return;
      W = r.width; H = r.height; dpr = Math.min(window.devicePixelRatio || 1, 2);
      cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      make();
    };
    size();
    if (window.ResizeObserver) new ResizeObserver(size).observe(card);

    const strike = (now) => {   // a jagged line from the clouds down, it flashes twice
      const pts = []; let x = rnd(W * 0.25, W * 0.75), y = 0;
      while (y < H * 0.62) { pts.push([x, y]); x += rnd(-16, 16); y += rnd(14, 30); }
      bolt.pts = pts; bolt.until = now + 260; bolt.flash = 1; bolt.again = now + 150;
    };

    let last = performance.now();
    const frame = (now) => {
      if (!card.isConnected) return;                       // the card was replaced: this loop ends with it
      if (!still) requestAnimationFrame(frame);
      if (document.hidden || now - last < 30) return;     // about 30 pictures a second are enough for rain, and kinder to the battery
      const dt = Math.min(0.1, (now - last) / 1000);
      last = now;
      ctx.clearRect(0, 0, W, H);
      const t = now / 1000;
      if (kind === 'rain' || kind === 'storm') {
        ctx.lineCap = 'round';
        for (const d of items) {
          d.y += d.v * dt; d.x -= d.v * dt * 0.22;
          if (d.y > H + 20) { d.y = rnd(-40, -5); d.x = rnd(-20, W + 40); }
          ctx.strokeStyle = `rgba(222,236,255,${Math.min(1, d.a + 0.25)})`; ctx.lineWidth = d.len > 16 ? 1.7 : 1.3;
          ctx.beginPath(); ctx.moveTo(d.x, d.y); ctx.lineTo(d.x + d.len * 0.22, d.y - d.len); ctx.stroke();
        }
        if (kind === 'storm') {
          if (now > bolt.next) { strike(now); bolt.next = now + rnd(3500, 9000); }
          if (bolt.flash > 0) {
            if (bolt.again && now > bolt.again) { bolt.flash = 1; bolt.again = 0; }
            ctx.fillStyle = `rgba(210,220,255,${0.4 * bolt.flash})`; ctx.fillRect(0, 0, W, H);
            bolt.flash = Math.max(0, bolt.flash - dt * 4.5);
          }
          if (now < bolt.until && bolt.pts.length) {
            ctx.strokeStyle = 'rgba(255,255,255,0.95)'; ctx.shadowColor = '#b9c8ff'; ctx.shadowBlur = 14; ctx.lineWidth = 2; ctx.lineJoin = 'round';
            ctx.beginPath(); bolt.pts.forEach((p, i) => (i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1]))); ctx.stroke(); ctx.shadowBlur = 0;
          }
        }
      } else if (kind === 'snow') {
        for (const f of items) {
          f.y += f.v * dt;
          if (f.y > H + 6) { f.y = -6; f.x = rnd(0, W); }
          const x = f.x + Math.sin(t * 0.9 + f.ph) * f.sw;
          ctx.fillStyle = `rgba(255,255,255,${f.a})`; ctx.beginPath(); ctx.arc(x, f.y, f.r, 0, 6.2832); ctx.fill();
        }
      } else if (items.length) {   // the stars
        for (const s of items) {
          const tw = 0.55 + 0.45 * Math.sin(t * s.sp + s.ph);
          ctx.fillStyle = `rgba(255,255,255,${s.a * tw})`; ctx.beginPath(); ctx.arc(s.x, s.y, s.r, 0, 6.2832); ctx.fill();
        }
      }
    };
    if (still) { last = -1e9; frame(performance.now()); } else requestAnimationFrame(frame);
  }
  return { sky, start };
})();
