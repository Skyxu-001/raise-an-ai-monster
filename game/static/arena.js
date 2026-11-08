// Battle scene: two monsters face each other in one three.js scene.
// With a skeleton (rigged by SkinTokens on the GPU machine, see gpu/rig/), body parts animate: lean in and breathe, swing the tail, flap the wings.
// Without one (not rigged yet, or rigging failed) the whole body moves; with no 3D model yet, the picture is used as a card.
// Models all face +Z with +Y up; bones have no rotation in the bind pose, so rotating around a bone's local X = bending forward or backward.
import * as THREE from "three";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { RoomEnvironment } from "three/addons/environments/RoomEnvironment.js";

const V = THREE.Vector3;
const loader = new GLTFLoader();
const E = new THREE.Euler(), Q = new THREE.Quaternion();
const clamp01 = x => Math.min(1, Math.max(0, x));
const ease = (t, a, b) => { const x = clamp01((t - a) / (b - a)); return x * x * (3 - 2 * x); };  // smooth from 0 to 1 over a→b
const pulse = (t, a, b, c, d) => ease(t, a, b) - ease(t, c, d);                                    // rise, hold, fall

// Particle look per element: colors (bright→dark), gravity (negative floats up), size, speed, spread, additive glow or not
// Some backdrops are bright, like snow and sand: additive glow (colors add up) turns pure white where it overlaps on a bright background, so only fire uses it (a white fire core looks natural);
// the other elements use normal transparency, and their colors should stay away from near-white
const FX = {
  fire:     { colors: [0xfff1a8, 0xffa133, 0xe8461c], gravity: -0.8, size: 0.16, speed: 3.0, spread: 0.20, glow: true },
  water:    { colors: [0xe6f6ff, 0x5fb2ff, 0x1f63d6], gravity: 2.2, size: 0.11, speed: 3.6, spread: 0.10, glow: false },
  ice:      { colors: [0xcff4ff, 0x7fd6f0, 0x38a8d8], gravity: -0.15, size: 0.18, speed: 2.3, spread: 0.24, glow: false },
  electric: { colors: [0xfff27a, 0xffc800, 0xe89a00], gravity: 0, size: 0.09, speed: 5.0, spread: 0.30, glow: false, jitter: 2.2 },
  grass:    { colors: [0xc8f59a, 0x5cc653, 0x2e8b3a], gravity: 0.4, size: 0.12, speed: 2.7, spread: 0.26, glow: false, swirl: 2 },
  rock:     { colors: [0xd2ad84, 0x8d6e4f, 0x5a4535], gravity: 4.5, size: 0.15, speed: 3.6, spread: 0.12, glow: false },
  wind:     { colors: [0xd8fff0, 0x6fd6b4, 0x2f9c7c], gravity: 0, size: 0.14, speed: 3.8, spread: 0.28, glow: false, swirl: 5 },
  shadow:   { colors: [0xd7b4ff, 0x7a55b8, 0x2a1840], gravity: -0.25, size: 0.18, speed: 2.5, spread: 0.22, glow: false },
  normal:   { colors: [0xffffff, 0xeeeeee, 0xbdbdbd], gravity: 0.6, size: 0.10, speed: 3.0, spread: 0.20, glow: false },
};
const fxOf = el => FX[el] || FX.normal;

// ---------- Particles: one pool, updated on the CPU each frame, one round soft-edged point sprite ----------

