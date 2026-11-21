/* Live pipeline diagram: search -> crop -> two judges in parallel -> code checks -> verdict.
   Nodes light only when the matching server-sent event arrives; code checks tick from the finished card's own
   checks list. Nothing here is simulated: with no events, nothing lights. createElement / textContent only. */
const el = (t, c, txt) => { const e = document.createElement(t); if (c) e.className = c; if (txt != null) e.textContent = txt; return e; };
const reduced = () => window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
const modelName = m => { const n = String(m || '').replace(/^gemini-\d+-/, ''); return n ? n[0].toUpperCase() + n.slice(1) : ''; };
const vWord = v => v === 'conflict' ? 'Conflict' : v === 'consistent' ? 'Consistent' : 'Unclear';
const vCls = v => v === 'conflict' ? 'v-conflict' : v === 'consistent' ? 'v-consistent' : 'v-other';

const CODE_LABEL = {
  grounding: k => k.pass ? 'Boxes land on PDF words' : 'Boxes miss the PDF words',
  currency: k => k.pass ? 'Sheet is the current issue' : 'Sheet is not current',
  addendum_diff: () => 'Addendum word diff',
  dimension: k => k.pass ? 'Dimension math agrees' : 'Dimension math disagrees',
  spatial: k => k.room ? `Room ${k.room.number}: tags counted` + (k.pass ? '' : ', boxes outside') : 'Room geometry: not on this sheet',
  named_sheet: () => 'Named sheet honoured',
};

