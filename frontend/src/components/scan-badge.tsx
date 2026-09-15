import type { ScanStatus } from "@/lib/types";

/**
 * The scan badge is a core trust signal (Design.md §5), so it never overstates.
 *
 * Three rules this component exists to keep:
 *
 * 1. "Not scanned" is shown as exactly that. A product that claims a check it did not
 *    run is worse than one that admits the gap.
 * 2. "Not scanned" always says *which kind*. The badge covers three situations —
 *    scanning switched off, a file above the scanner's size ceiling, and a file the
 *    scanner had never seen whose content was deliberately not uploaded. Left
 *    unexplained, a reader assumes the most reassuring one.
 * 3. A released file stays flagged. An override grants access; it does not create a
 *    clean verdict, and turning the badge green would invent one.
 */
type Presentation = { label: string; className: string; title: string };

const PRESENTATION: Record<ScanStatus, Presentation> = {
  clean: {
    label: "Clean",
    className: "bg-status-clean-subtle text-status-clean",
    title: "Scanned and no threats were found.",
  },
  scanning: {
    label: "Scanning",
    className: "bg-status-scanning-subtle text-status-scanning",
    title: "Being scanned right now. It cannot be downloaded until this finishes.",
  },
  pending: {
    label: "Queued",
    // Neutral, not amber (Design.md §5). Amber means the scanner is working on it right
    // now; queued means nothing has happened yet. §3's own line said "pending" was amber
    // and this followed it until M7c.
    className: "bg-status-unscanned-subtle text-status-unscanned",
    title:
      "Waiting for scanner capacity, not being scanned yet. The scanner allows a " +
      "limited number of checks per day, so this can wait a while. It cannot be " +
      "downloaded until the scan runs.",
  },
  flagged: {
    label: "Flagged",
    className: "bg-status-flagged-subtle text-status-flagged",
    title: "A scanner flagged this file. Nobody can download it, including you.",
  },
  scan_skipped: {
    label: "Not scanned",
    className: "bg-status-unscanned-subtle text-status-unscanned",
    title:
      "This file was not scanned for malware. Treat it with the same care you would " +
      "any unchecked file.",
  },
  scan_failed: {
    label: "Scan failed",
    // Its own colour, not flagged red (Design.md §3, M7c): a scan that could not run is
    // not a conviction, and sharing red with "Flagged" made the two indistinguishable.
    className: "bg-status-failed-subtle text-status-failed",
    title: "The scan could not complete. Downloads stay blocked until it succeeds.",
  },
};

export type ScanFacts = {
  status: ScanStatus;
  /** The server's reason for a skipped scan, e.g. never seen by the scanner. */
  reason?: string | null;
  /** Set when an owner released the file despite its status. */
  overriddenAt?: string | null;
  overrideReason?: string | null;
};

export function ScanBadge({ status, reason, overriddenAt, overrideReason }: ScanFacts) {
  const base = PRESENTATION[status];
  const released = Boolean(overriddenAt);

  // Released files keep the flagged treatment. The label gains the release rather than
  // replacing the warning with reassurance.
  const label = released ? `${base.label} · released` : base.label;
  const title = released
    ? `${base.title}\n\nA workspace owner released it for download` +
      (overrideReason ? `: “${overrideReason}”` : ".") +
      "\nNothing re-scanned the file; the original result still stands."
    : reason
      ? `${base.title}\n\nReason: ${reason}`
      : base.title;

  return (
    <span
      className={`inline-flex shrink-0 items-center rounded px-1.5 py-0.5 font-mono text-[11px] ${base.className}`}
      title={title}
    >
      {label}
    </span>
  );
}
