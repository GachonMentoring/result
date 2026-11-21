# Front 1 진행 계획 (구현 전 제출)

> 대상: `front_1.md` — 좌측 메뉴(댓글 기능 / 피드백 / 인프라 관리) + **인프라 관리** 화면에서 Work 1 API로 리소스 상태 시각화
> 짝: `work_1` (server/) — GET API만 사용, 쓰기 기능 없음

---

## 1. 확정 사항

| 항목 | 결정 |
|---|---|
| 기술 | **HTML + Vanilla JS** (빌드 도구 없음, ES Module) |
| 위치 | 레포 루트 **`front/`** |
| 서버 연결 | 프론트가 서버 주소를 직접 호출 → **server에 CORS 추가** |
| 갱신 방식 | **주기적 polling 2초** (확정) |
| 화면 구성 | 개요 카드 + kind별 목록 테이블 + 상세 패널(요약 + manifest) |

---

## 2. 범위

| 포함 (Front 1) | 제외 (이후) |
|---|---|
| 좌측 메뉴 3개 + 해시 라우팅 (`#/infra`, `#/comments`, `#/feedback`) | 댓글 기능 / 피드백 화면 실제 구현 (자리만 만들어 둠) |
| 인프라 관리: 개요 카드, 목록 테이블, 상세 패널 | manifest 수정·apply, Desired vs Current 비교 |
| server CORS 설정 (환경변수로 허용 origin 지정) | 프론트 배포(컨테이너/Ingress) |

---

## 3. 화면 설계 (인프라 관리)

```
┌──────────┬───────────────────────────────────────────────────────────┐
│ 댓글 기능 │  인프라 관리 · ns: mentor     ● 동기화됨  갱신 3초 전  [⟳] │
│ 피드백    ├───────────────────────────────────────────────────────────┤
│▶인프라관리│ [Pods 1] [Deploy 1] [STS 0] [Svc 1] [Jobs 0] [PVC 1] [비정상 0]│
│          ├───────────────────────────────────────────────────────────┤
│          │ Pods | Deployments | StatefulSets | Services | Jobs | PVC   │
│          │ 🔍 label selector [app=mlflow      ]                       │
│          │ NAME              STATUS        READY RESTARTS NODE   AGE  │
│          │ toxicity-filter-… ● Running      1/1     0    worker2  20h │──▶ 상세 패널
└──────────┴───────────────────────────────────────────────────────────┘
```

- **개요 카드**: `/api/v1/resources` 의 kind별 개수 + **비정상 파드 수** (Running·Succeeded 아니거나 ready 미달). 카드 클릭 → 해당 탭.
- **목록 테이블**: 탭 = kind. kind마다 컬럼을 다르게 정의 (kubectl get 과 비슷하게)

  | kind | 컬럼 |
  |---|---|
  | Pod | NAME, STATUS, READY, RESTARTS, NODE, IP, AGE |
  | Deployment / StatefulSet | NAME, READY(ready/replicas), UP-TO-DATE, AVAILABLE, AGE |
  | Service | NAME, TYPE, CLUSTER-IP, PORTS, SELECTOR, AGE |
  | Job | NAME, STATE, COMPLETIONS(succeeded/completions), ACTIVE, FAILED, AGE |
  | PVC | NAME, STATUS, VOLUME, CAPACITY(요청 → 실제), ACCESS, STORAGECLASS, AGE |

- **상태 색상 배지**: 초록(Running/Bound/Complete/ready 일치), 노랑(Pending/진행 중/ready 미달), 빨강(CrashLoopBackOff/Error/Failed/Lost)
- **상세 패널**(우측 슬라이드): `/api/v1/resources/{kind}/{name}` → 요약 + 라벨 + **manifest YAML**
  - `metadata.managedFields` 는 kubectl처럼 기본 숨김 (토글로 보기)
  - 복사 버튼
- **헤더 상태**: `/readyz` 결과(동기화 중/완료), 마지막 갱신 시각, 서버 연결 실패 시 빨간 배너

## 4. 동작

- polling: 2초마다 현재 보고 있는 것만 조회 (개요 + 현재 탭 목록 + 열린 상세)
- 브라우저 탭이 안 보일 때(`visibilitychange`)는 polling 정지 → 다시 보이면 즉시 갱신
- 요청이 겹치지 않게 이전 요청이 끝난 뒤 다음 타이머 시작
- 서버 다운 시 마지막 데이터 유지 + 오류 배너, 복구되면 자동 해제
- 셀프힐링 시연: 파드 삭제 → 2초 안에 Terminating → 새 파드 등장이 테이블에 보임

## 5. 서버 변경 (작음)

