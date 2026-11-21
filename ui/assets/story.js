/* Present-mode opening: the problem, told with the project's own sheets. Every number comes from the sheet index
   (/api/sheetset), the held-out eval files (/api/heldout) or a recorded card (/api/card). The RFI cost line is a
   cited industry figure and is footnoted as such. createElement / textContent only. */
const el = (t, c, txt) => { const e = document.createElement(t); if (c) e.className = c; if (txt != null) e.textContent = txt; return e; };
const reduced = () => window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
let setP = null, cardP = new Map();
export const sheetset = () => (setP ||= fetch('/api/sheetset').then(r => r.ok ? r.json() : null).catch(() => null));
export const cardById = id => { if (!cardP.has(id)) cardP.set(id, fetch('/api/card/' + encodeURIComponent(id)).then(r => r.ok ? r.json() : null).catch(() => null)); return cardP.get(id); };

function slide(title, kicker) {
  const s = el('section', 'slide');
  s.append(el('h2', 'sl-t', title));
  if (kicker) s.append(el('p', 'sl-k', kicker));
  return s;
}

/* 1 + 2: the sheet wall. phase 1 = bid set only; phase 2 = superseded sheets marked, reissued pages added */
export async function sheetWall(phase) {
  const set = await sheetset();
  if (!set) { const s = slide('The drawing set'); s.append(el('p', 'sl-k', 'The sheet index is not built on this machine.')); return s; }
  const rows = set.rows || [];
  const bid = rows.filter(r => r.source === 'dwg'), re = rows.filter(r => r.source !== 'dwg');
  const supNums = new Set(bid.filter(r => r.current === false).map(r => r.sheet_number));
  const dates = {};
  for (const r of re) if (r.issue_date && !dates[r.source]) dates[r.source] = r.issue_date;
  const s = slide(phase === 1 ? `This daycare is ${bid.length} drawing sheets.` : 'Then the architect changes them.',
    phase === 1 ? 'One building, every trade, one PDF. Everyone prices and builds from these.'
      : 'The old versions don’t disappear: both PDFs sit in everyone’s inbox.');
  const wall = el('div', 'wall' + (phase === 2 ? ' changed' : ''));
  const tile = (r, cls) => {
    const f = el('figure', 'wt ' + cls);
    const img = el('img'); img.loading = 'eager'; img.decoding = 'async'; img.alt = r.sheet_number || r.sheet; img.src = '/sheets/' + r.sheet + '_t.jpg';
    f.append(img, el('figcaption', 'mono', r.sheet_number || ''));
    return f;
  };
  bid.forEach((r, i) => { const t = tile(r, phase === 2 && supNums.has(r.sheet_number) ? 'sup' : ''); t.style.setProperty('--i', i); wall.append(t); });
  if (phase === 2) re.forEach((r, i) => { const t = tile(r, 'new'); t.style.setProperty('--i', i); wall.append(t); });
  const cnt = el('div', 'counter');
  if (phase === 1) cnt.append(el('b', null, String(bid.length)), el('span', null, 'sheets in the bid set'));
  else {
    const nAdd = Object.keys(dates).length || new Set(re.map(r => r.source)).size;
    cnt.append(el('b', null, String(supNums.size)), el('span', null, `sheets reissued by ${nAdd} addenda, as ${re.length} new pages`));
    const key = el('div', 'wkey2');
    const k = (cls, t) => { const x = el('span', 'k ' + cls); x.append(el('i'), t); return x; };
    key.append(k('sup', 'superseded, still in the set'), k('new', 'reissued by an addendum'));
    cnt.append(key);
  }
  s.append(cnt, wall);
  return s;
}

