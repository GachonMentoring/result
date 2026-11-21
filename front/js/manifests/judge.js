// 판정(선언 ↔ 실제) 표시: 상태 배지, 리소스별 diff·충돌
import { html } from "../util.js";

export const STATES = {
  synced: { tone: "ok", label: "일치", help: "파일(선언)과 클러스터(실제)가 같습니다." },
  drift: { tone: "warn", label: "불일치", help: "클러스터가 파일 내용과 다릅니다. [적용]하면 선언대로 맞춥니다." },
  missing: { tone: "neutral", label: "미적용", help: "파일에는 있지만 클러스터에 아직 없습니다." },
  error: { tone: "bad", label: "오류", help: "파일을 해석하거나 검증할 수 없습니다." },
  empty: { tone: "neutral", label: "빈 파일", help: "리소스가 선언되어 있지 않습니다." },
};

export function stateBadge(state, { small = false } = {}) {
  const s = STATES[state];
  if (!s) return html`<span class="badge badge-neutral ${small ? "badge-sm" : ""}"><span class="dot"></span>확인 중</span>`;
  return html`<span class="badge badge-${s.tone} ${small ? "badge-sm" : ""}" title="${s.help}"><span class="dot"></span>${s.label}</span>`;
}

export function stateDot(state) {
  const s = STATES[state];
  return html`<span class="state-dot tone-${s?.tone ?? "neutral"}" title="${s ? `${s.label} — ${s.help}` : "확인 중"}"
    aria-label="${s?.label ?? "확인 중"}"></span>`;
}

function fmt(v) {
  if (v === null || v === undefined) return html`<span class="muted">(없음)</span>`;
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

export function diffTable(diff) {
  if (!diff?.length) return "";
  return html`
    <table class="diff">
      <thead><tr><th scope="col">필드</th><th scope="col">파일 (선언)</th><th scope="col">클러스터 (실제)</th></tr></thead>
      <tbody>${diff.map((d) => html`
        <tr><td class="mono">${d.path}</td><td class="mono diff-file">${fmt(d.file)}</td><td class="mono diff-live">${fmt(d.cluster)}</td></tr>`)}
      </tbody>
    </table>`;
}

export function conflictList(conflicts) {
  if (!conflicts?.length) return "";
  return html`
    <div class="banner banner-warn conflict">
      <strong>소유권 충돌 ${conflicts.length}건</strong> — 다른 관리자(kubectl 등)가 바꾼 필드입니다. 적용하면 이 플랫폼이 강제로 가져옵니다(force).
      <ul>${conflicts.map((c) => html`<li><span class="mono">${c.resource ? `${c.resource} ` : ""}${c.field}</span> <span class="muted">${c.message}</span></li>`)}</ul>
    </div>`;
}

/** 한 파일의 판정 패널 */
export function judgePanel(j, { checkedAt } = {}) {
  if (!j) return html`<div class="muted judge-empty">판정 중…</div>`;
  if (j.error) {
    return html`<div class="banner banner-bad">${j.error}</div>`;
  }
  if (!j.resources.length) return html`<div class="muted judge-empty">선언된 리소스가 없습니다.</div>`;
  return html`
    ${checkedAt ? html`<div class="muted small judge-time">${checkedAt} 기준</div>` : ""}
    <ul class="judge-list">
      ${j.resources.map((r) => html`
        <li class="judge-item">
          <div class="judge-head">
            ${stateBadge(r.state, { small: true })}
            <span class="mono judge-name">${r.kind}/${r.name}</span>
            ${r.state === "drift" ? html`<span class="muted small">${r.diff.length}개 필드 다름</span>` : ""}
          </div>
          ${r.error ? html`<div class="judge-error">${r.error}</div>` : ""}
          ${diffTable(r.diff)}
          ${conflictList(r.conflicts)}
        </li>`)}
    </ul>`;
}
