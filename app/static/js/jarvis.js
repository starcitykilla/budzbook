/* Jarvis — the little 3D robot hanging out on the BudzBook landing page.
 * Procedural Three.js hover-bot: idle bob + blink, typing sessions at a
 * hologram keyboard, and a wave when you tap him. Fail-soft: if WebGL or
 * the CDN is unavailable the stage hides itself quietly.
 */
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const stage = document.getElementById('jarvis-stage');
if (!stage) throw new Error('no stage');
const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

let renderer;
try {
  renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
} catch (e) {
  stage.style.display = 'none';
  throw e;
}

const GREEN = 0x3ddc84;
const H = 300;
const width = () => stage.clientWidth || 320;

renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
renderer.setSize(width(), H);
stage.appendChild(renderer.domElement);

const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(38, width() / H, 0.1, 50);
camera.position.set(0, 1.6, 5.4);

const controls = new OrbitControls(camera, renderer.domElement);
controls.target.set(0, 1.35, 0);
controls.enableDamping = true;
controls.enablePan = false;
controls.minDistance = 3.6;
controls.maxDistance = 8;
controls.minPolarAngle = 0.85;
controls.maxPolarAngle = 1.7;
if ('ontouchstart' in window) {
  // Don't trap page scroll on phones; tap still waves.
  controls.enableRotate = false;
  controls.enableZoom = false;
}

scene.add(new THREE.HemisphereLight(0xcfffe0, 0x0d1512, 0.95));
const key = new THREE.DirectionalLight(0xffffff, 1.7);
key.position.set(3, 5, 4);
scene.add(key);
const under = new THREE.PointLight(GREEN, 10, 7);
under.position.set(0, 0.4, 1.6);
scene.add(under);

const dark = new THREE.MeshStandardMaterial({ color: 0x1b2620, metalness: 0.65, roughness: 0.35 });
const darker = new THREE.MeshStandardMaterial({ color: 0x101714, metalness: 0.5, roughness: 0.55 });
const glowMat = new THREE.MeshStandardMaterial({ color: 0x0b3d24, emissive: GREEN, emissiveIntensity: 2.4 });

const bot = new THREE.Group();
scene.add(bot);

// Body
const body = new THREE.Mesh(new THREE.CapsuleGeometry(0.5, 0.65, 8, 24), dark);
body.position.y = 1.15;
bot.add(body);
const strip = new THREE.Mesh(new THREE.CapsuleGeometry(0.055, 0.38, 4, 12), glowMat);
strip.position.set(0, 1.12, 0.47);
strip.rotation.x = 0.08;
bot.add(strip);

// Head
const headG = new THREE.Group();
headG.position.y = 2.02;
bot.add(headG);
const head = new THREE.Mesh(new THREE.SphereGeometry(0.44, 32, 24), dark);
head.scale.set(1, 0.92, 0.95);
headG.add(head);
const eyeGeo = new THREE.SphereGeometry(0.085, 16, 12);
const eyeL = new THREE.Mesh(eyeGeo, glowMat);
eyeL.position.set(-0.17, 0.06, 0.36);
headG.add(eyeL);
const eyeR = eyeL.clone();
eyeR.position.x = 0.17;
headG.add(eyeR);
const ant = new THREE.Mesh(new THREE.CylinderGeometry(0.02, 0.02, 0.34, 8), darker);
ant.position.y = 0.56;
headG.add(ant);
const antTip = new THREE.Mesh(new THREE.SphereGeometry(0.06, 12, 10), glowMat);
antTip.position.y = 0.76;
headG.add(antTip);