- `CORSMiddleware` 추가, 허용 origin은 환경변수 `CORS_ORIGINS` (기본 `http://localhost:5500`)
- 메서드는 GET만 허용

## 6. 디렉토리 구조

```
front/
├─ index.html          레이아웃(사이드바 + 본문)
├─ config.js           API_BASE (기본 http://localhost:8000), POLL_MS
├─ css/style.css       라이트/다크 대응
├─ js/
│  ├─ main.js          해시 라우터, 메뉴 활성화
│  ├─ api.js           fetch 래퍼 (타임아웃, 오류 처리)
│  ├─ poller.js        polling + 탭 숨김 시 정지
│  ├─ pages/
│  │  ├─ infra.js      인프라 관리 화면
│  │  ├─ comments.js   자리표시 ("준비 중")
│  │  └─ feedback.js   자리표시
│  └─ infra/
│     ├─ columns.js    kind별 컬럼 정의
│     ├─ status.js     상태 → 배지 색 판정
│     └─ detail.js     상세 패널, YAML 변환
├─ vendor/js-yaml.min.js   manifest YAML 표시용 (오프라인에서도 동작하게 파일로 포함)
└─ README.md
```

## 7. 실행 방법 (예정)

```powershell
# 1) 서버
cd server; $env:WATCH_NAMESPACE="mentor"; .venv\Scripts\python -m uvicorn app.main:app --port 8000
# 2) 프론트 (정적 파일 서버; ES Module은 file:// 로는 동작 안 함)
cd front; python -m http.server 5500
# 3) http://localhost:5500
```

## 8. 진행 순서

1. server CORS 추가 + 테스트
2. 레이아웃 + 사이드바 + 해시 라우팅 (댓글/피드백 자리표시)
3. api.js / poller.js
4. 개요 카드 + 헤더 상태
5. kind별 테이블 + 상태 배지 + label selector
6. 상세 패널 + YAML
7. 실제 클러스터로 확인 (서버 중단/복구, 탭 전환, 상세 갱신)

## 9. 완료 기준

- [ ] 좌측 메뉴 3개 전환 동작, 인프라 관리가 기본 화면
- [ ] 6개 kind 테이블이 kubectl get 결과와 일치
- [ ] 2초 polling으로 상태 변화 반영, 탭 숨김 시 요청 멈춤
- [ ] 행 클릭 시 상세 패널에 요약 + YAML 표시
- [ ] 서버 중단 시 오류 배너, 복구 시 자동 해제
- [ ] CORS: 허용 origin만 통과

## 10. 검토 의견 / 남은 확인 사항

- **Vanilla JS 선택에 따른 주의**: 화면이 늘어나도 관리되도록 페이지·컴포넌트를 파일 단위로 나누고(위 구조), 문자열로 HTML을 만들 때는 **이스케이프 처리**를 공통 함수로 강제합니다 (라벨·이름에 들어간 값이 그대로 HTML로 해석되지 않도록).
- **서버 주소**: 개발 중엔 `config.js`에서 `http://localhost:8000`. 클러스터 배포 시 주소(NodePort/Ingress)는 배포 Work에서 결정 — 그때 `config.js`만 바꾸면 되도록 함.
- **polling 주기 5초**가 괜찮은지 (시연 시 더 빠르게 보이려면 2~3초도 가능, 서버는 캐시 응답이라 부담 없음)

## 11. 구현 결과 (2026-11-21)

- [x] 좌측 메뉴 3개 전환 동작, 인프라 관리가 기본 화면
- [x] 6개 kind 테이블이 kubectl get 결과와 일치 (실제 클러스터 mentor 확인)
- [x] 2초 polling으로 상태 반영, 탭 숨김 시 요청 멈춤
- [x] 행 클릭 시 상세 패널에 요약 + YAML 표시
- [x] 서버 중단 시 오류 배너, 복구 시 자동 해제 (API 서버 kill → 재시작으로 확인)
- [x] CORS: 허용 origin만 통과 (server 테스트)

계획과 달라진 점
- 개요 카드와 탭이 같은 목록을 중복으로 보여줘서 **카드가 탭 역할**을 하도록 합침 (URL `#/infra/<kind>`로 유지)
- js-yaml은 `.mjs` 대신 `.js`로 포함 — Python http.server가 `.mjs`를 text/plain으로 보내 브라우저가 모듈 로드를 거부하기 때문

미확인
- 비정상 파드(빨간 배지/카드) 표시는 현재 클러스터에 비정상 파드가 없어 화면으로 확인하지 못함 (판정 로직만 구현)
- 실제 파드 삭제 → 셀프힐링 반영 시연은 하지 않음
