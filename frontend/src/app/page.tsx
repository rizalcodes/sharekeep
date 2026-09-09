"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { auth } from "@/lib/api";

/** Front door: straight to the files view when signed in, otherwise to sign-in. */
export default function Home() {
  const router = useRouter();

  useEffect(() => {
    auth
      .me()
      .then(() => router.replace("/files"))
      .catch(() => router.replace("/login"));
  }, [router]);

  return (
    <main className="flex flex-1 items-center justify-center p-8">
      <p className="text-sm text-ink-muted">Loading…</p>
    </main>
  );
}
