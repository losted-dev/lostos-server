/* Safe Markdown for the README of a plugin. The text of a plugin is NOT trusted, so this renderer never builds HTML from it:
 * every element is made with createElement and every piece of text with createTextNode / textContent.
 *   - raw HTML is shown as plain text, never parsed;
 *   - links: only http(s) and mailto (after removing the characters a browser ignores inside a scheme: "java\tscript:"), always with
 *     rel="noopener noreferrer nofollow", opened in a new tab, the real address in the tooltip;
 *   - pictures: only files of the plugin itself (png, jpg, webp, gif; no svg, no data:, no remote address, no "..").
 *     A remote picture is not loaded (it would tell its owner who reads the page); its description is shown instead;
 *   - no ids, no classes or styles from the text (no DOM clobbering, no CSS injection);
 *   - limits on the size, the number of blocks, the depth of nesting and the length of formatted text (no pages that hang the panel).
 * lostMd.render(text, assetsBase) -> DocumentFragment */
(() => {
  'use strict';
  const MAX_LEN = 100000, MAX_BLOCKS = 1500, MAX_DEPTH = 4, MAX_INLINE = 4000, MAX_CODE_LINES = 400;
  const SCHEME_OK = /^(https?:\/\/|mailto:)/i;
  const IMG_EXT = /\.(png|jpe?g|webp|gif)$/i;
  // characters that a browser drops while it reads a URL, plus invisible ones that make an address look different from what it is
  const INVISIBLE = /[\u0000-\u0020\u007f-\u009f\u00ad\u200b-\u200f\u2028\u2029\u202a-\u202e\u2060\ufeff]/g;

  const el = (tag, cls) => { const e = document.createElement(tag); if (cls) e.className = cls; return e; };
  const text = (s) => document.createTextNode(s);

  function safeLink(raw) {
    const u = String(raw).replace(INVISIBLE, '');
    return u.length <= 2000 && SCHEME_OK.test(u) ? u : null;
  }
  function safeImagePath(raw) {
    const u = String(raw).replace(INVISIBLE, '');
    if (!u || u.length > 300 || /^[a-z][a-z0-9+.-]*:/i.test(u) || u.startsWith('/') || u.startsWith('\\') || u.includes('?') || u.includes('#') || u.includes('%') || u.includes('\\')) return null;
    const parts = u.replace(/^\.\//, '').split('/');
    if (parts.some((p) => p === '' || p === '.' || p === '..') || parts[0].toLowerCase() === 'data' || !IMG_EXT.test(parts[parts.length - 1])) return null;
    return parts.map(encodeURIComponent).join('/');
  }

  /* ---------------------------------------------------------------- inline */
  const SPECIAL = /[\\`*_[!<~]/g;
  const PUNCT = '\\`*_{}[]()#+-.!|<>~"\'&:;=@^/$%,?';

  function matchBracket(s, i, open, close) {      // the index of the bracket that closes the one at i (nesting counted), within a sane distance
    let depth = 0;
    for (let j = i; j < s.length && j < i + 1200; j++) {
      if (s[j] === '\\') { j++; continue; }
      if (s[j] === open) depth++;
      else if (s[j] === close && --depth === 0) return j;
    }
    return -1;
  }
  function parseTarget(s, i) {                    // "(url "title")" at s[i] -> {url, end} or null
    if (s[i] !== '(') return null;
    const end = matchBracket(s, i, '(', ')');
    if (end < 0) return null;
    let inner = s.slice(i + 1, end).trim();
    if (inner.startsWith('<') && inner.includes('>')) inner = inner.slice(1, inner.indexOf('>'));
    else inner = inner.split(/\s/)[0];
    return { url: inner, end };
  }
  function closer(s, from, mark) {                 // the next occurrence of mark that is not escaped, or -1
    for (let j = s.indexOf(mark, from); j >= 0; j = s.indexOf(mark, j + 1)) {
      let slashes = 0;
      for (let k = j - 1; k >= 0 && s[k] === '\\'; k--) slashes++;
      if (slashes % 2 === 0) return j;
    }
    return -1;
  }

  function inline(parent, s, ctx, depth) {
    if (depth > MAX_DEPTH || s.length > MAX_INLINE) { parent.appendChild(text(s.slice(0, MAX_INLINE * 4))); return; }
    let i = 0, plain = '';
    const flush = () => { if (plain) { parent.appendChild(text(plain)); plain = ''; } };
    while (i < s.length) {
      SPECIAL.lastIndex = i;
      const m = SPECIAL.exec(s);
      if (!m) { plain += s.slice(i); break; }
      plain += s.slice(i, m.index);
      i = m.index;
      const c = s[i], two = s.substr(i, 2);
      if (c === '\\') {
        if (i + 1 < s.length && PUNCT.includes(s[i + 1])) { plain += s[i + 1]; i += 2; } else if (s[i + 1] === '\n') { flush(); parent.appendChild(el('br')); i += 2; } else { plain += c; i++; }
        continue;
      }
      if (c === '`') {
        let n = 1; while (s[i + n] === '`') n++;
        const mark = '`'.repeat(n), end = s.indexOf(mark, i + n);
        if (end > 0) {
          flush();
          let code = s.slice(i + n, end).replace(/\n/g, ' ');
          if (code.length > 2 && code.startsWith(' ') && code.endsWith(' ') && code.trim()) code = code.slice(1, -1);
          const e = el('code'); e.textContent = code; parent.appendChild(e);
          i = end + n; continue;
        }
        plain += mark; i += n; continue;
      }
      if (two === '![' || c === '[') {
        const isImg = two === '![', open = isImg ? i + 1 : i, close = matchBracket(s, open, '[', ']');
        const target = close > 0 ? parseTarget(s, close + 1) : null;
        if (target) {
          flush();
          const label = s.slice(open + 1, close);
          if (isImg) {
            const path = safeImagePath(target.url);
            if (path && ctx.assets) {
              const img = el('img'); img.src = ctx.assets + path; img.alt = label.replace(/[\\*_`[\]]/g, '').slice(0, 200);
              img.loading = 'lazy'; img.referrerPolicy = 'no-referrer'; img.decoding = 'async';
              parent.appendChild(img);
            } else {                                // a remote picture is not fetched: only what it says is shown
              const off = el('span', 'md-noimg'); off.textContent = '🖼 ' + (label.replace(/[\\*_`[\]]/g, '').slice(0, 120) || 'картинка') + ' · внешняя картинка не загружается';
              parent.appendChild(off);
            }
          } else {
            const url = safeLink(target.url), box = url ? el('a') : el('span');
            if (url) { box.href = url; box.target = '_blank'; box.rel = 'noopener noreferrer nofollow'; box.title = url; }
            inline(box, label, ctx, depth + 1);
            parent.appendChild(box);
          }
          i = target.end + 1; continue;
        }
        plain += isImg ? '![' : '['; i += isImg ? 2 : 1; continue;
      }
      if (c === '<') {
        const end = s.indexOf('>', i);
        const url = end > 0 && end - i < 300 ? safeLink(s.slice(i + 1, end)) : null;
        if (url && !/\s/.test(s.slice(i + 1, end))) {
          flush(); const a = el('a'); a.href = url; a.target = '_blank'; a.rel = 'noopener noreferrer nofollow'; a.title = url; a.textContent = s.slice(i + 1, end);
          parent.appendChild(a); i = end + 1; continue;
        }
        plain += c; i++; continue;
      }
      let tag = null, mark = '';
      if (two === '**' || two === '__') { tag = 'strong'; mark = two; }
      else if (two === '~~') { tag = 'del'; mark = two; }
      else if (c === '*' || c === '_') { tag = 'em'; mark = c; }
      else { plain += c; i++; continue; }
      if (c === '_' && i > 0 && /[A-Za-z0-9]/.test(s[i - 1])) { plain += mark; i += mark.length; continue; }     // snake_case is not emphasis
      let end = closer(s, i + mark.length, mark);
      if (tag === 'em') while (end >= 0 && s[end + 1] === mark) end = closer(s, end + 2, mark);      // a single mark closes, not half of a double one
      if (end > i + mark.length && !/^\s/.test(s[i + mark.length])) {
        flush(); const e = el(tag); inline(e, s.slice(i + mark.length, end), ctx, depth + 1); parent.appendChild(e);
        i = end + mark.length; continue;
      }
      plain += mark; i += mark.length;
    }
    flush();
  }

  /* ---------------------------------------------------------------- blocks */
  const LIST_RE = /^( {0,6})([-*+]|\d{1,9}[.)])[ \t]+(.*)$/;
  const FENCE_RE = /^ {0,3}(`{3,}|~{3,})/;

  function headingOf(line) {                       // {level, text} or null (no backtracking regex on long lines)
    let n = 0; const t = line.replace(/^ {0,3}/, '');
    while (t[n] === '#') n++;
    if (n < 1 || n > 6 || (t.length > n && t[n] !== ' ' && t[n] !== '\t')) return null;
    let body = t.slice(n).trim();
    while (body.endsWith('#')) body = body.slice(0, -1);
    return { level: n, text: body.trim() };
  }
  function isRule(line) {
    const t = line.trim();
    if (t.length < 3 || t.length > 120) return false;
    const c = t[0];
    return (c === '-' || c === '*' || c === '_') && [...t].every((x) => x === c || x === ' ') && t.replace(/ /g, '').length >= 3;
  }
  function splitRow(line) {
    let t = line.trim();
    if (t.startsWith('|')) t = t.slice(1);
    if (t.endsWith('|') && !t.endsWith('\\|')) t = t.slice(0, -1);
    const cells = []; let cur = '';
    for (let j = 0; j < t.length; j++) {
      if (t[j] === '\\' && t[j + 1] === '|') { cur += '|'; j++; } else if (t[j] === '|') { cells.push(cur.trim()); cur = ''; } else cur += t[j];
    }
    cells.push(cur.trim());
    return cells;
  }
  const isSepRow = (line) => line.includes('-') && line.includes('|') || /^\s*:?-+:?\s*$/.test(line) ? splitRow(line).every((c) => /^:?-+:?$/.test(c)) : false;
  const startsBlock = (line) => FENCE_RE.test(line) || headingOf(line) || isRule(line) || /^ {0,3}>/.test(line) || LIST_RE.test(line);

  function blocks(lines, parent, ctx, depth) {
    let i = 0;
    while (i < lines.length && ctx.count < MAX_BLOCKS) {
      const line = lines[i];
      if (!line.trim()) { i++; continue; }
      ctx.count++;
      const fence = FENCE_RE.exec(line);
      if (fence) {
        const mark = fence[1], body = [];
        const closes = (l) => { const t = l.trim(); return t.length >= mark.length && [...t].every((ch) => ch === mark[0]); };
        i++;
        while (i < lines.length && !closes(lines[i])) { if (body.length < MAX_CODE_LINES) body.push(lines[i]); i++; }
        i++;
        const pre = el('pre'), code = el('code'); code.textContent = body.join('\n'); pre.appendChild(code); parent.appendChild(pre);
        continue;
      }
      const h = headingOf(line);
      if (h) { const e = el('h' + Math.min(h.level + 1, 6)); inline(e, h.text, ctx, 0); parent.appendChild(e); i++; continue; }
      if (isRule(line)) { parent.appendChild(el('hr')); i++; continue; }
      if (/^ {0,3}>/.test(line)) {
        const inner = [];
        while (i < lines.length && /^ {0,3}>/.test(lines[i])) { inner.push(lines[i].replace(/^ {0,3}> ?/, '')); i++; }
        const q = el('blockquote');
        if (depth < MAX_DEPTH) blocks(inner, q, ctx, depth + 1); else q.textContent = inner.join('\n');
        parent.appendChild(q); continue;
      }
      const li = LIST_RE.exec(line);
      if (li) {
        const ordered = /\d/.test(li[2]), list = el(ordered ? 'ol' : 'ul');
        if (ordered) { const start = parseInt(li[2], 10); if (start > 1 && start < 100000) list.start = start; }
        while (i < lines.length && ctx.count < MAX_BLOCKS) {
          const mm = LIST_RE.exec(lines[i]);
          if (!mm || /\d/.test(mm[2]) !== ordered || mm[1].length > 1 + li[1].length) break;
          const indent = mm[1].length + mm[2].length + 1, itemLines = [mm[3]]; i++;
          while (i < lines.length) {
            const nx = lines[i];
            if (!nx.trim()) { if (i + 1 < lines.length && /^ {2,}\S/.test(lines[i + 1])) { itemLines.push(''); i++; continue; } break; }
            if (/^ {2,}\S/.test(nx) || (!startsBlock(nx) && itemLines[itemLines.length - 1] !== '')) { itemLines.push(nx.replace(new RegExp('^ {0,' + indent + '}'), '')); i++; } else break;
          }
          ctx.count++;
          const item = el('li'); let first = itemLines[0];
          const task = /^\[([ xX])\][ \t]+/.exec(first);
          if (task) { first = first.slice(task[0].length); const box = el('span', 'md-task'); box.textContent = task[1] === ' ' ? '☐' : '☑'; item.appendChild(box); }
          const sub = [first, ...itemLines.slice(1)];
          if (depth < MAX_DEPTH && sub.length > 1) blocks(sub, item, ctx, depth + 1); else inline(item, sub.join(' '), ctx, 0);
          list.appendChild(item);
        }
        parent.appendChild(list); continue;
      }
      if (line.includes('|') && i + 1 < lines.length && isSepRow(lines[i + 1])) {
        const head = splitRow(line), align = splitRow(lines[i + 1]).map((c) => (c.startsWith(':') && c.endsWith(':') ? 'center' : c.endsWith(':') ? 'right' : c.startsWith(':') ? 'left' : ''));
        const table = el('table'), thead = el('thead'), tr = el('tr');
        head.slice(0, 12).forEach((c, k) => { const th = el('th'); if (align[k]) th.style.textAlign = align[k]; inline(th, c, ctx, 1); tr.appendChild(th); });
        thead.appendChild(tr); table.appendChild(thead);
        const tbody = el('tbody'); i += 2;
        while (i < lines.length && lines[i].trim() && lines[i].includes('|') && tbody.childNodes.length < 300) {
          const row = el('tr'); splitRow(lines[i]).slice(0, 12).forEach((c, k) => { const td = el('td'); if (align[k]) td.style.textAlign = align[k]; inline(td, c, ctx, 1); row.appendChild(td); });
          tbody.appendChild(row); i++;
        }
        table.appendChild(tbody);
        const wrap = el('div', 'md-table'); wrap.appendChild(table); parent.appendChild(wrap); continue;
      }
      const para = [line]; i++;
      while (i < lines.length && lines[i].trim() && !startsBlock(lines[i])) { para.push(lines[i]); i++; }
      const p = el('p');
      para.forEach((l, k) => {
        const hard = / {2,}$/.test(l);
        inline(p, l.trim(), ctx, 0);
        if (k < para.length - 1) p.appendChild(hard ? el('br') : text(' '));
      });
      parent.appendChild(p);
    }
  }

  window.lostMd = {
    render(src, assets) {
      const frag = document.createDocumentFragment();
      const body = String(src || '').slice(0, MAX_LEN).replace(/\r\n?/g, '\n').replace(/\u0000/g, '');
      blocks(body.split('\n'), frag, { assets: assets || '', count: 0 }, 0);
      return frag;
    },
  };
})();
