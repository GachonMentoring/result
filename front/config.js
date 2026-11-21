// 서버 주소와 갱신 주기. 클러스터에 배포할 때는 이 파일만 바꾸면 됩니다.
export const API_BASE = "http://localhost:8000";
export const POLL_MS = 2000;            // 리소스 상태 갱신 주기
export const REQUEST_TIMEOUT_MS = 5000; // 요청 1건 최대 대기
export const EVENTS_PAGE_SIZE = 10;     // 이벤트 목록 한 페이지 개수 (◀ ▶ 로 넘김)
