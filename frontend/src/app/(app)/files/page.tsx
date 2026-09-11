"use client";

import { useCallback, useEffect, useRef, useState } from "react";
// Aliased: this page already has an `items` state for the upload queue, and the local
// name would shadow the import silently.
import {
  ApiError,
  folders,
  items as itemApi,
  uploads,
  workspaces,
  type ItemRef,
} from "@/lib/api";
import { formatBytes, formatRelative } from "@/lib/format";
import type {
  FileOut,
  FolderListing,
  FolderOut,
  InFlightUpload,
  Quota,
  WorkspaceRole,
} from "@/lib/types";
import { can, useWorkspace } from "@/lib/workspace-context";
import {
  classify,
  describeFailure,
  matchesInterrupted,
  resumePlan,
  runUpload,
  startPlan,
  type UploadItem,
} from "@/lib/upload-manager";
import { DeleteDialog, MoveDialog, RenameDialog } from "@/components/item-dialogs";
import { PermissionsDialog } from "@/components/permissions-dialog";
import { PermissionMark, RowMenu, type RowAction } from "@/components/row-menu";
import { ScanBadge } from "@/components/scan-badge";
import { ShareDialog } from "@/components/share-dialog";
import { UploadQueue } from "@/components/upload-queue";
import { VersionsDialog } from "@/components/versions-dialog";

/**
 * Which dialog is open, holding the item it acts on. Kept in state so the object stays
 * the same while the dialog is open: the dialogs load their data keyed on it.
 */
type Open =
  | { kind: "share"; file: FileOut }
  | { kind: "versions"; file: FileOut }
  | { kind: "rename"; item: ItemRef }
  | { kind: "move"; item: ItemRef }
  | { kind: "delete"; item: ItemRef }
  | { kind: "permissions"; item: ItemRef }
  | null;

type Undo = { batchId: string; kind: "file" | "folder"; name: string };