export function createPipeline(claim, opts = {}) {
  const root = el('section', 'pipe');
  root.setAttribute('aria-label', 'Check pipeline');
  root.setAttribute('aria-live', 'polite');
  const head = el('header', 'pipe-h');
  const title = el('div', 'pipe-claim');
  title.append(el('span', 'pipe-q', '“' + claim + '”'));
  const mode = el('span', 'pipe-mode', opts.recorded ? 'Recorded run' : 'Live run');
  const clock = el('span', 'pipe-clock mono', '0.0 s');
  head.append(title, mode, clock);
  const note = el('p', 'pipe-note', opts.recorded ? opts.recordedNote || 'Model answers come from a recorded run.' : 'Each step lights when the server reports it.');

  const flow = el('ol', 'flow');
  const nodes = {};
  const node = (id, name, sub) => {
    const li = el('li', 'pn idle'); li.dataset.step = id;
    const nm = el('b', 'pn-name', name), sb = el('span', 'pn-sub', sub || ''), tm = el('span', 'pn-t mono', '');
    li.append(el('i', 'pn-dot'), nm, sb, tm);
    nodes[id] = { li, nm, sb, tm, start: null };
    return li;
  };
  const col = (cls, ...kids) => { const c = el('li', 'col ' + cls); const inner = el('ol'); inner.append(...kids); c.append(inner); return c; };
  flow.append(
    col('c-search', node('search', 'Search sheets', 'text index of every sheet')),
    col('c-crop', node('crop', 'Crop region', 'current issue and its earlier twin')),
    col('c-judges', node('judge_a', 'Judge A', 'waiting'), node('judge_b', 'Judge B', 'waiting')),
  );
  const checksCol = el('li', 'col c-checks');
  const checksHead = el('div', 'pn idle'); checksHead.dataset.step = 'checks';
  checksHead.append(el('i', 'pn-dot'), el('b', 'pn-name', 'Code checks'), el('span', 'pn-sub', 'deterministic, no model'));
  const ticks = el('ul', 'ticks');
  checksCol.append(checksHead, ticks);
  const verdictCol = el('li', 'col c-verdict');
  const vbox = el('div', 'vslot');
  vbox.append(el('span', 'vwait', 'Verdict'));
  verdictCol.append(vbox);
  flow.append(checksCol, verdictCol);
  root.append(head, flow, note);

  let t0 = null, timer = 0, lastT = 0;
  const tick = () => { if (t0 == null) return; clock.textContent = ((performance.now() - t0) / 1000).toFixed(1) + ' s'; timer = requestAnimationFrame(tick); };
  const tickRows = {};
  const addTick = (id, label, state) => {
    let r = tickRows[id];
    if (!r) { r = el('li', 'tk'); r.append(el('i', 'tk-m'), el('span', 'tk-l', label)); ticks.append(r); tickRows[id] = r; }
    r.className = 'tk ' + state;
    r.querySelector('.tk-l').textContent = label;
    r.querySelector('.tk-m').textContent = state === 'ok' ? '✓' : state === 'bad' ? '✗' : state === 'info' ? '·' : '';
    return r;
  };
  const setState = (n, s) => { n.li.classList.remove('idle', 'run', 'done', 'bad'); n.li.classList.add(s); };

  function apply(ev) {
    if (t0 == null && !opts.recorded) { t0 = performance.now(); tick(); }
    if (opts.recorded) clock.textContent = ev.replay ? 'no model calls' : (ev.t || 0).toFixed(2) + ' s server time';
    lastT = ev.t || lastT;
    if (ev.step === 'grounding' || ev.step === 'revision') {
      setState({ li: checksHead }, ev.status === 'start' ? 'run' : 'run');
      const label = ev.step === 'grounding' ? 'Boxes land on PDF words' : 'Sheet is the current issue';
      addTick(ev.step === 'grounding' ? 'grounding' : 'currency', label, ev.status === 'start' ? 'run' : ev.ok === false ? 'bad' : 'ok');
      return;
    }
    if (ev.step === 'sealed') { if (ev.seal) checksHead.querySelector('.pn-sub').textContent = 'sealed ' + ev.seal; return; }
    const n = nodes[ev.step];
    if (!n) return;
    if (ev.status === 'start') {
      setState(n, 'run'); n.start = ev.t;
      if (ev.model) { n.nm.textContent = modelName(ev.model) + ' judge'; n.sb.textContent = 'reading the crop'; }
      if (ev.step === 'judge_a' || ev.step === 'judge_b') root.classList.add('judging');
    } else {
      setState(n, ev.ok === false ? 'bad' : 'done');
      if (!opts.recorded) n.tm.textContent = (n.start != null ? Math.max(0, (ev.t || 0) - n.start) : 0).toFixed(2) + ' s';
      if (ev.step === 'search') n.sb.textContent = ev.hits != null ? `${ev.hits} candidate sheet${ev.hits === 1 ? '' : 's'}${ev.sheet ? ', ' + ev.sheet : ''}` : 'done';
      if (ev.step === 'crop') n.sb.textContent = ev.superseded ? 'current and superseded issue cropped' : 'current issue cropped';
      if (ev.verdict) { n.sb.textContent = 'says ' + ev.verdict; n.li.classList.add(vCls(ev.verdict)); }
    }
  }

  /* after the card: tick the code checks it actually carries, one by one, then land the verdict stamp */
  function finish(card) {
    cancelAnimationFrame(timer);
    if (!opts.recorded && card.latency_s != null) clock.textContent = Number(card.latency_s).toFixed(1) + ' s';
    for (const id of ['search', 'crop']) if (nodes[id].li.classList.contains('idle') && card.evidence && card.evidence.length) setState(nodes[id], 'done');
    const js = (card.checks || []).filter(k => k.by && k.by !== 'code');
    js.slice(0, 2).forEach((j, i) => {
      const n = nodes[i ? 'judge_b' : 'judge_a'];
      if (n.li.classList.contains('idle')) { setState(n, 'done'); n.nm.textContent = modelName(j.by) + ' judge'; n.sb.textContent = 'says ' + j.verdict; n.li.classList.add(vCls(j.verdict)); }
    });
    root.classList.remove('judging');
    const code = (card.checks || []).filter(k => k.by === 'code' && CODE_LABEL[k.check]);
    const gap = reduced() ? 0 : 260;
    return new Promise(res => {
      setState({ li: checksHead }, 'run');
      let i = 0;
      const next = () => {
        if (i < code.length) {
          const k = code[i++];
          addTick(k.check, CODE_LABEL[k.check](k), k.info ? 'info' : k.pass === false ? 'bad' : 'ok');
          setTimeout(next, gap); return;
        }
        setState({ li: checksHead }, code.some(k => !k.info && k.pass === false) ? 'bad' : 'done');
        vbox.textContent = '';
        const st = el('div', 'vstamp ' + vCls(card.verdict));
        st.append(el('span', null, vWord(card.verdict)), el('small', null, Math.round((Number(card.confidence) || 0) * 100) + '% confidence'));
        vbox.append(st);
        root.classList.add('done');
        setTimeout(res, reduced() ? 200 : 900);
      };
      setTimeout(next, gap);
    });
  }
  function fail(msg) {
    cancelAnimationFrame(timer);
    vbox.textContent = '';
    vbox.append(el('div', 'vfail', msg));
    for (const n of Object.values(nodes)) if (n.li.classList.contains('run')) setState(n, 'bad');
  }
  function setRecorded(text) { opts.recorded = true; mode.textContent = 'Recorded run'; root.classList.add('recorded'); note.textContent = text; }
  return { node: root, apply, finish, fail, setRecorded };
}
