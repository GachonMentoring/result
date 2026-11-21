// 인프라 관리: 개요 카드(=탭) + kind별 테이블 + 상세 패널(조작 포함), 2초 polling
import { POLL_MS } from "../../config.js";
import { api } from "../api.js";
import { createPoller } from "../poller.js";
import { clock, debounce, html, render } from "../util.js";
import { KINDS, KIND_BY_KEY } from "../infra/columns.js";
import { createDetailPanel } from "../infra/detail.js";
import { isUnhealthyPod } from "../infra/status.js";
import { EFFECTS } from "../infra/confirm.js";
import { clampOffset, movePage, pager } from "../infra/pager.js";
import { toast } from "../toast.js";

const ACTION_LABEL = { delete: "삭제", restart: "재시작" };

const DEFAULT_KIND = "pods";

export function mount(el, params) {
  const state = {
    kind: KIND_BY_KEY[params[0]] ? params[0] : DEFAULT_KIND,
    selector: "",
    namespace: null,
    ready: null,          // { ok, pending }
    counts: null,
    unhealthy: null,
    list: null,           // 현재 탭 목록 응답
    listKind: null,       // list가 어느 kind의 결과인지
    error: null,          // 연결 오류 메시지
    listError: null,
    updatedAt: null,
    offset: 0,            // 페이지 시작 위치 (pageSize 있는 kind만: Events)
  };

  render(el, html`
    <header class="page-header">
      <div>
        <h1>인프라 관리</h1>
        <p class="subtitle">Kubernetes 리소스 상태 · <span data-slot="ns" class="mono">-</span></p>
      </div>
      <div class="header-meta">
        <span data-slot="sync"></span>
        <span class="muted" data-slot="updated"></span>
        <button class="btn" data-action="refresh" title="지금 갱신">⟳ 새로고침</button>
      </div>
    </header>
    <div data-slot="banner"></div>
    <section class="cards" role="tablist" aria-label="리소스 종류" data-slot="cards"></section>
    <section class="table-card">
      <div class="table-toolbar">
        <h2 data-slot="table-title"></h2>
        <div data-slot="pager"></div>
        <label class="search">
          <span class="sr-only">label selector</span>
          <input type="search" placeholder="label selector (예: app=mlflow)" data-slot="selector" spellcheck="false" autocomplete="off">
        </label>
      </div>
      <div class="table-wrap" data-slot="table"></div>
    </section>
    <aside class="detail" aria-hidden="true" aria-label="리소스 상세" data-slot="detail"></aside>`);

  const $ = (name) => el.querySelector(`[data-slot="${name}"]`);
  const last = new Map(); // slot → 마지막으로 그린 HTML (바뀔 때만 다시 그림)
  function paint(name, tpl) {
    const s = String(tpl);
    if (last.get(name) === s) return;
    last.set(name, s);
    $(name).innerHTML = s;
  }

  const detail = createDetailPanel($("detail"), {
    onClose: () => drawTable(),
    // 조작 결과: 성공이면 effect(선언 상태에 주는 영향)와 함께 알림 → 즉시 갱신해서 변화를 보여준다
    onResult: ({ ok, action, message, effect }) => {
      if (ok) toast(message, EFFECTS[effect]?.tone === "bad" ? "warn" : "ok", 8000);
      else toast(`${ACTION_LABEL[action]} 실패: ${message}`, "bad", 8000);
      poller.refreshNow();
    },
  });

  // ------------------------------------------------------------ 그리기
  function drawHeader() {
    $("ns").textContent = state.namespace ?? "-";
    let sync;
    if (state.error) sync = html`<span class="badge badge-bad"><span class="dot"></span>연결 끊김</span>`;
    else if (!state.ready) sync = html`<span class="badge badge-neutral"><span class="dot"></span>확인 중</span>`;
    else if (state.ready.ok) sync = html`<span class="badge badge-ok"><span class="dot"></span>동기화됨</span>`;
    else sync = html`<span class="badge badge-warn" title="${state.ready.pending.join(", ")}"><span class="dot"></span>동기화 중</span>`;
    paint("sync", sync);
    $("updated").textContent = state.updatedAt ? `${clock(state.updatedAt)} 갱신 · ${POLL_MS / 1000}초 주기` : "";
    paint("banner", state.error
      ? html`<div class="banner banner-bad" role="alert"><strong>서버 연결 실패</strong> ${state.error} — 마지막으로 받은 데이터를 표시 중이며, 복구되면 자동으로 갱신됩니다.</div>`
      : "");
    el.classList.toggle("stale", !!state.error);
  }

  function drawCards() {
    const cards = KINDS.map((k) => {
      const n = state.counts?.[k.key];
      const active = k.key === state.kind;
      return html`
        <a class="card ${active ? "active" : ""}" role="tab" aria-selected="${String(active)}" href="#/infra/${k.key}">
          <span class="card-label">${k.label}</span>
          <span class="card-value">${n ?? "–"}</span>
        </a>`;
    });
    const bad = state.unhealthy;
    cards.push(html`
      <a class="card card-alert ${bad ? "has-alert" : ""}" href="#/infra/pods" title="Running·Ready가 아니거나 오류 상태인 파드">
        <span class="card-label">비정상 파드</span>
        <span class="card-value">${bad ?? "–"}</span>
      </a>`);
    paint("cards", cards.map(String).join(""));
  }

  function drawTable() {
    const k = KIND_BY_KEY[state.kind];
    $("table-title").textContent = state.list && state.listKind === state.kind
      ? `${k.label} (${state.list.count})` : k.label;

    paint("pager", state.list && state.listKind === state.kind ? pager(state.list) : "");

    if (state.listKind !== state.kind || !state.list) {
      paint("table", state.listError
        ? html`<div class="empty">${state.listError}</div>`
        : html`<div class="empty">불러오는 중…</div>`);
      return;
    }
    const items = state.list.items;
    if (!items.length) {
      paint("table", html`<div class="empty">${state.selector
        ? `'${state.selector}' 에 맞는 ${k.label}가 없습니다.`
        : `${state.namespace} 네임스페이스에 ${k.label}가 없습니다.`}</div>`);
      return;
    }
    const sel = detail.current();
    paint("table", html`
      <table>
        <thead><tr>${k.columns.map((c) => html`<th class="${c.cls ?? ""}" scope="col">${c.label}</th>`)}</tr></thead>
        <tbody>
          ${items.map((it) => html`
            <tr tabindex="0" data-name="${it.name}"
                class="${sel && sel.kind === state.kind && sel.name === it.name ? "selected" : ""} ${it.terminating ? "terminating" : ""}">
              ${k.columns.map((c) => html`<td class="${c.cls ?? ""}">${c.cell(it)}</td>`)}
            </tr>`)}
        </tbody>
      </table>`);
  }

  function drawAll() { drawHeader(); drawCards(); drawTable(); }

  // ------------------------------------------------------------ 데이터
  async function poll() {
    const kind = state.kind;
    const selector = state.selector;
    const offset = state.offset;
    const pageSize = KIND_BY_KEY[kind].pageSize;
    const needPods = !(kind === "pods" && !selector);
    const [ready, overview, list, pods] = await Promise.allSettled([
      api.ready(),
      api.overview(),
      api.list(kind, selector, pageSize ? { limit: pageSize, offset } : {}),
      needPods ? api.list("pods") : Promise.resolve(null),
    ]);
    if (kind !== state.kind || selector !== state.selector || offset !== state.offset) return; // 그 사이 탭/필터/페이지 바뀜

    const failures = [ready, overview, list, pods].filter((r) => r.status === "rejected");
    const conn = failures.find((r) => r.reason?.status === 0);
    state.error = conn ? conn.reason.message : null;

    if (ready.status === "fulfilled") {
      state.ready = { ok: ready.value.status === 200, pending: ready.value.body?.pending ?? [] };
    }
    if (overview.status === "fulfilled") {
      state.namespace = overview.value.namespace;
      state.counts = overview.value.counts;
    }
    if (list.status === "fulfilled") {
      state.list = list.value;
      state.listKind = kind;
      state.listError = null;
      // 이벤트가 만료(1시간)되어 개수가 줄면 현재 페이지가 비어버릴 수 있음 → 마지막 페이지로
      const fixed = clampOffset(offset, pageSize, list.value.count);
      if (pageSize && fixed !== offset) { state.offset = fixed; poller.refreshNow(); }
    } else if (!conn) {
      state.listError = list.reason?.message;
    }
    const podList = needPods ? (pods.status === "fulfilled" ? pods.value : null) : state.list;
    if (podList) state.unhealthy = podList.items.filter(isUnhealthyPod).length;
    if (!conn) state.updatedAt = new Date();

    drawAll();
    if (!conn) await detail.refresh();
  }

  const poller = createPoller(poll, POLL_MS);

  // ------------------------------------------------------------ 이벤트
  function openRow(tr) {
    if (!tr?.dataset.name) return;
    detail.open(state.kind, tr.dataset.name).then(drawTable);
    drawTable();
  }
  $("table").addEventListener("click", (e) => openRow(e.target.closest("tr[data-name]")));
  $("table").addEventListener("keydown", (e) => {
    if (e.key === "Enter") openRow(e.target.closest("tr[data-name]"));
  });
  $("pager").addEventListener("click", (e) => {
    const dir = e.target.closest("[data-page]")?.dataset.page;
    if (!dir || !state.list) return;
    state.offset = movePage(dir, state.list);
    poller.refreshNow();
  });
  el.querySelector("[data-action=refresh]").addEventListener("click", () => poller.refreshNow());

  const applySelector = debounce((v) => {
    state.selector = v.trim();
    state.offset = 0;
    state.list = null;
    drawTable();
    poller.refreshNow();
  }, 350);
  $("selector").addEventListener("input", (e) => applySelector(e.target.value));

  drawAll();
  poller.start();

  return {
    update(p) {
      const kind = KIND_BY_KEY[p[0]] ? p[0] : DEFAULT_KIND;
      if (kind === state.kind) return;
      state.kind = kind;
      state.offset = 0;
      state.list = null;
      state.listError = null;
      drawAll();
      poller.refreshNow();
    },
    unmount() {
      poller.stop();
      detail.destroy();
    },
  };
}
