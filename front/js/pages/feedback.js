import { html, render } from "../util.js";

export function mount(el) {
  render(el, html`
    <header class="page-header"><h1>피드백</h1></header>
    <section class="placeholder">
      <p class="placeholder-title">준비 중</p>
      <p>분류 결과에 대한 피드백과 추론 결과 시각화가 이곳에 들어갑니다.</p>
    </section>`);
}
