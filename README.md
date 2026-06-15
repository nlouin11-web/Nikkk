# Blockcraft — Mobile

A mobile-friendly **Minecraft-style voxel sandbox** that runs in any modern
browser. Built with [Three.js](https://threejs.org/) and plain ES modules — no
build step, no framework.

![block world](https://img.shields.io/badge/engine-three.js-blue) ![mobile](https://img.shields.io/badge/controls-touch-green)

## Features

- **Procedural voxel world** — a 128×128 island generated from value-noise
  terrain with hills, beaches, an ocean at sea level, and scattered trees.
- **Build & mine** — break any block by tapping and place blocks from a
  10-slot hotbar (grass, dirt, stone, cobble, planks, wood, leaves, sand,
  brick, glass).
- **Touch-first controls**
  - **Left thumb pad** — move
  - **Drag anywhere on the right** — look around
  - **Tap the world** — break the targeted block
  - **PLACE** button — build with the selected block
  - **JUMP** button, plus a **FLY** toggle for creative building
- **First-person physics** — gravity, jumping, AABB collision, and swimming
  in water.
- **Performant chunked rendering** — the world is split into 16×16 chunks with
  face-culled, vertex-shaded greedy-ish meshes; only edited chunks are rebuilt.
- **Procedural pixel-art textures** — every block texture is drawn at runtime
  to a canvas atlas, so there are zero image assets to download.
- **Self-contained** — Three.js is vendored in `vendor/`, so the game works
  fully offline.

## Run it

The game uses ES modules, so it must be served over HTTP (not opened as a
`file://` URL).

```bash
# any static file server works, e.g.:
npm start                 # python3 -m http.server 8080
# or
npx http-server -p 8080
```

Then open **http://localhost:8080** — on a phone, open the same address on your
local network. Tap **TAP TO PLAY**.

### Desktop controls (handy for testing)

- **WASD** — move, **mouse drag** — look, **Space** — jump
- **Left click / tap** — break, **right click** — place
- **1–0** — select hotbar slot, **F** — toggle fly

## Project layout

```
index.html        # markup, HUD, importmap
styles.css        # HUD + touch UI styling (safe-area aware)
vendor/           # vendored three.module.js
src/
  blocks.js       # block definitions + procedural texture atlas
  world.js        # chunked voxel storage, terrain gen, mesher, voxel raycast
  player.js       # first-person physics & collision
  controls.js     # touch joystick / look / buttons + keyboard fallback
  main.js         # scene setup, hotbar UI, block interaction, render loop
logic.test.mjs    # headless tests for gen / meshing / raycast / physics
```

## Tests

```bash
npm install   # pulls in three for the test runner
npm test      # runs the headless logic tests
```

The tests cover terrain generation, chunk mesh buffers, the DDA voxel raycast,
block edits, and player gravity/collision (no WebGL context required).

## License

MIT
