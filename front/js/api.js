import { API_BASE, REQUEST_TIMEOUT_MS } from "../config.js";

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status; // 0 = 연결 실패/시간 초과
  }
}

async function request(path, { method = "GET", okStatus = [] } = {}) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), REQUEST_TIMEOUT_MS);
  let res;
  try {
    res = await fetch(API_BASE + path, { method, signal: ctrl.signal });
  } catch (e) {
    const msg = e.name === "AbortError" ? "서버 응답 시간 초과" : "서버에 연결할 수 없습니다";
    throw new ApiError(`${msg} (${API_BASE})`, 0);
  } finally {
    clearTimeout(timer);
  }
  const body = await res.json().catch(() => null);
  if (!res.ok && !okStatus.includes(res.status)) {
    const detail = body?.detail;
    // FastAPI 검증 오류(422)는 detail이 배열
    const msg = Array.isArray(detail) ? detail.map((d) => d.msg).join(", ") : detail;
    throw new ApiError(msg ?? `HTTP ${res.status}`, res.status);
  }
  return { status: res.status, body };
}

const enc = encodeURIComponent;
const qs = (params) => {
  const q = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => { if (v !== undefined && v !== null && v !== "") q.set(k, v); });
  const s = q.toString();
  return s ? `?${s}` : "";
};
const res = (kind, name) => `/api/v1/resources/${enc(kind)}/${enc(name)}`;

export const api = {
  ready: () => request("/readyz", { okStatus: [503] }),
  overview: async () => (await request("/api/v1/resources")).body,
  list: async (kind, selector, { limit, offset } = {}) =>
    (await request(`/api/v1/resources/${enc(kind)}${qs({ label_selector: selector, limit, offset })}`)).body,
  detail: async (kind, name) => (await request(res(kind, name))).body,
  events: async (kind, name, { limit, offset } = {}) =>
    (await request(`${res(kind, name)}/events${qs({ limit, offset })}`)).body,
  logs: async (name, { container, tail = 200, previous = false } = {}) =>
    (await request(`${res("pods", name)}/logs${qs({ container, tail_lines: tail, previous })}`)).body,

  // 쓰기: 확인창에서 [예]를 누른 경우에만 호출된다 → confirm=true
  remove: async (kind, name, uid) =>
    (await request(`${res(kind, name)}${qs({ confirm: true, uid })}`, { method: "DELETE" })).body,
  restart: async (kind, name) =>
    (await request(`${res(kind, name)}/restart${qs({ confirm: true })}`, { method: "POST" })).body,
};
