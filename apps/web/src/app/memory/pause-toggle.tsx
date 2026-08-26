"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { callWorker, explain } from "@/lib/worker-client";

export function PauseToggle({ enabled }: { enabled: boolean }) {
  const router = useRouter();
  const [message, setMessage] = useState<string | null>(null);

  async function toggle() {
    setMessage(null);
    const result = await callWorker("/v1/memory/enabled", { enabled: !enabled });
    if (!result.ok) return setMessage(explain(result));
    router.refresh();
  }

  return (
    <div className="flex flex-wrap items-center gap-3">
      <p>{enabled ? "Memory is on." : "Memory is paused: nothing new is learned or used."}</p>
      <button type="button" onClick={() => void toggle()} className="rounded border px-3 py-1 text-sm">
        {enabled ? "Pause memory" : "Resume memory"}
      </button>
      {message && <p role="alert" className="w-full text-sm text-red-600">{message}</p>}
    </div>
  );
}
