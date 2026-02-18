"use client";

import { useRouter } from "next/navigation";
import { useCallback, useState } from "react";
import { Turnstile } from "@/components/turnstile";
import { createClient } from "@/lib/supabase/client";

export default function LoginPage() {
  const router = useRouter();
  const [captchaToken, setCaptchaToken] = useState<string | null>(null);
  const [captchaResetKey, setCaptchaResetKey] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const onToken = useCallback((token: string | null) => setCaptchaToken(token), []);

  async function signIn(form: FormData) {
    if (!captchaToken) return;
    setPending(true);
    setError(null);
    const { error } = await createClient().auth.signInWithPassword({
      email: String(form.get("email")),
      password: String(form.get("password")),
      options: { captchaToken },
    });
    setCaptchaResetKey((k) => k + 1); // the token is spent either way
    setPending(false);
    if (error) {
      setError(error.message);
      return;
    }
    router.replace("/mfa");
  }

  return (
    <main className="mx-auto mt-24 w-full max-w-sm px-4">
      <h1 className="mb-6 text-2xl font-semibold">Sign in</h1>
      <form action={signIn} className="flex flex-col gap-3">
        <label className="flex flex-col gap-1 text-sm">
          Email
          <input name="email" type="email" required autoComplete="username" className="rounded border px-3 py-2" />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          Password
          <input name="password" type="password" required autoComplete="current-password" className="rounded border px-3 py-2" />
        </label>
        <Turnstile onToken={onToken} resetKey={captchaResetKey} />
        {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
        <button
          type="submit"
          disabled={!captchaToken || pending}
          className="rounded bg-foreground px-3 py-2 text-background disabled:opacity-50"
        >
          {pending ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </main>
  );
}