class Particles {
  constructor(scene, glow, n = 1500) {
    this.n = n; this.next = 0;
    this.pos = new Float32Array(n * 3); this.col = new Float32Array(n * 3);
    this.size = new Float32Array(n); this.alpha = new Float32Array(n);
    this.vel = new Float32Array(n * 3); this.age = new Float32Array(n); this.life = new Float32Array(n);
    this.grav = new Float32Array(n); this.size0 = new Float32Array(n); this.jit = new Float32Array(n);
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.BufferAttribute(this.pos, 3));
    g.setAttribute("pcolor", new THREE.BufferAttribute(this.col, 3));
    g.setAttribute("psize", new THREE.BufferAttribute(this.size, 1));
    g.setAttribute("palpha", new THREE.BufferAttribute(this.alpha, 1));
    this.mat = new THREE.ShaderMaterial({
      uniforms: { scale: { value: 300 } },
      vertexShader: `attribute vec3 pcolor; attribute float psize; attribute float palpha; uniform float scale;
        varying vec3 vC; varying float vA;
        void main() { vC = pcolor; vA = palpha; vec4 mv = modelViewMatrix * vec4(position, 1.0);
          gl_PointSize = psize * scale / max(0.1, -mv.z); gl_Position = projectionMatrix * mv; }`,
      fragmentShader: `varying vec3 vC; varying float vA;
        void main() { float r = length(gl_PointCoord - 0.5); if (r > 0.5) discard;
          gl_FragColor = vec4(vC, vA * smoothstep(0.5, 0.1, r)); }`,
      transparent: true, depthWrite: false, blending: glow ? THREE.AdditiveBlending : THREE.NormalBlending,
    });
    this.points = new THREE.Points(g, this.mat); this.points.frustumCulled = false; scene.add(this.points);
  }
  spawn(p, v, color, size, life, grav = 0, jit = 0) {
    const i = this.next; this.next = (this.next + 1) % this.n;
    this.pos.set([p.x, p.y, p.z], i * 3); this.vel.set([v.x, v.y, v.z], i * 3);
    const c = new THREE.Color(color); this.col.set([c.r, c.g, c.b], i * 3);
    this.size0[i] = size; this.size[i] = size; this.alpha[i] = 1; this.age[i] = 0; this.life[i] = life;
    this.grav[i] = grav; this.jit[i] = jit;
  }
  update(dt) {
    for (let i = 0; i < this.n; i++) {
      if (this.alpha[i] <= 0) continue;
      this.age[i] += dt;
      const k = this.age[i] / this.life[i];
      if (k >= 1) { this.alpha[i] = 0; this.size[i] = 0; continue; }
      const o = i * 3;
      this.vel[o + 1] -= this.grav[i] * dt;
      if (this.jit[i]) for (let a = 0; a < 3; a++) this.vel[o + a] += (Math.random() - 0.5) * this.jit[i];
      for (let a = 0; a < 3; a++) this.pos[o + a] += this.vel[o + a] * dt;
      this.alpha[i] = k < 0.15 ? k / 0.15 : 1 - (k - 0.15) / 0.85;
      this.size[i] = this.size0[i] * (1 + k * 0.8);
    }
    const a = this.points.geometry.attributes;
    a.position.needsUpdate = a.pcolor.needsUpdate = a.psize.needsUpdate = a.palpha.needsUpdate = true;
  }
}

// ---------- One monster ----------

class Puppet {
  constructor(arena, side, data) {
    this.arena = arena; this.side = side; this.data = data;
    this.root = new THREE.Group(); arena.scene.add(this.root);
    this.facing = side === "me" ? Math.PI / 2 - 0.4 : -Math.PI / 2 + 0.4;  // side-on to the opponent, turned slightly toward the camera
    this.homeX = side === "me" ? -1.2 : 1.2;
    this.action = null; this.flash = 0; this.fallen = 0;
  }

