// 우측 상세 패널
//   헤더: [재시작] [삭제] — 확인창(예/아니오)을 거친 뒤에만 요청
//   탭  : 개요(요약 + 라벨 + manifest) / 이벤트 / 로그(Pod)
import { dump } from "../../vendor/js-yaml.js";
import { api } from "../api.js";
import { age, html, render } from "../util.js";
import { KIND_BY_KEY, badge, labelChips } from "./columns.js";
import { confirmAction } from "./confirm.js";
import { clampOffset, movePage, pager } from "./pager.js";
import { EVENTS_PAGE_SIZE } from "../../config.js";

const STATUS_LABELS = {
  phase: "Phase", display_status: "상태", ready: "Ready", restarts: "재시작", node: "노드", pod_ip: "Pod IP",
  replicas: "원하는 replicas", ready_replicas: "Ready", updated_replicas: "최신", available_replicas: "Available",
  current_replicas: "Current", type: "타입", cluster_ip: "Cluster IP", ports: "포트", selector: "Selector",
  state: "상태", completions: "목표 완료 수", active: "실행 중", succeeded: "성공", failed: "실패",
  start_time: "시작", completion_time: "완료", volume: "PV", requested: "요청 용량", capacity: "실제 용량",
  access_modes: "Access modes", storage_class: "StorageClass", volume_mode: "Volume mode",
  reason: "Reason", object: "대상", message: "메시지", count: "횟수", last_seen: "마지막 발생", source: "보고 주체",
  object_kind: null, object_name: null,
};

const ACTIONS = {
  restart: { button: "⟳ 재시작", title: "롤아웃 재시작하시겠습니까?", yes: "재시작", cls: "btn" },
  delete: { button: "삭제", title: "정말 삭제하시겠습니까?", yes: "삭제", cls: "btn btn-danger-outline" },
};

const LOG_TAIL = 200;

function fmtValue(key, v) {
  if (key === "selector") return labelChips(v);
  if (Array.isArray(v)) return v.length ? v.join(", ") : "-";
  if (v === null || v === undefined || v === "") return "-";
  return String(v);
}

function toYaml(manifest, showManaged) {
  const copy = structuredClone(manifest);
  if (!showManaged && copy.metadata) delete copy.metadata.managedFields;
  return dump(copy, { noRefs: true, lineWidth: 120, sortKeys: false });
}

