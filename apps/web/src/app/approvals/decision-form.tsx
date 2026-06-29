"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { CodeField } from "@/components/code-field";
import { callWorker, confirmWithCode, explain } from "@/lib/worker-client";

export function DecisionForm({ approvalId }: { approvalId: string }) {
  const router = useRouter();
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function decide(decision: "approved" | "denied", form: HTMLFormElement) {
    setBusy(true);
    setMessage(null);
    try {
      if (decision === "approved") {
        const problem = await confirmWithCode(String(new FormData(form).get("code")));
        if (problem) return setMessage(problem);
      }
      const result = await callWorker(`/v1/approvals/${approvalId}/decide`, { decision });
      if (!result.ok) return setMessage(explain(result));
      router.refresh();
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      className="mt-3 flex flex-wrap items-center gap-3"
      onSubmit={(event) => {
        event.preventDefault();
        void decide("approved", event.currentTarget);
      }}
    >
      <CodeField id={`code-${approvalId}`} />
      <button type="submit" disabled={busy} className="rounded bg-black px-3 py-1 text-white">
        Approve
      </button>
      <button
        type="button"
        disabled={busy}
        className="rounded border px-3 py-1"
        onClick={(event) => void decide("denied", event.currentTarget.form!)}
      >
        Deny
      </button>
      {message && <p role="alert" className="w-full text-sm text-red-600">{message}</p>}
    </form>
  );
}
