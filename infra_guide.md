# infra_guide.md — 플랫폼 배포용 쿠버네티스 매니페스트 작성 가이드

> **대상**: **최상위 인프라 관리 디렉터리**에서 Claude Code 를 실행해
> MLOps 플랫폼(플랫폼 API · 화면)을 `mentor` 네임스페이스에 배포하는 YAML 을 작성할 때 따르는 규칙과 템플릿.
> 최상위 관리 디렉터리는 플랫폼·NFS 와 **무관한 별도 공간**이며, 플랫폼이 관리할 수 없는 최상위 권한·기준(정답)이다.
> **근거 문서**: 레포 `k8sapimd/deploy.md` (1~8장). 이 가이드는 그 결정 사항을 "그대로 작성 가능한 YAML" 로 옮긴 것.
> **작성 기준일**: 2026-11-22 (아래 클러스터 사실은 이 날짜에 확인한 값)

---

## 0. 먼저 알아야 할 것 (가장 중요)

1. **두 공간은 완전히 별개다.**

   | | 최상위 관리 디렉터리 (Claude Code 실행 위치, `./`) | NFS 매니페스트 폴더 (`192.168.2.40:/home/suhan/manifests`, 파드 안 `/manifests`) |
   |---|---|---|
   | 담는 것 | **플랫폼 자신**의 배포·권한(Role)·PV·Secret 등 플랫폼보다 위의 것 (`./mentor-manifests/`), 모든 서비스의 Secret (`./secret/`) | 플랫폼이 관리하는 **프로젝트 워크로드 YAML** (9장) |
   | 관리 주체 | 사람 + 이 가이드로 실행한 Claude Code, `kubectl` | 플랫폼의 매니페스트 관리 화면 (판정·적용) |
   | 권한 | **최상위 — 기준(정답)**. 플랫폼은 이곳을 보거나 바꿀 수 없음 | 하위 — 플랫폼 도구의 관리 대상 |

2. **이 가이드로 만드는 YAML 은 플랫폼 화면으로 판정·적용하지 않는다.** 항상 최상위 디렉터리에서 `kubectl apply`.
   플랫폼이 자기 자신·자기 권한을 관리하는 순환을 만들지 않는다.
3. **기준은 최상위 디렉터리다.** 클러스터 실제 상태가 다르게 바뀌면(사람·플랫폼 어느 쪽이든) **수동으로라도 최상위 디렉터리 내용에 맞춘다.**
4. **Secret 은 `./secret/` 에 YAML 로 만들되, 값은 공란(`""`)으로 둔다.** 실제 값은 사람이 채운다 — Claude 는 비밀번호를 채우거나 추측하지 않는다 (4-4). Secret 은 NFS 에 두지 않는다.
5. 기존 리소스 `mentor/toxicity-filter`(모델), `mentor/mlflow`, PVC `mlflow-data` 는 **플랫폼이 관리하는 워크로드**다.
   - 플랫폼이 관리하는 YAML 은 **NFS 매니페스트 폴더**에 둔다 (9장). 최상위 `./mentor-manifests/` 에도 **있을 수도, 없을 수도** 있다 — 있으면 최상위가 기준(0장 3)
   - 워크로드에는 **보호 라벨을 붙이지 않는다** → 인프라 관리 화면에서 삭제·재시작·셀프힐링 시연, 매니페스트 관리 화면에서 판정·적용이 가능해야 함
   - 단, `toxicity-filter` 는 댓글 기능의 `MODEL_URL` 대상 → 바꾸면 댓글 분류에 바로 영향 (교체 시 같은 Service 이름·포트 8088 유지)

---

## 1. 고정 값 (클러스터에서 확인한 사실)

