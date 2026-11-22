# 배포 설정 정리 (로컬 개발 → 클러스터 배포)

> 목적: 지금 PC 에서 돌리는 3개 서버(플랫폼 API · 모델 · 화면)를 `mentor` 네임스페이스에 파드로 올릴 때
> **무엇이 바뀌어야 하고, 각 값을 어디(ConfigMap / Secret / 매니페스트 / 이미지 / 코드)에 둘지** 정리한다.
> 기준: Work 1~5 완료 시점 코드 (환경변수 14개 + 하드코딩 값 전수 조사)

---

## 1. 배포 구성

```
브라우저 ──▶ Ingress (ingress-nginx, 192.168.2.100, host: mlops.studylink.click)
               ├─ /            ──▶ Service mentor-web  ──▶ Pod [nginx + front/ 정적 파일]
               └─ /api /healthz /readyz /docs ──▶ Service mentor-api ──▶ Pod [server/ 플랫폼 API]
                                                                          │  ServiceAccount mentor-platform (Role)
                                                                          │  /manifests ◀── PVC ◀── NFS 192.168.2.40:/home/suhan/manifests
                                                                          ├─▶ Service toxicity-filter:8088 ──▶ Pod [model/ 추론]
                                                                          └─▶ mariadb-0.mariadb-headless.database:3306 (쓰기)
```

- **화면과 API를 같은 주소(같은 출처)로 묶는다** → 브라우저가 다른 주소로 요청하지 않으므로 **CORS 설정 불필요**, 화면의 API 주소 하드코딩도 없앰
- 모델은 별도 파드 (Work 4 결정) — 기존 `mentor/toxicity-filter` 자리를 새 이미지로 교체
- 이미지 저장소: Docker Hub 공개 (`suhannugul/*`) — push 는 사용자 계정으로 직접

| 구성 요소 | 이미지 (예) | 포트 | 비고 |
|---|---|---|---|
| 플랫폼 API (`server/`) | `suhannugul/mentor-platform-api:<태그>` | 8000 | **Dockerfile 아직 없음** |
| 모델 (`model/`) | **`suhannugul/mentor:model_v1`** (Docker Hub 업로드 완료, digest `sha256:7883dd7c…`) | 8088 | 학습 → 서빙 2단계. 모델 버전 `seed-633bdbb4ded6`, Python 3.11.18, sklearn 1.9.1 |
| 화면 (`front/`) | `suhannugul/mentor-platform-web:<태그>` (nginx) | 80 | **Dockerfile·nginx 설정 아직 없음** |

---

## 2. 어디에 둘지 판단 기준

| 분류 | 넣는 것 | 이유 |
|---|---|---|
| **ConfigMap** | 주소·포트·이름·임계값·로그 레벨 등 **보여도 되는 설정** | 이미지 재빌드 없이 환경(개발/운영)별로 바꿈 |
| **Secret** | **DB 계정·비밀번호** | 매니페스트에는 Secret 이름만 남음, RBAC 로 조회 제한. ※ base64 일 뿐 암호화 아님 (가이드 10장) |
| **Deployment 에 직접** | 볼륨 마운트 경로처럼 **매니페스트 구조와 짝을 이루는 값** (`MANIFEST_DIR`) | 마운트 경로와 같은 파일에 있어야 어긋나지 않음 |
| **이미지에 고정** | 모델 파일 경로·모델 버전 | 이미지와 함께 움직여야 하는 값 |
| **쿠버네티스 리소스** | 권한(ServiceAccount·Role), 저장소(PV/PVC), 노출(Service·Ingress), probe | 환경변수가 아니라 리소스로 선언 |
| **코드 수정** | 지금 개발용으로 하드코딩된 값 | 4장 목록 |

⚠ **ConfigMap·Secret 을 바꿔도 실행 중인 파드에는 반영되지 않는다** — 환경변수는 컨테이너 시작 때 한 번 읽힘 (가이드 17장 시나리오 3).
→ 바꾼 뒤 **롤아웃 재시작** 필요 (Work 2 인프라 관리 화면의 [재시작] 사용 가능).