  async load() {
    const d = this.data;
    let obj = null, rig = null;
    if (d.rig) {
      try {
        const [g, j] = await Promise.all([loader.loadAsync("/media/" + d.rig),
          fetch("/media/" + d.rig.replace(/\.glb$/, ".json")).then(r => r.json())]);
        obj = g.scene; rig = j;
      } catch (e) { console.warn("Rig failed to load, using whole-body moves instead", e); }
    }
    if (!obj && d.glb) obj = (await loader.loadAsync("/media/" + d.glb)).scene;
    if (!obj && d.image) obj = await imageCard("/media/" + d.image, -this.facing);
    if (!obj) obj = new THREE.Mesh(new THREE.SphereGeometry(0.4, 24, 16), new THREE.MeshStandardMaterial({ color: 0x999999 }));

    obj.updateMatrixWorld(true);  // measuring a skinned mesh needs the bones' world matrices, which aren't computed yet after loading; without this update the size comes out wrong
    const box = new THREE.Box3().setFromObject(obj), size = box.getSize(new V());
    this.box = box;
    const s = Math.min(1.05 / Math.max(size.y, 1e-3), 1.5 / Math.max(size.x, size.z, 1e-3));  // about 1 tall; very flat ones scale by footprint
    obj.position.set(-(box.min.x + box.max.x) / 2, -box.min.y, -(box.min.z + box.max.z) / 2);
    this.body = new THREE.Group(); this.body.add(obj); this.body.scale.setScalar(s); this.s = s;
    this.root.add(this.body);
    this.height = size.y * s;
    this.mats = [];
    obj.traverse(o => {
      if (!o.isMesh) return;
      o.frustumCulled = false;
      for (const m of [].concat(o.material)) if (m.emissive) this.mats.push({ m, e0: m.emissive.clone() });
      if (o.isSkinnedMesh) this.sm = o;
    });
    const anchor = (parent, p) => { const a = new THREE.Object3D(); a.position.fromArray(p); parent.add(a); return a; };
    const c = box.getCenter(new V());
    this.chest = anchor(obj, [c.x, box.min.y + size.y * 0.55, c.z]);
    this.top = anchor(obj, [c.x, box.max.y, c.z]);

    if (this.sm && rig && rig.roles) {
      this.bones = this.sm.skeleton.bones;
      this.rest = this.bones.map(b => b.quaternion.clone());
      const r = rig.roles, j = rig.joints;
      this.trunk = r.head_path || [];
      this.tail = r.tail || [];
      this.wings = r.wings || [];
      this.arms = r.arms || [];
      this.legs = r.legs || [];
      this.other = r.other || [];
      if (["serpent", "floating"].includes(d.body_plan)) { this.other = this.other.concat(this.legs); this.legs = []; }
      this.cx = c.x;
      this.wingSign = this.wings.map(w => Math.sign(j[w[0]][0] - c.x) || 1);
      this.mouth = anchor(this.bones[r.mouth.bone], r.mouth.offset);
      if (this.trunk.length >= 2) {  // neck upright (T. rex, snake) or level (turtle): sets how much of "lean in" is bending at the waist
        const a = j[this.trunk[0]], b = j[this.trunk[this.trunk.length - 1]];
        this.upright = (b[1] - a[1]) / (Math.hypot(b[0] - a[0], b[1] - a[1], b[2] - a[2]) || 1);
      } else this.upright = 0;
      this.tailTip = this.tail.length ? anchor(this.bones[this.tail[this.tail.length - 1]], [0, 0, 0]) : null;
      const limb = this.arms[0] || this.legs[0];
      this.clawTip = limb ? anchor(this.bones[limb[limb.length - 1]], [0, 0, 0]) : null;
      this.hornTip = this.trunk.length ? anchor(this.bones[this.trunk[this.trunk.length - 1]], [0, 0.05, 0.05]) : null;
    } else {
      this.bones = null;
      this.mouth = anchor(obj, [c.x, box.min.y + size.y * 0.72, box.max.z]);
    }
    this.hover = d.can_fly || d.body_plan === "floating" ? 0.18 : 0;
    this.reset(0);
    return this;
  }

  rot(i, x, y = 0, z = 0) { E.set(x, y, z); Q.setFromEuler(E); this.bones[i].quaternion.multiply(Q); }

  // Lean in (amount>0) / lean back (amount<0), keeping the mouth roughly level: the middle bends forward, the last two joints turn back the other way
  lean(amount) {
    if (!this.bones || !this.trunk.length) { this.body.rotation.x = amount * 0.25; return; }
    const t = this.trunk, n = t.length;
    if (n < 3) { this.rot(t[n - 1], amount * 0.4); return; }
    const mid = t.slice(Math.max(1, Math.floor(n / 3)), n - 2);
    const total = 0.75 * amount * (0.35 + 0.65 * clamp01(this.upright));
    for (const j of mid) this.rot(j, total / Math.max(1, mid.length));
    this.rot(t[n - 2], -total * 0.8); this.rot(t[n - 1], -total * 0.25);
  }

