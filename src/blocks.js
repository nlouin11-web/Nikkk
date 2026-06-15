import * as THREE from "three";

// Block type ids. 0 is always "air".
export const BLOCK = {
  AIR: 0,
  GRASS: 1,
  DIRT: 2,
  STONE: 3,
  WOOD: 4,
  LEAVES: 5,
  SAND: 6,
  WATER: 7,
  PLANK: 8,
  GLASS: 9,
  BRICK: 10,
  COBBLE: 11,
};

// Per-block definition. `faces` maps a face key to a texture name.
// Face order used by the mesher: px, nx, py, ny, pz, nz
export const BLOCK_DEFS = {
  [BLOCK.GRASS]: {
    name: "Grass",
    faces: { top: "grass_top", bottom: "dirt", side: "grass_side" },
  },
  [BLOCK.DIRT]: { name: "Dirt", faces: { all: "dirt" } },
  [BLOCK.STONE]: { name: "Stone", faces: { all: "stone" } },
  [BLOCK.WOOD]: {
    name: "Wood",
    faces: { top: "wood_top", bottom: "wood_top", side: "wood_side" },
  },
  [BLOCK.LEAVES]: { name: "Leaves", faces: { all: "leaves" }, transparent: true },
  [BLOCK.SAND]: { name: "Sand", faces: { all: "sand" } },
  [BLOCK.WATER]: {
    name: "Water",
    faces: { all: "water" },
    transparent: true,
    liquid: true,
  },
  [BLOCK.PLANK]: { name: "Planks", faces: { all: "plank" } },
  [BLOCK.GLASS]: { name: "Glass", faces: { all: "glass" }, transparent: true },
  [BLOCK.BRICK]: { name: "Brick", faces: { all: "brick" } },
  [BLOCK.COBBLE]: { name: "Cobble", faces: { all: "cobble" } },
};

// Blocks shown in the hotbar (player-placeable).
export const HOTBAR = [
  BLOCK.GRASS,
  BLOCK.DIRT,
  BLOCK.STONE,
  BLOCK.COBBLE,
  BLOCK.PLANK,
  BLOCK.WOOD,
  BLOCK.LEAVES,
  BLOCK.SAND,
  BLOCK.BRICK,
  BLOCK.GLASS,
];

export function isTransparent(id) {
  if (id === BLOCK.AIR) return true;
  const def = BLOCK_DEFS[id];
  return !!(def && def.transparent);
}

export function isSolid(id) {
  // Things the player collides with. Water is non-solid.
  return id !== BLOCK.AIR && id !== BLOCK.WATER;
}

export function isLiquid(id) {
  const def = BLOCK_DEFS[id];
  return !!(def && def.liquid);
}

// ---------------------------------------------------------------------------
// Procedural 16x16 pixel-art textures drawn to canvases, packed into an atlas.
// ---------------------------------------------------------------------------

const TILE = 16;

function makeCanvas(size = TILE) {
  const c = document.createElement("canvas");
  c.width = c.height = size;
  return c;
}

// deterministic noise helper
function rng(seed) {
  let s = seed >>> 0;
  return () => {
    s = (s * 1664525 + 1013904223) >>> 0;
    return s / 4294967296;
  };
}

function fill(ctx, color) {
  ctx.fillStyle = color;
  ctx.fillRect(0, 0, TILE, TILE);
}

// add per-pixel speckle variation
function speckle(ctx, colors, density = 0.5, seed = 1) {
  const r = rng(seed);
  for (let y = 0; y < TILE; y++) {
    for (let x = 0; x < TILE; x++) {
      if (r() < density) {
        ctx.fillStyle = colors[(r() * colors.length) | 0];
        ctx.fillRect(x, y, 1, 1);
      }
    }
  }
}

