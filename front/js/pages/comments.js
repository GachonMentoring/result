import { html, render } from "../util.js";

export function mount(el) {
  render(el, html`
    <header class="page-header"><h1>댓글 기능</h1></header>
    <section class="placeholder">
      <p class="placeholder-title">준비 중</p>
      <p>댓글을 입력하면 독성 분류 모델이 결과를 돌려주는 화면이 이곳에 들어갑니다.</p>
    </section>`);
}