  reset(t) {
    const ph = t + (this.side === "me" ? 0 : 1.7);
    this.root.position.set(this.homeX, this.hover + (this.hover ? Math.sin(ph * 2.2) * 0.05 : 0), 0);
    this.root.rotation.set(0, this.facing, 0);
    this.body.rotation.set(0, 0, 0);
    this.body.scale.set(this.s, this.s * (1 + Math.sin(ph * 2) * 0.012), this.s);
    if (!this.bones) return;
    this.bones.forEach((b, i) => b.quaternion.copy(this.rest[i]));
    this.trunk.forEach((j, k) => k > 0 && this.rot(j, Math.sin(ph * 2) * 0.02));           // breathing
    this.tail.forEach((j, k) => this.rot(j, 0, Math.sin(ph * 2.3 - k * 0.7) * 0.14));       // tail swing
    this.other.forEach((c, ci) => c.forEach((j, k) =>                                        // tentacles, coiled snake body
      this.rot(j, Math.sin(ph * 1.7 - k * 0.6 + ci) * 0.09, 0, Math.cos(ph * 1.3 - k * 0.5 + ci) * 0.07)));
    this.wings.forEach((w, i) =>                                                              // flap if it can fly, otherwise move gently
      this.rot(w[0], 0, 0, this.wingSign[i] * Math.sin(ph * (this.data.can_fly ? 9 : 1.5)) * (this.data.can_fly ? 0.45 : 0.06)));
  }

  forward() { return new V(Math.sin(this.facing), 0, Math.cos(this.facing)); }
  world(o) { return o.getWorldPosition(new V()); }

  update(t, dt) {
    this.reset(t);
    if (this.action) {
      const a = this.action, tau = t - a.t0;
      a.pose(this, tau);
      if (!a.hit && a.impact != null && tau >= a.impact) { a.hit = true; a.onImpact && a.onImpact(); }
      if (tau >= a.dur) { this.action = null; a.resolve(); }
    }
    if (this.fallen) {  // knocked out: fall over sideways and sink
      const k = ease(t - this.fallen, 0, 0.8);
      this.root.rotation.z = (this.side === "me" ? 1 : -1) * k * 1.3; this.root.position.y = this.hover * (1 - k) - k * 0.1;
    }
    if (this.shadow) this.shadow.position.x = this.root.position.x;
    if (this.flash > 0) {  // flash red when hit
      this.flash = Math.max(0, this.flash - dt * 2.5);
      for (const { m, e0 } of this.mats) m.emissive.copy(e0).lerp(new THREE.Color(0xff2a2a), this.flash * 0.8);
    }
  }

  play(action, t) {
    return new Promise(resolve => { this.action = { ...action, t0: t, resolve, hit: false }; });
  }
}

async function imageCard(url, yaw) {  // no 3D model yet: use the picture as a card facing the camera
  const tex = await new THREE.TextureLoader().loadAsync(url);
  tex.colorSpace = THREE.SRGBColorSpace;
  const m = new THREE.Mesh(new THREE.PlaneGeometry(1, 1), new THREE.MeshBasicMaterial({ map: tex, transparent: true, side: THREE.DoubleSide }));
  m.rotation.y = yaw;
  const g = new THREE.Group(); g.add(m); m.position.y = 0.5;
  return g;
}

// ---------- Actions ----------

