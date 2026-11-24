// 해시 라우터: #/infra, #/infra/pods, #/manifests/app/web.yaml, #/comments, #/feedback, #/retrain/dataset, #/monitoring/model
import * as infra from "./pages/infra.js";
import * as manifests from "./pages/manifests.js";
import * as comments from "./pages/comments.js";
import * as feedback from "./pages/feedback.js";
import * as retrain from "./pages/retrain.js";
import * as monitoring from "./pages/monitoring.js";

const routes = { "/infra": infra, "/manifests": manifests, "/comments": comments, "/feedback": feedback, "/retrain": retrain,
  "/monitoring": monitoring };
const DEFAULT_ROUTE = "/infra";

let pageEl = document.getElementById("page");
let current = null; // { path, unmount, update, beforeLeave }

// 페이지마다 새 영역을 만들어 통째로 교체한다.
// 같은 요소를 재사용하면 페이지가 그 요소에 붙인 이벤트 처리기가 지워지지 않고 쌓여서,
// 다시 방문할 때마다 버튼 한 번에 처리기가 여러 번 실행된다 (예전 입력값까지 중복 전송된 버그).
function freshPage() {
  const el = document.createElement("main");
  el.id = "page";
  el.className = "page";
  pageEl.replaceWith(el);
  pageEl = el;
  return el;
}
let lastHash = location.hash;
let reverting = false;

function parseHash() {
  const parts = location.hash.replace(/^#/, "").split("/").filter(Boolean);
  return { path: "/" + (parts[0] ?? ""), params: parts.slice(1).map(decodeURIComponent) };
}

async function navigate() {
  if (reverting) { reverting = false; return; }
  // 페이지가 "떠나도 되는지"(저장 안 한 변경 등) 묻는다. 거절하면 주소를 되돌린다.
  if (current?.beforeLeave && !(await current.beforeLeave(parseHash()))) {
    reverting = true;
    location.replace(lastHash || "#" + DEFAULT_ROUTE);
    return;
  }
  lastHash = location.hash;
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
  const handle = routes[path].mount(freshPage(), params) ?? {};
  current = { path, ...handle };
}

window.addEventListener("hashchange", navigate);
navigate();
