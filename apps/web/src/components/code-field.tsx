"use client";

/** The authenticator-code box that guards sensitive actions (approving, connecting accounts). */
export function CodeField({ id = "code" }: { id?: string }) {
  return (
    <label className="flex items-center gap-2 text-sm" htmlFor={id}>
      Authenticator code
      <input
        id={id}
        name="code"
        inputMode="numeric"
        autoComplete="one-time-code"
        pattern="[0-9]{6}"
        maxLength={6}
        required
        className="w-24 rounded border px-2 py-1 font-mono"
      />
    </label>
  );
}