---

## 3. 설정값 전체 목록

### 3-1. 플랫폼 API (`server/`)

| 환경변수 | 로컬 (지금) | 클러스터 값 | 둘 곳 | 비고 |
|---|---|---|---|---|
| `WATCH_NAMESPACE` | `mentor` (직접 지정) | 생략 가능 | — | 파드 안에서는 ServiceAccount 의 자기 네임스페이스를 자동 사용 (`config.py`) |
| `WATCH_TIMEOUT_SECONDS` | 300 | 300 | ConfigMap | 기본값이면 생략 가능 |
| `LOG_LEVEL` | INFO | INFO | ConfigMap | 장애 조사 시 DEBUG |
| `CORS_ORIGINS` | `http://localhost:5500,…` | **불필요** | (ConfigMap) | 1장처럼 같은 주소로 묶으면 안 씀. 화면을 다른 주소로 띄울 때만 그 주소 |
| `MANIFEST_DIR` | 미지정 → `server/dev-manifests` | `/manifests` | **Deployment env** | `volumeMounts.mountPath` 와 반드시 같은 값 (5장) |
| `MODEL_URL` | `http://localhost:8088` | `http://toxicity-filter.mentor.svc.cluster.local:8088` | ConfigMap | Service 이름으로 (IP 금지 — 재생성 시 바뀜, 가이드 16장) |
| `THRESHOLD` | 0.5 | 0.5 | ConfigMap | 바꾸면 재시작 필요. 행마다 DB 에 함께 저장되므로 변경 이력 추적 가능 |
| `MODEL_TIMEOUT` | 3 | 3 | ConfigMap | |
| `DB_HOST` | `192.168.2.30` (NodePort) | **`mariadb-0.mariadb-headless.database.svc.cluster.local`** | ConfigMap | ⚠ `mariadb-read` 는 두 파드(쓰기+복제본)를 모두 가리켜 **쓰기 실패 가능** → 쓰기 전용 주소 사용 |
| `DB_PORT` | `31306` (NodePort) | **`3306`** | ConfigMap | 클러스터 안에서는 NodePort 가 아니라 Service 포트 |
| `DB_NAME` | toxicity | toxicity | ConfigMap | |
| `DB_USER` | mentor_app | mentor_app | **Secret** | 가이드 16장처럼 계정도 Secret 에 |
| `DB_PASSWORD` | 실행할 때 직접 입력 | (실제 비밀번호) | **Secret** | 없으면 피드백 기능 꺼짐 (분류는 동작) |
| (kubeconfig) | PC 의 `~/.kube/config` (관리자) | **없음 → ServiceAccount 토큰** | 리소스 | `client.py` 가 파드 안이면 자동으로 in-cluster 사용. 권한은 Role 로 (6장) |

### 3-2. 모델 (`model/`)

| 값 | 로컬 | 클러스터 | 둘 곳 | 비고 |
|---|---|---|---|---|
| `MODEL_PATH` | `artifacts/model.joblib` | `/app/artifacts/model.joblib` | **이미지 (Dockerfile `ENV`)** | 재학습 Work 에서 볼륨으로 바꿀 때만 Deployment env 로 덮어씀 |
| `MODEL_VERSION` | 데이터 해시 (`seed-…`) | 이미지 태그와 맞추기 권장 | **빌드 인자** `--build-arg MODEL_VERSION=v1` | DB `model_version` 에 기록되는 값 |
| `MODEL_NAME` | toxicity-filter | — | — | `model_info.json` 이 없을 때만 쓰는 예비값 |
| 권한 | — | `automountServiceAccountToken: false` | 매니페스트 | 쿠버네티스 권한 불필요 |
| 실행 사용자 | — | uid 10001 (이미지에 지정) | 이미지 | root 아님 |

### 3-3. 화면 (`front/config.js`)

