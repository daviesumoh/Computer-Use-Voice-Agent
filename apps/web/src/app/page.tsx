import { connection } from "next/server";
import { Suspense } from "react";
import { signOut } from "@/app/actions";
import Link from "next/link";
import { requireMfaSession } from "@/lib/supabase/server";
import { LiveRefresh } from "@/components/live-refresh";
import { EmergencyStopBanner } from "./emergency-stop";
import { PushToggle } from "./push-toggle";

const PAGE_SIZE = 50;
const TASK_TABLES = ["tasks", "alerts", "approvals"];

export default function HomePage() {
  return (
    <main className="mx-auto mt-12 w-full max-w-3xl px-4">
      <header className="mb-8 flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Tasks</h1>
        <nav className="flex items-center gap-4 text-sm">
          <Link href="/approvals" className="underline">Approvals</Link>
          <Link href="/accounts" className="underline">Websites</Link>
          <Link href="/memory" className="underline">Memory</Link>
          <form action={signOut}>
            <button type="submit" className="underline">Sign out</button>
          </form>
        </nav>
      </header>
      <LiveRefresh tables={TASK_TABLES} />
      <PushToggle />
      <Suspense>
        <StopBanner />
        <AlertBanner />
      </Suspense>
      <Suspense fallback={<p>Loading tasks…</p>}>
        <TaskList />
      </Suspense>
    </main>
  );
}

async function StopBanner() {
  await connection();
  const { supabase } = await requireMfaSession();
  const { data } = await supabase.from("user_settings").select("emergency_stop").maybeSingle();
  return data?.emergency_stop ? <EmergencyStopBanner /> : null;
}

/** Problems the worker found with itself. They close on their own when the cause clears. */
async function AlertBanner() {
  await connection();
  const { supabase } = await requireMfaSession();
  const { data: alerts } = await supabase
    .from("alerts")
    .select("id, detail, raised_at")
    .is("resolved_at", null)
    .order("raised_at", { ascending: false })
    .limit(10);
  if (!alerts?.length) return null;
  return (
    <ul role="alert" className="mb-6 flex flex-col gap-1 rounded border border-amber-600 px-3 py-2" data-testid="alerts">
      {alerts.map((a) => (
        <li key={a.id}>
          {a.detail} <span className="text-sm opacity-70">(since {new Date(a.raised_at).toLocaleString()})</span>
        </li>
      ))}
    </ul>
  );
}

async function TaskList() {
  await connection(); // per-user data: always rendered at request time, never prerendered
  const { supabase } = await requireMfaSession();
  const { data: tasks, error } = await supabase
    .from("tasks")
    .select("id, short_code, title, status, created_at")
    .order("created_at", { ascending: false })
    .order("id", { ascending: false })
    .limit(PAGE_SIZE);

  if (error) return <p role="alert" className="text-red-600">Could not load tasks: {error.message}</p>;
  if (tasks.length === 0) return <p>No tasks yet.</p>;
  return (
    <ul className="flex flex-col gap-2">
      {tasks.map((task) => (
        <li key={task.id} className="rounded border px-3 py-2">
          <Link href={`/tasks/${task.id}`} className="underline">
            <span className="font-mono text-sm">{task.short_code}</span> {task.title}
          </Link>{" "}
          <span className="text-sm opacity-70">({task.status})</span>
        </li>
      ))}
    </ul>
  );
}
