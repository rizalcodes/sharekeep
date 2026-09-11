/**
 * The invitation token between the emailed link and the moment it is used
 * (Architecture.md §4.6).
 *
 * The email carries the token in the URL **fragment**, the one part of a URL no server
 * receives, so no access log on any hop can record it. This module takes it out of the
 * fragment, removes it from the address bar at once — so it does not sit in browser
 * history or on a shared screen — and holds it in `sessionStorage` across a sign-in or
 * sign-up. It leaves only in a POST body to the API.
 *
 * `sessionStorage` rather than `localStorage`: the token should not outlive the tab it
 * was opened in. Every access is wrapped, because storage can be unavailable (private
 * windows, blocked site data) and a missing store must degrade to "open the link again",
 * not to a crash.
 */

export type PendingInvitation = {
  token: string;
  /** Filled in once the preview has been fetched, so the sign-up form can lock it. */
  email?: string;
  workspaceName?: string;
};

const KEY = "fileshare.pendingInvitation";

/** Take the token out of `#token=…`, if present, and scrub it from the address bar. */
export function takeTokenFromFragment(): string | null {
  if (typeof window === "undefined") return null;
  const hash = window.location.hash.replace(/^#/, "");
  if (!hash) return null;
  const token = new URLSearchParams(hash).get("token");
  // Scrub whether or not a token was found: a fragment on this page has no other use.
  window.history.replaceState(null, "", window.location.pathname);
  return token && token.trim() ? token.trim() : null;
}

export function savePendingInvitation(value: PendingInvitation): void {
  try {
    window.sessionStorage.setItem(KEY, JSON.stringify(value));
  } catch {
    // Storage unavailable: the page still works for a user who is already signed in.
  }
}

export function readPendingInvitation(): PendingInvitation | null {
  try {
    const raw = window.sessionStorage.getItem(KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as PendingInvitation;
    return typeof parsed?.token === "string" && parsed.token ? parsed : null;
  } catch {
    return null;
  }
}

export function clearPendingInvitation(): void {
  try {
    window.sessionStorage.removeItem(KEY);
  } catch {
    // Nothing to clear if storage is unavailable.
  }
}
