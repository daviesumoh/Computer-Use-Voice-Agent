"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { CodeField } from "@/components/code-field";
import { callWorker, confirmWithCode, explain } from "@/lib/worker-client";

/** The password goes from this form straight to the worker, never through the web server. */
export function ConnectForm() {
  const router = useRouter();
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function connect(form: HTMLFormElement) {
    const data = new FormData(form);
    setBusy(true);
    setMessage(null);
    try {
      const problem = await confirmWithCode(String(data.get("code")));
      if (problem) return setMessage(problem);
      const result = await callWorker("/v1/accounts", {
        site_domain: String(data.get("site_domain")).trim().toLowerCase(),
        label: String(data.get("label")).trim(),
        username: String(data.get("username")),
        password: String(data.get("password")),
      });
      if (!result.ok) return setMessage(explain(result));
      form.reset();
      router.refresh();
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      className="flex flex-col gap-3 rounded border p-4"
      onSubmit={(event) => {
        event.preventDefault();
        void connect(event.currentTarget);
      }}
    >
      <h2 className="font-medium">Connect a website</h2>
      <label className="flex flex-col text-sm">Website (e.g. bestbuy.ca)
        <input name="site_domain" required className="rounded border px-2 py-1" />
      </label>
      <label className="flex flex-col text-sm">Label
        <input name="label" defaultValue="main" required className="rounded border px-2 py-1" />
      </label>
      <label className="flex flex-col text-sm">Username or email
        <input name="username" autoComplete="off" required className="rounded border px-2 py-1" />
      </label>
      <label className="flex flex-col text-sm">Password
        <input name="password" type="password" autoComplete="new-password" required className="rounded border px-2 py-1" />
      </label>
      <CodeField id="connect-code" />
      <button type="submit" disabled={busy} className="self-start rounded bg-black px-3 py-1 text-white">
        Connect
      </button>
      {message && <p role="alert" className="text-sm text-red-600">{message}</p>}
    </form>
  );
}
