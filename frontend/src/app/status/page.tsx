"use client";

import { useCallback, useEffect, useState } from "react";
import { fetchReadiness, type DependencyCheck, type ReadinessResponse } from "@/lib/api";

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

export default function Readiness() {
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
        Operations · readiness
      </p>
      <h1 className="mt-3 font-display text-3xl font-semibold tracking-tight text-ink">
        Secure File Sharing Platform
      </h1>
      <p className="mt-2 max-w-lg text-ink-muted">
        Every dependency below is touched live on each load — nothing here reports
        from cache, and no sign-in is needed to read it.
      </p>

      <section className="mt-10 overflow-hidden rounded-lg border border-border bg-surface">
        <header className="flex items-center justify-between gap-3 border-b border-border px-5 py-3">
          <div className="flex items-center gap-2.5">
            <h2 className="font-display text-sm font-semibold text-ink">
              Service readiness
            </h2>
            {state.kind === "loaded" && <Verdict status={state.data.status} />}
          </div>
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
          <>
            <ul className="divide-y divide-border">
              {Object.entries(state.data.checks).map(([key, check]) => (
                <li
                  key={key}
                  className="flex items-center justify-between gap-4 px-5 py-3"
                >
                  <span className="min-w-0">
                    <span className="block text-sm text-ink">
                      {DEPENDENCY_LABELS[key] ?? key}
                    </span>
                    {describe(key, check) && (
                      <span className="mt-0.5 block truncate font-mono text-xs text-ink-subtle">
                        {describe(key, check)}
                      </span>
                    )}
                  </span>
                  <StatusBadge ok={check.ok} detail={check.detail} />
                </li>
              ))}
            </ul>
            <Scanner scanner={state.data.scanner} />
          </>
        )}
      </section>
    </main>
  );
}

/**
 * The one fact the API sends that this page used to throw away: whether the service
 * as a whole is ready. A reader should not have to scan four rows to learn it.
 */
function Verdict({ status }: { status: ReadinessResponse["status"] }) {
  const ready = status === "ready";
  return (
    <span
      className={`rounded-md px-2 py-0.5 font-mono text-xs ${
        ready
          ? "bg-status-clean-subtle text-status-clean"
          : "bg-status-flagged-subtle text-status-flagged"
      }`}
    >
      {status}
    </span>
  );
}

/** What each check reports about itself, beyond ok/not — worker count, bucket, region. */
function describe(key: string, check: DependencyCheck): string | null {
  if (key === "celery" && check.workers !== undefined) {
    return `${check.workers} worker${check.workers === 1 ? "" : "s"} responding`;
  }
  if (key === "storage" && check.bucket) {
    return [check.provider, check.bucket, check.region].filter(Boolean).join(" · ");
  }
  return null;
}

/**
 * The scan budget, reported below the checks and never as one of them.
 *
 * An exhausted free-tier quota is ordinary operation, not a sick service, so the
 * backend deliberately keeps it out of the readiness verdict (Architecture.md §3a).
 * Showing it here in neutral type keeps that distinction visible instead of letting
 * a spent quota read as an outage.
 */
function Scanner({ scanner }: { scanner: ReadinessResponse["scanner"] }) {
  if (!scanner) return null;
  return (
    <div className="border-t border-border bg-surface-sunken px-5 py-3">
      <div className="flex items-baseline justify-between gap-4">
        <span className="text-sm text-ink">Malware scanning</span>
        <span className="font-mono text-xs text-ink-muted">
          {scanner.enabled ? "enabled" : "disabled"}
        </span>
      </div>
      {scanner.enabled && scanner.day_limit !== undefined && (
        <p className="mt-1 font-mono text-xs text-ink-subtle">
          {scanner.day_used ?? 0}/{scanner.day_limit} scans used today
          {scanner.minute_limit !== undefined &&
            ` · ${scanner.minute_used ?? 0}/${scanner.minute_limit} this minute`}
          {scanner.available === false && " · rate limit reached, queued files wait"}
        </p>
      )}
      <p className="mt-1 text-xs text-ink-subtle">
        A spent allowance delays scans; it does not make the service unready.
      </p>
    </div>
  );
}

function StatusBadge({ ok, detail }: { ok: boolean; detail?: string }) {
  const className = ok
    ? "bg-status-clean-subtle text-status-clean"
    : "bg-status-flagged-subtle text-status-flagged";
  return (
    <span
      className={`shrink-0 rounded-md px-2 py-0.5 font-mono text-xs ${className}`}
      title={detail}
    >
      {ok ? "ok" : (detail ?? "unavailable")}
    </span>
  );
}
