// 모델 재학습: 하위 메뉴 [데이터셋 재생성] · [모델 재학습] · [모델 교체]
//   #/retrain/dataset (기본) · #/retrain/train · #/retrain/deploy
//   데이터셋 재생성 (k8sapimd/dataset.md): 버튼 → 쿠버네티스 Job → NFS /home/suhan/csv 에 seed + DB 피드백 CSV
//   모델 재학습 (k8sapimd/train.md): 데이터셋 선택 → 학습 Job → MLflow 등록 모델 새 버전
//   모델 교체 (k8sapimd/train.md 7장): 워크로드 YAML 의 MODEL_URI 한 줄 변경 → 적용 → 롤링 업데이트
import { datasets, feedback, models } from "../api.js";
import { openDialog } from "../infra/confirm.js";
import { createPoller } from "../poller.js";
import { toast } from "../toast.js";
import { age, html, render } from "../util.js";

const TABS = [
  { key: "dataset", label: "데이터셋 재생성" },
  { key: "train", label: "모델 재학습" },
  { key: "deploy", label: "모델 교체" },
];
const POLL_MS = 3000;

const fmtTime = (iso) => {
  if (!iso) return "-";
  const d = new Date(iso);
  return `${d.toLocaleDateString("ko-KR", { month: "2-digit", day: "2-digit" })} ${d.toLocaleTimeString("ko-KR", { hour12: false })}`;
};
const counts = (lc) => html`<span class="mono">${lc?.["0"] ?? 0}</span> / <span class="mono">${lc?.["1"] ?? 0}</span>`;

const JOB_BADGE = {
  Pending: ["badge-neutral", "대기"], Running: ["badge-warn", "실행 중"],
  Complete: ["badge-ok", "완료"], Failed: ["badge-bad", "실패"],
};

export function mount(el, params) {
  render(el, html`
    <header class="page-header">
      <div>
        <h1>모델 재학습</h1>
        <p class="subtitle">데이터셋 재생성 → 모델 재학습 → 모델 교체</p>
      </div>
    </header>
    <nav class="detail-tabs rt-tabs" role="tablist" aria-label="모델 재학습 메뉴" data-slot="tabs"></nav>
    <div data-slot="body"></div>`);

  const tabsEl = el.querySelector('[data-slot="tabs"]');
  const bodyEl = el.querySelector('[data-slot="body"]');
  let tab = null;
  let sub = null;   // 현재 하위 화면 { unmount }

  function show(p) {
    const next = TABS.some((t) => t.key === p[0]) ? p[0] : "dataset";
    if (next === tab) return;
    tab = next;
    render(tabsEl, TABS.map((t) => html`
      <a class="tab ${t.key === tab ? "active" : ""}" role="tab" aria-selected="${String(t.key === tab)}"
         href="#/retrain/${t.key}">${t.label}</a>`));
    sub?.unmount?.();
    const host = document.createElement("div");
    bodyEl.replaceChildren(host);
    sub = tab === "train" ? mountTrain(host) : tab === "deploy" ? mountDeploy(host) : mountDataset(host);
  }

  show(params);
  return {
    update: show,
    unmount() { sub?.unmount?.(); },
  };
}

