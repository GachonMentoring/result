// 피드백 (Work 5): 댓글 분류 결과(재학습 데이터)를 표로 조회하고, 사람이 정답을 입력
//   DB: toxicity.comment_feedback — 분류할 때마다 자동 저장, 정답(true_label)은 한 번만 입력 가능
import { feedback } from "../api.js";
import { createPoller } from "../poller.js";
import { clampOffset, movePage, pager } from "../infra/pager.js";
import { toast } from "../toast.js";
import { html, render } from "../util.js";

const PAGE = 10;
const POLL_MS = 5000;
const FILTERS = [
  { key: "all", label: "전체", summary: "total", help: "저장된 모든 분류 결과" },
  { key: "unlabeled", label: "정답 미확인", summary: "unlabeled", help: "아직 사람이 정답을 입력하지 않음" },
  { key: "labeled", label: "정답 있음", summary: "labeled", help: "재학습에 쓸 수 있는 데이터" },
  { key: "wrong", label: "모델이 틀림", summary: "wrong", help: "모델 판정 ≠ 사람 정답 — 재학습으로 고쳐야 할 사례" },
];

const fmtTime = (iso) => {
  if (!iso) return "-";
  const d = new Date(iso);
  return `${d.toLocaleDateString("ko-KR", { month: "2-digit", day: "2-digit" })} ${d.toLocaleTimeString("ko-KR", { hour12: false })}`;
};
const labelBadge = (label, name) =>
  html`<span class="badge ${label ? "badge-bad" : "badge-ok"} badge-sm"><span class="dot"></span>${name}</span>`;

