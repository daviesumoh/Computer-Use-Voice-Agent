import assert from "node:assert/strict";
import { test } from "node:test";
import { keyMessages, mouseMessage, toPagePoint, wheelMessage } from "./takeover.ts";

const shown = { left: 100, top: 50, width: 640, height: 360 }; // picture drawn at half size
const frame = { width: 1280, height: 720 };

test("pointer positions are scaled from the displayed picture to the remote page", () => {
  assert.deepEqual(toPagePoint({ clientX: 100, clientY: 50 }, shown, frame), { x: 0, y: 0 });
  assert.deepEqual(toPagePoint({ clientX: 420, clientY: 230 }, shown, frame), { x: 640, y: 360 });
  assert.deepEqual(toPagePoint({ clientX: 740, clientY: 410 }, shown, frame), { x: 1280, y: 720 });
});

test("mouse and wheel messages carry page coordinates in the shape the worker expects", () => {
  assert.deepEqual(mouseMessage("press", { clientX: 420, clientY: 230 }, shown, frame), {
    type: "mouse", action: "press", x: 640, y: 360, button: "left",
  });
  assert.deepEqual(wheelMessage(0, 120, { clientX: 100, clientY: 50 }, shown, frame), {
    type: "mouse", action: "wheel", x: 0, y: 0, button: "none", delta_x: 0, delta_y: 120,
  });
});

test("printable keys are sent as text and control keys as presses", () => {
  const base = { code: "", ctrlKey: false, metaKey: false };
  assert.deepEqual(keyMessages({ ...base, key: "a" }), [{ type: "text", text: "a" }]);
  assert.deepEqual(keyMessages({ ...base, key: "Enter", code: "Enter" }), [
    { type: "key", action: "down", key: "Enter", code: "Enter" },
    { type: "key", action: "up", key: "Enter", code: "Enter" },
  ]);
  // A shortcut such as Ctrl+A must not type the letter.
  assert.equal(keyMessages({ ...base, key: "a", ctrlKey: true })[0].type, "key");
});
