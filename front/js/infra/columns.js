// kind별 테이블 컬럼 정의 (kubectl get 과 비슷하게)
import { EVENTS_PAGE_SIZE } from "../../config.js";
import { age, html } from "../util.js";
import { statusOf } from "./status.js";

export function badge(kind, item) {
  const { tone, text } = statusOf(kind, item);
  return html`<span class="badge badge-${tone}"><span class="dot" aria-hidden="true"></span>${text}</span>`;
}

const name = { label: "NAME", cell: (i) => html`<span class="mono name">${i.name}</span>` };
const ageCol = { label: "AGE", cell: (i) => age(i.created_at), cls: "num" };
const mono = (v) => html`<span class="mono">${v ?? "-"}</span>`;
const ratio = (a, b) => `${a ?? 0}/${b ?? 0}`;

export const KINDS = [
  {
    key: "pods", label: "Pods",
    columns: [
      name,
      { label: "STATUS", cell: (i) => badge("pods", i) },
      { label: "READY", cell: (i) => i.status.ready, cls: "num" },
      { label: "RESTARTS", cell: (i) => i.status.restarts, cls: "num" },
      { label: "NODE", cell: (i) => mono(i.status.node) },
      { label: "IP", cell: (i) => mono(i.status.pod_ip) },
      ageCol,
    ],
  },
  {
    key: "deployments", label: "Deployments",
    columns: [
      name,
      { label: "STATUS", cell: (i) => badge("deployments", i) },
      { label: "READY", cell: (i) => ratio(i.status.ready_replicas, i.status.replicas), cls: "num" },
      { label: "UP-TO-DATE", cell: (i) => i.status.updated_replicas, cls: "num" },
      { label: "AVAILABLE", cell: (i) => i.status.available_replicas, cls: "num" },
      ageCol,
    ],
  },
  {
    key: "statefulsets", label: "StatefulSets",
    columns: [
      name,
      { label: "STATUS", cell: (i) => badge("statefulsets", i) },
      { label: "READY", cell: (i) => ratio(i.status.ready_replicas, i.status.replicas), cls: "num" },
      ageCol,
    ],
  },
  {
    key: "services", label: "Services",
    columns: [
      name,
      { label: "TYPE", cell: (i) => badge("services", i) },
      { label: "CLUSTER-IP", cell: (i) => mono(i.status.cluster_ip) },
      { label: "PORTS", cell: (i) => mono((i.status.ports ?? []).join(", ") || "-") },
      { label: "SELECTOR", cell: (i) => labelChips(i.status.selector) },
      ageCol,
    ],
  },
  {
    key: "jobs", label: "Jobs",
    columns: [
      name,
      { label: "STATE", cell: (i) => badge("jobs", i) },
      { label: "COMPLETIONS", cell: (i) => ratio(i.status.succeeded, i.status.completions), cls: "num" },
      { label: "ACTIVE", cell: (i) => i.status.active, cls: "num" },
      { label: "FAILED", cell: (i) => i.status.failed, cls: "num" },
      ageCol,
    ],
  },
  {
    key: "persistentvolumeclaims", label: "PVC",
    columns: [
      name,
      { label: "STATUS", cell: (i) => badge("persistentvolumeclaims", i) },
      { label: "VOLUME", cell: (i) => mono(i.status.volume) },
      {
        label: "CAPACITY",
        cell: (i) => i.status.capacity && i.status.capacity !== i.status.requested
          ? html`${i.status.capacity} <span class="muted">(요청 ${i.status.requested})</span>`
          : (i.status.capacity ?? i.status.requested ?? "-"),
      },
      { label: "ACCESS", cell: (i) => (i.status.access_modes ?? []).join(", ") || "-" },
      { label: "STORAGECLASS", cell: (i) => i.status.storage_class || "-" },
      ageCol,
    ],
  },
  {
    key: "events", label: "Events",
    pageSize: EVENTS_PAGE_SIZE,   // 최신순 10개씩, ◀ ▶ 로 넘김
    columns: [
      { label: "LAST SEEN", cell: (i) => age(i.status.last_seen), cls: "num" },
      { label: "TYPE", cell: (i) => badge("events", i) },
      { label: "REASON", cell: (i) => html`<span class="name">${i.status.reason ?? "-"}</span>` },
      { label: "OBJECT", cell: (i) => mono(i.status.object) },
      { label: "MESSAGE", cell: (i) => i.status.message ?? "-", cls: "wrap" },
      { label: "COUNT", cell: (i) => i.status.count, cls: "num" },
    ],
  },
];

export const KIND_BY_KEY = Object.fromEntries(KINDS.map((k) => [k.key, k]));

export function labelChips(labels) {
  const entries = Object.entries(labels ?? {});
  if (!entries.length) return html`<span class="muted">-</span>`;
  return html`<span class="chips">${entries.map(([k, v]) => html`<span class="chip mono">${k}=${v}</span>`)}</span>`;
}