| 값 | 지금 | 클러스터 | 둘 곳 | 비고 |
|---|---|---|---|---|
| `API_BASE` | `http://localhost:8000` | **`""` (빈 값 = 같은 주소)** | **코드 수정** | 같은 주소로 묶으면 환경마다 바꿀 필요 없음 → ConfigMap 불필요 |
| `POLL_MS` | 2000 | 2000 | (선택) ConfigMap | 바꿀 일이 있으면 `config.js` 를 ConfigMap 으로 만들어 `subPath` 마운트 |
| `REQUEST_TIMEOUT_MS` | 5000 | 5000 | 〃 | |
| `EVENTS_PAGE_SIZE` | 10 | 10 | 〃 | |

---

## 4. 배포 전에 코드에서 바꿀 것 (TODO)

| # | 위치 | 지금 | 바꿀 내용 |
|---|---|---|---|
| 1 | `front/config.js` | `API_BASE = "http://localhost:8000"` | `""` (같은 주소). 로컬 개발은 5500 → 8000 이라 다른 주소 → 로컬용 값을 따로 두는 방법 결정 필요 (예: `location.port === "5500"` 이면 `http://localhost:8000`) |
| 2 | `server/` | Dockerfile 없음 | 개발 환경과 같은 `python:3.14-slim` (지금 `server/.venv` 가 3.14), `uvicorn app.main:app --host 0.0.0.0 --port 8000`, root 아닌 사용자. `requirements.txt` 가 `>=` 라 빌드 시점마다 버전이 달라짐 → 버전 고정 권장 (모델과 같은 이유) |
| 3 | `server/` 실행 사용자 | — | **uid/gid 1000** (NFS `root_squash`, 파일 소유자 suhan) — 다른 uid 면 매니페스트 저장 실패 |
| 4 | `server/app/config.py` | `MANIFEST_DIR` 미지정 시 `dev-manifests` | 파드 안이면 기본 `/manifests` (Work 3 때 합의한 ③안, 아직 미반영) |
| 5 | `front/` | Dockerfile·nginx 설정 없음 | `nginx:alpine` 에 정적 파일 복사 (API 프록시는 Ingress 가 하므로 nginx 는 정적 파일만). **JS·CSS 캐시 헤더 필수**: `Cache-Control: no-cache` (또는 파일명에 버전) — 없으면 새 버전 배포 후에도 브라우저가 예전 JS 를 실행 (개발 중 실제로 겪음, `front/dev_server.py` 참고) |
| 6 | `server/app/main.py` | `/readyz` = Watch 동기화 | 그대로 readinessProbe 로 사용 가능. DB·모델 장애는 readiness 에 넣지 않음 (가이드 17장 퀴즈) |
| 7 | 플랫폼 자신 보호 | — | 플랫폼 Deployment·Service·PVC 에 `platform.mentor/protected: "true"` 라벨 → Work 2 삭제 기능으로 자기 자신을 지우는 사고 방지 |

---

## 5. ConfigMap · Secret · 볼륨 예시

### ConfigMap (`mentor-platform-config`)
```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: mentor-platform-config
  namespace: mentor
data:
  LOG_LEVEL: "INFO"
  MODEL_URL: "http://toxicity-filter.mentor.svc.cluster.local:8088"
  THRESHOLD: "0.5"
  MODEL_TIMEOUT: "3"
  DB_HOST: "mariadb-0.mariadb-headless.database.svc.cluster.local"
  DB_PORT: "3306"
  DB_NAME: "toxicity"
```

### Secret (`mentor-platform-db`) — **파일로 만들지 말고 명령으로**
```bash
kubectl -n mentor create secret generic mentor-platform-db \
  --from-literal=DB_USER='mentor_app' \
  --from-literal=DB_PASSWORD='<비밀번호>'
```
- YAML 파일로 만들면 비밀번호가 파일에 남아 git·NFS 에 올라갈 위험 (`.gitignore` 의 `*secret*.yaml` 은 안전망일 뿐)
- **NFS 매니페스트 폴더(`/home/suhan/manifests`)에 Secret YAML 을 두지 말 것** — Work 3 화면에 내용이 그대로 보임
- `--from-literal` 로 만들면 값 끝에 줄바꿈이 들어가지 않음 (`echo | base64` 로 만들 때의 흔한 실수 방지)

