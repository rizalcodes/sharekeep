import type {
  AuditAction,
  AuditPage,
  DeletionReceipt,
  DownloadResponse,
  Grant,
  Invitation,
  Member,
  SearchOut,
  SearchParams,
  TagCount,
  TrashOut,
  VersionOut,
  WorkspaceOut,
  FolderListing,
  FolderOut,
  InvitationPreview,
  InFlightUpload,
  Quota,
  ResumeState,
  Session,
  ShareDownload,
  ShareLinkCreated,
  ShareLinkOut,
  ShareLinkType,
  SharePreview,
  UploadInitResponse,
  WorkspaceRole,
} from "@/lib/types";

const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";
const V1 = `${API_BASE_URL}/api/v1`;

export type DependencyCheck = { ok: boolean; detail?: string };

export type ReadinessResponse = {
  status: "ready" | "degraded";
  checks: Record<string, DependencyCheck>;
};

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }

  /** Retrying will never help: the server has made a decision, not hit a hiccup. */
  get isPermanent(): boolean {
    return this.status >= 400 && this.status < 500 && this.status !== 429;
  }
}

export class NetworkError extends Error {
  constructor(message = "Could not reach the server.") {
    super(message);
    this.name = "NetworkError";
  }
}

/**
 * Every request sends cookies — the session JWT lives in an httpOnly cookie, never in
 * localStorage, so an XSS bug cannot exfiltrate a session (Architecture.md §9).
 */
export async function apiFetch<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${V1}${path}`, {
      ...init,
      credentials: "include",
      cache: "no-store",
      headers:
        init.body instanceof FormData
          ? init.headers
          : { "Content-Type": "application/json", ...init.headers },
    });
  } catch {
    throw new NetworkError();
  }

  if (response.status === 204) return undefined as T;

  const body = await response.json().catch(() => null);
  if (!response.ok) {
    throw new ApiError(readableDetail(body, response), response.status);
  }
  return body as T;
}

/**
 * The reason a request was refused, in words a person can read.
 *
 * FastAPI sends two shapes. A deliberate refusal — `HTTPException` — has `detail` as a
 * string, which is the wording the server chose. A validation failure has `detail` as a
 * LIST of Pydantic errors, and the old code called `String()` on it: every validation
 * refusal in the product reached the user as "[object Object]". Found at M7b by a
 * browser test that asked why a rejected tag showed no reason.
 *
 * Pydantic prefixes its own messages with "Value error, "; that is noise to a reader, so
 * it is stripped. The field name is prefixed only when the message does not already name
 * it, which keeps "At most 25 tags." from becoming "tags: At most 25 tags."
 */
function readableDetail(body: unknown, response: Response): string {
  const fallback = response.statusText || "Request failed.";
  if (!body || typeof body !== "object" || !("detail" in body)) return fallback;

  const detail = (body as { detail: unknown }).detail;
  if (typeof detail === "string") return detail;
  if (!Array.isArray(detail)) return fallback;

  const messages = detail
    .map((entry) => {
      if (!entry || typeof entry !== "object") return String(entry ?? "");
      const raw = String((entry as { msg?: unknown }).msg ?? "").replace(
        /^Value error,\s*/,
        "",
      );
      if (!raw) return "";
      const loc = (entry as { loc?: unknown }).loc;
      const field = Array.isArray(loc)
        ? loc.filter((part) => part !== "body" && typeof part === "string").join(" ")
        : "";
      return field && !raw.toLowerCase().includes(field.toLowerCase())
        ? `${field}: ${raw}`
        : raw;
    })
    .filter(Boolean);

  return messages.length > 0 ? messages.join(" ") : fallback;
}

// --- Auth -------------------------------------------------------------------

export const auth = {
  /**
   * With an invitation token the server takes the address from the invitation and
   * ignores `email`, so the form shows it locked rather than letting it be typed.
   */
  signup: (
    email: string,
    password: string,
    fullName: string,
    invitationToken?: string,
  ) =>
    apiFetch<Session>("/auth/signup", {
      method: "POST",
      body: JSON.stringify({
        email,
        password,
        full_name: fullName,
        ...(invitationToken ? { invitation_token: invitationToken } : {}),
      }),
    }),
  login: (email: string, password: string) =>
    apiFetch<Session>("/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    }),
  logout: () => apiFetch<void>("/auth/logout", { method: "POST" }),
  /** Revokes every session this account has, on every device. */
  logoutAll: () => apiFetch<{ revoked: number }>("/auth/logout-all", { method: "POST" }),
  me: () => apiFetch<Session>("/auth/me"),
};

// --- Workspace content ------------------------------------------------------

export const workspaces = {
  list: () => apiFetch<Session["workspaces"]>("/workspaces"),
  quota: (workspaceId: string) =>
    apiFetch<Quota>(`/workspaces/${workspaceId}/quota`),
  get: (workspaceId: string) => apiFetch<WorkspaceOut>(`/workspaces/${workspaceId}`),
  create: (name: string) =>
    apiFetch<WorkspaceOut>("/workspaces", {
      method: "POST",
      body: JSON.stringify({ name }),
    }),
  /** Quota is lower-only for owners, up to the platform ceiling (Architecture §4.6). */
  update: (
    workspaceId: string,
    body: { name?: string; storage_quota_bytes?: number },
  ) =>
    apiFetch<WorkspaceOut>(`/workspaces/${workspaceId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  remove: (workspaceId: string) =>
    apiFetch<void>(`/workspaces/${workspaceId}`, { method: "DELETE" }),
  leave: (workspaceId: string) =>
    apiFetch<void>(`/workspaces/${workspaceId}/leave`, { method: "POST" }),
};

