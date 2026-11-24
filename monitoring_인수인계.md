# monitoring_인수인계.md — 모니터링(Prometheus · Grafana) 클러스터 반영

> **대상**: ① 최상위 인프라 관리 디렉터리(`./`)에서 실행하는 Claude Code (= "인프라 AI", 2·3·5·7·8장) ② 사람 (4·6장, 플랫폼 화면 · Grafana 관리자 화면)
> **선행 문서**: `infra_guide.md` (0~3장 규칙, 4-4 Secret 규칙, 9장 워크로드 관리), `train_인수인계.md` (적용 완료 상태여야 함). 충돌하면 `infra_guide.md` 우선.
> **근거**: 레포 `k8sapimd/monitoring.md` (설계 0~8장)
> **작성일**: 2026-11-24 (아래 클러스터 사실은 이 날짜에 읽기 전용으로 확인)
> ✅ **레포 코드 구현 · 이미지 push 완료 (2026-11-24).** 남은 것은 2장 이후 적용.

---

## 0. 한 줄 요약

모델 파드와 플랫폼 API가 `/metrics`를 내고 → 공용 Prometheus에 **`mentor` 수집 규칙**을 추가해 15초마다 수집 → Grafana에 대시보드 2개(모델 / 플랫폼)를 JSON으로 import하고 **공개 대시보드**로 설정 →
**새 도메인 `grafanas.studylink.click`에는 공개 대시보드 경로만 여는 Ingress**를 만들어 → 플랫폼 화면 [모니터링] 메뉴가 그 공개 URL을 iframe으로 띄운다. **VPN 없이 시연**할 수 있게 한다.

**순서**
```
2 Prometheus 수집 규칙 ─┐
3 플랫폼 API·화면 이미지 ─┼─▶ 4 모델 워크로드(화면) ─▶ 5 공개 Ingress ─▶ 6 Grafana 설정·토큰(사람) ─▶ 7 토큰을 ConfigMap에 ─▶ 8 시연용 접근 설정 ─▶ 9 검증
```
- 수집 대상이 없어도 2장의 규칙은 무해하다 (대상 0개). 3·4장 이후 자동으로 대상이 잡힌다.
- **6장의 공개 토큰이 나와야 7장(화면 연결)을 할 수 있다.** 토큰이 없는 동안 화면의 [모니터링] 메뉴는 "대시보드 준비 중"을 보여 준다.

---

## 1. 지금 상태

### 1-1. 레포 (구현 후 채움)
| 항목 | 상태 |
|---|---|
| 레포 구현 완료 (`serve.py` · `server/` `/metrics`, [모니터링] 화면, 대시보드 JSON) | ✅ 2026-11-24 — 테스트 server 147 · model 28 통과 (k8sapimd/monitoring.md 9장) |
| 이미지 (Docker Hub `suhannugul/mentor`) | ✅ **push 완료 (2026-11-24)**: `model_v3` (`sha256:b14b178e…`, 413MB) · `platform-api_v5` (`sha256:8d651615…`, 284MB) · `platform-web_v4` (`sha256:313e5534…`, 48.5MB). 로컬 실행 점검: 모델 `/predict`·`/metrics`, API `/metrics`·`/api/v1/monitoring`(uid 1000), 화면 [모니터링] 메뉴·`monitoring.js` |
| 대시보드 JSON | ✅ 레포 `monitoring/dashboards/mentor-model.json`, `mentor-platform.json` (`build.py` 로 생성 — JSON 직접 수정 금지) |
| ConfigMap 키 이름 | ✅ 확정: `GRAFANA_MODEL_URL`, `GRAFANA_PLATFORM_URL` (3-1) |

빌드 명령 (사람, Docker Desktop 실행 후 레포 루트에서):
```bash
docker build -t suhannugul/mentor:model_v3 model/
docker build -t suhannugul/mentor:platform-api_v5 server/
docker build -t suhannugul/mentor:platform-web_v4 front/
docker push suhannugul/mentor:model_v3 && docker push suhannugul/mentor:platform-api_v5 && docker push suhannugul/mentor:platform-web_v4
```

