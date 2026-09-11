"use client";

import { useRouter } from "next/navigation";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { auth } from "@/lib/api";
import type { Session, WorkspaceSummary } from "@/lib/types";

/**
 * The signed-in session and the chosen workspace, shared by every page in the shell
 * (Design.md §5, M7).
 *
 * Before M7 each page loaded the session itself and started on the first workspace in
 * the list, so choosing a workspace on Files and then opening Activity silently showed a
 * different one. The choice belongs to the shell, not to a page.
 *
 * The choice is remembered per user in localStorage — a per-browser convenience, never
 * an authority: the server re-checks membership on every request, and a remembered id
 * the user no longer belongs to simply falls back to the first workspace.
 */
type WorkspaceContextValue = {
  session: Session;
  workspace: WorkspaceSummary;
  selectWorkspace: (id: string) => void;
  /** Re-read the session, e.g. after creating, renaming or leaving a workspace. */
  refreshSession: (preferId?: string) => Promise<void>;
};

const Context = createContext<WorkspaceContextValue | null>(null);

const keyFor = (userId: string) => `fileshare.workspace.${userId}`;

function remembered(userId: string): string | null {
  try {
    return window.localStorage.getItem(keyFor(userId));
  } catch {
    return null;
  }
}

function remember(userId: string, workspaceId: string): void {
  try {
    window.localStorage.setItem(keyFor(userId), workspaceId);
  } catch {
    // Storage unavailable: the choice lasts for this page view only.
  }
}

function pick(session: Session, preferId?: string | null): WorkspaceSummary | null {
  const byId = (id?: string | null) =>
    id ? session.workspaces.find((w) => w.id === id) : undefined;
  return (
    byId(preferId) ??
    byId(remembered(session.user.id)) ??
    session.workspaces[0] ??
    null
  );
}

export function WorkspaceProvider({ children }: { children: ReactNode }) {
  const router = useRouter();
  const [session, setSession] = useState<Session | null>(null);
  const [workspaceId, setWorkspaceId] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const me = await auth.me();
        if (cancelled) return;
        setSession(me);
        setWorkspaceId(pick(me)?.id ?? null);
      } catch {
        if (!cancelled) router.replace("/login");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [router]);

  const selectWorkspace = useCallback(
    (id: string) => {
      if (!session || !session.workspaces.some((w) => w.id === id)) return;
      remember(session.user.id, id);
      setWorkspaceId(id);
    },
    [session],
  );

  const refreshSession = useCallback(async (preferId?: string) => {
    const me = await auth.me();
    setSession(me);
    const next = pick(me, preferId);
    if (next) remember(me.user.id, next.id);
    setWorkspaceId(next?.id ?? null);
  }, []);

  const value = useMemo<WorkspaceContextValue | null>(() => {
    if (!session) return null;
    const workspace = session.workspaces.find((w) => w.id === workspaceId);
    if (!workspace) return null;
    return { session, workspace, selectWorkspace, refreshSession };
  }, [session, workspaceId, selectWorkspace, refreshSession]);

  if (!value) {
    return (
      <main className="flex flex-1 items-center justify-center p-8">
        <p className="text-sm text-ink-muted">
          {session && session.workspaces.length === 0
            ? "You are not a member of any workspace."
            : "Loading…"}
        </p>
      </main>
    );
  }
  return <Context.Provider value={value}>{children}</Context.Provider>;
}

export function useWorkspace(): WorkspaceContextValue {
  const value = useContext(Context);
  if (!value) throw new Error("useWorkspace must be used inside WorkspaceProvider");
  return value;
}

/** Role checks in one place, mirroring Architecture §4.6. Presentation only: every
 * action is re-checked by the server, which is the only place a role is enforced. */
export function can(role: WorkspaceSummary["role"], needed: "viewer" | "editor" | "owner") {
  const level = { viewer: 0, editor: 1, owner: 2 } as const;
  return level[role] >= level[needed];
}
