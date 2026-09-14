"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
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
  foldersToCreate,
  readDrop,
  totalBytes,
  MAX_FILES,
  type DropReading,
} from "@/lib/folder-drop";
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
import { CATEGORY_LABEL, FileTypeIcon, FolderTypeIcon } from "@/components/file-icon";
import { TagList, TagsDialog } from "@/components/tags-dialog";
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
  | { kind: "tags"; file: FileOut }
  | null;

type Undo = { batchId: string; kind: "file" | "folder"; name: string };

/** Table or grid (Design.md §5). A preference, so it is remembered per person. */
type View = "table" | "grid";
const VIEW_KEY = "fileshare.filesView";

export default function FilesPage() {
  // The shell owns the session and the chosen workspace (M7). This page is re-mounted
  // when the workspace changes, so it always starts at that workspace's root.
  const { workspace } = useWorkspace();
  const router = useRouter();

  // A folder result on the search page links here with ?folder=…, so opening one
  // lands in it rather than at the workspace root. Read from `location` rather than
  // useSearchParams, which would put this page behind a Suspense boundary it does not
  // otherwise need.
  const [folderId, setFolderId] = useState<string | null>(null);
  const [open, setOpen] = useState<Open>(null);
  const [undo, setUndo] = useState<Undo | null>(null);
  const [listing, setListing] = useState<FolderListing | null>(null);
  const [quota, setQuota] = useState<Quota | null>(null);
  const [interrupted, setInterrupted] = useState<InFlightUpload[]>([]);
  const [items, setItems] = useState<UploadItem[]>([]);
  const [dragging, setDragging] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [view, setView] = useState<View>("table");

  const controllers = useRef(new Map<string, AbortController>());
  const fileInput = useRef<HTMLInputElement>(null);
  const resumeInput = useRef<HTMLInputElement>(null);
  const resumeTarget = useRef<InFlightUpload | null>(null);

  useEffect(() => {
    let cancelled = false;
    // Deferred a tick: setting state synchronously in an effect body triggers cascading
    // renders, and reading `location` or storage during render would not match the
    // server's first paint.
    void Promise.resolve().then(() => {
      if (cancelled) return;
      const requested = new URLSearchParams(window.location.search).get("folder");
      if (requested) setFolderId(requested);
      try {
        const remembered = window.localStorage.getItem(VIEW_KEY);
        if (remembered === "grid" || remembered === "table") setView(remembered);
      } catch {
        // Storage unavailable: the view lasts for this visit only.
      }
    });
    return () => {
      cancelled = true;
    };
  }, []);

  const chooseView = useCallback((next: View) => {
    setView(next);
    try {
      window.localStorage.setItem(VIEW_KEY, next);
    } catch {
      // Same as above: a preference that cannot be stored is still honoured now.
    }
  }, []);

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
    (files: File[], destination?: string | null) => {
      if (!quota) return;
      // A dropped folder places its files in the folders it just created, not in the
      // one the user happened to be looking at.
      const target = destination === undefined ? folderId : destination;

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
          folderId: target,
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

  /**
   * A dropped folder is uploaded with its structure (Design.md §5, M7c).
   *
   * Folders are created top-down and **merge** into an existing folder of the same
   * name, which is what dropping "Invoices" onto an existing "Invoices" means. A file
   * whose name already exists becomes a new version, exactly as a single re-upload has
   * since M2, so nothing is overwritten.
   */
  const enqueueTree = useCallback(
    async (reading: DropReading) => {
      if (!quota) return;
      const bytes = totalBytes(reading);
      if (bytes > quota.storage_available_bytes) {
        // Checked across the whole tree before anything starts: five files that each
        // fit can still overflow the workspace together (Design.md §5).
        setNotice(
          `That folder needs ${formatBytes(bytes)}, and only ` +
            `${formatBytes(quota.storage_available_bytes)} is left in this workspace. ` +
            "Nothing was uploaded.",
        );
        return;
      }

      const resolved = new Map<string, string | null>([["", folderId]]);
      try {
        for (const path of foldersToCreate(reading)) {
          const key = path.join("/");
          if (resolved.has(key)) continue;
          const parent = resolved.get(path.slice(0, -1).join("/")) ?? null;
          const name = path[path.length - 1];
          try {
            const created = await folders.create(workspace.id, name, parent);
            resolved.set(key, created.id);
          } catch (error) {
            // 409 means a folder of that name is already here: merge into it. The
            // server may have cleaned the name, so the match is made against what it
            // actually stored rather than against the name from the local disk.
            if (!(error instanceof ApiError) || error.status !== 409) throw error;
            const siblings = await folders.listing(workspace.id, parent);
            const existing = siblings.folders.find(
              (candidate) =>
                candidate.name.toLowerCase() === name.trim().toLowerCase(),
            );
            if (!existing) throw error;
            resolved.set(key, existing.id);
          }
        }
      } catch (error) {
        setNotice(
          error instanceof ApiError
            ? `Could not recreate the folder structure: ${error.message}`
            : "Could not recreate the folder structure.",
        );
        return;
      }

      await refresh();
      for (const item of reading.files) {
        const destination = resolved.get(item.path.join("/")) ?? folderId;
        enqueue([item.file], destination);
      }
      if (reading.truncated) {
        setNotice(
          `That folder holds more than ${MAX_FILES} files, so only the first ` +
            `${MAX_FILES} were queued. Drop the rest in smaller batches.`,
        );
      }
    },
    [quota, folderId, workspace.id, enqueue, refresh],
  );

  const onDrop = useCallback(
    (event: React.DragEvent) => {
      event.preventDefault();
      setDragging(false);
      setNotice(null);

      // `dataTransfer.items` is emptied the moment this handler returns, so the entries
      // are taken now and walked afterwards.
      const entries = Array.from(event.dataTransfer.items ?? []).map(
        (item) => item.webkitGetAsEntry?.() ?? null,
      );
      const plain = Array.from(event.dataTransfer.files ?? []).filter(
        (file) => file.size > 0 || file.type !== "",
      );

      if (entries.some((entry) => entry?.isDirectory)) {
        void (async () => {
          const reading = await readDrop(entries);
          if (reading.files.length === 0) {
            setNotice("That folder is empty, so there was nothing to upload.");
            return;
          }
          await enqueueTree(reading);
        })();
        return;
      }

      if (plain.length > 0) enqueue(plain);
    },
    [enqueue, enqueueTree],
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
          Drop files or a folder here, or click to choose
        </p>
        <p className="mt-1 text-xs text-ink-muted">
          Uploaded in chunks straight to storage. A dropped folder keeps its structure.
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
        view={view}
        onView={chooseView}
        onOpen={(id) => {
          setUndo(null);
          setFolderId(id);
        }}
        onAction={setOpen}
        onDownload={(file) => void download(file)}
        onSearchTag={(tag) => router.push(`/search?tag=${encodeURIComponent(tag)}`)}
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
      {open?.kind === "tags" && (
        <TagsDialog
          workspaceId={workspace.id}
          item={{ kind: "file", id: open.file.id, name: open.file.name }}
          initial={open.file.tags}
          onClose={() => setOpen(null)}
          onSaved={afterChange}
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
  view,
  onView,
  onOpen,
  onNewFolder,
  onAction,
  onDownload,
  onSearchTag,
}: {
  listing: FolderListing | null;
  workspaceRole: WorkspaceRole;
  canAdd: boolean;
  view: View;
  onView: (view: View) => void;
  onOpen: (id: string | null) => void;
  onNewFolder: (name: string) => void;
  onAction: (open: Open) => void;
  onDownload: (file: FileOut) => void;
  onSearchTag: (tag: string) => void;
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
        label: "Tags",
        hint: "Used by search.",
        onSelect: () => onAction({ kind: "tags", file }),
      });
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

  /** The name, as a download button when that is possible and plain text when not. */
  function fileName(file: FileOut, className: string) {
    return file.is_downloadable ? (
      <button
        type="button"
        onClick={() => onDownload(file)}
        title="Download"
        className={`${className} text-left hover:underline`}
      >
        {file.name}
      </button>
    ) : (
      <span
        className={className}
        title="This file cannot be downloaded in its current scan state."
      >
        {file.name}
      </span>
    );
  }

  return (
    <section className="mt-8 rounded-lg border border-border bg-surface">
      <header className="flex flex-wrap items-center justify-between gap-3 border-b border-border px-4 py-2.5">
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

        <div className="flex shrink-0 items-center gap-2">
          {/* Table or grid (Design.md §5). The grid is a density choice, not a gallery:
              there are no thumbnails, because previews would mean reading file contents
              this product never reads. */}
          <div role="group" aria-label="View" className="flex rounded-md border border-border">
            {(["table", "grid"] as const).map((option) => (
              <button
                key={option}
                type="button"
                aria-pressed={view === option}
                onClick={() => onView(option)}
                className={`px-2 py-1 text-xs capitalize transition-colors first:rounded-l-md last:rounded-r-md ${
                  view === option
                    ? "bg-primary-subtle font-medium text-primary"
                    : "text-ink-muted hover:bg-surface-sunken hover:text-ink"
                }`}
              >
                {option}
              </button>
            ))}
          </div>

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
        </div>
      </header>

      {empty ? (
        <p className="px-4 py-8 text-center text-sm text-ink-muted">Nothing here yet.</p>
      ) : view === "table" ? (
        <ul className="divide-y divide-border">
          {listing.folders.map((folder) => (
            <li key={folder.id} className="flex items-center gap-3 px-4 py-2">
              <button
                type="button"
                onClick={() => onOpen(folder.id)}
                className="flex min-w-0 flex-1 items-center gap-3 rounded py-0.5 text-left"
              >
                <FolderTypeIcon />
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
              <FileTypeIcon category={file.category} />
              {fileName(file, "min-w-0 flex-1 truncate text-sm text-ink")}
              <TagList tags={file.tags} onSelect={onSearchTag} />
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
      ) : (
        <ul className="grid grid-cols-1 gap-3 p-4 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4">
          {listing.folders.map((folder) => (
            <li
              key={folder.id}
              className="flex flex-col gap-2 rounded-md border border-border p-3"
            >
              <div className="flex items-start justify-between gap-2">
                <FolderTypeIcon />
                <RowMenu label={folder.name} actions={folderActions(folder)} />
              </div>
              <button
                type="button"
                onClick={() => onOpen(folder.id)}
                className="truncate text-left text-sm text-ink hover:underline"
                title={folder.name}
              >
                {folder.name}
              </button>
              <div className="flex items-center justify-between gap-2">
                <span className="text-xs text-ink-subtle">Folder</span>
                <PermissionMark
                  myRole={folder.my_role}
                  workspaceRole={workspaceRole}
                  grants={folder.explicit_grants}
                />
              </div>
            </li>
          ))}
          {listing.files.map((file) => (
            <li
              key={file.id}
              className="flex flex-col gap-2 rounded-md border border-border p-3"
            >
              <div className="flex items-start justify-between gap-2">
                <FileTypeIcon category={file.category} />
                <RowMenu label={file.name} actions={fileActions(file)} />
              </div>
              {fileName(file, "truncate text-sm text-ink")}
              <div className="flex items-center justify-between gap-2">
                <span className="truncate font-mono text-xs text-ink-subtle">
                  {CATEGORY_LABEL[file.category ?? "other"]} · {formatBytes(file.size_bytes)}
                </span>
                <PermissionMark
                  myRole={file.my_role}
                  workspaceRole={workspaceRole}
                  shareLinks={file.active_share_links}
                  grants={file.explicit_grants}
                />
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <ScanBadge
                  status={file.scan_status}
                  reason={file.scan_reason}
                  overriddenAt={file.scan_override_at}
                  overrideReason={file.scan_override_reason}
                />
                <TagList tags={file.tags} onSelect={onSearchTag} max={2} />
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
