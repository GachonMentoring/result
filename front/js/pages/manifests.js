// 매니페스트 관리 (Work 3): NFS 의 YAML 을 수정·저장·삭제·apply 하고, 선언 ↔ 실제 일치 여부를 판정
//   - apply 는 "저장된 파일" 기준 (저장 안 한 변경이 있으면 [적용] 비활성)
//   - 편집 중 외부에서 파일이 바뀌면 저장을 막고 선택하게 함
import { mf } from "../api.js";
import { createPoller } from "../poller.js";
import { clock, html, render } from "../util.js";
import { openDialog } from "../infra/confirm.js";
import { toast } from "../toast.js";
import { createEditor } from "../manifests/editor.js";
import { STATES, conflictList, diffTable, judgePanel, stateBadge, stateDot } from "../manifests/judge.js";

const LIST_MS = 3000;     // 파일 목록 (외부에서 추가·수정·삭제 감지)
const STATUS_MS = 10000;  // 전체 판정 배지 + 선언 없음
const JUDGE_MS = 5000;    // 열린 파일 판정 (kubectl scale 같은 변화를 화면에서 바로 확인)

const encPath = (p) => p.split("/").map(encodeURIComponent).join("/");
const hashOf = (p) => "#/manifests" + (p ? "/" + encPath(p) : "");
const pathFromParams = (params) => (params.length ? params.join("/") : null);

function buildTree(files) {
  const root = { dirs: new Map(), files: [] };
  for (const f of files) {
    const parts = f.path.split("/");
    let node = root;
    parts.slice(0, -1).forEach((part, i) => {
      if (!node.dirs.has(part)) node.dirs.set(part, { dirs: new Map(), files: [], path: parts.slice(0, i + 1).join("/") });
      node = node.dirs.get(part);
    });
    node.files.push(f);
  }
  return root;
}

function templateFor(path) {
  const base = path.split("/").pop().replace(/\.ya?ml$/i, "").toLowerCase()
    .replace(/[^a-z0-9-]+/g, "-").replace(/^-+|-+$/g, "") || "example";
  return `apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: ${base}\ndata:\n  key: value\n`;
}

