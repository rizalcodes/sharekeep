"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, audit } from "@/lib/api";
import type { AuditAction, AuditEntry } from "@/lib/types";
import { useWorkspace } from "@/lib/workspace-context";
import { AuditRow } from "@/components/audit-row";

/**
 * The workspace activity log (Architecture.md §4.7, Design.md §5).
 *
 * A reading surface, not a dashboard: no counters, no charts, no "insights". This is
 * where the product's security claim is either legible or it is not, so the page is
 * dense and reverse-chronological — an owner scanning for something that looks wrong
 * needs many rows visible at once.
 *
 * Three deliberate honesty rules, all of them things this page could easily fake:
 *
 * - **Owner-only, and the refusal says why.** An editor reaching this page is told it
 *   is owner-only, not shown a blank list. The log holds visitors' IP addresses and
 *   colleagues' activity patterns, and a reader who expected access deserves the
 *   reason rather than an error code.
 * - **Sign-ins are absent, and the page says so.** They carry no workspace, so they
 *   appear in no workspace's log. Unexplained, that gap looks like a hole in the record.
 * - **Paging is by cursor**, matching the server. An append-only table shifts under an
 *   offset every time a row lands.
 */

/** The filters worth offering: the rows a reader actually goes looking for. */
const FILTERS: { label: string; actions: AuditAction[] }[] = [
  { label: "Everything", actions: [] },
  {
    label: "Refusals",
    actions: ["share_access_denied", "login_failed", "quota_exceeded", "scan_flagged"],
  },
  { label: "Sharing", actions: ["share_created", "share_revoked", "share_accessed"] },
  {
    label: "Files",
    actions: ["upload", "download", "delete", "version_restored", "version_rolled_back"],
  },
  {
    label: "People",
    actions: [
      "member_added",
      "member_removed",
      "member_role_changed",
      "invitation_sent",
      "invitation_accepted",
      "invitation_revoked",
      "permission_changed",
    ],
  },
];

type Refusal = { status: number; message: string };

function asRefusal(caught: unknown): Refusal {
  return caught instanceof ApiError
    ? { status: caught.status, message: caught.message }
    : { status: 0, message: "Could not reach the server." };
}

