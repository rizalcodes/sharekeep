"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { ApiError, auth, invitations } from "@/lib/api";
import {
  clearPendingInvitation,
  readPendingInvitation,
  savePendingInvitation,
  takeTokenFromFragment,
} from "@/lib/pending-invitation";
import type { InvitationPreview, Session } from "@/lib/types";

/**
 * Where the invitation email lands (Architecture.md §4.6, Design.md §5).
 *
 * This page did not exist when M6 was first marked done: the email linked here and got
 * a 404. The token arrives in the URL fragment, which no server ever receives, and is
 * scrubbed from the address bar on arrival.
 *
 * Joining is an explicit click, never automatic on load — joining a workspace is a grant
 * of access and should be a decision the person makes, not a side effect of opening an
 * email.
 */
type State =
  | { kind: "loading" }
  | { kind: "missing" }
  | { kind: "invalid"; message: string }
  | { kind: "ready"; token: string; preview: InvitationPreview; session: Session | null }
  | { kind: "accepted"; workspaceName: string };

export default function InvitationPage() {
  const router = useRouter();
  const [state, setState] = useState<State>({ kind: "loading" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    // Each run supersedes the last, so a slow preview for an old token cannot overwrite
    // the answer for a newer one.
    let run = 0;

    async function resolve(token: string | null, id: number) {
      const current = () => !cancelled && id === run;
      if (!token) {
        await Promise.resolve();
        if (current()) setState({ kind: "missing" });
        return;
      }
      try {
        const preview = await invitations.preview(token);
        const session = await auth.me().catch(() => null);
        if (!current()) return;
        savePendingInvitation({
          token,
          email: preview.email,
          workspaceName: preview.workspace_name,
        });
        setState({ kind: "ready", token, preview, session });
      } catch (caught) {
        if (!current()) return;
        clearPendingInvitation();
        setState({
          kind: "invalid",
          message:
            caught instanceof ApiError
              ? caught.message
              : "Could not reach the server.",
        });
      }
    }

    function begin(fallbackToStored: boolean) {
      // Read and scrub the fragment before any await, so the token leaves the address
      // bar as early as possible.
      const fromLink = takeTokenFromFragment();
      const token =
        fromLink ?? (fallbackToStored ? (readPendingInvitation()?.token ?? null) : null);
      void resolve(token, ++run);
    }

    // Pasting the full link into a tab that is already on this page is a fragment-only
    // navigation: the browser does not reload, so without this the page would keep
    // saying "incomplete" beside a perfectly good link. Found by a real-browser test,
    // not by reading the code. (`replaceState` never fires hashchange, so the scrub in
    // takeTokenFromFragment cannot loop back here.)
    function onHashChange() {
      if (!window.location.hash) return;
      setState({ kind: "loading" });
      begin(false);
    }

    begin(true);
    window.addEventListener("hashchange", onHashChange);
    return () => {
      cancelled = true;
      window.removeEventListener("hashchange", onHashChange);
    };
  }, []);

  async function accept(token: string, workspaceName: string) {
    setBusy(true);
    setError(null);
    try {
      await invitations.accept(token);
      clearPendingInvitation();
      setState({ kind: "accepted", workspaceName });
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not reach the server.");
    } finally {
      setBusy(false);
    }
  }

  async function switchAccount() {
    setBusy(true);
    await auth.logout().catch(() => undefined);
    // The token stays in sessionStorage, so the sign-in page brings them straight back.
    router.push("/login");
  }

  return (
    <main className="mx-auto flex w-full max-w-md flex-1 flex-col justify-center px-6 py-16">
      <p className="font-mono text-xs uppercase tracking-widest text-ink-subtle">
        Workspace invitation
      </p>

      {state.kind === "loading" && (
        <p className="mt-4 text-sm text-ink-muted">Checking this invitation…</p>
      )}

      {state.kind === "missing" && (
        <>
          <h1 className="mt-3 font-display text-2xl font-semibold tracking-tight text-ink">
            This link is incomplete
          </h1>
          <p className="mt-2 text-sm text-ink-muted">
            The invitation code was not in the link. Open it again directly from the
            email rather than copying part of it.
          </p>
        </>
      )}

      {state.kind === "invalid" && (
        <>
          <h1 className="mt-3 font-display text-2xl font-semibold tracking-tight text-ink">
            This invitation cannot be used
          </h1>
          <p className="mt-2 text-sm text-ink-muted">{state.message}</p>
          <p className="mt-6 text-xs text-ink-subtle">
            It may have expired, been revoked, or already been accepted. Ask whoever
            invited you to send a new one.
          </p>
        </>
      )}

      {state.kind === "ready" && (
        <ReadyView
          state={state}
          busy={busy}
          error={error}
          onAccept={() => accept(state.token, state.preview.workspace_name)}
          onSwitchAccount={switchAccount}
        />
      )}

      {state.kind === "accepted" && (
        <>
          <h1 className="mt-3 font-display text-2xl font-semibold tracking-tight text-ink">
            You joined {state.workspaceName}
          </h1>
          <p className="mt-2 text-sm text-ink-muted">
            It is now in your workspace switcher on the files page.
          </p>
          <Link
            href="/files"
            className="mt-5 inline-block rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-ink transition-colors hover:bg-primary-hover"
          >
            Go to files
          </Link>
        </>
      )}
    </main>
  );
}