| 항목 | 값 |
|---|---|
| 네임스페이스 | `mentor` (모든 리소스에 `metadata.namespace: mentor` 명시) |
| 플랫폼 API 이미지 | `suhannugul/mentor:platform-api_v1` (digest `sha256:bafb58c2…`) — 포트 **8000**, 실행 uid/gid **1000** |
| 화면 이미지 | `suhannugul/mentor:platform-web_v1` (digest `sha256:e247ed43…`) — nginx 비루트, 포트 **8080**, probe `/web-healthz` |
| 모델 (배포됨) | Service `toxicity-filter.mentor` :8088 (ClusterIP), 이미지 `suhannugul/mentor:model_v1` |
| DB (쓰기) | `mariadb-0.mariadb-headless.database.svc.cluster.local:3306`, DB `toxicity`, 계정 `mentor_app` (SELECT·INSERT·UPDATE) — ⚠ `mariadb-read` 는 복제본 포함이라 **쓰지 말 것** |
| NFS | 서버 `192.168.2.40`, 경로 `/home/suhan/manifests`, export `192.168.2.0/24(rw,sync,no_subtree_check,root_squash)`, 파일 소유자 uid 1000 |
| Ingress | `ingressClassName: nginx`, LB `192.168.2.100`, `externalTrafficPolicy: Local` (접속자 IP 보존) |
| 도메인 | `mlops.studylink.click` — Route 53 에는 **공인 IP**, 공유기가 80/443 → 192.168.2.100 포워딩 |
| HTTPS | ClusterIssuer **`letsencrypt-route53`** (DNS-01 → IP 제한과 무관하게 발급됨). 기존 argocd·grafana 와 같은 방식 |
| 접근 허용 대역 | `10.0.0.0/24` (VPN) + `192.168.2.0/24` (내부망) — 기존 argocd·grafana·prometheus 와 동일 |
| NetworkPolicy | 없음 (mentor → database:3306, mentor → toxicity-filter:8088 연결 확인됨) |

---

## 2. 파일 구성과 적용 순서

> **모든 경로는 Claude Code 를 실행한 최상위 관리 디렉터리(이하 `./`) 기준이다.** 절대경로를 쓰지 않는다.
> 이 디렉터리는 NFS·플랫폼 화면과 관계없다 (0장 1).

```
./                              ← 최상위 인프라 관리 디렉터리 (Claude Code 실행 위치)
├─ secret/                      모든 서비스의 Secret YAML — 값은 공란 (4-4). 다른 서비스 Secret 도 이미 여기 있음
└─ mentor-manifests/            이 프로젝트의 Secret 이외 YAML
   └─ platform/                 플랫폼 배포 YAML (이 가이드 4-1 ~ 4-7)

(별도 공간) NFS /home/suhan/manifests  ← 플랫폼이 관리하는 워크로드 YAML (9장)
```

`./mentor-manifests/platform/` 아래에 다음 파일로 나눠 작성한다 (번호 = 적용 순서).

| 파일 | 내용 | 적용 방법 |
|---|---|---|
| `00-rbac.yaml` | ServiceAccount, Role, RoleBinding | **kubectl** |
| `10-config.yaml` | ConfigMap `mentor-platform-config` | **kubectl** |
| `./secret/mentor-platform-db.yaml` | Secret `mentor-platform-db` — **값 공란 템플릿** | 사람이 값 입력 후 kubectl (4-4) |
| `20-storage.yaml` | PersistentVolume + PersistentVolumeClaim | **kubectl** (PV 때문) |
| `30-api.yaml` | 플랫폼 API Deployment + Service | **kubectl** |
| `40-web.yaml` | 화면 Deployment + Service | **kubectl** |
| `50-ingress.yaml` | Ingress (같은 주소 · 크기 제한 · IP 제한 · HTTPS) | **kubectl** |

- 한 파일에 여러 리소스는 `---` 로 구분 (Work 3 지원)
- `./secret/` 에 이미 있는 다른 서비스의 Secret 파일은 **읽기만** 하고 수정·덮어쓰기 하지 않는다 (같은 이름 파일이 있으면 멈추고 사람에게 확인)

---

## 3. 공통 작성 규칙

| 규칙 | 내용 |
|---|---|
| 라벨 | 모든 리소스: `app.kubernetes.io/part-of: mlops-platform`, `app.kubernetes.io/name: <mentor-api \| mentor-web …>` |
| **보호 라벨** | 플랫폼 자신의 리소스(Deployment·파드 템플릿·Service·PVC·ConfigMap·Ingress·ServiceAccount)에 `platform.mentor/protected: "true"` → 인프라 관리 화면의 [삭제]가 403 으로 거부 (자기 삭제 사고 방지). **파드 템플릿 라벨에도** 넣을 것 (파드 삭제 보호) |
| 이미지 | 태그 고정 (`latest` 금지), `imagePullPolicy: IfNotPresent` |
| 보안 | `runAsNonRoot: true`, `allowPrivilegeEscalation: false`, `capabilities.drop: [ALL]` |
| 권한 토큰 | 화면 파드는 쿠버네티스 권한 불필요 → `automountServiceAccountToken: false`. API 파드만 ServiceAccount 사용 |
| 레플리카 | API **1개** (Watch 캐시가 프로세스 메모리), 화면 1~2개 |
| 리소스 | requests·limits 명시 (아래 값은 시작점) |
| 비밀값 | 비밀번호·토큰·키는 `secret/` 폴더의 Secret YAML 에만, **값은 공란으로 생성**. Deployment 등에는 `secretRef` 이름 참조만 |

