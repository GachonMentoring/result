// HTML 생성은 반드시 html`` 템플릿을 거친다: 끼워 넣는 값은 자동 이스케이프.
// 이미 안전한 HTML 조각만 raw()로 감싸서 그대로 넣을 수 있다.

class SafeHtml {
  constructor(s) { this.s = s; }
  toString() { return this.s; }
}

const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

export function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ESC[c]);
}

export function raw(s) {
  return new SafeHtml(String(s));
}

function fmt(v) {
  if (v instanceof SafeHtml) return v.s;
  if (Array.isArray(v)) return v.map(fmt).join("");
  if (v === null || v === undefined || v === false) return "";
  return esc(v);
}

export function html(strings, ...values) {
  let out = strings[0];
  values.forEach((v, i) => { out += fmt(v) + strings[i + 1]; });
  return new SafeHtml(out);
}

export function render(el, tpl) {
  el.innerHTML = fmt(tpl);
}

// kubectl 의 AGE 와 같은 표기: 45s, 12m, 5h, 10d
export function age(iso) {
  if (!iso) return "-";
  const sec = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 1000));
  if (sec < 120) return `${sec}s`;
  const min = Math.floor(sec / 60);
  if (min < 120) return `${min}m`;
  const hr = Math.floor(min / 60);
  if (hr < 48) return `${hr}h`;
  return `${Math.floor(hr / 24)}d`;
}

export function clock(date = new Date()) {
  return date.toLocaleTimeString("ko-KR", { hour12: false });
}

export function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}
