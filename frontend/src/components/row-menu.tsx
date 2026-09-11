"use client";

import { useEffect, useRef, useState } from "react";
import type { WorkspaceRole } from "@/lib/types";

export type RowAction = {
  label: string;
  onSelect: () => void;
  /** Destructive actions are set apart and never first in the list. */
  danger?: boolean;
  hint?: string;
};

/**
 * One menu per row for every action on that item (Design.md §5, M7). The caller passes
 * only the actions the viewer's role on the item permits — shown and then refused would
 * be worse than not shown.
 */
export function RowMenu({ label, actions }: { label: string; actions: RowAction[] }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const close = (event: MouseEvent) => {
      if (!ref.current?.contains(event.target as Node)) setOpen(false);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", escape);
    };
  }, [open]);

  if (actions.length === 0) return <span className="w-7 shrink-0" />;

  const safe = actions.filter((a) => !a.danger);
  const risky = actions.filter((a) => a.danger);

  return (
    <div ref={ref} className="relative shrink-0">
      <button
        type="button"
        aria-label={`Actions for ${label}`}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={(e) => {
          e.stopPropagation();
          setOpen((v) => !v);
        }}
        className="flex h-7 w-7 items-center justify-center rounded text-ink-subtle transition-colors hover:bg-surface-sunken hover:text-ink"
      >
        <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden>
          <circle cx="7" cy="3" r="1.2" fill="currentColor" />
          <circle cx="7" cy="7" r="1.2" fill="currentColor" />
          <circle cx="7" cy="11" r="1.2" fill="currentColor" />
        </svg>
      </button>
      {open && (
        <div
          role="menu"
          className="absolute right-0 z-20 mt-1 w-52 rounded-md border border-border bg-surface py-1 text-sm"
        >
          {[...safe, ...risky].map((action, index) => (
            <button
              key={action.label}
              role="menuitem"
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                setOpen(false);
                action.onSelect();
              }}
              className={`block w-full px-3 py-1.5 text-left hover:bg-surface-sunken ${
                action.danger ? "text-status-flagged" : "text-ink"
              } ${action.danger && index === safe.length && safe.length > 0 ? "mt-1 border-t border-border pt-2" : ""}`}
            >
              {action.label}
              {action.hint && (
                <span className="block text-xs text-ink-subtle">{action.hint}</span>
              )}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/**
 * The permission indicator (Design.md §5, clarified at M7).
 *
 * The viewer's own role is shown only where it DIFFERS from their workspace role — that
 * difference is the thing worth noticing; "editor" on every row of an editor's view is
 * noise. A separate quiet mark says the item is shared or has explicit grants. Text and
 * a small glyph only: the status colours belong to scan state.
 */
export function PermissionMark({
  myRole,
  workspaceRole,
  shareLinks = 0,
  grants = 0,
}: {
  myRole?: WorkspaceRole | null;
  workspaceRole: WorkspaceRole;
  shareLinks?: number;
  grants?: number;
}) {
  const differs = myRole && myRole !== workspaceRole;
  const shared = shareLinks > 0 || grants > 0;
  if (!differs && !shared) return null;

  const detail = [
    shareLinks > 0 ? `${shareLinks} active share link${shareLinks === 1 ? "" : "s"}` : null,
    grants > 0 ? `${grants} ${grants === 1 ? "person has" : "people have"} a role set here` : null,
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <span className="flex shrink-0 items-center gap-2 text-xs text-ink-subtle">
      {differs && (
        <span
          className="font-mono"
          title={`Your role on this item is ${myRole}; elsewhere in the workspace you are ${workspaceRole}.`}
        >
          you: {myRole}
        </span>
      )}
      {shared && (
        <span className="flex items-center gap-1" title={detail}>
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none" aria-hidden>
            <circle cx="3" cy="6" r="1.6" stroke="currentColor" strokeWidth="1.1" />
            <circle cx="9" cy="3" r="1.6" stroke="currentColor" strokeWidth="1.1" />
            <circle cx="9" cy="9" r="1.6" stroke="currentColor" strokeWidth="1.1" />
            <path d="M4.4 5.3 7.6 3.7M4.4 6.7l3.2 1.6" stroke="currentColor" strokeWidth="1.1" />
          </svg>
          <span className="sr-only">{detail}</span>
          shared
        </span>
      )}
    </span>
  );
}
