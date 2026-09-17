import { NextResponse, type NextRequest } from "next/server";

/**
 * Content-Security-Policy with a per-request nonce (Architecture.md §7, M8).
 *
 * Next.js renames this file convention from `middleware` to `proxy` as of 16, and it is
 * the only place a nonce can be minted: Next reads the nonce out of the request's CSP
 * header during server rendering and attaches it to the framework and page scripts it
 * emits, which is why the root layout forces dynamic rendering — a page built at build
 * time has no request to carry a nonce.
 *
 * What the policy is actually defending: this product serves files other people uploaded
 * and shows names they chose, so an injected `<script>` is the attack that would matter
 * most. Without `unsafe-inline`, a script the page did not author does not run, and
 * `connect-src` means even a script that somehow did run could not post the data
 * anywhere.
 */
const API_ORIGIN = (() => {
  try {
    return new URL(
      process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000",
    ).origin;
  } catch {
    return "";
  }
})();

/**
 * Uploads go browser-direct to object storage (Architecture.md §2 step 4), so the storage
 * origin has to be named here. Downloads do not: they are a top-level navigation to a
 * signed URL, which `connect-src` does not govern.
 */
const STORAGE_ORIGIN = (() => {
  const configured = process.env.NEXT_PUBLIC_STORAGE_ORIGIN;
  if (!configured) return "";
  try {
    return new URL(configured).origin;
  } catch {
    return "";
  }
})();

export function proxy(request: NextRequest) {
  const nonce = crypto.randomUUID().replace(/-/g, "");
  const isDev = process.env.NODE_ENV === "development";

  const policy = [
    "default-src 'self'",
    // 'strict-dynamic' lets the nonced framework bundle load the chunks it needs without
    // naming each one. 'unsafe-eval' is React's development-only requirement for readable
    // error stacks; it is never sent in production.
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'${isDev ? " 'unsafe-eval'" : ""}`,
    `style-src 'self' 'nonce-${nonce}'`,
    // The progress bars set their width through a style attribute, which style-src-attr
    // governs separately from <style> elements. A style attribute cannot execute script;
    // the alternative was a stylesheet rule per percentage.
    "style-src-attr 'unsafe-inline'",
    "img-src 'self' data: blob:",
    // next/font self-hosts the three faces, so no font CDN is needed.
    "font-src 'self'",
    [
      "connect-src 'self'",
      API_ORIGIN && API_ORIGIN !== "'self'" ? API_ORIGIN : "",
      STORAGE_ORIGIN,
    ]
      .filter(Boolean)
      .join(" "),
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    // Belt and braces with the X-Frame-Options: DENY in next.config.ts. This one is the
    // modern directive and covers browsers that ignore the legacy header.
    "frame-ancestors 'none'",
    ...(isDev ? [] : ["upgrade-insecure-requests"]),
  ].join("; ");

  const headers = new Headers(request.headers);
  headers.set("x-nonce", nonce);
  headers.set("Content-Security-Policy", policy);

  const response = NextResponse.next({ request: { headers } });
  response.headers.set("Content-Security-Policy", policy);
  return response;
}

export const config = {
  matcher: [
    {
      // Static assets and image optimisation carry no inline script, and a prefetch is
      // not a document, so neither needs a nonce minted for it.
      source: "/((?!_next/static|_next/image|favicon.ico).*)",
      missing: [
        { type: "header", key: "next-router-prefetch" },
        { type: "header", key: "purpose", value: "prefetch" },
      ],
    },
  ],
};
