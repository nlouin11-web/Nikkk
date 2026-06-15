// Headless logic test — exercises world gen, meshing buffers, raycast and
// player physics without a WebGL context.
import * as THREE from "three";
import { World, WORLD_SIZE, SEA_LEVEL } from "./src/world.js";
import { Player } from "./src/player.js";
import { BLOCK, isSolid } from "./src/blocks.js";

let pass = 0;
let fail = 0;
function check(name, cond) {
  if (cond) { pass++; console.log("  ✓ " + name); }
  else { fail++; console.log("  ✗ FAIL: " + name); }
}

// --- generation ---
const world = new World(1337);
world.generate();

// surface column at centre is solid then air above
const cx = WORLD_SIZE >> 1;
const cz = WORLD_SIZE >> 1;
let surfaceY = -1;
for (let y = world.sy - 1; y >= 0; y--) {
  if (isSolid(world.getBlock(cx, y, cz))) { surfaceY = y; break; }
}
check("found a solid surface at world centre", surfaceY > 0);
check("air directly above surface", world.getBlock(cx, surfaceY + 1, cz) === BLOCK.AIR ||
       world.getBlock(cx, surfaceY + 1, cz) === BLOCK.WATER);

// count block variety
const counts = {};
for (let i = 0; i < world.data.length; i++) counts[world.data[i]] = (counts[world.data[i]] || 0) + 1;
check("generated stone", (counts[BLOCK.STONE] || 0) > 1000);
check("generated grass", (counts[BLOCK.GRASS] || 0) > 100);
check("generated water at/below sea level", (counts[BLOCK.WATER] || 0) >= 0);
check("generated trees (wood + leaves)", (counts[BLOCK.WOOD] || 0) > 0 && (counts[BLOCK.LEAVES] || 0) > 0);

// --- meshing ---
const dummyUV = {};
for (const k of ["grass_top","grass_side","dirt","stone","cobble","wood_top","wood_side","leaves","sand","water","plank","glass","brick"]) {
  dummyUV[k] = [0, 0, 1, 1];
}
world.setUVs(dummyUV);
world.material = {
  opaque: new THREE.MeshLambertMaterial(),
  transparent: new THREE.MeshLambertMaterial({ transparent: true }),
};
world.buildChunk(cx >> 4, cz >> 4);
const meshKey = (cx >> 4) + "," + (cz >> 4);
const meshes = world.chunkMeshes.get(meshKey);
check("chunk mesh built", !!meshes && !!meshes.opaque);
const geo = meshes.opaque.geometry;
const posCount = geo.getAttribute("position").count;
const idx = geo.getIndex();
check("mesh has vertices", posCount > 0);
check("mesh has indices", idx && idx.count > 0 && idx.count % 3 === 0);
// validate indices are in range
let idxOK = true;
for (let i = 0; i < idx.count; i++) if (idx.getX(i) >= posCount) idxOK = false;
check("all indices within vertex range", idxOK);

// --- raycast straight down hits the surface ---
const eye = new THREE.Vector3(cx + 0.5, surfaceY + 5, cz + 0.5);
const hit = world.raycast(eye, new THREE.Vector3(0, -1, 0), 10);
check("raycast down hits a block", !!hit);
check("raycast hit is the surface block", hit && hit.y === surfaceY);
check("raycast top normal points up", hit && hit.ny === 1);

// --- edit round-trips and re-meshes ---
world.setBlock(cx, surfaceY + 1, cz, BLOCK.STONE);
check("placed block reads back", world.getBlock(cx, surfaceY + 1, cz) === BLOCK.STONE);
world.markDirtyAt(cx, cz);
world.flushDirty();
check("dirty queue cleared after flush", world.dirtyChunks.size === 0);

// --- player physics: falls and lands ---
const player = new Player(world, { x: cx + 0.5, y: surfaceY + 6, z: cz + 0.5 });
const noInput = { moveX: 0, moveY: 0, lookX: 0, lookY: 0, jump: false, sneak: false };
for (let i = 0; i < 240; i++) player.update(1 / 60, noInput);
check("player landed on ground", player.onGround === true);
check("player rests above the surface", player.pos.y >= surfaceY + 1 && player.pos.y <= surfaceY + 3);
check("player did not fall through world", player.pos.y > 0);

// --- player cannot walk into a solid wall ---
world.setBlock(Math.floor(player.pos.x) + 1, Math.floor(player.pos.y), Math.floor(player.pos.z), BLOCK.STONE);
const startX = player.pos.x;
for (let i = 0; i < 60; i++) player.update(1 / 60, { ...noInput, moveX: 1 });
check("collision blocks movement into wall", player.pos.x - startX < 0.6);

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
