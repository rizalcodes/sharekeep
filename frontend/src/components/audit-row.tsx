import type { AuditAction, AuditEntry } from "@/lib/types";
import { formatRelative } from "@/lib/format";

/**
 * One line of history (Design.md §5).
 *
 * Two rules drive everything here:
 *
 * - **A failed action must not look like a successful one.** `login_failed` and
 *   `share_access_denied` are the rows an audit log exists for. Rendering them in the
 *   same grey as a download buries exactly what the reader came to find.
 * - **The name shown is the name recorded.** The log is append-only, so a row written
 *   before M6 may carry no label at all. Those say the name was not recorded rather
 *   than showing a bare UUID or resolving it live — resolving it would mean a rename
 *   today rewrites what the log says happened last week.
 */

/** Past tense, because every row describes something that already happened. */
const PHRASING: Record<AuditAction, string> = {
  upload: "uploaded",
  download: "downloaded",
  share_created: "created a share link for",
  share_revoked: "revoked a share link for",
  share_accessed: "opened a share link for",
  share_access_denied: "was refused a share link for",
  delete: "deleted",
  permission_changed: "changed permissions on",
  // Named for history: this action has always meant restore-from-trash (Schema §10).
  version_restored: "restored from the trash",
  version_rolled_back: "rolled back",
  folder_created: "created the folder",
  folder_deleted: "deleted the folder",
  login: "signed in",
  login_failed: "failed to sign in",
  logout: "signed out",
  member_added: "added a member to",
  member_removed: "removed a member from",
  member_role_changed: "changed a member's role in",
  invitation_sent: "invited",
  invitation_accepted: "accepted an invitation to",
  invitation_revoked: "revoked an invitation for",
  scan_flagged: "was flagged by the malware scanner",
  scan_override: "released a file the scanner flagged:",
  quota_exceeded: "was refused an upload, over quota, in",
};

/** Rows that record something being refused, denied or withheld. */
const REFUSALS: ReadonlySet<AuditAction> = new Set<AuditAction>([
  "share_access_denied",
  "login_failed",
  "quota_exceeded",
  "scan_flagged",
]);

function actorLabel(entry: AuditEntry): string {
  if (entry.actor_name) return entry.actor_name;
  if (entry.actor_email) return entry.actor_email;
  // Not "Unknown": a null actor is specifically an anonymous share-link visitor or a
  // scanner verdict, and saying so is more truthful than implying we lost the record.
  if (entry.action === "scan_flagged") return "The scanner";
  return "Someone with a share link";
}

function denialReason(entry: AuditEntry): string | null {
  const reason = entry.metadata?.["denial_reason"];
  return typeof reason === "string" ? reason.replace(/_/g, " ") : null;
}

export function AuditRow({ entry }: { entry: AuditEntry }) {
  const refusal = REFUSALS.has(entry.action);
  const reason = denialReason(entry);

  return (
    <li
      className={`flex flex-wrap items-baseline gap-x-1.5 gap-y-1 border-b border-border px-4 py-2.5 text-sm last:border-b-0 ${
        refusal ? "bg-status-flagged-subtle/40" : ""
      }`}
    >
      <span className="font-medium text-ink">{actorLabel(entry)}</span>
      <span className={refusal ? "text-status-flagged" : "text-ink-muted"}>
        {PHRASING[entry.action]}
      </span>

      {entry.label ? (
        <span className="font-medium text-ink">{entry.label}</span>
      ) : (
        entry.target_id && (
          <span
            className="text-ink-subtle italic"
            title="This row was written before the name was recorded alongside it. The log is append-only, so it cannot be filled in after the fact."
          >
            name not recorded
          </span>
        )
      )}

      {reason && (
        <span className="font-mono text-xs text-status-flagged">({reason})</span>
      )}

      <span className="ml-auto flex items-baseline gap-3 whitespace-nowrap">
        {entry.ip_address && (
          <span
            className="font-mono text-xs text-ink-subtle"
            title="The address the request came from."
          >
            {entry.ip_address}
          </span>
        )}
        <time
          dateTime={entry.created_at}
          title={new Date(entry.created_at).toLocaleString()}
          className="text-xs text-ink-subtle"
        >
          {formatRelative(entry.created_at)}
        </time>
      </span>
    </li>
  );
}

export { PHRASING as AUDIT_PHRASING, REFUSALS as AUDIT_REFUSALS };