export const members = {
  list: (workspaceId: string) =>
    apiFetch<Member[]>(`/workspaces/${workspaceId}/members`),
  changeRole: (workspaceId: string, memberId: string, role: WorkspaceRole) =>
    apiFetch<Member>(`/workspaces/${workspaceId}/members/${memberId}`, {
      method: "PATCH",
      body: JSON.stringify({ role }),
    }),
  remove: (workspaceId: string, memberId: string) =>
    apiFetch<void>(`/workspaces/${workspaceId}/members/${memberId}`, {
      method: "DELETE",
    }),
  invitations: (workspaceId: string) =>
    apiFetch<Invitation[]>(`/workspaces/${workspaceId}/invitations`),
  invite: (workspaceId: string, email: string, role: WorkspaceRole) =>
    apiFetch<{ invitation: Invitation; token: string }>(
      `/workspaces/${workspaceId}/invitations`,
      { method: "POST", body: JSON.stringify({ email, role }) },
    ),
  revokeInvitation: (workspaceId: string, invitationId: string) =>
    apiFetch<void>(`/workspaces/${workspaceId}/invitations/${invitationId}`, {
      method: "DELETE",
    }),
};

/** Per-item actions. The server re-checks every role; the UI only hides what would be
 * refused, it never grants anything (Design.md §5). */
