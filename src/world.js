import * as THREE from "three";
import {
  BLOCK,
  BLOCK_DEFS,
  isTransparent,
  faceTexture,
} from "./blocks.js";

export const CHUNK_SIZE = 16;
export const WORLD_CHUNKS = 8; // world is WORLD_CHUNKS^2 chunks
export const WORLD_SIZE = CHUNK_SIZE * WORLD_CHUNKS; // blocks per axis
export const WORLD_HEIGHT = 48;
export const SEA_LEVEL = 18;

// Face definitions: each face has a normal, four corner offsets (CCW),
// and a shade factor for fake directional lighting.
const FACES = [
  {
    dir: [1, 0, 0],
    face: "side",
    shade: 0.82,
    corners: [
      [1, 1, 0],
      [1, 0, 0],
      [1, 0, 1],
      [1, 1, 1],
    ],
  },
  {
    dir: [-1, 0, 0],
    face: "side",
    shade: 0.82,
    corners: [
      [0, 1, 1],
      [0, 0, 1],
      [0, 0, 0],
      [0, 1, 0],
    ],
  },
  {
    dir: [0, 1, 0],
    face: "top",
    shade: 1.0,
    corners: [
      [0, 1, 1],
      [0, 1, 0],
      [1, 1, 0],
      [1, 1, 1],
    ],
  },
  {
    dir: [0, -1, 0],
    face: "bottom",
    shade: 0.55,
    corners: [
      [0, 0, 0],
      [0, 0, 1],
      [1, 0, 1],
      [1, 0, 0],
    ],
  },
  {
    dir: [0, 0, 1],
    face: "side",
    shade: 0.9,
    corners: [
      [1, 1, 1],
      [1, 0, 1],
      [0, 0, 1],
      [0, 1, 1],
    ],
  },
  {
    dir: [0, 0, -1],
    face: "side",
    shade: 0.7,
    corners: [
      [0, 1, 0],
      [0, 0, 0],
      [1, 0, 0],
      [1, 1, 0],
    ],
  },
];

// ---- deterministic value-noise terrain ----
function hash2(x, z, seed) {
  // 32-bit integer hash. Uses Math.imul + unsigned shifts so the full
  // [0, 1) range is produced (arithmetic >> would pin the high bit to 0).
  let h =
    Math.imul(x | 0, 374761393) ^
    Math.imul(z | 0, 668265263) ^
    Math.imul(seed | 0, 2147483647);
  h = Math.imul(h ^ (h >>> 13), 1274126177);
  h ^= h >>> 16;
  return (h >>> 0) / 4294967296;
}

function smooth(t) {
  return t * t * (3 - 2 * t);
}

function valueNoise(x, z, seed) {
  const xi = Math.floor(x);
  const zi = Math.floor(z);
  const xf = x - xi;
  const zf = z - zi;
  const a = hash2(xi, zi, seed);
  const b = hash2(xi + 1, zi, seed);
  const c = hash2(xi, zi + 1, seed);
  const d = hash2(xi + 1, zi + 1, seed);
  const u = smooth(xf);
  const v = smooth(zf);
  return (
    a * (1 - u) * (1 - v) +
    b * u * (1 - v) +
    c * (1 - u) * v +
    d * u * v
  );
}

function fbm(x, z, seed) {
  let total = 0;
  let amp = 1;
  let freq = 1;
  let max = 0;
  for (let o = 0; o < 4; o++) {
    total += valueNoise(x * freq, z * freq, seed + o * 17) * amp;
    max += amp;
    amp *= 0.5;
    freq *= 2;
  }
  return total / max;
}

export class World {
  constructor(seed = 1337) {
    this.seed = seed;
    this.sx = WORLD_SIZE;
    this.sy = WORLD_HEIGHT;
    this.sz = WORLD_SIZE;
    this.data = new Uint8Array(this.sx * this.sy * this.sz);
    this.group = new THREE.Group();
    this.chunkMeshes = new Map(); // "cx,cz" -> { opaque, transparent }
    this.dirtyChunks = new Set();
  }

  inBounds(x, y, z) {
    return (
      x >= 0 &&
      y >= 0 &&
      z >= 0 &&
      x < this.sx &&
      y < this.sy &&
      z < this.sz
    );
  }

  idx(x, y, z) {
    return (y * this.sz + z) * this.sx + x;
  }

  getBlock(x, y, z) {
    if (!this.inBounds(x, y, z)) return BLOCK.AIR;
    return this.data[this.idx(x, y, z)];
  }

  setBlock(x, y, z, id) {
    if (!this.inBounds(x, y, z)) return;
    this.data[this.idx(x, y, z)] = id;
  }