export default function ActivityPage() {
  // The shell owns the session and the chosen workspace (M7): opening Activity shows the
  // workspace the user was just looking at, not the first one in their list.
  const { workspace } = useWorkspace();

  const [entries, setEntries] = useState<AuditEntry[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [filter, setFilter] = useState(0);
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [refusal, setRefusal] = useState<Refusal | null>(null);

  // Guards against a slow page landing after the filter has already moved on.
  const requestId = useRef(0);

  // The first page, and every refiltered one. The fetch is awaited before any state is
  // touched: setting state synchronously in an effect body triggers cascading renders,
  // so the "loading" flag is raised by whatever *caused* the reload instead.
  useEffect(() => {
    const id = ++requestId.current;
    let cancelled = false;

    void (async () => {
      try {
        const page = await audit.page(workspace.id, {
          limit: 50,
          action: FILTERS[filter].actions,
          q: search.trim() || undefined,
        });
        if (cancelled || requestId.current !== id) return;
        setEntries(page.entries);
        setCursor(page.next_cursor);
        setRefusal(null);
      } catch (caught) {
        if (cancelled || requestId.current !== id) return;
        setEntries([]);
        setCursor(null);
        setRefusal(asRefusal(caught));
      } finally {
        if (!cancelled && requestId.current === id) setLoading(false);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [workspace, filter, search]);

  /** Appends the next page. Separate from the effect because it adds rather than
   * replaces, and because a cursor is the *result* of a load — depending on it in an
   * effect would page forever. */
  const loadOlder = useCallback(async () => {
    if (!cursor) return;
    setLoadingMore(true);
    const id = requestId.current;
    try {
      const page = await audit.page(workspace.id, {
        limit: 50,
        cursor,
        action: FILTERS[filter].actions,
        q: search.trim() || undefined,
      });
      if (requestId.current !== id) return;
      setEntries((previous) => [...previous, ...page.entries]);
      setCursor(page.next_cursor);
    } catch (caught) {
      if (requestId.current !== id) return;
      setRefusal(asRefusal(caught));
    } finally {
      setLoadingMore(false);
    }
  }, [workspace, cursor, filter, search]);

  return (
    <main className="flex flex-1 flex-col">
      <header>
        <h1 className="font-display text-xl font-semibold tracking-tight text-ink">
          Activity
        </h1>
        <p className="mt-0.5 text-sm text-ink-muted">
          Everything that has happened in {workspace.name}, newest first.
        </p>
      </header>

      {/* Owner-only is a policy, so the refusal explains the policy. */}
      {refusal?.status === 403 && (
        <section className="mt-6 rounded-lg border border-border bg-surface p-5">
          <h2 className="font-display text-base font-semibold text-ink">
            Only workspace owners can read the activity log
          </h2>
          <p className="mt-2 text-sm text-ink-muted">
            It records the IP address of everyone who opened a share link, and every
            action each member took. That is restricted to owners on purpose rather than
            by oversight.
          </p>
          <p className="mt-2 text-sm text-ink-muted">
            You are {workspace.role} in {workspace.name}.
          </p>
        </section>
      )}

      {refusal && refusal.status !== 403 && (
        <p
          role="alert"
          className="mt-6 rounded-md bg-status-flagged-subtle px-3 py-2 text-sm text-status-flagged"
        >
          {refusal.message}
        </p>
      )}

      {!refusal && (
        <>
          <div className="mt-6 flex flex-wrap items-center gap-2">
            {FILTERS.map((option, index) => (
              <button
                key={option.label}
                type="button"
                onClick={() => {
                  if (index === filter) return;
                  setLoading(true);
                  setFilter(index);
                }}
                aria-pressed={filter === index}
                className={`rounded-md px-2.5 py-1.5 text-sm transition-colors ${
                  filter === index
                    ? "bg-primary-subtle font-medium text-primary"
                    : "text-ink-muted hover:bg-surface-sunken hover:text-ink"
                }`}
              >
                {option.label}
              </button>
            ))}
            <input
              type="search"
              value={search}
              onChange={(e) => {
                setLoading(true);
                setSearch(e.target.value);
              }}
              placeholder="Search names and people"
              className="ml-auto w-52 rounded-md border border-border bg-surface px-3 py-1.5 text-sm text-ink outline-none focus:border-primary"
            />
          </div>

          <section className="mt-4 overflow-hidden rounded-lg border border-border bg-surface">
            {loading ? (
              <p className="px-4 py-6 text-sm text-ink-muted">Loading activity…</p>
            ) : entries.length === 0 ? (
              <p className="px-4 py-6 text-sm text-ink-muted">
                {search.trim() || FILTERS[filter].actions.length > 0
                  ? "Nothing in this workspace matches that."
                  : "Nothing has happened in this workspace yet."}
              </p>
            ) : (
              <ul>
                {entries.map((entry) => (
                  <AuditRow key={entry.id} entry={entry} />
                ))}
              </ul>
            )}
          </section>

          {cursor && !loading && (
            <button
              type="button"
              onClick={() => void loadOlder()}
              disabled={loadingMore}
              className="mt-4 rounded-md border border-border px-3 py-1.5 text-sm text-ink-muted transition-colors hover:bg-surface-sunken disabled:opacity-60"
            >
              {loadingMore ? "Loading…" : "Load older activity"}
            </button>
          )}

          {/* The gap is explained rather than left looking like a missing record. */}
          <p className="mt-6 max-w-prose text-xs text-ink-subtle">
            Sign-ins, sign-outs and failed sign-in attempts are not listed here. They
            belong to a user&rsquo;s account rather than to this workspace, so showing
            them would reveal when someone was working on something unrelated. This log
            is append-only: nothing in it can be edited or removed, including by an
            owner.
          </p>
        </>
      )}
    </main>
  );
}
