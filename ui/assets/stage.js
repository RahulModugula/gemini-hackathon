/* Sheet stage: the full current sheet, a camera move into the evidence, numbered boxes, the room traced from the
   vector walls, a revision cloud around the words that changed, and a before/after wipe of the same region.
   Every shape drawn here comes from the card or from /api/sheet (rooms.py): nothing is invented.
   DOM is built with createElement / textContent only. */
const SVGNS = 'http://www.w3.org/2000/svg';
const el = (t, c, txt) => { const e = document.createElement(t); if (c) e.className = c; if (txt != null) e.textContent = txt; return e; };
const sv = (t, attrs) => { const e = document.createElementNS(SVGNS, t); for (const k in attrs || {}) e.setAttribute(k, attrs[k]); return e; };
const reduced = () => window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
const vColor = v => v === 'conflict' ? '#c8261b' : v === 'consistent' ? '#12794a' : '#5b6670';
const revNum = r => (/^add(\d+)$/.exec(r || '') || [])[1] || null;
const revName = r => r === 'dwg' ? 'Bid set' : revNum(r) ? 'Addendum ' + revNum(r) : (r || 'unknown issue');
const meta = new Map();
const ZOOM_MS = 1200;

export function sheetMeta(sheet) {
  if (!meta.has(sheet)) {
    meta.set(sheet, fetch('/api/sheet/' + encodeURIComponent(sheet))
      .then(r => r.ok ? r.json() : null).catch(() => null)
      .then(m => { if (!m) meta.delete(sheet); return m; }));
  }
  return meta.get(sheet);
}

const clampBox = b => { const cl = v => Math.max(0, Math.min(1000, Number(v) || 0));
  return [cl(Math.min(b[0], b[2])), cl(Math.min(b[1], b[3])), cl(Math.max(b[0], b[2])), cl(Math.max(b[1], b[3]))]; };
const isBox = b => Array.isArray(b) && b.length === 4 && b.every(v => Number.isFinite(Number(v)));
/* crop 0..1000 grid [y0,x0,y1,x1] -> sheet pt [x0,y0,x1,y1] */
function gridToPt(b, rect) {
  const [y0, x0, y1, x1] = clampBox(b), w = rect[2] - rect[0], h = rect[3] - rect[1];
  return [rect[0] + x0 / 1000 * w, rect[1] + y0 / 1000 * h, rect[0] + x1 / 1000 * w, rect[1] + y1 / 1000 * h];
}
function union(bs) {
  if (!bs.length) return null;
  return bs.reduce((a, b) => [Math.min(a[0], b[0]), Math.min(a[1], b[1]), Math.max(a[2], b[2]), Math.max(a[3], b[3])]);
}
/* pad a pt rect and stretch it to the stage aspect, staying on the sheet */
/* bounds = [x0, y0, x1, y1] of the world (the sheet, or the crop when the sheet is unavailable) */
export function frame(r, aspect, bounds, pad, minW) {
  const [bx0, by0, bx1, by1] = bounds, W = bx1 - bx0, H = by1 - by0;
  let w = Math.max(minW, (r[2] - r[0]) + 2 * pad), h = Math.max(minW / aspect, (r[3] - r[1]) + 2 * pad);
  if (w / h < aspect) w = h * aspect; else h = w / aspect;
  w = Math.min(w, W); h = Math.min(h, H);
  const cx = (r[0] + r[2]) / 2, cy = (r[1] + r[3]) / 2;
  const x = Math.max(bx0, Math.min(bx1 - w, cx - w / 2)), y = Math.max(by0, Math.min(by1 - h, cy - h / 2));
  return [x, y, x + w, y + h];
}

