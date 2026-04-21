import Link from "next/link";
import { notFound } from "next/navigation";
import { connection } from "next/server";
import { Suspense } from "react";
import { LiveRefresh } from "@/components/live-refresh";
import { requireMfaSession } from "@/lib/supabase/server";
import { TakeoverViewer } from "./takeover-viewer";

const EVENT_LIMIT = 100;
const TASK_TABLES = ["tasks", "task_events"];

export default function TaskPage({ params }: { params: Promise<{ id: string }> }) {
  return (
    <main className="mx-auto mt-12 w-full max-w-3xl px-4">
      <Link href="/" className="text-sm underline">Tasks</Link>
      <LiveRefresh tables={TASK_TABLES} />
      <Suspense fallback={<p className="mt-6">Loading task…</p>}>
        <Task params={params} />
      </Suspense>
    </main>
  );
}

async function Task({ params }: { params: Promise<{ id: string }> }) {
  await connection();
  const { id } = await params;
  const { supabase } = await requireMfaSession();
  const { data: task } = await supabase
    .from("tasks")
    .select("id, short_code, title, status, waiting_reason, spent_cents, budget_cents")
    .eq("id", id)
    .maybeSingle();
  if (!task) notFound();

  const { data: events } = await supabase
    .from("task_events")
    .select("seq, type, payload, created_at")
    .eq("task_id", id)
    .order("seq", { ascending: false })
    .limit(EVENT_LIMIT);

  return (
    <>
      <h1 className="mt-6 text-2xl font-semibold">
        <span className="font-mono">{task.short_code}</span> {task.title}
      </h1>
      <p className="text-sm opacity-70">
        {task.status}
        {task.waiting_reason ? ` (${task.waiting_reason})` : ""} · ${(task.spent_cents / 100).toFixed(2)} of $
        {(task.budget_cents / 100).toFixed(2)}
      </p>
      {task.status === "waiting_user" && task.waiting_reason === "approval" && (
        <p className="mt-4"><Link href="/approvals" className="underline">Review the approval request</Link></p>
      )}
      {task.status === "waiting_user" && <TakeoverViewer taskId={task.id} />}
      <h2 className="mt-8 mb-2 font-medium">Activity</h2>
      <ol className="flex flex-col gap-1 text-sm">
        {(events ?? []).map((e) => (
          <li key={e.seq}>
            <span className="font-mono opacity-60">{e.seq}</span> {e.type}
            {typeof e.payload?.summary === "string" ? ` — ${e.payload.summary}` : ""}
          </li>
        ))}
      </ol>
    </>
  );
}