---

## 4. 리소스별 템플릿

### 4-1. `00-rbac.yaml` — ServiceAccount · Role · RoleBinding

⚠ **Watch 대상 7종(pods, deployments, statefulsets, services, jobs, persistentvolumeclaims, events)의 `list`·`watch` 가 하나라도 빠지면 `/readyz` 가 계속 503 → API 파드가 Ready 가 되지 않는다.**

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: mentor-platform
  namespace: mentor
  labels: {app.kubernetes.io/part-of: mlops-platform, platform.mentor/protected: "true"}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: mentor-platform
  namespace: mentor
  labels: {app.kubernetes.io/part-of: mlops-platform}
rules:
  # Work 1 조회·Watch + Work 2 삭제 + Work 3 apply(SSA=patch)·판정·삭제
  - apiGroups: [""]
    resources: [pods, services, persistentvolumeclaims, configmaps, secrets, serviceaccounts]
    verbs: [get, list, watch, create, patch, delete]
  - apiGroups: [""]
    resources: [events]                 # 조회 전용 (Watch 대상 — 빠지면 readyz 실패)
    verbs: [get, list, watch]
  - apiGroups: [""]
    resources: [pods/log]               # Work 2 파드 로그
    verbs: [get]
  - apiGroups: [apps]
    resources: [deployments, statefulsets]
    verbs: [get, list, watch, create, patch, delete]   # patch = Work 2 재시작 + Work 3 apply
  - apiGroups: [batch]
    resources: [jobs, cronjobs]
    verbs: [get, list, watch, create, patch, delete]
  - apiGroups: [networking.k8s.io]
    resources: [ingresses]
    verbs: [get, list, watch, create, patch, delete]
  # rbac.authorization.k8s.io 는 넣지 않는다 — 화면에서 자기 권한을 늘리는 것 방지
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: mentor-platform
  namespace: mentor
  labels: {app.kubernetes.io/part-of: mlops-platform}
subjects:
  - {kind: ServiceAccount, name: mentor-platform, namespace: mentor}
roleRef:
  {apiGroup: rbac.authorization.k8s.io, kind: Role, name: mentor-platform}
```
- `secrets` 포함 (결정 사항 deploy.md 8-4) → 화면에서 Secret 조회·생성 가능. 그래서 IP 제한(4-7)이 필수
- 점검: `kubectl auth can-i --list -n mentor --as=system:serviceaccount:mentor:mentor-platform`

### 4-2. `10-config.yaml` — ConfigMap

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: mentor-platform-config
  namespace: mentor
  labels: {app.kubernetes.io/part-of: mlops-platform, platform.mentor/protected: "true"}
data:
  LOG_LEVEL: "INFO"
  WATCH_TIMEOUT_SECONDS: "300"
  MODEL_URL: "http://toxicity-filter.mentor.svc.cluster.local:8088"   # IP 금지 (재생성 시 바뀜)
  THRESHOLD: "0.5"
  MODEL_TIMEOUT: "3"
  DB_HOST: "mariadb-0.mariadb-headless.database.svc.cluster.local"   # 쓰기 전용 주소
  DB_PORT: "3306"                                                     # NodePort(31306) 아님
  DB_NAME: "toxicity"
```
- 값은 모두 문자열(따옴표)
- 넣지 않는 것: `WATCH_NAMESPACE`(파드 안에서 자동), `CORS_ORIGINS`(같은 주소 구성이라 불필요), `MANIFEST_DIR`(Deployment 에 직접 — 4-5)
- ⚠ **바꿔도 실행 중인 파드에 반영되지 않음** → 바꾼 뒤 API Deployment 롤아웃 재시작

### 4-3. `20-storage.yaml` — NFS PersistentVolume · PersistentVolumeClaim

