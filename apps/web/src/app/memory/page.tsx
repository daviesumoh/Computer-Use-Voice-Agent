import Link from "next/link";
import { connection } from "next/server";
import { Suspense } from "react";
import { requireMfaSession } from "@/lib/supabase/server";
import { ClearForm } from "./clear-form";
import { MemoryActions, SettleButtons } from "./memory-actions";
import { PauseToggle } from "./pause-toggle";

const PAGE_SIZE = 50;
const TYPES = ["rule", "preference", "fact"] as const;

type Search = { q?: string; type?: string; before?: string };

export default function MemoryPage({ searchParams }: { searchParams: Promise<Search> }) {
  return (
    <main className="mx-auto mt-12 w-full max-w-3xl px-4">
      <header className="mb-8 flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Memory</h1>
        <Link href="/" className="text-sm underline">Tasks</Link>
      </header>
      <Suspense fallback={<p>Loading memory…</p>}>
        <Memory searchParams={searchParams} />
      </Suspense>
    </main>
  );
}

/** PostgREST treats these as pattern and filter syntax, so they are escaped before searching. */
function likePattern(text: string) {
  return `%${text.replace(/[\\%_,()]/g, " ").trim()}%`;
}

async function Memory({ searchParams }: { searchParams: Promise<Search> }) {
  await connection();
  const { q = "", type = "", before = "" } = await searchParams;
  const { supabase, userId } = await requireMfaSession();

  const settings = await supabase
    .from("user_settings")
    .select("memory_enabled")
    .eq("user_id", userId)
    .maybeSingle();
  const enabled = settings.data?.memory_enabled ?? true;

  let list = supabase
    .from("memories")
    .select(
      "id, type, canonical_text, status, origin, created_at, announced_at, memory_sources(source_type, span)",
    )
    .neq("status", "superseded")
    .order("created_at", { ascending: false })
    .limit(PAGE_SIZE + 1);
  if (TYPES.includes(type as (typeof TYPES)[number])) list = list.eq("type", type);
  if (q.trim()) list = list.ilike("canonical_text", likePattern(q));
  if (before) list = list.lt("created_at", before);
  const { data, error } = await list;
  if (error) return <p role="alert" className="text-red-600">Could not load memory: {error.message}</p>;

  const memories = data.slice(0, PAGE_SIZE);
  const older = data.length > PAGE_SIZE ? memories[memories.length - 1].created_at : null;
  const pending = memories.filter((m) => m.status === "pending_confirm");
  const shown = memories.filter((m) => m.status !== "pending_confirm");

  return (
    <>
      <PauseToggle enabled={enabled} />

      {pending.length > 0 && (
        <section className="mt-8">
          <h2 className="mb-2 text-lg font-medium">Noticed, not yet confirmed</h2>
          <p className="mb-3 text-sm opacity-70">
            You made these choices more than once. Nothing is remembered until you say so.
          </p>
          <ul className="flex flex-col gap-3">
            {pending.map((m) => (
              <li key={m.id} className="rounded border px-3 py-3" data-testid="pending-memory">
                <p>{m.canonical_text}</p>
                <SettleButtons memoryId={m.id} />
              </li>
            ))}
          </ul>
        </section>
      )}

      <section className="mt-8">
        <form className="mb-4 flex flex-wrap items-center gap-2" role="search">
          <input
            name="q"
            defaultValue={q}
            placeholder="Search what is remembered"
            aria-label="Search"
            className="min-w-48 flex-1 rounded border px-2 py-1"
          />
          <select name="type" defaultValue={type} aria-label="Type" className="rounded border px-2 py-1">
            <option value="">All types</option>
            {TYPES.map((t) => (
              <option key={t} value={t}>{t}</option>
            ))}
          </select>
          <button type="submit" className="rounded border px-3 py-1">Filter</button>
        </form>

        {shown.length === 0 ? (
          <p>{q || type ? "Nothing matches." : "Nothing is remembered yet."}</p>
        ) : (
          <ul className="flex flex-col gap-3">
            {shown.map((m) => (
              <li key={m.id} className="rounded border px-3 py-3" data-testid="memory">
                <p className={m.status === "deleted" ? "line-through opacity-60" : ""}>{m.canonical_text}</p>
                <p className="text-sm opacity-70">
                  {m.type} · {m.origin.replace("_", " ")} · {new Date(m.created_at).toLocaleDateString()}
                  {m.status === "deleted" && " · forgotten, can be restored for a limited time"}
                </p>
                {m.memory_sources.length > 0 && (
                  <details className="mt-1 text-sm">
                    <summary className="cursor-pointer opacity-70">Where this came from</summary>
                    <ul className="mt-1 list-disc pl-5">
                      {m.memory_sources.map((s, i) => (
                        <li key={i}>{s.source_type === "message" ? `You said: “${s.span}”` : `You wrote: “${s.span}”`}</li>
                      ))}
                    </ul>
                  </details>
                )}
                <MemoryActions memoryId={m.id} text={m.canonical_text} forgotten={m.status === "deleted"} />
              </li>
            ))}
          </ul>
        )}
        {older && (
          <p className="mt-4">
            <Link
              className="underline"
              href={{ pathname: "/memory", query: { ...(q && { q }), ...(type && { type }), before: older } }}
            >
              Older
            </Link>
          </p>
        )}
      </section>

      <section className="mt-10">
        <ClearForm />
      </section>
    </>
  );
}