export function mount(el) {
  const state = { filter: "all", offset: 0, data: null, error: null, busy: new Set(), updatedAt: null };

  render(el, html`
    <header class="page-header">
      <div>
        <h1>피드백</h1>
        <p class="subtitle">댓글 분류 결과(재학습 데이터) · 사람이 확인한 정답을 입력합니다</p>
      </div>
      <div class="header-meta">
        <span class="muted small" data-slot="updated"></span>
        <button class="btn" data-action="refresh">⟳ 새로고침</button>
      </div>
    </header>
    <div data-slot="banner"></div>
    <section class="cards" role="tablist" aria-label="필터" data-slot="cards"></section>
    <section class="table-card">
      <div class="table-toolbar">
        <h2 data-slot="title"></h2>
        <div data-slot="pager"></div>
      </div>
      <div class="table-wrap" data-slot="table"></div>
    </section>`);

  const $ = (n) => el.querySelector(`[data-slot="${n}"]`);
  const last = new Map();
  const paint = (n, tpl) => { const s = String(tpl); if (last.get(n) !== s) { last.set(n, s); $(n).innerHTML = s; } };

  // ------------------------------------------------------------ 그리기
  function draw() {
    const d = state.data;
    const f = FILTERS.find((x) => x.key === state.filter);
    paint("banner", state.error ? html`<div class="banner banner-bad" role="alert"><strong>피드백을 불러올 수 없습니다</strong> ${state.error}</div>` : "");
    $("updated").textContent = state.updatedAt ? `${state.updatedAt.toLocaleTimeString("ko-KR", { hour12: false })} 갱신` : "";

    paint("cards", FILTERS.map((x) => html`
      <button class="card ${x.key === state.filter ? "active" : ""} ${x.key === "wrong" && d?.summary?.wrong ? "card-alert has-alert" : ""}"
              role="tab" aria-selected="${String(x.key === state.filter)}" data-filter="${x.key}" title="${x.help}">
        <span class="card-label">${x.label}</span>
        <span class="card-value">${d?.summary?.[x.summary] ?? "–"}</span>
      </button>`).map(String).join(""));

    paint("title", `${f.label}${d ? ` (${d.count})` : ""}`);
    paint("pager", d ? pager(d) : "");

    if (!d) { paint("table", html`<div class="empty">${state.error ? "-" : "불러오는 중…"}</div>`); return; }
    if (!d.items.length) {
      paint("table", html`<div class="empty">${state.filter === "all"
        ? "아직 저장된 분류 결과가 없습니다. 댓글 기능에서 댓글을 분류하면 자동으로 쌓입니다."
        : `${f.label} 항목이 없습니다.`}</div>`);
      return;
    }
    paint("table", html`
      <table class="fb-table">
        <thead><tr>
          <th scope="col">시각</th><th scope="col">댓글</th><th scope="col">모델 판정</th>
          <th scope="col" class="num">유해 확률</th><th scope="col">사람 정답</th><th scope="col">결과</th><th scope="col">모델 버전</th>
        </tr></thead>
        <tbody>${d.items.map((it) => html`
          <tr>
            <td class="mono small">${fmtTime(it.created_at)}</td>
            <td class="wrap fb-text">${it.text}</td>
            <td>${labelBadge(it.pred_label, it.pred_label_name)}</td>
            <td class="num mono">${it.toxic_proba.toFixed(3)}</td>
            <td>${it.true_label === null
              ? html`<span class="fb-label-btns">
                  <button class="btn btn-sm" data-label="0" data-id="${it.id}" ${state.busy.has(it.id) ? "disabled" : ""} title="정상 댓글이 맞음">정상</button>
                  <button class="btn btn-sm" data-label="1" data-id="${it.id}" ${state.busy.has(it.id) ? "disabled" : ""} title="주의 댓글이 맞음">주의</button>
                </span>`
              : html`${labelBadge(it.true_label, it.true_label_name)} <span class="muted small">${fmtTime(it.labeled_at)}</span>`}</td>
            <td>${it.correct === null ? html`<span class="muted">미확인</span>`
              : it.correct ? html`<span class="fb-ok">✓ 맞음</span>` : html`<span class="fb-wrong">✕ 틀림</span>`}</td>
            <td class="mono small muted">${it.model_version}</td>
          </tr>`)}
        </tbody>
      </table>`);
  }

  // ------------------------------------------------------------ 데이터
  async function load() {
    const { filter, offset } = state;
    try {
      const d = await feedback.list({ filter, limit: PAGE, offset });
      if (filter !== state.filter || offset !== state.offset) return;
      // 항목이 줄어 현재 페이지가 비면 마지막 페이지로
      const fixed = clampOffset(offset, PAGE, d.count);
      if (fixed !== offset) { state.offset = fixed; poller.refreshNow(); return; }
      state.data = d;
      state.error = null;
      state.updatedAt = new Date();
    } catch (e) {
      state.error = e.message;
    }
    draw();
  }

  async function setLabel(id, label) {
    state.busy.add(id);
    draw();
    try {
      const item = await feedback.label(id, label);
      toast(`정답 저장: ${item.true_label_name} — 모델 판정이 ${item.correct ? "맞았습니다" : "틀렸습니다"}`, item.correct ? "ok" : "warn", 3500);
    } catch (e) {
      toast(`정답 저장 실패: ${e.message}`, "bad", 6000);
    } finally {
      state.busy.delete(id);
      poller.refreshNow();
    }
  }

  el.addEventListener("click", (e) => {
    const t = e.target;
    const fBtn = t.closest("[data-filter]");
    if (fBtn) {
      if (fBtn.dataset.filter !== state.filter) {
        state.filter = fBtn.dataset.filter;
        state.offset = 0;
        state.data = null;
        draw();
        poller.refreshNow();
      }
      return;
    }
    const pBtn = t.closest("[data-page]");
    if (pBtn && state.data) {
      state.offset = movePage(pBtn.dataset.page, state.data);
      poller.refreshNow();
      return;
    }
    const lBtn = t.closest("[data-label]");
    if (lBtn) { setLabel(lBtn.dataset.id, Number(lBtn.dataset.label)); return; }
    if (t.closest("[data-action=refresh]")) poller.refreshNow();
  });

  const poller = createPoller(load, POLL_MS);
  draw();
  poller.start();
  return { unmount() { poller.stop(); } };
}
