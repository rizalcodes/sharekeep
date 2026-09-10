"use client";

import { useCallback, useEffect, useState } from "react";
import { ApiError, shares } from "@/lib/api";
import { formatBytes } from "@/lib/format";
import type { FileOut, ShareLinkCreated, ShareLinkOut, ShareLinkType } from "@/lib/types";

/**
 * Create and manage share links for one file (Design.md §5).
 *
 * Three things this dialog exists to get right:
 *
 * 1. **Type and options are independent.** The type is a radio choice; password, expiry
 *    and a use limit are checkboxes that apply to *any* type. Presenting them as four
 *    exclusive options would hide the combination most people actually want — a
 *    password-protected link that also expires — which is the misreading Schema §9 v1.1
 *    was written to correct.
 * 2. **The link is shown once, and it says so before it is gone.** Tokens are stored
 *    hashed, so the raw link truly cannot be recovered. That warning belongs beside the
 *    copy button, not in a tooltip discovered later.
 * 3. **A use limit is labelled "uses".** The API never sees the bytes, so what gets
 *    counted is redemptions. Calling it downloads would promise accounting the
 *    architecture cannot deliver.
 */
const TYPES: { value: ShareLinkType; label: string; explanation: string }[] = [
  {
    value: "public",
    label: "Anyone with the link",
    explanation:
      "No sign-in and no password. Anybody who has the link can download the file, including people it was forwarded to.",
  },
  {
    value: "private",
    label: "One specific person",
    explanation:
      "Only the account you name can open it, and they must be signed in. Forwarding the link to anyone else achieves nothing.",
  },
  {
    value: "password_protected",
    label: "Anyone with the link and the password",
    explanation:
      "Send the link and the password separately, and the link alone reveals nothing — not even the file's name.",
  },
  {
    value: "expiring",
    label: "Anyone with the link, until a deadline",
    explanation:
      "Works like a public link, then stops at the time you set. You can still revoke it earlier.",
  },
];