export default function FilesPage() {
  // The shell owns the session and the chosen workspace (M7). This page is re-mounted
  // when the workspace changes, so it always starts at that workspace's root.
  const { workspace } = useWorkspace();

  const [folderId, setFolderId] = useState<string | null>(null);
  const [open, setOpen] = useState<Open>(null);
  const [undo, setUndo] = useState<Undo | null>(null);
  const [listing, setListing] = useState<FolderListing | null>(null);
  const [quota, setQuota] = useState<Quota | null>(null);
  const [interrupted, setInterrupted] = useState<InFlightUpload[]>([]);
  const [items, setItems] = useState<UploadItem[]>([]);
  const [dragging, setDragging] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const controllers = useRef(new Map<string, AbortController>());
  const fileInput = useRef<HTMLInputElement>(null);
  const resumeInput = useRef<HTMLInputElement>(null);
  const resumeTarget = useRef<InFlightUpload | null>(null);

  const requestSeq = useRef(0);

  const refresh = useCallback(async () => {
    const seq = ++requestSeq.current;
    const [listed, q, pending] = await Promise.all([
      folders.listing(workspace.id, folderId),
      workspaces.quota(workspace.id),
      uploads.inFlight(workspace.id).catch(() => []),
    ]);
    // A response for a folder the user has already navigated away from is stale.
    if (seq !== requestSeq.current) return;
    setListing(listed);
    setQuota(q);
    setInterrupted(pending);
  }, [workspace, folderId]);

  useEffect(() => {
    // Cancellation matters beyond satisfying the lint rule: switching folder quickly
    // would otherwise let a slower earlier response overwrite the newer listing.
    let cancelled = false;
    void (async () => {
      try {
        await refresh();
      } catch {
        if (!cancelled) setNotice("Could not load this folder.");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [refresh]);

  // --- Upload --------------------------------------------------------------

  const patch = useCallback((id: string, changes: Partial<UploadItem>) => {
    setItems((current) =>
      current.map((item) => (item.id === id ? { ...item, ...changes } : item)),
    );
  }, []);

  const drive = useCallback(
    async (item: UploadItem, resumeVersionId?: string) => {
      const controller = new AbortController();
      controllers.current.set(item.id, controller);
      patch(item.id, { status: "checking", error: undefined, failureKind: undefined });

      try {
        const plan = resumeVersionId
          ? await resumePlan(workspace.id, resumeVersionId)
          : await startPlan(workspace.id, item.file, item.folderId);
        patch(item.id, { versionId: plan.versionId });

        await runUpload(
          workspace.id,
          item,
          plan,
          (changes) => patch(item.id, changes),
          controller.signal,
        );
        await refresh();
      } catch (error) {
        if (controller.signal.aborted) {
          patch(item.id, { status: "cancelled", progress: 0 });
        } else {
          const { kind } = classify(error);
          patch(item.id, {
            status: "failed",
            failureKind: kind,
            error: describeFailure(
              kind,
              error instanceof ApiError ? error.message : undefined,
            ),
          });
          // A permanent failure may have changed the quota picture; show the truth.
          refresh().catch(() => undefined);
        }
      } finally {
        controllers.current.delete(item.id);
      }
    },
    [workspace, patch, refresh],
  );

  /**
   * Quota and the provider ceiling are checked the moment a file is chosen — before a
   * single byte moves (Design.md §5). Discovering this three chunks in is worse.
   */
  const enqueue = useCallback(
    (files: File[]) => {
      if (!quota) return;

      // Account for what is already queued, or five files that each fit individually
      // could still overflow the workspace together.
      let projected = items
        .filter((i) => ["queued", "checking", "uploading", "retrying"].includes(i.status))
        .reduce((sum, i) => sum + (i.size - i.uploadedBytes), 0);

      const created: UploadItem[] = [];
      for (const file of files) {
        const base: UploadItem = {
          id: `${file.name}-${file.size}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
          file,
          name: file.name,
          size: file.size,
          folderId,
          status: "queued",
          progress: 0,
          uploadedBytes: 0,
          partsCompleted: 0,
          partsTotal: 0,
          attempt: 0,
          isResume: false,
        };

        if (file.size > quota.max_file_size_bytes) {
          created.push({
            ...base,
            status: "failed",
            failureKind: "too_large",
            error: `${formatBytes(file.size)} exceeds the ${formatBytes(quota.max_file_size_bytes)} per-file limit.`,
          });
          continue;
        }
        if (projected + file.size > quota.storage_available_bytes) {
          created.push({
            ...base,
            status: "failed",
            failureKind: "quota",
            error: `Needs ${formatBytes(file.size)}; only ${formatBytes(Math.max(quota.storage_available_bytes - projected, 0))} left in this workspace.`,
          });
          continue;
        }
        projected += file.size;
        created.push(base);
      }

      setItems((current) => [...current, ...created]);
      // One rejected file must not stop the others.
      created
        .filter((i) => i.status === "queued")
        .forEach((i) => void drive(i));
    },
    [quota, items, folderId, drive],
  );

  // --- Drag and drop -------------------------------------------------------

  const onDrop = useCallback(
    (event: React.DragEvent) => {
      event.preventDefault();
      setDragging(false);
      setNotice(null);

      const entries = Array.from(event.dataTransfer.items ?? []);
      // Folder drop needs recursive traversal and server-side structure creation; it
      // is deferred to M7. Refusing loudly beats silently uploading nothing.
      const hasDirectory = entries.some(
        (entry) => entry.webkitGetAsEntry?.()?.isDirectory,
      );
      if (hasDirectory) {
        setNotice("Folders cannot be dropped yet — drop individual files for now.");
      }

      const files = Array.from(event.dataTransfer.files ?? []).filter(
        (file) => file.size > 0 || file.type !== "",
      );
      if (files.length > 0) enqueue(files);
    },
    [enqueue],
  );

  // --- Resume --------------------------------------------------------------

  function askForFile(target: InFlightUpload) {
    resumeTarget.current = target;
    resumeInput.current?.click();
  }

  const onResumeFileChosen = useCallback(
    (file: File | undefined) => {
      const target = resumeTarget.current;
      resumeTarget.current = null;
      if (!file || !target) return;

      // Verified on name AND exact byte size: continuing with a different file would
      // splice foreign bytes into an existing object.
      if (!matchesInterrupted(file, target)) {
        setNotice(
          `That is not the same file. Expected "${target.file_name}" at ${formatBytes(target.total_bytes)}.`,
        );
        return;
      }
      setNotice(null);
      const item: UploadItem = {
        id: `resume-${target.version_id}`,
        file,
        name: target.file_name,
        size: target.total_bytes,
        folderId: target.folder_id,
        status: "queued",
        progress: target.uploaded_bytes / Math.max(target.total_bytes, 1),
        uploadedBytes: target.uploaded_bytes,
        partsCompleted: target.parts_completed,
        partsTotal: target.parts_total,
        attempt: 0,
        isResume: true,
      };
      setItems((current) => [...current, item]);
      void drive(item, target.version_id);
    },
    [drive],
  );

  async function discard(target: InFlightUpload) {
    await uploads.cancel(workspace.id, target.version_id).catch(() => undefined);
    await refresh();
  }

  // --- Item actions (M7) ---------------------------------------------------

  async function download(file: FileOut) {
    setNotice(null);
    try {
      const { url } = await itemApi.download(workspace.id, file.id);
      // The signed URL carries an attachment disposition, so this saves the file rather
      // than navigating away from the app.
      window.location.assign(url);
    } catch (error) {
      setNotice(
        error instanceof ApiError ? error.message : "Could not start the download.",
      );
    }
  }

  async function undoDelete(target: Undo) {
    try {
      await itemApi.restore(workspace.id, target.kind, target.batchId);
      setUndo(null);
      await refresh();
    } catch (error) {
      setNotice(error instanceof ApiError ? error.message : "Could not restore it.");
    }
  }

  function afterChange() {
    setOpen(null);
    void refresh();
  }

  // --- Render --------------------------------------------------------------

  const usedFraction = quota
    ? quota.storage_used_bytes / Math.max(quota.storage_quota_bytes, 1)
    : 0;
  // The role HERE: at the workspace root the workspace role, inside a folder whatever
  // §4.1 resolves for it. Adding is offered only where it would be allowed.
  const roleHere: WorkspaceRole = listing?.my_role ?? workspace.role;
  const canAdd = can(roleHere, "editor");

  return (
    <main className="flex flex-1 flex-col">
      {quota && (
        <div className="mt-5">
          <div className="flex items-baseline justify-between text-xs text-ink-muted">
            <span>
              {formatBytes(quota.storage_used_bytes)} of{" "}
              {formatBytes(quota.storage_quota_bytes)} used
            </span>
            <span className="font-mono">
              {formatBytes(quota.storage_available_bytes)} free
            </span>
          </div>
          <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-surface-sunken">
            <div
              className={`h-full rounded-full ${usedFraction > 0.9 ? "bg-status-scanning" : "bg-primary"}`}
              style={{ width: `${Math.min(usedFraction * 100, 100)}%` }}
            />
          </div>
        </div>
      )}

      {interrupted.length > 0 && (
        <section className="mt-6 rounded-lg border border-border bg-surface-sunken p-4">
          <h2 className="font-display text-sm font-semibold text-ink">
            Unfinished uploads
          </h2>
          <p className="mt-1 text-xs text-ink-muted">
            Your browser cannot re-open a file on its own, so choose the same file again
            to carry on from where it stopped.
          </p>
          <ul className="mt-3 flex flex-col gap-2">
            {interrupted.map((entry) => (
              <li
                key={entry.version_id}
                className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-border bg-surface px-3 py-2"
              >
                <span className="min-w-0 flex-1 truncate text-sm text-ink">
                  {entry.file_name}
                </span>
                <span className="font-mono text-xs text-ink-subtle">
                  {formatBytes(entry.uploaded_bytes)} / {formatBytes(entry.total_bytes)} ·{" "}
                  {entry.parts_completed}/{entry.parts_total} parts ·{" "}
                  {formatRelative(entry.started_at)}
                </span>
                <span className="flex gap-1">
                  <button
                    type="button"
                    onClick={() => askForFile(entry)}
                    className="rounded px-2 py-1 text-xs text-primary transition-colors hover:bg-primary-subtle"
                  >
                    Choose file &amp; resume
                  </button>
                  <button
                    type="button"
                    onClick={() => void discard(entry)}
                    className="rounded px-2 py-1 text-xs text-ink-muted transition-colors hover:bg-surface-sunken"
                  >
                    Discard
                  </button>
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {canAdd ? (
      <div
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
        onClick={() => fileInput.current?.click()}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") fileInput.current?.click();
        }}
        className={`mt-6 cursor-pointer rounded-lg border-2 border-dashed px-6 py-10 text-center transition-colors ${
          dragging
            ? "border-primary bg-primary-subtle"
            : "border-border bg-surface hover:border-border-strong"
        }`}
      >
        <p className="text-sm font-medium text-ink">
          Drop files here, or click to choose
        </p>
        <p className="mt-1 text-xs text-ink-muted">
          Uploaded in chunks straight to storage.
          {quota && ` Up to ${formatBytes(quota.max_file_size_bytes)} per file.`}
        </p>
      </div>
      ) : (
        <p className="mt-6 rounded-lg border border-border bg-surface px-4 py-3 text-sm text-ink-muted">
          You can view and download here, but not add to it — your role in this{" "}
          {listing?.folder ? "folder" : "workspace"} is {roleHere}.
        </p>
      )}

      <input
        ref={fileInput}
        type="file"
        multiple
        hidden
        onChange={(e) => {
          enqueue(Array.from(e.target.files ?? []));
          e.target.value = "";
        }}
      />
      <input
        ref={resumeInput}
        type="file"
        hidden
        onChange={(e) => {
          onResumeFileChosen(e.target.files?.[0]);
          e.target.value = "";
        }}
      />

      {notice && (
        <p
          role="status"
          className="mt-3 rounded-md bg-status-scanning-subtle px-3 py-2 text-sm text-status-scanning"
        >
          {notice}
        </p>
      )}

      {undo && (
        <p
          role="status"
          className="mt-3 flex flex-wrap items-center justify-between gap-2 rounded-md border border-border bg-surface px-3 py-2 text-sm text-ink"
        >
          <span>&ldquo;{undo.name}&rdquo; moved to the trash.</span>
          <button
            type="button"
            onClick={() => void undoDelete(undo)}
            className="rounded px-2 py-0.5 text-sm text-primary hover:bg-primary-subtle"
          >
            Undo
          </button>
        </p>
      )}

      <UploadQueue
        items={items}
        onCancel={(id) => controllers.current.get(id)?.abort()}
        onRetry={(id) => {
          const item = items.find((i) => i.id === id);
          if (item) void drive(item, item.versionId);
        }}
        onDismiss={(id) => setItems((c) => c.filter((i) => i.id !== id))}
      />

      <Browser
        listing={listing}
        workspaceRole={workspace.role}
        canAdd={canAdd}
        onOpen={(id) => {
          setUndo(null);
          setFolderId(id);
        }}
        onAction={setOpen}
        onDownload={(file) => void download(file)}
        onNewFolder={async (name) => {
          try {
            await folders.create(workspace.id, name, folderId);
            await refresh();
          } catch (error) {
            setNotice(
              error instanceof ApiError ? error.message : "Could not create folder.",
            );
          }
        }}
      />

      {open?.kind === "share" && (
        <ShareDialog workspaceId={workspace.id} file={open.file} onClose={afterChange} />
      )}
      {open?.kind === "versions" && (
        <VersionsDialog
          workspaceId={workspace.id}
          file={open.file}
          canEdit={can(open.file.my_role ?? workspace.role, "editor")}
          canRelease={can(workspace.role, "owner")}
          onClose={() => setOpen(null)}
          onChanged={() => void refresh()}
        />
      )}
      {open?.kind === "rename" && (
        <RenameDialog
          workspaceId={workspace.id}
          item={open.item}
          onClose={() => setOpen(null)}
          onDone={afterChange}
        />
      )}
      {open?.kind === "move" && (
        <MoveDialog
          workspaceId={workspace.id}
          item={open.item}
          currentFolderId={folderId}
          onClose={() => setOpen(null)}
          onDone={afterChange}
        />
      )}
      {open?.kind === "delete" && (
        <DeleteDialog
          workspaceId={workspace.id}
          item={open.item}
          onClose={() => setOpen(null)}
          onDeleted={(batchId) => {
            const target = open.item;
            setUndo({ batchId, kind: target.kind, name: target.name });
            afterChange();
          }}
        />
      )}
      {open?.kind === "permissions" && (
        <PermissionsDialog
          workspaceId={workspace.id}
          item={open.item}
          onClose={() => setOpen(null)}
          onChanged={() => void refresh()}
        />
      )}
    </main>
  );
}

function Browser({
  listing,
  workspaceRole,
  canAdd,
  onOpen,
  onNewFolder,
  onAction,
  onDownload,
}: {
  listing: FolderListing | null;
  workspaceRole: WorkspaceRole;
  canAdd: boolean;
  onOpen: (id: string | null) => void;
  onNewFolder: (name: string) => void;
  onAction: (open: Open) => void;
  onDownload: (file: FileOut) => void;
}) {
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");

  if (!listing) {
    return <p className="mt-8 text-sm text-ink-muted">Loading folder…</p>;
  }

  const empty = listing.folders.length === 0 && listing.files.length === 0;

  /**
   * Actions are built from the viewer's role on THAT item, not their workspace role: a
   * grant can narrow or widen it per item (Architecture §4.1). Anything their role
   * would not permit is absent rather than shown and then refused (Design.md §5).
   */
  function folderActions(folder: FolderOut): RowAction[] {
    const role = folder.my_role ?? workspaceRole;
    const ref: ItemRef = { kind: "folder", id: folder.id, name: folder.name };
    const actions: RowAction[] = [{ label: "Open", onSelect: () => onOpen(folder.id) }];
    if (can(role, "editor")) {
      actions.push({
        label: "Rename",
        onSelect: () => onAction({ kind: "rename", item: ref }),
      });
      actions.push({ label: "Move", onSelect: () => onAction({ kind: "move", item: ref }) });
    }
    if (can(role, "owner")) {
      actions.push({
        label: "Permissions",
        onSelect: () => onAction({ kind: "permissions", item: ref }),
      });
    }
    if (can(role, "editor")) {
      actions.push({
        label: "Delete",
        danger: true,
        hint: "Everything inside goes to the trash.",
        onSelect: () => onAction({ kind: "delete", item: ref }),
      });
    }
    return actions;
  }

  function fileActions(file: FileOut): RowAction[] {
    const role = file.my_role ?? workspaceRole;
    const ref: ItemRef = { kind: "file", id: file.id, name: file.name };
    const actions: RowAction[] = [];
    if (file.is_downloadable) {
      actions.push({ label: "Download", onSelect: () => onDownload(file) });
    }
    actions.push({
      label: "Versions",
      hint:
        file.scan_status === "flagged" || file.scan_status === "scan_failed"
          ? "Review the scan result here."
          : undefined,
      onSelect: () => onAction({ kind: "versions", file }),
    });
    // Share is offered only where it could succeed: stricter than downloadable, because
    // a released flagged file is downloadable here and still not shareable (§4.2).
    if (
      can(role, "editor") &&
      (file.scan_status === "clean" || file.scan_status === "scan_skipped")
    ) {
      actions.push({ label: "Share", onSelect: () => onAction({ kind: "share", file }) });
    }
    if (can(role, "editor")) {
      actions.push({
        label: "Rename",
        onSelect: () => onAction({ kind: "rename", item: ref }),
      });
      actions.push({ label: "Move", onSelect: () => onAction({ kind: "move", item: ref }) });
    }
    if (can(role, "owner")) {
      actions.push({
        label: "Permissions",
        onSelect: () => onAction({ kind: "permissions", item: ref }),
      });
    }
    if (can(role, "editor")) {
      actions.push({
        label: "Delete",
        danger: true,
        hint: "Goes to the trash with every version.",
        onSelect: () => onAction({ kind: "delete", item: ref }),
      });
    }
    return actions;
  }

  return (
    <section className="mt-8 rounded-lg border border-border bg-surface">
      <header className="flex items-center justify-between gap-3 border-b border-border px-4 py-2.5">
        <nav aria-label="Breadcrumb" className="flex min-w-0 items-center gap-1 text-sm">
          <button
            type="button"
            onClick={() => onOpen(null)}
            className="rounded px-1.5 py-0.5 text-ink-muted transition-colors hover:bg-surface-sunken hover:text-ink"
          >
            Workspace
          </button>
          {listing.breadcrumb.map((entry, index) => (
            <span key={entry.id} className="flex min-w-0 items-center gap-1">
              <span aria-hidden className="text-ink-subtle">
                /
              </span>
              <button
                type="button"
                onClick={() => onOpen(entry.id)}
                className={`truncate rounded px-1.5 py-0.5 transition-colors hover:bg-surface-sunken ${
                  index === listing.breadcrumb.length - 1
                    ? "font-medium text-ink"
                    : "text-ink-muted hover:text-ink"
                }`}
              >
                {entry.name}
              </button>
            </span>
          ))}
        </nav>

        {!canAdd ? null : creating ? (
          <form
            onSubmit={(e) => {
              e.preventDefault();
              if (name.trim()) onNewFolder(name.trim());
              setName("");
              setCreating(false);
            }}
            className="flex shrink-0 gap-1"
          >
            <input
              autoFocus
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Folder name"
              className="w-36 rounded border border-border px-2 py-1 text-sm outline-none focus:border-primary"
            />
            <button type="submit" className="rounded px-2 py-1 text-xs text-primary">
              Create
            </button>
          </form>
        ) : (
          <button
            type="button"
            onClick={() => setCreating(true)}
            className="shrink-0 rounded px-2 py-1 text-xs text-ink-muted transition-colors hover:bg-surface-sunken hover:text-ink"
          >
            New folder
          </button>
        )}
      </header>

      {empty ? (
        <p className="px-4 py-8 text-center text-sm text-ink-muted">Nothing here yet.</p>
      ) : (
        <ul className="divide-y divide-border">
          {listing.folders.map((folder) => (
            <li key={folder.id} className="flex items-center gap-3 px-4 py-2">
              <button
                type="button"
                onClick={() => onOpen(folder.id)}
                className="flex min-w-0 flex-1 items-center gap-3 rounded py-0.5 text-left"
              >
                <FolderIcon />
                <span className="truncate text-sm text-ink hover:underline">
                  {folder.name}
                </span>
              </button>
              <PermissionMark
                myRole={folder.my_role}
                workspaceRole={workspaceRole}
                grants={folder.explicit_grants}
              />
              <RowMenu label={folder.name} actions={folderActions(folder)} />
            </li>
          ))}
          {listing.files.map((file) => (
            <li key={file.id} className="flex items-center gap-3 px-4 py-2">
              <FileIcon />
              {file.is_downloadable ? (
                <button
                  type="button"
                  onClick={() => onDownload(file)}
                  title="Download"
                  className="min-w-0 flex-1 truncate text-left text-sm text-ink hover:underline"
                >
                  {file.name}
                </button>
              ) : (
                <span
                  className="min-w-0 flex-1 truncate text-sm text-ink"
                  title="This file cannot be downloaded in its current scan state."
                >
                  {file.name}
                </span>
              )}
              <PermissionMark
                myRole={file.my_role}
                workspaceRole={workspaceRole}
                shareLinks={file.active_share_links}
                grants={file.explicit_grants}
              />
              <ScanBadge
                status={file.scan_status}
                reason={file.scan_reason}
                overriddenAt={file.scan_override_at}
                overrideReason={file.scan_override_reason}
              />
              <span className="w-20 shrink-0 text-right font-mono text-xs text-ink-subtle">
                {formatBytes(file.size_bytes)}
              </span>
              <RowMenu label={file.name} actions={fileActions(file)} />
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function FolderIcon() {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 16 16"
      fill="none"
      aria-hidden
      className="shrink-0 text-ink-subtle"
    >
      <path
        d="M1.5 4.5A1.5 1.5 0 0 1 3 3h3l1.5 1.5H13A1.5 1.5 0 0 1 14.5 6v5.5A1.5 1.5 0 0 1 13 13H3a1.5 1.5 0 0 1-1.5-1.5v-7Z"
        stroke="currentColor"
        strokeWidth="1.2"
      />
    </svg>
  );
}

function FileIcon() {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 16 16"
      fill="none"
      aria-hidden
      className="shrink-0 text-ink-subtle"
    >
      <path
        d="M4 2h5l3 3v9a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1Z"
        stroke="currentColor"
        strokeWidth="1.2"
      />
      <path d="M9 2v3.5h3.5" stroke="currentColor" strokeWidth="1.2" />
    </svg>
  );
}