### 1-2. 클러스터 (확인됨)
| 항목 | 상태 |
|---|---|
| Prometheus | `monitoring/prometheus-statefulset` (`prom/prometheus:v2.45.0`), 설정 = ConfigMap `prometheus-config`, **`--web.enable-lifecycle` 없음** → 설정 변경 시 파드 재시작 필요. 데이터 = PVC `prometheus-pvc` (재시작해도 유지), 보존 7d |
| Prometheus 권한 | ClusterRole `prometheus-cluster-role`: pods·endpoints·services get/list/watch (클러스터 전체) → **변경 불필요** |
| Grafana | `monitoring/grafana-deployment` (`grafana/grafana-oss:latest`, 실제 **13.0.2**), Service `grafana-service` **80 → targetPort 3000**, `grafana.ini`: `allow_embedding = true`, anonymous Viewer. `[public_dashboards] enabled = true` (기본값, `/api/public/dashboards/…` 응답 확인). `root_url` 미설정 |
| 기존 `monitoring/grafana-ingress` | `grafana.studylink.click`, whitelist `10.0.0.0/24,192.168.2.0/24` → **변경하지 않음** |
| 새 도메인 | `grafanas.studylink.click` → 공개 DNS `<공인 IP>` ✅ (공유기 → Ingress 192.168.2.100) |
| `mentor` 파드 | `toxicity-filter` = `model_v2`, 라벨 `app=toxicity-filter`, 포트 이름 `http`(8088) / `mentor-api` = `platform-api_v4`, 라벨 `app.kubernetes.io/name=mentor-api`, 포트 이름 `http`(8000) / `mentor-web` = `platform-web_v3` |
| DB | `database/mariadb-0`(쓰기), `mariadb-1`(복제본). 개별 주소 `mariadb-1.mariadb-headless.database.svc.cluster.local:3306`. ⚠ `mariadb-read` Service는 **두 파드 모두** 가리킴 → Grafana에 쓰지 않음 |
| ⚠ **`mentor/mentor-platform` Ingress** | **화이트리스트 annotation이 없다** (`cert-manager`, `proxy-body-size`, `proxy-read-timeout`, `ssl-redirect`만 있음). 공인 IP로 `/healthz` 200 확인 → **지금 로그인 없는 플랫폼이 인터넷에 공개된 상태.** deploy.md 8-5에서 정한 whitelist가 적용되지 않았거나 빠진 것으로 보임 → **8장에서 기본 인증을 추가할 때까지 위험** |

---

## 2. 작업 A — 인프라 AI: Prometheus `mentor` 수집 규칙

> "job" = Prometheus 설정의 **수집 규칙 이름(`job_name`)**이다. 쿠버네티스 Job이 아니다. 상시 수집한다.
> 공용 리소스(`monitoring` 네임스페이스). 최상위 디렉터리에 사본을 두고 `kubectl`로 적용한다 (예: `./monitoring/prometheus-config.yaml`).

### 2-1. 백업
```bash
kubectl -n monitoring get cm prometheus-config -o yaml > ./monitoring/prometheus-config.bak-20261124.yaml
```

### 2-2. `prometheus.yml`의 `scrape_configs` **맨 끝에 추가** (기존 규칙은 한 글자도 바꾸지 않는다)
```yaml
  # 7. mentor MLOps 플랫폼 (모델 · 플랫폼 API) — k8sapimd/monitoring.md 6장
  - job_name: 'mentor'
    metrics_path: /metrics
    kubernetes_sd_configs:
      - role: pod
        namespaces:
          names: ['mentor']
    relabel_configs:
      - source_labels: [__meta_kubernetes_pod_label_platform_mentor_metrics]   # 파드 라벨 platform.mentor/metrics
        action: keep
        regex: "true"
      - source_labels: [__meta_kubernetes_pod_container_port_name]
        action: keep
        regex: http
      - source_labels: [__meta_kubernetes_pod_phase]
        action: drop
        regex: (Succeeded|Failed)
      - source_labels: [__meta_kubernetes_namespace]
        target_label: namespace
      - source_labels: [__meta_kubernetes_pod_name]
        target_label: pod
      - source_labels: [__meta_kubernetes_pod_container_name]
        target_label: container
```

