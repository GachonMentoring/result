// 댓글 기능 (Work 4·5): 댓글 입력 → 플랫폼 서버 → 모델 서버 추론 → 판정 결과 표시
//   분류 결과는 자동으로 피드백 DB 에 저장되고(Work 5), 여기서 "맞아요/틀려요"로 사람 정답을 남긴다
import { comments, feedback } from "../api.js";
import { toast } from "../toast.js";
import { createPoller } from "../poller.js";
import { clock, html, render } from "../util.js";

const MAX = 300;
const STATUS_MS = 10000;
const HISTORY_MAX = 20;
const EXAMPLES = [
  "이 글 덕분에 이해가 잘 됐어요",
  "테스트 환경과 운영 환경을 분리하면 좋아요",
  "말이 너무 심하네요",
  "똑똑한 척 좀 그만하세요",
  "안녕하세요",
];

const pct = (p) => `${(p * 100).toFixed(1)}%`;

export function mount(el) {
  const state = {
    model: null,       // /api/v1/comments/model 응답
    modelError: null,  // 플랫폼 서버 연결 실패
    busy: false,
    result: null,      // 마지막 결과
    error: null,       // 마지막 요청 오류
    history: [],       // [{at, ...result}]
    labels: new Map(), // 결과 id → {true_label, correct} (이번 화면에서 입력한 정답)
    labeling: null,    // 정답 저장 중인 id
  };

  render(el, html`
    <header class="page-header">
      <div>
        <h1>댓글 기능</h1>
        <p class="subtitle">댓글을 입력하면 분류 모델이 <strong>정상</strong> / <strong>주의</strong>(비난·무례·공격적)를 판정합니다.</p>
      </div>
      <div class="header-meta" data-slot="model"></div>
    </header>
    <div data-slot="banner"></div>

    <section class="cm-card">
      <label class="sr-only" for="cm-text">댓글</label>
      <textarea id="cm-text" class="cm-input" rows="3" placeholder="댓글을 입력하세요 (최대 ${MAX}자)"
                data-slot="text" spellcheck="false"></textarea>
      <div class="cm-row">
        <div class="cm-examples" aria-label="예시 문장">
          <span class="muted small">예시</span>
          ${EXAMPLES.map((t) => html`<button class="chip-btn" data-example="${t}">${t}</button>`)}
        </div>
        <div class="cm-submit">
          <span class="muted small" data-slot="count"></span>
          <button class="btn btn-primary" data-action="submit" title="분류하기 (Ctrl+Enter)">분류하기</button>
        </div>
      </div>
    </section>

    <section class="cm-card" aria-live="polite" data-slot="result"></section>

    <section class="cm-card">
      <div class="cm-history-head">
        <h2>이번 화면 기록</h2>
        <span class="muted small">새로고침하면 사라집니다 · 전체 기록은 <a href="#/feedback">피드백</a> 메뉴</span>
      </div>
      <div data-slot="history"></div>
    </section>`);

  const $ = (n) => el.querySelector(`[data-slot="${n}"]`);
  const ta = $("text");
  const submitBtn = el.querySelector("[data-action=submit]");

  // ------------------------------------------------------------ 그리기
  function drawModel() {
    const m = state.model;
    let chip;
    if (state.modelError) chip = html`<span class="badge badge-bad"><span class="dot"></span>플랫폼 서버 연결 끊김</span>`;
    else if (!m) chip = html`<span class="badge badge-neutral"><span class="dot"></span>모델 확인 중</span>`;
    else if (!m.connected) chip = html`<span class="badge badge-bad" title="${m.url}"><span class="dot"></span>모델 연결 끊김</span>`;
    else {
      const info = m.info;
      chip = html`
        <span class="badge badge-ok" title="${m.url}"><span class="dot"></span>모델 연결됨</span>
        <span class="muted small mono" title="${info ? `학습 ${info.trained_at ?? "-"} · f1 ${info.metrics?.f1?.toFixed?.(3) ?? "-"}` : "구버전 모델 서버 (/info 없음)"}">
          ${info ? `${info.name} ${info.version}` : "모델 정보 없음"} · 임계값 ${m.threshold}</span>
        ${info?.version_mismatch ? html`<span class="badge badge-warn" title="학습 ${info.sklearn_version} / 서빙 ${info.runtime_sklearn_version}"><span class="dot"></span>버전 불일치</span>` : ""}`;
    }
    render($("model"), chip);

    const down = state.modelError || (m && !m.connected);
    render($("banner"), down ? html`
      <div class="banner banner-bad" role="alert">
        ${state.modelError
          ? html`<strong>플랫폼 서버 연결 실패</strong> ${state.modelError}`
          : html`<strong>모델 서버에 연결할 수 없습니다</strong> <span class="mono">${m.url}</span>`}
        — 연결되면 자동으로 사라집니다.
      </div>` : "");
  }

  function drawInput() {
    const n = ta.value.length;
    const over = n > MAX;
    const empty = !ta.value.trim();
    $("count").textContent = `${n}/${MAX}`;
    $("count").classList.toggle("over", over);
    submitBtn.disabled = state.busy || empty || over;
    submitBtn.textContent = state.busy ? "분류 중…" : "분류하기";
  }

  function drawResult() {
    const r = state.result;
    if (state.error) {
      render($("result"), html`<div class="banner banner-bad">${state.error}</div>`);
      return;
    }
    if (!r) {
      render($("result"), html`<div class="muted cm-empty">결과가 여기에 표시됩니다.</div>`);
      return;
    }
    const flagged = r.label === 1;
    render($("result"), html`
      <div class="cm-result">
        <div class="cm-verdict ${flagged ? "is-flagged" : "is-normal"}">
          <span class="cm-verdict-label">${flagged ? "주의" : "정상"}</span>
          <span class="cm-verdict-sub">${flagged ? "비난·무례·공격적인 댓글로 판정" : "일반 댓글로 판정"}</span>
        </div>
        <div class="cm-detail">
          <p class="cm-text">“${r.text}”</p>
          <div class="cm-prob">
            <div class="cm-prob-head">
              <span>유해 확률 <strong>${pct(r.toxic_proba)}</strong></span>
              <span class="muted small">임계값 ${r.threshold} 이상이면 주의</span>
            </div>
            <div class="cm-bar" role="img" aria-label="유해 확률 ${pct(r.toxic_proba)}, 임계값 ${r.threshold}">
              <div class="cm-bar-fill ${flagged ? "is-flagged" : "is-normal"}" style="width:${(r.toxic_proba * 100).toFixed(1)}%"></div>
              <div class="cm-bar-th" style="left:${r.threshold * 100}%"></div>
            </div>
          </div>
          ${r.uncertain ? html`<p class="cm-uncertain">⚠ 확률이 0.4~0.6 사이라 모델이 확신하지 못하는 문장입니다. 피드백으로 보강하기 좋은 후보입니다.</p>` : ""}
          <p class="muted small">${r.model ? `${r.model.name} ${r.model.version}` : "모델 정보 없음"} · ${r.latency_ms}ms</p>
          ${feedbackBox(r)}
        </div>
      </div>`);
  }

  // 판정이 맞았는지 사람이 알려줌 → DB true_label (재학습 라벨)
  function feedbackBox(r) {
    if (!r.saved) {
      return html`<div class="cm-feedback is-off">⚠ 피드백 DB 에 저장되지 않았습니다 — ${r.save_error ?? "알 수 없는 오류"}</div>`;
    }
    const done = state.labels.get(r.id);
    if (done) {
      return html`<div class="cm-feedback is-done">
        정답 저장됨: <strong>${done.true_label ? "주의" : "정상"}</strong> — 모델 판정이 ${done.correct ? "맞았습니다 ✓" : "틀렸습니다 ✕ (재학습 데이터로 쓰입니다)"}
      </div>`;
    }
    const busy = state.labeling === r.id;
    return html`<div class="cm-feedback">
      <span>이 판정이 맞나요?</span>
      <button class="btn btn-sm" data-verdict="yes" ${busy ? "disabled" : ""}>맞아요</button>
      <button class="btn btn-sm" data-verdict="no" ${busy ? "disabled" : ""}>틀려요</button>
      <span class="muted small">DB 에 저장됨 · 정답은 한 번만 입력할 수 있어요</span>
    </div>`;
  }

  function drawHistory() {
    if (!state.history.length) {
      render($("history"), html`<div class="muted cm-empty">아직 분류한 댓글이 없습니다.</div>`);
      return;
    }
    render($("history"), html`
      <ul class="cm-history">
        ${state.history.map((h, i) => html`
          <li>
            <button class="cm-history-item" data-history="${i}" title="입력창에 다시 넣기">
              <span class="mono muted small">${clock(h.at)}</span>
              <span class="cm-history-text">${h.text}</span>
              ${h.uncertain ? html`<span class="badge badge-warn badge-sm" title="모델이 헷갈림 (0.4~0.6)">경계</span>` : ""}
              ${!h.saved ? html`<span class="muted small" title="${h.save_error ?? ""}">미저장</span>`
                : state.labels.has(h.id) ? html`<span class="small ${state.labels.get(h.id).correct ? "fb-ok" : "fb-wrong"}">${state.labels.get(h.id).correct ? "✓ 맞음" : "✕ 틀림"}</span>` : ""}
              <span class="badge ${h.label ? "badge-bad" : "badge-ok"} badge-sm"><span class="dot"></span>${h.label_name}</span>
              <span class="mono small cm-history-p">${h.toxic_proba.toFixed(3)}</span>
            </button>
          </li>`)}
      </ul>`);
  }

  // ------------------------------------------------------------ 동작
  async function loadModel() {
    try {
      state.model = await comments.model();
      state.modelError = null;
    } catch (e) {
      state.modelError = e.message;
    }
    drawModel();
  }

  async function submit() {
    const text = ta.value;
    if (state.busy || !text.trim() || text.length > MAX) return;
    state.busy = true;
    state.error = null;
    drawInput();
    try {
      const r = await comments.predict(text);
      state.result = r;
      state.history = [{ at: new Date(), ...r }, ...state.history].slice(0, HISTORY_MAX);
      drawHistory();
    } catch (e) {
      state.result = null;
      state.error = e.message;
      if (e.status === 503 || e.status === 0) poller.refreshNow(); // 연결 상태 배너 갱신
    } finally {
      state.busy = false;
      drawInput();
      drawResult();
      ta.focus();
    }
  }

  ta.addEventListener("input", drawInput);
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && !e.isComposing) {
      e.preventDefault();
      submit();
    }
  });
  async function sendVerdict(correct) {
    const r = state.result;
    if (!r?.saved || state.labeling || state.labels.has(r.id)) return;
    const trueLabel = correct ? r.label : 1 - r.label;
    state.labeling = r.id;
    drawResult();
    try {
      const item = await feedback.label(r.id, trueLabel);
      state.labels.set(r.id, { true_label: item.true_label, correct: item.correct });
    } catch (e) {
      if (e.status === 409) state.labels.set(r.id, { true_label: trueLabel, correct });
      toast(`정답 저장 실패: ${e.message}`, e.status === 409 ? "warn" : "bad", 6000);
    } finally {
      state.labeling = null;
      drawResult();
      drawHistory();
    }
  }

  el.addEventListener("click", (e) => {
    if (e.target.closest("[data-action=submit]")) { submit(); return; }
    const verdict = e.target.closest("[data-verdict]");
    if (verdict) { sendVerdict(verdict.dataset.verdict === "yes"); return; }
    const ex = e.target.closest("[data-example]");
    const hi = e.target.closest("[data-history]");
    const fill = ex?.dataset.example ?? (hi ? state.history[Number(hi.dataset.history)]?.text : null);
    if (fill != null) {
      ta.value = fill;
      drawInput();
      ta.focus();
    }
  });

  const poller = createPoller(loadModel, STATUS_MS);
  drawModel();
  drawInput();
  drawResult();
  drawHistory();
  poller.start();
  ta.focus();

  return { unmount() { poller.stop(); } };
}
