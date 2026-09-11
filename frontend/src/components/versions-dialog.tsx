"use client";

import { useCallback, useEffect, useState } from "react";
import { ApiError, items } from "@/lib/api";
import { formatBytes, formatRelative } from "@/lib/format";
import type { FileOut, VersionOut } from "@/lib/types";
import { Dialog, DialogError, buttonPrimary, buttonQuiet, fieldClass } from "@/components/dialog";
import { ScanBadge } from "@/components/scan-badge";

/**
 * A file's history (Design.md §5, M7): what each version is, who uploaded it, and its
 * own scan result — each version is judged by that, never by the current one's.
 *
 * Restoring says, before it happens, that it creates a new version and counts toward
 * the quota (Architecture §4.9): history is never rewritten. Releasing a flagged
 * version is an owner's act, demands a reason, and is recorded against the version.
 */
export function VersionsDialog({
  workspaceId,
  file,
  canEdit,
  canRelease,
  onClose,
  onChanged,
}: {
  workspaceId: string;
  file: FileOut;
  canEdit: boolean;
  canRelease: boolean;
  onClose: () => void;
  onChanged: () => void;
}) {
  const [versions, setVersions] = useState<VersionOut[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [confirming, setConfirming] = useState<VersionOut | null>(null);
  const [releasing, setReleasing] = useState<VersionOut | null>(null);

  const load = useCallback(async () => {
    const next = await items.versions(workspaceId, file.id);
    setVersions(next);
  }, [workspaceId, file.id]);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const next = await items.versions(workspaceId, file.id);
        if (!cancelled) setVersions(next);
      } catch (caught) {
        if (!cancelled)
          setError(caught instanceof ApiError ? caught.message : "Could not reach the server.");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [workspaceId, file.id]);

  async function download(version: VersionOut) {
    setBusy(version.id);
    setError(null);
    try {
      const { url } = await items.download(
        workspaceId,
        file.id,
        version.is_current ? undefined : version.id,
      );
      window.location.assign(url);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not reach the server.");
    } finally {
      setBusy(null);
    }
  }

  async function restore(version: VersionOut) {
    setBusy(version.id);
    setError(null);
    try {
      await items.rollback(workspaceId, file.id, version.id);
      setConfirming(null);
      await load();
      onChanged();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not reach the server.");
    } finally {
      setBusy(null);
    }
  }

  return (
    <Dialog title="Versions" subtitle={file.name} onClose={onClose} width="max-w-2xl">
      {!versions ? (
        <p className="text-sm text-ink-muted">{error ?? "Loading versions…"}</p>
      ) : (
        <>
          <ul className="divide-y divide-border rounded-md border border-border">
            {versions.map((version) => (
              <li key={version.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2.5">
                <span className="w-10 shrink-0 font-mono text-xs text-ink">
                  v{version.version_number}
                </span>
                <span className="min-w-0 flex-1 text-sm text-ink">
                  <span className="text-ink-muted">
                    {version.restored_from_version_number
                      ? `Restored from version ${version.restored_from_version_number} by `
                      : "Uploaded by "}
                  </span>
                  {version.uploaded_by_name || version.uploaded_by_email || "someone"}
                  <span className="text-ink-subtle"> · {formatRelative(version.created_at)}</span>
                  {version.is_current && (
                    <span className="ml-2 font-mono text-[11px] text-primary">current</span>
                  )}
                </span>
                <ScanBadge
                  status={version.scan_status}
                  overriddenAt={version.scan_override_at}
                  overrideReason={version.scan_override_reason}
                />
                <span className="w-16 text-right font-mono text-xs text-ink-subtle">
                  {formatBytes(version.size_bytes)}
                </span>
                <span className="flex gap-1">
                  {version.is_downloadable && (
                    <button
                      type="button"
                      onClick={() => void download(version)}
                      disabled={busy !== null}
                      className="rounded px-2 py-0.5 text-xs text-primary hover:bg-primary-subtle"
                    >
                      Download
                    </button>
                  )}
                  {canEdit && !version.is_current && version.scan_status !== "flagged" && (
                    <button
                      type="button"
                      onClick={() => setConfirming(version)}
                      disabled={busy !== null}
                      className="rounded px-2 py-0.5 text-xs text-primary hover:bg-primary-subtle"
                    >
                      Restore
                    </button>
                  )}
                  {canRelease &&
                    (version.scan_status === "flagged" || version.scan_status === "scan_failed") &&
                    !version.scan_override_at && (
                      <button
                        type="button"
                        onClick={() => setReleasing(version)}
                        className="rounded px-2 py-0.5 text-xs text-status-flagged hover:bg-status-flagged-subtle"
                      >
                        Release…
                      </button>
                    )}
                </span>
              </li>
            ))}
          </ul>
          <p className="mt-2 text-xs text-ink-subtle">
            Up to 10 versions are kept. Every kept version counts toward the workspace
            quota.
          </p>
          <DialogError message={error} />
        </>
      )}

      {confirming && (
        <div className="mt-4 rounded-md border border-border bg-surface-sunken p-3">
          <p className="text-sm text-ink">
            Restore version {confirming.version_number}?
          </p>
          <p className="mt-1 text-sm text-ink-muted">
            This makes a copy of it the new current version. Nothing is overwritten —
            every version stays in the history — and the copy uses{" "}
            {formatBytes(confirming.size_bytes)} of the workspace quota. Share links will
            serve the restored content.
          </p>
          <div className="mt-3 flex justify-end gap-2">
            <button type="button" onClick={() => setConfirming(null)} className={buttonQuiet}>
              Cancel
            </button>
            <button
              type="button"
              onClick={() => void restore(confirming)}
              disabled={busy !== null}
              className={buttonPrimary}
            >
              {busy ? "Restoring…" : `Restore as version ${Math.max(...(versions ?? []).map((v) => v.version_number)) + 1}`}
            </button>
          </div>
        </div>
      )}

      {releasing && (
        <ReleaseForm
          workspaceId={workspaceId}
          fileId={file.id}
          version={releasing}
          onCancel={() => setReleasing(null)}
          onReleased={async () => {
            setReleasing(null);
            await load();
            onChanged();
          }}
        />
      )}
    </Dialog>
  );
}

/**
 * Releasing a flagged file (Architecture §3d). The badge does not turn green afterwards
 * (Design.md §5): the release grants access, it does not create a clean verdict. And it
 * does not extend to share links (§4.2), which the form says rather than leaves to be
 * discovered.
 */
function ReleaseForm({
  workspaceId,
  fileId,
  version,
  onCancel,
  onReleased,
}: {
  workspaceId: string;
  fileId: string;
  version: VersionOut;
  onCancel: () => void;
  onReleased: () => Promise<void>;
}) {
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await items.release(workspaceId, fileId, version.id, reason.trim());
      await onReleased();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not reach the server.");
      setBusy(false);
    }
  }

  return (
    <form
      onSubmit={submit}
      className="mt-4 rounded-md border border-status-flagged/40 bg-status-flagged-subtle/40 p-3"
    >
      <p className="text-sm font-medium text-ink">
        Release version {version.version_number} despite its scan result?
      </p>
      <p className="mt-1 text-sm text-ink-muted">
        Workspace members will be able to download it. It stays marked as flagged, with
        your name and reason beside it, and it still cannot be shared by link. Only do
        this if you have judged the detection to be a false positive.
      </p>
      <label className="mt-3 flex flex-col gap-1.5">
        <span className="text-sm font-medium text-ink">Reason</span>
        <textarea
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          required
          minLength={10}
          maxLength={1000}
          rows={3}
          className={fieldClass}
        />
        <span className="text-xs text-ink-subtle">
          At least 10 characters. Recorded in the activity log.
        </span>
      </label>
      <DialogError message={error} />
      <div className="mt-3 flex justify-end gap-2">
        <button type="button" onClick={onCancel} className={buttonQuiet}>
          Cancel
        </button>
        <button
          type="submit"
          disabled={busy || reason.trim().length < 10}
          className={buttonPrimary}
        >
          {busy ? "Releasing…" : "Release"}
        </button>
      </div>
    </form>
  );
}
