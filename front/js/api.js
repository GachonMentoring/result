import { API_BASE, REQUEST_TIMEOUT_MS } from "../config.js";

export class ApiError extends Error {
  constructor(message, status, data = null) {
    super(message);
    this.status = status; // 0 = 연결 실패/시간 초과
    this.data = data;     // 오류 응답 본문 (예: line, conflicts)
  }
}

async function request(path, { method = "GET", okStatus = [], json, timeout = REQUEST_TIMEOUT_MS } = {}) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeout);
  let res;
  try {
    const init = { method, signal: ctrl.signal };
    if (json !== undefined) {
      init.headers = { "Content-Type": "application/json" };
      init.body = JSON.stringify(json);
    }
    res = await fetch(API_BASE + path, init);
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
    throw new ApiError(msg ?? `HTTP ${res.status}`, res.status, body);
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

// ---------------------------------------------------------------- 매니페스트 (Work 3)
// 판정·apply 는 dry-run 을 여러 번 하므로 시간 여유를 둔다
const MF = "/api/v1/manifests";
const SLOW = { timeout: 30000 };

export const mf = {
  list: async () => (await request(MF)).body,
  read: async (path) => (await request(`${MF}/file${qs({ path })}`)).body,
  save: async (path, content, etag, force = false) =>
    (await request(`${MF}/file${qs({ path, force: force || undefined })}`, { method: "PUT", json: { content, etag } })).body,
  create: async (path, content) =>
    (await request(`${MF}/file${qs({ path })}`, { method: "POST", json: { content } })).body,
  remove: async (path, withResources) =>
    (await request(`${MF}/file${qs({ path, confirm: true, with_resources: withResources })}`,
      { method: "DELETE", ...SLOW })).body,
  status: async (path) => (await request(`${MF}/status${qs({ path })}`, SLOW)).body,
  statusAll: async () => (await request(`${MF}/status-all`, SLOW)).body,
  apply: async (path, force) =>
    (await request(`${MF}/apply${qs({ path, confirm: true, force: force || undefined })}`, { method: "POST", ...SLOW })).body,
  removeOrphan: async ({ apiVersion, kind, name }) =>
    (await request(`${MF}/orphans${qs({ api_version: apiVersion, kind, name, confirm: true })}`,
      { method: "DELETE", ...SLOW })).body,
};

// ---------------------------------------------------------------- 댓글 기능 (Work 4)
export const comments = {
  predict: async (text) =>
    (await request("/api/v1/comments/predict", { method: "POST", json: { text }, timeout: 10000 })).body,
  model: async () => (await request("/api/v1/comments/model", { timeout: 8000 })).body,
};

// ---------------------------------------------------------------- 피드백 (Work 5)
export const feedback = {
  list: async ({ filter = "all", limit = 10, offset = 0 } = {}) =>
    (await request(`/api/v1/feedback${qs({ filter, limit, offset })}`, { timeout: 10000 })).body,
  label: async (id, trueLabel) =>
    (await request(`/api/v1/feedback/${enc(id)}/label`, { method: "POST", json: { true_label: trueLabel }, timeout: 10000 })).body,
};