```yaml
apiVersion: v1
kind: PersistentVolume
metadata:
  name: mentor-manifests-pv
  labels: {app.kubernetes.io/part-of: mlops-platform}
spec:
  capacity: {storage: 1Gi}
  accessModes: [ReadWriteMany]
  persistentVolumeReclaimPolicy: Retain        # PV·PVC 를 지워도 NFS 파일은 남음
  storageClassName: ""
  mountOptions: [actimeo=1]                    # NFS 외부 수정이 1초 안에 보이도록 (캐시 축소)
  claimRef: {namespace: mentor, name: mentor-manifests}   # 이 PVC 에만 묶임
  nfs:
    server: 192.168.2.40
    path: /home/suhan/manifests
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: mentor-manifests
  namespace: mentor
  labels: {app.kubernetes.io/part-of: mlops-platform, platform.mentor/protected: "true"}
spec:
  accessModes: [ReadWriteMany]
  storageClassName: ""
  volumeName: mentor-manifests-pv
  resources: {requests: {storage: 1Gi}}
```
- PV 는 클러스터 범위 → **kubectl 로만** 적용
- 사전 검증(Work 3)에서 같은 설정으로 마운트·쓰기(uid 1000) 성공 확인됨

### 4-4. `secret/mentor-platform-db.yaml` — Secret (**값은 공란으로 생성**)

모든 서비스의 Secret 은 **실행 경로의 `secret/` 폴더(`./secret/`) 아래 YAML 로 정리**하는 것이 이 환경의 규칙이다 (다른 서비스들도 같은 방식).
Claude 는 **구조·키 이름만 작성하고 실제 값은 반드시 빈 문자열 `""` 로 둔다.** 값은 사람이 직접 채운다.

```yaml
# ./secret/mentor-platform-db.yaml
# ⚠ 값("")은 사람이 직접 입력한다. Claude 는 값을 채우거나 추측하지 않는다.
apiVersion: v1
kind: Secret
metadata:
  name: mentor-platform-db
  namespace: mentor
  labels: {app.kubernetes.io/part-of: mlops-platform, platform.mentor/protected: "true"}
type: Opaque
stringData:                      # 평문으로 적으면 쿠버네티스가 base64 로 저장 (직접 인코딩할 때의 줄바꿈 실수 방지)
  DB_USER: ""                    # ← 앱 계정 이름 (mentor_app) 입력
  DB_PASSWORD: ""                # ← mentor_app 비밀번호 입력
```

| 규칙 | 내용 |
|---|---|
| 위치 | `./secret/<Secret 이름>.yaml` — Secret 만. 이 프로젝트의 그 외 YAML 은 `./mentor-manifests/`. NFS 에는 Secret 을 두지 않는다. 폴더가 없으면 만들지 말고 사람에게 확인 (경로를 잘못 잡았을 가능성) |
| 값 | Claude 는 **항상 `""`** + 무엇을 넣을지 주석. 대화·문서에 나온 비밀번호가 있어도 **넣지 않는다** |
| 형식 | `stringData` 사용 (`data` + base64 는 사람이 인코딩하다 줄바꿈이 섞이는 실수가 잦음) |
| 키 이름 | 앱이 읽는 **환경변수 이름과 정확히 같게** (`envFrom.secretRef` 로 그대로 주입) — 플랫폼 API: `DB_USER`, `DB_PASSWORD` |
| 적용 | 사람이 값을 채운 뒤 (실행 경로에서) `kubectl apply -f secret/mentor-platform-db.yaml` — 플랫폼 화면으로는 다루지 않음 (최상위에서만) |
| 빈 값 확인 | 적용 전 `grep -n '""' secret/*.yaml` 로 안 채운 값이 없는지 확인 — 빈 값으로 적용하면 파드는 뜨지만 **DB 로그인 실패** (댓글 "DB 저장 안 됨") |
| 노출 | 이 폴더도 매니페스트 관리 화면에 보이고 NFS 에 남는다 → 접근은 Ingress IP 제한(VPN·내부망)으로 보호. **git 레포에는 올리지 않는다** |

- 확인은 키 이름만 (값 출력 금지): `kubectl -n mentor get secret mentor-platform-db -o jsonpath='{.data}' | grep -o '"[A-Z_]*"'`

### 4-5. `30-api.yaml` — 플랫폼 API Deployment · Service

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: mentor-api
  namespace: mentor
  labels: {app.kubernetes.io/name: mentor-api, app.kubernetes.io/part-of: mlops-platform, platform.mentor/protected: "true"}
