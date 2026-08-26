"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { callWorker, explain } from "@/lib/worker-client";

function useWorkerAction() {
  const router = useRouter();
  const [message, setMessage] = useState<string | null>(null);
  async function run(path: string, body?: unknown) {
    setMessage(null);
    const result = await callWorker(path, body);
    if (!result.ok) return setMessage(explain(result));
    router.refresh();
  }
  return { message, run };
}

export function MemoryActions({
  memoryId,
  text,
  forgotten,
}: {
  memoryId: string;
  text: string;
  forgotten: boolean;
}) {
  const { message, run } = useWorkerAction();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(text);

  if (forgotten) {
    return (
      <div className="mt-2 flex gap-2">
        <button type="button" className="rounded border px-3 py-1 text-sm" onClick={() => void run(`/v1/memories/${memoryId}/restore`)}>
          Restore
        </button>
        <button type="button" className="rounded border px-3 py-1 text-sm" onClick={() => void run(`/v1/memories/${memoryId}/delete`, { immediately: true })}>
          Erase now
        </button>
        {message && <p role="alert" className="text-sm text-red-600">{message}</p>}
      </div>
    );
  }

  if (editing) {
    return (
      <form
        className="mt-2 flex flex-wrap gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          void run(`/v1/memories/${memoryId}/edit`, { text: draft }).then(() => setEditing(false));
        }}
      >
        <input
          aria-label="New wording"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          minLength={3}
          maxLength={500}
          className="min-w-64 flex-1 rounded border px-2 py-1"
        />
        <button type="submit" className="rounded bg-black px-3 py-1 text-sm text-white">Save</button>
        <button type="button" className="rounded border px-3 py-1 text-sm" onClick={() => setEditing(false)}>Cancel</button>
        {message && <p role="alert" className="w-full text-sm text-red-600">{message}</p>}
      </form>
    );
  }

  return (
    <div className="mt-2 flex flex-wrap gap-2">
      <button type="button" className="rounded border px-3 py-1 text-sm" onClick={() => setEditing(true)}>Edit</button>
      <button type="button" className="rounded border px-3 py-1 text-sm" onClick={() => void run(`/v1/memories/${memoryId}/delete`)}>
        Forget
      </button>
      <button
        type="button"
        className="rounded border px-3 py-1 text-sm"
        title="Erases it and the messages it was learned from"
        onClick={() => void run(`/v1/memories/${memoryId}/delete`, { immediately: true, with_sources: true })}
      >
        Erase with its sources
      </button>
      {message && <p role="alert" className="w-full text-sm text-red-600">{message}</p>}
    </div>
  );
}

export function SettleButtons({ memoryId }: { memoryId: string }) {
  const { message, run } = useWorkerAction();
  const settle = (keep: boolean) => run(`/v1/memories/${memoryId}/settle`, { memory_id: memoryId, keep });
  return (
    <div className="mt-2 flex gap-2">
      <button type="button" className="rounded bg-black px-3 py-1 text-sm text-white" onClick={() => void settle(true)}>Remember</button>
      <button type="button" className="rounded border px-3 py-1 text-sm" onClick={() => void settle(false)}>Don’t remember</button>
      {message && <p role="alert" className="text-sm text-red-600">{message}</p>}
    </div>
  );
}
