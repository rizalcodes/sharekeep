import type { ScanStatus } from "@/lib/types";

/**
 * The scan badge is a core trust signal (Design.md §5), so it never overstates.
 * "Not scanned" is shown as exactly that — a product that claims a check it did not
 * run is worse than one that admits the gap.
 */
const PRESENTATION: Record<
  ScanStatus,
  { label: string; className: string; title: string }
> = {
  clean: {
    label: "Clean",
    className: "bg-status-clean-subtle text-status-clean",
    title: "Scanned and no threats were found.",
  },
  scanning: {
    label: "Scanning",
    className: "bg-status-scanning-subtle text-status-scanning",
    title: "Being scanned. It cannot be downloaded until this finishes.",
  },
  pending: {
    label: "Queued",
    className: "bg-status-scanning-subtle text-status-scanning",
    title: "Waiting to be scanned. It cannot be downloaded yet.",
  },
  flagged: {
    label: "Flagged",
    className: "bg-status-flagged-subtle text-status-flagged",
    title: "A scanner flagged this file. Nobody can download it, including you.",
  },
  scan_skipped: {
    label: "Not scanned",
    className: "bg-status-unscanned-subtle text-status-unscanned",
    title: "This file was not scanned for malware. Treat it with the same care you would any unchecked file.",
  },
  scan_failed: {
    label: "Scan failed",
    className: "bg-status-flagged-subtle text-status-flagged",
    title: "The scan could not complete. Downloads stay blocked until it succeeds.",
  },
};

export function ScanBadge({ status }: { status: ScanStatus }) {
  const { label, className, title } = PRESENTATION[status];
  return (
    <span
      className={`inline-flex shrink-0 items-center rounded px-1.5 py-0.5 font-mono text-[11px] ${className}`}
      title={title}
    >
      {label}
    </span>
  );
}

export function isDownloadable(status: ScanStatus): boolean {
  return status === "clean" || status === "scan_skipped";
}