### Deployment (플랫폼 API) 중 설정 관련 부분
```yaml
spec:
  template:
    metadata:
      labels: {app: mentor-api, platform.mentor/protected: "true"}
    spec:
      serviceAccountName: mentor-platform
      securityContext: {runAsUser: 1000, runAsGroup: 1000}      # NFS 파일 소유자 suhan
      containers:
        - name: api
          image: suhannugul/mentor-platform-api:<태그>
          envFrom:
            - configMapRef: {name: mentor-platform-config}
            - secretRef: {name: mentor-platform-db}
          env:
            - {name: MANIFEST_DIR, value: /manifests}            # mountPath 와 같아야 함
          volumeMounts:
            - {name: manifests, mountPath: /manifests}
          readinessProbe: {httpGet: {path: /readyz, port: 8000}}
          livenessProbe: {httpGet: {path: /healthz, port: 8000}}
      volumes:
        - name: manifests
          persistentVolumeClaim: {claimName: mentor-manifests}
```

### NFS 볼륨 (Work 3 사전 검증과 같은 설정)
```yaml
apiVersion: v1
kind: PersistentVolume
metadata: {name: mentor-manifests-pv}
spec:
  capacity: {storage: 1Gi}
  accessModes: [ReadWriteMany]
  persistentVolumeReclaimPolicy: Retain          # 지워도 NFS 파일 유지
  storageClassName: ""
  mountOptions: [actimeo=1]                      # 외부 수정 반영 지연 최소화
  claimRef: {namespace: mentor, name: mentor-manifests}
  nfs: {server: 192.168.2.40, path: /home/suhan/manifests}
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata: {name: mentor-manifests, namespace: mentor}
spec:
  accessModes: [ReadWriteMany]
  storageClassName: ""
  volumeName: mentor-manifests-pv
  resources: {requests: {storage: 1Gi}}
```

---

## 6. 권한 (ServiceAccount + Role) — 로컬의 관리자 kubeconfig 를 대체

로컬에서는 `kubernetes-admin`(전체 권한)이라 드러나지 않던 권한 부족이 배포 후 **403** 으로 나타날 수 있다.
`mentor` 네임스페이스 Role 하나로 충분 (ClusterRole 불필요 — Work 1 결정).

| 기능 | 리소스 | verbs |
|---|---|---|
| Work 1 조회 · Watch | pods, services, persistentvolumeclaims, events / deployments, statefulsets / jobs | get, list, watch |
| Work 2 삭제 | pods, services, persistentvolumeclaims / deployments, statefulsets / jobs | delete |
| Work 2 재시작 | deployments, statefulsets | patch |
| Work 2 로그 | pods/log | get |
| Work 3 apply · 판정 · 삭제 | 매니페스트로 다룰 kind 전부 (위 + configmaps, secrets?, ingresses, cronjobs, serviceaccounts …) | get, list, create, patch, delete |

- Work 3 는 어떤 kind 든 apply 할 수 있으므로 **허용할 kind 를 Role 에 명시하는 것이 곧 apply 허용 범위**가 됨 → `secrets` 를 넣을지는 결정 필요 (넣으면 화면에서 Secret 생성·조회 가능)
- 배포 후 점검: `kubectl auth can-i --list -n mentor --as=system:serviceaccount:mentor:mentor-platform`

---

## 7. 배포 후 확인 체크리스트