// Arms
function makeArm(side) {
  const g = new THREE.Group();
  g.position.set(0.56 * side, 1.48, 0);
  bot.add(g);
  const upper = new THREE.Mesh(new THREE.CapsuleGeometry(0.09, 0.28, 4, 12), dark);
  upper.position.y = -0.2;
  g.add(upper);
  const fore = new THREE.Group();
  fore.position.y = -0.42;
  g.add(fore);
  const foreM = new THREE.Mesh(new THREE.CapsuleGeometry(0.075, 0.26, 4, 12), darker);
  foreM.position.y = -0.17;
  fore.add(foreM);
  const hand = new THREE.Mesh(new THREE.SphereGeometry(0.09, 12, 10), darker);
  hand.position.y = -0.36;
  fore.add(hand);
  const tip = new THREE.Mesh(new THREE.SphereGeometry(0.032, 8, 8), glowMat);
  tip.position.set(0, -0.36, 0.07);
  fore.add(tip);
  return { g, fore };
}
const armL = makeArm(-1);
const armR = makeArm(1);

// Hover glow ring
const ring = new THREE.Mesh(
  new THREE.TorusGeometry(0.55, 0.028, 10, 48),
  new THREE.MeshBasicMaterial({ color: GREEN, transparent: true, opacity: 0.45 })
);
ring.rotation.x = Math.PI / 2;
ring.position.y = 0.34;
bot.add(ring);
const disc = new THREE.Mesh(
  new THREE.CircleGeometry(0.5, 32),
  new THREE.MeshBasicMaterial({ color: GREEN, transparent: true, opacity: 0.08 })
);
disc.rotation.x = -Math.PI / 2;
disc.position.y = 0.33;
bot.add(disc);

// Hologram keyboard (fades in while typing)
const kb = new THREE.Group();
kb.position.set(0, 0.92, 0.9);
kb.rotation.x = -0.45;
bot.add(kb);
const kbPlane = new THREE.Mesh(
  new THREE.PlaneGeometry(1.15, 0.52),
  new THREE.MeshBasicMaterial({ color: GREEN, transparent: true, opacity: 0, side: THREE.DoubleSide, depthWrite: false })
);
kb.add(kbPlane);
const keyDots = [];
const dotGeo = new THREE.PlaneGeometry(0.055, 0.055);
for (let r = 0; r < 3; r++) {
  for (let c = 0; c < 8; c++) {
    const d = new THREE.Mesh(dotGeo, new THREE.MeshBasicMaterial({ color: GREEN, transparent: true, opacity: 0, depthWrite: false }));
    d.position.set(-0.42 + c * 0.12, 0.14 - r * 0.13, 0.006);
    kb.add(d);
    keyDots.push(d);
  }
}

// Typing particles
const P_COUNT = 70;
const pGeo = new THREE.BufferGeometry();
const pPos = new Float32Array(P_COUNT * 3);
const pLife = new Float32Array(P_COUNT); // 0 = dead
pGeo.setAttribute('position', new THREE.BufferAttribute(pPos, 3));
const pMat = new THREE.PointsMaterial({ color: GREEN, size: 0.045, transparent: true, opacity: 0.9, depthWrite: false });
const points = new THREE.Points(pGeo, pMat);
points.frustumCulled = false;
scene.add(points);
let pCursor = 0;
function spawnParticle() {
  const i = pCursor;
  pCursor = (pCursor + 1) % P_COUNT;
  pPos[i * 3] = (Math.random() - 0.5) * 1.0;
  pPos[i * 3 + 1] = 0.95 + Math.random() * 0.15;
  pPos[i * 3 + 2] = 0.9 + (Math.random() - 0.5) * 0.3;
  pLife[i] = 1;
}

// Animation state machine: idle -> typing -> idle ...; wave on tap
let mode = 'idle';
let modeT = 0;
let nextTyping = 4; // seconds until first typing burst
let blinkAt = 2.5;
let waveT = -1;

const ray = new THREE.Raycaster();
const ptr = new THREE.Vector2();
renderer.domElement.addEventListener('pointerdown', (e) => {
  const r = renderer.domElement.getBoundingClientRect();
  ptr.x = ((e.clientX - r.left) / r.width) * 2 - 1;
  ptr.y = -((e.clientY - r.top) / r.height) * 2 + 1;
  ray.setFromCamera(ptr, camera);
  if (ray.intersectObject(bot, true).length && waveT < 0) waveT = 0;
});

