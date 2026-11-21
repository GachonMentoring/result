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
  dialog.addEventListener("click", (e) => { if (e.target === dialog) dialog.close(); });
  return dialog;
}

/**
 * 범용 대화상자. 누른 버튼의 value 와 폼 입력값(체크박스·텍스트)을 돌려준다.
 * Esc·바깥 클릭은 cancelValue(기본 "no").
 * enterValue: 입력칸에서 Enter 를 누르면 이 버튼으로 처리 (지정 안 하면 Enter 무시)
 * @param {{title, target?, body?, buttons:{value,label,cls?,autofocus?}[], cancelValue?, enterValue?}} opts
 * @returns {Promise<{value:string, data:Object}>}
 */
export function openDialog({ title, target, body = "", buttons, cancelValue = "no", enterValue }) {
  const d = ensureDialog();
  render(d, html`
    <form method="dialog">
      <h2>${title}</h2>
      ${target ? html`<p class="confirm-target mono">${target}</p>` : ""}
      ${body}
      <div class="confirm-actions">
        ${buttons.map((b) => html`<button class="${b.cls ?? "btn"}" value="${b.value}" ${b.autofocus ? "autofocus" : ""}>${b.label}</button>`)}
      </div>
    </form>`);
  const form = d.querySelector("form");
  form.querySelectorAll("input[type=text]").forEach((i) => i.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    e.preventDefault();
    if (enterValue) d.close(enterValue);
  }));
  d.returnValue = cancelValue;
  d.showModal();
  return new Promise((resolve) => {
    d.addEventListener("close", () => {
      const data = {};
      form.querySelectorAll("input[name]").forEach((i) => { data[i.name] = i.type === "checkbox" ? i.checked : i.value; });
      resolve({ value: d.returnValue || cancelValue, data });
    }, { once: true });
  });
}

/**
 * @param {{title:string, target:string, plan:{effect,message,details}, yesLabel:string}} opts
 * @returns {Promise<boolean>}
 */
export async function confirmAction({ title, target, plan, yesLabel }) {
  const { value } = await openDialog({
    title, target,
    body: html`
      <div class="confirm-effect">${effectBadge(plan.effect)}</div>
      <p>${plan.message}</p>
      ${plan.details?.length ? html`<ul>${plan.details.map((t) => html`<li>${t}</li>`)}</ul>` : ""}`,
    buttons: [
      { value: "no", label: "아니오", autofocus: true },
      { value: "yes", label: yesLabel, cls: "btn btn-danger" },
    ],
  });
  return value === "yes";
}
