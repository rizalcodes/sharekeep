"use client";

import { useRouter } from "next/navigation";
import Link from "next/link";
import { useState } from "react";
import { ApiError, auth } from "@/lib/api";

type Mode = "login" | "signup";

/**
 * Deliberately plain (Design.md §5). A security product's sign-in screen should look
 * unremarkable and legible rather than designed.
 */
export function AuthForm({ mode }: { mode: Mode }) {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [fullName, setFullName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const isSignup = mode === "signup";

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      if (isSignup) {
        await auth.signup(email, password, fullName);
      } else {
        await auth.login(email, password);
      }
      router.push("/files");
      router.refresh();
    } catch (caught) {
      setError(
        caught instanceof ApiError
          ? caught.message
          : "Could not reach the server. Is the API running?",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="mx-auto flex w-full max-w-sm flex-1 flex-col justify-center px-6 py-16">
      <h1 className="font-display text-2xl font-semibold tracking-tight text-ink">
        {isSignup ? "Create an account" : "Sign in"}
      </h1>
      <p className="mt-1.5 text-sm text-ink-muted">
        {isSignup
          ? "You get a personal workspace straight away."
          : "Secure File Sharing Platform"}
      </p>

      <form onSubmit={submit} className="mt-8 flex flex-col gap-4">
        {isSignup && (
          <Field
            label="Full name"
            value={fullName}
            onChange={setFullName}
            autoComplete="name"
            required
          />
        )}
        <Field
          label="Email"
          type="email"
          value={email}
          onChange={setEmail}
          autoComplete="email"
          required
        />
        <Field
          label="Password"
          type="password"
          value={password}
          onChange={setPassword}
          autoComplete={isSignup ? "new-password" : "current-password"}
          required
          hint={isSignup ? "At least 12 characters." : undefined}
        />

        {error && (
          <p
            role="alert"
            className="rounded-md bg-status-flagged-subtle px-3 py-2 text-sm text-status-flagged"
          >
            {error}
          </p>
        )}

        <button
          type="submit"
          disabled={busy}
          className="mt-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-ink transition-colors hover:bg-primary-hover disabled:opacity-60"
        >
          {busy ? "Working…" : isSignup ? "Create account" : "Sign in"}
        </button>
      </form>

      <p className="mt-6 text-sm text-ink-muted">
        {isSignup ? "Already have an account? " : "No account yet? "}
        <Link
          href={isSignup ? "/login" : "/signup"}
          className="text-primary underline underline-offset-2"
        >
          {isSignup ? "Sign in" : "Create one"}
        </Link>
      </p>
    </main>
  );
}

function Field({
  label,
  value,
  onChange,
  type = "text",
  hint,
  ...rest
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  type?: string;
  hint?: string;
  autoComplete?: string;
  required?: boolean;
}) {
  return (
    <label className="flex flex-col gap-1.5">
      <span className="text-sm font-medium text-ink">{label}</span>
      <input
        {...rest}
        type={type}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="rounded-md border border-border bg-surface px-3 py-2 text-sm text-ink outline-none transition-colors focus:border-primary"
      />
      {hint && <span className="text-xs text-ink-subtle">{hint}</span>}
    </label>
  );
}