spec:
  replicas: 1                                   # Watch 캐시가 프로세스 메모리 → 1개
  selector:
    matchLabels: {app.kubernetes.io/name: mentor-api}
  strategy:
    type: RollingUpdate
    rollingUpdate: {maxSurge: 1, maxUnavailable: 0}
  template:
    metadata:
      labels: {app.kubernetes.io/name: mentor-api, app.kubernetes.io/part-of: mlops-platform, platform.mentor/protected: "true"}
    spec:
      serviceAccountName: mentor-platform       # 쿠버네티스 접속 = 이 SA 의 Role 권한
      securityContext:
        runAsUser: 1000                         # NFS 파일 소유자 suhan (root_squash)
        runAsGroup: 1000
        runAsNonRoot: true
      containers:
        - name: api
          image: suhannugul/mentor:platform-api_v1
          imagePullPolicy: IfNotPresent
          ports: [{name: http, containerPort: 8000}]
          envFrom:
            - configMapRef: {name: mentor-platform-config}
            - secretRef: {name: mentor-platform-db}      # 없으면 CreateContainerConfigError (빠뜨림을 바로 알 수 있게 optional 아님)
          env:
            - {name: MANIFEST_DIR, value: /manifests}    # volumeMounts.mountPath 와 반드시 같은 값
          volumeMounts:
            - {name: manifests, mountPath: /manifests}
          startupProbe:                                  # 첫 기동 보호 (최대 60초)
            httpGet: {path: /healthz, port: http}
            periodSeconds: 2
            failureThreshold: 30
          readinessProbe:                                # Watch 7종 첫 동기화 완료 시 200
            httpGet: {path: /readyz, port: http}
            periodSeconds: 5
            timeoutSeconds: 2
            failureThreshold: 3
          livenessProbe:                                 # 프로세스 생존만 (DB·모델 장애를 넣지 않음 — 가이드 17장)
            httpGet: {path: /healthz, port: http}
            periodSeconds: 10
            timeoutSeconds: 2
            failureThreshold: 3
          resources:
            requests: {cpu: 100m, memory: 256Mi}
            limits:   {cpu: 500m, memory: 512Mi}
          securityContext:
            allowPrivilegeEscalation: false
            capabilities: {drop: [ALL]}
      volumes:
        - name: manifests
          persistentVolumeClaim: {claimName: mentor-manifests}
---
apiVersion: v1
kind: Service
metadata:
  name: mentor-api
  namespace: mentor
  labels: {app.kubernetes.io/name: mentor-api, app.kubernetes.io/part-of: mlops-platform, platform.mentor/protected: "true"}
spec:
  type: ClusterIP                                # 외부 노출은 Ingress 로만
  selector: {app.kubernetes.io/name: mentor-api}
  ports: [{name: http, port: 8000, targetPort: http}]
```
- probe 경로는 kubelet 이 파드에 직접 호출 → Ingress IP 제한과 무관
- `readOnlyRootFilesystem` 은 쓰지 않음 (매니페스트 저장은 `/manifests` 에 하지만, 라이브러리 임시 파일 대비)

### 4-6. `40-web.yaml` — 화면 Deployment · Service

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: mentor-web
  namespace: mentor
  labels: {app.kubernetes.io/name: mentor-web, app.kubernetes.io/part-of: mlops-platform, platform.mentor/protected: "true"}
spec:
  replicas: 1
  selector:
    matchLabels: {app.kubernetes.io/name: mentor-web}
  template:
    metadata:
      labels: {app.kubernetes.io/name: mentor-web, app.kubernetes.io/part-of: mlops-platform, platform.mentor/protected: "true"}
    spec:
      automountServiceAccountToken: false       # 쿠버네티스 권한 불필요
      securityContext: {runAsNonRoot: true}
      containers:
        - name: web
          image: suhannugul/mentor:platform-web_v1
          imagePullPolicy: IfNotPresent
          ports: [{name: http, containerPort: 8080}]     # 비루트 nginx → 8080
          readinessProbe:
            httpGet: {path: /web-healthz, port: http}
            periodSeconds: 5
          livenessProbe:
            httpGet: {path: /web-healthz, port: http}
            periodSeconds: 10
          resources:
            requests: {cpu: 20m, memory: 32Mi}
            limits:   {cpu: 200m, memory: 128Mi}
          securityContext:
            allowPrivilegeEscalation: false
            capabilities: {drop: [ALL]}
---
apiVersion: v1
kind: Service
metadata:
  name: mentor-web
  namespace: mentor
  labels: {app.kubernetes.io/name: mentor-web, app.kubernetes.io/part-of: mlops-platform, platform.mentor/protected: "true"}
spec:
  type: ClusterIP
  selector: {app.kubernetes.io/name: mentor-web}
  ports: [{name: http, port: 80, targetPort: http}]       # 80 → 컨테이너 8080
```
- 화면 이미지의 `config.js` 는 포트가 5500 이 아니면 API 주소를 `""`(같은 주소)로 씀 → Ingress 로 같은 호스트에 묶여야 동작
- nginx 가 `Cache-Control: no-cache` 를 붙임 → 새 버전 배포 후 예전 JS 재사용 문제 없음