/* architect's revision cloud: scallops bulging outward around a rectangle */
function cloudPath(r, scallop) {
  const [x0, y0, x1, y1] = r;
  const pts = [];
  const side = (ax, ay, bx, by) => {
    const len = Math.hypot(bx - ax, by - ay), n = Math.max(2, Math.round(len / scallop));
    for (let i = 1; i <= n; i++) pts.push([ax + (bx - ax) * i / n, ay + (by - ay) * i / n, len / n]);
  };
  side(x0, y0, x1, y0); side(x1, y0, x1, y1); side(x1, y1, x0, y1); side(x0, y1, x0, y0);
  let d = `M${x0.toFixed(1)} ${y0.toFixed(1)}`;
  for (const [x, y, s] of pts) d += ` A${(s * 0.62).toFixed(1)} ${(s * 0.62).toFixed(1)} 0 0 1 ${x.toFixed(1)} ${y.toFixed(1)}`;
  return d + ' Z';
}

export function roomOf(card, metaRooms) {
  const sp = (card.checks || []).find(k => k.check === 'spatial' && k.room);
  const evRoom = ((card.evidence || []).find(e => e.room) || {}).room;
  let number = (sp && sp.room.number) || (evRoom && evRoom.number) || null;
  let source = 'spatial';
  if (!number && metaRooms && metaRooms.length) {
    /* the claim names a room number that rooms.py resolved on this sheet */
    const toks = String(card.claim || '').toUpperCase().split(/[^0-9A-Z]+/).filter(t => /^\d{3}[A-Z]?$/.test(t));
    const hit = metaRooms.find(r => toks.includes(String(r.number || '').toUpperCase()));
    if (hit) { number = hit.number; source = 'rooms'; }
  }
  if (!number) return null;
  const geo = (metaRooms || []).find(r => r.number === number) || null;
  const tags = (sp && (sp.tag_count || sp.room.tags)) || (geo && geo.tags) || {};
  return { number, name: (sp && sp.room.name) || (evRoom && evRoom.name) || (geo && geo.name) || '', tags, geo, source,
    bbox: (sp && sp.room.bbox) || (geo && geo.bbox) || null };
}

export function tagLine(room, claim) {
  const fam = new Set(String(claim || '').toUpperCase().match(/\b[A-Z]{1,3}(?=-\d)/g) || []);
  const ent = Object.entries(room.tags || {}).sort((a, b) => {
    const fa = fam.has(a[0].split('-')[0]) ? 0 : 1, fb = fam.has(b[0].split('-')[0]) ? 0 : 1;
    return fa - fb || b[1] - a[1] || a[0].localeCompare(b[0]);
  });
  return ent.map(([t, n]) => `${t} ×${n}`).join(', ');
}

function diffInfo(card) {
  const d = (card.checks || []).find(k => k.check === 'addendum_diff');
  if (!d) return null;
  const sup = (card.evidence || []).find(e => e.revision === 'dwg');
  const crop = d.superseded_crop || (sup && sup.crop) || null;
  const words = m => Object.entries(m || {}).filter(([w]) => w.length > 1).sort((a, b) => b[1] - a[1]).slice(0, 4);
  return { d, crop, cur: (d.diff_boxes || {}).current || [], sup: (d.diff_boxes || {}).superseded || [],
    added: words(d.only_current), removed: words(d.only_superseded), supSheet: d.superseded_sheet || (sup && sup.sheet) || 'bid set' };
}

