import * as THREE from "three";
import { isSolid, isLiquid } from "./blocks.js";
import { WORLD_HEIGHT } from "./world.js";

// Player as an axis-aligned box that does swept-ish collision per axis.
const WIDTH = 0.6;
const HEIGHT = 1.8;
const EYE = 1.62;

const GRAVITY = 26;
const JUMP_SPEED = 8.4;
const WALK_SPEED = 4.7;
const FLY_SPEED = 9;

export class Player {
  constructor(world, spawn) {
    this.world = world;
    this.pos = new THREE.Vector3(spawn.x, spawn.y, spawn.z);
    this.vel = new THREE.Vector3();
    this.yaw = 0; // around Y
    this.pitch = 0; // up/down
    this.onGround = false;
    this.flying = false;
    this.half = WIDTH / 2;
  }

  getEyePosition() {
    return new THREE.Vector3(this.pos.x, this.pos.y + EYE, this.pos.z);
  }

  getForward() {
    return new THREE.Vector3(
      -Math.sin(this.yaw) * Math.cos(this.pitch),
      Math.sin(this.pitch),
      -Math.cos(this.yaw) * Math.cos(this.pitch)
    ).normalize();
  }

  applyToCamera(camera) {
    const eye = this.getEyePosition();
    camera.position.copy(eye);
    const dir = this.getForward();
    camera.lookAt(eye.x + dir.x, eye.y + dir.y, eye.z + dir.z);
  }

  // collision test for the player box at a candidate position
  collides(px, py, pz) {
    const minX = Math.floor(px - this.half);
    const maxX = Math.floor(px + this.half);
    const minY = Math.floor(py);
    const maxY = Math.floor(py + HEIGHT);
    const minZ = Math.floor(pz - this.half);
    const maxZ = Math.floor(pz + this.half);

    for (let x = minX; x <= maxX; x++) {
      for (let y = minY; y <= maxY; y++) {
        for (let z = minZ; z <= maxZ; z++) {
          if (isSolid(this.world.getBlock(x, y, z))) return true;
        }
      }
    }
    return false;
  }

  inLiquid() {
    const x = Math.floor(this.pos.x);
    const z = Math.floor(this.pos.z);
    const y = Math.floor(this.pos.y + 0.5);
    return isLiquid(this.world.getBlock(x, y, z));
  }

  update(dt, input) {
    // clamp dt to avoid tunnelling on lag spikes
    dt = Math.min(dt, 0.05);

    // look
    this.yaw -= input.lookX;
    this.pitch -= input.lookY;
    const lim = Math.PI / 2 - 0.01;
    this.pitch = Math.max(-lim, Math.min(lim, this.pitch));

    // movement direction relative to yaw
    const sin = Math.sin(this.yaw);
    const cos = Math.cos(this.yaw);
    // forward vector on XZ plane
    const fx = -sin;
    const fz = -cos;
    // right vector
    const rx = cos;
    const rz = -sin;

    const moveX = fx * input.moveY + rx * input.moveX;
    const moveZ = fz * input.moveY + rz * input.moveX;

    const inLiquid = this.inLiquid();

    if (this.flying) {
      const speed = FLY_SPEED;
      this.vel.x = moveX * speed;
      this.vel.z = moveZ * speed;
      this.vel.y = 0;
      if (input.jump) this.vel.y = speed;
      if (input.sneak) this.vel.y = -speed;
    } else {
      const speed = inLiquid ? WALK_SPEED * 0.6 : WALK_SPEED;
      this.vel.x = moveX * speed;
      this.vel.z = moveZ * speed;

      if (inLiquid) {
        this.vel.y -= GRAVITY * 0.25 * dt;
        if (this.vel.y < -3) this.vel.y = -3;
        if (input.jump) this.vel.y = 4.5; // swim up
      } else {
        this.vel.y -= GRAVITY * dt;
        if (input.jump && this.onGround) {
          this.vel.y = JUMP_SPEED;
          this.onGround = false;
        }
      }
    }

    this.moveAxis("x", this.vel.x * dt);
    this.moveAxis("z", this.vel.z * dt);
    this.moveAxis("y", this.vel.y * dt);

    // fell out of the world -> respawn at top
    if (this.pos.y < -10) {
      this.pos.y = WORLD_HEIGHT + 2;
      this.vel.set(0, 0, 0);
    }
  }

  moveAxis(axis, amount) {
    if (amount === 0) return;
    const next = this.pos.clone();
    next[axis] += amount;

    if (this.collides(next.x, next.y, next.z)) {
      if (axis === "y") {
        if (amount < 0) this.onGround = true;
        this.vel.y = 0;
      } else {
        this.vel[axis] = 0;
      }
      return;
    }

    if (axis === "y" && amount < 0) this.onGround = false;
    this.pos.copy(next);
  }
}
