"use client";

import { useEffect, useState } from "react";
import { ApiError, folders, items, type ItemRef } from "@/lib/api";
import type { FolderListing } from "@/lib/types";
import {
  Dialog,
  DialogError,
  buttonDanger,
  buttonPrimary,
  buttonQuiet,
  fieldClass,
} from "@/components/dialog";

const message = (caught: unknown) =>
  caught instanceof ApiError ? caught.message : "Could not reach the server.";

/** Rename a file or folder. The server re-checks the role and the name's uniqueness. */
export function RenameDialog({
  workspaceId,
  item,
  onClose,
  onDone,
}: {
  workspaceId: string;
  item: ItemRef;
  onClose: () => void;
  onDone: () => void;
}) {
  const [name, setName] = useState(item.name);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (item.kind === "file") await items.updateFile(workspaceId, item.id, { name });
      else await items.updateFolder(workspaceId, item.id, { name });
      onDone();
    } catch (caught) {
      setError(message(caught));
      setBusy(false);
    }
  }

  return (
    <Dialog title={`Rename ${item.kind}`} subtitle={item.name} onClose={onClose} width="max-w-sm">
      <form onSubmit={submit} className="flex flex-col gap-3">
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          required
          maxLength={255}
          className={fieldClass}
          aria-label="New name"
        />
        {item.kind === "file" && (
          <p className="text-xs text-ink-subtle">
            Renaming keeps every version. Share links keep working, because they point at
            the file, not its name.
          </p>
        )}
        <DialogError message={error} />
        <div className="flex justify-end gap-2">
          <button type="button" onClick={onClose} className={buttonQuiet}>
            Cancel
          </button>
          <button
            type="submit"
            disabled={busy || !name.trim() || name.trim() === item.name}
            className={buttonPrimary}
          >
            {busy ? "Renaming…" : "Rename"}
          </button>
        </div>
      </form>
    </Dialog>
  );
}

/**
 * Move a file or folder by browsing to the destination. A folder cannot be moved into
 * itself or anything inside it — the server refuses that (it would create a cycle the
 * permission walk could never finish, Architecture §4.1), and the picker simply never
 * lets you navigate into the folder being moved.
 */
export function MoveDialog({
  workspaceId,
  item,
  currentFolderId,
  onClose,
  onDone,
}: {
  workspaceId: string;
  item: ItemRef;
  currentFolderId: string | null;
  onClose: () => void;
  onDone: () => void;
}) {
  const [at, setAt] = useState<string | null>(null);
  const [listing, setListing] = useState<FolderListing | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const next = await folders.listing(workspaceId, at);
        if (!cancelled) setListing(next);
      } catch (caught) {
        if (!cancelled) setError(message(caught));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [workspaceId, at]);

  const destinationIsCurrent = at === currentFolderId;

  async function moveHere() {
    setBusy(true);
    setError(null);
    try {
      if (item.kind === "file") {
        await items.updateFile(workspaceId, item.id, { folder_id: at, move: true });
      } else {
        await items.updateFolder(workspaceId, item.id, { parent_folder_id: at, move: true });
      }
      onDone();
    } catch (caught) {
      setError(message(caught));
      setBusy(false);
    }
  }

  const where = listing?.breadcrumb.at(-1)?.name ?? "Workspace";

  return (
    <Dialog title={`Move ${item.kind}`} subtitle={item.name} onClose={onClose}>
      <nav aria-label="Destination" className="flex flex-wrap items-center gap-1 text-sm">
        <button
          type="button"
          onClick={() => setAt(null)}
          className="rounded px-1.5 py-0.5 text-ink-muted hover:bg-surface-sunken hover:text-ink"
        >
          Workspace
        </button>
        {listing?.breadcrumb.map((entry) => (
          <span key={entry.id} className="flex items-center gap-1">
            <span aria-hidden className="text-ink-subtle">
              /
            </span>
            <button
              type="button"
              onClick={() => setAt(entry.id)}
              className="rounded px-1.5 py-0.5 text-ink-muted hover:bg-surface-sunken hover:text-ink"
            >
              {entry.name}
            </button>
          </span>
        ))}
      </nav>

      <ul className="mt-3 max-h-64 overflow-y-auto rounded-md border border-border">
        {!listing ? (
          <li className="px-3 py-3 text-sm text-ink-muted">Loading…</li>
        ) : listing.folders.filter((f) => f.id !== item.id).length === 0 ? (
          <li className="px-3 py-3 text-sm text-ink-muted">No folders inside {where}.</li>
        ) : (
          listing.folders
            .filter((f) => f.id !== item.id)
            .map((folder) => (
              <li key={folder.id} className="border-b border-border last:border-b-0">
                <button
                  type="button"
                  onClick={() => setAt(folder.id)}
                  className="w-full px-3 py-2 text-left text-sm text-ink hover:bg-surface-sunken"
                >
                  {folder.name}
                </button>
              </li>
            ))
        )}
      </ul>

      <DialogError message={error} />
      <div className="mt-4 flex items-center justify-between gap-2">
        <p className="text-xs text-ink-subtle">
          {destinationIsCurrent ? "It is already here." : `Move into ${where}.`}
        </p>
        <div className="flex gap-2">
          <button type="button" onClick={onClose} className={buttonQuiet}>
            Cancel
          </button>
          <button
            type="button"
            onClick={() => void moveHere()}
            disabled={busy || destinationIsCurrent}
            className={buttonPrimary}
          >
            {busy ? "Moving…" : "Move here"}
          </button>
        </div>
      </div>
    </Dialog>
  );
}

/**
 * Delete says what will happen and whether it can be undone (Design.md §5): it goes to
 * the trash, where it can be restored for the retention period, and then it is gone.
 */
export function DeleteDialog({
  workspaceId,
  item,
  retentionDays = 30,
  onClose,
  onDeleted,
}: {
  workspaceId: string;
  item: ItemRef;
  retentionDays?: number;
  onClose: () => void;
  onDeleted: (batchId: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function confirm() {
    setBusy(true);
    setError(null);
    try {
      const receipt =
        item.kind === "file"
          ? await items.deleteFile(workspaceId, item.id)
          : await items.deleteFolder(workspaceId, item.id);
      onDeleted(receipt.deletion_batch_id);
    } catch (caught) {
      setError(message(caught));
      setBusy(false);
    }
  }

  return (
    <Dialog title={`Delete ${item.kind}`} subtitle={item.name} onClose={onClose} width="max-w-md">
      <p className="text-sm text-ink">
        {item.kind === "folder"
          ? "The folder and everything inside it go to the trash, together."
          : "The file and all of its versions go to the trash."}
      </p>
      <p className="mt-2 text-sm text-ink-muted">
        It can be restored from the trash for {retentionDays} days. After that it is
        permanently removed. Any share links stop working now.
      </p>
      <DialogError message={error} />
      <div className="mt-4 flex justify-end gap-2">
        <button type="button" onClick={onClose} className={buttonQuiet}>
          Cancel
        </button>
        <button type="button" onClick={() => void confirm()} disabled={busy} className={buttonDanger}>
          {busy ? "Deleting…" : "Move to trash"}
        </button>
      </div>
    </Dialog>
  );
}
