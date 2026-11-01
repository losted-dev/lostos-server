/* The panel uses the official Material Web components (mdweb.js) in the Material 3 style. The logic of the panel stays where it was (bindSwitch, the sliders, the handlers by id),
   the components are put into the existing elements: the switch draws <md-switch>, the buttons, the rows and the tabs get the ripple and the focus ring of Material. */
(function () {
  'use strict';
  const m3 = () => document.documentElement.dataset.style === 'm3';

  /* switches: <md-switch> inside .switch, it only draws; the click, the drag and the state stay with the panel (the class "on") */
  function adoptSwitch(el) {
    if (el._md) return;
    const sw = document.createElement('md-switch');
    sw.setAttribute('aria-hidden', 'true'); sw.tabIndex = -1; sw.className = 'md-sw';
    sw.selected = el.classList.contains('on');
    el.appendChild(sw); el._md = sw;
    new MutationObserver(() => { sw.selected = el.classList.contains('on'); }).observe(el, { attributes: true, attributeFilter: ['class'] });
  }

  /* ripples: <md-ripple> in the pressable elements */
  const RIPPLE = '.row.action, .row.nav, .th-row, .circle, .icon-btn, .tab, .th-shape button, .mbtn, .mi';
  function adoptRipple(el) {
    if (el._rip || el.querySelector(':scope > md-ripple')) return;
    el._rip = true;
    const r = document.createElement('md-ripple'); r.className = 'md-rip';
    el.appendChild(r);
  }
  /* buttons: the component draws the button and its label (the text and the icon are copied from the original); the click, the id and the handlers stay with the original */
  const BUTTONS = [['.big-btn, .upd-btn, .cw-btn, .mini-btn.acc, .pu-btns button.pri', 'md-filled-button'], ['.pill-btn, .mini-btn, .pu-btns button', 'md-filled-tonal-button']];
  function adoptButton(el, tag) {
    if (el._mdb) return;
    const m = document.createElement(tag);
    m.className = 'md-btn'; m.tabIndex = -1; m.setAttribute('aria-hidden', 'true');
    el.appendChild(m); el._mdb = m;
    const sync = () => {
      m.replaceChildren();
      el.childNodes.forEach((n) => {
        if (n === m || (n.nodeType === 1 && /^MD-/.test(n.tagName))) return;
        if (n.nodeType === 3) { const t = n.textContent.trim(); if (t) m.append(t); }
        else if (n.nodeType === 1 && n.tagName.toLowerCase() === 'svg') { const c = n.cloneNode(true); c.setAttribute('slot', 'icon'); m.appendChild(c); }
        else if (n.nodeType === 1) { const t = n.textContent.trim(); if (t) m.append(' ' + t); }
      });
    };
    sync();
    new MutationObserver((rec) => { if (rec.every((r) => m.contains(r.target) || r.target === m)) return; sync(); }).observe(el, { childList: true, characterData: true, subtree: true });
  }
  function scan(root) {
    if (!root.querySelectorAll) return;
    root.querySelectorAll('.switch').forEach(adoptSwitch);
    BUTTONS.forEach(([sel, tag]) => { root.querySelectorAll(sel).forEach((el) => adoptButton(el, tag)); });
    root.querySelectorAll(RIPPLE).forEach(adoptRipple);
    if (root.matches) { if (root.matches('.switch')) adoptSwitch(root); BUTTONS.forEach(([sel, tag]) => { if (root.matches(sel)) adoptButton(root, tag); }); if (root.matches(RIPPLE)) adoptRipple(root); }
  }
  function start() {
    scan(document);
    new MutationObserver((list) => { for (const m of list) m.addedNodes.forEach((n) => { if (n.nodeType === 1) scan(n); }); }).observe(document.body, { childList: true, subtree: true });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
  /* the tiles of the control center work as the quick settings of Android: a tap turns the switch of the tile, a long press opens the page of the plugin (the switch is not drawn in the Material 3 style) */
  let press = null;
  const tileOf = (e) => e.target.closest && e.target.closest('#shelfSrc .src-card');
  document.addEventListener('pointerdown', (e) => {
    const card = m3() && tileOf(e);
    if (!card || !card.querySelector('.switch')) return;
    card._long = false; clearTimeout(press);
    press = setTimeout(() => { card._long = true; if (navigator.vibrate) navigator.vibrate(12); }, 450);
  }, true);
  ['pointerup', 'pointercancel', 'pointermove'].forEach((n) => document.addEventListener(n, () => { if (n !== 'pointermove') clearTimeout(press); }, true));
  document.addEventListener('click', (e) => {
    if (!m3() || e.target.closest('.switch')) return;
    const card = tileOf(e), sw = card && card.querySelector('.switch');
    if (!sw || card.closest('.editing')) return;
    if (card._long) { card._long = false; return; }       // the long press goes on to the page of the plugin
    e.stopImmediatePropagation(); e.preventDefault(); sw.click();
  }, true);
  window.addEventListener('lostos-theme', () => scan(document));
})();
