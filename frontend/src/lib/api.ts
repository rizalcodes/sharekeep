const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

export type DependencyCheck = { ok: boolean; detail?: string };

export type ReadinessResponse = {
  status: "ready" | "degraded";
  checks: Record<string, DependencyCheck>;
};

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status?: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/**
 * Every request sends cookies — the session JWT lives in an httpOnly cookie,
 * never in localStorage (Architecture.md §9).
 */
export async function apiFetch<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      ...init,
      credentials: "include",
      headers: { "Content-Type": "application/json", ...init.headers },
    });
  } catch {
    throw new ApiError("Could not reach the API.");
  }

  const body = await response.json().catch(() => null);
  if (!response.ok && response.status !== 503) {
    const detail =
      body && typeof body === "object" && "detail" in body
        ? String((body as { detail: unknown }).detail)
        : response.statusText;
    throw new ApiError(detail, response.status);
  }
  return body as T;
}

export function fetchReadiness(): Promise<ReadinessResponse> {
  // 503 is a valid, informative answer here: it carries the per-dependency detail.
  return apiFetch<ReadinessResponse>("/health/ready", { cache: "no-store" });
}