const TEXTURE_PAINTERS = {
  grass_top(ctx) {
    fill(ctx, "#5fa844");
    speckle(ctx, ["#6cb84e", "#549a3c", "#71c050", "#4f8f38"], 0.7, 11);
  },
  grass_side(ctx) {
    // dirt base
    fill(ctx, "#8a6240");
    speckle(ctx, ["#7d5738", "#946b47", "#6f4d31"], 0.6, 21);
    // grass lip on top
    ctx.fillStyle = "#5fa844";
    ctx.fillRect(0, 0, TILE, 4);
    const r = rng(7);
    for (let x = 0; x < TILE; x++) {
      const h = 3 + ((r() * 3) | 0);
      ctx.fillStyle = r() < 0.5 ? "#6cb84e" : "#549a3c";
      ctx.fillRect(x, 0, 1, h);
    }
  },
  dirt(ctx) {
    fill(ctx, "#8a6240");
    speckle(ctx, ["#7d5738", "#946b47", "#6f4d31", "#9c7350"], 0.7, 31);
  },
  stone(ctx) {
    fill(ctx, "#8a8a8a");
    speckle(ctx, ["#7e7e7e", "#969696", "#767676", "#9c9c9c"], 0.7, 41);
  },
  cobble(ctx) {
    fill(ctx, "#7c7c7c");
    speckle(ctx, ["#6a6a6a", "#8e8e8e", "#5e5e5e"], 0.8, 47);
    ctx.strokeStyle = "#555";
    ctx.lineWidth = 1;
    for (let i = 0; i < 4; i++) {
      ctx.strokeRect(
        (i % 2) * 8 + 0.5,
        Math.floor(i / 2) * 8 + 0.5,
        7,
        7
      );
    }
  },
  wood_top(ctx) {
    fill(ctx, "#9b7641");
    ctx.strokeStyle = "#7a5c30";
    for (let r = 6; r > 0; r -= 2) {
      ctx.beginPath();
      ctx.arc(8, 8, r, 0, Math.PI * 2);
      ctx.stroke();
    }
  },
  wood_side(ctx) {
    fill(ctx, "#6b4f2a");
    const r = rng(53);
    for (let x = 0; x < TILE; x += 1) {
      if (r() < 0.4) {
        ctx.fillStyle = "#5a4222";
        ctx.fillRect(x, 0, 1, TILE);
      }
    }
    speckle(ctx, ["#7a5c30", "#5a4222"], 0.2, 57);
  },
  leaves(ctx) {
    fill(ctx, "#3f7a2e");
    speckle(ctx, ["#356a26", "#499034", "#2d5d20", "#52a03a"], 0.9, 61);
  },
  sand(ctx) {
    fill(ctx, "#e0d29a");
    speckle(ctx, ["#d8c98c", "#ead9a4", "#cfc080"], 0.6, 71);
  },
  water(ctx) {
    fill(ctx, "#3a6fd0");
    speckle(ctx, ["#3566c4", "#4279de", "#2f5cb0"], 0.5, 81);
  },
  plank(ctx) {
    fill(ctx, "#b08a4e");
    ctx.fillStyle = "#9a7740";
    for (let y = 0; y < TILE; y += 4) ctx.fillRect(0, y, TILE, 1);
    ctx.fillStyle = "#c79c5c";
    speckle(ctx, ["#a8824a", "#c79c5c"], 0.25, 91);
  },
  glass(ctx) {
    ctx.clearRect(0, 0, TILE, TILE);
    ctx.fillStyle = "rgba(170,220,235,0.25)";
    ctx.fillRect(0, 0, TILE, TILE);
    ctx.strokeStyle = "rgba(220,245,255,0.9)";
    ctx.lineWidth = 1;
    ctx.strokeRect(0.5, 0.5, TILE - 1, TILE - 1);
    ctx.beginPath();
    ctx.moveTo(2, 13);
    ctx.lineTo(6, 9);
    ctx.stroke();
  },
  brick(ctx) {
    fill(ctx, "#9c4a3c");
    ctx.fillStyle = "#caa";
    // mortar lines
    for (let y = 0; y < TILE; y += 4) ctx.fillRect(0, y, TILE, 1);
    for (let y = 0; y < TILE; y += 8) {
      ctx.fillRect(7, y, 1, 4);
    }
    for (let y = 4; y < TILE; y += 8) {
      ctx.fillRect(3, y, 1, 4);
      ctx.fillRect(11, y, 1, 4);
    }
  },
};

// Build a single texture atlas containing every named tile, and return
// { texture, uv } where uv maps texture name -> [u0, v0, u1, v1].
export function buildAtlas() {
  const names = Object.keys(TEXTURE_PAINTERS);
  const cols = Math.ceil(Math.sqrt(names.length));
  const rows = Math.ceil(names.length / cols);
  const atlas = makeCanvas(cols * TILE);
  atlas.height = rows * TILE;
  const ctx = atlas.getContext("2d");
  ctx.imageSmoothingEnabled = false;

  const uv = {};
  names.forEach((name, i) => {
    const cx = (i % cols) * TILE;
    const cy = Math.floor(i / cols) * TILE;
    const tile = makeCanvas();
    const tctx = tile.getContext("2d");
    tctx.imageSmoothingEnabled = false;
    TEXTURE_PAINTERS[name](tctx);
    ctx.drawImage(tile, cx, cy);

    // small inset to avoid bleeding between atlas tiles
    const pad = 0.02;
    const u0 = (cx + pad) / atlas.width;
    const u1 = (cx + TILE - pad) / atlas.width;
    const v0 = 1 - (cy + TILE - pad) / atlas.height;
    const v1 = 1 - (cy + pad) / atlas.height;
    uv[name] = [u0, v0, u1, v1];
  });

  const texture = new THREE.CanvasTexture(atlas);
  texture.magFilter = THREE.NearestFilter;
  texture.minFilter = THREE.NearestFilter;
  texture.colorSpace = THREE.SRGBColorSpace;
  texture.generateMipmaps = false;

  return { texture, uv, atlasCanvas: atlas };
}

// Resolve the texture name for a given block id + face.
// face: "top" | "bottom" | "side"
export function faceTexture(id, face) {
  const def = BLOCK_DEFS[id];
  if (!def) return "stone";
  const f = def.faces;
  return f[face] || f.side || f.all || "stone";
}

// Draw a single block icon (used for the hotbar) into a target canvas.
export function drawBlockIcon(canvas, id) {
  const ctx = canvas.getContext("2d");
  ctx.imageSmoothingEnabled = false;
  canvas.width = canvas.height = TILE;
  const name = faceTexture(id, "side");
  TEXTURE_PAINTERS[name]
    ? TEXTURE_PAINTERS[name](ctx)
    : TEXTURE_PAINTERS.stone(ctx);
}
