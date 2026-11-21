// 가벼운 YAML 편집기: textarea + 줄 번호. 외부 라이브러리 없음.
//   Tab = 공백 2칸, Enter = 윗줄 들여쓰기 유지, Ctrl/Cmd+S = 저장, 오류 줄 강조
import { html, render } from "../util.js";

const INDENT = "  ";

export function createEditor(root, { onChange, onSave }) {
  render(root, html`
    <div class="code">
      <div class="code-gutter mono" aria-hidden="true"></div>
      <textarea class="code-text mono" spellcheck="false" autocomplete="off" autocapitalize="off"
                wrap="off" aria-label="YAML 편집기"></textarea>
    </div>`);
  const gutter = root.querySelector(".code-gutter");
  const ta = root.querySelector(".code-text");
  let errorLine = null;
  let lineCount = 0;

  function drawGutter(force = false) {
    const n = ta.value.split("\n").length;
    if (!force && n === lineCount) return;
    lineCount = n;
    let out = "";
    for (let i = 1; i <= n; i++) out += `<div${i === errorLine ? ' class="err"' : ""}>${i}</div>`;
    gutter.innerHTML = out;
  }

  // 되돌리기(Ctrl+Z)가 유지되도록 execCommand 우선, 안 되면 setRangeText
  function insert(text) {
    if (!document.execCommand("insertText", false, text)) {
      ta.setRangeText(text, ta.selectionStart, ta.selectionEnd, "end");
      ta.dispatchEvent(new Event("input"));
    }
  }

  ta.addEventListener("input", () => { drawGutter(); onChange?.(ta.value); });
  ta.addEventListener("scroll", () => { gutter.scrollTop = ta.scrollTop; });
  ta.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
      e.preventDefault();
      onSave?.();
    } else if (e.key === "Tab" && !e.shiftKey && !e.ctrlKey && !e.altKey) {
      e.preventDefault();
      insert(INDENT);
    } else if (e.key === "Enter" && !e.ctrlKey && !e.metaKey && !e.isComposing) {
      const before = ta.value.slice(0, ta.selectionStart);
      const line = before.slice(before.lastIndexOf("\n") + 1);
      let indent = line.match(/^\s*/)[0];
      if (/:\s*$/.test(line) || /^\s*-\s*$/.test(line)) indent += INDENT; // "key:" 다음 줄은 한 단계 더
      e.preventDefault();
      insert("\n" + indent);
    }
  });

  return {
    /** 내용 교체. keepView=true 면 커서·스크롤 위치 유지 (외부 변경 다시 불러오기) */
    setValue(text, { keepView = false } = {}) {
      const { selectionStart: s, selectionEnd: en, scrollTop } = ta;
      ta.value = text;
      errorLine = null;
      drawGutter(true);
      if (keepView) {
        ta.setSelectionRange(Math.min(s, text.length), Math.min(en, text.length));
        ta.scrollTop = scrollTop;
      } else {
        ta.setSelectionRange(0, 0);
        ta.scrollTop = 0;
      }
      gutter.scrollTop = ta.scrollTop;
    },
    getValue: () => ta.value,
    /** 오류 줄 강조 + 그 줄로 이동 (null 이면 해제) */
    markError(line) {
      errorLine = line ?? null;
      drawGutter(true);
      if (!line) return;
      const lines = ta.value.split("\n");
      const pos = lines.slice(0, line - 1).reduce((acc, l) => acc + l.length + 1, 0);
      ta.focus();
      ta.setSelectionRange(pos, pos + (lines[line - 1]?.length ?? 0));
      const lh = parseFloat(getComputedStyle(ta).lineHeight) || 20;
      ta.scrollTop = Math.max(0, (line - 4) * lh);
      gutter.scrollTop = ta.scrollTop;
    },
    setReadOnly(ro) { ta.readOnly = ro; },
    focus: () => ta.focus(),
  };
}
