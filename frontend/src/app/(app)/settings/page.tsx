"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { ApiError, workspaces } from "@/lib/api";
import { formatBytes } from "@/lib/format";
import type { Quota, WorkspaceOut } from "@/lib/types";
import { can, useWorkspace } from "@/lib/workspace-context";
import {
  Dialog,
  DialogError,
  buttonDanger,
  buttonPrimary,
  buttonQuiet,
  fieldClass,
} from "@/components/dialog";

/**
 * Workspace settings (Architecture §4.6, M7).
 *
 * Renaming, the quota and deletion are an owner's. Leaving is anyone's, subject to the
 * invariants the server holds: the last owner cannot leave, and nobody leaves their own
 * personal workspace. Both refusals come back with their reason, which this page shows
 * rather than guessing at in advance.
 *
 * The quota is lower-only, up to the platform ceiling — a limit its own subject could
 * raise at will would not be a limit (decided at M7). The form says so, because a field
 * that silently refuses half its range is worse than one that explains its range.
 */
export default function SettingsPage() {
  const { workspace, refreshSession } = useWorkspace();
  const router = useRouter();
  const isOwner = can(workspace.role, "owner");

  const [detail, setDetail] = useState<WorkspaceOut | null>(null);
  const [quota, setQuota] = useState<Quota | null>(null);
  const [name, setName] = useState(workspace.name);
  const [quotaMb, setQuotaMb] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [confirmLeave, setConfirmLeave] = useState(false);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const [got, q] = await Promise.all([
          workspaces.get(workspace.id),
          workspaces.quota(workspace.id),
        ]);
        if (cancelled) return;
        setDetail(got);
        setQuota(q);
        setName(got.name);
        setQuotaMb(String(Math.round(got.storage_quota_bytes / 1024 / 1024)));
      } catch (caught) {
        if (!cancelled) setError(describe(caught));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [workspace.id]);

  async function save(body: { name?: string; storage_quota_bytes?: number }) {
    setBusy(true);
    setError(null);
    setSaved(null);
    try {
      const updated = await workspaces.update(workspace.id, body);
      setDetail(updated);
      setQuota(await workspaces.quota(workspace.id));
      await refreshSession(workspace.id);
      setSaved("Saved.");
    } catch (caught) {
      setError(describe(caught));
    } finally {
      setBusy(false);
    }
  }

  const usedBytes = quota?.storage_used_bytes ?? 0;

  return (
    <main className="flex flex-1 flex-col">
      <header>
        <h1 className="font-display text-xl font-semibold tracking-tight text-ink">
          Workspace settings
        </h1>
        <p className="mt-0.5 text-sm text-ink-muted">
          {workspace.name}
          {workspace.is_personal && " · your personal workspace"} · you are{" "}
          {workspace.role}
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
      {saved && (
        <p role="status" className="mt-4 text-sm text-status-clean">
          {saved}
        </p>
      )}

      {isOwner && (
        <>
          <section className="mt-6 rounded-lg border border-border bg-surface p-5">
            <h2 className="font-display text-sm font-semibold text-ink">Name</h2>
            <form
              onSubmit={(e) => {
                e.preventDefault();
                void save({ name: name.trim() });
              }}
              className="mt-3 flex flex-wrap items-end gap-2"
            >
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                maxLength={255}
                required
                className={`${fieldClass} max-w-sm flex-1`}
                aria-label="Workspace name"
              />
              <button
                type="submit"
                disabled={busy || !name.trim() || name.trim() === detail?.name}
                className={buttonPrimary}
              >
                Rename
              </button>
            </form>
          </section>

          <section className="mt-6 rounded-lg border border-border bg-surface p-5">
            <h2 className="font-display text-sm font-semibold text-ink">Storage quota</h2>
            <p className="mt-1 text-sm text-ink-muted">
              {formatBytes(usedBytes)} of {formatBytes(detail?.storage_quota_bytes ?? 0)}{" "}
              used.
            </p>
            <form
              onSubmit={(e) => {
                e.preventDefault();
                void save({ storage_quota_bytes: Number(quotaMb) * 1024 * 1024 });
              }}
              className="mt-3 flex flex-wrap items-end gap-2"
            >
              <label className="flex flex-col gap-1">
                <span className="text-xs font-medium text-ink">Quota (MB)</span>
                <input
                  type="number"
                  min={Math.ceil(usedBytes / 1024 / 1024)}
                  value={quotaMb}
                  onChange={(e) => setQuotaMb(e.target.value)}
                  className={`${fieldClass} w-40`}
                />
              </label>
              <button type="submit" disabled={busy} className={buttonPrimary}>
                Set quota
              </button>
            </form>
            <p className="mt-2 max-w-prose text-xs text-ink-subtle">
              You can lower this quota, not raise it beyond what the platform allows —
              every workspace shares the same storage, so a limit an owner could lift at
              will would not be a limit. It also cannot go below what is already stored
              ({formatBytes(usedBytes)}); delete files first. Raising the platform
              allowance is a server setting.
            </p>
          </section>
        </>
      )}

      <section className="mt-6 rounded-lg border border-border bg-surface p-5">
        <h2 className="font-display text-sm font-semibold text-ink">Leave workspace</h2>
        <p className="mt-1 max-w-prose text-sm text-ink-muted">
          You lose access to everything in {workspace.name}. Nothing is deleted, and an
          owner can invite you back.
        </p>
        <button
          type="button"
          onClick={() => setConfirmLeave(true)}
          disabled={busy}
          className={`${buttonQuiet} mt-3 border border-border`}
        >
          Leave {workspace.name}
        </button>
        {workspace.is_personal && (
          <p className="mt-2 text-xs text-ink-subtle">
            This is your personal workspace, so you cannot leave it — it exists for as
            long as your account does.
          </p>
        )}
      </section>

      {isOwner && !workspace.is_personal && (
        <section className="mt-6 rounded-lg border border-status-flagged/40 bg-status-flagged-subtle/30 p-5">
          <h2 className="font-display text-sm font-semibold text-ink">Delete workspace</h2>
          <p className="mt-1 max-w-prose text-sm text-ink-muted">
            Every file, folder, version and share link in {workspace.name} is destroyed,
            for everyone in it. This is <strong>not</strong> the trash: there is no
            restore, and the stored contents are deleted afterwards.
          </p>
          <button
            type="button"
            onClick={() => setConfirmDelete(true)}
            disabled={busy}
            className={`${buttonDanger} mt-3`}
          >
            Delete this workspace
          </button>
        </section>
      )}

      {confirmLeave && (
        <ConfirmDialog
          title={`Leave ${workspace.name}?`}
          body="You will lose access immediately. Nothing is deleted."
          confirmLabel="Leave workspace"
          danger={false}
          onClose={() => setConfirmLeave(false)}
          onConfirm={async () => {
            await workspaces.leave(workspace.id);
            // The session no longer includes it, so the shell picks another workspace.
            await refreshSession();
            router.push("/files");
          }}
        />
      )}

      {confirmDelete && (
        <ConfirmDialog
          title={`Delete ${workspace.name}?`}
          body="This destroys every file and version in it, for everyone in it, permanently. Type the workspace name to confirm."
          confirmLabel="Delete permanently"
          danger
          requirePhrase={workspace.name}
          onClose={() => setConfirmDelete(false)}
          onConfirm={async () => {
            await workspaces.remove(workspace.id);
            await refreshSession();
            router.push("/files");
          }}
        />
      )}
    </main>
  );
}

