// "정말 ~하시겠습니까?" 예/아니오 모달. 결과는 true(예) / false(아니오·Esc·바깥 클릭)
import { html, render } from "../util.js";

// 조작이 "선언(매니페스트) ↔ 실제 인프라" 일치에 주는 영향 — 서버 actions.py 의 effect
export const EFFECTS = {
  reconcile: { tone: "ok", label: "선언 상태 유지 · 컨트롤러가 복구" },
  drift: { tone: "bad", label: "선언과 실제 인프라가 불일치하게 됨" },
  rollout: { tone: "neutral", label: "선언(spec) 변경 없음 · 파드 순차 교체" },
};

export function effectBadge(effect) {
  const e = EFFECTS[effect];
  if (!e) return "";
  return html`<span class="badge badge-${e.tone}"><span class="dot" aria-hidden="true"></span>${e.label}</span>`;
}

let dialog = null;

function ensureDialog() {
  if (dialog) return dialog;
  dialog = document.createElement("dialog");
  dialog.className = "confirm";
  document.body.appendChild(dialog);
  // 바깥(backdrop) 클릭 = 아니오
  dialog.addEventListener("click", (e) => { if (e.target === dialog) dialog.close("no"); });
  return dialog;
}

/**
 * @param {{title:string, target:string, plan:{effect,message,details}, yesLabel:string}} opts
 * @returns {Promise<boolean>}
 */
export function confirmAction({ title, target, plan, yesLabel }) {
  const d = ensureDialog();
  render(d, html`
    <form method="dialog">
      <h2>${title}</h2>
      <p class="confirm-target mono">${target}</p>
      <div class="confirm-effect">${effectBadge(plan.effect)}</div>
      <p>${plan.message}</p>
      ${plan.details?.length ? html`<ul>${plan.details.map((t) => html`<li>${t}</li>`)}</ul>` : ""}
      <div class="confirm-actions">
        <button class="btn" value="no" autofocus>아니오</button>
        <button class="btn btn-danger" value="yes">${yesLabel}</button>
      </div>
    </form>`);
  d.returnValue = "no";
  d.showModal();
  return new Promise((resolve) => {
    // Esc 는 cancel → close 로 이어지며 returnValue 는 "no" 그대로
    d.addEventListener("close", () => resolve(d.returnValue === "yes"), { once: true });
  });
}
