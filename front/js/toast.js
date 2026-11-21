// 화면 우측 하단 알림 (조작 결과)
import { html, render } from "./util.js";

let box = null;

export function toast(message, tone = "neutral", ms = 6000) {
  if (!box) {
    box = document.createElement("div");
    box.className = "toasts";
    box.setAttribute("role", "status");
    box.setAttribute("aria-live", "polite");
    document.body.appendChild(box);
  }
  const el = document.createElement("div");
  el.className = `toast toast-${tone}`;
  render(el, html`<span>${message}</span><button class="icon-btn" aria-label="닫기">✕</button>`);
  el.querySelector("button").addEventListener("click", () => el.remove());
  box.appendChild(el);
  setTimeout(() => el.remove(), ms);
}
