/* 3D view: the floor sheet as a texture, every room rooms.py resolved extruded as low walls from its traced
   outline, verdict pins at the evidence boxes, and a camera that flies to the active card's room.
   three.js loads lazily (pinned in the page's importmap); if it cannot load or WebGL is missing, a flat
   sheet with the same pins is shown instead. createElement / textContent only. */
import { sheetMeta, roomOf } from './stage.js';

const el = (t, c, txt) => { const e = document.createElement(t); if (c) e.className = c; if (txt != null) e.textContent = txt; return e; };
const reduced = () => window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
const vHex = v => v === 'conflict' ? 0xd2382c : v === 'consistent' ? 0x1f9a5e : 0x7b8794;
const vCls = v => v === 'conflict' ? 'v-conflict' : v === 'consistent' ? 'v-consistent' : 'v-other';
const vWord = v => v === 'conflict' ? 'Conflict' : v === 'consistent' ? 'Consistent' : 'Unclear';
export const FLOOR_SHEET = 'add1-p018';
const K = 0.1;   // world units per pt

export function mount3D(o) {
  const { container, tip, off, focus, getCards, getSelId, onPick } = o;
  let T = null, renderer, scene, camera, controls, raycaster, mouse, floorMeta = null, W = 3024, H = 2160;
  let pins = [], dyn = [], roomMeshes = new Map(), fly = null, flat = null, failed = false, started = false, lastFocus = null;
  const toX = x => x * K - W * K / 2, toZ = y => y * K - H * K / 2;

  async function start() {
    if (started) return; started = true;
    floorMeta = await sheetMeta(FLOOR_SHEET);
    if (floorMeta) { W = floorMeta.width; H = floorMeta.height; }
    try {
      T = await import('three');
      const { OrbitControls } = await import('three/addons/controls/OrbitControls.js');
      renderer = new T.WebGLRenderer({ antialias: true });
      if (!renderer.getContext()) throw new Error('no WebGL context');
      renderer.outputColorSpace = T.SRGBColorSpace;
      renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
      container.append(renderer.domElement);
      scene = new T.Scene();
      scene.background = new T.Color(0x22302b);
      camera = new T.PerspectiveCamera(42, 1, 1, 3000);
      camera.position.set(0, 230, 190);
      controls = new OrbitControls(camera, renderer.domElement);
      controls.enableDamping = true; controls.maxPolarAngle = Math.PI * 0.47;
      scene.add(new T.HemisphereLight(0xffffff, 0x334433, 1.6));
      const dir = new T.DirectionalLight(0xffffff, 1.1); dir.position.set(-120, 260, 140); scene.add(dir);
      const tex = new T.TextureLoader().load(floorMeta ? floorMeta.image : 'assets/p101_current.png');
      tex.colorSpace = T.SRGBColorSpace; tex.anisotropy = renderer.capabilities.getMaxAnisotropy();
      const floor = new T.Mesh(new T.PlaneGeometry(W * K, H * K), new T.MeshBasicMaterial({ map: tex }));
      floor.rotation.x = -Math.PI / 2; scene.add(floor);
      buildRooms();
      raycaster = new T.Raycaster(); mouse = new T.Vector2();
      wirePointer();
      const loop = now => {
        requestAnimationFrame(loop);
        if (container.offsetParent === null) return;
        if (fly) stepFly(now);
        controls.update(); renderer.render(scene, camera);
      };
      requestAnimationFrame(loop);
    } catch (e) {
      failed = true;
      if (renderer && renderer.domElement) renderer.domElement.remove();
      flat = el('div', 'flat3d');
      flat.append(el('p', 'flatmsg', '3D needs WebGL and the three.js library, which did not load here. Showing the flat sheet with the same pins.'));
      const wrap = el('div', 'flatwrap');
      const img = el('img'); img.src = floorMeta ? floorMeta.image : 'assets/p101_current.png'; img.alt = 'Floor plan sheet ' + FLOOR_SHEET;
      wrap.append(img); flat.append(wrap); container.append(flat);
    }
    update(); resize();
  }

  function wallGeometry(edges, h) {
    const pos = new Float32Array(edges.length * 18);
    let i = 0;
    for (const [a, b, c, d] of edges) {
      const x0 = toX(a), z0 = toZ(b), x1 = toX(c), z1 = toZ(d);
      pos.set([x0, 0, z0, x1, 0, z1, x1, h, z1, x0, 0, z0, x1, h, z1, x0, h, z0], i); i += 18;
    }
    const g = new T.BufferGeometry();
    g.setAttribute('position', new T.BufferAttribute(pos, 3));
    g.computeVertexNormals();
    return g;
  }
  function buildRooms() {
    for (const r of (floorMeta && floorMeta.rooms) || []) {
      if (!r.edges || !r.edges.length) continue;
      const mat = new T.MeshStandardMaterial({ color: 0x3a4b55, transparent: true, opacity: 0.55, side: T.DoubleSide, roughness: 0.8 });
      const mesh = new T.Mesh(wallGeometry(r.edges, 1), mat);
      const top = [];
      for (const [a, b, c, d] of r.edges) top.push(toX(a), 1, toZ(b), toX(c), 1, toZ(d));
      const lg = new T.BufferGeometry(); lg.setAttribute('position', new T.Float32BufferAttribute(top, 3));
      const line = new T.LineSegments(lg, new T.LineBasicMaterial({ color: 0x14191e }));
      const grp = new T.Group(); grp.add(mesh, line); grp.scale.y = 3.5;
      grp.userData = { room: r };
      scene.add(grp); roomMeshes.set(r.number, { grp, mat, line });
    }
  }

  function activeRoom(card) {
    if (!card || !floorMeta) return null;
    if (!(card.evidence || []).some(e => e.sheet === FLOOR_SHEET)) return null;
    return roomOf(card, floorMeta.rooms);
  }

  function update() {
    if (!started) return;
    const cards = getCards(), sel = cards.find(c => c.id === getSelId()) || null;
    const room = activeRoom(sel);
    const offList = [];
    if (failed) {
      if (!flat) return;
      const wrap = flat.querySelector('.flatwrap');
      wrap.querySelectorAll('.pdot').forEach(n => n.remove());
      for (const c of cards) for (const e of (c.evidence || [])) {
        if (e.sheet !== FLOOR_SHEET || !Array.isArray(e.bbox)) continue;
        const d = el('button', 'pdot ' + vCls(c.verdict) + (c.id === getSelId() ? ' sel' : '')); d.type = 'button';
        d.style.left = ((e.bbox[0] + e.bbox[2]) / 2 / W * 100) + '%'; d.style.top = ((e.bbox[1] + e.bbox[3]) / 2 / H * 100) + '%';
        d.title = c.claim || ''; d.setAttribute('aria-label', vWord(c.verdict) + ': ' + (c.claim || ''));
        d.onclick = () => onPick(c.id); wrap.append(d);
      }
    } else {
      for (const m of dyn) scene.remove(m);
      dyn = []; pins = [];
      for (const [num, r] of roomMeshes) {
        const on = room && room.number === num;
        r.mat.color.setHex(on ? vHex(sel.verdict) : 0x3a4b55);
        r.mat.opacity = on ? 0.88 : 0.5;
        r.grp.scale.y = on ? 9 : 3.5;
        r.line.material.color.setHex(on ? 0xffffff : 0x14191e);
      }
      const stemGeo = new T.CylinderGeometry(0.45, 0.45, 14, 10), headGeo = new T.SphereGeometry(2.4, 18, 14);
      const seen = {};
      for (const c of cards) {
        const evs = (c.evidence || []).filter(e => e.sheet === FLOOR_SHEET && Array.isArray(e.bbox) && e.bbox.length >= 4);
        const mat = new T.MeshStandardMaterial({ color: vHex(c.verdict), roughness: 0.4 });
        for (const e of evs) {
          const k = Math.round(e.bbox[0] / 20) + ',' + Math.round(e.bbox[1] / 20), n = seen[k] = (seen[k] || 0) + 1;
          const g = new T.Group();
          g.position.set(toX((e.bbox[0] + e.bbox[2]) / 2) + (n - 1) * 3, 0, toZ((e.bbox[1] + e.bbox[3]) / 2) + (n - 1) * 3);
          if (c.id === getSelId()) g.scale.setScalar(1.5);
          const stem = new T.Mesh(stemGeo, mat); stem.position.y = 7;
          const head = new T.Mesh(headGeo, mat); head.position.y = 15;
          stem.userData = head.userData = { card: c, ev: e };
          g.add(stem, head); scene.add(g); dyn.push(g); pins.push(stem, head);
        }
      }
    }
    for (const c of cards) {
      const evs = (c.evidence || []).filter(e => Array.isArray(e.bbox));
      if (evs.length && !evs.some(e => e.sheet === FLOOR_SHEET)) offList.push(c);
    }
    off.textContent = ''; off.hidden = !offList.length;
    if (offList.length) {
      off.append(el('b', null, 'On other sheets'));
      const ul = el('ul');
      for (const c of offList) { const li = el('li'); const b = el('button', null, c.claim || ''); b.type = 'button'; b.onclick = () => onPick(c.id); li.append(b); ul.append(li); }
      off.append(ul);
    }
    focus.textContent = ''; focus.hidden = !sel;
    if (sel) {
      focus.className = 'focus3d ' + vCls(sel.verdict);
      focus.append(el('b', null, vWord(sel.verdict)), el('span', null, sel.claim || ''));
      if (room) focus.append(el('span', 'fr', `Room ${room.number} raised${Object.keys(room.tags || {}).length ? ': ' + Object.entries(room.tags).map(([t, n]) => t + ' ×' + n).join(', ') : ''}`));
      else if (!(sel.evidence || []).some(e => e.sheet === FLOOR_SHEET)) focus.append(el('span', 'fr', 'This card is not on the floor plan sheet ' + FLOOR_SHEET + '.'));
    }
    const key = sel ? sel.id + '|' + (room ? room.number : '') : '';
    if (!failed && key !== lastFocus) { lastFocus = key; flyTo(sel, room); }
  }

  function flyTo(card, room) {
    let cx = 0, cz = 0, dist = 300;
    const ev = card && (card.evidence || []).find(e => e.sheet === FLOOR_SHEET && Array.isArray(e.bbox));
    if (room && room.bbox) { cx = toX((room.bbox[0] + room.bbox[2]) / 2); cz = toZ((room.bbox[1] + room.bbox[3]) / 2); dist = 80; }
    else if (ev) { cx = toX((ev.bbox[0] + ev.bbox[2]) / 2); cz = toZ((ev.bbox[1] + ev.bbox[3]) / 2); dist = 110; }
    const to = { p: new T.Vector3(cx + dist * 0.35, dist * 0.95, cz + dist * 0.8), t: new T.Vector3(cx, 0, cz) };
    if (reduced()) { camera.position.copy(to.p); controls.target.copy(to.t); return; }
    fly = { t0: performance.now(), ms: 1600, p0: camera.position.clone(), t0v: controls.target.clone(), to };
  }
  function stepFly(now) {
    const k = Math.min(1, (now - fly.t0) / fly.ms), e = k < .5 ? 4 * k * k * k : 1 - Math.pow(-2 * k + 2, 3) / 2;
    camera.position.lerpVectors(fly.p0, fly.to.p, e);
    camera.position.y += Math.sin(Math.PI * e) * 40;      // arc over the plan rather than sliding through it
    controls.target.lerpVectors(fly.t0v, fly.to.t, e);
    if (k >= 1) fly = null;
  }

  function wirePointer() {
    let down = null;
    const pick = e => {
      const r = renderer.domElement.getBoundingClientRect();
      mouse.x = ((e.clientX - r.left) / r.width) * 2 - 1; mouse.y = -((e.clientY - r.top) / r.height) * 2 + 1;
      raycaster.setFromCamera(mouse, camera);
      return raycaster.intersectObjects(pins, false)[0] || null;
    };
    renderer.domElement.addEventListener('pointermove', e => {
      const h = pick(e);
      if (h) { const u = h.object.userData; tip.textContent = `${vWord(u.card.verdict)}: ${u.card.claim}`; tip.style.display = 'block';
        tip.style.left = Math.min(e.clientX + 14, window.innerWidth - 300) + 'px'; tip.style.top = (e.clientY + 14) + 'px'; renderer.domElement.style.cursor = 'pointer'; }
      else { tip.style.display = 'none'; renderer.domElement.style.cursor = 'grab'; }
    });
    renderer.domElement.addEventListener('pointerdown', e => { down = { x: e.clientX, y: e.clientY }; });
    renderer.domElement.addEventListener('pointerup', e => {
      if (!down || Math.hypot(e.clientX - down.x, e.clientY - down.y) > 6) return;
      const h = pick(e); if (h) { tip.style.display = 'none'; onPick(h.object.userData.card.id); }
    });
  }
  function resize() {
    if (!renderer || failed) return;
    const w = container.clientWidth || 300, h = container.clientHeight || 400;
    camera.aspect = w / h; camera.updateProjectionMatrix(); renderer.setSize(w, h, false);
  }
  return { start, update, resize, get failed() { return failed; } };
}
