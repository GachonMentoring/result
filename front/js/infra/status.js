// 리소스 요약 → 배지 톤(ok / warn / bad / neutral) + 표시 문구

const BAD_POD_REASONS = new Set([
  "CrashLoopBackOff", "Error", "ImagePullBackOff", "ErrImagePull", "InvalidImageName",
  "CreateContainerConfigError", "CreateContainerError", "RunContainerError",
  "OOMKilled", "Failed", "Evicted", "ContainerStatusUnknown",
]);

function readyParts(ready) {
  const [a, b] = String(ready ?? "0/0").split("/").map(Number);
  return { ready: a || 0, total: b || 0 };
}

function replicaTone(s) {
  const want = s.replicas ?? 0;
  const ready = s.ready_replicas ?? 0;
  if (want === 0) return { tone: "neutral", text: "0개로 축소" };
  if (ready >= want) return { tone: "ok", text: "정상" };
  if (ready === 0) return { tone: "bad", text: "준비된 파드 없음" };
  return { tone: "warn", text: "일부 준비 중" };
}

const RULES = {
  pods(s) {
    const text = s.display_status ?? s.phase ?? "Unknown";
    if (BAD_POD_REASONS.has(text) || s.phase === "Failed") return { tone: "bad", text };
    if (text === "Completed" || s.phase === "Succeeded") return { tone: "ok", text };
    if (text === "Terminating") return { tone: "neutral", text };
    if (s.phase === "Running") {
      const { ready, total } = readyParts(s.ready);
      return { tone: ready === total ? "ok" : "warn", text };
    }
    return { tone: "warn", text };
  },
  deployments: replicaTone,
  statefulsets: replicaTone,
  services: (s) => ({ tone: "neutral", text: s.type ?? "-" }),
  jobs(s) {
    const map = { Complete: "ok", Failed: "bad", Running: "warn", Pending: "warn", Suspended: "neutral" };
    return { tone: map[s.state] ?? "neutral", text: s.state ?? "-" };
  },
  events: (s) => ({ tone: s.type === "Warning" ? "warn" : "neutral", text: s.type ?? "-" }),
  persistentvolumeclaims(s) {
    const map = { Bound: "ok", Pending: "warn", Lost: "bad" };
    return { tone: map[s.phase] ?? "neutral", text: s.phase ?? "-" };
  },
};

export function statusOf(kind, item) {
  // 삭제 요청을 받고 종료 대기 중인 리소스 (모든 kind 공통)
  if (item.terminating) return { tone: "neutral", text: "Terminating" };
  return (RULES[kind] ?? (() => ({ tone: "neutral", text: "-" })))(item.status ?? {});
}

export function isUnhealthyPod(item) {
  const { tone } = RULES.pods(item.status ?? {});
  return tone === "bad" || tone === "warn";
}
