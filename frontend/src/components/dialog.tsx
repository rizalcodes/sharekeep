"use client";

import { useEffect, useRef, type ReactNode } from "react";

/**
 * The one modal every M7 dialog uses, matching the share dialog's look so actions feel
 * like one product rather than one per author.
 *
 * Escape and a click on the backdrop close it; focus moves into it when it opens and
 * back to whatever opened it when it closes, so keyboard users are not stranded.
 */
export function Dialog({
  title,
  subtitle,
  onClose,
  children,
  width = "max-w-lg",
}: {
  title: string;
  subtitle?: string;
  onClose: () => void;
  children: ReactNode;
  width?: string;
}) {
  const panel = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    const first = panel.current?.querySelector<HTMLElement>(
      "input, select, textarea, button:not([data-dialog-close])",
    );
    (first ?? panel.current)?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      opener?.focus?.();
    };
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/40 p-4 sm:items-center"
      role="dialog"
      aria-modal="true"
      aria-label={title}
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        ref={panel}
        tabIndex={-1}
        className={`w-full ${width} rounded-lg border border-border bg-surface shadow-lg outline-none`}
      >
        <header className="flex items-baseline justify-between gap-4 border-b border-border px-5 py-3">
          <div className="min-w-0">
            <h2 className="font-display text-sm font-semibold text-ink">{title}</h2>
            {subtitle && (
              <p className="truncate text-xs text-ink-muted" title={subtitle}>
                {subtitle}
              </p>
            )}
          </div>
          <button
            type="button"
            data-dialog-close
            onClick={onClose}
            className="shrink-0 rounded px-2 py-0.5 text-xs text-ink-muted transition-colors hover:bg-surface-sunken hover:text-ink"
          >
            Close
          </button>
        </header>
        <div className="px-5 py-4">{children}</div>
      </div>
    </div>
  );
}

/** A refusal or failure inside a dialog, in the reserved error treatment. */
export function DialogError({ message }: { message: string | null }) {
  if (!message) return null;
  return (
    <p
      role="alert"
      className="mt-3 rounded-md bg-status-flagged-subtle px-3 py-2 text-sm text-status-flagged"
    >
      {message}
    </p>
  );
}

export const buttonPrimary =
  "rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-ink transition-colors hover:bg-primary-hover disabled:opacity-60";
export const buttonQuiet =
  "rounded-md px-3 py-1.5 text-sm text-ink-muted transition-colors hover:bg-surface-sunken hover:text-ink disabled:opacity-60";
export const buttonDanger =
  "rounded-md bg-status-flagged px-3 py-1.5 text-sm font-medium text-primary-ink transition-opacity hover:opacity-90 disabled:opacity-60";
export const fieldClass =
  "w-full rounded-md border border-border bg-surface px-3 py-2 text-sm text-ink outline-none transition-colors focus:border-primary";