  // ---- generation ----
  generate() {
    const scale = 0.045;
    for (let x = 0; x < this.sx; x++) {
      for (let z = 0; z < this.sz; z++) {
        const n = fbm(x * scale, z * scale, this.seed);
        const hilly = fbm(x * scale * 0.4, z * scale * 0.4, this.seed + 99);
        let height = Math.floor(
          SEA_LEVEL - 4 + n * 18 + hilly * 10
        );
        height = Math.max(1, Math.min(this.sy - 6, height));

        for (let y = 0; y <= height; y++) {
          let id = BLOCK.STONE;
          if (y === height) {
            if (height < SEA_LEVEL + 1) id = BLOCK.SAND;
            else id = BLOCK.GRASS;
          } else if (y > height - 4) {
            id = height < SEA_LEVEL + 1 ? BLOCK.SAND : BLOCK.DIRT;
          }
          this.setBlock(x, y, z, id);
        }
        // water fill
        for (let y = height + 1; y <= SEA_LEVEL; y++) {
          this.setBlock(x, y, z, BLOCK.WATER);
        }
      }
    }
    this.plantTrees();
  }

  plantTrees() {
    for (let x = 4; x < this.sx - 4; x++) {
      for (let z = 4; z < this.sz - 4; z++) {
        if (hash2(x, z, this.seed + 555) > 0.992) {
          // find surface
          let y = this.sy - 1;
          while (y > 0 && this.getBlock(x, y, z) === BLOCK.AIR) y--;
          if (this.getBlock(x, y, z) !== BLOCK.GRASS) continue;
          this.makeTree(x, y + 1, z);
        }
      }
    }
  }

  makeTree(x, y, z) {
    const h = 4 + ((hash2(x, z, this.seed + 7) * 3) | 0);
    for (let i = 0; i < h; i++) this.setBlock(x, y + i, z, BLOCK.WOOD);
    const top = y + h;
    for (let dx = -2; dx <= 2; dx++) {
      for (let dz = -2; dz <= 2; dz++) {
        for (let dy = -2; dy <= 1; dy++) {
          const d = Math.abs(dx) + Math.abs(dz) + Math.abs(dy);
          if (d > 3) continue;
          const bx = x + dx;
          const by = top + dy;
          const bz = z + dz;
          if (this.getBlock(bx, by, bz) === BLOCK.AIR) {
            this.setBlock(bx, by, bz, BLOCK.LEAVES);
          }
        }
      }
    }
  }

  // ---- meshing ----
  chunkKey(cx, cz) {
    return cx + "," + cz;
  }

  markDirtyAt(x, z) {
    const cx = Math.floor(x / CHUNK_SIZE);
    const cz = Math.floor(z / CHUNK_SIZE);
    this.dirtyChunks.add(this.chunkKey(cx, cz));
    // also mark neighbours when on a border
    if (x % CHUNK_SIZE === 0) this.dirtyChunks.add(this.chunkKey(cx - 1, cz));
    if (x % CHUNK_SIZE === CHUNK_SIZE - 1)
      this.dirtyChunks.add(this.chunkKey(cx + 1, cz));
    if (z % CHUNK_SIZE === 0) this.dirtyChunks.add(this.chunkKey(cx, cz - 1));
    if (z % CHUNK_SIZE === CHUNK_SIZE - 1)
      this.dirtyChunks.add(this.chunkKey(cx, cz + 1));
  }

  buildAllChunks(material) {
    this.material = material;
    for (let cx = 0; cx < WORLD_CHUNKS; cx++) {
      for (let cz = 0; cz < WORLD_CHUNKS; cz++) {
        this.buildChunk(cx, cz);
      }
    }
  }

  flushDirty() {
    if (this.dirtyChunks.size === 0) return;
    for (const key of this.dirtyChunks) {
      const [cx, cz] = key.split(",").map(Number);
      if (cx < 0 || cz < 0 || cx >= WORLD_CHUNKS || cz >= WORLD_CHUNKS)
        continue;
      this.buildChunk(cx, cz);
    }
    this.dirtyChunks.clear();
  }

  buildChunk(cx, cz) {
    const key = this.chunkKey(cx, cz);
    const old = this.chunkMeshes.get(key);
    if (old) {
      if (old.opaque) {
        this.group.remove(old.opaque);
        old.opaque.geometry.dispose();
      }
      if (old.transparent) {
        this.group.remove(old.transparent);
        old.transparent.geometry.dispose();
      }
    }

    const opaque = { pos: [], norm: [], uv: [], col: [], idx: [] };
    const trans = { pos: [], norm: [], uv: [], col: [], idx: [] };

    const x0 = cx * CHUNK_SIZE;
    const z0 = cz * CHUNK_SIZE;

    for (let lx = 0; lx < CHUNK_SIZE; lx++) {
      for (let lz = 0; lz < CHUNK_SIZE; lz++) {
        for (let y = 0; y < this.sy; y++) {
          const x = x0 + lx;
          const z = z0 + lz;
          const id = this.getBlock(x, y, z);
          if (id === BLOCK.AIR) continue;
          const target =
            BLOCK_DEFS[id] && BLOCK_DEFS[id].transparent ? trans : opaque;
          this.addBlockFaces(target, x, y, z, id);
        }
      }
    }

    const meshes = {};
    meshes.opaque = this.makeMesh(opaque, false);
    meshes.transparent = this.makeMesh(trans, true);
    if (meshes.opaque) this.group.add(meshes.opaque);
    if (meshes.transparent) this.group.add(meshes.transparent);
    this.chunkMeshes.set(key, meshes);
  }