export function mount(el, params) {
  const state = {
    root: null,
    files: null,          // [{path, size, mtime, etag}]
    listError: null,
    statusAll: null,      // {files: {path: {state, error}}, orphans: [...]}
    collapsed: new Set(), // 접힌 폴더
    path: null,           // 열린 파일
    file: null,           // {content, etag} — 디스크에 저장된 내용
    dirty: false,
    gone: false,          // 열린 파일이 외부에서 삭제됨
    external: false,      // 편집 중 외부에서 바뀜
    judge: null,
    judgeAt: null,
    busy: null,           // save / apply / delete / load
    fileError: null,      // 저장 거부 사유 {message, line}
    writeSeq: 0,          // 저장할 때마다 증가 → 저장 전에 출발한 목록 응답으로 오판하지 않기
  };

  render(el, html`
    <header class="page-header">
      <div>
        <h1>매니페스트 관리</h1>
        <p class="subtitle">선언(YAML) 수정·적용 · 선언 ↔ 실제 판정</p>
      </div>
    </header>
    <div class="mf-layout">
      <aside class="mf-side" aria-label="매니페스트 파일">
        <div class="mf-side-head">
          <h2>파일</h2>
          <button class="btn btn-sm" data-action="new" title="새 YAML 파일 만들기">+ 새 파일</button>
        </div>
        <div class="mf-root mono small muted" data-slot="root"></div>
        <nav class="mf-tree" data-slot="tree"></nav>
        <div data-slot="orphans"></div>
      </aside>
      <section class="mf-main">
        <div data-slot="empty"></div>
        <div class="mf-file" data-slot="file" hidden>
          <div class="mf-toolbar">
            <div class="mf-title"><span class="mono" data-slot="title"></span><span data-slot="flags"></span></div>
            <div class="mf-actions" data-slot="actions"></div>
          </div>
          <div data-slot="banner"></div>
          <div class="mf-editor" data-slot="editor"></div>
          <section class="mf-judge" aria-label="판정">
            <div class="mf-judge-head">
              <h3>판정 <span class="muted small">저장된 파일(선언) ↔ 클러스터(실제)</span></h3>
              <span data-slot="judge-badge"></span>
            </div>
            <div data-slot="judge"></div>
          </section>
        </div>
      </section>
    </div>`);

  const $ = (name) => el.querySelector(`[data-slot="${name}"]`);
  const last = new Map();
  function paint(name, tpl) {
    const s = String(tpl);
    if (last.get(name) === s) return;
    last.set(name, s);
    $(name).innerHTML = s;
  }

  const editor = createEditor($("editor"), {
    onChange(value) {
      const dirty = !!state.file && value !== state.file.content;
      if (dirty !== state.dirty) { state.dirty = dirty; drawFile(); }
    },
    onSave: () => save(),
  });

  // ------------------------------------------------------------ 그리기
  function fileState(path) {
    if (path === state.path && state.judge && !state.judge.loading) return state.judge.state;
    return state.statusAll?.files?.[path]?.state;
  }

  function treeHtml(node, depth = 0) {
    const dirs = [...node.dirs.entries()].sort(([a], [b]) => a.localeCompare(b));
    const files = [...node.files].sort((a, b) => a.path.localeCompare(b.path));
    return html`<ul class="tree" role="${depth ? "group" : "tree"}">
      ${dirs.map(([name, d]) => {
        const open = !state.collapsed.has(d.path);
        return html`<li role="treeitem" aria-expanded="${String(open)}">
          <button class="tree-dir" data-dir="${d.path}" style="--depth:${depth}">
            <span class="caret">${open ? "▾" : "▸"}</span>${name}/
          </button>
          ${open ? treeHtml(d, depth + 1) : ""}
        </li>`;
      })}
      ${files.map((f) => {
        const name = f.path.split("/").pop();
        const active = f.path === state.path;
        return html`<li role="treeitem" aria-selected="${String(active)}">
          <a class="tree-file ${active ? "active" : ""}" href="${hashOf(f.path)}" style="--depth:${depth}" title="${f.path}">
            ${stateDot(fileState(f.path))}<span class="tree-name mono">${name}</span>
            ${active && state.dirty ? html`<span class="dirty-dot" title="저장 안 됨">●</span>` : ""}
          </a>
        </li>`;
      })}
    </ul>`;
  }

  function drawSide() {
    paint("root", state.root ? html`<span title="MANIFEST_DIR">${state.root}</span>` : "");
    if (state.listError) paint("tree", html`<div class="banner banner-bad">${state.listError}</div>`);
    else if (!state.files) paint("tree", html`<div class="muted small pad">불러오는 중…</div>`);
    else if (!state.files.length) paint("tree", html`<div class="muted small pad">YAML 파일이 없습니다.</div>`);
    else paint("tree", treeHtml(buildTree(state.files)));

    const orphans = state.statusAll?.orphans ?? [];
    paint("orphans", orphans.length ? html`
      <div class="mf-orphans">
        <h3>선언 없음 <span class="count count-warn">${orphans.length}</span></h3>
        <p class="muted small">이 플랫폼이 적용했지만 어떤 파일에도 없는 리소스</p>
        <ul>${orphans.map((o, i) => html`
          <li><span class="mono small">${o.kind}/${o.name}</span>
            <button class="btn btn-sm btn-danger-outline" data-action="orphan" data-i="${i}">삭제</button></li>`)}
        </ul>
      </div>` : "");
  }

  function drawEmpty() {
    if (state.path) { paint("empty", ""); return; }
    const files = state.files ?? [];
    const counts = {};
    files.forEach((f) => { const s = fileState(f.path) ?? "unknown"; counts[s] = (counts[s] ?? 0) + 1; });
    paint("empty", html`
      <div class="placeholder">
        <p class="placeholder-title">${files.length ? "왼쪽에서 파일을 선택하세요" : "매니페스트 폴더가 비어 있습니다"}</p>
        <p>${files.length ? "YAML을 수정·저장하고 [적용]으로 클러스터에 반영합니다." : "[+ 새 파일]로 만들거나 NFS 폴더에 YAML을 넣으면 자동으로 나타납니다."}</p>
        ${files.length ? html`<div class="state-summary">${Object.entries(STATES).filter(([k]) => counts[k])
          .map(([k]) => html`<span>${stateBadge(k)} ${counts[k]}</span>`)}</div>` : ""}
      </div>`);
  }

  function drawFile() {
    $("file").hidden = !state.path;
    drawEmpty();
    if (!state.path) return;
    const busy = state.busy;
    const jState = state.judge && !state.judge.loading ? state.judge.state : null;

    paint("title", state.path);
    paint("flags", html`
      ${state.dirty ? html`<span class="flag-dirty">● 저장 안 됨</span>` : ""}
      ${stateBadge(jState)}`);

    const applyBlock = state.dirty ? "먼저 저장하세요 (적용은 저장된 파일 기준)"
      : state.gone ? "파일이 삭제되었습니다"
      : jState === "error" ? "오류가 있어 적용할 수 없습니다"
      : jState === "empty" ? "선언된 리소스가 없습니다" : null;
    paint("actions", html`
      <button class="btn" data-action="reload" ${busy ? "disabled" : ""} title="디스크(NFS)에서 다시 불러오기">↻</button>
      <button class="btn" data-action="save" ${!state.dirty || busy || state.gone ? "disabled" : ""} title="저장 (Ctrl+S)">
        ${busy === "save" ? "저장 중…" : "저장"}</button>
      <button class="btn btn-primary" data-action="apply" ${applyBlock || busy ? "disabled" : ""}
        title="${applyBlock ?? "저장된 파일을 클러스터에 적용"}">${busy === "apply" ? "적용 중…" : "적용"}</button>
      <button class="btn btn-danger-outline" data-action="delete" ${busy || state.gone ? "disabled" : ""}>삭제</button>`);

    let banner = "";
    if (state.gone) {
      banner = html`<div class="banner banner-bad">이 파일이 외부에서 삭제되었습니다. 편집 내용이 필요하면 복사해 두세요.</div>`;
    } else if (state.external) {
      banner = html`<div class="banner banner-warn">편집하는 동안 이 파일이 외부(NFS)에서 변경되었습니다.
        <button class="btn btn-sm" data-action="reload">다시 불러오기 (내 변경 버림)</button></div>`;
    } else if (state.fileError) {
      banner = html`<div class="banner banner-bad">저장하지 않았습니다 — ${state.fileError.message}</div>`;
    }
    paint("banner", banner);

    paint("judge-badge", state.judge && !state.judge.loading ? stateBadge(state.judge.state) : "");
    paint("judge", state.judge?.loading ? html`<div class="muted judge-empty">판정 중…</div>`
      : judgePanel(state.judge, { checkedAt: state.judgeAt }));
  }

  function drawAll() { drawSide(); drawFile(); }

  // ------------------------------------------------------------ 데이터
  async function loadList() {
    const seq = state.writeSeq;
    try {
      const r = await mf.list();
      state.root = r.root;
      state.files = r.files;
      state.listError = null;
    } catch (e) {
      state.listError = e.message;
    }
    if (state.path && state.files && seq === state.writeSeq && state.busy !== "save" && state.file) {
      const f = state.files.find((x) => x.path === state.path);
      state.gone = !f;
      if (f && f.etag !== state.file.etag) {
        if (!state.dirty) await reloadFile({ keepView: true, notify: true });
        else state.external = true;
      }
    }
    drawAll();
  }

  async function loadStatusAll() {
    try {
      state.statusAll = await mf.statusAll();
    } catch {
      /* 판정 실패는 배지만 비움 — 목록은 계속 동작 */
    }
    drawAll();
  }

  async function judgeCurrent() {
    const p = state.path;
    if (!p) return;
    let j;
    try {
      j = await mf.status(p);
    } catch (e) {
      j = { path: p, state: "error", resources: [], error: e.message };
    }
    if (p !== state.path) return;
    state.judge = j;
    state.judgeAt = clock();
    drawAll();
  }

  async function reloadFile({ keepView = false, notify = false } = {}) {
    const p = state.path;
    try {
      const r = await mf.read(p);
      if (p !== state.path) return;
      state.file = { content: r.content, etag: r.etag };
      editor.setValue(r.content, { keepView });
      Object.assign(state, { dirty: false, external: false, gone: false, fileError: null });
      if (notify) toast(`외부에서 변경된 '${p}'을(를) 다시 불러왔습니다.`, "warn");
    } catch (e) {
      if (e.status === 404) state.gone = true;
      else toast(`불러오기 실패: ${e.message}`, "bad");
    }
    drawAll();
  }

  async function openFile(p) {
    Object.assign(state, { path: p, file: null, dirty: false, gone: false, external: false,
      judge: p ? { loading: true } : null, judgeAt: null, fileError: null });
    if (!p) { drawAll(); return; }
    editor.setValue("");
    editor.setReadOnly(true);
    drawAll();
    await reloadFile();
    editor.setReadOnly(false);
    if (state.gone && !state.file) {
      toast(`파일이 없습니다: ${p}`, "bad");
      location.hash = hashOf(null);
      return;
    }
    judgeCurrent();
  }

  // ------------------------------------------------------------ 저장
  async function save() {
    if (!state.dirty || state.busy || state.gone) return;
    const content = editor.getValue();
    state.busy = "save";
    state.writeSeq++;
    drawFile();
    try {
      const r = await mf.save(state.path, content, state.file.etag);
      afterSave(content, r.etag);
    } catch (e) {
      if (e.status === 409) await resolveConflict(content);
      else if (e.status === 400) {
        state.fileError = { message: e.message, line: e.data?.line };
        editor.markError(e.data?.line);
      } else toast(`저장 실패: ${e.message}`, "bad");
    } finally {
      state.busy = null;
      drawAll();
    }
  }

  function afterSave(content, etag) {
    state.file = { content, etag };
    state.dirty = editor.getValue() !== content;
    Object.assign(state, { external: false, fileError: null });
    editor.markError(null);
    toast("저장했습니다. 클러스터 반영은 [적용]으로 합니다.", "ok", 3500);
    judgeCurrent();
    statusPoller.refreshNow();
  }

  async function resolveConflict(content) {
    const { value } = await openDialog({
      title: "파일이 외부에서 변경되었습니다",
      target: state.path,
      body: html`<p>불러온 뒤 NFS 폴더에서 누군가 이 파일을 수정했습니다. 어떻게 할까요?</p>
        <ul><li><strong>다시 불러오기</strong>: 외부 변경을 가져오고 내 편집은 버립니다.</li>
        <li><strong>강제 덮어쓰기</strong>: 내 편집으로 덮어씁니다. 외부 변경은 사라집니다.</li></ul>`,
      buttons: [
        { value: "cancel", label: "취소", autofocus: true },
        { value: "reload", label: "다시 불러오기" },
        { value: "force", label: "강제 덮어쓰기", cls: "btn btn-danger" },
      ],
      cancelValue: "cancel",
    });
    if (value === "reload") {
      await reloadFile();
      judgeCurrent();
    } else if (value === "force") {
      try {
        const r = await mf.save(state.path, content, null, true);
        afterSave(content, r.etag);
      } catch (e) {
        toast(`저장 실패: ${e.message}`, "bad");
      }
    } else {
      state.external = true; // 취소: 편집 내용은 그대로 두고 경고만 유지
    }
  }

  // ------------------------------------------------------------ 적용
  async function apply() {
    if (state.dirty || state.busy || state.gone) return;
    const p = state.path;
    state.busy = "apply";
    drawFile();
    try {
      // 적용 직전 최신 판정으로 미리보기 (무엇이 생성·변경되는지, 충돌이 있는지)
      const preview = await mf.status(p);
      state.judge = preview;
      state.judgeAt = clock();
      drawAll();
      if (preview.error || preview.state === "error") {
        toast("오류가 있는 리소스가 있어 적용할 수 없습니다. 판정 패널을 확인하세요.", "bad");
        return;
      }
      const conflicts = preview.resources.flatMap((r) => r.conflicts.map((c) => ({ ...c, resource: `${r.kind}/${r.name}` })));
      const ACTION = { missing: "생성", drift: "변경", synced: "변경 없음" };
      const { value } = await openDialog({
        title: "클러스터에 적용하시겠습니까?",
        target: `${p} — 리소스 ${preview.resources.length}개`,
        body: html`
          <p>저장된 파일 내용으로 Server-Side Apply 합니다. 클러스터가 파일(선언)과 같아집니다.</p>
          <ul class="apply-plan">${preview.resources.map((r) => html`
            <li><span class="plan-action plan-${r.state}">${ACTION[r.state] ?? r.state}</span>
              <span class="mono">${r.kind}/${r.name}</span>
              ${r.state === "drift" ? diffTable(r.diff) : ""}</li>`)}
          </ul>
          ${conflictList(conflicts)}`,
        buttons: [
          { value: "no", label: "아니오", autofocus: true },
          { value: "yes", label: conflicts.length ? "강제 적용" : "적용", cls: conflicts.length ? "btn btn-danger" : "btn btn-primary" },
        ],
      });
      if (value === "yes") {
        try {
          const r = await mf.apply(p, conflicts.length > 0);
          const n = (a) => r.resources.filter((x) => x.action === a).length;
          toast(`적용했습니다 — 생성 ${n("created")} · 변경 ${n("updated")} · 변경 없음 ${n("unchanged")}`, "ok");
        } catch (e) {
          toast(e.status === 409 ? "적용 직전에 새 충돌이 생겼습니다. 판정을 확인하고 다시 시도하세요." : `적용 실패: ${e.message}`,
            e.status === 409 ? "warn" : "bad", 8000);
        }
      } else {
        // [아니오] — 서버에 적용 요청을 보내지 않는다
      }
    } catch (e) {
      toast(`판정 실패: ${e.message}`, "bad");
    } finally {
      state.busy = null;
      drawAll();
      judgeCurrent();
      statusPoller.refreshNow();
    }
  }

  // ------------------------------------------------------------ 삭제
  async function remove() {
    if (state.busy) return;
    const p = state.path;
    const parsed = state.judge && !state.judge.loading && !state.judge.error;
    const live = parsed ? state.judge.resources.filter((r) => r.state === "synced" || r.state === "drift") : [];
    const { value, data } = await openDialog({
      title: "정말 삭제하시겠습니까?",
      target: p,
      body: html`
        <p>NFS 폴더에서 파일을 삭제합니다. 되돌릴 수 없습니다.${state.dirty ? " 저장하지 않은 변경도 사라집니다." : ""}</p>
        <label class="check">
          <input type="checkbox" name="withResources" ${parsed ? "checked" : "disabled"}>
          클러스터 리소스도 함께 삭제
          ${parsed ? html`<span class="muted">(${live.length ? live.map((r) => `${r.kind}/${r.name}`).join(", ") : "클러스터에 있는 리소스 없음"})</span>`
            : html`<span class="muted">(파일을 해석할 수 없어 선택 불가)</span>`}
        </label>
        <p class="muted small">체크를 끄면 리소스는 클러스터에 남고 '선언 없음'으로 표시됩니다.</p>`,
      buttons: [
        { value: "no", label: "아니오", autofocus: true },
        { value: "yes", label: "삭제", cls: "btn btn-danger" },
      ],
    });
    if (value !== "yes") return; // [아니오] — 요청 보내지 않음
    state.busy = "delete";
    drawFile();
    try {
      const r = await mf.remove(p, !!data.withResources);
      const gone = r.resources.filter((x) => x.deleted).length;
      toast(`삭제했습니다: ${p}${data.withResources ? ` (클러스터 리소스 ${gone}개 삭제)` : ""}`, "ok");
      state.dirty = false;
      state.busy = null;
      location.hash = hashOf(null);
      listPoller.refreshNow();
      statusPoller.refreshNow();
    } catch (e) {
      state.busy = null;
      toast(`삭제 실패: ${e.message}`, "bad", 8000);
      drawAll();
    }
  }

  async function removeOrphan(o) {
    const { value } = await openDialog({
      title: "선언 없는 리소스를 삭제하시겠습니까?",
      target: `${o.kind}/${o.name}`,
      body: html`<p>어떤 매니페스트 파일에도 선언되지 않은 리소스입니다. 삭제하면 클러스터에서 사라집니다.</p>
        <p class="muted small">남겨두려면 이 리소스를 선언하는 YAML 파일을 만들어 관리 대상으로 되돌리세요.</p>`,
      buttons: [
        { value: "no", label: "아니오", autofocus: true },
        { value: "yes", label: "삭제", cls: "btn btn-danger" },
      ],
    });
    if (value !== "yes") return;
    try {
      await mf.removeOrphan(o);
      toast(`삭제했습니다: ${o.kind}/${o.name}`, "ok");
    } catch (e) {
      toast(`삭제 실패: ${e.message}`, "bad", 8000);
    }
    statusPoller.refreshNow();
  }

  // ------------------------------------------------------------ 새 파일
  async function newFile() {
    const dir = state.path?.includes("/") ? state.path.slice(0, state.path.lastIndexOf("/") + 1) : "";
    const { value, data } = await openDialog({
      title: "새 매니페스트 파일",
      body: html`
        <label class="field">경로 <span class="muted small">(.yaml / .yml, 하위 폴더는 / 로 구분)</span>
          <input type="text" name="path" class="mono" value="${dir}" placeholder="app/web.yaml" autocomplete="off" spellcheck="false" autofocus>
        </label>
        <p class="muted small">ConfigMap 예시 내용으로 만들어집니다. 만든 뒤 수정하고 [저장] → [적용] 하세요.</p>`,
      buttons: [
        { value: "no", label: "취소" },
        { value: "create", label: "만들기", cls: "btn btn-primary" },
      ],
      enterValue: "create",
    });
    if (value !== "create") return;
    const p = (data.path ?? "").trim();
    try {
      await mf.create(p, templateFor(p));
      toast(`만들었습니다: ${p}`, "ok", 3000);
      await loadList();
      location.hash = hashOf(p);
    } catch (e) {
      toast(`만들기 실패: ${e.message}`, "bad", 8000);
    }
  }

  // ------------------------------------------------------------ 이벤트
  el.addEventListener("click", (e) => {
    const dirBtn = e.target.closest("[data-dir]");
    if (dirBtn) {
      const d = dirBtn.dataset.dir;
      if (state.collapsed.has(d)) state.collapsed.delete(d); else state.collapsed.add(d);
      drawSide();
      return;
    }
    const action = e.target.closest("[data-action]")?.dataset.action;
    if (action === "new") newFile();
    if (action === "save") save();
    if (action === "apply") apply();
    if (action === "delete") remove();
    if (action === "reload") reloadFile().then(judgeCurrent);
    if (action === "orphan") {
      const o = state.statusAll?.orphans?.[Number(e.target.closest("[data-i]").dataset.i)];
      if (o) removeOrphan(o);
    }
  });

  const onBeforeUnload = (e) => { if (state.dirty) { e.preventDefault(); e.returnValue = ""; } };
  window.addEventListener("beforeunload", onBeforeUnload);

  const listPoller = createPoller(loadList, LIST_MS);
  const statusPoller = createPoller(loadStatusAll, STATUS_MS);
  const judgePoller = createPoller(async () => { if (state.path && !state.busy) await judgeCurrent(); }, JUDGE_MS);

  drawAll();
  listPoller.start();
  statusPoller.start();
  openFile(pathFromParams(params)).then(() => judgePoller.start());

  return {
    update(p) {
      const next = pathFromParams(p);
      if (next !== state.path) openFile(next);
    },
    // 저장 안 한 변경이 있으면 이동 전에 확인 (같은 파일로의 이동은 통과)
    async beforeLeave(next) {
      if (!state.dirty) return true;
      if (next.path === "/manifests" && pathFromParams(next.params) === state.path) return true;
      const { value } = await openDialog({
        title: "저장하지 않은 변경이 있습니다",
        target: state.path,
        body: html`<p>이동하면 편집한 내용이 사라집니다.</p>`,
        buttons: [
          { value: "stay", label: "계속 편집", autofocus: true },
          { value: "discard", label: "버리고 이동", cls: "btn btn-danger" },
        ],
        cancelValue: "stay",
      });
      return value === "discard";
    },
    unmount() {
      listPoller.stop();
      statusPoller.stop();
      judgePoller.stop();
      window.removeEventListener("beforeunload", onBeforeUnload);
    },
  };
}
