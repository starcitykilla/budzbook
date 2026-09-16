/* BudzBook 3D avatar viewer.
 *
 * Finds every `.avatar3d[data-url]` on the page and renders the Ready Player Me
 * GLB inside it with three.js: slow turntable, gentle bob, drag-to-spin,
 * scroll-to-zoom, click for a hop. One shared rAF loop drives all instances;
 * offscreen canvases are paused via IntersectionObserver. Any failure
 * (no WebGL, bad URL, load error) degrades to a static fallback glyph.
 */
import * as THREE from "three";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

const loader = new GLTFLoader();
const instances = [];
let rafStarted = false;

function failSoft(el) {
  el.classList.add("avatar3d-failed");
  el.innerHTML = '<span class="avatar3d-fallback">🌿</span>';
}

function makeViewer(el) {
  const url = el.getAttribute("data-url");
  if (!url) { failSoft(el); return; }

  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  } catch (e) {
    failSoft(el);
    return;
  }
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  el.appendChild(renderer.domElement);

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(32, 1, 0.1, 50);

  scene.add(new THREE.HemisphereLight(0xffffff, 0x2a3d2a, 1.15));
  const key = new THREE.DirectionalLight(0xffffff, 1.1);
  key.position.set(2.5, 4, 3.5);
  scene.add(key);
  const rim = new THREE.DirectionalLight(0x9dffb0, 0.5);
  rim.position.set(-3, 2, -2.5);
  scene.add(rim);

  const inst = {
    el, renderer, scene, camera,
    group: null, controls: null,
    visible: false, dragging: false,
    rotY: Math.random() * Math.PI * 2,
    t: Math.random() * 10,
    hopY: 0, hopV: 0,
    baseScale: 1,
    downX: 0, downY: 0,
  };

  const sizeRenderer = () => {
    const w = Math.max(1, el.clientWidth), h = Math.max(1, el.clientHeight);
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  };
  sizeRenderer();

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enablePan = false;
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  controls.rotateSpeed = 0.9;
  controls.minPolarAngle = Math.PI * 0.18;
  controls.maxPolarAngle = Math.PI * 0.62;
  controls.addEventListener("start", () => { inst.dragging = true; });
  controls.addEventListener("end", () => { inst.dragging = false; });
  inst.controls = controls;

  // Click (not drag) => hop.
  renderer.domElement.addEventListener("pointerdown", (e) => {
    inst.downX = e.clientX; inst.downY = e.clientY;
  });
  renderer.domElement.addEventListener("pointerup", (e) => {
    const dx = e.clientX - inst.downX, dy = e.clientY - inst.downY;
    if (dx * dx + dy * dy < 36 && inst.hopV === 0 && inst.hopY === 0) {
      inst.hopV = 2.4; // little hop
    }
  });

  loader.load(url, (gltf) => {
    const group = new THREE.Group();
    group.add(gltf.scene);
    // Normalize: feet on y=0, centered on x/z, ~1.7 units tall.
    const box = new THREE.Box3().setFromObject(gltf.scene);
    const size = box.getSize(new THREE.Vector3());
    const center = box.getCenter(new THREE.Vector3());
    if (size.y <= 0) { failSoft(el); return; }
    const s = 1.7 / size.y;
    inst.baseScale = s;
    gltf.scene.position.set(-center.x, -box.min.y, -center.z);
    group.scale.setScalar(s);
    scene.add(group);
    inst.group = group;
    const dist = 2.9;
    camera.position.set(0.6, 1.15, dist);
    controls.target.set(0, 0.95, 0);
    controls.minDistance = dist * 0.45;
    controls.maxDistance = dist * 1.9;
    controls.update();
  }, undefined, () => failSoft(el));

  const observer = new IntersectionObserver((entries) => {
    inst.visible = entries.some((e) => e.isIntersecting);
  }, { threshold: 0.05 });
  observer.observe(el);
  inst.visible = true;

  instances.push(inst);
  if (!rafStarted) {
    rafStarted = true;
    requestAnimationFrame(tick);
  }
}

let lastT = 0;
function tick(nowMs) {
  requestAnimationFrame(tick);
  const now = nowMs / 1000;
  const dt = Math.min(0.05, lastT ? now - lastT : 0.016);
  lastT = now;
  for (const inst of instances) {
    if (!inst.visible) continue;
    inst.t += dt;
    if (inst.group && !inst.dragging) {
      inst.rotY += dt * 0.45; // slow turntable
      inst.group.rotation.y = inst.rotY;
      // Hop physics.
      if (inst.hopV !== 0 || inst.hopY !== 0) {
        inst.hopV -= 9.5 * dt;
        inst.hopY += inst.hopV * dt;
        if (inst.hopY <= 0) { inst.hopY = 0; inst.hopV = 0; }
      }
      const bob = Math.sin(inst.t * 1.9) * 0.028;
      inst.group.position.y = inst.hopY + bob;
      const breathe = 1 + 0.009 * Math.sin(inst.t * 2.3);
      inst.group.scale.set(inst.baseScale, inst.baseScale * breathe, inst.baseScale);
    }
    if (inst.controls) inst.controls.update();
    inst.renderer.render(inst.scene, inst.camera);
  }
}

window.addEventListener("resize", () => {
  for (const inst of instances) {
    const w = Math.max(1, inst.el.clientWidth), h = Math.max(1, inst.el.clientHeight);
    inst.renderer.setSize(w, h, false);
    inst.camera.aspect = w / h;
    inst.camera.updateProjectionMatrix();
  }
});

document.querySelectorAll(".avatar3d[data-url]").forEach(makeViewer);