- [ ] `kubectl exec deploy/mentor-api -n mentor -- env | grep -E "DB_|MODEL_|THRESHOLD"` — 값 주입 확인 (비밀번호는 출력 주의)
- [ ] `/readyz` 200 (Watch 동기화), 인프라 관리 화면에 `mentor` 리소스 표시 (권한 403 없음)
- [ ] 댓글 기능: 분류 → "DB에 저장됨" (DB_HOST 가 쓰기 주소인지)
- [ ] 매니페스트 관리: `/manifests` 목록 표시, 저장 시 NFS 에 파일 소유자 1000 으로 생성, NFS 에서 직접 수정 → 몇 초 안에 화면 반영 (Work 3 이월 항목)
- [ ] 모델 파드: `/info` 의 `version_mismatch: false`
- [ ] 브라우저 개발자 도구에 CORS 오류 없음 (같은 주소 구성 확인)

---

## 8. 배포 전 추가 검토 결과 (2026-11-22)

> 1~7장 대로 진행하면 기본 구성은 맞다. 아래는 코드·클러스터를 대조해 **추가로 필요하거나 보완할 것**.

### 8-1. 클러스터 확인 결과
| 항목 | 결과 |
|---|---|
| 모델 | `mentor/toxicity-filter` = `suhannugul/mentor:model_v1`, `automountServiceAccountToken: false`, probe `/healthz`, `/info` 버전 일치 ✅. **Service 가 ClusterIP 로 바뀜** → PC 에서 `:30089` 로 못 씀 (로컬 개발은 로컬 모델 서버 8088) |
| DB 주소 | `mentor` 파드 안에서 `mariadb-0.mariadb-headless…` → **10.244.140.60 (mariadb-0) 만** ✅ / `mariadb-read` → 두 파드 모두 (쓰기에 쓰면 안 됨 확인) |
| 네트워크 | NetworkPolicy 없음 → mentor → database:3306, mentor → toxicity-filter:8088 연결 ✅ |
| Ingress | ingress-nginx `192.168.2.100`, `proxy-body-size` 기본값(1m) 그대로 |
| mentor 네임스페이스 | ServiceAccount·Role·ConfigMap·Secret·PVC 아직 없음 (mlflow·toxicity-filter 만) |

### 8-2. 추가로 바꿀 것 / 정할 것
| # | 분류 | 내용 | 이유 |
|---|---|---|---|
| A | Ingress | 라우팅 경로에 **`/openapi.json`** 추가 (또는 `/docs` 를 빼기) | `/docs`(Swagger) 화면이 `/openapi.json` 을 불러옴 → 없으면 빈 화면 |
| B | Ingress | **host 기반 라우팅 필수** (`mlops.studylink.click`), 경로 기반(`/mentor/…`) 금지 | 화면이 `/api/…` 절대경로로 호출 → 하위 경로에 두면 깨짐 |
| C | Ingress | `nginx.ingress.kubernetes.io/proxy-body-size: "2m"` | 매니페스트 저장 상한이 1MB → JSON 포장 포함 시 기본 1m 초과 → **413** |
| D | DNS | **Route 53 에 `mlops.studylink.click` → 공인 IP** 등록. 공유기(NAT)가 80/443 을 내부 Ingress(192.168.2.100)로 포워딩 (기존 argocd·capstone 과 같은 구조) | 내부망에서 공인 도메인으로 접속이 안 되면 공유기의 헤어핀 NAT 지원 여부 확인, 또는 PC hosts 파일로 192.168.2.100 지정 |
| E | Role | Watch 대상 7종 (pods, deployments, statefulsets, services, jobs, persistentvolumeclaims, **events**) 의 `list, watch` **하나라도 빠지면 `/readyz` 가 영원히 503 → 파드가 Ready 안 됨** | readiness 가 "모든 watcher 동기화" 기준이라 권한 누락이 곧 배포 실패 |
| F | Role | Work 3 "선언 없음" 이 훑는 kind (configmaps, secrets, serviceaccounts, cronjobs, ingresses) 에 `list` 없으면 **조용히 건너뜀** → 목록 불완전 | `secrets` 포함 여부 결정과 함께 정리 |
| G | 서버 | **4장 #4 (파드 안 기본 `MANIFEST_DIR=/manifests`) 우선순위 상향** | uid 1000 으로 실행하면 `/app` 에 쓸 수 없음 → `MANIFEST_DIR` 을 빠뜨리면 `dev-manifests` 생성 실패로 **기동 실패(CrashLoop)** |
| H | 서버 | replicas **1**, uvicorn worker **1** | Watch 캐시가 프로세스 메모리 — 여러 개면 watch 연결·메모리가 배로 늘어남 |
| I | 서버 | `requirements.txt` 고정값 (지금 개발 환경): `fastapi==0.165.0 uvicorn==0.54.0 kubernetes==37.0.3 httpx==0.28.1 pydantic==2.14.4 PyMySQL==1.2.4 PyYAML==6.0.4` | `>=` 라 빌드 시점마다 버전이 달라짐 |
| J | 이미지 | `server/.dockerignore` (`.venv/ dev-manifests/ tests/ .pytest_cache/ __pycache__/`), `front/.dockerignore` (`dev_server.py README.md`) | 개발 폴더·가상환경이 이미지에 들어가지 않게 (공개 저장소) |
| K | 화면 | `config.js`: `API_BASE = location.port === "5500" ? "http://localhost:8000" : ""` | 로컬(5500→8000)과 배포(같은 주소)를 코드 수정 없이 둘 다 지원 — 4장 #1 의 방법 확정안 |
| L | 화면 | nginx 비루트 실행이 필요하면 `nginxinc/nginx-unprivileged:alpine` (포트 8080) | 기본 `nginx:alpine` 은 root·80 포트 |
| M | 운영 | 플랫폼 자신의 YAML 을 NFS 매니페스트 폴더에 두고 [적용]하면 **플랫폼이 스스로 재시작** | Work 3 로 자기 자신을 관리할 때 주의 |

