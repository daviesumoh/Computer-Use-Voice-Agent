import Link from "next/link";
import { connection } from "next/server";
import { Suspense } from "react";
import { requireMfaSession } from "@/lib/supabase/server";
import { ConnectForm } from "./connect-form";
import { DisconnectButton } from "./disconnect-button";

export default function AccountsPage() {
  return (
    <main className="mx-auto mt-12 w-full max-w-3xl px-4">
      <header className="mb-8 flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Connected websites</h1>
        <Link href="/" className="text-sm underline">Tasks</Link>
      </header>
      <Suspense fallback={<p>Loading accounts…</p>}>
        <AccountList />
      </Suspense>
      <div className="mt-8"><ConnectForm /></div>
    </main>
  );
}

async function AccountList() {
  await connection();
  const { supabase } = await requireMfaSession();
  // Never selects the secret reference; the database does not grant that column anyway.
  const { data: accounts, error } = await supabase
    .from("connected_accounts")
    .select("id, site_domain, label, username_hint, allowed_origins, status")
    .order("site_domain")
    .order("label")
    .limit(100);

  if (error) return <p role="alert" className="text-red-600">Could not load accounts: {error.message}</p>;
  if (accounts.length === 0) return <p>No websites connected.</p>;
  return (
    <ul className="flex flex-col gap-3">
      {accounts.map((a) => (
        <li key={a.id} className="rounded border px-3 py-3">
          <p className="font-medium">{a.site_domain} <span className="text-sm opacity-70">({a.label})</span></p>
          <p className="text-sm opacity-70">{a.username_hint} · may sign in only on {a.allowed_origins.join(", ")}</p>
          <DisconnectButton accountId={a.id} />
        </li>
      ))}
    </ul>
  );
}
