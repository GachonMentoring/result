// ◀ 1 / 5 ▶ 페이지 넘김. 서버 목록 응답의 { count, offset, limit } 를 그대로 사용한다.
import { html } from "../util.js";

export function pager({ count = 0, offset = 0, limit } = {}) {
  if (!limit || count <= limit) return "";
  const page = Math.floor(offset / limit) + 1;
  const pages = Math.ceil(count / limit);
  const from = offset + 1;
  const to = Math.min(offset + limit, count);
  return html`
    <div class="pager" role="navigation" aria-label="페이지 이동">
      <button class="btn pager-btn" data-page="prev" aria-label="이전 ${limit}개" title="이전 ${limit}개 (더 최근)"
        ${page <= 1 ? "disabled" : ""}>◀</button>
      <span class="pager-info"><strong>${page}</strong> / ${pages}
        <span class="muted small">(${from}–${to} / 전체 ${count})</span></span>
      <button class="btn pager-btn" data-page="next" aria-label="다음 ${limit}개" title="다음 ${limit}개 (더 오래된)"
        ${page >= pages ? "disabled" : ""}>▶</button>
    </div>`;
}

// 버튼 방향 → 새 offset
export function movePage(dir, { count = 0, offset = 0, limit }) {
  const next = dir === "next" ? offset + limit : offset - limit;
  return clampOffset(next, limit, count);
}

// 목록이 줄어서 현재 페이지가 범위를 벗어나면 마지막 페이지로
export function clampOffset(offset, limit, count) {
  if (!limit || count <= 0) return 0;
  const lastStart = Math.floor((count - 1) / limit) * limit;
  return Math.max(0, Math.min(offset, lastStart));
}