### 8-3. 바꾸지 않아도 되는 것 (확인함)
- `CORS_ORIGINS` 기본값(localhost:5500) — 같은 주소 구성에선 쓰이지 않아 남겨도 무해
- 쿠버네티스 접속 — `client.py` 가 파드 안이면 ServiceAccount 로 자동 전환
- `WATCH_NAMESPACE` — 파드 안에서는 자기 네임스페이스 자동
- 매니페스트 임시 파일(`.파일명.tmp`) — NFS 폴더 안에 잠깐 생겼다 교체됨, 목록에서는 숨김 처리됨 (uid 1000 쓰기 권한만 있으면 됨)
- 시간대 — DB 는 UTC, API 가 `Z` 붙여 반환 → 화면이 현지 시각 표시

### 8-4. 확정 사항 (2026-11-22)
| 항목 | 결정 |
|---|---|
| 도메인 | **`mlops.studylink.click`** — Route 53 에는 **공인 IP**, 공유기가 80/443 → Ingress 192.168.2.100 로 포워딩 |
| Role 의 secrets | **포함** — 매니페스트 관리에서 Secret 생성·조회·apply 가능. ⚠ 화면에 Secret 내용(base64)이 보이므로 NFS 매니페스트 폴더에 실제 비밀번호 Secret 을 두지 않는 원칙 유지 (`mentor-platform-db` 는 kubectl 로 별도 생성) |
| 로컬 테스트 | 배포용 수정 후에도 그대로 가능 — 화면은 포트 5500 이면 `localhost:8000`, 서버는 파드가 아니면 `dev-manifests`, 환경변수 기본값은 로컬용 유지 (클러스터에서만 ConfigMap·Secret 이 덮어씀). 모델은 로컬 모델 서버(8088) 사용 |

### 8-5. ⚠ 접근 제어 — VPN·내부망에서만 접속 (2026-11-22 결정 방향)
플랫폼에는 **로그인이 없다** (리소스 삭제·재시작, 임의 YAML apply, **Secret 조회·생성**, 피드백 조작 가능).
공유기가 공인 IP 80/443 → Ingress(192.168.2.100) 로 넘기므로, DNS 를 안 만들어도 **Host 헤더만 넣으면 누구나 도달 가능** → 막는 지점은 Ingress.

