"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ApiError, auth, folders, uploads, workspaces } from "@/lib/api";
import { formatBytes, formatRelative } from "@/lib/format";
import type {
  FileOut,
  FolderListing,
  InFlightUpload,
  Quota,
  Session,
  WorkspaceSummary,
} from "@/lib/types";
import {
  classify,
  describeFailure,
  matchesInterrupted,
  resumePlan,
  runUpload,
  startPlan,
  type UploadItem,
} from "@/lib/upload-manager";
import { ScanBadge } from "@/components/scan-badge";
import { ShareDialog } from "@/components/share-dialog";
import { UploadQueue } from "@/components/upload-queue";

export default function FilesPage() {
  const router = useRouter();

  const [session, setSession] = useState<Session | null>(null);
  const [workspace, setWorkspace] = useState<WorkspaceSummary | null>(null);
  const [folderId, setFolderId] = useState<string | null>(null);
  const [sharing, setSharing] = useState<FileOut | null>(null);
  const [listing, setListing] = useState<FolderListing | null>(null);
  const [quota, setQuota] = useState<Quota | null>(null);
  const [interrupted, setInterrupted] = useState<InFlightUpload[]>([]);
  const [items, setItems] = useState<UploadItem[]>([]);
  const [dragging, setDragging] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const controllers = useRef(new Map<string, AbortController>());
  const fileInput = useRef<HTMLInputElement>(null);
  const resumeInput = useRef<HTMLInputElement>(null);
  const resumeTarget = useRef<InFlightUpload | null>(null);

  // --- Session -------------------------------------------------------------

  useEffect(() => {
    auth
      .me()
      .then((s) => {
        setSession(s);
        setWorkspace(s.workspaces[0] ?? null);
      })
      .catch(() => router.replace("/login"))
      .finally(() => setLoading(false));
  }, [router]);

  const requestSeq = useRef(0);

  const refresh = useCallback(async () => {
    if (!workspace) return;
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
      if (!workspace) return;
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
      if (!workspace || !quota) return;

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
    [workspace, quota, items, folderId, drive],
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
    if (!workspace) return;
    await uploads.cancel(workspace.id, target.version_id).catch(() => undefined);
    await refresh();
  }

  // --- Render --------------------------------------------------------------

  if (loading) {
    return <main className="p-8 text-sm text-ink-muted">Loading…</main>;
  }
  if (!session || !workspace) {
    return <main className="p-8 text-sm text-ink-muted">No workspace available.</main>;
  }

  const usedFraction = quota
    ? quota.storage_used_bytes / Math.max(quota.storage_quota_bytes, 1)
    : 0;

  return (
    <main className="mx-auto w-full max-w-4xl flex-1 px-6 py-10">
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="font-display text-xl font-semibold tracking-tight text-ink">
            {workspace.name}
          </h1>
          <p className="mt-0.5 text-sm text-ink-muted">
            {session.user.email} · {workspace.role}
          </p>
        </div>
        <div className="flex items-center gap-3">
          {session.workspaces.length > 1 && (
            <select
              value={workspace.id}
              onChange={(e) => {
                const next = session.workspaces.find((w) => w.id === e.target.value);
                if (next) {
                  setWorkspace(next);
                  setFolderId(null);
                }
              }}
              className="rounded-md border border-border bg-surface px-2 py-1.5 text-sm text-ink"
            >
              {session.workspaces.map((w) => (
                <option key={w.id} value={w.id}>
                  {w.name}
                </option>
              ))}
            </select>
          )}
          {/* Owners only: the log is owner-only server-side, so offering the link to
              an editor would be an invitation to a refusal. Anyone who arrives by URL
              is told why rather than shown a blank page (Design.md §5). */}
          {workspace.role === "owner" && (
            <Link
              href="/activity"
              className="rounded-md px-2.5 py-1.5 text-sm text-ink-muted transition-colors hover:bg-surface-sunken"
            >
              Activity
            </Link>
          )}
          <button
            type="button"
            onClick={async () => {
              await auth.logout().catch(() => undefined);
              router.replace("/login");
            }}
            className="rounded-md px-2.5 py-1.5 text-sm text-ink-muted transition-colors hover:bg-surface-sunken"
          >
            Sign out
          </button>
        </div>
      </header>

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
        onOpen={setFolderId}
        onShare={setSharing}
        onNewFolder={async (name) => {
          if (!workspace) return;
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

      {sharing && workspace && (
        <ShareDialog
          workspaceId={workspace.id}
          file={sharing}
          onClose={() => {
            setSharing(null);
            void refresh();
          }}
        />
      )}
    </main>
  );
}

function Browser({
  listing,
  onOpen,
  onNewFolder,
  onShare,
}: {
  listing: FolderListing | null;
  onOpen: (id: string | null) => void;
  onNewFolder: (name: string) => void;
  onShare: (file: FileOut) => void;
}) {
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");

  if (!listing) {
    return <p className="mt-8 text-sm text-ink-muted">Loading folder…</p>;
  }

  const empty = listing.folders.length === 0 && listing.files.length === 0;

  return (
    <section className="mt-8 overflow-hidden rounded-lg border border-border bg-surface">
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

        {creating ? (
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
        <p className="px-4 py-8 text-center text-sm text-ink-muted">
          Nothing here yet.
        </p>
      ) : (
        <ul className="divide-y divide-border">
          {listing.folders.map((folder) => (
            <li key={folder.id}>
              <button
                type="button"
                onClick={() => onOpen(folder.id)}
                className="flex w-full items-center gap-3 px-4 py-2.5 text-left transition-colors hover:bg-surface-sunken"
              >
                <FolderIcon />
                <span className="truncate text-sm text-ink">{folder.name}</span>
              </button>
            </li>
          ))}
          {listing.files.map((file) => (
            <li
              key={file.id}
              className="flex items-center gap-3 px-4 py-2.5"
              title={
                file.is_downloadable
                  ? undefined
                  : "This file cannot be downloaded in its current scan state."
              }
            >
              <FileIcon />
              <span className="min-w-0 flex-1 truncate text-sm text-ink">
                {file.name}
              </span>
              <ScanBadge
                status={file.scan_status}
                reason={file.scan_reason}
                overriddenAt={file.scan_override_at}
                overrideReason={file.scan_override_reason}
              />
              <span className="w-20 shrink-0 text-right font-mono text-xs text-ink-subtle">
                {formatBytes(file.size_bytes)}
              </span>
              {/* Offered only when the file could actually be shared. A share button
                  that always opens a dialog which then refuses is worse than no button:
                  the reason belongs where the decision is made. Note this is stricter
                  than downloadable — a released flagged file is downloadable here and
                  still not shareable (Architecture.md §4.2). */}
              {file.scan_status === "clean" || file.scan_status === "scan_skipped" ? (
                <button
                  type="button"
                  onClick={() => onShare(file)}
                  className="shrink-0 rounded px-2 py-0.5 text-xs text-primary transition-colors hover:bg-primary-subtle"
                >
                  Share
                </button>
              ) : (
                <span
                  className="shrink-0 px-2 py-0.5 text-xs text-ink-subtle"
                  title="This file cannot be shared in its current scan state."
                >
                  &mdash;
                </span>
              )}
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
