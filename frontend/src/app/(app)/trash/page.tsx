"use client";

import { useEffect, useState } from "react";
import { ApiError, items } from "@/lib/api";
import { formatBytes, formatRelative } from "@/lib/format";
import type { TrashEntry } from "@/lib/types";
import { can, useWorkspace } from "@/lib/workspace-context";

/**
 * The trash (Architecture §6b, Design.md §5).
 *
 * One entry per deletion, not per row: restore revives exactly one deletion batch, so a
 * row-per-file list would offer choices the restore cannot honour. Each entry says how
 * much it holds, who deleted it, and when it stops being restorable — and an entry that
 * cannot be restored says why, rather than failing when pressed.
 *
 * Editors and owners only, because only they can restore; the refusal explains that
 * rather than showing an empty list, which would read as "the trash is empty".
 */
export default function TrashPage() {
  const { workspace } = useWorkspace();
  const allowed = can(workspace.role, "editor");

  const [entries, setEntries] = useState<TrashEntry[] | null>(null);
  const [retentionDays, setRetentionDays] = useState(30);
  const [refusal, setRefusal] = useState<{ status: number; message: string } | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const trash = await items.trash(workspace.id);
        if (cancelled) return;
        setEntries(trash.entries);
        setRetentionDays(trash.retention_days);
        setRefusal(null);
      } catch (caught) {
        if (cancelled) return;
        setEntries([]);
        setRefusal(
          caught instanceof ApiError
            ? { status: caught.status, message: caught.message }
            : { status: 0, message: "Could not reach the server." },
        );
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [workspace.id, reload]);

  async function restore(entry: TrashEntry) {
    setBusy(entry.deletion_batch_id);
    setRefusal(null);
    try {
      await items.restore(workspace.id, entry.kind, entry.deletion_batch_id);
      setReload((n) => n + 1);
    } catch (caught) {
      setRefusal(
        caught instanceof ApiError
          ? { status: caught.status, message: caught.message }
          : { status: 0, message: "Could not reach the server." },
      );
    } finally {
      setBusy(null);
    }
  }

  return (
    <main className="flex flex-1 flex-col">
      <header>
        <h1 className="font-display text-xl font-semibold tracking-tight text-ink">Trash</h1>
        <p className="mt-0.5 text-sm text-ink-muted">
          Deleted from {workspace.name} in the last {retentionDays} days.
        </p>
      </header>

      {refusal?.status === 403 && (
        <section className="mt-6 rounded-lg border border-border bg-surface p-5">
          <h2 className="font-display text-base font-semibold text-ink">
            Only editors and owners can see the trash
          </h2>
          <p className="mt-2 text-sm text-ink-muted">
            Restoring something is an edit, so the list is shown to the people who could
            act on it. You are {workspace.role} in {workspace.name}.
          </p>
        </section>
      )}

      {refusal && refusal.status !== 403 && (
        <p
          role="alert"
          className="mt-4 rounded-md bg-status-flagged-subtle px-3 py-2 text-sm text-status-flagged"
        >
          {refusal.message}
        </p>
      )}

      {allowed && refusal?.status !== 403 && (
        <section className="mt-6 rounded-lg border border-border bg-surface">
          {!entries ? (
            <p className="px-4 py-6 text-sm text-ink-muted">Loading…</p>
          ) : entries.length === 0 ? (
            <p className="px-4 py-8 text-center text-sm text-ink-muted">
              Nothing has been deleted recently.
            </p>
          ) : (
            <ul className="divide-y divide-border">
              {entries.map((entry) => (
                <li
                  key={entry.deletion_batch_id}
                  className="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-3"
                >
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-sm text-ink">{entry.name}</span>
                    <span className="block text-xs text-ink-subtle">
                      {entry.kind === "folder"
                        ? `folder · ${entry.folders} folder${entry.folders === 1 ? "" : "s"}, ${entry.files} file${entry.files === 1 ? "" : "s"}`
                        : `file · ${entry.files} item`}
                      {entry.size_bytes > 0 && ` · ${formatBytes(entry.size_bytes)}`}
                      {entry.deleted_by_email && ` · deleted by ${entry.deleted_by_email}`}
                      {` · ${formatRelative(entry.deleted_at)}`}
                    </span>
                  </span>

                  <span
                    className="text-xs text-ink-subtle"
                    title={`Permanently removed after ${new Date(entry.purge_after).toLocaleString()}.`}
                  >
                    gone {formatRelative(entry.purge_after)}
                  </span>

                  {entry.restorable ? (
                    <button
                      type="button"
                      disabled={busy !== null}
                      onClick={() => void restore(entry)}
                      className="rounded px-2 py-1 text-xs text-primary transition-colors hover:bg-primary-subtle disabled:opacity-60"
                    >
                      {busy === entry.deletion_batch_id ? "Restoring…" : "Restore"}
                    </button>
                  ) : (
                    <span
                      className="max-w-64 text-xs text-status-flagged"
                      title={entry.blocked_reason ?? undefined}
                    >
                      {entry.blocked_reason ?? "Cannot be restored."}
                    </span>
                  )}
                </li>
              ))}
            </ul>
          )}
        </section>
      )}

      {allowed && refusal?.status !== 403 && (
        <p className="mt-6 max-w-prose text-xs text-ink-subtle">
          Restoring brings back exactly what one delete removed — not anything deleted
          separately before it, which stays deleted. A restore is refused if the
          workspace no longer has room for it, or if the folder it came from is itself
          deleted; restore that first. After {retentionDays} days the contents are
          permanently removed and an entry disappears from this list.
        </p>
      )}
    </main>
  );
}