/**
 * A confirmation that can demand the item's name be typed. Reserved for the
 * irreversible: deleting a workspace has no trash behind it, and a misplaced click
 * should not be enough.
 */
function ConfirmDialog({
  title,
  body,
  confirmLabel,
  danger,
  requirePhrase,
  onClose,
  onConfirm,
}: {
  title: string;
  body: string;
  confirmLabel: string;
  danger: boolean;
  requirePhrase?: string;
  onClose: () => void;
  onConfirm: () => Promise<void>;
}) {
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const ready = !requirePhrase || typed.trim() === requirePhrase;

  return (
    <Dialog title={title} onClose={onClose} width="max-w-md">
      <p className="text-sm text-ink-muted">{body}</p>
      {requirePhrase && (
        <input
          value={typed}
          onChange={(e) => setTyped(e.target.value)}
          placeholder={requirePhrase}
          aria-label={`Type ${requirePhrase} to confirm`}
          className={`${fieldClass} mt-3`}
        />
      )}
      <DialogError message={error} />
      <div className="mt-4 flex justify-end gap-2">
        <button type="button" onClick={onClose} className={buttonQuiet}>
          Cancel
        </button>
        <button
          type="button"
          disabled={busy || !ready}
          onClick={async () => {
            setBusy(true);
            setError(null);
            try {
              await onConfirm();
            } catch (caught) {
              setError(describe(caught));
              setBusy(false);
            }
          }}
          className={danger ? buttonDanger : buttonPrimary}
        >
          {busy ? "Working…" : confirmLabel}
        </button>
      </div>
    </Dialog>
  );
}

function describe(caught: unknown): string {
  return caught instanceof ApiError ? caught.message : "Could not reach the server.";
}
