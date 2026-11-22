// 서버 주소와 갱신 주기.
//
// API_BASE
//   로컬 개발: 화면(5500)과 API(8000)가 다른 포트 → http://<같은 호스트>:8000
//   클러스터 배포: Ingress 가 화면과 API 를 같은 주소(mlops.studylink.click)로 묶음 → "" (같은 주소)
//   → 코드 수정 없이 두 환경 모두 동작 (k8sapimd/deploy.md 8-2 K)
const LOCAL_DEV = location.port === "5500";
export const API_BASE = LOCAL_DEV ? `${location.protocol}//${location.hostname}:8000` : "";

export const POLL_MS = 2000;            // 리소스 상태 갱신 주기
export const REQUEST_TIMEOUT_MS = 5000; // 요청 1건 최대 대기
export const EVENTS_PAGE_SIZE = 10;     // 이벤트 목록 한 페이지 개수 (◀ ▶ 로 넘김)