  addBlockFaces(buf, x, y, z, id) {
    for (const f of FACES) {
      const nx = x + f.dir[0];
      const ny = y + f.dir[1];
      const nz = z + f.dir[2];
      const neighbor = this.getBlock(nx, ny, nz);

      // Cull this face if the neighbour hides it.
      if (!this.shouldRenderFace(id, neighbor)) continue;

      const uv = this.uvs[faceTexture(id, f.face)];
      const start = buf.pos.length / 3;
      const shade = f.shade;

      for (let i = 0; i < 4; i++) {
        const c = f.corners[i];
        buf.pos.push(x + c[0], y + c[1], z + c[2]);
        buf.norm.push(f.dir[0], f.dir[1], f.dir[2]);
        buf.col.push(shade, shade, shade);
      }
      // uv: map corners -> atlas rect. Corner order chosen so textures are upright.
      buf.uv.push(uv[2], uv[3], uv[2], uv[1], uv[0], uv[1], uv[0], uv[3]);
      // Winding flipped (0,2,1 / 0,3,2) so the corner lists above produce
      // outward-facing triangles for THREE's default CCW front faces.
      buf.idx.push(start, start + 2, start + 1, start, start + 3, start + 2);
    }
  }

  shouldRenderFace(id, neighbor) {
    if (neighbor === BLOCK.AIR) return true;
    const neighborTransparent = isTransparent(neighbor);
    if (!neighborTransparent) return false;
    // Both transparent: don't render face between two of the same type
    // (e.g. inside water / glass) to reduce overdraw.
    if (neighbor === id) return false;
    return true;
  }

  makeMesh(buf, transparent) {
    if (buf.pos.length === 0) return null;
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.Float32BufferAttribute(buf.pos, 3));
    g.setAttribute("normal", new THREE.Float32BufferAttribute(buf.norm, 3));
    g.setAttribute("uv", new THREE.Float32BufferAttribute(buf.uv, 2));
    g.setAttribute("color", new THREE.Float32BufferAttribute(buf.col, 3));
    g.setIndex(buf.idx);
    const mat = transparent ? this.material.transparent : this.material.opaque;
    const mesh = new THREE.Mesh(g, mat);
    mesh.frustumCulled = true;
    return mesh;
  }

  setUVs(uvs) {
    this.uvs = uvs;
  }

  // ---- voxel raycast (DDA). Returns {x,y,z, nx,ny,nz} or null ----
  raycast(origin, dir, maxDist = 7) {
    let x = Math.floor(origin.x);
    let y = Math.floor(origin.y);
    let z = Math.floor(origin.z);

    const stepX = Math.sign(dir.x);
    const stepY = Math.sign(dir.y);
    const stepZ = Math.sign(dir.z);

    const tDeltaX = stepX !== 0 ? Math.abs(1 / dir.x) : Infinity;
    const tDeltaY = stepY !== 0 ? Math.abs(1 / dir.y) : Infinity;
    const tDeltaZ = stepZ !== 0 ? Math.abs(1 / dir.z) : Infinity;

    const distToBoundary = (s, o, step) => {
      if (step > 0) return Math.floor(o) + 1 - o;
      if (step < 0) return o - Math.floor(o);
      return Infinity;
    };

    let tMaxX = tDeltaX * distToBoundary(x, origin.x, stepX);
    let tMaxY = tDeltaY * distToBoundary(y, origin.y, stepY);
    let tMaxZ = tDeltaZ * distToBoundary(z, origin.z, stepZ);

    let nx = 0,
      ny = 0,
      nz = 0;
    let t = 0;

    while (t <= maxDist) {
      const block = this.getBlock(x, y, z);
      if (block !== BLOCK.AIR && block !== BLOCK.WATER) {
        return { x, y, z, nx, ny, nz };
      }
      if (tMaxX < tMaxY && tMaxX < tMaxZ) {
        x += stepX;
        t = tMaxX;
        tMaxX += tDeltaX;
        nx = -stepX;
        ny = 0;
        nz = 0;
      } else if (tMaxY < tMaxZ) {
        y += stepY;
        t = tMaxY;
        tMaxY += tDeltaY;
        nx = 0;
        ny = -stepY;
        nz = 0;
      } else {
        z += stepZ;
        t = tMaxZ;
        tMaxZ += tDeltaZ;
        nx = 0;
        ny = 0;
        nz = -stepZ;
      }
    }
    return null;
  }
}