export function createStage(card, opts = {}) {
  const cev = (card.evidence || []).filter(e => e.revision !== 'dwg');
  const first = cev[0] || (card.evidence || [])[0] || null;
  const root = el('section', 'stage');
  root.setAttribute('aria-label', 'Sheet stage');
  if (!first) {
    const empty = el('div', 'stage-empty');
    empty.append(el('b', null, 'No sheet to show'), el('span', null, 'The search found no drawing region for this claim, so there is nothing to box.'));
    root.append(empty);
    return { node: root, play() {}, wipe() {}, sheet() {}, destroy() {} };
  }
  const evs = cev.filter(e => e.sheet === first.sheet && isBox(e.bbox));
  const dx = diffInfo(card);
  const hasWipe = !!(dx && dx.crop && first.crop);

  const mat = el('div', 'mat');
  const cam = el('div', 'cam');
  const sheetImg = el('img', 'sheetimg'); sheetImg.alt = 'Full sheet ' + (first.sheet_number || first.sheet); sheetImg.decoding = 'async';
  const cropImg = el('img', 'cropimg'); cropImg.alt = 'Evidence region at full resolution';
  const ov = sv('svg', { class: 'ov', 'aria-hidden': 'true' });
  cam.append(sheetImg, cropImg, ov);
  const loading = el('div', 'stage-load', 'Loading sheet ' + (first.sheet_number || first.sheet) + '…');
  mat.append(cam, loading);

  /* heads-up display */
  const cap = el('div', 'hud cap');
  cap.append(el('b', null, first.sheet_number || first.sheet), el('span', null, revName(first.revision) + ', current'),
    el('span', 'mono', first.sheet));
  const ctl = el('div', 'hud ctl'); ctl.setAttribute('role', 'group'); ctl.setAttribute('aria-label', 'Stage view');
  const mkBtn = (txt, fn, dis) => { const b = el('button', 'seg', txt); b.type = 'button'; b.onclick = fn; if (dis) { b.disabled = true; b.title = dis; } ctl.append(b); return b; };
  const bEv = mkBtn('Evidence', () => sheet(true));
  const bAll = mkBtn('Whole sheet', () => whole());
  const bWipe = mkBtn('Before / after', () => wipe(true), hasWipe ? null : 'No earlier issue of this region to compare');
  const can3d = typeof opts.on3d === 'function' && (!opts.can3d || opts.can3d(first));
  const b3d = can3d ? mkBtn('3D', () => tilt()) : null;
  const roomChip = el('div', 'hud roomchip'); roomChip.hidden = true;
  const legend = el('div', 'hud legend2'); legend.hidden = true;
  mat.append(cap, ctl, roomChip);
  root.append(mat, legend);

  let W = 0, H = 0, ox = 0, oy = 0, target = null, mode = 'sheet', timers = [], wipeApi = null, landed = false, played = false;
  const later = (fn, ms) => timers.push(setTimeout(fn, ms));
  const setSeg = m => { root.classList.toggle('mode-wipe', m === 'wipe'); for (const [b, k] of [[bEv, 'sheet'], [bAll, 'whole'], [bWipe, 'wipe'], [b3d, '3d']]) { if (!b) continue; b.classList.toggle('on', m === k); b.setAttribute('aria-pressed', String(m === k)); } };

  function look(r, ms) {
    const mw = mat.clientWidth || 800, mh = mat.clientHeight || 450;
    const s = Math.min(mw / (r[2] - r[0]), mh / (r[3] - r[1]));
    const tx = (mw - (r[2] - r[0]) * s) / 2 - (r[0] - ox) * s, ty = (mh - (r[3] - r[1]) * s) / 2 - (r[1] - oy) * s;
    cam.style.transition = ms && !reduced() ? `transform ${ms}ms cubic-bezier(.65,0,.25,1)` : 'none';
    cam.style.transform = `translate(${tx.toFixed(2)}px,${ty.toFixed(2)}px) scale(${s.toFixed(5)})`;
    root.style.setProperty('--z', String(s));
  }

  /* overlay groups */
  const gRoom = sv('g', { class: 'g-room' }), gCloud = sv('g', { class: 'g-cloud' }), gBox = sv('g', { class: 'g-box' });
  ov.append(gRoom, gCloud, gBox);

  function build(m) {
    const useSheet = !!(m && m.width && m.height && first.sheet);
    if (useSheet) { W = m.width; H = m.height; ox = 0; oy = 0; sheetImg.src = m.image; }
    else if (isBox(first.crop_rect_pt) && first.crop) {
      const r = first.crop_rect_pt; ox = r[0]; oy = r[1]; W = r[2] - r[0]; H = r[3] - r[1]; sheetImg.src = first.crop;
    } else { W = 1000; H = 1000; sheetImg.src = first.crop || ''; }
    const vw = useSheet ? W : W, vh = useSheet ? H : H;
    cam.style.width = vw + 'px'; cam.style.height = vh + 'px';
    ov.setAttribute('viewBox', `${ox} ${oy} ${vw} ${vh}`);
    ov.setAttribute('width', vw); ov.setAttribute('height', vh);
    if (isBox(first.crop_rect_pt) && first.crop && useSheet) {
      const r = first.crop_rect_pt;
      Object.assign(cropImg.style, { left: (r[0] - ox) + 'px', top: (r[1] - oy) + 'px', width: (r[2] - r[0]) + 'px', height: (r[3] - r[1]) + 'px' });
      cropImg.src = first.crop;
    } else cropImg.hidden = true;

    const room = roomOf(card, m && m.rooms);
    const ptBoxes = evs.map(e => e.bbox.map(Number));
    let focus = union(ptBoxes) || (isBox(first.crop_rect_pt) ? first.crop_rect_pt : [ox, oy, ox + W, oy + H]);
    if (room && room.bbox) focus = union([focus, room.bbox]);
    const aspect = (mat.clientWidth || 16) / (mat.clientHeight || 9);
    target = frame(focus, aspect, [ox, oy, ox + W, oy + H], Math.max(40, (focus[2] - focus[0]) * 0.45), Math.min(340, W, Math.max(280, (focus[2] - focus[0]) * 2.2)));
    const unit = (target[2] - target[0]) / 100;      // overlay sizes scale with the framed region
    root.style.setProperty('--u', unit.toFixed(3));

    /* room traced from the vector walls */
    if (room && room.geo && room.geo.edges && room.geo.edges.length) {
      let d = '';
      for (const [a, b, c, e] of room.geo.edges) d += `M${a} ${b}L${c} ${e}`;
      gRoom.append(sv('path', { d, class: 'room-hl' }), sv('path', { d, class: 'room-ln' }));
    }
    if (room) {
      roomChip.textContent = '';
      const line = tagLine(room, card.claim);
      roomChip.append(el('b', null, `Room ${room.number}${room.name ? ' ' + room.name.toLowerCase() : ''}`),
        el('span', 'tags', line || 'no fixture tags inside'),
        el('span', 'by', room.geo ? 'Counted by code inside the walls traced from the PDF' : 'Counted by code'));
      roomChip.hidden = false;
    }

    /* revision cloud around the words the addendum changed (diff boxes near the evidence) */
    const rn = revNum(first.revision);
    if (dx && rn && isBox(first.crop_rect_pt) && dx.cur.length) {
      /* only changed words that sit on an evidence box: the cloud marks what this card is about, not every diff */
      const eu = ptBoxes.map(b => [b[0] - 12, b[1] - 12, b[2] + 12, b[3] + 12]);
      const near = dx.cur.filter(isBox).map(b => gridToPt(b, first.crop_rect_pt))
        .filter(b => (b[2] - b[0]) > 4 && eu.some(e => b[0] < e[2] && b[2] > e[0] && b[1] < e[3] && b[3] > e[1]));
      const u = union(near.length ? near : []);
      if (u) {
        const pad = Math.max(8, unit * 2.2);
        const r = [u[0] - pad, u[1] - pad, u[2] + pad, u[3] + pad];
        const path = sv('path', { d: cloudPath(r, Math.max(6, unit * 2.6)), class: 'cloud', pathLength: '1' });
        const t = unit * 3.2, tx = r[2] + t * 0.2, ty = r[1] - t * 0.25;
        const tri = sv('g', { class: 'delta' });
        tri.append(sv('path', { d: `M${tx} ${ty - t}L${tx + t * 0.58} ${ty}L${tx - t * 0.58} ${ty}Z` }),
          Object.assign(sv('text', { x: tx, y: ty - t * 0.24, 'font-size': (t * 0.55).toFixed(2) }), { textContent: rn }));
        gCloud.append(path, tri);
      }
    }

    /* numbered evidence boxes */
    evs.forEach((e, i) => {
      const num = String(cev.indexOf(e) + 1);       // same number as the evidence list, even when some boxes sit on another sheet
      const [a, b, c, d] = e.bbox.map(Number);
      const g = sv('g', { class: 'ebox', style: `--i:${i}` });
      g.append(sv('rect', { x: a, y: b, width: c - a, height: d - b, pathLength: '1', stroke: vColor(card.verdict) }));
      const ts = unit * 2.3;
      g.append(sv('rect', { class: 'etag', x: a - ts * 0.15, y: b - ts * 1.05, width: ts * (num.length * 0.6 + 0.5), height: ts, fill: vColor(card.verdict) }));
      g.append(Object.assign(sv('text', { x: a + ts * 0.1, y: b - ts * 0.25, 'font-size': (ts * 0.8).toFixed(2) }), { textContent: num }));
      gBox.append(g);
    });

    /* legend */
    const lg = [];
    if (evs.length) lg.push(['box', `${evs.length} evidence box${evs.length > 1 ? 'es' : ''}, numbered as in the list below`]);
    if (gCloud.childNodes.length) lg.push(['cloud', `Words that changed in ${revName(first.revision)}, clouded by the checker`]);
    if (room && room.geo) lg.push(['room', `Room ${room.number} traced from the vector walls`]);
    legend.textContent = '';
    for (const [k, t] of lg) { const r = el('span', 'lg ' + k); r.append(el('i'), t); legend.append(r); }
    legend.hidden = !lg.length;
  }

  function land() {
    landed = true;
    root.classList.add('landed');
  }
  /* stage 4: the flat sheet tilts back and the 3D plan (walls from the vector geometry) fades in over it */
  let host3d = null, api3d = null, tiltT = 0;
  const tiltWrap = el('div', 'tiltwrap');
  mat.insertBefore(tiltWrap, cam); tiltWrap.append(cam);
  function flat() {
    clearTimeout(tiltT);
    root.classList.remove('in3d', 'tilting');
    tiltWrap.style.transform = '';
  }
  async function tilt() {
    if (!can3d) return;
    mode = '3d'; setSeg(mode);
    if (wipeApi) wipeApi.hide();
    if (target) look(target, 0);
    root.classList.add('tilting');
    tiltWrap.style.transform = reduced() ? '' : 'rotateX(52deg) scale(.92)';
    if (!host3d) {
      host3d = el('div', 'host3d'); mat.insertBefore(host3d, cap);
      try { api3d = await opts.on3d(host3d, card); } catch (e) { api3d = null; }
      if (!api3d) {
        host3d.remove(); host3d = null; flat(); mode = 'sheet'; setSeg('sheet');
        if (b3d) { b3d.disabled = true; b3d.title = '3D needs WebGL; showing the flat sheet'; }
        return;
      }
    } else if (api3d.focus) api3d.focus(card.id);
    /* crossfade starts once the tilt is ~60% through, so the walls appear to rise out of the leaning sheet */
    tiltT = setTimeout(() => root.classList.add('in3d'), reduced() ? 0 : 480);
  }
  function sheet(anim) {
    flat();
    mode = 'sheet'; setSeg(mode);
    if (wipeApi) wipeApi.hide();
    if (target) look(target, anim ? 700 : 0);
  }
  function whole() {
    flat();
    mode = 'whole'; setSeg(mode);
    if (wipeApi) wipeApi.hide();
    look([ox, oy, ox + W, oy + H], 700);
  }
  function wipe(auto, pos) {
    if (!hasWipe) return;
    flat();
    mode = 'wipe'; setSeg(mode);
    if (!wipeApi) { wipeApi = createWipe(card, first, dx); mat.append(wipeApi.node); }
    wipeApi.show(auto);
    if (pos != null && !auto) wipeApi.set(pos);
  }

  const ready = sheetMeta(first.sheet).then(m => {
    build(m);
    look([ox, oy, ox + W, oy + H], 0);
    return new Promise(res => {
      if (sheetImg.complete && sheetImg.naturalWidth) res();
      else { sheetImg.onload = () => res(); sheetImg.onerror = () => res(); setTimeout(res, 2500); }
    });
  }).then(() => { loading.hidden = true; root.classList.add('ready'); });

  function play(o = {}) {
    if (played && !o.again) return ready;
    played = true;
    return ready.then(() => new Promise(res => {
      setSeg('sheet');
      if (reduced() || o.instant) { look(target, 0); land(); res(); return; }
      root.classList.remove('landed');
      later(() => look(target, ZOOM_MS), o.hold ?? 450);
      later(() => { land(); res(); }, (o.hold ?? 450) + ZOOM_MS - 120);
    }));
  }
  const onResize = () => { if (!target) return; if (mode === 'whole') look([ox, oy, ox + W, oy + H], 0); else look(target, 0); };
  window.addEventListener('resize', onResize);
  return {
    node: root, play, wipe, whole, tilt, sheet: () => sheet(true), hasWipe, can3d, ready,
    get landed() { return landed; },
    destroy() { timers.forEach(clearTimeout); clearTimeout(tiltT); window.removeEventListener('resize', onResize); if (wipeApi) wipeApi.destroy(); if (api3d && api3d.destroy) api3d.destroy(); }
  };
}