// ---------------------------------------------------------------- 데이터셋 재생성
function mountDataset(el) {
  const state = { data: null, jobs: null, labeled: undefined, error: null, busy: false, open: null, detail: null };

  render(el, html`
    <div data-slot="banner"></div>
    <section class="table-card rt-panel">
      <div class="rt-action">
        <div>
          <h2>데이터셋 생성</h2>
          <p class="muted small" data-slot="sources"></p>
        </div>
        <button class="btn btn-primary" data-action="create">데이터셋 생성</button>
      </div>
      <div data-slot="jobs"></div>
    </section>
    <section class="table-card">
      <div class="table-toolbar">
        <h2 data-slot="title">데이터셋</h2>
        <span class="muted small mono" data-slot="root"></span>
      </div>
      <div class="table-wrap" data-slot="table"></div>
    </section>
    <section class="table-card rt-preview" data-slot="detail" hidden></section>`);

  const $ = (n) => el.querySelector(`[data-slot="${n}"]`);
  const last = new Map();
  const paint = (n, tpl) => { const s = String(tpl); if (last.get(n) !== s) { last.set(n, s); $(n).innerHTML = s; } };

  function draw() {
    const d = state.data;
    paint("banner", state.error ? html`<div class="banner banner-bad" role="alert"><strong>불러올 수 없습니다</strong> ${state.error}</div>` : "");

    const seed = d?.seed;
    paint("sources", d ? html`
      seed: ${seed?.exists ? html`<strong>${seed.rows ?? "?"}행</strong> (${seed.file})` : html`<span class="rt-warn">seed.csv 없음 — DB 데이터만으로 생성됩니다</span>`}
      · 정답이 입력된 피드백: <strong>${state.labeled === undefined ? "…" : state.labeled === null ? "DB 연결 안 됨" : `${state.labeled}건`}</strong>
      · seed 행 다음에 피드백 행을 이어 붙여 CSV 하나로 저장 (정리 없이 그대로)` : "불러오는 중…");

    const running = d?.running;
    const btn = el.querySelector('[data-action="create"]');
    btn.disabled = state.busy || !!running || !d;
    btn.textContent = running ? "생성 중…" : "데이터셋 생성";

    // 최근 Job (끝나고 TTL 동안만 남음)
    const jobs = state.jobs?.items ?? [];
    const ttlMin = Math.round((state.jobs?.ttl_seconds ?? d?.ttl_seconds ?? 600) / 60);
    paint("jobs", jobs.length ? html`
      <table class="rt-jobs">
        <thead><tr><th>Job</th><th>상태</th><th>시작</th><th>결과</th></tr></thead>
        <tbody>${jobs.map((j) => {
          const [cls, label] = JOB_BADGE[j.state] ?? ["badge-neutral", j.state];
          const r = j.result;
          return html`<tr>
            <td class="mono small">${j.name}</td>
            <td><span class="badge ${cls} badge-sm"><span class="dot"></span>${label}</span>
                ${j.waiting ? html` <span class="muted small">${j.waiting}</span>` : ""}</td>
            <td class="small muted">${age(j.start_time || j.created_at)} 전</td>
            <td class="wrap small">${!r ? html`<span class="muted">-</span>`
              : r.ok ? html`<span class="fb-ok">${r.name}</span> · ${r.rows}행 (seed ${r.seed ?? "없음"} + DB ${r.db})`
              : html`<span class="fb-wrong">${r.error}</span>`}</td>
          </tr>`;
        })}</tbody>
      </table>
      <p class="muted small pad">끝난 Job 은 ${ttlMin}분 뒤 자동 삭제됩니다 (만들어진 CSV 는 남음).</p>`
      : html`<p class="muted small pad">최근 실행한 Job 이 없습니다.</p>`);

    paint("root", d?.root ?? "");
    paint("title", `데이터셋${d ? ` (${d.count})` : ""}`);
    if (!d) { paint("table", html`<div class="empty">${state.error ? "-" : "불러오는 중…"}</div>`); return; }
    paint("table", d.items.length ? html`
      <table>
        <thead><tr>
          <th>이름</th><th class="num">전체</th><th class="num">seed</th><th class="num">DB</th>
          <th>정상 / 주의</th><th>생성 시각</th>
        </tr></thead>
        <tbody>${d.items.map((it) => html`
          <tr tabindex="0" data-name="${it.name}" class="${state.open === it.name ? "selected" : ""}">
            <td class="mono"><span class="name">${it.name}</span></td>
            <td class="num">${it.rows}</td>
            <td class="num">${it.sources?.seed ? it.sources.seed.rows : html`<span class="muted">없음</span>`}</td>
            <td class="num">${it.sources?.db?.rows ?? "-"}</td>
            <td>${counts(it.label_counts)}</td>
            <td class="small">${fmtTime(it.created_at)}</td>
          </tr>`)}</tbody>
      </table>`
      : html`<div class="empty">아직 만든 데이터셋이 없습니다. [데이터셋 생성]을 누르세요.</div>`);

    drawDetail();
  }

  function drawDetail() {
    const box = $("detail");
    const x = state.detail;
    box.hidden = !state.open;
    if (!state.open) return;
    paint("detail", !x ? html`<div class="empty">불러오는 중…</div>` : x.error
      ? html`<div class="banner banner-bad">${x.error}</div>`
      : html`
        <div class="table-toolbar">
          <h2 class="mono">${x.name}</h2>
          <button class="icon-btn" data-action="close" aria-label="닫기">✕</button>
        </div>
        <dl class="kv pad">
          <dt>파일</dt><dd class="mono">${x.file}</dd>
          <dt>행</dt><dd>${x.rows} (seed ${x.sources?.seed?.rows ?? "없음"} + DB ${x.sources?.db?.rows ?? 0})</dd>
          <dt>정상 / 주의</dt><dd>${counts(x.label_counts)}</dd>
          <dt>생성</dt><dd>${fmtTime(x.created_at)} · Job <span class="mono">${x.job ?? "-"}</span></dd>
          <dt>sha256</dt><dd class="mono small">${x.sha256}</dd>
          ${x.sources?.seed ? html`<dt>seed sha256</dt><dd class="mono small">${x.sources.seed.sha256}</dd>` : ""}
        </dl>
        <h3 class="pad rt-sub">미리보기 (처음 ${x.preview.length}행)</h3>
        <div class="table-wrap">
          <table class="fb-table">
            <thead><tr><th>label</th><th>text</th></tr></thead>
            <tbody>${x.preview.map((r) => html`<tr><td class="mono">${r.label}</td><td class="wrap">${r.text}</td></tr>`)}</tbody>
          </table>
        </div>`);
  }

  async function load() {
    try {
      const [d, j] = await Promise.all([datasets.list(), datasets.jobs()]);
      // 실행 중이던 Job 이 끝나면 알림
      const before = new Map((state.jobs?.items ?? []).map((x) => [x.name, x.state]));
      for (const x of j.items) {
        const prev = before.get(x.name);
        if (prev && prev !== x.state && (x.state === "Complete" || x.state === "Failed")) {
          if (x.result?.ok) toast(`데이터셋 생성 완료: ${x.result.name} (${x.result.rows}행)`, "ok", 5000);
          else toast(`데이터셋 생성 실패: ${x.result?.error ?? x.name}`, "bad", 8000);
        }
      }
      state.data = d;
      state.jobs = j;
      state.error = null;
    } catch (e) {
      state.error = e.message;
    }
    draw();
  }

  async function loadLabeled() {
    try {
      const f = await feedback.list({ filter: "labeled", limit: 1 });
      state.labeled = f.summary?.labeled ?? f.count;
    } catch {
      state.labeled = null;   // 피드백 DB 미설정 — 생성은 가능 (Job 이 직접 DB 에 접속)
    }
    draw();
  }

  async function create() {
    state.busy = true;
    draw();
    try {
      const r = await datasets.create();
      toast(`데이터셋 생성 Job 시작: ${r.job}`, "neutral", 4000);
    } catch (e) {
      toast(`데이터셋 생성 실패: ${e.message}`, "bad", 8000);
    } finally {
      state.busy = false;
      poller.refreshNow();
    }
  }

  async function openDetail(name) {
    if (state.open === name) { state.open = null; state.detail = null; draw(); return; }
    state.open = name;
    state.detail = null;
    draw();
    try {
      const x = await datasets.get(name);
      if (state.open === name) state.detail = x;
    } catch (e) {
      if (state.open === name) state.detail = { error: e.message };
    }
    draw();
  }

  el.addEventListener("click", (e) => {
    if (e.target.closest('[data-action="create"]')) { create(); return; }
    if (e.target.closest('[data-action="close"]')) { state.open = null; state.detail = null; draw(); return; }
    const row = e.target.closest("tr[data-name]");
    if (row) openDetail(row.dataset.name);
  });
  el.addEventListener("keydown", (e) => {
    const row = e.target.closest?.("tr[data-name]");
    if (row && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); openDetail(row.dataset.name); }
  });

  const poller = createPoller(load, POLL_MS);
  draw();
  poller.start();
  loadLabeled();
  const labeledTimer = setInterval(loadLabeled, 15000);
  return { unmount() { poller.stop(); clearInterval(labeledTimer); } };
}