### 2-3. 검사 → 적용 → 재시작
```bash
# 문법 검사 (Prometheus 이미지의 promtool 사용 — 수정한 파일을 파드에 넣지 않고 stdin으로)
kubectl -n monitoring exec -i prometheus-statefulset-0 -- promtool check config /dev/stdin < ./monitoring/prometheus.yml
kubectl apply -f ./monitoring/prometheus-config.yaml
kubectl -n monitoring delete pod prometheus-statefulset-0        # StatefulSet이 다시 띄움 (PVC 유지)
kubectl -n monitoring rollout status statefulset/prometheus-statefulset
```
- 재시작하는 수십 초 동안 **다른 프로젝트의 수집도 멈춘다** → 짧게 끝낸다.
- 확인: `kubectl -n monitoring exec prometheus-statefulset-0 -- wget -qO- 'http://localhost:9090/api/v1/targets?scrapePool=mentor'` → 3·4장 전에는 대상 0개가 정상, 이후에는 2개 `"health":"up"`
- 기존 규칙들이 그대로 UP인지도 함께 확인한다 (`/api/v1/targets` 전체에서 `"health":"down"`이 새로 생기지 않았는지)

---

## 3. 작업 B — 인프라 AI: 플랫폼 API · 화면 (`./mentor-manifests/platform/`)

### 3-1. ConfigMap `mentor-platform-config` — 키 2개 추가 (값은 7장에서 채움)
```yaml
  GRAFANA_MODEL_URL: ""       # 공개 대시보드 URL (모델)   — 7장에서 채움
  GRAFANA_PLATFORM_URL: ""    # 공개 대시보드 URL (플랫폼) — 7장에서 채움
```
- 화면의 [모니터링] 메뉴는 API에서 이 값을 받아 iframe을 띄운다. 비어 있으면 "대시보드 준비 중"을 보여 준다.
- 값이 공개 URL이므로 **Secret이 아니라 ConfigMap**에 둔다.
- 키 이름 확정 (1-1). 값이 http(s):// 로 시작하지 않으면 화면에 설정 오류로 표시된다.

### 3-2. `30-api.yaml` — 이미지 + **파드 템플릿 라벨**
```yaml
  template:
    metadata:
      labels:
        app.kubernetes.io/name: mentor-api            # 기존 그대로 (selector와 같아야 함)
        platform.mentor/protected: "true"             # 기존 그대로
        platform.mentor/metrics: "true"               # ← 추가: Prometheus mentor 규칙의 수집 대상 표시
    spec:
      containers:
        - name: api
          image: suhannugul/mentor:platform-api_v5    # ← v4 → v5
```
- **`spec.selector`는 바꾸지 않는다** (Deployment selector는 변경 불가 — 라벨은 template에만 추가).
- 포트 이름 `http`는 이미 있음 → 변경 없음.

### 3-3. `40-web.yaml` — 이미지만
```yaml
          image: suhannugul/mentor:platform-web_v4    # ← v3 → v4 ([모니터링] 메뉴 추가)
```
- 화면 파드에는 `/metrics`가 없으므로 수집 라벨을 붙이지 않는다.

```bash
kubectl apply -f mentor-manifests/platform/10-config.yaml
kubectl apply -f mentor-manifests/platform/30-api.yaml
kubectl apply -f mentor-manifests/platform/40-web.yaml
kubectl -n mentor rollout status deploy/mentor-api
kubectl -n mentor rollout status deploy/mentor-web
```

---