/* ---- before / after wipe over the same region ---- */
function createWipe(card, first, dx) {
  const node = el('div', 'wipe'); node.hidden = true;
  const fr = el('div', 'wframe');
  const evb = (card.evidence || []).filter(e => e.revision !== 'dwg' && e.crop === first.crop).map(e => e.box_2d_on_crop).filter(isBox);
  const all = evb.concat(dx.cur.filter(isBox), dx.sup.filter(isBox)).map(clampBox);
  let y0 = 1e9, x0 = 1e9, y1 = -1e9, x1 = -1e9;
  for (const [a, b, c, d] of all.length ? all : [[0, 0, 1000, 1000]]) { y0 = Math.min(y0, a); x0 = Math.min(x0, b); y1 = Math.max(y1, c); x1 = Math.max(x1, d); }
  /* keep the window tight on the evidence: the changed words close to it, not every diff on the crop */
  if (evb.length) { const u = evb.reduce((p, q) => [Math.min(p[0], q[0]), Math.min(p[1], q[1]), Math.max(p[2], q[2]), Math.max(p[3], q[3])]);
    [y0, x0, y1, x1] = [Math.max(0, u[0] - 120), Math.max(0, u[1] - 160), Math.min(1000, u[2] + 120), Math.min(1000, u[3] + 160)]; }
  const aspect = 16 / 9;
  let w = Math.max(200, x1 - x0), h = Math.max(200 / aspect, y1 - y0);
  if (w / h < aspect) w = h * aspect; else h = w / aspect;
  w = Math.min(w, 1000); h = Math.min(h, 1000);
  const win = { x: Math.max(0, Math.min(1000 - w, (x0 + x1) / 2 - w / 2)), y: Math.max(0, Math.min(1000 - h, (y0 + y1) / 2 - h / 2)), w, h };
  fr.style.aspectRatio = `${w} / ${h}`;
  const layer = (src, cls, boxes, sign) => {
    const L = el('div', 'wl ' + cls);
    const img = el('img'); img.src = src; img.alt = cls === 'now' ? 'Current issue of the region' : 'Superseded issue of the region';
    Object.assign(img.style, { width: (1000 / win.w * 100) + '%', height: (1000 / win.h * 100) + '%', left: (-win.x / win.w * 100) + '%', top: (-win.y / win.h * 100) + '%' });
    /* crops clamped at a sheet edge are not square: size the frame from the real pixels so nothing stretches */
    if (cls === 'now') img.onload = () => { const ar = (win.w * img.naturalWidth) / (win.h * img.naturalHeight); if (ar > 0 && isFinite(ar)) fr.style.aspectRatio = String(ar); };
    L.append(img);
    for (const b of boxes) {
      const [a, bb, c, d] = clampBox(b);
      if (c < win.y || a > win.y + win.h || d < win.x || bb > win.x + win.w) continue;
      const m = el('div', 'dmark ' + cls);
      Object.assign(m.style, { left: ((bb - win.x) / win.w * 100) + '%', top: ((a - win.y) / win.h * 100) + '%', width: ((d - bb) / win.w * 100) + '%', height: ((c - a) / win.h * 100) + '%' });
      m.append(el('span', 'glyph', sign));
      L.append(m);
    }
    return L;
  };
  /* only diff marks that touch a grounded evidence box: the change this card is about, never a stray diff */
  const supEvB = (card.evidence || []).filter(e => e.revision === 'dwg').map(e => e.box_2d_on_crop).filter(isBox);
  const touches = (b, set) => { const [a, bb, c, d] = clampBox(b); return set.some(e => { const [p, q, r, s] = clampBox(e);
    return a < r + 15 && c > p - 15 && bb < s + 15 && d > q - 15; }); };
  const before = layer(dx.crop, 'was', dx.sup.filter(isBox).filter(b => touches(b, supEvB.length ? supEvB : evb)), '−');
  const now = layer(first.crop, 'now', dx.cur.filter(isBox).filter(b => touches(b, evb)), '+');
  const handle = el('div', 'whandle');
  handle.tabIndex = 0; handle.setAttribute('role', 'slider'); handle.setAttribute('aria-label', 'Before and after divider');
  handle.setAttribute('aria-valuemin', '0'); handle.setAttribute('aria-valuemax', '100');
  handle.append(el('span', 'knob', '‹ ›'));
  const tagL = el('div', 'wtag was'); tagL.append(el('b', null, 'Before'), el('span', null, 'Bid set, ' + dx.supSheet));
  const tagR = el('div', 'wtag now'); tagR.append(el('b', null, 'Now'), el('span', null, revName(first.revision) + ', ' + first.sheet));
  fr.append(before, now, handle, tagL, tagR);
  const key = el('div', 'wkey');
  const row = (cls, sign, label, words) => {
    const r = el('div', 'wk ' + cls);
    r.append(el('span', 'glyph', sign), el('b', null, label));
    r.append(el('span', 'words', words.length ? words.map(([w, n]) => n > 1 ? `${w} ×${n}` : w).join(', ') : 'none'));
    return r;
  };
  key.append(row('now', '+', 'Only on the current sheet', dx.added), row('was', '−', 'Only on the bid set', dx.removed),
    el('span', 'wsrc', 'Word diff computed by code from the PDF text layer'));
  node.append(fr, key);

  let pos = 50, raf = 0;
  const set = p => { pos = Math.max(0, Math.min(100, p)); now.style.clipPath = `inset(0 0 0 ${pos}%)`; handle.style.left = pos + '%';
    handle.setAttribute('aria-valuenow', String(Math.round(pos))); handle.setAttribute('aria-valuetext', `${Math.round(100 - pos)}% current`); };
  set(50);
  const fromX = x => { const r = fr.getBoundingClientRect(); return (x - r.left) / r.width * 100; };
  let drag = false;
  fr.addEventListener('pointerdown', e => { drag = true; cancelAnimationFrame(raf); fr.setPointerCapture(e.pointerId); set(fromX(e.clientX)); });
  fr.addEventListener('pointermove', e => { if (drag) set(fromX(e.clientX)); });
  fr.addEventListener('pointerup', () => { drag = false; });
  handle.addEventListener('keydown', e => {
    if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') { e.preventDefault(); e.stopPropagation(); cancelAnimationFrame(raf); set(pos + (e.key === 'ArrowLeft' ? -5 : 5)); }
  });
  function sweep() {
    if (reduced()) { set(50); return; }
    const keys = [[0, 96], [900, 96], [2100, 4], [2700, 4], [3500, 50]];
    const t0 = performance.now();
    const ease = t => t < .5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
    const step = now_ => {
      const t = now_ - t0;
      let i = 0; while (i < keys.length - 1 && t > keys[i + 1][0]) i++;
      if (i >= keys.length - 1) { set(keys[keys.length - 1][1]); return; }
      const [ta, pa] = keys[i], [tb, pb] = keys[i + 1];
      set(pa + (pb - pa) * ease(Math.min(1, (t - ta) / (tb - ta))));
      raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
  }
  return {
    node,
    show(auto) { node.hidden = false; node.classList.remove('in'); void node.offsetWidth; node.classList.add('in'); if (auto) sweep(); },
    hide() { cancelAnimationFrame(raf); node.hidden = true; },
    set, destroy() { cancelAnimationFrame(raf); }
  };
}
