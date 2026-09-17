/* BudzBook figurine viewer.
 *
 * Finds every `.figurine[data-png][data-json]` on the page and renders a
 * spinnable 3D figurine with three.js: the silhouette polygon from the JSON
 * is extruded, and the transparent cutout PNG is textured onto the
 * front/back caps. Slow auto-spin, drag to rotate. Honors
 * prefers-reduced-motion (static 3/4 view). One shared rAF loop drives all
 * instances; offscreen canvases pause via IntersectionObserver. Any failure
 * (no WebGL, bad JSON/PNG) degrades to the flat <img> fallback already in
 * the element.
 */
import * as THREE from "three";

const instances = [];
let rafStarted = false;
const reduceMotion =
  window.matchMedia &&
  window.matchMedia("(prefers-reduced-motion: reduce)").matches;

function failSoft(el) {
  // Leave the fallback <img> in place; just mark the failure.
  el.classList.add("figurine-failed");
  const img = el.querySelector("img");
  if (img) img.style.display = "";
}

function sizeRenderer(inst) {
  const el = inst.el;
  const w = Math.max(1, el.clientWidth);
  const h = Math.max(1, el.clientHeight);
  inst.renderer.setSize(w, h, false);
  inst.camera.aspect = w / h;
  inst.camera.updateProjectionMatrix();
}

function buildFigure(el, data, tex) {
  const size = data.size || [512, 512];
  const W = size[0], H = size[1];
  const pts = data.points || [];
  if (pts.length < 8) { failSoft(el); return; }

  // Shape space: unit square, y-up. Default ExtrudeGeometry UVs equal the
  // vertex x/y, so the texture maps 1:1 onto the caps.
  const shape = new THREE.Shape();
  pts.forEach(([x, y], i) => {
    const sx = x / W, sy = 1 - y / H;
    if (i === 0) shape.moveTo(sx, sy); else shape.lineTo(sx, sy);
  });
  shape.closePath();

  const depth = 0.14;
  const geo = new THREE.ExtrudeGeometry(shape, {
    depth,
    bevelEnabled: true,
    bevelThickness: 0.025,
    bevelSize: 0.02,
    bevelSegments: 2,
    curveSegments: 4,
  });
  geo.center();

  tex.colorSpace = THREE.SRGBColorSpace;
  tex.anisotropy = 4;
  const capMat = new THREE.MeshStandardMaterial({
    map: tex, transparent: true, roughness: 0.55, metalness: 0.05,
  });
  const sideMat = new THREE.MeshStandardMaterial({
    color: 0x14301c, roughness: 0.85, metalness: 0.0,
  });
  const mesh = new THREE.Mesh(geo, [capMat, sideMat]);

  const scene = new THREE.Scene();
  scene.add(new THREE.HemisphereLight(0xffffff, 0x2a3d2a, 1.15));
  const key = new THREE.DirectionalLight(0xffffff, 1.1);
  key.position.set(2.5, 4, 3.5);
  scene.add(key);
  const rim = new THREE.DirectionalLight(0x9dffb0, 0.5);
  rim.position.set(-3, 2, -2.5);
  scene.add(rim);
  scene.add(mesh);

  const camera = new THREE.PerspectiveCamera(32, 1, 0.1, 50);
  camera.position.set(0, 0.05, 2.35);
  camera.lookAt(0, 0, 0);

  const inst = {
    el, renderer: el._figRenderer, scene, camera, mesh,
    rotY: reduceMotion ? 0.6 : Math.random() * Math.PI * 2,
    dragging: false, lastX: 0, visible: true,
  };
  // Static 3/4 view for reduced motion.
  if (reduceMotion) inst.rotY = 0.6;

  const cv = inst.renderer.domElement;
  cv.style.touchAction = "pan-y";
  cv.addEventListener("pointerdown", (e) => {
    inst.dragging = true; inst.lastX = e.clientX;
    cv.setPointerCapture(e.pointerId);
  });
  cv.addEventListener("pointermove", (e) => {
    if (!inst.dragging) return;
    inst.rotY += (e.clientX - inst.lastX) * 0.012;
    inst.lastX = e.clientX;
  });
  const endDrag = () => { inst.dragging = false; };
  cv.addEventListener("pointerup", endDrag);
  cv.addEventListener("pointercancel", endDrag);

  instances.push(inst);
  sizeRenderer(inst);

  // Hide the fallback img now that WebGL is rendering.
  const img = el.querySelector("img");
  if (img) img.style.display = "none";

  new IntersectionObserver((entries) => {
    entries.forEach((en) => { inst.visible = en.isIntersecting; });
  }).observe(el);

  if (!rafStarted) { rafStarted = true; requestAnimationFrame(tick); }
}

let lastT = 0;
function tick(t) {
  requestAnimationFrame(tick);
  const dt = Math.min(0.05, (t - lastT) / 1000 || 0.016);
  lastT = t;
  for (const inst of instances) {
    if (!inst.visible) continue;
    if (!reduceMotion && !inst.dragging) inst.rotY += dt * 0.55;
    inst.mesh.rotation.y = inst.rotY;
    inst.renderer.render(inst.scene, inst.camera);
  }
}

function makeViewer(el) {
  const png = el.getAttribute("data-png");
  const jsonUrl = el.getAttribute("data-json");
  if (!png || !jsonUrl) return;

  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  } catch (e) {
    failSoft(el);
    return;
  }
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  el._figRenderer = renderer;
  el.appendChild(renderer.domElement);

  fetch(jsonUrl, { cache: "no-store" })
    .then((r) => { if (!r.ok) throw new Error("json " + r.status); return r.json(); })
    .then((data) => {
      new THREE.TextureLoader().load(
        png,
        (tex) => buildFigure(el, data, tex),
        undefined,
        () => failSoft(el)
      );
    })
    .catch(() => failSoft(el));
}

document.querySelectorAll(".figurine[data-png][data-json]").forEach(makeViewer);
