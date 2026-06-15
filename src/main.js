import * as THREE from "three";
import {
  BLOCK,
  HOTBAR,
  BLOCK_DEFS,
  buildAtlas,
  drawBlockIcon,
  isSolid,
} from "./blocks.js";
import {
  World,
  WORLD_SIZE,
  WORLD_HEIGHT,
  SEA_LEVEL,
} from "./world.js";
import { Player } from "./player.js";
import { Controls } from "./controls.js";

const canvas = document.getElementById("game");
const overlay = document.getElementById("overlay");
const loadingEl = document.getElementById("loading");
const playBtn = document.getElementById("playBtn");
const hud = document.getElementById("hud");
const infoEl = document.getElementById("info");
const hotbarEl = document.getElementById("hotbar");

// ---------------------------------------------------------------------------
// Renderer / scene
// ---------------------------------------------------------------------------
const renderer = new THREE.WebGLRenderer({
  canvas,
  antialias: false,
  powerPreference: "high-performance",
});
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.setClearColor(0x7ec0ee);

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x7ec0ee);
scene.fog = new THREE.Fog(0x9fcdef, 40, WORLD_SIZE * 0.9);

const camera = new THREE.PerspectiveCamera(
  72,
  window.innerWidth / window.innerHeight,
  0.05,
  1000
);

// Lights: hemisphere + directional for a soft, daylight look.
scene.add(new THREE.HemisphereLight(0xffffff, 0x6688aa, 0.9));
const sun = new THREE.DirectionalLight(0xfff4e0, 0.8);
sun.position.set(0.5, 1, 0.3);
scene.add(sun);

// ---------------------------------------------------------------------------
// Build world + materials
// ---------------------------------------------------------------------------
const { texture, uv } = buildAtlas();

const opaqueMat = new THREE.MeshLambertMaterial({
  map: texture,
  vertexColors: true,
});
const transparentMat = new THREE.MeshLambertMaterial({
  map: texture,
  vertexColors: true,
  transparent: true,
  alphaTest: 0.1,
  opacity: 0.85,
  side: THREE.DoubleSide,
  depthWrite: false,
});

const world = new World(1337 + Math.floor(Math.random() * 1000));
world.setUVs(uv);

let player;
let controls;
let activeSlot = 0;
let lastTime = performance.now();
let frames = 0;
let fps = 0;
let fpsTimer = 0;

function init() {
  loadingEl.textContent = "Carving terrain…";
  world.generate();
  world.material = { opaque: opaqueMat, transparent: transparentMat };
  world.buildAllChunks(world.material);
  scene.add(world.group);

  // spawn at world centre, on the surface
  const sx = Math.floor(WORLD_SIZE / 2);
  const sz = Math.floor(WORLD_SIZE / 2);
  let sy = WORLD_HEIGHT - 1;
  while (sy > 0 && !isSolid(world.getBlock(sx, sy, sz))) sy--;
  player = new Player(world, { x: sx + 0.5, y: sy + 1, z: sz + 0.5 });
  player.yaw = Math.PI / 4;

  setupHotbar();
  setupControls();

  loadingEl.textContent = "Ready!";
  playBtn.disabled = false;
}

// ---------------------------------------------------------------------------
// Hotbar UI
// ---------------------------------------------------------------------------
function setupHotbar() {
  hotbarEl.innerHTML = "";
  HOTBAR.forEach((id, i) => {
    const slot = document.createElement("div");
    slot.className = "slot" + (i === 0 ? " active" : "");
    const c = document.createElement("canvas");
    drawBlockIcon(c, id);
    slot.appendChild(c);
    const label = document.createElement("span");
    label.className = "label";
    label.textContent = i + 1;
    slot.appendChild(label);
    slot.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      selectSlot(i);
    });
    hotbarEl.appendChild(slot);
  });
}

function selectSlot(i) {
  activeSlot = i;
  [...hotbarEl.children].forEach((s, idx) =>
    s.classList.toggle("active", idx === i)
  );
}