### 4-7. `50-ingress.yaml` — 같은 주소 · 크기 제한 · /openapi.json · IP 제한 · HTTPS

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: mentor-platform
  namespace: mentor
  labels: {app.kubernetes.io/part-of: mlops-platform, platform.mentor/protected: "true"}
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt-route53            # DNS-01 → IP 제한과 무관하게 발급
    nginx.ingress.kubernetes.io/ssl-redirect: "true"               # http → https
    nginx.ingress.kubernetes.io/whitelist-source-range: "10.0.0.0/24,192.168.2.0/24"   # VPN + 내부망만, 그 외 403
    nginx.ingress.kubernetes.io/proxy-body-size: "2m"              # 매니페스트 저장(최대 1MB+JSON) 413 방지
    nginx.ingress.kubernetes.io/proxy-read-timeout: "60"           # 판정·apply(dry-run 여러 번) 여유
spec:
  ingressClassName: nginx
  tls:
    - hosts: [mlops.studylink.click]
      secretName: mlops-studylink-tls                              # cert-manager 가 생성
  rules:
    - host: mlops.studylink.click                                  # host 기반 (경로 기반 /mentor/… 금지)
      http:
        paths:
          - {path: /api,          pathType: Prefix, backend: {service: {name: mentor-api, port: {name: http}}}}
          - {path: /docs,         pathType: Prefix, backend: {service: {name: mentor-api, port: {name: http}}}}
          - {path: /openapi.json, pathType: ImplementationSpecific, backend: {service: {name: mentor-api, port: {name: http}}}}  # /docs 가 불러옴. 점(.) 포함 경로는 Exact·Prefix 불가 (ingress-nginx 경로 검사)
          - {path: /healthz,      pathType: Exact,  backend: {service: {name: mentor-api, port: {name: http}}}}
          - {path: /readyz,       pathType: Exact,  backend: {service: {name: mentor-api, port: {name: http}}}}
          - {path: /,             pathType: Prefix, backend: {service: {name: mentor-web, port: {name: http}}}}
