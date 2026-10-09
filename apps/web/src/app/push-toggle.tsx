"use client";

import { useEffect, useState } from "react";
import { callWorker, explain } from "@/lib/worker-client";

const PUBLIC_KEY = process.env.NEXT_PUBLIC_VAPID_PUBLIC_KEY;

/** The VAPID key is base64url; the push API wants raw bytes. */
function keyBytes(base64url: string) {
  const padded = base64url.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(base64url.length / 4) * 4, "=");
  return Uint8Array.from(atob(padded), (c) => c.charCodeAt(0));
}

type State = "unsupported" | "off" | "on";

/** Turns notifications on or off for this browser. Texts keep working either way. */
export function PushToggle() {
  const [state, setState] = useState<State | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    const supported = PUBLIC_KEY && "serviceWorker" in navigator && "PushManager" in window;
    const found: Promise<State> = supported
      ? navigator.serviceWorker
          .register("/sw.js")
          .then((registration) => registration.pushManager.getSubscription())
          .then((subscription) => (subscription ? "on" : "off"))
      : Promise.resolve("unsupported");
    void found.then(setState);
  }, []);

  async function enable() {
    setMessage(null);
    if ((await Notification.requestPermission()) !== "granted") {
      return setMessage("Notifications are blocked for this site in the browser.");
    }
    const registration = await navigator.serviceWorker.ready;
    const subscription = await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: keyBytes(PUBLIC_KEY!),
    });
    const { endpoint, keys } = subscription.toJSON() as { endpoint: string; keys: { p256dh: string; auth: string } };
    const result = await callWorker("/v1/push/subscribe", { endpoint, p256dh: keys.p256dh, auth: keys.auth });
    if (!result.ok) {
      await subscription.unsubscribe();
      return setMessage(explain(result));
    }
    setState("on");
  }

  async function disable() {
    setMessage(null);
    const registration = await navigator.serviceWorker.ready;
    const subscription = await registration.pushManager.getSubscription();
    if (!subscription) return setState("off");
    const result = await callWorker("/v1/push/unsubscribe", { endpoint: subscription.endpoint });
    if (!result.ok) return setMessage(explain(result));
    await subscription.unsubscribe();
    setState("off");
  }

  if (state === null || state === "unsupported") return null;
  return (
    <div className="mb-4 flex flex-wrap items-center gap-3 text-sm">
      <span>{state === "on" ? "Notifications are on for this browser." : "Notifications are off for this browser."}</span>
      <button type="button" className="rounded border px-3 py-1" onClick={() => void (state === "on" ? disable() : enable())}>
        {state === "on" ? "Turn off" : "Turn on notifications"}
      </button>
      {message && <p role="alert" className="w-full text-red-600">{message}</p>}
    </div>
  );
}