## 4. 작업 C — 사람: 모델 워크로드 (플랫폼 매니페스트 관리 화면, `workloads/toxicity-filter.yaml`)

모델은 플랫폼이 관리하는 워크로드다 → **화면에서 수정 → 저장 → [적용]** (kubectl 아님, infra_guide 9장).
```yaml
  template:
    metadata:
      labels:
        app: toxicity-filter                      # 기존 그대로
        platform.mentor/metrics: "true"           # ← 추가
    spec:
      containers:
        - name: app
          image: suhannugul/mentor:model_v3       # ← model_v2 → v3 (/metrics 추가, 그 외 동작 동일)
```
- **initContainer(`fetch-model`)·`MODEL_URI` 줄·`# platform.mentor/model-uri` 표식은 건드리지 않는다.**
- 적용하면 롤링 업데이트가 일어난다. 같은 MLflow 버전을 다시 받으므로 판정 결과는 바뀌지 않는다.
- 확인: [모델 교체] 탭 "선언과 일치", 2-3의 targets에서 `toxicity-filter-…` UP

---

## 5. 작업 D — 인프라 AI: 공개용 Ingress `monitoring/grafana-public`

> 목적: VPN 없이 **mentor 공개 대시보드 2개만** 보이게 한다. Grafana 전체(다른 프로젝트 대시보드, 데이터소스 임의 쿼리, 로그인)는 열지 않는다.
> 방식: **허용할 경로만 규칙으로 적는다.** 규칙이 없는 경로는 ingress-nginx가 404로 응답한다.

`./monitoring/grafana-public-ingress.yaml`
```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: grafana-public
  namespace: monitoring
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt-route53     # 기존 grafana-ingress와 같은 발급자 (DNS-01)
    nginx.ingress.kubernetes.io/ssl-redirect: "true"
    nginx.ingress.kubernetes.io/limit-rps: "10"              # 공개 쿼리 API 남용 방지 (IP당)
    nginx.ingress.kubernetes.io/limit-connections: "20"
    # whitelist-source-range 없음 = 의도된 공개
spec:
  ingressClassName: nginx
  tls:
    - hosts: [grafanas.studylink.click]
      secretName: grafana-public-tls                          # 기존 grafana-tls와 다른 이름
  rules:
    - host: grafanas.studylink.click
      http:
        paths:
          - path: /public-dashboards/        # 공개 대시보드 화면 (토큰 URL)
            pathType: Prefix
            backend: {service: {name: grafana-service, port: {number: 80}}}
          - path: /api/public/               # 공개 대시보드 전용 API (저장된 패널 쿼리만 실행)
            pathType: Prefix
            backend: {service: {name: grafana-service, port: {number: 80}}}
          - path: /public/                   # 정적 파일 (JS·CSS·이미지·폰트)
            pathType: Prefix
            backend: {service: {name: grafana-service, port: {number: 80}}}
```
```bash
kubectl apply -f ./monitoring/grafana-public-ingress.yaml
kubectl -n monitoring get certificate grafana-public-tls -w      # READY True까지
```
- **경로를 추가하지 않는다** (`/`, `/api/`, `/login`, `/d/` 등). 9장 검증에서 화면이 깨지면, 개발자 도구에서 404가 난 경로를 확인하고 **꼭 필요한 공개 경로인지 판단한 뒤에만** 추가한다. `/api/ds/`, `/api/datasources`, `/api/dashboards/`, `/login`, `/api/live/`는 어떤 경우에도 추가하지 않는다.
- 기존 `grafana-ingress`와 Grafana Deployment·ConfigMap은 **변경하지 않는다.**

---

## 6. 작업 E — 사람: Grafana 설정 (관리자 로그인, VPN에서 `https://grafana.studylink.click`)