/* 3: spot the difference on a real recorded card's region; diff marks light after a pause */
export async function spotDiff(cardId, caption) {
  const c = cardId ? await cardById(cardId) : null;
  const d = c && (c.checks || []).find(k => k.check === 'addendum_diff');
  const cur = c && (c.evidence || []).find(e => e.revision !== 'dwg');
  const supCrop = d && (d.superseded_crop || ((c.evidence || []).find(e => e.revision === 'dwg') || {}).crop);
  const s = slide('Spot the difference.', caption);
  if (!c || !cur || !supCrop) { s.append(el('p', 'sl-k', 'The recorded example card is not available here.')); return s; }
  const boxes = (c.evidence || []).filter(e => e.revision !== 'dwg' && Array.isArray(e.box_2d_on_crop)).map(e => e.box_2d_on_crop);
  let y0 = 1e9, x0 = 1e9, y1 = -1e9, x1 = -1e9;
  for (const b of boxes) { y0 = Math.min(y0, b[0]); x0 = Math.min(x0, b[1]); y1 = Math.max(y1, b[2]); x1 = Math.max(x1, b[3]); }
  const pad = 70; y0 = Math.max(0, y0 - pad); x0 = Math.max(0, x0 - pad * 1.6); y1 = Math.min(1000, y1 + pad); x1 = Math.min(1000, x1 + pad * 1.6);
  const win = { x: x0, y: y0, w: x1 - x0, h: y1 - y0 };
  const pane = (src, label, sub, cls, marks, sign) => {
    const f = el('figure', 'sd ' + cls);
    const cap = el('figcaption'); cap.append(el('b', null, label), el('span', null, sub));
    const z = el('div', 'sdz'); z.style.aspectRatio = `${win.w} / ${win.h}`;
    const img = el('img'); img.src = src; img.alt = label;
    Object.assign(img.style, { width: (1000 / win.w * 100) + '%', height: (1000 / win.h * 100) + '%', left: (-win.x / win.w * 100) + '%', top: (-win.y / win.h * 100) + '%' });
    img.onload = () => { const ar = (win.w * img.naturalWidth) / (win.h * img.naturalHeight); if (ar > 0 && isFinite(ar)) z.style.aspectRatio = String(ar); };
    z.append(img);
    for (const b of marks || []) {
      if (b[2] < win.y || b[0] > win.y + win.h || b[3] < win.x || b[1] > win.x + win.w) continue;
      const m = el('div', 'dmark ' + cls);
      Object.assign(m.style, { left: ((b[1] - win.x) / win.w * 100) + '%', top: ((b[0] - win.y) / win.h * 100) + '%', width: ((b[3] - b[1]) / win.w * 100) + '%', height: ((b[2] - b[0]) / win.h * 100) + '%' });
      m.append(el('span', 'glyph', sign)); z.append(m);
    }
    f.append(cap, z); return f;
  };
  /* light the judges' grounded evidence boxes (each sits on PDF words, checked by code), not raw diff boxes */
  const isB = b => Array.isArray(b) && b.length === 4;
  const nowB = (c.evidence || []).filter(e => e.revision !== 'dwg' && isB(e.box_2d_on_crop) && /revis|cloud|chang|addend/i.test(e.label || '')).map(e => e.box_2d_on_crop);
  const wasB = (c.evidence || []).filter(e => e.revision === 'dwg' && isB(e.box_2d_on_crop)).map(e => e.box_2d_on_crop);
  const two = el('div', 'sdpair');
  two.append(pane(supCrop, 'Bid set', d.superseded_sheet || 'superseded', 'was', wasB, '−'),
    pane(cur.crop, 'Addendum ' + String(cur.revision).replace('add', ''), cur.sheet, 'now', nowB, '+'));
  s.append(two);
  const words = m => Object.entries(m || {}).filter(([w]) => w.length > 1).map(([w, n]) => n > 1 ? `${w} ×${n}` : w).join(', ');
  const after = el('p', 'sdafter');
  after.append(el('span', 'k was', '− ' + (words(d.only_superseded) || 'nothing')), el('span', 'k now', '+ ' + (words(d.only_current) || 'nothing')),
    el('span', 'src', 'word diff computed by code from the two PDFs'));
  s.append(after);
  setTimeout(() => s.classList.add('lit'), reduced() ? 0 : 2000);
  return s;
}

/* 4: why it matters */
export function rfiCost() {
  const s = slide('So people ask.');
  const big = el('div', 'bigline');
  big.append(el('p', null, 'A question to the architect is an RFI.'),
    el('p', 'num', 'About $1,080 and 8 hours each.'),
    el('p', null, '10–13% ask something the documents already answer.'));
  s.append(big, el('p', 'foot2', 'Reported figures: Navigant RFI study, via Procore. Not measured by this project.'));
  return s;
}

export function askInstead() {
  const s = slide('Ask the current drawings instead.', 'Every answer is boxed on the sheet, judged by two models, and checked by code. If they disagree, it says unclear.');
  return s;
}

/* last: trust, from the frozen held-out eval files */
export async function trust() {
  const m = await fetch('/api/heldout').then(r => r.ok ? r.json() : null).catch(() => null);
  const s = slide('Can you trust it?');
  if (!m || !m.n) { s.append(el('p', 'sl-k', 'The held-out eval results are not on this machine.')); return s; }
  const g = el('div', 'trust');
  const stat = (b, t, sub) => { const x = el('div', 'st'); x.append(el('b', null, b), el('span', null, t)); if (sub) x.append(el('small', null, sub)); g.append(x); };
  const pct = v => Math.round(v * 100);
  const v2 = m.runs.find(r => /v2/.test(r.label || '')) || m.runs[m.runs.length - 1];
  if (v2 && v2.correct != null && Array.isArray(v2.accuracy_ci95)) {
    stat(`${v2.correct} / ${v2.n}`, 'correct on held-out set v2', `${pct(v2.accuracy)}% accuracy, 95% CI ${pct(v2.accuracy_ci95[0])}–${pct(v2.accuracy_ci95[1])}%`);
  }
  stat(String(m.false_alarms), 'false alarms', `across both held-out sets, ${m.n} claims`);
  stat(`${m.answered_right} / ${m.answered}`, 'right when it gave a verdict', `said unclear on the other ${m.n - m.answered} instead of guessing`);
  const dates = [...new Set(m.runs.map(r => r.date).filter(Boolean))].join(', ');
  s.append(g, el('p', 'foot2', `Frozen held-out runs, ${dates}: ` + m.runs.map(r => `${r.label} (n=${r.n})`).join(', ') + '. End to end: search, crop, two judges, code verifier.'));
  return s;
}
