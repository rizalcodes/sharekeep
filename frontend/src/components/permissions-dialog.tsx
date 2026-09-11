"use client";

import { useCallback, useEffect, useState } from "react";
import { ApiError, items, members, type ItemRef } from "@/lib/api";
import type { Grant, Member, WorkspaceRole } from "@/lib/types";
import { Dialog, DialogError, buttonPrimary, buttonQuiet } from "@/components/dialog";

/**
 * Explicit grants on one file or folder (Architecture §4.1, M7). Owner-only, like the
 * endpoints it calls.
 *
 * Two rules the form states rather than leaves to be discovered, because both surprise
 * people: a grant can make someone's access NARROWER as well as wider — the nearest
 * grant wins even when it is less permissive — and only workspace members can be given
 * one. Removing a grant returns the person to whatever they would otherwise have.
 */
export function PermissionsDialog({
  workspaceId,
  item,
  onClose,
  onChanged,
}: {
  workspaceId: string;
  item: ItemRef;
  onClose: () => void;
  onChanged: () => void;
}) {
  const [grants, setGrants] = useState<Grant[] | null>(null);
  const [people, setPeople] = useState<Member[]>([]);
  const [userId, setUserId] = useState("");
  const [role, setRole] = useState<WorkspaceRole>("viewer");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    const [nextGrants, nextPeople] = await Promise.all([
      items.grants(workspaceId, item),
      members.list(workspaceId),
    ]);
    setGrants(nextGrants);
    setPeople(nextPeople);
  }, [workspaceId, item]);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const [nextGrants, nextPeople] = await Promise.all([
          items.grants(workspaceId, item),
          members.list(workspaceId),
        ]);
        if (cancelled) return;
        setGrants(nextGrants);
        setPeople(nextPeople);
      } catch (caught) {
        if (!cancelled)
          setError(caught instanceof ApiError ? caught.message : "Could not reach the server.");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [workspaceId, item]);

  async function run(action: () => Promise<unknown>) {
    setBusy(true);
    setError(null);
    try {
      await action();
      await load();
      onChanged();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not reach the server.");
    } finally {
      setBusy(false);
    }
  }

  const granted = new Set((grants ?? []).map((g) => g.user_id));
  const candidates = people.filter((p) => !granted.has(p.user_id));
  const roleOf = (id: string) => people.find((p) => p.user_id === id)?.role;

  return (
    <Dialog title="Permissions" subtitle={item.name} onClose={onClose} width="max-w-xl">
      <p className="text-sm text-ink-muted">
        Everyone in the workspace has their workspace role here unless you set something
        different for them. A setting on this {item.kind}
        {item.kind === "folder" ? " applies to everything inside it, and" : ""} overrides
        their workspace role — including making it <em>narrower</em>.
      </p>

      {!grants ? (
        <p className="mt-4 text-sm text-ink-muted">{error ?? "Loading…"}</p>
      ) : grants.length === 0 ? (
        <p className="mt-4 text-sm text-ink-subtle">
          Nobody has a role set specifically on this {item.kind}.
        </p>
      ) : (
        <ul className="mt-4 divide-y divide-border rounded-md border border-border">
          {grants.map((grant) => {
            const workspaceRole = roleOf(grant.user_id);
            const narrower =
              workspaceRole &&
              { viewer: 0, editor: 1, owner: 2 }[grant.role] <
                { viewer: 0, editor: 1, owner: 2 }[workspaceRole];
            return (
              <li key={grant.id} className="flex flex-wrap items-center gap-3 px-3 py-2">
                <span className="min-w-0 flex-1 truncate text-sm text-ink">
                  {grant.email ?? grant.user_id}
                </span>
                <span className="font-mono text-xs text-ink">{grant.role}</span>
                {workspaceRole && (
                  <span className="text-xs text-ink-subtle">
                    {narrower ? "narrower than" : "instead of"} {workspaceRole}
                  </span>
                )}
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void run(() => items.revokeGrant(workspaceId, item, grant.user_id))}
                  className="rounded px-2 py-0.5 text-xs text-ink-muted hover:bg-surface-sunken hover:text-ink"
                >
                  Remove
                </button>
              </li>
            );
          })}
        </ul>
      )}

      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (userId) void run(() => items.grant(workspaceId, item, userId, role)).then(() => setUserId(""));
        }}
        className="mt-4 flex flex-wrap items-end gap-2"
      >
        <label className="flex min-w-48 flex-1 flex-col gap-1">
          <span className="text-xs font-medium text-ink">Member</span>
          <select
            value={userId}
            onChange={(e) => setUserId(e.target.value)}
            className="rounded-md border border-border bg-surface px-2 py-1.5 text-sm text-ink"
          >
            <option value="">Choose a member…</option>
            {candidates.map((p) => (
              <option key={p.user_id} value={p.user_id}>
                {p.full_name ? `${p.full_name} — ${p.email}` : p.email} ({p.role})
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-xs font-medium text-ink">Role here</span>
          <select
            value={role}
            onChange={(e) => setRole(e.target.value as WorkspaceRole)}
            className="rounded-md border border-border bg-surface px-2 py-1.5 text-sm text-ink"
          >
            <option value="viewer">viewer</option>
            <option value="editor">editor</option>
            <option value="owner">owner</option>
          </select>
        </label>
        <button type="submit" disabled={busy || !userId} className={buttonPrimary}>
          Set role
        </button>
      </form>
      <p className="mt-2 text-xs text-ink-subtle">
        Only members appear here. To give someone outside the workspace access, invite
        them from the Members page, or share a link.
      </p>
      <DialogError message={grants ? error : null} />

      <div className="mt-4 flex justify-end">
        <button type="button" onClick={onClose} className={buttonQuiet}>
          Done
        </button>
      </div>
    </Dialog>
  );
}
