/* Plan3D: a 3D view of one drawing sheet built only from the PDF's vector geometry (GET /api/plan3d/<sheet>).
 *
 *   Plan3D.mount(el, {sheet, cards, activeCardId, intro: "rise", focusRectPt, freeze: {state, t}}) -> Promise
 *   Plan3D.focus(cardId)   fly to the card's room, mark it active
 *   Plan3D.flatten()       animate back to the flat top-down sheet (walls down)
 *   Plan3D.tour()          guided camera path through the verdict pins (Present mode) -> Promise
 *   Plan3D.unmount()
 * Emits CustomEvent 'plan3d:select' {cardId, sheet, bbox, evidenceIndex} (bubbles) on the mount element when a pin is
 * clicked. Walls are the sheet's own wall strokes, traced server-side; the only assumption is the 3.0 m wall height.
 * DOM is built with createElement/textContent only. No build step: three.js is the page's global THREE if present,
 * else the same pinned module index.html's import map uses (three@0.160.0), loaded with a dynamic import. */
(function () {
  'use strict';
  var THREE_URL = 'https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.js';
  var WALL_H = 3.0;                 // m; assumed (plans carry no heights)
  var CUT_H = 1.2;                  // m; section cut height
  var PIN_H = 4.2;                  // m; pins stand above the walls so they read in any view
  var DEFAULT_PT_PER_M = 9 / 0.3048; // 1/8" = 1'-0" when the sheet states no scale
  var ISO = {az: -0.62, pol: 0.98};
  var FOCUS_POL = 0.7;              // steeper when flown in, so the view looks down into the room
  var INTRO_MS = 1600, FLY_MS = 1500, TOGGLE_MS = 900;
  var COL = {
    paperEdge: 0x1f2a36, ink: 0x1f2a36, wall: 0xf2eee6, cap: 0x2c3540, bg: 0x10151a, room: 0x5b7fa6,
    conflict: 0xc2453d, consistent: 0x3f8f5f, unclear: 0x8a9099, accent: 0xd99a2b
  };
  var threeP = null;
  var inst = null;

  function loadThree() {
    if (window.THREE && window.THREE.WebGLRenderer) return Promise.resolve(window.THREE);
    if (!threeP) threeP = import(THREE_URL);
    return threeP;
  }

  function reducedMotion() {
    return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }

  function webglOK() {
    try {
      var c = document.createElement('canvas');
      return !!(window.WebGLRenderingContext && (c.getContext('webgl2') || c.getContext('webgl')));
    } catch (e) { return false; }
  }

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = String(text);
    return e;
  }

  function verdictKey(v) {
    v = String(v || '').toLowerCase();
    return v === 'conflict' ? 'conflict' : v === 'consistent' ? 'consistent' : 'unclear';
  }
  var VWORD = {conflict: 'Conflict', consistent: 'Consistent', unclear: 'Unclear'};

  function ease(p) { return p < 0.5 ? 4 * p * p * p : 1 - Math.pow(-2 * p + 2, 3) / 2; }
  function lerp(a, b, p) { return a + (b - a) * p; }
  function angLerp(a, b, p) {
    var d = ((b - a + Math.PI) % (2 * Math.PI) + 2 * Math.PI) % (2 * Math.PI) - Math.PI;
    return a + d * p;
  }

  function pointInRing(x, y, ring) {
    var c = false;
    for (var i = 0, j = ring.length - 1; i < ring.length; j = i++) {
      var xi = ring[i][0], yi = ring[i][1], xj = ring[j][0], yj = ring[j][1];
      if ((yi > y) !== (yj > y) && x < xi + (y - yi) * (xj - xi) / (yj - yi)) c = !c;
    }
    return c;
  }

  var CSS = [
    '.p3-root{position:relative;width:100%;height:100%;overflow:hidden;background:#10151a;font:12px/1.35 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;color:#d6dde4;user-select:none;-webkit-user-select:none}',
    '.p3-root canvas{display:block;width:100%;height:100%;touch-action:none;cursor:grab}',
    '.p3-root canvas.p3-drag{cursor:grabbing}',
    '.p3-labels{position:absolute;inset:0;pointer-events:none}',
    '.p3-room{position:absolute;transform:translate(-50%,-50%);text-align:center;white-space:nowrap;color:#1f2a36;text-shadow:0 0 3px #f6f3ec,0 0 3px #f6f3ec}',
    '.p3-room b{display:block;font-size:11px;font-weight:700;letter-spacing:.02em}',
    '.p3-room span{display:block;font-size:9px;letter-spacing:.06em;opacity:.8}',
    '.p3-room.p3-small span{display:none}',
    '.p3-room.p3-act b{color:#fff;background:var(--p3v,#c2453d);padding:1px 5px;border-radius:2px;text-shadow:none}',
    '.p3-call{position:absolute;transform:translate(-50%,-100%);margin-top:-14px;max-width:240px;padding:6px 9px;background:#11161b;border:1px solid #2a333d;border-left:3px solid var(--p3v,#c2453d);border-radius:2px;color:#e8edf1;font-size:12px;white-space:normal}',
    '.p3-call b{display:block;font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:var(--p3v,#c2453d);margin-bottom:2px}',
    '.p3-bar{position:absolute;left:12px;bottom:12px;display:flex;gap:6px;flex-wrap:wrap;z-index:2}',
    '.p3-bar button{font:inherit;font-size:12px;padding:5px 10px;background:#11161b;color:#d6dde4;border:1px solid #2a333d;border-radius:3px;cursor:pointer}',
    '.p3-bar button:hover{border-color:#4a5562}',
    '.p3-bar button[aria-pressed=true]{border-color:#d99a2b;color:#fff;box-shadow:inset 0 -2px 0 #d99a2b}',
    '.p3-bar button:focus-visible{outline:2px solid #d99a2b;outline-offset:1px}',
    '.p3-legend{position:absolute;right:12px;bottom:12px;max-width:300px;padding:7px 10px;background:#11161b;border:1px solid #2a333d;border-radius:3px;font-size:11px;color:#aab4be;z-index:2}',
    '.p3-legend i{display:inline-block;width:8px;height:8px;border-radius:50%;margin:0 4px 0 9px;vertical-align:0}',
    '.p3-legend i:first-child{margin-left:0}',
    '.p3-legend div+div{margin-top:3px}',
    '.p3-tip{position:absolute;display:none;pointer-events:none;max-width:280px;padding:6px 9px;background:#11161b;border:1px solid #2a333d;border-radius:3px;color:#fff;font-size:12px;z-index:3}',
    '.p3-msg{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;padding:24px;text-align:center;color:#aab4be}',
    '@media (max-width:560px){.p3-legend{display:none}}'
  ].join('\n');

  function ensureStyle() {
    if (document.getElementById('p3-style')) return;
    var s = el('style');
    s.id = 'p3-style';
    s.textContent = CSS;
    document.head.appendChild(s);
  }

  // ---- instance ------------------------------------------------------------------------------------
  function Inst(root, opts) {
    this.root = root;
    this.opts = opts;
    this.cards = Array.isArray(opts.cards) ? opts.cards : [];
    this.activeId = opts.activeCardId || null;
    this.anims = [];
    this.frozenNow = null;
    this.dead = false;
    this.rise = 0;
    this.cutOn = false;
    this.wallsUp = true;
    this.mode3d = true;
    this.listeners = [];
  }

  Inst.prototype.now = function () { return this.frozenNow != null ? this.frozenNow : performance.now(); };

  Inst.prototype.animate = function (dur, fn, opt) {
    var self = this;
    opt = opt || {};
    if (reducedMotion() && !opt.always) dur = 0;
    return new Promise(function (resolve) {
      var a = {t0: self.now(), dur: dur, fn: fn, done: resolve, tag: opt.tag};
      if (opt.tag) self.anims = self.anims.filter(function (b) { if (b.tag === opt.tag) { b.done(false); return false; } return true; });
      if (dur <= 0) { fn(1); resolve(true); return; }
      fn(0);
      self.anims.push(a);
    });
  };

  Inst.prototype.stepAnims = function () {
    var t = this.now(), keep = [];
    for (var i = 0; i < this.anims.length; i++) {
      var a = this.anims[i], p = Math.min(1, Math.max(0, (t - a.t0) / a.dur));
      a.fn(ease(p));
      if (p >= 1) a.done(true); else keep.push(a);
    }
    this.anims = keep;
  };

  Inst.prototype.on = function (target, ev, fn, o) {
    target.addEventListener(ev, fn, o);
    this.listeners.push([target, ev, fn, o]);
  };

  // pt <-> world (metres, sheet centred on the origin, plan y -> world +z)
  Inst.prototype.wx = function (x) { return (x - this.cx) * this.k; };
  Inst.prototype.wz = function (y) { return (y - this.cy) * this.k; };

  Inst.prototype.build = function (T, data) {
    var self = this;
    this.T = T;
    this.data = data;
    var sp = data.scale && data.scale.pt_per_m ? data.scale.pt_per_m : DEFAULT_PT_PER_M;
    this.k = 1 / sp;
    this.cx = data.size_pt[0] / 2;
    this.cy = data.size_pt[1] / 2;
    var b = data.bounds_pt || [0, 0, data.size_pt[0], data.size_pt[1]];
    this.bounds = [this.wx(b[0]), this.wz(b[1]), this.wx(b[2]), this.wz(b[3])];

    var r = this.renderer = new T.WebGLRenderer({antialias: true, preserveDrawingBuffer: !!this.opts.freeze});
    r.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    r.outputColorSpace = T.SRGBColorSpace;
    r.localClippingEnabled = true;
    r.shadowMap.enabled = true;
    r.shadowMap.type = T.PCFSoftShadowMap;
    r.setClearColor(COL.bg, 1);
    this.root.insertBefore(r.domElement, this.root.firstChild);

    var scene = this.scene = new T.Scene();
    this.cam = new T.OrthographicCamera(-1, 1, 1, -1, 0.1, 2000);

    // lights: soft sky fill + one sun for the contact shadow
    scene.add(new T.HemisphereLight(0xffffff, 0xcfc6b4, 2.1));
    var sun = new T.DirectionalLight(0xffffff, 2.4);
    var bw = this.bounds[2] - this.bounds[0], bd = this.bounds[3] - this.bounds[1];
    var bcx = (this.bounds[0] + this.bounds[2]) / 2, bcz = (this.bounds[1] + this.bounds[3]) / 2;
    sun.position.set(bcx - 30, 60, bcz - 22);
    sun.target.position.set(bcx, 0, bcz);
    sun.castShadow = true;
    var half = Math.max(bw, bd) / 2 + 8;
    var sc = sun.shadow.camera;
    sc.left = -half; sc.right = half; sc.top = half; sc.bottom = -half; sc.near = 1; sc.far = 200;
    sun.shadow.mapSize.set(2048, 2048);
    sun.shadow.bias = -0.0004;
    sun.shadow.radius = 3;
    scene.add(sun, sun.target);

    // paper: the rendered sheet, plus a sharper render of the building extent laid over it
    var W = data.size_pt[0] * this.k, D = data.size_pt[1] * this.k;
    var loader = new T.TextureLoader();
    var maxAniso = r.capabilities.getMaxAnisotropy();
    function tex(url) {
      return new Promise(function (res, rej) {
        loader.load(url, function (t) { t.colorSpace = T.SRGBColorSpace; t.anisotropy = maxAniso; res(t); }, undefined, rej);
      });
    }
    function plane(w, d, t, x, z, y) {
      var g = new T.PlaneGeometry(w, d);
      g.rotateX(-Math.PI / 2);
      var m = new T.Mesh(g, new T.MeshBasicMaterial({map: t, toneMapped: false}));
      m.position.set(x, y, z);
      scene.add(m);
      return m;
    }
    var texs = [tex(data.texture_url).then(function (t) { self.sheetMesh = plane(W, D, t, 0, 0, 0); })];
    if (data.detail) {
      var dr = data.detail.rect_pt;
      texs.push(tex(data.detail.url).then(function (t) {
        plane((dr[2] - dr[0]) * self.k, (dr[3] - dr[1]) * self.k, t, self.wx((dr[0] + dr[2]) / 2), self.wz((dr[1] + dr[3]) / 2), 0.004);
      }));
    }
    // sheet edge in ink
    var edge = new T.LineSegments(new T.EdgesGeometry(new T.PlaneGeometry(W, D).rotateX(-Math.PI / 2)),
      new T.LineBasicMaterial({color: 0x3a4652}));
    edge.position.y = 0.001;
    scene.add(edge);

    // contact shadow catcher
    var sh = new T.Mesh(new T.PlaneGeometry(bw + 16, bd + 16).rotateX(-Math.PI / 2), new T.ShadowMaterial({opacity: 0.16}));
    sh.position.set(bcx, 0.008, bcz);
    sh.receiveShadow = true;
    scene.add(sh);

    // rooms: faint tint, the active card's room fills with its verdict colour
    this.roomMeshes = {};
    var roomsG = new T.Group();
    (data.rooms || []).forEach(function (rm) {
      if (!rm.polygon_pt || rm.polygon_pt.length < 3) return;
      var s = new T.Shape(rm.polygon_pt.map(function (p) { return new T.Vector2(self.wx(p[0]), -self.wz(p[1])); }));
      var g = new T.ShapeGeometry(s);
      g.rotateX(-Math.PI / 2);
      var m = new T.Mesh(g, new T.MeshBasicMaterial({color: COL.room, transparent: true, opacity: 0.07, depthWrite: false,
        polygonOffset: true, polygonOffsetFactor: -1, polygonOffsetUnits: -1}));
      m.position.y = 0.012;
      m.renderOrder = 1;
      roomsG.add(m);
      self.roomMeshes[rm.number] = m;
    });
    scene.add(roomsG);

    // walls: the traced wall polygons, extruded
    var shapes = [];
    (data.walls || []).forEach(function (w) {
      var s = new T.Shape(w.outer.map(function (p) { return new T.Vector2(self.wx(p[0]), -self.wz(p[1])); }));
      (w.holes || []).forEach(function (h) {
        s.holes.push(new T.Path(h.map(function (p) { return new T.Vector2(self.wx(p[0]), -self.wz(p[1])); })));
      });
      shapes.push(s);
    });
    this.cut = new T.Plane(new T.Vector3(0, -1, 0), 1000);
    var wallsG = this.wallsG = new T.Group();
    if (shapes.length) {
      var g = new T.ExtrudeGeometry(shapes, {depth: WALL_H, bevelEnabled: false, curveSegments: 4});
      g.rotateX(-Math.PI / 2);
      var front = new T.Mesh(g, new T.MeshStandardMaterial({color: COL.wall, roughness: 0.92, metalness: 0,
        clippingPlanes: [this.cut]}));
      front.castShadow = true;
      // back faces in ink: where the section plane cuts a wall, its solid reads as poché
      var back = new T.Mesh(g, new T.MeshBasicMaterial({color: COL.cap, side: T.BackSide, clippingPlanes: [this.cut]}));
      var edges = new T.LineSegments(new T.EdgesGeometry(g, 25), new T.LineBasicMaterial({color: COL.ink,
        clippingPlanes: [this.cut]}));
      wallsG.add(back, front, edges);
      // cut line: the wall outlines at the section height
      var pos = [];
      (data.walls || []).forEach(function (w) {
        [w.outer].concat(w.holes || []).forEach(function (ring) {
          for (var i = 0; i < ring.length; i++) {
            var a = ring[i], c = ring[(i + 1) % ring.length];
            pos.push(self.wx(a[0]), 0, self.wz(a[1]), self.wx(c[0]), 0, self.wz(c[1]));
          }
        });
      });
      var lg = new T.BufferGeometry();
      lg.setAttribute('position', new T.Float32BufferAttribute(pos, 3));
      this.cutLine = new T.LineSegments(lg, new T.LineBasicMaterial({color: 0x10161c}));
      this.cutLine.visible = false;
      scene.add(this.cutLine);
    }
    scene.add(wallsG);

    this.buildPins();
    this.buildDom();
    this.setRise(0);
    return Promise.all(texs);
  };

  Inst.prototype.cardsOnSheet = function () {
    var sheet = this.data.sheet;
    return this.cards.filter(function (c) {
      return (c.evidence || []).some(function (e) { return e && e.sheet === sheet && Array.isArray(e.bbox) && e.bbox.length >= 4; });
    });
  };

  Inst.prototype.roomOf = function (card) {
    var rooms = this.data.rooms || [], sheet = this.data.sheet;
    var evs = (card.evidence || []).filter(function (e) { return e && e.sheet === sheet && Array.isArray(e.bbox); });
    for (var i = 0; i < evs.length; i++) {
      var named = evs[i].room && rooms.find(function (r) { return r.number === String(evs[i].room).toUpperCase(); });
      if (named) return named;
    }
    for (i = 0; i < evs.length; i++) {
      var b = evs[i].bbox, x = (b[0] + b[2]) / 2, y = (b[1] + b[3]) / 2;
      var hit = rooms.find(function (r) { return r.polygon_pt && r.polygon_pt.length > 2 && pointInRing(x, y, r.polygon_pt); });
      if (hit) return hit;
    }
    return null;
  };

  Inst.prototype.buildPins = function () {
    var T = this.T, self = this, sheet = this.data.sheet;
    this.pins = [];
    this.pinHits = [];
    var g = this.pinsG = new T.Group();
    var stemG = new T.CylinderGeometry(0.06, 0.06, PIN_H, 10);
    stemG.translate(0, PIN_H / 2, 0);
    var headG = new T.SphereGeometry(0.36, 24, 16);
    var baseG = new T.CircleGeometry(0.34, 28).rotateX(-Math.PI / 2);
    var ringG = new T.RingGeometry(0.42, 0.56, 40).rotateX(-Math.PI / 2);
    var glowT = (function () {
      var c = document.createElement('canvas');
      c.width = c.height = 64;
      var x = c.getContext('2d'), gr = x.createRadialGradient(32, 32, 0, 32, 32, 32);
      gr.addColorStop(0, 'rgba(255,255,255,1)');
      gr.addColorStop(0.35, 'rgba(255,255,255,.35)');
      gr.addColorStop(1, 'rgba(255,255,255,0)');
      x.fillStyle = gr;
      x.fillRect(0, 0, 64, 64);
      return new T.CanvasTexture(c);
    })();
    var seen = {};
    this.cardsOnSheet().forEach(function (card) {
      var v = verdictKey(card.verdict), col = COL[v];
      (card.evidence || []).forEach(function (e, idx) {
        if (!e || e.sheet !== sheet || !Array.isArray(e.bbox)) return;
        var x = self.wx((e.bbox[0] + e.bbox[2]) / 2), z = self.wz((e.bbox[1] + e.bbox[3]) / 2);
        var key = Math.round(x / 0.6) + ',' + Math.round(z / 0.6) + ',' + card.id;
        if (seen[key]) return;          // one pin per spot per card
        seen[key] = 1;
        var grp = new T.Group();
        grp.position.set(x, 0, z);
        var mat = new T.MeshStandardMaterial({color: col, roughness: 0.45, emissive: col, emissiveIntensity: 0.15});
        var stem = new T.Mesh(stemG, mat), head = new T.Mesh(headG, mat);
        head.position.y = PIN_H;
        stem.castShadow = head.castShadow = true;
        var base = new T.Mesh(baseG, new T.MeshBasicMaterial({color: col, transparent: true, opacity: 0.55, depthWrite: false}));
        base.position.y = 0.02;
        var glow = new T.Sprite(new T.SpriteMaterial({map: glowT, color: col, transparent: true, opacity: 0,
          blending: T.AdditiveBlending, depthWrite: false}));
        glow.position.y = PIN_H;
        glow.scale.setScalar(1.5);
        var ring = new T.Mesh(ringG, new T.MeshBasicMaterial({color: col, transparent: true, opacity: 0, depthWrite: false,
          side: T.DoubleSide}));
        ring.position.y = 0.03;
        grp.add(stem, head, base, glow, ring);
        var u = {card: card, ev: e, idx: idx};
        stem.userData = head.userData = u;
        g.add(grp);
        self.pins.push({grp: grp, card: card, ev: e, idx: idx, glow: glow, ring: ring, head: head, v: v});
        self.pinHits.push(stem, head);
      });
    });
    this.scene.add(g);
  };

  Inst.prototype.buildDom = function () {
    var self = this;
    var labels = this.labelsEl = el('div', 'p3-labels');
    this.roomLabels = (this.data.rooms || []).map(function (rm) {
      var d = el('div', 'p3-room');
      d.appendChild(el('b', null, rm.number));
      if (rm.name) d.appendChild(el('span', null, rm.name));
      labels.appendChild(d);
      return {el: d, rm: rm, x: self.wx(rm.label_pt[0]), z: self.wz(rm.label_pt[1])};
    });
    this.call = el('div', 'p3-call');
    this.call.style.display = 'none';
    labels.appendChild(this.call);
    this.root.appendChild(labels);

    this.tip = el('div', 'p3-tip');
    this.root.appendChild(this.tip);

    var bar = el('div', 'p3-bar');
    function btn(text, title, fn) {
      var b = el('button', null, text);
      b.type = 'button';
      b.title = title;
      b.addEventListener('click', fn);
      bar.appendChild(b);
      return b;
    }
    this.b3d = btn('3D', 'Isometric view', function () { self.setMode(true); });
    this.bPlan = btn('Plan', 'Top-down plan view', function () { self.setMode(false); });
    this.bWalls = btn('Walls', 'Raise / lower the walls', function () { self.setWalls(!self.wallsUp); });
    this.bCut = btn('Section', 'Cut the walls at ' + CUT_H + ' m', function () { self.setSection(!self.cutOn); });
    this.bTour = btn('Tour', 'Fly past each verdict pin', function () { self.tour(); });
    this.root.appendChild(bar);

    var lg = el('div', 'p3-legend');
    var row = el('div');
    ['conflict', 'consistent', 'unclear'].forEach(function (k) {
      var i = el('i');
      i.style.background = '#' + COL[k].toString(16).padStart(6, '0');
      row.appendChild(i);
      row.appendChild(document.createTextNode(VWORD[k]));
    });
    lg.appendChild(row);
    var sc = this.data.scale ? this.data.scale.text : 'scale not stated; 1/8" = 1\'-0" assumed';
    lg.appendChild(el('div', null, (this.opts.sheetLabel || this.data.sheet) + ' · ' + sc));
    lg.appendChild(el('div', null, 'Walls traced from the PDF’s vector wall strokes; ' + WALL_H.toFixed(1) + ' m height assumed.'));
    this.root.appendChild(lg);
    this.syncButtons();
  };

  Inst.prototype.syncButtons = function () {
    if (!this.b3d) return;
    this.b3d.setAttribute('aria-pressed', String(this.mode3d));
    this.bPlan.setAttribute('aria-pressed', String(!this.mode3d));
    this.bWalls.setAttribute('aria-pressed', String(this.wallsUp));
    this.bCut.setAttribute('aria-pressed', String(this.cutOn));
  };

  // ---- state setters -------------------------------------------------------------------------------
  Inst.prototype.setRise = function (p) {
    this.rise = p;
    this.wallsG.scale.y = Math.max(0.0005, p);
    this.wallsG.visible = p > 0.002;
    var self = this, act = this.activeRoom ? this.activeRoom.number : null;
    if (this.roomMeshes) Object.keys(this.roomMeshes).forEach(function (n) {   // flat sheet = the page, untinted
      if (n !== act) self.roomMeshes[n].material.opacity = 0.07 * Math.min(1, 2 * p);
    });
    this.pinsG && this.pins.forEach(function (pin) { pin.grp.scale.y = Math.max(0.0005, Math.min(1, p * 1.25)); });
    this.updateCutLine();
  };

  Inst.prototype.updateCutLine = function () {
    if (!this.cutLine) return;
    var h = Math.min(this.cut.constant, WALL_H * this.rise);
    this.cutLine.visible = this.cut.constant < WALL_H && this.rise > 0.01;
    this.cutLine.position.y = h + 0.002;
  };

  Inst.prototype.setCut = function (h) {
    this.cut.constant = h;
    this.updateCutLine();
  };

  Inst.prototype.applyActive = function (fill) {
    var self = this, act = this.cards.find(function (c) { return c.id === self.activeId; }) || null;
    var room = act ? this.roomOf(act) : null;
    this.activeRoom = room;
    var vcol = act ? COL[verdictKey(act.verdict)] : COL.room;
    Object.keys(this.roomMeshes).forEach(function (n) {
      var m = self.roomMeshes[n];
      if (room && n === room.number) { m.material.color.setHex(vcol); m.material.opacity = 0.07 + 0.33 * fill; }
      else { m.material.color.setHex(COL.room); m.material.opacity = 0.07 * Math.min(1, 2 * self.rise); }
    });
    var css = '#' + vcol.toString(16).padStart(6, '0');
    this.roomLabels.forEach(function (l) {
      var on = !!room && l.rm.number === room.number && fill > 0.5;
      l.el.classList.toggle('p3-act', on);
      l.el.style.setProperty('--p3v', css);
    });
    this.pins.forEach(function (p) {
      var on = act && p.card.id === act.id;
      p.on = on;
      p.glow.material.opacity = on ? 0.75 * fill : 0;
      p.head.scale.setScalar(on ? 1 + 0.35 * fill : (act ? 0.8 : 1));
    });
    if (act && fill > 0.5) {
      this.call.textContent = '';
      this.call.appendChild(el('b', null, VWORD[verdictKey(act.verdict)] + (room ? ' · ' + room.number : '')));
      var t = String(act.claim || '');
      this.call.appendChild(document.createTextNode(t.length > 110 ? t.slice(0, 107) + '…' : t));
      this.call.style.setProperty('--p3v', css);
      this.callPin = this.pins.find(function (p) { return p.on; }) || null;
    } else {
      this.callPin = null;
    }
  };

  // ---- camera --------------------------------------------------------------------------------------
  Inst.prototype.size = function () {
    return {w: this.root.clientWidth || 300, h: this.root.clientHeight || 200};
  };

  Inst.prototype.applyCam = function () {
    var c = this.cs, s = this.size(), asp = s.w / s.h, cam = this.cam, T = this.T;
    cam.left = -c.halfH * asp; cam.right = c.halfH * asp; cam.top = c.halfH; cam.bottom = -c.halfH;
    var D = 400, sp = Math.sin(c.pol), cp = Math.cos(c.pol), sa = Math.sin(c.az), ca = Math.cos(c.az);
    cam.position.set(c.tx + D * sp * sa, D * cp, c.tz + D * sp * ca);
    cam.up.set(-cp * sa, sp, -cp * ca);
    cam.lookAt(new T.Vector3(c.tx, 0, c.tz));
    cam.near = 1; cam.far = D * 2 + 50;
    cam.updateProjectionMatrix();
  };

  // camera state framing a world rect [x0, z0, x1, z1] at the given angles (approximate for tilted views)
  Inst.prototype.frame = function (rect, az, pol, pad, fit) {
    var s = this.size(), asp = s.w / s.h;
    var w = rect[2] - rect[0], d = rect[3] - rect[1];
    var ca = Math.abs(Math.cos(az)), sa = Math.abs(Math.sin(az));
    var sw = w * ca + d * sa, sd = (w * sa + d * ca) * Math.cos(pol) + (pol > 0.05 ? WALL_H * Math.sin(pol) : 0);
    var a = sd / 2, b = sw / 2 / asp;
    var halfH = (fit === 'cover' ? Math.min(a, b) : Math.max(a, b)) * (pad || 1);
    return {tx: (rect[0] + rect[2]) / 2, tz: (rect[1] + rect[3]) / 2, az: az, pol: pol, halfH: halfH};
  };

  Inst.prototype.flyTo = function (to, dur) {
    var self = this, from = Object.assign({}, this.cs);
    return this.animate(dur, function (p) {
      self.cs = {tx: lerp(from.tx, to.tx, p), tz: lerp(from.tz, to.tz, p), az: angLerp(from.az, to.az, p),
        pol: lerp(from.pol, to.pol, p), halfH: Math.exp(lerp(Math.log(from.halfH), Math.log(to.halfH), p))};
    }, {tag: 'cam'});
  };

  Inst.prototype.sheetRect = function () {
    var r = this.opts.focusRectPt;
    if (Array.isArray(r) && r.length === 4) return [this.wx(r[0]), this.wz(r[1]), this.wx(r[2]), this.wz(r[3])];
    var W = this.data.size_pt[0] * this.k / 2, D = this.data.size_pt[1] * this.k / 2;
    return [-W, -D, W, D];
  };

  Inst.prototype.flatState = function () { return this.frame(this.sheetRect(), 0, 0, 1, this.opts.fit); };
  Inst.prototype.overviewState = function () {
    return this.frame(this.bounds, this.mode3d ? ISO.az : 0, this.mode3d ? ISO.pol : 0, 1.04);
  };

  Inst.prototype.cardRect = function (card) {
    var sheet = this.data.sheet, self = this, room = this.roomOf(card);
    var xs = [], zs = [];
    if (room) { xs.push(this.wx(room.bbox_pt[0]), this.wx(room.bbox_pt[2])); zs.push(this.wz(room.bbox_pt[1]), this.wz(room.bbox_pt[3])); }
    (card.evidence || []).forEach(function (e) {
      if (e && e.sheet === sheet && Array.isArray(e.bbox)) {
        xs.push(self.wx(e.bbox[0]), self.wx(e.bbox[2]));
        zs.push(self.wz(e.bbox[1]), self.wz(e.bbox[3]));
      }
    });
    if (!xs.length) return null;
    var m = 4.5;
    return [Math.min.apply(null, xs) - m, Math.min.apply(null, zs) - m, Math.max.apply(null, xs) + m, Math.max.apply(null, zs) + m];
  };

  Inst.prototype.focusState = function (card) {
    var r = this.cardRect(card);
    return r ? this.frame(r, this.mode3d ? ISO.az : 0, this.mode3d ? FOCUS_POL : 0, 1.3) : null;
  };

  // ---- public actions ------------------------------------------------------------------------------
  Inst.prototype.intro = function () {
    var self = this;
    this.cs = this.flatState();
    this.setRise(0);
    this.applyActive(0);
    var to = this.overviewState();
    return Promise.all([
      this.flyTo(to, INTRO_MS),
      this.animate(INTRO_MS, function (p) { self.setRise(p); }, {tag: 'rise'}),
      this.animate(INTRO_MS, function (p) { self.applyActive(Math.max(0, (p - 0.45) / 0.55)); }, {tag: 'fill'})
    ]);
  };

  Inst.prototype.focus = function (cardId, dur) {
    if (cardId !== undefined) this.activeId = cardId;
    var self = this, card = this.cards.find(function (c) { return c.id === self.activeId; });
    this.applyActive(1);
    if (!card) return Promise.resolve(false);
    var st = this.focusState(card);
    return st ? this.flyTo(st, dur == null ? FLY_MS : dur) : Promise.resolve(false);
  };

  Inst.prototype.flatten = function () {
    var self = this, from = this.rise;
    this.mode3d = true;
    this.wallsUp = false;
    this.cutOn = false;
    this.setCut(1000);
    this.syncButtons();
    return Promise.all([
      this.flyTo(this.flatState(), INTRO_MS),
      this.animate(INTRO_MS, function (p) { self.setRise(from * (1 - p)); }, {tag: 'rise'})
    ]);
  };

  Inst.prototype.setMode = function (is3d) {
    this.mode3d = is3d;
    this.syncButtons();
    var self = this, card = this.cards.find(function (c) { return c.id === self.activeId; });
    var base = card && this.focusState(card);
    var to = Object.assign({}, this.cs, {az: is3d ? ISO.az : 0, pol: is3d ? ISO.pol : 0});
    if (!base) to = this.overviewState();
    return this.flyTo(to, TOGGLE_MS);
  };

  Inst.prototype.setWalls = function (up) {
    var self = this, from = this.rise;
    this.wallsUp = up;
    this.syncButtons();
    return this.animate(1200, function (p) { self.setRise(lerp(from, up ? 1 : 0, p)); }, {tag: 'rise'});
  };

  Inst.prototype.setSection = function (on) {
    var self = this, from = Math.min(this.cut.constant, WALL_H + 0.01);
    this.cutOn = on;
    this.syncButtons();
    if (on && !this.wallsUp) this.setWalls(true);
    return this.animate(TOGGLE_MS, function (p) {
      var h = lerp(from, on ? CUT_H : WALL_H + 0.01, p);
      self.setCut(!on && p >= 1 ? 1000 : h);
    }, {tag: 'cut'});
  };

  Inst.prototype.tour = function () {
    var self = this, seq = this.cardsOnSheet(), token = {};
    this.tourToken = token;
    var i = 0;
    function next() {
      if (self.dead || self.tourToken !== token || i >= seq.length) {
        if (self.tourToken === token) self.tourToken = null;
        return Promise.resolve(i >= seq.length);
      }
      var c = seq[i++];
      self.root.dispatchEvent(new CustomEvent('plan3d:tour', {bubbles: true, detail: {cardId: c.id, step: i, of: seq.length}}));
      return self.focus(c.id).then(function () {
        return new Promise(function (res) { setTimeout(res, reducedMotion() ? 3000 : 2200); });
      }).then(next);
    }
    return next();
  };

  // ---- interaction ---------------------------------------------------------------------------------
  Inst.prototype.userMoved = function () {
    this.tourToken = null;
    this.anims = this.anims.filter(function (a) { if (a.tag === 'cam') { a.done(false); return false; } return true; });
  };

  Inst.prototype.pick = function (cx, cy) {
    var T = this.T, rect = this.renderer.domElement.getBoundingClientRect();
    var v = new T.Vector2(((cx - rect.left) / rect.width) * 2 - 1, -((cy - rect.top) / rect.height) * 2 + 1);
    var rc = new T.Raycaster();
    rc.setFromCamera(v, this.cam);
    var hit = rc.intersectObjects(this.pinHits, false)[0];
    return hit ? hit.object.userData : null;
  };

  Inst.prototype.bindInput = function () {
    var self = this, cv = this.renderer.domElement, ptrs = new Map(), down = null, pinch = null;
    this.on(cv, 'contextmenu', function (e) { e.preventDefault(); });
    this.on(cv, 'pointerdown', function (e) {
      cv.setPointerCapture(e.pointerId);
      ptrs.set(e.pointerId, {x: e.clientX, y: e.clientY});
      down = {x: e.clientX, y: e.clientY, pan: e.button === 2 || e.shiftKey || e.ctrlKey || e.metaKey, moved: false};
      if (ptrs.size === 2) {
        var a = Array.from(ptrs.values());
        pinch = {d: Math.hypot(a[0].x - a[1].x, a[0].y - a[1].y), h: self.cs.halfH};
      }
      cv.classList.add('p3-drag');
    });
    this.on(cv, 'pointermove', function (e) {
      var prev = ptrs.get(e.pointerId);
      if (!prev) {
        var u = self.pick(e.clientX, e.clientY);
        var r = self.root.getBoundingClientRect();
        if (u) {
          self.tip.textContent = (VWORD[verdictKey(u.card.verdict)] + ': ' + (u.card.claim || '')).slice(0, 220);
          self.tip.style.display = 'block';
          self.tip.style.left = Math.min(e.clientX - r.left + 14, r.width - 290) + 'px';
          self.tip.style.top = (e.clientY - r.top + 14) + 'px';
          cv.style.cursor = 'pointer';
        } else { self.tip.style.display = 'none'; cv.style.cursor = ''; }
        return;
      }
      var dx = e.clientX - prev.x, dy = e.clientY - prev.y;
      ptrs.set(e.pointerId, {x: e.clientX, y: e.clientY});
      if (down && Math.hypot(e.clientX - down.x, e.clientY - down.y) > 5) down.moved = true;
      if (!down || !down.moved) return;
      self.userMoved();
      if (ptrs.size === 2 && pinch) {
        var a = Array.from(ptrs.values());
        var d = Math.hypot(a[0].x - a[1].x, a[0].y - a[1].y) || 1;
        self.cs.halfH = Math.min(120, Math.max(1.5, pinch.h * pinch.d / d));
        return;
      }
      if (down.pan) {
        var s = self.size(), m = 2 * self.cs.halfH / s.h, ca = Math.cos(self.cs.az), sa = Math.sin(self.cs.az);
        var k = 1 / Math.max(0.35, Math.cos(self.cs.pol));
        self.cs.tx -= (dx * ca + dy * sa * k) * m;
        self.cs.tz -= (-dx * sa + dy * ca * k) * m;
      } else {
        self.cs.az -= dx * 0.006;
        self.cs.pol = Math.min(1.3, Math.max(0, self.cs.pol - dy * 0.005));
        self.mode3d = self.cs.pol > 0.05;
        self.syncButtons();
      }
    });
    function up(e) {
      ptrs.delete(e.pointerId);
      if (ptrs.size < 2) pinch = null;
      cv.classList.remove('p3-drag');
      if (down && !down.moved && e.type === 'pointerup') {
        var u = self.pick(e.clientX, e.clientY);
        if (u) {
          self.focus(u.card.id);
          self.root.dispatchEvent(new CustomEvent('plan3d:select', {bubbles: true,
            detail: {cardId: u.card.id, sheet: self.data.sheet, bbox: u.ev.bbox, evidenceIndex: u.idx}}));
        }
      }
      if (!ptrs.size) down = null;
    }
    this.on(cv, 'pointerup', up);
    this.on(cv, 'pointercancel', up);
    this.on(cv, 'wheel', function (e) {
      e.preventDefault();
      self.userMoved();
      self.cs.halfH = Math.min(120, Math.max(1.5, self.cs.halfH * Math.exp(e.deltaY * 0.0012)));
    }, {passive: false});
  };

  // ---- frame ---------------------------------------------------------------------------------------
  Inst.prototype.resize = function () {
    var s = this.size();
    this.renderer.setSize(s.w, s.h, false);
  };

  Inst.prototype.render = function () {
    this.stepAnims();
    this.applyCam();
    var t = this.now(), still = reducedMotion();
    var self = this;
    this.pins.forEach(function (p) {
      if (p.on) {
        var ph = still ? 0.35 : (t % 1600) / 1600;
        p.ring.scale.setScalar(1 + 1.6 * ph);
        p.ring.material.opacity = 0.9 * (1 - ph);
      } else {
        p.ring.material.opacity = 0;
      }
    });
    this.renderer.render(this.scene, this.cam);
    // labels: project room anchors and the active pin's callout
    var s = this.size(), v = new this.T.Vector3(), small = this.cs.halfH > 14;
    this.roomLabels.forEach(function (l) {
      v.set(l.x, 0.05, l.z).project(self.cam);
      var on = v.z < 1 && Math.abs(v.x) < 1.05 && Math.abs(v.y) < 1.05 && self.rise > 0.25;
      l.el.style.display = on ? '' : 'none';
      if (on) {
        l.el.style.left = ((v.x + 1) / 2 * s.w).toFixed(1) + 'px';
        l.el.style.top = ((1 - v.y) / 2 * s.h).toFixed(1) + 'px';
        l.el.classList.toggle('p3-small', small);
      }
    });
    if (this.callPin && this.rise > 0.5) {
      var g = this.callPin.grp;
      v.set(g.position.x, PIN_H * g.scale.y + 0.35, g.position.z).project(this.cam);
      this.call.style.display = '';
      this.call.style.left = ((v.x + 1) / 2 * s.w).toFixed(1) + 'px';
      this.call.style.top = ((1 - v.y) / 2 * s.h).toFixed(1) + 'px';
    } else {
      this.call.style.display = 'none';
    }
  };

  Inst.prototype.loop = function () {
    var self = this;
    function tick() {
      if (self.dead) return;
      self.raf = requestAnimationFrame(tick);
      self.render();
    }
    tick();
  };

  Inst.prototype.destroy = function () {
    this.dead = true;
    cancelAnimationFrame(this.raf);
    this.anims.forEach(function (a) { a.done(false); });
    this.anims = [];
    this.listeners.forEach(function (l) { l[0].removeEventListener(l[1], l[2], l[3]); });
    if (this.ro) this.ro.disconnect();
    if (this.scene) {
      this.scene.traverse(function (o) {
        if (o.geometry) o.geometry.dispose();
        if (o.material) {
          if (o.material.map) o.material.map.dispose();
          o.material.dispose();
        }
      });
    }
    if (this.renderer) { this.renderer.dispose(); if (this.renderer.forceContextLoss) this.renderer.forceContextLoss(); }
    while (this.root.firstChild) this.root.removeChild(this.root.firstChild);
    if (this.root.parentNode) this.root.parentNode.removeChild(this.root);
  };

  // deterministic still frames for screenshots: state flat|rise|overview|focus|section|plan, t in 0..1
  Inst.prototype.applyFreeze = function (fz) {
    var t = Math.min(1, Math.max(0, Number(fz.t != null ? fz.t : 1)));
    this.frozenNow = 0;
    var st = String(fz.state || 'overview');
    if (st === 'flat') { this.cs = this.flatState(); this.setRise(0); this.applyActive(0); return; }
    if (st === 'rise') { this.intro(); this.frozenNow = t * INTRO_MS; return; }
    this.cs = this.overviewState();
    this.setRise(1);
    this.applyActive(1);
    if (st === 'plan') { this.mode3d = false; this.cs = this.overviewState(); this.syncButtons(); }
    if (st === 'section') { this.cutOn = true; this.setCut(lerp(WALL_H, CUT_H, ease(t))); this.syncButtons(); }
    if (st === 'focus') {
      var self = this, card = this.cards.find(function (c) { return c.id === self.activeId; });
      var to = card && this.focusState(card);
      if (to) { this.flyTo(to, FLY_MS); this.frozenNow = t * FLY_MS; }
    }
  };

  // ---- module API ----------------------------------------------------------------------------------
  function mount(host, opts) {
    unmount();
    opts = opts || {};
    ensureStyle();
    var root = el('div', 'p3-root');
    host.appendChild(root);
    var me = inst = new Inst(root, opts);
    if (!webglOK()) {
      root.appendChild(el('div', 'p3-msg', '3D view needs WebGL, which this browser has turned off. The 2D sheet still has every verdict.'));
      return Promise.resolve(false);
    }
    var sheet = String(opts.sheet || '');
    if (!/^[a-z0-9]{2,8}-p\d{3}$/.test(sheet)) {
      root.appendChild(el('div', 'p3-msg', 'No 3D plan for this sheet.'));
      return Promise.resolve(false);
    }
    var msg = el('div', 'p3-msg', 'Loading the plan geometry…');
    root.appendChild(msg);
    return Promise.all([loadThree(), fetch('/api/plan3d/' + encodeURIComponent(sheet)).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    })]).then(function (res) {
      if (me.dead) return false;
      return me.build(res[0], res[1]).then(function () {
        if (me.dead) return false;
        msg.remove();
        me.resize();
        me.ro = new ResizeObserver(function () { me.resize(); });
        me.ro.observe(root);
        me.bindInput();
        if (opts.freeze) {
          me.applyFreeze(opts.freeze);
          me.render();
          me.loop();
          root.dataset.ready = '1';
          return true;
        }
        var start;
        if (opts.intro === 'rise') start = me.intro();
        else { me.cs = me.overviewState(); me.setRise(1); me.applyActive(1); start = Promise.resolve(); }
        me.loop();
        root.dataset.ready = '1';
        return start.then(function () { return me.activeId && !me.dead ? me.focus(me.activeId) : true; });
      });
    }).catch(function () {
      if (me.dead) return false;
      msg.textContent = 'Could not load the 3D plan for ' + sheet + '.';
      return false;
    });
  }

  function unmount() {
    if (inst) { inst.destroy(); inst = null; }
  }

  function ready() { return inst && inst.cs && !inst.dead ? inst : null; }

  window.Plan3D = {
    mount: mount,
    unmount: unmount,
    focus: function (cardId) {
      var i = ready();
      if (!i && inst && !inst.dead) inst.activeId = cardId;     // still loading: the intro flies there
      return i ? i.focus(cardId) : Promise.resolve(false);
    },
    flatten: function () { var i = ready(); return i ? i.flatten() : Promise.resolve(false); },
    tour: function () { var i = ready(); return i ? i.tour() : Promise.resolve(false); },
    setMode: function (mode) { var i = ready(); return i ? i.setMode(mode !== 'plan') : Promise.resolve(false); },
    section: function (on) { var i = ready(); return i ? i.setSection(!!on) : Promise.resolve(false); }
  };
})();