export function ShareDialog({
  workspaceId,
  file,
  onClose,
}: {
  workspaceId: string;
  file: FileOut;
  onClose: () => void;
}) {
  const [linkType, setLinkType] = useState<ShareLinkType>("public");
  const [usePassword, setUsePassword] = useState(false);
  const [password, setPassword] = useState("");
  const [useExpiry, setUseExpiry] = useState(false);
  const [expiresAt, setExpiresAt] = useState("");
  const [useLimit, setUseLimit] = useState(false);
  const [limit, setLimit] = useState("1");
  const [email, setEmail] = useState("");

  const [existing, setExisting] = useState<ShareLinkOut[] | null>(null);
  const [created, setCreated] = useState<ShareLinkCreated | null>(null);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // A password-protected or expiring link needs its own field, so the checkbox is
  // forced on and locked rather than silently ignored.
  const passwordRequired = linkType === "password_protected";
  const expiryRequired = linkType === "expiring";
  const passwordOn = usePassword || passwordRequired;
  const expiryOn = useExpiry || expiryRequired;

  const refresh = useCallback(async () => {
    try {
      setExisting(await shares.list(workspaceId, file.id));
    } catch {
      setExisting([]);
    }
  }, [workspaceId, file.id]);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const links = await shares.list(workspaceId, file.id);
        if (!cancelled) setExisting(links);
      } catch {
        if (!cancelled) setExisting([]);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [workspaceId, file.id]);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      const link = await shares.create(workspaceId, file.id, {
        link_type: linkType,
        password: passwordOn && password ? password : null,
        expires_at: expiryOn && expiresAt ? new Date(expiresAt).toISOString() : null,
        max_downloads: useLimit ? Number(limit) : null,
        allowed_user_email: linkType === "private" ? email : null,
      });
      setCreated(link);
      setCopied(false);
      await refresh();
    } catch (caught) {
      setError(
        caught instanceof ApiError
          ? caught.message
          : "Could not reach the server.",
      );
    } finally {
      setBusy(false);
    }
  }

  async function revoke(id: string) {
    try {
      await shares.revoke(workspaceId, id);
      await refresh();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not revoke.");
    }
  }

  const fullUrl = created
    ? `${window.location.origin}${created.url_path}`
    : "";

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/40 p-4 sm:items-center"
      role="dialog"
      aria-modal="true"
      aria-label={`Share ${file.name}`}
    >
      <div className="w-full max-w-xl rounded-lg border border-border bg-surface shadow-lg">
        <header className="flex items-baseline justify-between gap-4 border-b border-border px-5 py-3">
          <div className="min-w-0">
            <h2 className="font-display text-sm font-semibold text-ink">Share file</h2>
            <p className="truncate text-xs text-ink-muted" title={file.name}>
              {file.name} · {formatBytes(file.size_bytes)}
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded px-2 py-1 text-sm text-ink-muted transition-colors hover:bg-surface-sunken"
          >
            Close
          </button>
        </header>

        {created ? (
          <div className="px-5 py-4">
            <p className="text-sm font-medium text-ink">Your link is ready.</p>
            <p className="mt-1 text-xs text-status-flagged">
              This is the only time it can be shown. The link is stored hashed, so it
              cannot be looked up again — copy it now. If you lose it, create a new link
              and revoke this one.
            </p>
            <div className="mt-3 flex gap-2">
              <input
                readOnly
                value={fullUrl}
                onFocus={(e) => e.currentTarget.select()}
                className="min-w-0 flex-1 rounded-md border border-border bg-surface-sunken px-3 py-2 font-mono text-xs text-ink"
              />
              <button
                type="button"
                onClick={() => {
                  void navigator.clipboard.writeText(fullUrl).then(
                    () => setCopied(true),
                    () => setCopied(false),
                  );
                }}
                className="shrink-0 rounded-md bg-primary px-3 py-2 text-xs font-medium text-primary-ink transition-colors hover:bg-primary-hover"
              >
                {copied ? "Copied" : "Copy"}
              </button>
            </div>
            <button
              type="button"
              onClick={() => setCreated(null)}
              className="mt-4 rounded px-2 py-1 text-xs text-primary transition-colors hover:bg-primary-subtle"
            >
              Create another link
            </button>
          </div>
        ) : (
          <form onSubmit={submit} className="px-5 py-4">
            <fieldset>
              <legend className="text-xs font-medium uppercase tracking-wide text-ink-subtle">
                Who can open it
              </legend>
              <div className="mt-2 flex flex-col gap-2">
                {TYPES.map((option) => (
                  <label
                    key={option.value}
                    className="flex cursor-pointer gap-2.5 rounded-md border border-border p-2.5 transition-colors hover:bg-surface-sunken"
                  >
                    <input
                      type="radio"
                      name="link_type"
                      value={option.value}
                      checked={linkType === option.value}
                      onChange={() => setLinkType(option.value)}
                      className="mt-0.5"
                    />
                    <span className="min-w-0">
                      <span className="block text-sm text-ink">{option.label}</span>
                      <span className="block text-xs text-ink-muted">
                        {option.explanation}
                      </span>
                    </span>
                  </label>
                ))}
              </div>
            </fieldset>

            {linkType === "private" && (
              <label className="mt-3 flex flex-col gap-1.5">
                <span className="text-sm font-medium text-ink">
                  Their account email
                </span>
                <input
                  type="email"
                  required
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  className="rounded-md border border-border bg-surface px-3 py-2 text-sm text-ink outline-none focus:border-primary"
                />
                <span className="text-xs text-ink-subtle">
                  They need an account here already — a private link for an address with
                  no account could not be opened by anyone.
                </span>
              </label>
            )}

            <fieldset className="mt-4">
              <legend className="text-xs font-medium uppercase tracking-wide text-ink-subtle">
                Extra conditions
              </legend>
              <p className="mt-1 text-xs text-ink-muted">
                These apply on top of any choice above — you can combine them.
              </p>

              <div className="mt-2 flex flex-col gap-2">
                <label className="flex items-center gap-2 text-sm text-ink">
                  <input
                    type="checkbox"
                    checked={passwordOn}
                    disabled={passwordRequired}
                    onChange={(e) => setUsePassword(e.target.checked)}
                  />
                  Require a password
                  {passwordRequired && (
                    <span className="text-xs text-ink-subtle">(always, for this type)</span>
                  )}
                </label>
                {passwordOn && (
                  <input
                    type="text"
                    required
                    minLength={8}
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    placeholder="At least 8 characters"
                    className="ml-6 rounded-md border border-border bg-surface px-3 py-2 text-sm text-ink outline-none focus:border-primary"
                  />
                )}

                <label className="flex items-center gap-2 text-sm text-ink">
                  <input
                    type="checkbox"
                    checked={expiryOn}
                    disabled={expiryRequired}
                    onChange={(e) => setUseExpiry(e.target.checked)}
                  />
                  Stop working at a set time
                  {expiryRequired && (
                    <span className="text-xs text-ink-subtle">(always, for this type)</span>
                  )}
                </label>
                {expiryOn && (
                  <input
                    type="datetime-local"
                    required
                    value={expiresAt}
                    onChange={(e) => setExpiresAt(e.target.value)}
                    className="ml-6 rounded-md border border-border bg-surface px-3 py-2 text-sm text-ink outline-none focus:border-primary"
                  />
                )}

                <label className="flex items-center gap-2 text-sm text-ink">
                  <input
                    type="checkbox"
                    checked={useLimit}
                    onChange={(e) => setUseLimit(e.target.checked)}
                  />
                  Limit how many times it can be used
                </label>
                {useLimit && (
                  <div className="ml-6">
                    <input
                      type="number"
                      min={1}
                      value={limit}
                      onChange={(e) => setLimit(e.target.value)}
                      className="w-24 rounded-md border border-border bg-surface px-3 py-2 text-sm text-ink outline-none focus:border-primary"
                    />
                    <p className="mt-1 text-xs text-ink-subtle">
                      Counts how many times the link is opened for a download, not
                      completed downloads — we hand the file over directly, so we cannot
                      see whether a transfer finished.
                    </p>
                  </div>
                )}
              </div>
            </fieldset>

            {error && (
              <p
                role="alert"
                className="mt-3 rounded-md bg-status-flagged-subtle px-3 py-2 text-sm text-status-flagged"
              >
                {error}
              </p>
            )}

            <button
              type="submit"
              disabled={busy}
              className="mt-4 w-full rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-ink transition-colors hover:bg-primary-hover disabled:opacity-60"
            >
              {busy ? "Creating…" : "Create link"}
            </button>
          </form>
        )}

        {existing !== null && existing.length > 0 && (
          <section className="border-t border-border px-5 py-3">
            <h3 className="text-xs font-medium uppercase tracking-wide text-ink-subtle">
              Existing links
            </h3>
            <ul className="mt-2 divide-y divide-border">
              {existing.map((link) => (
                <li key={link.id} className="flex items-center gap-3 py-2">
                  <span className="min-w-0 flex-1">
                    <span className="block text-sm text-ink">
                      {TYPES.find((t) => t.value === link.link_type)?.label ??
                        link.link_type}
                      {link.has_password && " · password"}
                    </span>
                    <span className="block font-mono text-xs text-ink-subtle">
                      {link.uses} use{link.uses === 1 ? "" : "s"}
                      {link.max_downloads !== null && ` of ${link.max_downloads}`}
                      {link.expires_at &&
                        ` · until ${new Date(link.expires_at).toLocaleString()}`}
                      {!link.is_active && " · revoked"}
                    </span>
                  </span>
                  {link.is_active && (
                    <button
                      type="button"
                      onClick={() => void revoke(link.id)}
                      className="shrink-0 rounded px-2 py-1 text-xs text-status-flagged transition-colors hover:bg-status-flagged-subtle"
                    >
                      Revoke
                    </button>
                  )}
                </li>
              ))}
            </ul>
            <p className="mt-2 text-xs text-ink-subtle">
              Revoking stops new downloads. A download link already handed out keeps
              working for up to a minute — a signed URL cannot be recalled once issued.
            </p>
          </section>
        )}
      </div>
    </div>
  );
}
