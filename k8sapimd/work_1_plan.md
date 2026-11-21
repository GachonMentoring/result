# Work 1 진행 계획 (구현 전 제출)

> 대상: `work_1.md` — K8s 리소스 상태를 GET으로 반환하는 Python(FastAPI) API 서버
> 배경: `base.md` — 선언형 관리(Desired vs Current)를 보여주는 미니 운영 플랫폼의 **첫 번째 기반 서버**

---

## 1. 이번 Work의 범위

| 포함 (Work 1) | 제외 (이후 Work) |
|---|---|
| 클러스터 리소스 **조회(GET)** API | 리소스 생성/수정/삭제, 매니페스트 apply |
| Watch 루프 + 메모리 캐시 | Desired vs Current 비교(drift 점검) |
| 로컬 kubeconfig로 실행 | ServiceAccount / RBAC / Dockerfile / 파드 배포 |
| 헬스체크, 기본 테스트 | 재학습 Job 트리거, 모니터링, 프론트엔드 |

단, **파드 배포를 염두에 두고** 설정 로딩은 `in-cluster → kubeconfig` 순으로 자동 전환되게 만들어 둡니다. (나중에 SA만 붙이면 코드 수정 없이 동작)

---

## 2. Watch 루프 + 메모리 캐시 검토 결과

### 결론: **채택. 단, 아래 보완점 포함해서 구현**

이 방식은 client-go의 Informer(List → Watch → 캐시) 패턴을 Python으로 단순화한 것이고, 이후 Work(리소스 상태 시각화, drift 점검)에서 "현재 상태"를 빠르게, 반복적으로 조회해야 하므로 요청마다 API 서버를 직접 호출하는 방식보다 적합하다고 판단했습니다.

| 비교 | 요청 시 직접 조회 | Watch + 캐시 (채택) |
|---|---|---|
| 구현 난이도 | 매우 쉬움 | 중간 |
| API 서버 부하 | 요청 수에 비례 | 최초 List + Watch 스트림만 |
| 응답 속도 | 매번 네트워크 왕복 | 메모리 조회 |
| 변화 감지(이후 실시간 UI/셀프힐링 시연) | 불가 (polling 필요) | 이벤트로 즉시 반영 |

### 동작 흐름 (리소스 종류당 스레드 1개)

```
[시작] LIST → 캐시 전체 교체, resourceVersion(RV) 저장
   ↓
WATCH(resource_version=RV, allow_watch_bookmarks=True, timeout_seconds=300)
   ├ ADDED / MODIFIED → 캐시 upsert, RV 갱신
   ├ DELETED          → 캐시에서 제거, RV 갱신
   ├ BOOKMARK         → RV만 갱신
   ├ 스트림 정상 종료(timeout) → 마지막 RV로 다시 WATCH
   ├ 410 Gone         → RE-LIST (캐시 통째로 교체) 후 WATCH
   └ 기타 에러        → 지수 백오프(1s → 최대 30s) 후 재시도
```

### 원안에서 보완할 점

1. **410은 두 경로로 온다** — Python 클라이언트에서는 `ApiException(status=410)` 예외뿐 아니라, 스트림 안의 `type: ERROR` 이벤트(`object.code == 410`)로도 들어옵니다. 둘 다 처리하지 않으면 re-list가 안 되고 무한 재시도에 빠집니다.
2. **Bookmark 사용** — `allow_watch_bookmarks=True`로 RV를 주기적으로 갱신받아 410 발생 자체를 줄입니다.
3. **re-list 시 캐시는 "교체"** — 410 동안 놓친 삭제 이벤트가 있을 수 있으므로 기존 캐시에 덮어쓰기가 아니라 새 dict로 원자적 교체.
4. **스레드 안전성** — 캐시 접근은 `threading.Lock`으로 보호, 조회 시에는 복사본 반환.
5. **동기화 완료 여부 노출** — 최초 LIST 전엔 빈 목록이 "리소스 없음"으로 오해될 수 있으므로 `/readyz`는 모든 watcher가 첫 sync를 끝낸 뒤에만 200.
6. **스레드 수명** — FastAPI `lifespan`에서 daemon 스레드 시작, 종료 시 stop 이벤트로 정리.
7. **조회 범위 (확정: 네임스페이스 1개)** — `list_namespaced_*` 만 사용. 대상은 환경변수 `WATCH_NAMESPACE`로 지정하고, 미지정 시 파드 안에서는 `/var/run/secrets/kubernetes.io/serviceaccount/namespace`(자기 네임스페이스)를 사용. 로컬에서는 미지정이면 시작 시 에러로 명확히 알림. → 나중에 SA 만들 때 **Role + RoleBinding(get/list/watch)** 만으로 충분하고 ClusterRole은 필요 없습니다.

> 스레드 방식 vs asyncio(`kubernetes_asyncio`): 공식 `kubernetes` 클라이언트가 동기 방식이고 리소스 종류가 6~7개 수준이라 스레드로 충분합니다. 일정(4~5일)을 고려해 검증된 공식 클라이언트 + 스레드로 갑니다.

---

## 3. 감시할 리소스 (1차)

플랫폼 목표(서빙 API, DB, 재학습 Job)에 필요한 것 위주:

| Kind | 용도 |
|---|---|
| Pod | 서빙/DB/Job 파드 상태 |
| Deployment | 모델 서빙 API |
| StatefulSet | DB |
| Service | 서빙 엔드포인트 |
| Job | 데이터셋 생성 / 재학습 |