### 6-1. MySQL 데이터소스 (`mentor_app` 재사용 — 결정 2026-11-24)
| 항목 | 값 |
|---|---|
| 이름 | `mentor-feedback` |
| Host | **`mariadb-1.mariadb-headless.database.svc.cluster.local:3306`** (복제본 한 대만) — `mariadb-read`·`mariadb-0`은 쓰지 않음 |
| Database | `toxicity` |
| User / Password | `mentor_app` / (Secret `mentor-platform-db`의 값. 사람이 직접 입력, 문서·채팅에 남기지 않음) |
| 최대 연결 수 | 5 |

⚠ **`mentor_app`은 SELECT·INSERT·UPDATE 권한이 있다.** 공개 대시보드 방문자는 저장된 쿼리만 실행하므로 안전하다. 하지만 **VPN 안의 기존 Grafana는 anonymous Viewer도 데이터소스에 임의 쿼리를 보낼 수 있다.** 이 위험은 복제본의 `read_only`로 막는다 → **반드시 확인한다**:
```sql
-- DB 관리자가 mariadb-1에서 실행
SELECT @@hostname, @@read_only;      -- 기대값: mariadb-1, 1
SHOW GRANTS FOR 'mentor_app'@'%';    -- 복제본에도 계정이 있는지 (데이터소스 [Save & test] 성공으로도 확인 가능)
```
- `read_only = 0`이면 **`mentor_app`을 재사용하지 않는다.** 대신 SELECT만 있는 계정(`grafana_ro`)을 만든다 (monitoring.md 3장).
- `mentor_app`에는 `SUPER`·`READ ONLY ADMIN` 권한이 없어야 한다 (있으면 read_only를 우회함). 기존 GRANT는 `SELECT, INSERT, UPDATE ON toxicity.comment_feedback`뿐이다 (`db/schema.sql`).

### 6-2. 대시보드 import
1. Dashboards → New → Import → 레포 `monitoring/dashboards/mentor-model.json` 업로드
2. 입력란 `DS_PROMETHEUS` → 기존 Prometheus 데이터소스, `DS_MYSQL` → `mentor-feedback` 선택
3. 폴더: `mentor` (없으면 생성) → Import
4. `mentor-platform.json`도 같은 방식으로 import
- uid는 JSON에 고정되어 있다 (`mentor-model`, `mentor-platform`). 다시 import하면 덮어쓴다.

### 6-3. 공개 설정 (각 대시보드마다)
Share → **Share externally** (Grafana 13 이름. 이전 버전의 Public dashboard)
| 설정 | 값 | 이유 |
|---|---|---|
| 접근 | Anyone with the link | 시연용 |
| 시간 선택 (time picker) | **끄기** | 넓은 기간으로 무거운 쿼리를 반복하지 못하게 |
| Annotations | **켜기** | 모델 교체 시점 표시 |
- 발급된 URL 2개를 복사한다: `https://grafana.studylink.click/public-dashboards/<토큰>` 형태로 나온다 (`root_url` 미설정이면 `localhost:3000`으로 보일 수 있음 — **토큰 부분만** 쓴다).
- 다른 대시보드는 공개 설정하지 않는다.

---

## 7. 작업 F — 인프라 AI: 공개 URL을 ConfigMap에 반영

```yaml
  GRAFANA_MODEL_URL: "https://grafanas.studylink.click/public-dashboards/<모델 토큰>"
  GRAFANA_PLATFORM_URL: "https://grafanas.studylink.click/public-dashboards/<플랫폼 토큰>"
```
```bash
kubectl apply -f mentor-manifests/platform/10-config.yaml
kubectl -n mentor rollout restart deploy/mentor-api        # 환경변수는 시작할 때만 읽음
```
- 도메인은 반드시 **`grafanas`**(공개용)로 바꾼다. `grafana.`(VPN 전용)이면 시연장에서 보이지 않는다.
- 토큰은 공개 URL의 일부라 비밀값은 아니지만, 아는 사람이면 누구나 해당 대시보드를 볼 수 있다. 시연이 끝나도 공개를 유지할지는 10장을 따른다.

---

## 8. 작업 G — 인프라 AI + 사람: 시연용 플랫폼 접근 (화이트리스트 해제 — 결정 2026-11-24)

