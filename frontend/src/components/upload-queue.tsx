"use client";

import { formatBytes, formatPercent } from "@/lib/format";
import type { UploadItem } from "@/lib/upload-manager";

/**
 * The upload queue (Design.md §5).
 *
 * Progress is a real fraction of bytes that landed, never an indeterminate spinner.
 * Terminal failures are visually distinct from ones still being retried, because an
 * upload that will never succeed must not present as one still trying.
 */
export function UploadQueue({
  items,
  onCancel,
  onRetry,
  onDismiss,
}: {
  items: UploadItem[];
  onCancel: (id: string) => void;
  onRetry: (id: string) => void;
  onDismiss: (id: string) => void;
}) {
  if (items.length === 0) return null;

  const active = items.filter(
    (i) => !["done", "failed", "cancelled"].includes(i.status),
  ).length;

  return (
    <section className="mt-6 overflow-hidden rounded-lg border border-border bg-surface">
      <header className="flex items-center justify-between border-b border-border px-4 py-2.5">
        <h2 className="font-display text-sm font-semibold text-ink">
          Uploads{active > 0 && <span className="text-ink-muted"> · {active} active</span>}
        </h2>
        {items.some((i) => ["done", "failed", "cancelled"].includes(i.status)) && (
          <button
            type="button"
            onClick={() =>
              items
                .filter((i) => ["done", "failed", "cancelled"].includes(i.status))
                .forEach((i) => onDismiss(i.id))
            }
            className="rounded px-2 py-0.5 text-xs text-ink-muted transition-colors hover:bg-surface-sunken"
          >
            Clear finished
          </button>
        )}
      </header>

      <ul className="divide-y divide-border">
        {items.map((item) => (
          <li key={item.id} className="px-4 py-3">
            <div className="flex items-baseline justify-between gap-3">
              <span className="truncate text-sm text-ink" title={item.name}>
                {item.name}
              </span>
              <span className="shrink-0 font-mono text-xs text-ink-subtle">
                {formatBytes(item.size)}
              </span>
            </div>

            <div className="mt-2 flex items-center gap-3">
              <ProgressBar item={item} />
              <StatusText item={item} />
              <Actions
                item={item}
                onCancel={onCancel}
                onRetry={onRetry}
                onDismiss={onDismiss}
              />
            </div>

            {item.error && (
              <p className="mt-1.5 text-xs text-status-flagged">{item.error}</p>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}

function ProgressBar({ item }: { item: UploadItem }) {
  const failed = item.status === "failed" || item.status === "cancelled";
  const done = item.status === "done";
  const colour = failed
    ? "bg-status-flagged"
    : done
      ? "bg-status-clean"
      : item.status === "retrying"
        ? "bg-status-scanning"
        : "bg-primary";

  return (
    <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-surface-sunken">
      <div
        className={`h-full rounded-full transition-[width] duration-200 ${colour}`}
        style={{ width: `${Math.max(item.progress * 100, failed ? 100 : 0)}%` }}
        role="progressbar"
        aria-valuenow={Math.floor(item.progress * 100)}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label={`${item.name} upload progress`}
      />
    </div>
  );
}

function StatusText({ item }: { item: UploadItem }) {
  const text = (() => {
    switch (item.status) {
      case "queued":
        return "Queued";
      case "checking":
        return "Checking…";
      case "uploading":
        // Parts, not a vague percentage — this is the "visibly real" requirement.
        return `${formatPercent(item.progress)} · part ${item.partsCompleted}/${item.partsTotal}`;
      case "retrying":
        return `Retrying (${item.attempt}/4)…`;
      case "finalising":
        return "Finalising…";
      case "done":
        return "Done";
      case "cancelled":
        return "Cancelled";
      case "failed":
        return "Failed";
    }
  })();

  const tone =
    item.status === "failed"
      ? "text-status-flagged"
      : item.status === "retrying"
        ? "text-status-scanning"
        : item.status === "done"
          ? "text-status-clean"
          : "text-ink-muted";

  return (
    <span className={`w-40 shrink-0 text-right font-mono text-xs ${tone}`}>
      {text}
    </span>
  );
}

function Actions({
  item,
  onCancel,
  onRetry,
  onDismiss,
}: {
  item: UploadItem;
  onCancel: (id: string) => void;
  onRetry: (id: string) => void;
  onDismiss: (id: string) => void;
}) {
  const inFlight = ["checking", "uploading", "retrying", "finalising"].includes(
    item.status,
  );
  // A permanent refusal offers no retry: pretending it might work is the dishonesty
  // this whole design is trying to avoid.
  const retryable =
    item.status === "failed" && item.failureKind === "network";

  return (
    <span className="flex w-16 shrink-0 justify-end gap-1">
      {inFlight && (
        <ActionButton onClick={() => onCancel(item.id)}>Cancel</ActionButton>
      )}
      {retryable && (
        <ActionButton onClick={() => onRetry(item.id)}>Retry</ActionButton>
      )}
      {["done", "cancelled"].includes(item.status) && (
        <ActionButton onClick={() => onDismiss(item.id)}>Clear</ActionButton>
      )}
      {item.status === "failed" && !retryable && (
        <ActionButton onClick={() => onDismiss(item.id)}>Clear</ActionButton>
      )}
    </span>
  );
}

function ActionButton({
  onClick,
  children,
}: {
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="rounded px-1.5 py-0.5 text-xs text-ink-muted transition-colors hover:bg-surface-sunken hover:text-ink"
    >
      {children}
    </button>
  );
}