Node는 클러스터 범위 리소스라 Role로 조회할 수 없어 **제외** (확정).

`registry.py`에 kind → (list 함수, 요약 함수) 매핑으로 정의해서 종류 추가가 한 줄로 끝나게 합니다. (ConfigMap, PVC, CronJob 등은 필요 시 추가)

---

## 4. API 설계

| Method | Path | 설명 |
|---|---|---|
| GET | `/healthz` | 프로세스 생존 |
| GET | `/readyz` | 모든 watcher 첫 sync 완료 여부 |
| GET | `/api/v1/kinds` | 지원 kind 목록 + 각 watcher 상태(synced, 마지막 RV, 마지막 이벤트 시각) |
| GET | `/api/v1/resources` | 전체 kind별 요약 개수 (대시보드용) |
| GET | `/api/v1/resources/{kind}` | 목록. 쿼리: `label_selector` |
| GET | `/api/v1/resources/{kind}/{name}` | 단건 상세 |

네임스페이스가 1개로 고정이므로 경로에서 namespace는 뺍니다. (응답에는 namespace 필드 포함)

응답은 원본 객체 전체가 아니라 **요약 스키마(Pydantic)** 로 반환:
- 공통: `name`, `namespace`, `kind`, `labels`, `created_at`, `age`
- Pod: `phase`, `ready (1/1)`, `restarts`, `node`
- Deployment/StatefulSet: `replicas desired/ready/available`
- Job: `active/succeeded/failed`, `completion_time`
- Service: `type`, `cluster_ip`, `ports`

단건 상세에는 `spec`/`status` 원본도 포함해 두어, 다음 Work(매니페스트 확인, Desired vs Current 비교)에서 재사용합니다.

---

## 5. 디렉토리 구조

```
server/
├─ app/
│  ├─ main.py           # FastAPI 앱, lifespan에서 watcher 시작/종료
│  ├─ config.py         # 환경변수 (WATCH_NAMESPACE, WATCH_TIMEOUT 등)
│  ├─ k8s/
│  │  ├─ client.py      # in-cluster → kubeconfig 자동 로딩
│  │  ├─ registry.py    # 감시 대상 kind 정의
│  │  ├─ cache.py       # 스레드 안전 메모리 캐시
│  │  ├─ watcher.py     # List/Watch 루프, 410 re-list, 백오프
│  │  └─ summarize.py   # k8s 객체 → 요약 dict
│  ├─ api/routes.py
│  └─ schemas.py
├─ tests/
│  ├─ test_watcher.py   # 410(예외/ERROR 이벤트) → re-list 검증 (mock)
│  └─ test_routes.py
├─ requirements.txt     # fastapi, uvicorn, kubernetes, pydantic, pytest
└─ README.md            # 실행 방법
```

---

## 6. 진행 순서

1. 프로젝트 골격 + 설정 로딩 + `/healthz`
2. `cache.py`, `watcher.py` (Pod 하나로 먼저 검증)
3. 410 처리/백오프 단위 테스트 (mock watch 스트림)
4. registry에 나머지 kind 추가 + 요약 함수
5. 조회 API 라우트 + `/readyz`, `/api/v1/kinds`
6. 로컬 PC에서 kubeconfig로 **실제 클러스터**의 대상 네임스페이스에 붙어 수동 검증 (조회만 하므로 클러스터에 변경 없음)
   - 파드 삭제 → Deployment가 재생성 → API 목록에 즉시 반영되는지 (**셀프힐링이 API에 보이는 것**까지 확인 — base.md 핵심 메시지와 연결)
7. README에 실행법 + 나중에 필요한 RBAC 권한 메모

---

## 7. 완료 기준

- [x] `uvicorn app.main:app` 실행 시 로컬 kubeconfig로 클러스터 연결
- [x] 5개 kind 목록/단건 GET 동작 (대상 네임스페이스만)
- [x] 이벤트가 캐시에 반영 (실제 클러스터: MODIFIED 확인 · 생성/삭제는 단위 테스트로만 확인, 실제 파드 삭제 테스트는 미실시)
- [x] 410 (예외 + ERROR 이벤트) 시 re-list 테스트 통과
- [x] `/readyz`가 첫 sync 전 503, 이후 200

---

## 8. 확정 사항

- 배포: 실제 K8s 클러스터 (파드 + ServiceAccount는 이후 Work)
- 조회 범위: 네임스페이스 1개 (`WATCH_NAMESPACE`) → 권한은 Role/RoleBinding
- Node 제외 → 감시 kind 5개
- 개발 중 테스트: 로컬 kubeconfig로 실제 클러스터 접속 (현재 context `kubernetes-admin@kubernetes`)
- 코드 위치: 레포 루트 `server/`

- 대상 네임스페이스: **`mentor`** (현재 mlflow, toxicity-filter Deployment/Service 존재)

## 9. 참고

- 로컬 kubeconfig가 `kubernetes-admin`(관리자 권한)이라 개발 중엔 권한 문제가 드러나지 않습니다. 파드 배포 때 Role 권한이 부족하면 403이 날 수 있으므로, 배포 Work에서 `kubectl auth can-i --as=system:serviceaccount:<ns>:<sa>`로 점검할 예정입니다.