function setTypingPose(k, t) {
  // k: 0..1 blend into typing pose
  const fwd = -1.05 * k;
  armL.g.rotation.x += (fwd - armL.g.rotation.x) * 0.15;
  armR.g.rotation.x += (fwd - armR.g.rotation.x) * 0.15;
  const alt = Math.sin(t * 16);
  armL.fore.rotation.x = -0.55 * k + alt * 0.14 * k;
  armR.fore.rotation.x = -0.55 * k - alt * 0.14 * k;
  headG.rotation.x += ((0.3 * k) - headG.rotation.x) * 0.12;
  kbPlane.material.opacity += ((0.22 * k) - kbPlane.material.opacity) * 0.15;
  for (const d of keyDots) d.material.opacity += ((0.55 * k) - d.material.opacity) * 0.15;
}

const clock = new THREE.Clock();
let running = true;
new IntersectionObserver((es) => { running = es[0].isIntersecting; }).observe(stage);

function ease(x) { return x < 0 ? 0 : x > 1 ? 1 : x * x * (3 - 2 * x); }

function tick() {
  requestAnimationFrame(tick);
  if (!running) return;
  const dt = Math.min(clock.getDelta(), 0.05);
  const t = clock.elapsedTime;
  controls.update();

  // Base hover motion (always on)
  bot.position.y = reducedMotion ? 0 : Math.sin(t * 1.6) * 0.06;
  bot.rotation.y = reducedMotion ? 0 : Math.sin(t * 0.5) * 0.14;
  ring.material.opacity = 0.35 + Math.sin(t * 2.2) * 0.12;
  antTip.scale.setScalar(1 + Math.sin(t * 4) * 0.18);

  // Blink
  if (t > blinkAt) {
    blinkAt = t + 2 + Math.random() * 3;
    eyeL.scale.y = eyeR.scale.y = 0.12;
    setTimeout(() => { eyeL.scale.y = eyeR.scale.y = 1; }, 130);
  }

  // Wave takes over everything briefly
  if (waveT >= 0) {
    waveT += dt;
    const k = waveT / 1.8;
    if (k >= 1) { waveT = -1; }
    else {
      armR.g.rotation.z = -2.3 * ease(Math.min(k * 3, 1));
      armR.fore.rotation.z = Math.sin(t * 11) * 0.45 * ease(Math.min(k * 3, 1));
      bot.position.y += Math.abs(Math.sin(k * Math.PI * 2)) * 0.16;
      if (Math.random() < 0.35) spawnParticle();
    }
  } else if (!reducedMotion) {
    // Idle <-> typing cycle
    modeT += dt;
    if (mode === 'idle') {
      setTypingPose(0, t);
      armR.g.rotation.z *= 0.9; // relax wave arm
      if (modeT > nextTyping) { mode = 'typing'; modeT = 0; }
    } else {
      setTypingPose(1, t);
      if (Math.random() < 0.5) spawnParticle();
      if (modeT > 5) { mode = 'idle'; modeT = 0; nextTyping = 5 + Math.random() * 6; }
    }
  }

  // Particles
  let dirty = false;
  for (let i = 0; i < P_COUNT; i++) {
    if (pLife[i] > 0) {
      pLife[i] -= dt * 1.4;
      pPos[i * 3 + 1] += dt * 0.9;
      if (pLife[i] <= 0) pPos[i * 3 + 1] = -10;
      dirty = true;
    }
  }
  if (dirty) pGeo.attributes.position.needsUpdate = true;

  renderer.render(scene, camera);
}
tick();

window.addEventListener('resize', () => {
  camera.aspect = width() / H;
  camera.updateProjectionMatrix();
  renderer.setSize(width(), H);
});