```
- **같은 주소**: 화면과 API 가 같은 호스트 → 브라우저 CORS 없음, 화면 API 주소 하드코딩 없음
- **IP 제한이 핵심 방어선**: 플랫폼에는 로그인이 없고 Secret 권한까지 있음. 공유기가 80/443 을 그대로 넘기므로 DNS 를 안 만들어도 Host 헤더로 도달 가능 → 이 annotation 을 절대 빼지 말 것
- 더 긴 경로가 우선 매칭되므로 `/` 는 마지막에 두어도 나머지를 가로채지 않음

---

## 5. 적용 명령 (처음 한 번)

```bash
# 모두 실행 경로(./)에서
kubectl apply -f mentor-manifests/platform/00-rbac.yaml
kubectl apply -f mentor-manifests/platform/10-config.yaml
grep -n '""' secret/mentor-platform-db.yaml      # 빈 값 남았는지 확인 (값을 먼저 채울 것 — 출력이 없어야 함)
kubectl apply -f secret/mentor-platform-db.yaml
kubectl apply -f mentor-manifests/platform/20-storage.yaml && kubectl -n mentor get pvc mentor-manifests      # Bound 확인
kubectl apply -f mentor-manifests/platform/30-api.yaml && kubectl -n mentor rollout status deploy/mentor-api
kubectl apply -f mentor-manifests/platform/40-web.yaml && kubectl -n mentor rollout status deploy/mentor-web
kubectl apply -f mentor-manifests/platform/50-ingress.yaml
kubectl -n mentor get certificate mlops-studylink-tls -w                             # READY True 까지 (DNS-01, 1~2분)
```
- 순서 이유: Role 없이 API 가 뜨면 readyz 실패 / ConfigMap·Secret 없으면 컨테이너 생성 실패 / PVC 가 Bound 여야 파드 시작
- Route 53: `mlops.studylink.click` → **공인 IP** (기존 argocd 와 같은 방식). VPN 사용자는 argocd 와 같은 경로로 접속됨

---

## 6. 배포 후 확인 체크리스트

```bash
kubectl auth can-i --list -n mentor --as=system:serviceaccount:mentor:mentor-platform   # 4-1 권한
kubectl -n mentor get pods -l app.kubernetes.io/part-of=mlops-platform                  # 1/1 Running
kubectl -n mentor exec deploy/mentor-api -- env | grep -E '^(MODEL_URL|DB_HOST|DB_PORT|DB_NAME|MANIFEST_DIR|THRESHOLD)='   # 비밀번호 줄은 출력하지 않음
kubectl -n mentor exec deploy/mentor-api -- id          # uid=1000
kubectl -n mentor logs deploy/mentor-api | grep -E '피드백 DB|list 완료|ERROR|403'
```
- [ ] VPN 켜고 `https://mlops.studylink.click` → 화면 표시, 인증서 정상
- [ ] **VPN 끄고(외부망) 접속 → 403** (IP 제한 동작)
- [ ] 인프라 관리: `mentor` 리소스 표시, 플랫폼 자신의 리소스 [삭제] → 403 (보호 라벨)
- [ ] 댓글 기능: 분류 → "DB에 저장됨" (DB_HOST 가 쓰기 주소)
- [ ] 매니페스트 관리: NFS 매니페스트 폴더(`/manifests`)의 파일이 목록에 보임, 화면에서 새 파일 저장 시 NFS 에 소유자 1000 으로 생성, NFS 에서 직접 수정 → 몇 초 안에 반영
- [ ] `https://mlops.studylink.click/docs` Swagger 표시 (/openapi.json 경로)
- [ ] 브라우저 개발자 도구에 CORS 오류 없음

---

## 7. 문제가 생겼을 때

| 증상 | 확인 | 원인·해결 |
|---|---|---|
| API 파드 0/1 Ready 에서 멈춤 | `kubectl -n mentor exec deploy/mentor-api -- python -c "import urllib.request;print(urllib.request.urlopen('http://localhost:8000/api/v1/kinds').read()[:500])"` | 어떤 kind 의 `last_error` 에 403 → Role 에 그 리소스의 list/watch 추가 (4-1) |
| `CreateContainerConfigError` | `kubectl -n mentor describe pod -l app.kubernetes.io/name=mentor-api` | ConfigMap·Secret 이름 오타 또는 미생성 |
| 파드 `Pending` | `kubectl -n mentor get pvc mentor-manifests` | PVC 가 Bound 아님 → PV 의 `claimRef`·`volumeName` 확인 |
| 매니페스트 저장 실패 (권한) | `kubectl -n mentor exec deploy/mentor-api -- ls -ln /manifests` | `runAsUser` 가 1000 이 아님 |
| 댓글 "DB 저장 안 됨" | 화면 메시지 / API 로그 | 로그인 실패 → Secret 값이 비었거나 틀림 (`secret/` YAML 확인 후 적용 → API 롤아웃 재시작), 연결 실패 → `DB_HOST`(쓰기 주소)·`DB_PORT`(3306) |
| 413 Request Entity Too Large | — | Ingress `proxy-body-size` 누락 |
| `/docs` 빈 화면 | — | Ingress 에 `/openapi.json` 경로 누락, 또는 `pathType` 을 Exact 로 씀 (점 포함 경로는 ImplementationSpecific) |
| 화면은 뜨는데 API 호출 실패 | 개발자 도구 Network | Ingress 경로(`/api`) 누락 또는 host 기반이 아님 |
| 외부에서도 접속됨 | — | `whitelist-source-range` 누락 — **즉시 수정** |
| 인증서 발급 안 됨 | `kubectl -n mentor describe certificate mlops-studylink-tls` | ClusterIssuer 이름·Route 53 권한 (기존 argocd 인증서와 비교) |

