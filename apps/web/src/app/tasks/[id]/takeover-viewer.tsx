"use client";

import { useRef, useState } from "react";
import { keyMessages, mouseMessage, wheelMessage, type Frame } from "@/lib/takeover";
import { callWorker, explain, takeoverSocketUrl } from "@/lib/worker-client";

type State = "idle" | "connecting" | "live" | "ended";

/** A live view of the task's browser. Everything typed or clicked here goes to that page. */
export function TakeoverViewer({ taskId }: { taskId: string }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const socket = useRef<WebSocket | null>(null);
  const frame = useRef<Frame>({ width: 1280, height: 720 });
  const [state, setState] = useState<State>("idle");
  const [message, setMessage] = useState<string | null>(null);
  const [unfinished, setUnfinished] = useState<string | null>(null);

  const send = (payload: object) => socket.current?.send(JSON.stringify(payload));
  const box = () => canvas.current!.getBoundingClientRect();

  async function start() {
    setMessage(null);
    setState("connecting");
    const issued = await callWorker<{ token: string }>(`/v1/tasks/${taskId}/takeover`);
    if (!issued.ok) {
      setState("idle");
      return setMessage(explain(issued));
    }
    const ws = new WebSocket(takeoverSocketUrl(issued.data.token));
    socket.current = ws;
    ws.onopen = () => setState("live");
    ws.onclose = () => setState("ended");
    ws.onmessage = (event) => {
      const data = JSON.parse(event.data);
      if (data.type === "not_done") return setUnfinished(String(data.reason));
      if (data.type !== "frame") return;
      frame.current = { width: data.width, height: data.height };
      const image = new Image();
      image.onload = () => {
        const target = canvas.current;
        if (!target) return;
        target.width = data.width;
        target.height = data.height;
        target.getContext("2d")!.drawImage(image, 0, 0);
      };
      image.src = `data:image/jpeg;base64,${data.data}`;
    };
  }

  return (
    <section className="mt-8">
      {state === "idle" || state === "ended" ? (
        <div className="flex items-center gap-3">
          <button type="button" onClick={() => void start()} className="rounded bg-black px-3 py-1 text-white">
            {state === "ended" ? "Take over again" : "Take over the browser"}
          </button>
          {state === "ended" && <span className="text-sm">The takeover has ended.</span>}
        </div>
      ) : (
        <div className="flex flex-col gap-2">
          <p className="text-sm">
            You are controlling the browser. The agent is paused until you press Done.
          </p>
          <canvas
            ref={canvas}
            tabIndex={0}
            data-testid="takeover-canvas"
            className="w-full max-w-4xl rounded border outline-none"
            onMouseDown={(e) => send(mouseMessage("press", e, box(), frame.current, "left"))}
            onMouseUp={(e) => send(mouseMessage("release", e, box(), frame.current, "left"))}
            onWheel={(e) => send(wheelMessage(e.deltaX, e.deltaY, e, box(), frame.current))}
            onKeyDown={(e) => {
              e.preventDefault();
              keyMessages(e).forEach(send);
            }}
          />
          {unfinished && (
            <p role="alert" className="text-sm text-amber-700">
              Not finished yet: {unfinished}. Finish signing in, or continue anyway.
            </p>
          )}
          <div className="flex gap-2">
            <button
              type="button"
              onClick={() => {
                setUnfinished(null);
                send({ type: "done" });
              }}
              className="rounded bg-black px-3 py-1 text-white"
            >
              Done — let the agent continue
            </button>
            {unfinished && (
              <button
                type="button"
                onClick={() => send({ type: "done", anyway: true })}
                className="rounded border px-3 py-1"
              >
                Continue anyway
              </button>
            )}
          </div>
        </div>
      )}
      {message && <p role="alert" className="mt-2 text-sm text-red-600">{message}</p>}
    </section>
  );
}