// ---------------------------------------------------------------- 공통 (재학습 · 교체)
const f1 = (v) => (v === null || v === undefined ? "-" : Number(v).toFixed(4));
const int = (v) => (v === null || v === undefined ? "-" : String(Math.round(v)));
const versionLink = (links, v) => links?.mlflow_ui
  ? html`<a class="small" href="${links.mlflow_ui}/#/models/toxicity-filter/versions/${v}" target="_blank" rel="noopener">MLflow ↗</a>`
  : "";
const ROLLOUT = {
  complete: ["badge-ok", "완료"], progressing: ["badge-warn", "진행 중"],
  failed: ["badge-bad", "실패 — 이전 모델 계속 서빙"], unknown: ["badge-neutral", "알 수 없음"],
};

function painter(el) {
  const last = new Map();
  const $ = (n) => el.querySelector(`[data-slot="${n}"]`);
  const paint = (n, tpl) => { const s = String(tpl); if (last.get(n) !== s) { last.set(n, s); $(n).innerHTML = s; } };
  return { $, paint };
}

function versionFlags(v) {
  return html`${v.champion ? html`<span class="badge badge-ok badge-sm">champion</span> ` : ""}${
    v.declared ? html`<span class="badge badge-neutral badge-sm">선언</span>` : ""}`;
}

