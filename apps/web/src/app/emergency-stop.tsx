"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { CodeField } from "@/components/code-field";
import { callWorker, confirmWithCode, explain } from "@/lib/worker-client";

/** Shown while the emergency stop is on. Only a fresh authenticator code clears it. */
export function EmergencyStopBanner() {
  const router = useRouter();
  const [message, setMessage] = useState<string | null>(null);

  async function clear(form: HTMLFormElement) {
    setMessage(null);
    const problem = await confirmWithCode(String(new FormData(form).get("code")));
    if (problem) return setMessage(problem);
    const result = await callWorker("/v1/emergency-stop/clear");
    if (!result.ok) return setMessage(explain(result));
    router.refresh();
  }

  return (
    <form
      role="alert"
      className="mb-6 flex flex-wrap items-center gap-3 rounded border border-red-600 px-3 py-2"
      onSubmit={(event) => {
        event.preventDefault();
        void clear(event.currentTarget);
      }}
    >
      <strong>Emergency stop is on.</strong> Tasks are paused and will not resume.
      <CodeField id="stop-code" />
      <button type="submit" className="rounded bg-black px-3 py-1 text-white">Clear emergency stop</button>
      {message && <p className="w-full text-sm text-red-600">{message}</p>}
    </form>
  );
}