export const items = {
  download: (workspaceId: string, fileId: string, versionId?: string) =>
    apiFetch<DownloadResponse>(
      `/workspaces/${workspaceId}/files/${fileId}/download${
        versionId ? `?version_id=${encodeURIComponent(versionId)}` : ""
      }`,
    ),
  versions: (workspaceId: string, fileId: string) =>
    apiFetch<VersionOut[]>(`/workspaces/${workspaceId}/files/${fileId}/versions`),
  rollback: (workspaceId: string, fileId: string, versionId: string) =>
    apiFetch<VersionOut>(
      `/workspaces/${workspaceId}/files/${fileId}/versions/${versionId}/restore`,
      { method: "POST" },
    ),
  release: (workspaceId: string, fileId: string, versionId: string, reason: string) =>
    apiFetch<VersionOut>(
      `/workspaces/${workspaceId}/files/${fileId}/versions/${versionId}/scan-override`,
      { method: "POST", body: JSON.stringify({ reason }) },
    ),
  updateFile: (
    workspaceId: string,
    fileId: string,
    body: { name?: string; folder_id?: string | null; move?: boolean; tags?: string[] },
  ) =>
    apiFetch<unknown>(`/workspaces/${workspaceId}/files/${fileId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  updateFolder: (
    workspaceId: string,
    folderId: string,
    body: { name?: string; parent_folder_id?: string | null; move?: boolean },
  ) =>
    apiFetch<unknown>(`/workspaces/${workspaceId}/folders/${folderId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  deleteFile: (workspaceId: string, fileId: string) =>
    apiFetch<DeletionReceipt>(`/workspaces/${workspaceId}/files/${fileId}`, {
      method: "DELETE",
    }),
  deleteFolder: (workspaceId: string, folderId: string) =>
    apiFetch<DeletionReceipt>(`/workspaces/${workspaceId}/folders/${folderId}`, {
      method: "DELETE",
    }),
  restore: (workspaceId: string, kind: "file" | "folder", batchId: string) =>
    apiFetch<unknown>(
      `/workspaces/${workspaceId}/${kind === "folder" ? "folders" : "files"}/restore`,
      { method: "POST", body: JSON.stringify({ deletion_batch_id: batchId }) },
    ),
  trash: (workspaceId: string) => apiFetch<TrashOut>(`/workspaces/${workspaceId}/trash`),
  grants: (workspaceId: string, target: ItemRef) =>
    apiFetch<Grant[]>(`/workspaces/${workspaceId}/${target.kind}s/${target.id}/grants`),
  grant: (workspaceId: string, target: ItemRef, userId: string, role: WorkspaceRole) =>
    apiFetch<Grant>(`/workspaces/${workspaceId}/${target.kind}s/${target.id}/grants`, {
      method: "POST",
      body: JSON.stringify({ user_id: userId, role }),
    }),
  revokeGrant: (workspaceId: string, target: ItemRef, userId: string) =>
    apiFetch<void>(
      `/workspaces/${workspaceId}/${target.kind}s/${target.id}/grants/${userId}`,
      { method: "DELETE" },
    ),
};

export type ItemRef = { kind: "file" | "folder"; id: string; name: string };

export const folders = {
  listing: (workspaceId: string, folderId?: string | null) =>
    apiFetch<FolderListing>(
      `/workspaces/${workspaceId}/folders${folderId ? `/${folderId}` : ""}`,
    ),
  create: (workspaceId: string, name: string, parentFolderId: string | null) =>
    apiFetch<FolderOut>(`/workspaces/${workspaceId}/folders`, {
      method: "POST",
      body: JSON.stringify({ name, parent_folder_id: parentFolderId }),
    }),
};

export const uploads = {
  init: (
    workspaceId: string,
    body: {
      name: string;
      size_bytes: number;
      mime_type: string;
      folder_id: string | null;
    },
  ) =>
    apiFetch<UploadInitResponse>(`/workspaces/${workspaceId}/uploads/init`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  complete: (
    workspaceId: string,
    versionId: string,
    parts: { part_number: number; etag: string }[],
  ) =>
    apiFetch<unknown>(`/workspaces/${workspaceId}/uploads/complete`, {
      method: "POST",
      body: JSON.stringify({ version_id: versionId, parts }),
    }),
  inFlight: (workspaceId: string) =>
    apiFetch<InFlightUpload[]>(`/workspaces/${workspaceId}/uploads`),
  resumeState: (workspaceId: string, versionId: string) =>
    apiFetch<ResumeState>(
      `/workspaces/${workspaceId}/uploads/${versionId}/parts`,
    ),
  cancel: (workspaceId: string, versionId: string) =>
    apiFetch<void>(`/workspaces/${workspaceId}/uploads/${versionId}`, {
      method: "DELETE",
    }),
};

// --- Share links ------------------------------------------------------------
//
// `shares.create` returns the only copy of the raw token that will ever exist, so the
// caller must show it before discarding the response (Architecture.md §4.2).

export const shares = {
  create: (
    workspaceId: string,
    fileId: string,
    body: {
      link_type: ShareLinkType;
      password?: string | null;
      expires_at?: string | null;
      max_downloads?: number | null;
      allowed_user_email?: string | null;
    },
  ) =>
    apiFetch<ShareLinkCreated>(
      `/workspaces/${workspaceId}/files/${fileId}/shares`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  list: (workspaceId: string, fileId: string) =>
    apiFetch<ShareLinkOut[]>(
      `/workspaces/${workspaceId}/files/${fileId}/shares`,
    ),
  revoke: (workspaceId: string, shareId: string) =>
    apiFetch<void>(`/workspaces/${workspaceId}/shares/${shareId}`, {
      method: "DELETE",
    }),
};

/** The public side. No session required, and none is sent that matters. */
export const publicShare = {
  preview: (token: string) =>
    apiFetch<SharePreview>(`/shares/${encodeURIComponent(token)}`),
  redeem: (token: string, password?: string) =>
    apiFetch<ShareDownload>(
      `/shares/${encodeURIComponent(token)}/download`,
      {
        method: "POST",
        body: JSON.stringify(password ? { password } : {}),
      },
    ),
};

// --- Invitations ------------------------------------------------------------
//
// The token always travels in a POST body, never in a path or query string: those are
// written verbatim into every access log on the way (Architecture.md §4.6).

export const invitations = {
  preview: (token: string) =>
    apiFetch<InvitationPreview>("/invitations/preview", {
      method: "POST",
      body: JSON.stringify({ token }),
    }),
  accept: (token: string) =>
    apiFetch<unknown>("/invitations/accept", {
      method: "POST",
      body: JSON.stringify({ token }),
    }),
};

// --- Search (Architecture.md §4.8) -------------------------------------------

export const search = {
  run: (workspaceId: string, params: SearchParams) => {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value === null || value === undefined || value === "") continue;
      if (Array.isArray(value)) value.forEach((item) => query.append(key, item));
      else query.set(key, String(value));
    }
    const suffix = query.toString();
    return apiFetch<SearchOut>(
      `/workspaces/${workspaceId}/search${suffix ? `?${suffix}` : ""}`,
    );
  },
  /** Tags in use, with counts, so the exact-match filter can offer what exists. */
  tags: (workspaceId: string) => apiFetch<TagCount[]>(`/workspaces/${workspaceId}/tags`),
};

// --- Activity log -----------------------------------------------------------
//
// Owner-only server-side (Architecture.md §4.7). A 403 here is a real answer, not a
// bug to route around, so the caller surfaces the reason rather than an empty page.

export const audit = {
  page: (
    workspaceId: string,
    options: {
      cursor?: string | null;
      limit?: number;
      action?: AuditAction[];
      q?: string;
    } = {},
  ) => {
    const params = new URLSearchParams();
    if (options.limit) params.set("limit", String(options.limit));
    if (options.cursor) params.set("cursor", options.cursor);
    if (options.q) params.set("q", options.q);
    for (const action of options.action ?? []) params.append("action", action);
    const query = params.toString();
    return apiFetch<AuditPage>(
      `/workspaces/${workspaceId}/audit${query ? `?${query}` : ""}`,
    );
  },
};

export function fetchReadiness(): Promise<ReadinessResponse> {
  return fetch(`${API_BASE_URL}/health/ready`, { cache: "no-store" }).then((r) =>
    r.json(),
  );
}
