"use client";

import { useCallback, useEffect, useState } from "react";
import { fetchReadiness, type ReadinessResponse } from "@/lib/api";

const DEPENDENCY_LABELS: Record<string, string> = {
  database: "PostgreSQL",
  redis: "Redis",
  celery: "Celery worker",
  storage: "Object storage",
};

type LoadState =
  | { kind: "loading" }
  | { kind: "loaded"; data: ReadinessResponse }
  | { kind: "unreachable" };

export default function Home() {
  const [state, setState] = useState<LoadState>({ kind: "loading" });

  const fetchInto = useCallback(async (announceLoading: boolean) => {
    if (announceLoading) setState({ kind: "loading" });
    try {
      setState({ kind: "loaded", data: await fetchReadiness() });
    } catch {
      setState({ kind: "unreachable" });
    }
  }, []);

  // Inlined rather than delegated: the lint rule flags any call that *could* set state
  // synchronously, and it is right to. Here every state update provably follows an
  // await, and the cancel flag stops a late response from clobbering a newer one.
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const data = await fetchReadiness();
        if (!cancelled) setState({ kind: "loaded", data });
      } catch {
        if (!cancelled) setState({ kind: "unreachable" });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const load = useCallback(() => void fetchInto(true), [fetchInto]);

  return (
    <main className="mx-auto flex w-full max-w-2xl flex-1 flex-col justify-center px-6 py-16">
      <p className="font-mono text-xs uppercase tracking-widest text-ink-subtle">
        Milestone M0 · scaffold
      </p>
      <h1 className="mt-3 font-display text-3xl font-semibold tracking-tight text-ink">
        Secure File Sharing Platform
      </h1>
      <p className="mt-2 max-w-lg text-ink-muted">
        Backend, worker, and storage wiring. Every dependency below is checked
        live on each load — nothing here reports from cache.
      </p>

      <section className="mt-10 overflow-hidden rounded-lg border border-border bg-surface">
        <header className="flex items-center justify-between border-b border-border px-5 py-3">
          <h2 className="font-display text-sm font-semibold text-ink">
            Service readiness
          </h2>
          <button
            type="button"
            onClick={load}
            className="rounded-md px-2.5 py-1 text-sm text-primary transition-colors hover:bg-primary-subtle"
          >
            Re-check
          </button>
        </header>

        {state.kind === "loading" && (
          <p className="px-5 py-6 text-sm text-ink-muted">Checking…</p>
        )}

        {state.kind === "unreachable" && (
          <p className="px-5 py-6 text-sm text-status-flagged">
            The API is not reachable. Start the backend, then re-check.
          </p>
        )}

        {state.kind === "loaded" && (
          <ul className="divide-y divide-border">
            {Object.entries(state.data.checks).map(([key, check]) => (
              <li
                key={key}
                className="flex items-center justify-between gap-4 px-5 py-3"
              >
                <span className="text-sm text-ink">
                  {DEPENDENCY_LABELS[key] ?? key}
                </span>
                <StatusBadge ok={check.ok} detail={check.detail} />
              </li>
            ))}
          </ul>
        )}
      </section>
    </main>
  );
}

function StatusBadge({ ok, detail }: { ok: boolean; detail?: string }) {
  const className = ok
    ? "bg-status-clean-subtle text-status-clean"
    : "bg-status-flagged-subtle text-status-flagged";
  return (
    <span
      className={`rounded-md px-2 py-0.5 font-mono text-xs ${className}`}
      title={detail}
    >
      {ok ? "ok" : (detail ?? "unavailable")}
    </span>
  );
}
