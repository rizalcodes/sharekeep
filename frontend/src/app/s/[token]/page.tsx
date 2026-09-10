"use client";

import Link from "next/link";
import { use, useEffect, useState } from "react";
import { ApiError, publicShare } from "@/lib/api";
import { formatBytes } from "@/lib/format";
import type { SharePreview } from "@/lib/types";

/**
 * The public face of a share link. The only page in this app that expects a visitor
 * with no account (Architecture.md §4.2).
 *
 * `params` is a Promise in this version of Next, unwrapped with React's `use` — reading
 * `params.token` directly would silently be undefined.
 *
 * Two deliberate restraints:
 *
 * - A password gate is told to the visitor, but the file's name is not. Sending the link
 *   and the password through separate channels is the point of password protection, so
 *   the link alone must not reveal what the file is.
 * - Every refusal states its own reason. "Not available" would leave someone unable to
 *   tell an expired link from a file withheld pending its malware scan, and only one of
 *   those is worth asking the sender about.
 */
type State =
  | { kind: "loading" }
  | { kind: "ready"; preview: SharePreview }
  | { kind: "denied"; message: string; status: number }
  | { kind: "granted"; url: string; fileName: string; sizeBytes: number };

export default function SharePage({
  params,
}: {
  params: Promise<{ token: string }>;
}) {
  const { token } = use(params);
  const [state, setState] = useState<State>({ kind: "loading" });
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [attemptError, setAttemptError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const preview = await publicShare.preview(token);
        if (!cancelled) setState({ kind: "ready", preview });
      } catch (caught) {
        if (cancelled) return;
        setState(
          caught instanceof ApiError
            ? { kind: "denied", message: caught.message, status: caught.status }
            : {
                kind: "denied",
                message: "Could not reach the server.",
                status: 0,
              },
        );
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [token]);

  async function redeem(event?: React.FormEvent) {
    event?.preventDefault();
    setSubmitting(true);
    setAttemptError(null);
    try {
      const result = await publicShare.redeem(token, password || undefined);
      setState({
        kind: "granted",
        url: result.url,
        fileName: result.file_name,
        sizeBytes: result.size_bytes,
      });
      // Start the download immediately: the signed URL is valid for a minute, and
      // making the visitor click twice spends part of that window for no reason.
      window.location.href = result.url;
    } catch (caught) {
      if (caught instanceof ApiError) {
        // A wrong password keeps the form up; anything else is terminal for this link.
        if (caught.status === 403 && password) setAttemptError(caught.message);
        else if (caught.status === 403 && !password) setAttemptError(caught.message);
        else
          setState({
            kind: "denied",
            message: caught.message,
            status: caught.status,
          });
      } else {
        setAttemptError("Could not reach the server.");
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="mx-auto flex w-full max-w-md flex-1 flex-col justify-center px-6 py-16">
      <p className="font-mono text-xs uppercase tracking-widest text-ink-subtle">
        Shared file
      </p>

      {state.kind === "loading" && (
        <p className="mt-4 text-sm text-ink-muted">Checking this link…</p>
      )}

      {state.kind === "denied" && (
        <>
          <h1 className="mt-3 font-display text-2xl font-semibold tracking-tight text-ink">
            This link cannot be opened
          </h1>
          <p className="mt-2 text-sm text-ink-muted">{state.message}</p>
          {state.status === 401 && (
            <Link
              href="/login"
              className="mt-4 inline-block rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-ink transition-colors hover:bg-primary-hover"
            >
              Sign in
            </Link>
          )}
          <p className="mt-6 text-xs text-ink-subtle">
            If you were expecting this file, ask whoever sent the link for a new one.
          </p>
        </>
      )}

      {state.kind === "ready" && (
        <>
          <h1 className="mt-3 font-display text-2xl font-semibold tracking-tight text-ink">
            {state.preview.file_name ?? "A file has been shared with you"}
          </h1>
          {state.preview.size_bytes !== null && (
            <p className="mt-1 font-mono text-xs text-ink-subtle">
              {formatBytes(state.preview.size_bytes)}
            </p>
          )}
          {state.preview.expires_at && (
            <p className="mt-2 text-xs text-ink-muted">
              This link stops working on{" "}
              {new Date(state.preview.expires_at).toLocaleString()}.
            </p>
          )}

          {state.preview.requires_sign_in ? (
            <>
              <p className="mt-3 text-sm text-ink-muted">
                This link was issued to one specific person. Sign in to the account it
                was sent to.
              </p>
              <Link
                href="/login"
                className="mt-4 inline-block rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-ink transition-colors hover:bg-primary-hover"
              >
                Sign in
              </Link>
            </>
          ) : (
            <form onSubmit={redeem} className="mt-5 flex flex-col gap-3">
              {state.preview.requires_password && (
                <label className="flex flex-col gap-1.5">
                  <span className="text-sm font-medium text-ink">Password</span>
                  <input
                    type="password"
                    required
                    autoComplete="off"
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    className="rounded-md border border-border bg-surface px-3 py-2 text-sm text-ink outline-none focus:border-primary"
                  />
                  <span className="text-xs text-ink-subtle">
                    Sent to you separately from the link. The file&rsquo;s name is
                    withheld until it is correct.
                  </span>
                </label>
              )}

              {attemptError && (
                <p
                  role="alert"
                  className="rounded-md bg-status-flagged-subtle px-3 py-2 text-sm text-status-flagged"
                >
                  {attemptError}
                </p>
              )}

              <button
                type="submit"
                disabled={submitting}
                className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-ink transition-colors hover:bg-primary-hover disabled:opacity-60"
              >
                {submitting ? "Preparing…" : "Download"}
              </button>
            </form>
          )}
        </>
      )}

      {state.kind === "granted" && (
        <>
          <h1 className="mt-3 font-display text-2xl font-semibold tracking-tight text-ink">
            {state.fileName}
          </h1>
          <p className="mt-1 font-mono text-xs text-ink-subtle">
            {formatBytes(state.sizeBytes)}
          </p>
          <p className="mt-3 text-sm text-ink-muted">
            Your download should have started.
          </p>
          <a
            href={state.url}
            className="mt-4 inline-block rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-ink transition-colors hover:bg-primary-hover"
          >
            Download again
          </a>
          <p className="mt-4 text-xs text-ink-subtle">
            This download link is valid for about a minute. Reload the page to get a
            fresh one — that may use another of the link&rsquo;s allowed uses.
          </p>
        </>
      )}
    </main>
  );
}
