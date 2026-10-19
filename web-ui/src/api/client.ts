// API client for the teamEvolver unified console.
//
// All requests are same-origin: the 52010 evolve server hosts this SPA and
// serves dashboard, auth, user, skill, model-config and session-ingest endpoints natively.
// In dev, vite.config.ts proxies these paths to 127.0.0.1:52010.

import { toastErr } from "@/lib/toast";

export class ApiError extends Error {
  status?: number;
  constructor(message: string, status?: number) {
    super(message);
    this.status = status;
  }
}

// The console member selects a tenant; the choice survives reloads via
// localStorage and the ?tenant= URL param (URL wins so shared links are
// exact). The header is only a selector — the backend still authenticates
// the session and re-validates the tenant against its registry.

const TENANT_STORAGE_KEY = "teamEvolver.activeTenantId";

// Business requests start only after auth/status resolves the membership.
let tenantHeaderAllowed = false;

export function setTenantHeaderAllowed(allowed: boolean): void {
  tenantHeaderAllowed = allowed;
}

export function getActiveTenantId(): string {
  try {
    const fromUrl = new URLSearchParams(window.location.search).get("tenant");
    if (fromUrl) return fromUrl;
    return window.localStorage.getItem(TENANT_STORAGE_KEY) || "";
  } catch {
    return "";
  }
}

export function setActiveTenantId(tenantId: string): void {
  try {
    if (tenantId) window.localStorage.setItem(TENANT_STORAGE_KEY, tenantId);
    else window.localStorage.removeItem(TENANT_STORAGE_KEY);
    const url = new URL(window.location.href);
    if (tenantId) url.searchParams.set("tenant", tenantId);
    else url.searchParams.delete("tenant");
    window.history.replaceState(null, "", url.toString());
  } catch {
    // localStorage/URL unavailable (private mode etc.) — selection is lost.
  }
}

// Keep a rejected selector visible until the user chooses an allowed tenant.
function clearInvalidTenantSelection(message: string): void {
  toastErr("当前租户不可用，请重新选择", message);
  window.dispatchEvent(new Event("console-auth-changed"));
}

/**
 * Build the tenant selector header for the given path.
 *
 * The header is only a selector — the backend still authenticates the
 * session and re-validates the tenant against its registry.  Injection is
 * gated on the authenticated console session (see
 * {@link setTenantHeaderAllowed}).  Covers every non-`/v1/` endpoint so that
 * dashboard endpoints (`/status`, `/conversations`, `/storage/status`, …)
 * are also tenant-scoped, not just `/api/`.
 *
 * Returns a `Headers` (or the passed-in headers mutated) so callers that use
 * raw `fetch()` instead of {@link api} can still attach the header.
 */
export function tenantHeaders(path: string, init?: HeadersInit): Headers {
  const headers = init instanceof Headers ? init : new Headers(init);
  const tenantId = getActiveTenantId();
  if (
    tenantId &&
    (tenantHeaderAllowed || path === "/api/auth/status") &&
    !path.startsWith("/v1/") &&
    !headers.has("X-Tenant-Id")
  ) {
    headers.set("X-Tenant-Id", tenantId);
  }
  return headers;
}

// FastAPI validation errors are arrays; never stringify their input/ctx fields,
// which may contain an entire submitted document or credential.
function apiErrorMessage(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map(apiErrorMessage).filter(Boolean).join("；");
  if (detail && typeof detail === "object") {
    const value = detail as Record<string, unknown>;
    const message = [value.msg, value.message, value.code].find(v => typeof v === "string");
    const location = Array.isArray(value.loc)
      ? value.loc.filter(v => typeof v === "string" || typeof v === "number").join(".") : "";
    if (message) return location ? `${location}: ${message}` : String(message);
  }
  return "";
}

export async function api<T = any>(path: string, opts?: RequestInit): Promise<T> {
  // Every request must settle: the backend serves the SPA and the evolution
  // cycle on one event loop, so under load a request can hang for minutes.
  // A hung fetch wedges view polling (inflight guards never clear) — bound
  // GETs (polls) to 20s and mutations to 120s so the UI always recovers.
  const method = (opts?.method || "GET").toUpperCase();
  // Auth checks should be fast, but the backend shares one event loop with
  // the evolution cycle — under load even a trivial /api/auth/status can
  // hang.  Give auth paths more headroom so a busy server doesn't log the
  // user out with a scary timeout toast.
  const isAuthPath = path.startsWith("/api/auth/");
  const timeoutMs = isAuthPath ? 60_000 : 20_000;
  const timeoutSignal = AbortSignal.timeout(timeoutMs);
  const signal = opts?.signal
    ? AbortSignal.any([opts.signal, timeoutSignal])
    : timeoutSignal;
  let res: Response;
  try {
    // The server validates the selected tenant against current membership.
    // Covers every non-/v1/ endpoint so dashboard endpoints (/status,
    // /conversations, /storage/status, …) are also tenant-scoped.
    const headers = tenantHeaders(path, opts?.headers);
    res = await fetch(path, { ...opts, headers, signal });
  } catch (err: any) {
    if (err?.name === "TimeoutError" || err?.name === "AbortError") {
      throw new ApiError(`请求超时（${timeoutMs / 1000}s），稍后自动重试`);
    }
    throw err;
  }
  const text = await res.text();
  let data: any;
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { raw: text };
  }
  if (!res.ok) {
    // Recheck authority without silently changing the selected tenant.
    if (res.status === 403 || res.status === 401) {
      const detail = String(data?.detail || "");
      if (
        detail === "tenant mismatch" ||
        detail === "TENANT_MISMATCH" ||
        detail.startsWith("unknown tenant:") ||
        detail === "TENANT_ACCESS_DENIED" ||
        detail === "TENANT_MEMBERSHIP_REQUIRED"
      ) {
        clearInvalidTenantSelection(detail);
      }
      if (["TENANT_ADMIN_REQUIRED", "PLATFORM_ADMIN_REQUIRED"].includes(detail)) {
        window.dispatchEvent(new Event("console-auth-changed"));
      }
      // Recheck the session in App without reloading the SPA. A full reload
      // loses the current in-memory view and makes an isolated/transient 401
      // look like a navigation back to the dashboard.
      if (
        res.status === 401 &&
        (detail === "login or tenant token required" || detail === "login required")
      ) {
        window.dispatchEvent(new Event("console-auth-changed"));
      }
    }
    throw new ApiError(
      apiErrorMessage(data?.detail) || apiErrorMessage(data?.msg) || data?.raw || res.statusText || `${path} -> ${res.status}`,
      res.status
    );
  }
  return data as T;
}
