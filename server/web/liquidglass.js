/* Liquid Glass for Chromium/WebView2: physically based refraction (Snell's law on a bezel height
 * profile) rendered as SVG displacement + specular maps and applied with backdrop-filter.
 * Method follows the well-known "Liquid Glass in the browser" approach (kube.io) as implemented in
 * the demos listed on freefrontend.com/css-liquid-glass. */
(function () {
  const Surface = {
    convex_circle: (x) => Math.sqrt(1 - Math.pow(1 - x, 2)),
    convex_squircle: (x) => Math.pow(1 - Math.pow(1 - x, 4), 1 / 4),
    concave: (x) => 1 - Math.sqrt(1 - Math.pow(x, 2)),
    lip: (x) => {
      const convex = Math.pow(1 - Math.pow(1 - Math.min(x * 2, 1), 4), 1 / 4);
      const concave = 1 - Math.sqrt(1 - Math.pow(1 - x, 2)) + 0.1;
      const s = 6 * Math.pow(x, 5) - 15 * Math.pow(x, 4) + 10 * Math.pow(x, 3);
      return convex * (1 - s) + concave * s;
    },
  };

  // displacement along one radius of the bezel using Snell's law
  function displacement1D(glassThickness, bezelWidth, surfaceFn, refractiveIndex, samples = 128) {
    const eta = 1 / refractiveIndex;
    const refract = (nx, ny) => {
      const dot = ny, k = 1 - eta * eta * (1 - dot * dot);
      if (k < 0) return null;
      const ks = Math.sqrt(k);
      return [-(eta * dot + ks) * nx, eta - (eta * dot + ks) * ny];
    };
    const out = [];
    for (let i = 0; i < samples; i++) {
      const x = i / samples, y = surfaceFn(x);
      const dx = x < 1 ? 0.0001 : -0.0001;
      const y2 = surfaceFn(Math.max(0, Math.min(1, x + dx)));
      const derivative = (y2 - y) / dx;
      const mag = Math.sqrt(derivative * derivative + 1);
      const r = refract(-derivative / mag, -1 / mag);
      out.push(r ? r[0] * ((y * bezelWidth + glassThickness) / r[1]) : 0);
    }
    return out;
  }

  function displacement2D(w, h, radius, bezel, maxDisp, map) {
    const img = new ImageData(w, h), d = img.data;
    for (let i = 0; i < d.length; i += 4) { d[i] = 128; d[i + 1] = 128; d[i + 2] = 0; d[i + 3] = 255; }
    const r2 = radius * radius, rp2 = (radius + 1) * (radius + 1);
    const rb2 = Math.max(0, (radius - bezel) * (radius - bezel));
    const wb = w - radius * 2, hb = h - radius * 2;
    for (let y1 = 0; y1 < h; y1++) {
      for (let x1 = 0; x1 < w; x1++) {
        const x = x1 < radius ? x1 - radius : x1 >= w - radius ? x1 - radius - wb : 0;
        const y = y1 < radius ? y1 - radius : y1 >= h - radius ? y1 - radius - hb : 0;
        const dc2 = x * x + y * y;
        if (dc2 > rp2 || dc2 < rb2) continue;
        const dc = Math.sqrt(dc2);
        const opacity = dc2 < r2 ? 1 : 1 - (dc - radius) / (Math.sqrt(rp2) - radius);
        const ratio = Math.max(0, Math.min(1, (radius - dc) / bezel));
        const dist = map[Math.max(0, Math.min(Math.floor(ratio * map.length), map.length - 1))] || 0;
        const cos = dc > 0 ? x / dc : 0, sin = dc > 0 ? y / dc : 0;
        const dX = maxDisp > 0 ? (-cos * dist) / maxDisp : 0, dY = maxDisp > 0 ? (-sin * dist) / maxDisp : 0;
        const idx = (y1 * w + x1) * 4;
        d[idx] = Math.max(0, Math.min(255, 128 + dX * 127 * opacity));
        d[idx + 1] = Math.max(0, Math.min(255, 128 + dY * 127 * opacity));
      }
    }
    return img;
  }

  function specular2D(w, h, radius, angle = Math.PI / 3, thickness = 1.5) {
    const img = new ImageData(w, h), d = img.data;
    const sv = [Math.cos(angle), Math.sin(angle)];
    const r2 = radius * radius, rp2 = (radius + 1) * (radius + 1);
    const rs2 = Math.max(0, (radius - thickness) * (radius - thickness));
    const wb = w - radius * 2, hb = h - radius * 2;
    for (let y1 = 0; y1 < h; y1++) {
      for (let x1 = 0; x1 < w; x1++) {
        const x = x1 < radius ? x1 - radius : x1 >= w - radius ? x1 - radius - wb : 0;
        const y = y1 < radius ? y1 - radius : y1 >= h - radius ? y1 - radius - hb : 0;
        const dc2 = x * x + y * y;
        if (dc2 > rp2 || dc2 < rs2) continue;
        const dc = Math.sqrt(dc2);
        const opacity = dc2 < r2 ? 1 : 1 - (dc - radius) / (Math.sqrt(rp2) - radius);
        const cos = dc > 0 ? x / dc : 0, sin = dc > 0 ? -y / dc : 0;
        const dot = Math.abs(cos * sv[0] + sin * sv[1]);
        const edge = Math.max(0, Math.min(1, (radius - dc) / thickness));
        const coeff = dot * Math.sqrt(1 - (1 - edge) * (1 - edge));
        const c = Math.min(255, 255 * coeff), idx = (y1 * w + x1) * 4;
        d[idx] = d[idx + 1] = d[idx + 2] = c;
        d[idx + 3] = Math.min(255, c * coeff * opacity);
      }
    }
    return img;
  }

  const toURL = (img) => {
    const c = document.createElement("canvas"); c.width = img.width; c.height = img.height;
    c.getContext("2d").putImageData(img, 0, 0); return c.toDataURL();
  };

  const svgNS = "http://www.w3.org/2000/svg";
  let defs = null, counter = 0;
  function getDefs() {
    if (defs) return defs;
    const svg = document.createElementNS(svgNS, "svg");
    svg.setAttribute("width", "0"); svg.setAttribute("height", "0");
    svg.style.cssText = "position:absolute;pointer-events:none";
    defs = document.createElementNS(svgNS, "defs"); svg.appendChild(defs);
    document.body.appendChild(svg); return defs;
  }

  const DEFAULTS = { surface: "convex_squircle", thickness: 110, n: 1.5, scale: 1.3, specular: 0.9, blur: 0.6, saturate: 1.35, bezel: null };

  function build(el, o) {
    const r = el.getBoundingClientRect();
    const w = Math.round(r.width), h = Math.round(r.height);
    if (w < 4 || h < 4) return;
    const cs = getComputedStyle(el);
    let radius = parseFloat(cs.borderTopLeftRadius) || 0;
    radius = Math.max(2, Math.min(radius, w / 2, h / 2));
    const bezel = Math.max(2, Math.min(o.bezel != null ? o.bezel : radius * 0.85, radius));
    const key = [w, h, radius, bezel, o.surface, o.thickness, o.n, o.scale, o.specular, o.blur, o.saturate].join("|");
    if (el._lgKey === key) return;
    el._lgKey = key;

    const map = displacement1D(o.thickness, bezel, Surface[o.surface], o.n);
    const maxDisp = Math.max(...map.map(Math.abs)) || 1;
    const dispURL = toURL(displacement2D(w, h, radius, bezel, maxDisp, map));
    const specURL = toURL(specular2D(w, h, radius));

    const id = el._lgId || (el._lgId = "lg" + ++counter);
    let f = document.getElementById(id);
    if (!f) { f = document.createElementNS(svgNS, "filter"); f.id = id; getDefs().appendChild(f); }
    f.setAttribute("x", "-50%"); f.setAttribute("y", "-50%"); f.setAttribute("width", "200%"); f.setAttribute("height", "200%");
    f.setAttribute("color-interpolation-filters", "sRGB");
    f.innerHTML =
      `<feGaussianBlur in="SourceGraphic" stdDeviation="${o.blur}" result="b"/>` +
      `<feImage href="${dispURL}" x="0" y="0" width="${w}" height="${h}" result="dm" preserveAspectRatio="none"/>` +
      `<feDisplacementMap in="b" in2="dm" scale="${maxDisp * o.scale}" xChannelSelector="R" yChannelSelector="G" result="d"/>` +
      `<feColorMatrix in="d" type="saturate" values="${o.saturate}" result="ds"/>` +
      `<feImage href="${specURL}" x="0" y="0" width="${w}" height="${h}" result="sp" preserveAspectRatio="none"/>` +
      `<feComponentTransfer in="sp" result="spf"><feFuncA type="linear" slope="${o.specular}"/></feComponentTransfer>` +
      `<feBlend in="spf" in2="ds" mode="screen"/>`;
    el.style.backdropFilter = `url(#${id})`;
    el.style.webkitBackdropFilter = `url(#${id})`;
  }

  const registry = new Map();
  const ro = new ResizeObserver((entries) => {
    for (const e of entries) {
      const o = registry.get(e.target);
      if (!o) continue;
      clearTimeout(e.target._lgT);
      e.target._lgT = setTimeout(() => build(e.target, o), 60);
    }
  });

  window.LiquidGlass = {
    apply(el, opts) {
      const o = Object.assign({}, DEFAULTS, opts || {});
      registry.set(el, o); ro.observe(el); build(el, o);
    },
    attach(selector = "[data-glass]") {
      document.querySelectorAll(selector).forEach((el) => {
        const d = el.dataset;
        const o = {};
        for (const k of ["thickness", "n", "scale", "specular", "blur", "saturate", "bezel"]) if (d["lg" + k[0].toUpperCase() + k.slice(1)] != null) o[k] = parseFloat(d["lg" + k[0].toUpperCase() + k.slice(1)]);
        if (d.lgSurface) o.surface = d.lgSurface;
        window.LiquidGlass.apply(el, o);
      });
    },
  };
})();