// ---------------------------------------------------------------- 모델 재학습
function mountTrain(el) {
  const state = { ds: null, data: null, jobs: null, error: null, busy: false, selected: "" };

  render(el, html`
    <div data-slot="banner"></div>
    <section class="table-card rt-panel">
      <div class="rt-action">
        <div>
          <h2>모델 학습</h2>
          <p class="muted small">데이터셋을 골라 학습 → 고정 평가셋(eval.csv)으로 채점 → MLflow 등록 모델 <span class="mono">toxicity-filter</span> 의 새 버전으로 등록</p>
        </div>
        <div class="rt-train-form">
          <select data-slot="select" aria-label="학습할 데이터셋"></select>
          <button class="btn btn-primary" data-action="train">학습 시작</button>
        </div>
      </div>
      <p class="muted small pad" data-slot="info"></p>
      <div data-slot="jobs"></div>
    </section>
    <section class="table-card">
      <div class="table-toolbar">
        <h2 data-slot="title">등록된 버전</h2>
        <span data-slot="mlflow"></span>
      </div>
      <div class="table-wrap" data-slot="table"></div>
    </section>`);

  const { $, paint } = painter(el);
  const select = $("select");

  function options() {
    const items = state.ds?.items ?? [];
    const opts = items.map((d) => ({
      value: d.name,
      label: `${d.name} · ${d.rows}행 (seed ${d.sources?.seed?.rows ?? 0} + DB ${d.sources?.db?.rows ?? 0})`,
    }));
    if (state.ds?.seed?.exists) opts.push({ value: "seed", label: `seed · ${state.ds.seed.rows}행 (기준 모델용)` });
    return opts;
  }

  function draw() {
    const d = state.data;
    paint("banner", state.error ? html`<div class="banner banner-bad" role="alert"><strong>불러올 수 없습니다</strong> ${state.error}</div>`
      : d && !d.template?.ok ? html`<div class="banner banner-warn">Job 명세 파일 문제: ${d.template?.error}</div>`
      : d && !d.eval?.exists ? html`<div class="banner banner-warn">고정 평가셋 <span class="mono">eval.csv</span> 가 데이터셋 폴더에 없습니다 — 학습할 수 없습니다 (사람이 1회 배치).</div>`
      : "");

    const opts = options();
    if (!state.selected || !opts.some((o) => o.value === state.selected)) state.selected = opts[0]?.value ?? "";
    paint("select", opts.length
      ? opts.map((o) => html`<option value="${o.value}" ${o.value === state.selected ? "selected" : ""}>${o.label}</option>`)
      : html`<option value="">데이터셋 없음</option>`);

    const running = d?.running;
    const btn = el.querySelector('[data-action="train"]');
    btn.disabled = state.busy || !!running || !d || !state.selected || !d.eval?.exists || !d.template?.ok;
    btn.textContent = running ? "학습 중…" : "학습 시작";

    paint("info", html`정리 규칙: 평가셋 문장 제외 → 같은 문장은 마지막(최신 피드백) 유지 → 라벨별 최소 20행 · DB 피드백 행 가중치 3
      (Job 명세 파일 <span class="mono">${d?.template?.file ?? "job/train.yaml"}</span> 에서 변경)`);

    const jobs = state.jobs?.items ?? [];
    const ttlMin = Math.round((state.jobs?.ttl_seconds ?? 600) / 60);
    paint("jobs", jobs.length ? html`
      <table class="rt-jobs">
        <thead><tr><th>Job</th><th>상태</th><th>시작</th><th>결과</th></tr></thead>
        <tbody>${jobs.map((j) => {
          const [cls, label] = JOB_BADGE[j.state] ?? ["badge-neutral", j.state];
          const r = j.result;
          return html`<tr>
            <td class="mono small">${j.name}</td>
            <td><span class="badge ${cls} badge-sm"><span class="dot"></span>${label}</span>
                ${j.waiting ? html` <span class="muted small">${j.waiting}</span>` : ""}</td>
            <td class="small muted">${age(j.start_time || j.created_at)} 전</td>
            <td class="wrap small">${!r ? html`<span class="muted">-</span>`
              : r.ok ? html`<span class="fb-ok">버전 ${r.version} 등록</span> · ${r.dataset} · eval F1 <span class="mono">${f1(r.eval_f1)}</span> · 학습 ${r.n_train}행 (피드백 ${r.rows_feedback})`
              : html`<span class="fb-wrong">${r.error}</span>`}</td>
          </tr>`;
        })}</tbody>
      </table>
      <p class="muted small pad">끝난 Job 은 ${ttlMin}분 뒤 자동 삭제됩니다 (학습 기록·모델은 MLflow 에 남음).</p>`
      : html`<p class="muted small pad">최근 실행한 학습 Job 이 없습니다.</p>`);

    paint("mlflow", d?.links?.model_page
      ? html`<a class="small" href="${d.links.model_page}" target="_blank" rel="noopener">MLflow 에서 비교 ↗</a>` : "");
    paint("title", `등록된 버전${d ? ` (${d.count})` : ""}`);
    if (!d) { paint("table", html`<div class="empty">${state.error ? "-" : "불러오는 중…"}</div>`); return; }
    paint("table", d.items.length ? html`
      <table>
        <thead><tr>
          <th>버전</th><th>데이터셋</th><th class="num">eval F1</th><th class="num">학습 행</th><th class="num">피드백</th>
          <th>sklearn</th><th>상태</th><th>등록</th><th></th>
        </tr></thead>
        <tbody>${d.items.map((v) => html`
          <tr>
            <td class="mono"><strong>${v.version}</strong></td>
            <td class="mono small">${v.dataset ?? "-"}</td>
            <td class="num mono">${f1(v.eval_f1)}</td>
            <td class="num">${int(v.n_train)}</td>
            <td class="num">${int(v.rows_feedback)}</td>
            <td class="small ${v.compatible === false ? "fb-wrong" : ""}">${v.sklearn_version ?? "-"}</td>
            <td>${versionFlags(v)}${v.status !== "READY" ? html` <span class="muted small">${v.status}</span>` : ""}</td>
            <td class="small">${fmtTime(v.created_at)}</td>
            <td>${versionLink(d.links, v.version)}</td>
          </tr>`)}</tbody>
      </table>`
      : html`<div class="empty">아직 등록된 버전이 없습니다. 처음이면 <strong>seed</strong> 로 학습해 기준 모델(버전 1)을 만드세요.</div>`);
  }

  async function load() {
    try {
      const [ds, d, j] = await Promise.all([datasets.list(), models.list(), models.trainJobs()]);
      const before = new Map((state.jobs?.items ?? []).map((x) => [x.name, x.state]));
      for (const x of j.items) {
        const prev = before.get(x.name);
        if (prev && prev !== x.state && (x.state === "Complete" || x.state === "Failed")) {
          if (x.result?.ok) toast(`학습 완료: 버전 ${x.result.version} 등록 (eval F1 ${f1(x.result.eval_f1)})`, "ok", 6000);
          else toast(`학습 실패: ${x.result?.error ?? x.name}`, "bad", 8000);
        }
      }
      Object.assign(state, { ds, data: d, jobs: j, error: null });
    } catch (e) {
      state.error = e.message;
    }
    draw();
  }

  async function train() {
    const name = state.selected;
    if (!name) return;
    state.busy = true;
    draw();
    try {
      const r = await models.train(name);
      toast(`학습 Job 시작: ${r.job} (${r.dataset})`, "neutral", 4000);
    } catch (e) {
      toast(`학습 시작 실패: ${e.message}`, "bad", 8000);
    } finally {
      state.busy = false;
      poller.refreshNow();
    }
  }

  select.addEventListener("change", () => { state.selected = select.value; });
  el.addEventListener("click", (e) => { if (e.target.closest('[data-action="train"]')) train(); });

  const poller = createPoller(load, POLL_MS);
  draw();
  poller.start();
  return { unmount() { poller.stop(); } };
}