플랫폼 화면(`mlops.studylink.click`)은 iframe을 품는 페이지라 시연장에서 열려야 한다. 1-2에서 확인했듯 **화이트리스트는 지금 이미 없다.**
하지만 플랫폼에는 **로그인이 없고** 리소스 삭제·YAML apply·Secret 조회가 가능하다 → **기본 인증(아이디·비밀번호)을 반드시 건다.**

### 8-1. Secret (`./secret/mentor-platform-basic-auth.yaml`, infra_guide 4-4 규칙: 값 공란, 사람이 채움, git 금지)
```yaml
apiVersion: v1
kind: Secret
metadata:
  name: mentor-platform-basic-auth
  namespace: mentor
type: Opaque
stringData:
  auth: ""        # 사람이 채움: `htpasswd -nbB <아이디> <비밀번호>` 출력 한 줄 (bcrypt)
```

### 8-2. `mentor-platform` Ingress에 annotation 추가 (`./mentor-manifests/platform/` Ingress 파일)
```yaml
    nginx.ingress.kubernetes.io/auth-type: basic
    nginx.ingress.kubernetes.io/auth-secret: mentor-platform-basic-auth
    nginx.ingress.kubernetes.io/auth-realm: "MLOps Platform"
```
- 인증은 `/` · `/api` · `/docs` 등 Ingress의 모든 경로에 걸린다. 화면과 API가 같은 주소라 **브라우저가 한 번 입력한 인증을 API 호출에도 자동으로 쓴다** (코드 변경 없음).
- kubelet probe(`/healthz`, `/readyz`)는 파드에 직접 호출하므로 영향 없음.
- iframe 안의 Grafana(`grafanas.…`)는 다른 Ingress라 인증과 무관하다.
- 적용 순서: **8-1 Secret을 사람이 채워 적용한 뒤에** 8-2 적용 (Secret이 없으면 ingress-nginx가 해당 Ingress를 503으로 응답).
- 시연 후 VPN 전용으로 되돌리려면 `whitelist-source-range: "10.0.0.0/24,192.168.2.0/24"`를 추가한다 (기본 인증과 함께 걸면 둘 다 통과해야 접속).

---

## 9. 검증 체크리스트 (VPN을 끄고 — 휴대폰 LTE 등 외부망에서)

- [ ] `https://mlops.studylink.click` → **아이디·비밀번호 요구**, 입력 후 화면 정상, 각 메뉴 동작
- [ ] [모니터링] 메뉴 → 탭 2개 모두 대시보드 표시, 개발자 도구 네트워크 탭에 **404·403 없음**, 패널에 "No data"가 비정상적으로 많지 않음 (MySQL 패널 포함)
- [ ] `https://grafanas.studylink.click/login` → **404**
- [ ] `https://grafanas.studylink.click/api/datasources` → **404**
- [ ] `https://grafanas.studylink.click/api/ds/query` (POST) → **404**
- [ ] `https://grafanas.studylink.click/d/<다른 프로젝트 대시보드 uid>` → **404**
- [ ] `https://grafanas.studylink.click/` → **404**
- [ ] `https://grafana.studylink.click` → 이름을 찾을 수 없음 (VPN 전용 그대로)
- [ ] (VPN 켜고) Prometheus targets: `mentor` 규칙 2개 UP, **기존 규칙 전부 이전과 같은 상태**
- [ ] 모델 교체를 한 번 해 보고 → 모델 대시보드에 버전 변경 annotation 표시

---

## 10. 하지 않는 것

