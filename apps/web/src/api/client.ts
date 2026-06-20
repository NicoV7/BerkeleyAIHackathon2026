// Thin typed-ish fetch wrapper. Same-origin in dev (Vite proxies /api).
const BASE = import.meta.env.VITE_API_BASE ?? "";

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { "content-type": "application/json" },
    ...init,
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} on ${path}`);
  return (await res.json()) as T;
}

export const api = {
  get: <T>(path: string) => req<T>(path),
  post: <T>(path: string, body?: unknown) =>
    req<T>(path, { method: "POST", body: body ? JSON.stringify(body) : undefined }),
  put: <T>(path: string, body?: unknown) =>
    req<T>(path, { method: "PUT", body: body ? JSON.stringify(body) : undefined }),
  patch: <T>(path: string, body?: unknown) =>
    req<T>(path, { method: "PATCH", body: body ? JSON.stringify(body) : undefined }),
  health: () => req<{ status: string; db: boolean; redis: boolean; gateway: unknown }>("/api/health"),
};

// WebSocket URL helper for the live debate stream (WS-C uses this).
export function wsUrl(path: string): string {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const host = BASE ? new URL(BASE).host : location.host;
  return `${proto}://${host}${path}`;
}
