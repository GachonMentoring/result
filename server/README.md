# K8s Resource API (Work 1 · 2 · 3)

`mentor` 네임스페이스의 리소스 상태를 Watch 루프 + 메모리 캐시로 유지하고 GET API로 제공하는 FastAPI 서버.
Work 2: 삭제 / 롤아웃 재시작 / 이벤트(10개 단위 페이지) / 파드 로그.
Work 3: 매니페스트(YAML) 수정·저장·삭제·apply + 선언 ↔ 실제 판정.

## 실행 (로컬, kubeconfig 사용)

```bash
cd server
python -m venv .venv
.venv/Scripts/pip install -r requirements-dev.txt     # Windows (Linux/mac: .venv/bin/pip)

# PowerShell
$env:WATCH_NAMESPACE="mentor"; .venv/Scripts/python -m uvicorn app.main:app --port 8000
# bash
WATCH_NAMESPACE=mentor .venv/Scripts/python -m uvicorn app.main:app --port 8000
```

Swagger: http://localhost:8000/docs

## 환경변수

| 이름 | 기본값 | 설명 |
|---|---|---|
| `WATCH_NAMESPACE` | (파드 안이면 자기 네임스페이스) | 조회할 네임스페이스 |
| `WATCH_TIMEOUT_SECONDS` | 300 | watch 스트림 1회 최대 유지 시간 |
| `MANIFEST_DIR` | `server/dev-manifests` | 매니페스트 폴더 (운영: NFS 마운트 경로) |
| `LOG_LEVEL` | INFO | `DEBUG`로 두면 이벤트 하나하나 로그 |

## API

| Method | Path | 설명 |
|---|---|---|
| GET | `/healthz` | 프로세스 생존 |
| GET | `/readyz` | 모든 watcher 첫 sync 완료 시 200, 이전엔 503 |
| GET | `/api/v1/kinds` | watcher 상태 (rv, 마지막 이벤트 시각, re-list 횟수, 에러) |
| GET | `/api/v1/resources` | kind별 개수 |
| GET | `/api/v1/resources/{kind}?label_selector=app=mlflow` | 목록 (요약) |
| GET | `/api/v1/resources/{kind}/{name}` | 단건 (요약 + 원본 manifest + 가능한 조작 `actions`) |
| GET | `/api/v1/resources/{kind}/{name}/events` | 이 리소스의 이벤트 (최신순) |
| GET | `/api/v1/resources/pods/{name}/logs?container=&tail_lines=200&previous=false` | 파드 로그 |
| DELETE | `/api/v1/resources/{kind}/{name}?confirm=true&uid=` | 삭제 → 202 |
| POST | `/api/v1/resources/{kind}/{name}/restart?confirm=true` | 롤아웃 재시작 (Deployment/StatefulSet) → 202 |

목록·이벤트 API는 `limit`/`offset` 으로 페이지 단위 조회 가능 (`count`는 전체 개수).

### 매니페스트 관리 (Work 3)

| Method | Path | 설명 |
|---|---|---|
| GET | `/api/v1/manifests` | 파일 목록 (하위 폴더, .yaml/.yml, etag) |
| GET | `/api/v1/manifests/file?path=` | 내용 + etag |
| PUT | `/api/v1/manifests/file?path=&force=` | 저장 `{content, etag}`. etag 다르면 409 (외부에서 바뀜), YAML 오류 400(+line) |
| POST | `/api/v1/manifests/file?path=` | 새 파일 `{content}` (있으면 409) |
| DELETE | `/api/v1/manifests/file?path=&confirm=true&with_resources=true` | 파일 (+ 선언한 클러스터 리소스) 삭제 |
| GET | `/api/v1/manifests/status?path=` | 판정 + 리소스별 diff·소유권 충돌 |
| GET | `/api/v1/manifests/status-all` | 전체 판정 요약 + 선언 없음 목록 |
| POST | `/api/v1/manifests/apply?path=&confirm=true&force=` | **저장된 파일**을 Server-Side Apply. 충돌 시 409(+conflicts) |
| DELETE | `/api/v1/manifests/orphans?api_version=&kind=&name=&confirm=true` | 선언 없는 리소스 삭제 |

- 판정: 파일을 dry-run apply 한 결과 vs 현재 클러스터 → `synced`(일치) / `drift`(불일치) / `missing`(미적용) / `error` / `empty`
- 필드 관리자 `mentor-platform`. mentor 네임스페이스 리소스만 허용 (클러스터 범위·다른 네임스페이스 거부)
- 선언 없음: `mentor-platform`이 apply 했지만 어떤 파일에도 없는 리소스

