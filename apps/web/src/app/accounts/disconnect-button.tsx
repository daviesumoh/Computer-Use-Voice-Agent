"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { CodeField } from "@/components/code-field";
import { callWorker, confirmWithCode, explain } from "@/lib/worker-client";

export function DisconnectButton({ accountId }: { accountId: string }) {
  const router = useRouter();
  const [message, setMessage] = useState<string | null>(null);

  async function disconnect(form: HTMLFormElement) {
    setMessage(null);
    const problem = await confirmWithCode(String(new FormData(form).get("code")));
    if (problem) return setMessage(problem);
    const result = await callWorker(`/v1/accounts/${accountId}/disconnect`);
    if (!result.ok) return setMessage(explain(result));
    router.refresh();
  }

  return (
    <form
      className="mt-2 flex flex-wrap items-center gap-2"
      onSubmit={(event) => {
        event.preventDefault();
        void disconnect(event.currentTarget);
      }}
    >
      <CodeField id={`disconnect-${accountId}`} />
      <button type="submit" className="rounded border px-3 py-1 text-sm">Disconnect and delete the password</button>
      {message && <p role="alert" className="w-full text-sm text-red-600">{message}</p>}
    </form>
  );
}
