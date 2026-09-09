/**
 * Chunked upload driver (Architecture.md §2 and §2a, Design.md §5).
 *
 * Three things this file exists to get right:
 *
 * 1. **Progress is real.** Bytes are counted as parts actually land, not interpolated
 *    from a timer. Design.md §5 forbids a fake indeterminate spinner because large-file
 *    support is a core feature and a lying progress bar undermines it.
 * 2. **Transient and permanent failures are not the same thing.** A dropped connection
 *    retries itself; a refusal (over quota, above the provider ceiling, permission
 *    revoked, part rejected) stops immediately and says why. An upload that can never
 *    succeed must never present as one still trying.
 * 3. **Resume is honest.** The browser cannot re-read a file after a reload, so resume
 *    asks the user to re-select it, verifies name and exact byte size, then skips the
 *    parts the server says already landed.
 */

import { ApiError, NetworkError, uploads } from "@/lib/api";
import type { CompletedPart, UploadPart } from "@/lib/types";

export type UploadStatus =
  | "queued"
  | "checking"
  | "uploading"
  | "retrying"
  | "finalising"
  | "done"
  | "failed"
  | "cancelled";

export type FailureKind =
  | "quota"
  | "too_large"
  | "permission"
  | "conflict"
  | "rejected"
  | "network"
  | "unknown";

export type UploadItem = {
  id: string;
  file: File;
  name: string;
  size: number;
  folderId: string | null;
  status: UploadStatus;
  /** 0-1, derived from bytes that actually landed. */
  progress: number;
  uploadedBytes: number;
  partsCompleted: number;
  partsTotal: number;
  error?: string;
  failureKind?: FailureKind;
  /** Present once init succeeds; lets an interrupted upload be resumed or cancelled. */
  versionId?: string;
  attempt: number;
  isResume: boolean;
};

export const MAX_ATTEMPTS_PER_PART = 4;
export const PARALLEL_PARTS = 3;

/** Which failures are worth retrying, and which are the server's final answer. */
export function classify(error: unknown): { kind: FailureKind; permanent: boolean } {
  if (error instanceof NetworkError) return { kind: "network", permanent: false };
  if (error instanceof ApiError) {
    if (error.status === 413) {
      const overQuota = /space|quota/i.test(error.message);
      return { kind: overQuota ? "quota" : "too_large", permanent: true };
    }
    if (error.status === 403) return { kind: "permission", permanent: true };
    if (error.status === 404) return { kind: "permission", permanent: true };
    if (error.status === 409) return { kind: "conflict", permanent: true };
    if (error.status === 429) return { kind: "network", permanent: false };
    if (error.status >= 500) return { kind: "network", permanent: false };
    return { kind: "unknown", permanent: error.isPermanent };
  }
  if (error instanceof PartRejected) {
    // Storage refused the bytes themselves. A 5xx from storage is worth another go;
    // a 4xx means this part will never be accepted.
    return error.status >= 500
      ? { kind: "network", permanent: false }
      : { kind: "rejected", permanent: true };
  }
  return { kind: "unknown", permanent: false };
}

export class PartRejected extends Error {
  constructor(
    readonly partNumber: number,
    readonly status: number,
  ) {
    super(`Storage rejected part ${partNumber} (HTTP ${status}).`);
    this.name = "PartRejected";
  }
}

export function describeFailure(kind: FailureKind, detail?: string): string {
  switch (kind) {
    case "quota":
      return "Not enough space left in this workspace.";
    case "too_large":
      return "This file is larger than the storage provider allows.";
    case "permission":
      return "You no longer have permission to upload here.";
    case "conflict":
      return detail ?? "Something with that name is already here.";
    case "rejected":
      return "Storage rejected part of this file. The upload cannot continue.";
    case "network":
      return "Lost connection after several attempts.";
    default:
      return detail ?? "Upload failed.";
  }
}

const backoffMs = (attempt: number) =>
  Math.min(1000 * 2 ** (attempt - 1), 8000) + Math.random() * 250;

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** Uploads one part, reporting bytes as they go so progress reflects reality. */
async function putPart(
  part: UploadPart,
  blob: Blob,
  signal: AbortSignal,
): Promise<string> {
  const response = await fetch(part.url, {
    method: "PUT",
    body: blob,
    signal,
  }).catch((error) => {
    if (signal.aborted) throw error;
    throw new NetworkError();
  });

  if (!response.ok) throw new PartRejected(part.part_number, response.status);

  const etag = response.headers.get("ETag");
  if (!etag) {
    // Without ETags the upload cannot be completed at all. Surfaced as permanent
    // because retrying will not conjure the header (this is the CORS
    // expose-headers case, verified working during M0).
    throw new PartRejected(part.part_number, 400);
  }
  return etag;
}

type Progress = (patch: Partial<UploadItem>) => void;

