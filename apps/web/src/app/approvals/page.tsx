import Link from "next/link";
import { connection } from "next/server";
import { Suspense } from "react";
import { LiveRefresh } from "@/components/live-refresh";
import { requireMfaSession } from "@/lib/supabase/server";
import { DecisionForm } from "./decision-form";

const PAGE_SIZE = 50;
const APPROVAL_TABLES = ["approvals"];

export default function ApprovalsPage() {
  return (
    <main className="mx-auto mt-12 w-full max-w-3xl px-4">
      <header className="mb-8 flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Approvals</h1>
        <Link href="/" className="text-sm underline">Tasks</Link>
      </header>
      <LiveRefresh tables={APPROVAL_TABLES} />
      <Suspense fallback={<p>Loading approvals…</p>}>
        <ApprovalList />
      </Suspense>
    </main>
  );
}

async function ApprovalList() {
  await connection();
  const { supabase } = await requireMfaSession();
  const { data: approvals, error } = await supabase
    .from("approvals")
    .select("id, task_id, action_class, summary, status, requested_at, expires_at")
    .order("requested_at", { ascending: false })
    .order("id", { ascending: false })
    .limit(PAGE_SIZE);

  if (error) return <p role="alert" className="text-red-600">Could not load approvals: {error.message}</p>;
  if (approvals.length === 0) return <p>Nothing needs your approval.</p>;
  return (
    <ul className="flex flex-col gap-3">
      {approvals.map((a) => {
        const open = a.status === "pending" && new Date(a.expires_at) > new Date();
        return (
          <li key={a.id} className="rounded border px-3 py-3" data-testid="approval">
            <p className="font-medium">{a.summary}</p>
            <p className="text-sm opacity-70">
              {a.action_class} · {open ? `expires ${new Date(a.expires_at).toLocaleTimeString()}` : a.status}
            </p>
            {open && <DecisionForm approvalId={a.id} />}
          </li>
        );
      })}
    </ul>
  );
}