export function createDetailPanel(root, { onClose, onResult }) {
  let target = null;      // { kind, name }
  let data = null;        // 마지막 상세 응답
  let gone = false;       // 리소스가 삭제됨
  let error = null;
  let showManaged = false;
  let tab = "overview";   // overview / events / logs
  let events = null;      // 이 리소스의 이벤트 목록
  let eventsError = null;
  let eventsOffset = 0;   // 이벤트 페이지 시작 위치 (10개 단위)
  let logs = null;        // { text, containers, container, previous, error }
  let busy = null;        // 진행 중인 조작 이름
  const last = new Map(); // slot → 마지막으로 그린 HTML

  const $ = (slot) => root.querySelector(`[data-d="${slot}"]`);
  function paint(slot, tpl, keepScroll = false) {
    const el = $(slot);
    if (!el) return false;
    const s = String(tpl);
    if (last.get(slot) === s) return false;
    last.set(slot, s);
    const top = el.scrollTop;
    el.innerHTML = s;
    if (keepScroll) el.scrollTop = top;
    if (slot === "body") last.delete("log-error"); // 본문을 다시 그리면 안쪽 slot도 새로 그려야 함
    return true;
  }

  // ------------------------------------------------------------ 이벤트 처리
  root.addEventListener("click", (e) => {
    const pageBtn = e.target.closest("[data-page]");
    if (pageBtn && events) {
      eventsOffset = movePage(pageBtn.dataset.page, events);
      refresh();
      return;
    }
    const btn = e.target.closest("[data-action]");
    if (!btn) return;
    const action = btn.dataset.action;
    if (action === "close") close();
    if (action === "managed") { showManaged = !showManaged; draw(); }
    if (action === "tab") { tab = btn.dataset.tab; draw(); if (tab !== "overview") refresh(); }
    if (action === "restart" || action === "delete") runAction(action);
    if (action === "copy" && data) {
      navigator.clipboard?.writeText(toYaml(data.manifest, showManaged)).then(() => {
        btn.textContent = "복사됨";
        setTimeout(() => (btn.textContent = "YAML 복사"), 1200);
      });
    }
  });
  root.addEventListener("change", (e) => {
    if (!logs) return;
    if (e.target.dataset.log === "container") logs.container = e.target.value;
    if (e.target.dataset.log === "previous") logs.previous = e.target.checked;
    logs.text = null;
    drawLogs();
    refresh();
  });
  const onKey = (e) => {
    if (e.key !== "Escape" || !target) return;
    if (document.querySelector("dialog[open]")) return; // 확인창의 Esc는 확인창만 닫는다
    close();
  };
  document.addEventListener("keydown", onKey);

  // ------------------------------------------------------------ 조작 (삭제 / 재시작)
  async function runAction(action) {
    const plan = data?.actions?.[action];
    if (!plan?.allowed || busy || gone) return;
    const t = target;
    const uid = data.uid;
    const yes = await confirmAction({
      title: ACTIONS[action].title,
      target: plan.target ?? `${t.kind}/${t.name}`,
      plan,
      yesLabel: ACTIONS[action].yes,
    });
    if (yes) {
      busy = action;
      drawHead();
      try {
        const r = action === "delete" ? await api.remove(t.kind, t.name, uid) : await api.restart(t.kind, t.name);
        onResult?.({ ok: true, action, message: r.message, effect: r.effect });
      } catch (e) {
        onResult?.({ ok: false, action, message: e.message });
      } finally {
        busy = null;
        drawHead();
      }
    } else {
      // [아니오] — 서버에 아무 요청도 보내지 않는다
    }
  }

  // ------------------------------------------------------------ 열기 / 닫기 / 갱신
  function close() {
    target = null; data = null;
    root.classList.remove("open");
    root.setAttribute("aria-hidden", "true");
    onClose?.();
  }

  async function open(kind, name) {
    target = { kind, name };
    data = null; gone = false; error = null; showManaged = false;
    tab = "overview"; events = null; eventsError = null; eventsOffset = 0; logs = null; busy = null;
    last.clear();
    root.classList.add("open");
    root.setAttribute("aria-hidden", "false");
    render(root, html`
      <div class="detail-head">
        <div class="detail-title">
          <div class="detail-kind">${KIND_BY_KEY[kind]?.label ?? kind}</div>
          <h2 class="mono">${name}</h2>
        </div>
        <div class="detail-actions" data-d="actions"></div>
        <button class="icon-btn" data-action="close" aria-label="닫기" title="닫기 (Esc)">✕</button>
      </div>
      <div class="detail-tabs" role="tablist" data-d="tabs"></div>
      <div class="detail-body" data-d="body"><div class="detail-empty">불러오는 중…</div></div>`);
    await refresh();
  }

  async function refresh() {
    if (!target) return;
    const t = target;
    const wantLogs = tab === "logs" && t.kind === "pods" && !gone;
    const [d, ev, lg] = await Promise.allSettled([
      api.detail(t.kind, t.name),
      t.kind === "events" ? Promise.resolve(null)
        : api.events(t.kind, t.name, { limit: EVENTS_PAGE_SIZE, offset: eventsOffset }),
      wantLogs ? api.logs(t.name, { container: logs?.container, previous: logs?.previous, tail: LOG_TAIL })
        : Promise.resolve(null),
    ]);
    if (target !== t) return;

    if (d.status === "fulfilled") { data = d.value; gone = false; error = null; }
    else if (d.reason?.status === 404) gone = true;
    else error = d.reason?.message;

    if (ev.status === "fulfilled") {
      events = ev.value; eventsError = null;
      // Event 자체를 볼 때는 이벤트 목록을 부르지 않으므로 null
      if (events) {
        const fixed = clampOffset(eventsOffset, EVENTS_PAGE_SIZE, events.count);
        if (fixed !== eventsOffset) { eventsOffset = fixed; return refresh(); }
      }
    }
    else eventsError = ev.reason?.message;

    if (wantLogs) {
      logs = logs ?? { container: null, previous: false };
      if (lg.status === "fulfilled") {
        Object.assign(logs, { text: lg.value.text, containers: lg.value.containers,
          container: lg.value.container, error: null });
      } else {
        logs.error = lg.reason?.message;
      }
    }
    draw();
  }

  // ------------------------------------------------------------ 그리기
  function draw() {
    if (!target) return;
    drawHead();
    drawTabs();
    if (!data) {
      paint("body", html`<div class="detail-empty">${gone ? "리소스가 삭제되었습니다." : error ?? "불러오는 중…"}</div>`);
      return;
    }
    if (tab === "events") drawEvents();
    else if (tab === "logs") drawLogs();
    else drawOverview();
  }

  function drawHead() {
    if (!target || !$("actions")) return;
    if (!data || gone) { paint("actions", ""); return; }
    const buttons = ["restart", "delete"].filter((a) => data.actions?.[a]).map((a) => {
      const plan = data.actions[a];
      const disabled = !plan.allowed || !!busy;
      const label = busy === a ? "요청 중…" : ACTIONS[a].button;
      return html`<button class="${ACTIONS[a].cls}" data-action="${a}"
        title="${plan.allowed ? plan.message : plan.reason}" ${disabled ? "disabled" : ""}>${label}</button>`;
    });
    paint("actions", buttons.map(String).join(""));
  }

  function drawTabs() {
    const tabs = [["overview", "개요"]];
    if (target.kind !== "events") {
      const warn = events?.items?.some((e) => e.status.type === "Warning");
      tabs.push(["events", html`이벤트 <span class="count ${warn ? "count-warn" : ""}">${events?.count ?? "–"}</span>`]);
    }
    if (target.kind === "pods") tabs.push(["logs", "로그"]);
    if (!tabs.some(([k]) => k === tab)) tab = "overview";
    paint("tabs", tabs.map(([k, label]) => html`
      <button class="tab ${k === tab ? "active" : ""}" role="tab" aria-selected="${String(k === tab)}"
              data-action="tab" data-tab="${k}">${label}</button>`).map(String).join(""));
  }

  function drawOverview() {
    const kindDef = KIND_BY_KEY[target.kind];
    const rows = Object.entries(data.status ?? {}).filter(([k]) => STATUS_LABELS[k] !== null);
    paint("body", html`
      ${gone ? html`<div class="banner banner-warn">이 리소스는 삭제되었습니다. 마지막으로 받은 내용을 보여줍니다.</div>` : ""}
      ${error ? html`<div class="banner banner-bad">${error}</div>` : ""}
      ${data.terminating && !gone ? html`<div class="banner banner-warn">삭제 진행 중(Terminating)입니다. 종료가 끝나면 목록에서 사라집니다.</div>` : ""}
      <div class="detail-status">${badge(target.kind, data)} <span class="muted">· ${age(data.created_at)} 전 생성 · rv ${data.manifest?.metadata?.resourceVersion}</span></div>

      <h3>요약</h3>
      <dl class="kv">
        <dt>Namespace</dt><dd class="mono">${data.namespace}</dd>
        ${rows.map(([k, v]) => html`<dt>${STATUS_LABELS[k] ?? k}</dt><dd>${fmtValue(k, v)}</dd>`)}
        <dt>UID</dt><dd class="mono small">${data.uid}</dd>
      </dl>

      <h3>라벨</h3>
      ${labelChips(data.labels)}

      <div class="yaml-head">
        <h3>Manifest <span class="muted">(${kindDef?.label ?? target.kind} 원본)</span></h3>
        <div class="yaml-actions">
          <button class="btn" data-action="managed">${showManaged ? "managedFields 숨기기" : "managedFields 보기"}</button>
          <button class="btn" data-action="copy">YAML 복사</button>
        </div>
      </div>
      <pre class="yaml mono">${toYaml(data.manifest, showManaged)}</pre>`, true);
  }

  function drawEvents() {
    if (!events) {
      paint("body", html`<div class="detail-empty">${eventsError ?? "불러오는 중…"}</div>`);
      return;
    }
    if (!events.items.length) {
      paint("body", html`<div class="detail-empty">이 리소스에 대한 이벤트가 없습니다.<br>
        <span class="small">(Kubernetes는 이벤트를 기본 1시간만 보관합니다)</span></div>`);
      return;
    }
    paint("body", html`
      ${eventsError ? html`<div class="banner banner-bad">${eventsError}</div>` : ""}
      <div class="events-head"><span class="muted small">최신순 · ${EVENTS_PAGE_SIZE}개씩</span>${pager(events)}</div>
      <ol class="timeline">
        ${events.items.map((e) => html`
          <li class="tl-item ${e.status.type === "Warning" ? "tl-warn" : ""}">
            <div class="tl-head">
              <span class="tl-reason">${e.status.reason}</span>
              ${e.status.count > 1 ? html`<span class="muted">×${e.status.count}</span>` : ""}
              <span class="muted tl-time">${age(e.status.last_seen)} 전</span>
            </div>
            <div class="tl-msg">${e.status.message}</div>
            ${e.status.source ? html`<div class="muted small">${e.status.source}</div>` : ""}
          </li>`)}
      </ol>`, true);
  }

  function drawLogs() {
    if (gone) {
      paint("body", html`<div class="detail-empty">삭제된 파드의 로그는 볼 수 없습니다.</div>`);
      return;
    }
    const l = logs ?? {};
    const containers = l.containers ?? [];
    // 컨트롤 영역은 컨테이너 목록이 바뀔 때만 다시 그린다 (select 포커스 유지)
    paint("body", html`
      <div class="log-toolbar">
        ${containers.length > 1 ? html`
          <label>컨테이너
            <select data-log="container">
              ${containers.map((c) => html`<option value="${c}" ${c === l.container ? "selected" : ""}>${c}</option>`)}
            </select>
          </label>` : html`<span class="muted mono">${l.container ?? ""}</span>`}
        <label title="직전에 종료된 컨테이너의 로그 (CrashLoopBackOff 원인 확인)">
          <input type="checkbox" data-log="previous" ${l.previous ? "checked" : ""}> 이전 컨테이너
        </label>
        <span class="muted small">최근 ${LOG_TAIL}줄 · 자동 갱신</span>
      </div>
      <div data-d="log-error"></div>
      <pre class="yaml mono log" data-d="log-text"></pre>`);

    paint("log-error", l.error ? html`<div class="banner banner-bad">${l.error}</div>` : "");
    const pre = $("log-text");
    const text = l.text === undefined || l.text === null ? (l.error ? "" : "불러오는 중…") : (l.text || "(로그 없음)");
    if (pre.textContent !== text) {
      const body = $("body");
      const atBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 40;
      pre.textContent = text;
      if (atBottom) body.scrollTop = body.scrollHeight; // 맨 아래를 보고 있었으면 따라간다
    }
  }

  function destroy() {
    document.removeEventListener("keydown", onKey);
    target = null;
  }

  return { open, close, refresh, destroy, isOpen: () => !!target, current: () => target };
}