export type UploadPlan = {
  versionId: string;
  parts: UploadPart[];
  completed: CompletedPart[];
  /** Byte offset of each part in the source file, keyed by part number. */
  offsets: Map<number, number>;
  totalParts: number;
  alreadyUploaded: number;
};

/** Byte offsets for a part list, so a resumed upload slices the right ranges. */
export function planOffsets(
  allParts: { part_number: number; size_bytes: number }[],
): Map<number, number> {
  const offsets = new Map<number, number>();
  let cursor = 0;
  for (const part of [...allParts].sort((a, b) => a.part_number - b.part_number)) {
    offsets.set(part.part_number, cursor);
    cursor += part.size_bytes;
  }
  return offsets;
}

/**
 * Runs one upload to completion.
 *
 * Returns normally on success; throws with a classified failure otherwise. Retries
 * happen per part, so a blip on part 7 of 12 does not discard the first six.
 */
export async function runUpload(
  workspaceId: string,
  item: UploadItem,
  plan: UploadPlan,
  onProgress: Progress,
  signal: AbortSignal,
): Promise<void> {
  const completed = new Map<number, string>(
    plan.completed.map((p) => [p.part_number, p.etag]),
  );
  let uploadedBytes = plan.alreadyUploaded;

  const report = (status: UploadStatus = "uploading") =>
    onProgress({
      status,
      uploadedBytes,
      partsCompleted: completed.size,
      partsTotal: plan.totalParts,
      progress: item.size === 0 ? 1 : Math.min(uploadedBytes / item.size, 1),
    });

  report();

  const queue = [...plan.parts].sort((a, b) => a.part_number - b.part_number);
  let cursor = 0;
  let fatal: unknown = null;

  const worker = async () => {
    while (!fatal && !signal.aborted) {
      const index = cursor++;
      if (index >= queue.length) return;
      const part = queue[index];
      const offset = plan.offsets.get(part.part_number) ?? 0;
      const blob = item.file.slice(offset, offset + part.size_bytes);

      for (let attempt = 1; attempt <= MAX_ATTEMPTS_PER_PART; attempt++) {
        if (fatal || signal.aborted) return;
        try {
          const etag = await putPart(part, blob, signal);
          completed.set(part.part_number, etag);
          uploadedBytes += part.size_bytes;
          report();
          break;
        } catch (error) {
          if (signal.aborted) return;
          const { permanent } = classify(error);
          if (permanent || attempt === MAX_ATTEMPTS_PER_PART) {
            fatal = error;
            return;
          }
          onProgress({ status: "retrying", attempt });
          await sleep(backoffMs(attempt));
        }
      }
    }
  };

  await Promise.all(
    Array.from({ length: Math.min(PARALLEL_PARTS, queue.length) }, worker),
  );

  if (signal.aborted) throw new DOMException("Cancelled", "AbortError");
  if (fatal) throw fatal;

  report("finalising");
  await uploads.complete(
    workspaceId,
    plan.versionId,
    [...completed.entries()]
      .map(([part_number, etag]) => ({ part_number, etag }))
      .sort((a, b) => a.part_number - b.part_number),
  );
  onProgress({ status: "done", progress: 1, uploadedBytes: item.size });
}

/** Opens a fresh upload session. */
export async function startPlan(
  workspaceId: string,
  file: File,
  folderId: string | null,
): Promise<UploadPlan> {
  const session = await uploads.init(workspaceId, {
    name: file.name,
    size_bytes: file.size,
    mime_type: file.type || "application/octet-stream",
    folder_id: folderId,
  });
  return {
    versionId: session.version_id,
    parts: session.parts,
    completed: [],
    offsets: planOffsets(session.parts),
    totalParts: session.parts.length,
    alreadyUploaded: 0,
  };
}

/**
 * Rebuilds a plan for an upload interrupted earlier.
 *
 * Offsets are computed from the FULL part list (completed + remaining), not just what
 * is left — slicing the file by the remaining parts alone would upload the wrong byte
 * ranges and silently corrupt the result.
 */
export async function resumePlan(
  workspaceId: string,
  versionId: string,
): Promise<UploadPlan> {
  const state = await uploads.resumeState(workspaceId, versionId);

  // Offsets come from the server's layout, never inferred. If only the final (short)
  // part were outstanding, guessing a chunk size from the remaining parts would give
  // the wrong byte range and corrupt the object without any error surfacing.
  const offsets = new Map<number, number>();
  for (const part of state.layout) {
    offsets.set(part.part_number, part.offset);
  }

  return {
    versionId: state.version_id,
    parts: state.remaining_parts,
    completed: state.completed_parts,
    offsets,
    totalParts: state.layout.length,
    alreadyUploaded: state.uploaded_bytes,
  };
}

/** A re-selected file must be the same file, or resume would corrupt the object. */
export function matchesInterrupted(
  file: File,
  expected: { file_name: string; total_bytes: number },
): boolean {
  return file.name === expected.file_name && file.size === expected.total_bytes;
}
