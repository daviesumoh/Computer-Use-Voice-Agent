/** Messages the dashboard sends to the worker's takeover socket (see worker/takeover.py). */

export type Frame = { width: number; height: number };
export type Point = { clientX: number; clientY: number };
export type Box = { left: number; top: number; width: number; height: number };

/** Maps a pointer position on the displayed picture to coordinates in the remote page. */
export function toPagePoint(point: Point, shown: Box, frame: Frame) {
  return {
    x: ((point.clientX - shown.left) / shown.width) * frame.width,
    y: ((point.clientY - shown.top) / shown.height) * frame.height,
  };
}

export function mouseMessage(
  action: "press" | "release" | "move",
  point: Point,
  shown: Box,
  frame: Frame,
  button: "left" | "right" | "middle" = "left",
) {
  return { type: "mouse", action, ...toPagePoint(point, shown, frame), button };
}

export function wheelMessage(deltaX: number, deltaY: number, point: Point, shown: Box, frame: Frame) {
  return {
    type: "mouse",
    action: "wheel",
    ...toPagePoint(point, shown, frame),
    button: "none",
    delta_x: deltaX,
    delta_y: deltaY,
  };
}

/**
 * Printable characters travel as text (so the remote page receives real input events);
 * everything else, such as Enter or arrows, as key presses.
 */
export function keyMessages(event: { key: string; code: string; ctrlKey: boolean; metaKey: boolean }) {
  if (event.key.length === 1 && !event.ctrlKey && !event.metaKey) {
    return [{ type: "text", text: event.key }];
  }
  return [
    { type: "key", action: "down", key: event.key, code: event.code },
    { type: "key", action: "up", key: event.key, code: event.code },
  ];
}