// Breath / beam: wind up backward → lean in → emit particles from the source point (beams add a light column)
function breathAction(arena, atk, def, e, emitter) {
  const fx = fxOf(e.element), beam = e.template === "beam";
  return {
    dur: 1.9, impact: 0.95,
    pose(p, tau) {
      const wind = pulse(tau, 0, 0.32, 0.32, 0.5), thrust = pulse(tau, 0.32, 0.5, 1.45, 1.85);
      p.lean(-0.6 * wind + 1.0 * thrust);
      p.root.position.addScaledVector(p.forward(), 0.12 * thrust - 0.05 * wind);
      if (tau > 0.45 && tau < 1.45) {
        const from = p.world(emitter()), to = def.world(def.chest);
        if (beam) arena.beam(from, to, fx, tau);
        else arena.stream(from, to, fx);
      } else if (tau >= 1.45) arena.beamOff();
    },
  };
}

// Melee: charge in (with horns, lower the head and butt; with claws, swing an arm)
function lungeAction(arena, atk, def, e) {
  const fx = fxOf(e.element);
  return {
    dur: 1.15, impact: 0.42,
    pose(p, tau) {
      const go = pulse(tau, 0.05, 0.38, 0.6, 1.0);
      const dist = Math.abs(def.homeX - p.homeX) - 0.75;
      p.root.position.x += Math.sign(def.homeX - p.homeX) * dist * go;
      p.root.position.y += Math.sin(clamp01((tau - 0.05) / 0.33) * Math.PI) * 0.12;
      p.lean((e.source === "horns" ? 1.2 : 0.7) * pulse(tau, 0.25, 0.42, 0.55, 0.85));
      if (p.bones && e.source === "claws") for (const arm of p.arms) p.rot(arm[0], -1.4 * pulse(tau, 0.2, 0.42, 0.5, 0.8));
      if (tau > 0.4 && tau < 0.46 && e.element !== "normal") arena.burst(def.world(def.chest), fx, 50);
    },
  };
}

// Tail: spin in place and whip it across
function tailAction(arena, atk, def, e) {
  const fx = fxOf(e.element);
  return {
    dur: 1.2, impact: 0.62,
    pose(p, tau) {
      const k = ease(tau, 0.12, 0.85);
      p.root.rotation.y = p.facing + k * Math.PI * 2 * (p.side === "me" ? 1 : -1);
      p.root.position.x += Math.sign(def.homeX - p.homeX) * 0.35 * pulse(tau, 0.1, 0.5, 0.7, 1.1);
      p.root.position.y += Math.sin(clamp01((tau - 0.12) / 0.73) * Math.PI) * 0.1;
      if (p.bones) p.tail.forEach((j, i) => p.rot(j, 0, Math.sin(tau * 14 - i) * 0.35 * pulse(tau, 0.1, 0.3, 0.8, 1.1)));
      if (tau > 0.6 && tau < 0.66) arena.burst(def.world(def.chest), fx, e.element === "normal" ? 25 : 50);
    },
  };
}

function shieldAction(arena, atk, def, e) {
  const fx = fxOf(e.element);
  return {
    dur: 1.4, impact: 0.5,
    pose(p, tau) {
      p.lean(-0.5 * pulse(tau, 0, 0.3, 1.0, 1.4));
      if (p.bones) for (const arm of p.arms) p.rot(arm[0], -0.9 * pulse(tau, 0, 0.3, 1.0, 1.4));
      arena.bubble(p, fx, pulse(tau, 0.25, 0.55, 1.05, 1.4));
    },
  };
}

function healAction(arena, atk, def, e) {
  return {
    dur: 1.5, impact: 0.8,
    pose(p, tau) {
      p.root.position.y += Math.abs(Math.sin(tau * Math.PI * 2 / 0.75)) * 0.1 * (1 - ease(tau, 1.2, 1.5));
      if (tau < 1.2 && Math.random() < 0.8) {
        const c = p.world(p.chest);
        arena.spawn(c.add(new V((Math.random() - 0.5) * 0.7, -0.3 + Math.random() * 0.4, (Math.random() - 0.5) * 0.7)),
          new V(0, 0.9 + Math.random() * 0.5, 0), [0xe6ffe0, 0x7be36a, 0x2fbf4a][Math.random() * 3 | 0], 0.12, 1.0, 0, 0, true);
      }
    },
  };
}