### 이벤트 저장 위치

Event는 쿠버네티스 API 객체라 **클러스터 etcd**에 저장되고 kube-apiserver 기본값으로 **1시간 후 삭제**됩니다.
이 서버는 DB 없이 Watch로 받아 **메모리 캐시**에만 보관합니다 (재시작 시 다시 List).

### 조작 규칙 (Work 2)

- **`confirm=true` 없으면 400** — 화면의 확인창에서 [예]를 누른 요청만 보낸다.
- `platform.mentor/protected=true` 라벨이 있으면 삭제 403. Terminating 중이면 409.
- 삭제는 `propagationPolicy=Background`(kubectl과 동일) + 화면에서 본 객체의 uid precondition (다른 객체 삭제 방지).
- 재시작은 `spec.template.metadata.annotations["kubectl.kubernetes.io/restartedAt"]` 만 patch. paused Deployment는 409.
- 응답의 `effect` — 선언(매니페스트)과 실제의 일치에 주는 영향:
  `reconcile`(컨트롤러가 복구) / `drift`(매니페스트와 불일치 발생) / `rollout`(spec 변경 없이 파드 교체)
- replicas 조정은 선언을 API로 직접 바꾸는 조작이라 제공하지 않음 (이후 매니페스트 apply Work).

`kind`: `pods`(`po`), `deployments`(`deploy`), `statefulsets`(`sts`), `services`(`svc`), `jobs`, `persistentvolumeclaims`(`pvc`), `events`(`ev`, 조회 전용)

PV는 클러스터 범위 리소스라 ClusterRole이 필요해서 제외했습니다. PVC 요약의 `volume` 필드로 바인딩된 PV 이름을 확인할 수 있습니다.

## 구조

```
app/
├─ main.py          FastAPI 앱, lifespan에서 watcher 시작/종료
├─ config.py        WATCH_NAMESPACE 등 설정
├─ schemas.py       응답 모델
├─ api/routes.py    조회 API, label selector 필터
└─ k8s/
   ├─ client.py     in-cluster → kubeconfig 자동 선택
   ├─ registry.py   감시할 kind 정의 (추가는 여기 한 줄)
   ├─ cache.py      스레드 안전 캐시
   ├─ watcher.py    List → Watch 루프, 410 re-list, 백오프
   ├─ summarize.py  원본 → 요약 (terminating, owner 포함)
   └─ actions.py    삭제 / 재시작 / 로그, 조작 전 영향(effect) 판단, k8s 오류 매핑
└─ manifests/
   ├─ files.py      매니페스트 폴더 읽기/저장(etag 충돌, 원자적 쓰기, 경로 안전장치)
   └─ cluster.py    판정(dry-run diff) / apply(SSA) / 삭제 / 선언 없음
```

## 테스트

```bash
.venv/Scripts/python -m pytest -q
```

클러스터 없이 가짜 list/watch로 410 처리(예외/ERROR 이벤트 둘 다), re-list 시 캐시 교체, 일반 에러 재시도, 스트림 timeout 후 재watch를 검증합니다.

## 파드 배포 시 필요한 것 (Work 3 추가)

- 매니페스트 폴더: NFS PV/PVC (`192.168.2.40:/home/suhan/manifests`, `mountOptions: [actimeo=1]`) → `MANIFEST_DIR=/manifests`
- 파드 `securityContext.runAsUser/runAsGroup: 1000` (NFS root_squash, 파일 소유자 suhan)
- 매니페스트로 다룰 kind 마다 Role에 `get, list, create, patch, delete` (SSA = patch)

## 파드 배포 시 필요한 권한 (이후 Work)

`mentor` 네임스페이스에 Role + RoleBinding만 있으면 됩니다. (ClusterRole 불필요)

```yaml
rules:
  - apiGroups: [""]
    resources: [pods, services, persistentvolumeclaims]
    verbs: [get, list, watch, delete]
  - apiGroups: [""]
    resources: [events]
    verbs: [get, list, watch]
  - apiGroups: [""]
    resources: [pods/log]
    verbs: [get]
  - apiGroups: [apps]
    resources: [deployments, statefulsets]
    verbs: [get, list, watch, delete, patch]
  - apiGroups: [batch]
    resources: [jobs]
    verbs: [get, list, watch, delete]
```

배포 후 점검: `kubectl auth can-i delete pods -n mentor --as=system:serviceaccount:mentor:<sa이름>`
