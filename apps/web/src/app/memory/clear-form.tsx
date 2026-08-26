"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { CodeField } from "@/components/code-field";
import { callWorker, confirmWithCode, explain } from "@/lib/worker-client";

export function ClearForm() {
  const router = useRouter();
  const [message, setMessage] = useState<string | null>(null);

  async function clear(form: HTMLFormElement) {
    setMessage(null);
    const data = new FormData(form);
    const problem = await confirmWithCode(String(data.get("code")));
    if (problem) return setMessage(problem);
    const type = String(data.get("type"));
    const result = await callWorker<{ cleared: number }>("/v1/memories/clear", type ? { type } : {});
    if (!result.ok) return setMessage(explain(result));
    setMessage(`Cleared ${result.data.cleared}.`);
    router.refresh();
  }

  return (
    <form
      className="flex flex-wrap items-center gap-3 rounded border border-red-300 px-3 py-3"
      onSubmit={(event) => {
        event.preventDefault();
        void clear(event.currentTarget);
      }}
    >
      <label className="flex items-center gap-2 text-sm">
        Clear
        <select name="type" className="rounded border px-2 py-1">
          <option value="">everything (and call and task history)</option>
          <option value="rule">all rules</option>
          <option value="preference">all preferences</option>
          <option value="fact">all facts</option>
        </select>
      </label>
      <CodeField id="clear-code" />
      <button type="submit" className="rounded border border-red-600 px-3 py-1 text-sm text-red-700">Clear</button>
      {message && <p role="status" className="w-full text-sm">{message}</p>}
    </form>
  );
}
