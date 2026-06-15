// Touch + mouse controls. Exposes a frame-consumable input state plus
// callbacks for discrete actions (break / place).
//
// Layout:
//   - Left circular pad  -> movement (moveX, moveY in -1..1)
//   - Anywhere else on the canvas -> drag to look; quick tap = break
//   - Dedicated buttons  -> jump (hold), place (tap), fly (toggle)

const LOOK_SENSITIVITY = 0.005;
const TAP_MAX_MOVE = 12; // px
const TAP_MAX_TIME = 250; // ms

export class Controls {
  constructor({ canvas, joystick, knob, jumpBtn, placeBtn, flyBtn }) {
    this.canvas = canvas;
    this.joystick = joystick;
    this.knob = knob;

    this.state = {
      moveX: 0,
      moveY: 0,
      lookX: 0,
      lookY: 0,
      jump: false,
      sneak: false,
    };

    this.onBreak = () => {};
    this.onPlace = () => {};

    this.joystickId = null;
    this.joyCenter = { x: 0, y: 0 };
    this.joyRadius = 55;

    this.lookId = null;
    this.lookLast = { x: 0, y: 0 };
    this.lookStart = { x: 0, y: 0, t: 0 };

    this.bind(jumpBtn, placeBtn, flyBtn);
  }

  bind(jumpBtn, placeBtn, flyBtn) {
    // --- joystick ---
    this.joystick.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      this.joystickId = e.pointerId;
      const r = this.joystick.getBoundingClientRect();
      this.joyCenter = { x: r.left + r.width / 2, y: r.top + r.height / 2 };
      this.joyRadius = r.width / 2;
      this.updateJoystick(e.clientX, e.clientY);
    });

    // --- look / break on the whole canvas ---
    this.canvas.addEventListener("pointerdown", (e) => {
      if (this.lookId !== null) return;
      this.lookId = e.pointerId;
      this.lookLast = { x: e.clientX, y: e.clientY };
      this.lookStart = { x: e.clientX, y: e.clientY, t: performance.now() };
    });

    window.addEventListener(
      "pointermove",
      (e) => {
        if (e.pointerId === this.joystickId) {
          e.preventDefault();
          this.updateJoystick(e.clientX, e.clientY);
        } else if (e.pointerId === this.lookId) {
          const dx = e.clientX - this.lookLast.x;
          const dy = e.clientY - this.lookLast.y;
          this.lookLast = { x: e.clientX, y: e.clientY };
          this.state.lookX += dx * LOOK_SENSITIVITY;
          this.state.lookY += dy * LOOK_SENSITIVITY;
        }
      },
      { passive: false }
    );

    const endPointer = (e) => {
      if (e.pointerId === this.joystickId) {
        this.joystickId = null;
        this.state.moveX = 0;
        this.state.moveY = 0;
        this.knob.style.transform = "translate(0px, 0px)";
      } else if (e.pointerId === this.lookId) {
        const dx = e.clientX - this.lookStart.x;
        const dy = e.clientY - this.lookStart.y;
        const dist = Math.hypot(dx, dy);
        const dt = performance.now() - this.lookStart.t;
        if (dist < TAP_MAX_MOVE && dt < TAP_MAX_TIME) {
          this.onBreak();
        }
        this.lookId = null;
      }
    };
    window.addEventListener("pointerup", endPointer);
    window.addEventListener("pointercancel", endPointer);

    // --- buttons ---
    const hold = (el, key) => {
      el.addEventListener("pointerdown", (e) => {
        e.preventDefault();
        this.state[key] = true;
      });
      const release = (e) => {
        e.preventDefault();
        this.state[key] = false;
      };
      el.addEventListener("pointerup", release);
      el.addEventListener("pointercancel", release);
      el.addEventListener("pointerleave", release);
    };
    hold(jumpBtn, "jump");

    placeBtn.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      this.onPlace();
    });

    flyBtn.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      this.flying = !this.flying;
      flyBtn.classList.toggle("toggled", this.flying);
      if (this.onToggleFly) this.onToggleFly(this.flying);
    });

    // --- desktop keyboard (handy for testing) ---
    this.keys = {};
    window.addEventListener("keydown", (e) => {
      this.keys[e.code] = true;
      if (e.code === "Space") this.state.jump = true;
      if (e.code === "ShiftLeft") this.state.sneak = true;
    });
    window.addEventListener("keyup", (e) => {
      this.keys[e.code] = false;
      if (e.code === "Space") this.state.jump = false;
      if (e.code === "ShiftLeft") this.state.sneak = false;
    });
  }

  updateJoystick(cx, cy) {
    let dx = cx - this.joyCenter.x;
    let dy = cy - this.joyCenter.y;
    const dist = Math.hypot(dx, dy);
    const max = this.joyRadius;
    if (dist > max) {
      dx = (dx / dist) * max;
      dy = (dy / dist) * max;
    }
    this.knob.style.transform = `translate(${dx}px, ${dy}px)`;
    this.state.moveX = dx / max;
    this.state.moveY = -dy / max; // up = forward
  }

  // Merge keyboard movement into the joystick state for desktop.
  applyKeyboard() {
    if (!this.keys) return;
    let kx = 0;
    let ky = 0;
    if (this.keys["KeyW"]) ky += 1;
    if (this.keys["KeyS"]) ky -= 1;
    if (this.keys["KeyA"]) kx -= 1;
    if (this.keys["KeyD"]) kx += 1;
    if (kx !== 0 || ky !== 0) {
      const len = Math.hypot(kx, ky) || 1;
      this.state.moveX = kx / len;
      this.state.moveY = ky / len;
    }
  }

  // Read-and-reset the look delta so it's applied exactly once per frame.
  consumeLook() {
    const lx = this.state.lookX;
    const ly = this.state.lookY;
    this.state.lookX = 0;
    this.state.lookY = 0;
    return { lx, ly };
  }
}
