"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { ApiError, auth, workspaces } from "@/lib/api";
import { can, useWorkspace } from "@/lib/workspace-context";
import { Dialog, DialogError, buttonPrimary, buttonQuiet, fieldClass } from "@/components/dialog";

/**
 * One header for every signed-in page (Design.md §5, M7): workspace switcher, primary
 * navigation and account menu, in the same place everywhere.
 *
 * Navigation entries appear only for roles that can use them — Activity for owners,
 * Trash for editors and owners — because offering a page that will refuse is an
 * invitation to a refusal. A page reached by URL still explains its own refusal.
 */
export function AppShell({ children }: { children: React.ReactNode }) {
  const { session, workspace, selectWorkspace, refreshSession } = useWorkspace();
  const pathname = usePathname();
  const [creating, setCreating] = useState(false);

  const nav = [
    { href: "/files", label: "Files", show: true },
    { href: "/search", label: "Search", show: true },
    { href: "/members", label: "Members", show: true },
    { href: "/activity", label: "Activity", show: can(workspace.role, "owner") },
    { href: "/trash", label: "Trash", show: can(workspace.role, "editor") },
    { href: "/settings", label: "Settings", show: true },
  ].filter((item) => item.show);

  return (
    <div className="flex min-h-full flex-1 flex-col">
      <header className="border-b border-border bg-surface">
        <div className="mx-auto flex w-full max-w-5xl flex-wrap items-center gap-x-6 gap-y-2 px-6 py-3">
          <Link
            href="/files"
            className="font-display text-sm font-semibold tracking-tight text-ink"
          >
            Secure File Sharing
          </Link>

          <label className="flex items-center gap-2 text-sm">
            <span className="sr-only">Workspace</span>
            <select
              value={workspace.id}
              onChange={(e) => {
                if (e.target.value === "__new__") {
                  setCreating(true);
                  return;
                }
                selectWorkspace(e.target.value);
              }}
              className="max-w-56 rounded-md border border-border bg-surface px-2 py-1 text-sm text-ink"
            >
              {session.workspaces.map((w) => (
                <option key={w.id} value={w.id}>
                  {w.name}
                  {w.is_personal ? " (personal)" : ""}
                </option>
              ))}
              <option value="__new__">New workspace…</option>
            </select>
            <span className="font-mono text-xs text-ink-subtle">{workspace.role}</span>
          </label>

          <nav aria-label="Primary" className="flex flex-1 flex-wrap items-center gap-1">
            {nav.map((item) => {
              const active = pathname === item.href || pathname.startsWith(`${item.href}/`);
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  aria-current={active ? "page" : undefined}
                  className={`rounded-md px-2.5 py-1.5 text-sm transition-colors ${
                    active
                      ? "bg-primary-subtle font-medium text-primary"
                      : "text-ink-muted hover:bg-surface-sunken hover:text-ink"
                  }`}
                >
                  {item.label}
                </Link>
              );
            })}
          </nav>

          <AccountMenu email={session.user.email} />
        </div>
      </header>
      {/* Keyed by workspace: switching workspace starts each page fresh in the new one
          rather than carrying over a folder, filter or cursor from the old one. */}
      <div
        key={workspace.id}
        className="mx-auto flex w-full max-w-5xl flex-1 flex-col px-6 py-8"
      >
        {children}
      </div>
      {creating && (
        <NewWorkspaceDialog
          onClose={() => setCreating(false)}
          onCreated={async (id) => {
            setCreating(false);
            await refreshSession(id);
          }}
        />
      )}
    </div>
  );
}

function NewWorkspaceDialog({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (id: string) => Promise<void>;
}) {
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const created = await workspaces.create(name.trim());
      await onCreated(created.id);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not reach the server.");
      setBusy(false);
    }
  }

  return (
    <Dialog title="New workspace" onClose={onClose} width="max-w-sm">
      <form onSubmit={submit} className="flex flex-col gap-3">
        <label className="flex flex-col gap-1.5">
          <span className="text-sm font-medium text-ink">Name</span>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            required
            maxLength={255}
            className={fieldClass}
          />
          <span className="text-xs text-ink-subtle">
            You will be its owner. Invite people from the Members page.
          </span>
        </label>
        <DialogError message={error} />
        <div className="flex justify-end gap-2">
          <button type="button" onClick={onClose} className={buttonQuiet}>
            Cancel
          </button>
          <button type="submit" disabled={busy || !name.trim()} className={buttonPrimary}>
            {busy ? "Creating…" : "Create workspace"}
          </button>
        </div>
      </form>
    </Dialog>
  );
}

function AccountMenu({ email }: { email: string }) {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
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

  async function signOut(everywhere: boolean) {
    setBusy(true);
    await (everywhere ? auth.logoutAll() : auth.logout()).catch(() => undefined);
    router.replace("/login");
  }

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-haspopup="menu"
        className="max-w-48 truncate rounded-md px-2.5 py-1.5 text-sm text-ink-muted transition-colors hover:bg-surface-sunken hover:text-ink"
      >
        {email}
      </button>
      {open && (
        <div
          role="menu"
          className="absolute right-0 z-20 mt-1 w-60 rounded-md border border-border bg-surface py-1 text-sm"
        >
          <button
            role="menuitem"
            type="button"
            disabled={busy}
            onClick={() => void signOut(false)}
            className="block w-full px-3 py-2 text-left text-ink hover:bg-surface-sunken"
          >
            Sign out
          </button>
          <button
            role="menuitem"
            type="button"
            disabled={busy}
            onClick={() => void signOut(true)}
            className="block w-full px-3 py-2 text-left text-ink hover:bg-surface-sunken"
          >
            Sign out everywhere
            <span className="mt-0.5 block text-xs text-ink-subtle">
              Ends every session on every device, including this one.
            </span>
          </button>
        </div>
      )}
    </div>
  );
}
