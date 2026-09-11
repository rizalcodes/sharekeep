"use client";

import { AppShell } from "@/components/app-shell";
import { WorkspaceProvider } from "@/lib/workspace-context";

/**
 * Every signed-in page shares one session, one chosen workspace and one header
 * (Design.md §5, M7). A route group, so the URLs stay /files, /activity and so on.
 */
export default function SignedInLayout({ children }: { children: React.ReactNode }) {
  return (
    <WorkspaceProvider>
      <AppShell>{children}</AppShell>
    </WorkspaceProvider>
  );
}