// ---------------------------------------------------------------------------
// Controls + block interaction
// ---------------------------------------------------------------------------
function setupControls() {
  controls = new Controls({
    canvas,
    joystick: document.getElementById("joystick"),
    knob: document.getElementById("joystickKnob"),
    jumpBtn: document.getElementById("jumpBtn"),
    placeBtn: document.getElementById("placeBtn"),
    flyBtn: document.getElementById("flyBtn"),
  });

  controls.onBreak = breakBlock;
  controls.onPlace = placeBlock;
  controls.onToggleFly = (f) => {
    player.flying = f;
  };

  // number keys select hotbar slots on desktop
  window.addEventListener("keydown", (e) => {
    const n = parseInt(e.key, 10);
    if (n >= 1 && n <= HOTBAR.length) selectSlot(n - 1);
    if (e.code === "KeyF") {
      player.flying = !player.flying;
      document
        .getElementById("flyBtn")
        .classList.toggle("toggled", player.flying);
    }
  });
  // mouse buttons for desktop play
  canvas.addEventListener("contextmenu", (e) => e.preventDefault());
  canvas.addEventListener("mousedown", (e) => {
    if (e.button === 2) placeBlock();
  });
}

function targetBlock() {
  const eye = player.getEyePosition();
  const dir = player.getForward();
  return world.raycast(eye, dir, 7);
}

function breakBlock() {
  const hit = targetBlock();
  if (!hit) return;
  world.setBlock(hit.x, hit.y, hit.z, BLOCK.AIR);
  world.markDirtyAt(hit.x, hit.z);
  world.flushDirty();
}

function placeBlock() {
  const hit = targetBlock();
  if (!hit) return;
  const x = hit.x + hit.nx;
  const y = hit.y + hit.ny;
  const z = hit.z + hit.nz;
  if (!world.inBounds(x, y, z)) return;
  if (world.getBlock(x, y, z) !== BLOCK.AIR) return;

  // don't place inside the player
  if (intersectsPlayer(x, y, z)) return;

  world.setBlock(x, y, z, HOTBAR[activeSlot]);
  world.markDirtyAt(x, z);
  world.flushDirty();
}

function intersectsPlayer(x, y, z) {
  const p = player.pos;
  const half = player.half;
  const px0 = p.x - half;
  const px1 = p.x + half;
  const pz0 = p.z - half;
  const pz1 = p.z + half;
  const py0 = p.y;
  const py1 = p.y + 1.8;
  return (
    x + 1 > px0 &&
    x < px1 &&
    z + 1 > pz0 &&
    z < pz1 &&
    y + 1 > py0 &&
    y < py1
  );
}

// ---------------------------------------------------------------------------
// Loop
// ---------------------------------------------------------------------------
function frame() {
  requestAnimationFrame(frame);
  const now = performance.now();
  let dt = (now - lastTime) / 1000;
  lastTime = now;
  if (dt > 0.1) dt = 0.1;

  controls.applyKeyboard();
  const look = controls.consumeLook();
  const input = {
    moveX: controls.state.moveX,
    moveY: controls.state.moveY,
    lookX: look.lx,
    lookY: look.ly,
    jump: controls.state.jump,
    sneak: controls.state.sneak,
  };

  player.update(dt, input);
  player.applyToCamera(camera);

  // hud / fps
  frames++;
  fpsTimer += dt;
  if (fpsTimer >= 0.5) {
    fps = Math.round(frames / fpsTimer);
    frames = 0;
    fpsTimer = 0;
    updateInfo();
  }

  renderer.render(scene, camera);
}

function updateInfo() {
  const p = player.pos;
  const block = BLOCK_DEFS[HOTBAR[activeSlot]];
  infoEl.innerHTML =
    `Blockcraft · ${fps} fps<br>` +
    `XYZ ${p.x.toFixed(0)} ${p.y.toFixed(0)} ${p.z.toFixed(0)}<br>` +
    `Holding: ${block ? block.name : "?"}` +
    (player.flying ? " · FLYING" : "");
}

// ---------------------------------------------------------------------------
// Resize / start
// ---------------------------------------------------------------------------
function resize() {
  const w = window.innerWidth;
  const h = window.innerHeight;
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
  renderer.setSize(w, h, false);
}
window.addEventListener("resize", resize);
resize();

playBtn.addEventListener("click", () => {
  overlay.classList.add("hidden");
  hud.classList.remove("hidden");
  lastTime = performance.now();
  frame();
});

// kick off generation shortly after first paint so the loading text shows
setTimeout(init, 50);