function buffAction(arena, atk, def, e) {
  const fx = fxOf(e.element);
  return {
    dur: 1.4, impact: 0.8,
    pose(p, tau) {
      const crouch = pulse(tau, 0, 0.3, 0.3, 0.45), jump = Math.sin(clamp01((tau - 0.4) / 0.5) * Math.PI);
      p.body.scale.y = p.s * (1 - 0.12 * crouch); p.root.position.y += jump * 0.3;
      p.lean(-0.4 * jump);
      if (tau > 0.3 && tau < 1.2) {
        const c = p.world(p.root), a = tau * 12 + Math.random();
        arena.spawn(c.add(new V(Math.cos(a) * 0.55, 0.05, Math.sin(a) * 0.55)), new V(0, 1.4, 0),
          fx.colors[Math.random() * 3 | 0], fx.size, 0.8, 0, 0, fx.glow);
      }
    },
  };
}

function failAction(arena) {  // can't fly / out of heals: hop, slump, puff of gray smoke
  return {
    dur: 1.0, impact: null,
    pose(p, tau) {
      p.root.position.y += Math.sin(clamp01(tau / 0.4) * Math.PI) * 0.15;
      p.lean(0.6 * pulse(tau, 0.4, 0.6, 0.8, 1.0));
      if (tau > 0.4 && tau < 0.5) {
        const c = p.world(p.top);
        for (let i = 0; i < 6; i++) arena.spawn(c.clone(), new V((Math.random() - 0.5) * 0.5, 0.5, (Math.random() - 0.5) * 0.5), 0x9a9a9a, 0.14, 0.9, -0.2, 0, false);
      }
    },
  };
}

function hitReaction(def, atk) {
  return {
    dur: 0.6, impact: null,
    pose(p, tau) {
      const k = pulse(tau, 0, 0.08, 0.2, 0.6);
      p.root.position.addScaledVector(p.forward(), -0.2 * k);
      p.root.position.x += Math.sin(tau * 60) * 0.025 * (1 - clamp01(tau / 0.4));
      p.lean(-0.7 * k);
    },
  };
}

function dodgeReaction() {
  return {
    dur: 0.6, impact: null,
    pose(p, tau) {
      const k = Math.sin(clamp01(tau / 0.5) * Math.PI);
      p.root.position.z -= 0.45 * k; p.root.position.y += 0.15 * k;
    },
  };
}

// ---------- Scene ----------

