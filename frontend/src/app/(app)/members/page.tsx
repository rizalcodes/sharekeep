"use client";

import { useCallback, useEffect, useState } from "react";
import { ApiError, members } from "@/lib/api";
import { formatRelative } from "@/lib/format";
import type { Invitation, Member, WorkspaceRole } from "@/lib/types";
import { can, useWorkspace } from "@/lib/workspace-context";
import { buttonPrimary, fieldClass } from "@/components/dialog";

/**
 * Who is in the workspace, and who has been invited (Architecture §4.6, M7).
 *
 * Any member may see the list — that is §4.6's first row. Only an owner may change a
 * role, remove someone, or invite, so those controls are absent for everyone else
 * rather than shown and refused (Design.md §5).
 *
 * The invariant that a workspace always has at least one owner is the server's, and it
 * refuses with a reason; this page shows that reason rather than pre-empting it with a
 * disabled control whose tooltip nobody reads.
 */
export default function MembersPage() {
  const { workspace, session } = useWorkspace();
  const isOwner = can(workspace.role, "owner");

  const [people, setPeople] = useState<Member[] | null>(null);
  const [invitations, setInvitations] = useState<Invitation[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  /** An invitation link, shown once: the token is stored hashed (Rules §3). */
  const [issued, setIssued] = useState<{ email: string; url: string } | null>(null);
  const [copied, setCopied] = useState(false);

  const load = useCallback(async () => {
    const [list, invited] = await Promise.all([
      members.list(workspace.id),
      isOwner ? members.invitations(workspace.id) : Promise.resolve([] as Invitation[]),
    ]);
    setPeople(list);
    setInvitations(invited);
  }, [workspace.id, isOwner]);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const [list, invited] = await Promise.all([
          members.list(workspace.id),
          isOwner ? members.invitations(workspace.id) : Promise.resolve([] as Invitation[]),
        ]);
        if (cancelled) return;
        setPeople(list);
        setInvitations(invited);
      } catch (caught) {
        if (!cancelled) setError(describe(caught));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [workspace.id, isOwner]);

  async function run(key: string, action: () => Promise<unknown>) {
    setBusy(key);
    setError(null);
    try {
      await action();
      await load();
    } catch (caught) {
      setError(describe(caught));
    } finally {
      setBusy(null);
    }
  }

  const pending = invitations.filter((i) => i.status === "pending");
  const owners = (people ?? []).filter((p) => p.role === "owner").length;

  return (
    <main className="flex flex-1 flex-col">
      <header>
        <h1 className="font-display text-xl font-semibold tracking-tight text-ink">People</h1>
        <p className="mt-0.5 text-sm text-ink-muted">
          Who can reach {workspace.name}, and what they can do in it.
        </p>
      </header>

      {error && (
        <p
          role="alert"
          className="mt-4 rounded-md bg-status-flagged-subtle px-3 py-2 text-sm text-status-flagged"
        >
          {error}
        </p>
      )}

      <section className="mt-6 rounded-lg border border-border bg-surface">
        <header className="border-b border-border px-4 py-2.5">
          <h2 className="font-display text-sm font-semibold text-ink">Members</h2>
        </header>
        {!people ? (
          <p className="px-4 py-6 text-sm text-ink-muted">Loading…</p>
        ) : (
          <ul className="divide-y divide-border">
            {people.map((person) => {
              const isMe = person.user_id === session.user.id;
              return (
                <li key={person.id} className="flex flex-wrap items-center gap-3 px-4 py-2.5">
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-sm text-ink">
                      {person.full_name || person.email}
                      {isMe && <span className="text-ink-subtle"> · you</span>}
                    </span>
                    {person.full_name && (
                      <span className="block truncate text-xs text-ink-subtle">
                        {person.email}
                      </span>
                    )}
                  </span>
                  <span className="text-xs text-ink-subtle">
                    joined {formatRelative(person.created_at)}
                  </span>
                  {isOwner ? (
                    <select
                      value={person.role}
                      disabled={busy !== null}
                      onChange={(e) =>
                        void run(person.id, () =>
                          members.changeRole(
                            workspace.id,
                            person.id,
                            e.target.value as WorkspaceRole,
                          ),
                        )
                      }
                      className="rounded-md border border-border bg-surface px-2 py-1 text-sm text-ink"
                      aria-label={`Role for ${person.email}`}
                    >
                      <option value="viewer">viewer</option>
                      <option value="editor">editor</option>
                      <option value="owner">owner</option>
                    </select>
                  ) : (
                    <span className="font-mono text-xs text-ink">{person.role}</span>
                  )}
                  {isOwner && (
                    <button
                      type="button"
                      disabled={busy !== null}
                      onClick={() =>
                        void run(person.id, () => members.remove(workspace.id, person.id))
                      }
                      className="rounded px-2 py-1 text-xs text-ink-muted transition-colors hover:bg-surface-sunken hover:text-ink"
                    >
                      Remove
                    </button>
                  )}
                </li>
              );
            })}
          </ul>
        )}
        {isOwner && owners === 1 && (
          <p className="border-t border-border px-4 py-2 text-xs text-ink-subtle">
            This workspace has one owner. The last owner cannot be removed or demoted —
            otherwise nobody could administer it again.
          </p>
        )}
      </section>

      {isOwner && (
        <>
          <section className="mt-6 rounded-lg border border-border bg-surface">
            <header className="border-b border-border px-4 py-2.5">
              <h2 className="font-display text-sm font-semibold text-ink">Invite someone</h2>
            </header>
            <InviteForm
              disabled={busy !== null}
              onInvite={async (email, role) => {
                const created = await members.invite(workspace.id, email, role);
                // The token travels in the fragment, which no server ever logs
                // (Architecture §4.6). Shown once: only its hash is stored.
                setIssued({
                  email,
                  url: `${window.location.origin}/invitations#token=${encodeURIComponent(created.token)}`,
                });
                setCopied(false);
                await load();
              }}
            />
            {issued && (
              <div className="border-t border-border px-4 py-3">
                <p className="text-sm text-ink">
                  Invitation emailed to <span className="font-medium">{issued.email}</span>.
                </p>
                <div className="mt-2 flex flex-wrap items-center gap-2">
                  <input
                    readOnly
                    value={issued.url}
                    onFocus={(e) => e.currentTarget.select()}
                    className="min-w-0 flex-1 rounded-md border border-border bg-surface-sunken px-2 py-1.5 font-mono text-xs text-ink"
                  />
                  <button
                    type="button"
                    onClick={() => {
                      void navigator.clipboard.writeText(issued.url).then(() => setCopied(true));
                    }}
                    className={buttonPrimary}
                  >
                    {copied ? "Copied" : "Copy link"}
                  </button>
                </div>
                <p className="mt-2 text-xs text-ink-subtle">
                  This link is shown once and cannot be recovered — only its hash is
                  stored. Keep it if you need to pass it on yourself; otherwise you can
                  revoke this invitation and send a new one.
                </p>
              </div>
            )}
          </section>

          <section className="mt-6 rounded-lg border border-border bg-surface">
            <header className="border-b border-border px-4 py-2.5">
              <h2 className="font-display text-sm font-semibold text-ink">
                Pending invitations
              </h2>
            </header>
            {pending.length === 0 ? (
              <p className="px-4 py-6 text-sm text-ink-muted">
                Nobody is waiting on an invitation.
              </p>
            ) : (
              <ul className="divide-y divide-border">
                {pending.map((invitation) => (
                  <li
                    key={invitation.id}
                    className="flex flex-wrap items-center gap-3 px-4 py-2.5"
                  >
                    <span className="min-w-0 flex-1 truncate text-sm text-ink">
                      {invitation.email}
                    </span>
                    <span className="font-mono text-xs text-ink">{invitation.role}</span>
                    <span className="text-xs text-ink-subtle">
                      expires {formatRelative(invitation.expires_at)}
                    </span>
                    <button
                      type="button"
                      disabled={busy !== null}
                      onClick={() =>
                        void run(invitation.id, () =>
                          members.revokeInvitation(workspace.id, invitation.id),
                        )
                      }
                      className="rounded px-2 py-1 text-xs text-ink-muted transition-colors hover:bg-surface-sunken hover:text-ink"
                    >
                      Revoke
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </>
      )}

      {!isOwner && (
        <p className="mt-6 max-w-prose text-xs text-ink-subtle">
          Inviting people, changing roles and removing members are an owner&rsquo;s to do.
          You are {workspace.role} here.
        </p>
      )}
    </main>
  );
}

function InviteForm({
  disabled,
  onInvite,
}: {
  disabled: boolean;
  onInvite: (email: string, role: WorkspaceRole) => Promise<void>;
}) {
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<WorkspaceRole>("viewer");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  return (
    <form
      onSubmit={async (event) => {
        event.preventDefault();
        setBusy(true);
        setError(null);
        try {
          await onInvite(email.trim(), role);
          setEmail("");
        } catch (caught) {
          setError(describe(caught));
        } finally {
          setBusy(false);
        }
      }}
      className="flex flex-wrap items-end gap-2 px-4 py-3"
    >
      <label className="flex min-w-56 flex-1 flex-col gap-1">
        <span className="text-xs font-medium text-ink">Email address</span>
        <input
          type="email"
          required
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          className={fieldClass}
        />
      </label>
      <label className="flex flex-col gap-1">
        <span className="text-xs font-medium text-ink">Role</span>
        <select
          value={role}
          onChange={(e) => setRole(e.target.value as WorkspaceRole)}
          className="rounded-md border border-border bg-surface px-2 py-2 text-sm text-ink"
        >
          <option value="viewer">viewer</option>
          <option value="editor">editor</option>
        </select>
      </label>
      <button type="submit" disabled={disabled || busy || !email.trim()} className={buttonPrimary}>
        {busy ? "Inviting…" : "Send invitation"}
      </button>
      {error && (
        <p role="alert" className="w-full text-sm text-status-flagged">
          {error}
        </p>
      )}
      <p className="w-full text-xs text-ink-subtle">
        Ownership is transferred deliberately, not handed out by invitation — invite as
        editor or viewer, then change the role here.
      </p>
    </form>
  );
}

function describe(caught: unknown): string {
  return caught instanceof ApiError ? caught.message : "Could not reach the server.";
}
