// 해시 라우터: #/infra, #/infra/pods, #/comments, #/feedback
import * as infra from "./pages/infra.js";
import * as comments from "./pages/comments.js";
import * as feedback from "./pages/feedback.js";

const routes = { "/infra": infra, "/comments": comments, "/feedback": feedback };
const DEFAULT_ROUTE = "/infra";

const pageEl = document.getElementById("page");
let current = null; // { path, unmount }

function parseHash() {
  const parts = location.hash.replace(/^#/, "").split("/").filter(Boolean);
  return { path: "/" + (parts[0] ?? ""), params: parts.slice(1) };
}

function navigate() {
  const { path, params } = parseHash();
  if (!routes[path]) {
    location.replace("#" + DEFAULT_ROUTE);
    return;
  }

  document.querySelectorAll(".nav-item").forEach((a) => {
    const active = a.dataset.route === path;
    a.classList.toggle("active", active);
    if (active) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });

  // 같은 페이지 안에서 파라미터만 바뀐 경우 (예: 탭 전환) 페이지에 알려준다
  if (current?.path === path && current.update) {
    current.update(params);
    return;
  }
  current?.unmount?.();
  pageEl.replaceChildren();
  const handle = routes[path].mount(pageEl, params) ?? {};
  current = { path, ...handle };
}

window.addEventListener("hashchange", navigate);
navigate();