// ---------------------------------------------------------------- 모델 교체
function mountDeploy(el) {
  const state = { st: null, list: null, error: null, busy: false, selected: "" };

  render(el, html`
    <div data-slot="banner"></div>
    <section class="table-card rt-panel">
      <div class="table-toolbar"><h2>현재 상태</h2><span data-slot="links"></span></div>
      <dl class="kv pad" data-slot="status"></dl>
      <div class="rt-action rt-deploy-actions">
        <div data-slot="compare" class="small"></div>
        <div class="rt-train-form">
          <select data-slot="select" aria-label="교체할 버전"></select>
          <button class="btn btn-primary" data-action="deploy">교체</button>
          <button class="btn btn-danger-outline" data-action="rollback" hidden>되돌리기</button>
        </div>
      </div>
      <p class="muted small pad">교체 = NFS <span class="mono" data-slot="file">workloads/toxicity-filter.yaml</span> 의
        <span class="mono">MODEL_URI</span> 한 줄을 바꿔 적용 → 새 파드가 MLflow 에서 모델을 받아 Ready 가 되면 옛 파드가 내려갑니다.
        실패하면 옛 파드가 계속 서빙합니다.</p>
    </section>`);

  const { $, paint } = painter(el);
  const select = $("select");

  const current = () => state.list?.items?.find((v) => v.version === state.st?.declared?.version);

  function draw() {
    const st = state.st;
    const decl = st?.declared;
    const ro = st?.rollout;
    const notMlflow = decl?.error;
    paint("banner", state.error ? html`<div class="banner banner-bad" role="alert"><strong>불러올 수 없습니다</strong> ${state.error}</div>`
      : notMlflow ? html`<div class="banner banner-warn"><strong>교체할 수 없습니다</strong> ${decl.error}</div>`
      : st?.mlflow_error ? html`<div class="banner banner-warn">MLflow: ${st.mlflow_error}</div>` : "");
    paint("links", st?.links?.model_page
      ? html`<a class="small" href="${st.links.model_page}" target="_blank" rel="noopener">MLflow ↗</a>` : "");
    paint("file", decl?.file ?? "workloads/toxicity-filter.yaml");

    if (!st) {
      paint("status", html`<dt>상태</dt><dd>${state.error ? "-" : "불러오는 중…"}</dd>`);
    } else {
      const [cls, label] = ROLLOUT[ro.state] ?? ["badge-neutral", ro.state];
      const servingVer = st.serving.version;
      const match = decl.version && servingVer === `${st.model}/${decl.version}` && ro.state === "complete";
      paint("status", html`
        <dt>선언 (YAML)</dt><dd class="mono">${decl.uri ?? "-"}</dd>
        <dt>실제 서빙</dt><dd><span class="mono">${servingVer ?? "-"}</span>
          ${st.serving.connected ? (match ? html` <span class="fb-ok">✔ 선언과 일치</span>` : "") : html` <span class="fb-wrong">모델 서버 연결 안 됨</span>`}
          ${st.serving.sklearn ? html` <span class="muted small">· scikit-learn ${st.serving.sklearn}</span>` : ""}</dd>
        <dt>롤아웃</dt><dd><span class="badge ${cls} badge-sm"><span class="dot"></span>${label}</span>
          ${ro.reason ? html` <span class="muted small">${ro.reason}</span>` : ""}
          ${ro.total ? html` <span class="muted small">· 파드 ${ro.ready}/${ro.replicas} Ready (전체 ${ro.total})</span>` : ""}</dd>
        <dt>champion</dt><dd>${st.champion
          ? html`<span class="mono">${st.champion}</span> <span class="muted small">(마지막으로 교체가 끝난 버전 · 되돌리기 기준)</span>`
          : html`<span class="muted">없음</span>`}</dd>`);
    }

    const items = (state.list?.items ?? []).filter((v) => v.version !== decl?.version && v.status === "READY");
    if (!state.selected || !items.some((v) => v.version === state.selected)) state.selected = items[0]?.version ?? "";
    paint("select", items.length
      ? items.map((v) => html`<option value="${v.version}" ${v.version === state.selected ? "selected" : ""}>버전 ${v.version} · ${v.dataset ?? "-"} · F1 ${f1(v.eval_f1)}</option>`)
      : html`<option value="">교체할 다른 버전 없음</option>`);

    const target = items.find((v) => v.version === state.selected);
    const cur = current();
    const progressing = ro?.state === "progressing";
    let delta = "";
    if (target && cur && target.eval_f1 != null && cur.eval_f1 != null) {
      const d = target.eval_f1 - cur.eval_f1;
      delta = html` (현재 ${cur.version} 대비 <span class="${d < 0 ? "rt-warn" : "fb-ok"}">${d >= 0 ? "+" : ""}${d.toFixed(4)}</span>${d < 0 ? " ⚠" : ""})`;
    }
    paint("compare", !target ? "" : html`
      버전 <strong>${target.version}</strong> eval F1 <span class="mono">${f1(target.eval_f1)}</span>${delta}
      ${target.same_eval === false ? html`<br><span class="rt-warn">다른 평가셋으로 채점됨 — 지표 비교 불가</span>` : ""}
      ${target.compatible === false ? html`<br><span class="fb-wrong">scikit-learn ${target.sklearn_version} ≠ 서빙 ${st?.serving?.sklearn} — 교체 불가</span>` : ""}`);

    const dbtn = el.querySelector('[data-action="deploy"]');
    dbtn.disabled = state.busy || !st || !!notMlflow || !target || target.compatible === false || progressing;
    dbtn.textContent = progressing ? "교체 진행 중…" : "교체";
    const rbtn = el.querySelector('[data-action="rollback"]');
    rbtn.hidden = !(st && !notMlflow && st.champion && st.champion !== decl?.version && ro?.state !== "complete");
    rbtn.disabled = state.busy;
    rbtn.textContent = st?.champion ? `되돌리기 (버전 ${st.champion})` : "되돌리기";
  }

  async function load() {
    try {
      const [st, list] = await Promise.all([models.deployState(), models.list()]);
      const prev = state.st?.rollout?.state;
      if (prev && prev !== st.rollout.state) {
        if (st.rollout.state === "complete") toast(`교체 완료: ${st.declared.uri}`, "ok", 6000);
        if (st.rollout.state === "failed") toast(`교체 실패 (${st.rollout.reason}) — 이전 모델이 계속 서빙 중`, "bad", 10000);
      }
      Object.assign(state, { st, list, error: null });
    } catch (e) {
      state.error = e.message;
    }
    draw();
  }

  async function run(fn, okMsg) {
    state.busy = true;
    draw();
    try {
      toast(okMsg(await fn()), "neutral", 5000);
    } catch (e) {
      toast(`실패: ${e.message}`, "bad", 10000);
    } finally {
      state.busy = false;
      poller.refreshNow();
    }
  }

  async function deploy() {
    const target = state.list?.items?.find((v) => v.version === state.selected);
    if (!target) return;
    const cur = current();
    const lower = cur && target.eval_f1 != null && cur.eval_f1 != null && target.eval_f1 < cur.eval_f1;
    const { value } = await openDialog({
      title: "모델을 교체하시겠습니까?",
      target: `${state.st.declared.uri} → models:/${state.st.model}/${target.version}`,
      body: html`
        <p>워크로드 YAML 의 버전 번호를 바꿔 저장하고 클러스터에 적용합니다. 댓글 분류가 새 모델로 바뀝니다.</p>
        <ul>
          <li>새 버전: 데이터셋 <span class="mono">${target.dataset ?? "-"}</span>, eval F1 <span class="mono">${f1(target.eval_f1)}</span>${cur ? html` (현재 ${f1(cur.eval_f1)})` : ""}</li>
          ${lower ? html`<li class="rt-warn">현재 모델보다 eval F1 이 낮습니다.</li>` : ""}
          ${target.same_eval === false ? html`<li class="rt-warn">다른 평가셋으로 채점된 버전입니다.</li>` : ""}
          <li>새 파드가 준비되지 못하면 옛 파드가 계속 서빙합니다 (무중단).</li>
        </ul>`,
      buttons: [{ value: "no", label: "아니오", autofocus: true }, { value: "yes", label: "교체", cls: "btn btn-primary" }],
    });
    if (value !== "yes") return;
    await run(() => models.deploy(target.version), (r) => `교체 적용: 버전 ${r.from} → ${r.to} (롤링 업데이트 진행)`);
  }

  async function rollback() {
    const { value } = await openDialog({
      title: "되돌리시겠습니까?",
      target: `${state.st.declared.uri} → models:/${state.st.model}/${state.st.champion}`,
      body: html`<p>마지막으로 교체가 끝났던 버전(champion)으로 워크로드 YAML 을 되돌려 적용합니다.</p>`,
      buttons: [{ value: "no", label: "아니오", autofocus: true }, { value: "yes", label: "되돌리기", cls: "btn btn-danger" }],
    });
    if (value !== "yes") return;
    await run(() => models.rollback(), (r) => `되돌리기 적용: 버전 ${r.from} → ${r.to}`);
  }

  select.addEventListener("change", () => { state.selected = select.value; draw(); });
  el.addEventListener("click", (e) => {
    if (e.target.closest('[data-action="deploy"]')) deploy();
    else if (e.target.closest('[data-action="rollback"]')) rollback();
  });

  const poller = createPoller(load, POLL_MS);
  draw();
  poller.start();
  return { unmount() { poller.stop(); } };
}