| 항목 | 내용 |
|---|---|
| 방법 | 기존 argocd·maily/admin·grafana·prometheus 와 **같은 설정**: `nginx.ingress.kubernetes.io/whitelist-source-range: "10.0.0.0/24,192.168.2.0/24"` (VPN 대역 + 내부망) → 그 외는 Ingress 가 **403** |
| 전제 (확인함) | ingress-nginx `externalTrafficPolicy: Local` + 공유기가 접속자 IP 를 바꾸지 않음 (로그에 외부 공인 IP 그대로) → IP 제한이 정확히 동작 |
| 현실 위험 (확인함) | Ingress 로그에 외부 스캐너 접속이 상시 존재 (한 출처 277건) — 무인증 공개는 실제로 발견됨 |
| 확인 필요 | ① `10.0.0.0/24` 가 VPN 대역인지 ② VPN 사용자가 `mlops.studylink.click`(공인 IP) 으로 접속할 때 트래픽이 VPN 을 거치는지 — **VPN 켜고 argocd 접속이 되면 같은 구조로 OK**. split tunnel 이라 안 되면 VPN 사용자 DNS/hosts 를 192.168.2.100 으로 |
| HTTPS | cert-manager(설치됨) — argocd Ingress 의 ClusterIssuer·tls 설정을 그대로 따름 (HTTP-01 이면 인증서 발급용 경로가 IP 제한에 막히지 않는지 argocd 와 같은 방식으로 확인) |
| (선택) 추가 방어 | 기본 인증(`auth-type: basic`)을 함께 걸 수도 있음 — VPN + IP 제한이면 필수는 아님 |
| 무인증 경로 | `/healthz`, `/readyz` 는 kubelet 이 파드에 직접 호출 → Ingress 제한과 무관 |

### 8-6. 코드 수정 결과 (2026-11-22) — 로컬 동작 유지
| 항목 | 상태 | 파일 |
|---|---|---|
| 4장 #1 · 8-2 K 화면 API 주소 | ✅ 포트 5500 이면 `http://<호스트>:8000`, 그 외 `""` | `front/config.js` |
| 4장 #4 · 8-2 G `MANIFEST_DIR` | ✅ 환경변수 → 파드 안(ServiceAccount 파일 있음)이면 `/manifests` → 로컬이면 `dev-manifests` | `server/app/config.py` (+ `tests/test_config.py` 3개) |
| 8-2 I 버전 고정 | ✅ fastapi 0.165.0 · uvicorn 0.54.0 · kubernetes 37.0.3 · httpx 0.28.1 · pydantic 2.14.4 · PyMySQL 1.2.4 · PyYAML 6.0.4 | `server/requirements.txt` |
| 4장 #2·#3 · 8-2 H 서버 이미지 | ✅ python:3.14-slim, uid/gid 1000, worker 1 | `server/Dockerfile` |
| 4장 #5 · 8-2 L 화면 이미지 | ✅ nginx-unprivileged:1.27-alpine (uid 101, 포트 8080), `Cache-Control: no-cache`, probe `/web-healthz` | `front/Dockerfile`, `front/nginx.conf` |
| 8-2 J | ✅ | `server/.dockerignore`, `front/.dockerignore` |

- 이미지 태그 (로컬 빌드만, **push 안 함**): `suhannugul/mentor:platform-api_v1` (283MB), `suhannugul/mentor:platform-web_v1` (48.5MB) — 모델(`model_v1`)과 같은 저장소
- 화면 파드 포트가 **8080** 이므로 Service `targetPort: 8080`, probe `/web-healthz`
- 남은 것 (매니페스트 단계): 4장 #6 probe 연결, #7 보호 라벨, 8-2 A~F, 8-5 IP 제한·HTTPS