| ❌ | 이유 |
|---|---|
| 기존 `grafana-ingress`의 host 변경·whitelist 제거 | Grafana 전체가 공개됨 (다른 프로젝트 대시보드, 데이터소스 임의 쿼리, 로그인 페이지) |
| `grafana-public`에 `/` 또는 `/api/` 전체 경로 추가 | 위와 같음 |
| Grafana `grafana.ini`의 anonymous·embedding 설정 변경 | 다른 프로젝트가 쓰는 공용 설정. 이번 작업에는 필요 없음 |
| 파드 주석 `prometheus.io/scrape` 사용 | 기존 `spring-boot-actuator` 규칙에 섞여 수집됨 (결정: `mentor` 규칙 사용) |
| 기존 `scrape_configs` 단락 수정·순서 변경 | 다른 프로젝트 수집에 영향 |
| `/metrics`를 `mentor-platform` Ingress 경로에 추가 | 내부 상태 노출. Prometheus는 파드 IP로 직접 수집함 |
| Grafana 데이터소스에 `mariadb-0`·`mariadb-read` 사용 | 쓰기 가능한 파드에 연결됨 (6-1) |
| 기본 인증 없이 `mentor-platform`을 공개 상태로 두기 | 로그인 없는 관리 도구 (삭제·Secret 조회) |
| Grafana 이미지 버전 고정 | 이번 범위 밖 (결정 2026-11-24) |

---

## 11. 문제 해결

| 증상 | 원인 | 조치 |
|---|---|---|
| Prometheus 재시작 후 CrashLoop | 설정 문법 오류 | 2-1 백업을 apply → 파드 삭제. `promtool` 검사 후 재시도 |
| `mentor` 대상 0개 (3·4장 이후) | 파드 템플릿에 `platform.mentor/metrics` 라벨 없음 / 포트 이름이 `http`가 아님 | `kubectl -n mentor get pod --show-labels`, 컨테이너 `ports[].name` 확인 |
| `mentor` 대상 DOWN, 404 | 이미지가 아직 이전 버전 (`/metrics` 없음) | 이미지 태그 확인 (`model_v3`, `platform-api_v5`) |
| 공개 대시보드 빈 화면, 개발자 도구에 `/public/build/…` 404 | `/public/` 경로 규칙 누락 | 5장 YAML과 비교 |
| 공개 대시보드 패널 "No data" (Prometheus) | 3·4장 전이거나 트래픽이 없음 | 댓글 몇 개 분류 후 1분 대기. 쿼리의 `job="mentor"` 확인 |
| 공개 대시보드 MySQL 패널 오류 | 데이터소스 접속 실패 / 공개 대시보드 미지원 | VPN Grafana에서 데이터소스 [Save & test]. 공개 화면에서만 실패하면 해당 패널 쿼리 확인 |
| `grafanas.…` 인증서 경고 | Certificate 미발급 | `kubectl -n monitoring describe certificate grafana-public-tls` (DNS-01, Route 53) |
| 429 Too Many Requests | `limit-rps` 초과 (대시보드 새로고침 반복) | 정상 동작. 시연 중 계속 나면 `limit-rps`를 20으로 |
| 플랫폼 화면 503 | 8-2 적용 시 Secret 없음·형식 오류 | `kubectl -n mentor get secret mentor-platform-basic-auth`, `auth` 키가 `아이디:$2y$…` 한 줄인지 |
| [모니터링] 메뉴 "대시보드 준비 중" | ConfigMap 값 비어 있음 / API 재시작 전 | 7장 다시 확인 → `rollout restart deploy/mentor-api` |

---

## 12. 되돌리기

| 대상 | 방법 |
|---|---|
| Prometheus 규칙 | 2-1 백업 apply → `prometheus-statefulset-0` 삭제 |
| 공개 Grafana | `kubectl -n monitoring delete ingress grafana-public` (+ Grafana에서 공개 설정 해제 → 토큰 무효화) |
| 플랫폼 이미지 | `30-api.yaml` `platform-api_v4`, `40-web.yaml` `platform-web_v3`로 되돌려 apply |
| 모델 | 화면에서 `model_v2`로 되돌려 [적용] (라벨은 남겨도 무해) |
| 시연 후 접근 제한 | 8-2 Ingress에 `whitelist-source-range` 추가 (VPN·내부망 전용 복귀) |