function ReadyView({
  state,
  busy,
  error,
  onAccept,
  onSwitchAccount,
}: {
  state: Extract<State, { kind: "ready" }>;
  busy: boolean;
  error: string | null;
  onAccept: () => void;
  onSwitchAccount: () => void;
}) {
  const { preview, session } = state;
  const signedInAs = session?.user.email ?? null;
  const isInvitee =
    signedInAs !== null && signedInAs.toLowerCase() === preview.email.toLowerCase();

  return (
    <>
      <h1 className="mt-3 font-display text-2xl font-semibold tracking-tight text-ink">
        Join {preview.workspace_name}
      </h1>
      <p className="mt-2 text-sm text-ink-muted">
        You have been invited as <span className="font-medium text-ink">{preview.role}</span>.
        The invitation is for{" "}
        <span className="font-medium text-ink">{preview.email}</span> and works only for
        that address.
      </p>
      <p className="mt-1 text-xs text-ink-subtle">
        Valid until {new Date(preview.expires_at).toLocaleString()}.
      </p>

      {error && (
        <p
          role="alert"
          className="mt-4 rounded-md bg-status-flagged-subtle px-3 py-2 text-sm text-status-flagged"
        >
          {error}
        </p>
      )}

      {isInvitee && (
        <button
          type="button"
          onClick={onAccept}
          disabled={busy}
          className="mt-5 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-ink transition-colors hover:bg-primary-hover disabled:opacity-60"
        >
          {busy ? "Joining…" : `Join ${preview.workspace_name}`}
        </button>
      )}

      {signedInAs !== null && !isInvitee && (
        <div className="mt-5 rounded-md border border-border bg-surface p-4">
          <p className="text-sm text-ink-muted">
            You are signed in as <span className="font-medium text-ink">{signedInAs}</span>
            , but this invitation is for another address. Sign in as{" "}
            <span className="font-medium text-ink">{preview.email}</span> to accept it.
          </p>
          <button
            type="button"
            onClick={onSwitchAccount}
            disabled={busy}
            className="mt-3 rounded-md border border-border px-3 py-1.5 text-sm text-ink transition-colors hover:bg-surface-sunken disabled:opacity-60"
          >
            Sign out and continue
          </button>
        </div>
      )}

      {signedInAs === null && (
        <div className="mt-5 flex flex-col gap-2">
          <Link
            href={preview.requires_signup ? "/signup" : "/login"}
            className="inline-block rounded-md bg-primary px-4 py-2 text-center text-sm font-medium text-primary-ink transition-colors hover:bg-primary-hover"
          >
            {preview.requires_signup
              ? `Create an account for ${preview.email}`
              : `Sign in as ${preview.email}`}
          </Link>
          <p className="text-xs text-ink-subtle">
            {preview.requires_signup
              ? "Creating the account accepts the invitation in the same step."
              : "You will come back here to join after signing in."}
          </p>
        </div>
      )}
    </>
  );
}
