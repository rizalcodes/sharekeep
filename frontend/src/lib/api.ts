import type {
  FolderListing,
  FolderOut,
  InFlightUpload,
  Quota,
  ResumeState,
  Session,
  UploadInitResponse,
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
    const detail =
      body && typeof body === "object" && "detail" in body
        ? String((body as { detail: unknown }).detail)
        : response.statusText || "Request failed.";
    throw new ApiError(detail, response.status);
  }
  return body as T;
}

// --- Auth -------------------------------------------------------------------

export const auth = {
  signup: (email: string, password: string, fullName: string) =>
    apiFetch<Session>("/auth/signup", {
      method: "POST",
      body: JSON.stringify({ email, password, full_name: fullName }),
    }),
  login: (email: string, password: string) =>
    apiFetch<Session>("/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    }),
  logout: () => apiFetch<void>("/auth/logout", { method: "POST" }),
  me: () => apiFetch<Session>("/auth/me"),
};

// --- Workspace content ------------------------------------------------------

export const workspaces = {
  list: () => apiFetch<Session["workspaces"]>("/workspaces"),
  quota: (workspaceId: string) =>
    apiFetch<Quota>(`/workspaces/${workspaceId}/quota`),
};

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

export function fetchReadiness(): Promise<ReadinessResponse> {
  return fetch(`${API_BASE_URL}/health/ready`, { cache: "no-store" }).then((r) =>
    r.json(),
  );
}