되돌리기: `kubectl -n mentor rollout undo deploy/<이름>` / 전체 제거는 50 → 00 역순 `kubectl delete -f` (PV 는 `Retain` 이라 NFS 파일 보존, Secret 은 실행 경로에서 `kubectl delete -f secret/mentor-platform-db.yaml`)

---

## 8. 금지 사항 요약

- ❌ Claude 가 Secret 값을 채우거나 추측하기 (항상 `""`) / `secret/` 폴더를 git 레포에 올리기
- ❌ Ingress 에서 `whitelist-source-range` 빼기 / `latest` 태그 / 경로 기반 라우팅
- ❌ `DB_HOST` 에 `mariadb-read` 또는 NodePort(31306) 쓰기
- ❌ Role 에 `rbac.authorization.k8s.io`·`*` 권한 넣기, ClusterRole 사용
- ❌ API replicas 2 이상
- ❌ 워크로드(`toxicity-filter`, `mlflow`, `mlflow-data`)에 보호 라벨 붙이기 — 플랫폼이 삭제·재시작·판정할 수 있어야 함 (9장)
- ❌ 이 가이드로 만든 플랫폼 YAML 을 NFS 에 복사하거나 플랫폼 화면으로 판정·적용하기 — 최상위에서 kubectl 로만

---

## 9. (참고) 기존 워크로드를 플랫폼으로 관리하기 — NFS 쪽 작업, 플랫폼 배포 후

플랫폼의 목적 = 워크로드(모델 서빙 등)를 **선언(YAML) 기준으로 관리**하는 것. 지금 `toxicity-filter`·`mlflow` 는 kubectl 로 만들어져 NFS 매니페스트 폴더에 선언이 없다.
플랫폼이 관리하려면 YAML 이 **NFS 매니페스트 폴더**에 있어야 한다 (플랫폼 화면은 NFS 만 읽음). 같은 워크로드 YAML 이 최상위에도 있을 수 있으며, 그 경우 **최상위가 기준**이다 (0장 3).
→ 아래 순서로 **선언 관리 대상으로 편입**한다.

| 단계 | 할 일 |
|---|---|
| 1. 폴더 | NFS `/home/suhan/manifests/workloads/` (화면에서는 `workloads/…`) — 예: `workloads/toxicity-filter.yaml`, `workloads/mlflow.yaml`. 화면 [+ 새 파일] 로 만들거나 NFS 서버에서 직접 작성 |
| 2. 현재 상태를 YAML 로 | `kubectl -n mentor get deploy,svc toxicity-filter -o yaml` 을 바탕으로 **사람이 쓴 선언만** 남긴다: `status`, `metadata.uid / resourceVersion / generation / creationTimestamp / managedFields`, `kubectl.kubernetes.io/last-applied-configuration`, Service 의 `clusterIP(s)` 등 클러스터가 채운 값은 지운다 |
| 3. 판정 확인 | 매니페스트 관리 화면에서 파일을 열면 **일치** 가 나와야 정상. 불일치면 diff 를 보고 2단계에서 빠진/남은 필드 정리 |
| 4. 첫 적용 | 기존 kubectl 소유 필드와 **소유권 충돌**이 나올 수 있음 → 확인창에서 충돌 필드 확인 후 [강제 적용] → 이후 이 파일이 유일한 선언 |
| 5. 이후 운영 | 이미지 교체(예: `model_v1` → `model_v2`)·replicas·환경변수 변경은 **YAML 수정 → 저장 → 적용**. kubectl 로 바꾸면 판정 화면에 "불일치"로 드러남 (base.md 의 선언형 관리 시연) |

- `mlflow-data` PVC·PV(`mlflow-data-pv`, hostPath)는 데이터가 있으므로 YAML 편입은 하되 **삭제는 신중히** (PVC 삭제 = 데이터 손실 가능). PV 는 클러스터 범위라 화면 적용 불가 → 최상위 디렉터리에서 kubectl
- 워크로드 Secret 이 필요하면 **최상위 `./secret/<이름>.yaml`** 에 4-4 규칙(값 공란)으로 만들어 kubectl 로 적용하고, NFS 의 워크로드 YAML 에는 이름 참조만 (Secret 을 NFS 에 두지 않음)
- 모델 교체 시 `MODEL_URL`(Service `toxicity-filter:8088`)은 그대로 두고 Deployment 이미지만 바꾸면 댓글 기능은 무중단 롤링 교체

