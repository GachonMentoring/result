// 모니터링: Grafana 공개 대시보드 2개를 탭으로 임베드 (k8sapimd/monitoring.md 4·7장)
//   #/monitoring/model (기본) · #/monitoring/platform
//   URL 은 API(/api/v1/monitoring) 가 ConfigMap(GRAFANA_MODEL_URL · GRAFANA_PLATFORM_URL) 에서 읽어 준다.
//   공개 대시보드(grafanas.studylink.click)라 VPN 없이 보인다. 비어 있으면 "대시보드 준비 중".
import { monitoring } from "../api.js";
import { html, render } from "../util.js";

const DEFAULT_TABS = [
  { key: "model", title: "모델" },
  { key: "platform", title: "플랫폼" },
];
const HELP = {
  model: "서빙 중인 모델 버전 · 추론 요청 · 예측 확률 분포 · 버전별 정확도 · CPU/메모리",
  platform: "Watch 동기화 · 모델/DB/MLflow 연결 실패 · 운영 행위 · 재학습 Job · 파드 재시작",
};

export function mount(el, params) {
  render(el, html`
    <header class="page-header">
      <div>
        <h1>모니터링</h1>
        <p class="subtitle">Prometheus 수집 → Grafana 대시보드 (모델 · 플랫폼)</p>
      </div>
      <div data-slot="open"></div>
    </header>
    <nav class="detail-tabs rt-tabs" role="tablist" aria-label="모니터링 대시보드" data-slot="tabs"></nav>
    <div data-slot="body"></div>`);

  const $ = (name) => el.querySelector(`[data-slot="${name}"]`);
  const state = { tab: null, config: null, error: null };

  function current() {
    return state.config?.dashboards?.find((d) => d.key === state.tab) ?? null;
  }

  function paint() {
    const tabs = state.config?.dashboards ?? DEFAULT_TABS;
    render($("tabs"), tabs.map((t) => html`
      <a class="tab ${t.key === state.tab ? "active" : ""}" role="tab" aria-selected="${String(t.key === state.tab)}"
         href="#/monitoring/${t.key}">${t.title}</a>`));

    const d = current();
    render($("open"), d?.url
      ? html`<a class="btn" href="${d.url}" target="_blank" rel="noopener noreferrer">Grafana에서 열기 ↗</a>` : "");

    if (state.error) {
      render($("body"), html`<div class="banner banner-bad">모니터링 설정을 불러오지 못했습니다: ${state.error}</div>`);
      return;
    }
    if (!state.config) {
      render($("body"), html`<section class="table-card"><div class="empty">불러오는 중…</div></section>`);
      return;
    }
    if (!d?.url) {
      render($("body"), html`
        ${d?.error ? html`<div class="banner banner-bad">${d.error}</div>` : ""}
        <section class="table-card">
          <div class="empty mon-empty">
            <strong>대시보드 준비 중</strong>
            <p>${HELP[state.tab] ?? ""}</p>
            <p class="small">Grafana 공개 대시보드 URL 이 아직 설정되지 않았습니다
              (ConfigMap <span class="mono">${state.tab === "platform" ? "GRAFANA_PLATFORM_URL" : "GRAFANA_MODEL_URL"}</span>).</p>
          </div>
        </section>`);
      return;
    }
    // 같은 URL 이면 iframe 을 다시 만들지 않는다 (탭을 오갈 때만 교체)
    const frame = $("body").querySelector("iframe");
    if (frame?.dataset.src === d.url) return;
    render($("body"), html`
      <p class="small mon-help">${HELP[state.tab] ?? ""}</p>
      <section class="table-card mon-frame-card">
        <iframe class="mon-frame" src="${d.url}" data-src="${d.url}" title="${d.title} 대시보드 (Grafana)"
                referrerpolicy="no-referrer"
                sandbox="allow-scripts allow-same-origin allow-popups allow-popups-to-escape-sandbox"></iframe>
      </section>`);
  }

  function show(p) {
    const keys = (state.config?.dashboards ?? DEFAULT_TABS).map((t) => t.key);
    const next = keys.includes(p[0]) ? p[0] : "model";
    if (next === state.tab) return;
    state.tab = next;
    paint();
  }

  async function load() {
    try {
      state.config = await monitoring.config();
      state.error = null;
    } catch (e) {
      state.error = e.message;
    }
    paint();
  }

  show(params);
  load();
  return { update: show };
}