// manual: no animation loop of its own; the caller steps frames with arena.tick(seconds) (for recording demo videos)
export async function createArena(container, { manual = false } = {}) {
  const W = () => container.clientWidth, H = () => container.clientHeight;
  // Transparent canvas: the backdrop is an image underneath (index.html picks it by the opponent's element, see static/backdrops/); the 3D scene only draws monsters, shadows and effects
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setClearColor(0x000000, 0);
  renderer.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));
  renderer.setSize(W(), H());
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  container.innerHTML = ""; container.appendChild(renderer.domElement);

  const scene = new THREE.Scene();
  const pmrem = new THREE.PMREMGenerator(renderer);
  scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
  scene.add(new THREE.HemisphereLight(0xdfe8ff, 0x2a2333, 1.2));
  const sun = new THREE.DirectionalLight(0xffffff, 1.6); sun.position.set(2, 4, 3); scene.add(sun);

  const shadowTex = (() => { const c = document.createElement("canvas"); c.width = c.height = 64; const g = c.getContext("2d");
    const r = g.createRadialGradient(32, 32, 0, 32, 32, 32); r.addColorStop(0, "rgba(0,0,0,.55)"); r.addColorStop(1, "rgba(0,0,0,0)");
    g.fillStyle = r; g.fillRect(0, 0, 64, 64); return new THREE.CanvasTexture(c); })();

  const camera = new THREE.PerspectiveCamera(34, W() / H(), 0.05, 50);
  const fit = () => {  // camera distance comes from the view size: the width must fit both monsters (x about ±2.25; a long-tailed turtle side-on is very wide), the height about 1.6
    camera.aspect = W() / H(); camera.updateProjectionMatrix();
    const tan = Math.tan(THREE.MathUtils.degToRad(camera.fov / 2));
    const d = Math.max(2.25 / (tan * camera.aspect), 0.8 / tan);
    camera.position.set(0, 0.25 + d * 0.2, d); camera.lookAt(0, 0.5, 0);
  };
  fit();

  const glow = new Particles(scene, true), solid = new Particles(scene, false);
  let beamMesh = null, bubbles = new Map();
  const puppets = {};
  let t = 0, last = performance.now(), alive = true, loadedKey = "";

  const arena = {
    scene,
    spawn(p, v, color, size, life, grav = 0, jit = 0, isGlow = true) { (isGlow ? glow : solid).spawn(p, v, color, size, life, grav, jit); },
    stream(from, to, fx) {  // a stream of particles shooting forward
      const dir = to.clone().sub(from).normalize(), dist = from.distanceTo(to);
      for (let i = 0; i < 7; i++) {
        const v = dir.clone().multiplyScalar(fx.speed * (0.8 + Math.random() * 0.4))
          .add(new V((Math.random() - 0.5), (Math.random() - 0.5), (Math.random() - 0.5)).multiplyScalar(fx.speed * fx.spread * 2));
        if (fx.swirl) v.add(new V(0, Math.sin(t * fx.swirl + i), Math.cos(t * fx.swirl + i)).multiplyScalar(0.6));
        this.spawn(from.clone().addScaledVector(v, Math.random() * 0.02), v, fx.colors[Math.random() * 3 | 0],
          fx.size * (0.7 + Math.random() * 0.6), dist / fx.speed * 1.15, fx.gravity, fx.jitter || 0, fx.glow);
      }
    },
    beam(from, to, fx, tau) {
      if (!beamMesh) {
        beamMesh = new THREE.Mesh(new THREE.CylinderGeometry(1, 1, 1, 16, 1, true),
          new THREE.MeshBasicMaterial({ color: fx.colors[1], transparent: true, opacity: 0.9, depthWrite: false, toneMapped: false }));
        scene.add(beamMesh);
      }
      const mid = from.clone().add(to).multiplyScalar(0.5), len = from.distanceTo(to);
      beamMesh.position.copy(mid);
      beamMesh.quaternion.setFromUnitVectors(new V(0, 1, 0), to.clone().sub(from).normalize());
      const w = 0.05 + 0.025 * Math.sin(tau * 40);
      beamMesh.scale.set(w, len, w);
      for (let i = 0; i < 3; i++) this.spawn(to.clone(), new V((Math.random() - 0.5) * 2, Math.random() * 1.5, (Math.random() - 0.5) * 2),
        fx.colors[Math.random() * 3 | 0], fx.size, 0.5, fx.gravity, 0, fx.glow);
      this.spawn(from.clone(), new V(0, 0, 0), fx.colors[0], fx.size * 2.2, 0.12, 0, 0, true);
    },
    beamOff() { if (beamMesh) { scene.remove(beamMesh); beamMesh.geometry.dispose(); beamMesh.material.dispose(); beamMesh = null; } },
    burst(at, fx, n) {
      for (let i = 0; i < n; i++) {
        const v = new V(Math.random() - 0.5, Math.random() - 0.3, Math.random() - 0.5).normalize().multiplyScalar(1 + Math.random() * 2);
        this.spawn(at.clone(), v, fx.colors[Math.random() * 3 | 0], fx.size, 0.5 + Math.random() * 0.3, fx.gravity, 0, fx.glow);
      }
    },
    bubble(p, fx, k) {
      let b = bubbles.get(p);
      if (!b) {
        b = new THREE.Mesh(new THREE.SphereGeometry(1, 32, 16), new THREE.MeshBasicMaterial({ color: fx.colors[1], transparent: true,
          opacity: 0.3, depthWrite: false, side: THREE.DoubleSide, toneMapped: false }));
        scene.add(b); bubbles.set(p, b);
      }
      b.position.copy(p.world(p.chest)); b.scale.setScalar(Math.max(0.001, k * (p.height * 0.6 + 0.15)));
      b.material.opacity = 0.2 * k;
      if (k <= 0.001) { scene.remove(b); b.geometry.dispose(); b.material.dispose(); bubbles.delete(p); }
    },

    async load(spec) {
      const key = JSON.stringify(["me", "foe"].map(s => [spec[s].glb, spec[s].rig, spec[s].image, spec[s].can_fly]));
      for (const p of Object.values(puppets)) { p.fallen = 0; p.flash = 0; p.action = null; }  // same pair fights again: whoever fell last round stands back up
      if (key === loadedKey) return;
      for (const s of ["me", "foe"]) if (puppets[s]) { scene.remove(puppets[s].root, puppets[s].shadow); delete puppets[s]; }
      const ps = await Promise.all(["me", "foe"].map(s => new Puppet(arena, s, spec[s]).load()));
      for (const p of ps) {
        puppets[p.side] = p;
        const sh = new THREE.Mesh(new THREE.PlaneGeometry(1, 1), new THREE.MeshBasicMaterial({ map: shadowTex, transparent: true, depthWrite: false }));
        sh.rotation.x = -Math.PI / 2; sh.position.set(p.homeX, 0.004, 0); sh.scale.setScalar(1.1); scene.add(sh); p.shadow = sh;
      }
      loadedKey = key;
    },

    // Play one battle log entry; onImpact (HP down or up) is called at the moment of the hit. The returned Promise resolves when both sides' actions are done
    async play(e, onImpact) {
      const atk = puppets[e.side], def = puppets[e.side === "me" ? "foe" : "me"];
      if (!atk || !def) { onImpact && onImpact(); return; }
      let reaction = null;
      const impact = () => {
        onImpact && onImpact();
        if (e.dmg) { def.flash = 1; reaction = def.play(hitReaction(def, atk), t); }
        else if (e.miss) reaction = def.play(dodgeReaction(), t);
      };
      let action;
      if (e.fail) action = failAction(arena);
      else if (e.template === "projectile" || e.template === "beam") {
        const src = e.source || "mouth";
        const pick = { tail: atk.tailTip, claws: atk.clawTip, horns: atk.hornTip, back: atk.top }[src] || atk.mouth;
        action = breathAction(arena, atk, def, e, () => pick || atk.mouth);
      } else if (e.template === "strike") action = e.source === "tail" || (!e.source && /尾|tail/i.test(e.skill || ""))  // old moves have no source, so guess from the name
        ? tailAction(arena, atk, def, e) : lungeAction(arena, atk, def, e);
      else if (e.template === "shield") action = shieldAction(arena, atk, def, e);
      else if (e.template === "heal") action = healAction(arena, atk, def, e);
      else action = buffAction(arena, atk, def, e);
      action.onImpact = impact;
      await atk.play(action, t);
      arena.beamOff();
      if (action.impact == null) impact();
      if (reaction) await reaction;
    },
    faint(side) { const p = puppets[side]; if (p && !p.fallen) p.fallen = t; },
    dispose() { alive = false; renderer.dispose(); container.innerHTML = ""; },
  };

  const ro = new ResizeObserver(() => { renderer.setSize(W(), H()); fit(); });
  ro.observe(container);
  arena.tick = dt => {
    t += dt;
    for (const p of Object.values(puppets)) p.update(t, dt);
    scene.updateMatrixWorld(true);
    glow.mat.uniforms.scale.value = solid.mat.uniforms.scale.value = renderer.domElement.height / (2 * Math.tan(THREE.MathUtils.degToRad(camera.fov / 2)));
    glow.update(dt); solid.update(dt);
    renderer.render(scene, camera);
  };
  if (!manual) (function frame(now) {
    if (!alive) { ro.disconnect(); return; }
    requestAnimationFrame(frame);
    const dt = Math.min(0.05, (now - last) / 1000); last = now;
    if (container.offsetParent) arena.tick(dt);  // don't draw while the battle card is collapsed
  })(last);
  return arena;
}
