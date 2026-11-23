# train_인수인계.md — 모델 재학습 · 모델 교체 기능 클러스터 반영

> **대상**: ① 최상위 인프라 관리 디렉터리(`./`)에서 실행하는 Claude Code (2장) ② 사람 (3~6장, 플랫폼 화면·NFS)
> **선행 문서**: `infra_guide.md` (0~3장 규칙, 9장 워크로드 관리), `job_인수인계.md` (데이터셋 재생성 — 적용 완료 상태여야 함). 충돌하면 `infra_guide.md` 우선.
> **근거**: 레포 `k8sapimd/train.md` (설계 + 15장 구현 결과). 코드·이미지 완료, **남은 것은 적용**.
> **작성일**: 2026-11-23 (아래 클러스터 사실은 이 날짜에 읽기 전용으로 확인)

---

## 0. 한 줄 요약

플랫폼 화면 [모델 재학습] → **[모델 재학습]** 탭이 데이터셋으로 학습 Job 을 만들어 MLflow 등록 모델 `toxicity-filter` 의 새 버전을 등록하고,
**[모델 교체]** 탭이 NFS `workloads/toxicity-filter.yaml` 의 `MODEL_URI` 한 줄을 바꿔 적용 → 추론 서버 파드의 initContainer 가 MLflow 에서 그 버전을 받아 서빙한다.

**순서가 중요하다** — MLflow 정리 → 플랫폼 이미지 → NFS 파일 → 기준 모델(버전 1) 등록 → **마지막에** 추론 서버 전환.
(버전 1 이 없는 상태에서 추론 서버를 전환하면 새 파드가 `Init:Error` — 옛 파드가 계속 서빙하므로 서비스는 안전하지만 전환이 안 됨)

---

## 1. 지금 상태 (확인됨)

| 항목 | 상태 |
|---|---|
| 이미지 (Docker Hub `suhannugul/mentor`) | ✅ push 완료 — `job-train_v1` (`sha256:18bb0c50…`, 학습 Job · initContainer), `model_v2` (`sha256:0e0fa79a…`, 모델 없는 추론 서버), `platform-api_v4` (`sha256:8f655acb…`), `platform-web_v3` (`sha256:bf473e27…`) |
| 이미지 점검 | ✅ 로컬 MLflow 3.18.0 에 붙여 실제 이미지로: 학습 Job(uid 1000, 읽기 전용 루트) → 버전 등록 → initContainer 다운로드(uid 10001) → `model_v2` 가 `toxicity-filter/3` 으로 서빙 |
| MLflow | `ghcr.io/mlflow/mlflow:latest`(실제 3.18.0) + `imagePullPolicy: Always`, probe 없음, 등록 모델 0개 → **3장에서 정리** |
| `mentor-api` / `mentor-web` | `platform-api_v3` / `platform-web_v2` → **v4 / v3** (2장) |
| ConfigMap `mentor-platform-config` | `MLFLOW_*` 없음 → **2개 추가** (2장) |
| NFS `/home/suhan/csv/eval.csv` | ❌ 없음 → **4장** |
| NFS `/home/suhan/manifests/job/train.yaml` | ❌ 없음 → **4장** |
| NFS `workloads/toxicity-filter.yaml` | `model_v1` (모델이 이미지 안) → **6장에서 교체** |
| Role `mentor-platform` | 변경 없음 — Job create(학습 Job), Deployment apply(Work 3 로 이미 함) 모두 기존 권한 |
| PVC | 변경 없음 — 학습 Job 은 기존 `mentor-csv` 를 읽기 전용으로, 추론 서버는 NFS 를 쓰지 않음 |

---

## 2. 작업 A — 최상위 Claude Code: ConfigMap · API · 화면 (`./mentor-manifests/platform/`)

### 2-1. `10-config.yaml` — ConfigMap `mentor-platform-config` 에 두 키 추가
```yaml
  MLFLOW_TRACKING_URI: "http://mlflow.mentor.svc.cluster.local:5000"   # API 가 MLflow REST 호출 (없으면 재학습·교체 기능 503)
  MLFLOW_UI_URL: "http://192.168.2.20:30081"                          # 화면의 "MLflow ↗" 링크 (브라우저가 여는 주소)
```
- `MLFLOW_TRACKING_URI` 의 호스트는 MLflow `--allowed-hosts` 에 이미 있음 (`mlflow.mentor.svc.cluster.local:5000`)
- `MLFLOW_UI_URL` 은 링크용. 지금 MLflow 는 NodePort 30081 → 아무 노드 IP 나 가능. 나중에 Ingress 로 바꾸면(train.md 11장, 시연 후) 이 값만 수정
- 데이터셋 Job 도 이 ConfigMap 을 통째로 주입받지만 이 키는 쓰지 않음 (무해)

### 2-2. `30-api.yaml` — 이미지만
```yaml
          image: suhannugul/mentor:platform-api_v4        # ← v3 → v4 (마운트·env·probe 변경 없음)
```
### 2-3. `40-web.yaml` — 이미지만
```yaml
          image: suhannugul/mentor:platform-web_v3        # ← v2 → v3
```
```bash
kubectl apply -f mentor-manifests/platform/10-config.yaml
kubectl apply -f mentor-manifests/platform/30-api.yaml     # ConfigMap 변경은 파드 재시작 때 반영 → 이미지 교체로 함께 반영됨
kubectl apply -f mentor-manifests/platform/40-web.yaml
kubectl -n mentor rollout status deploy/mentor-api
kubectl -n mentor rollout status deploy/mentor-web
```
- 필요 시 선택 환경변수 (기본값이면 넣지 않음): `TRAIN_JOB_FILE`(job/train.yaml) · `MODEL_WORKLOAD_FILE`(workloads/toxicity-filter.yaml) · `REGISTERED_MODEL`(toxicity-filter) · `MODEL_DEPLOYMENT`(toxicity-filter)

---

## 3. 작업 B — 사람: MLflow 고정 (플랫폼 매니페스트 관리 화면, `workloads/mlflow.yaml`)

MLflow 는 플랫폼이 관리하는 워크로드 → **화면에서 수정 → 저장 → [적용]** (kubectl 아님, train.md 11장). 컨테이너 `mlflow` 에서:
```yaml
          image: ghcr.io/mlflow/mlflow@sha256:257204c77980b0f3450a26eb6089e487603fcdd4509529eadaf88f90669f5c13   # 지금 실행 중인 3.18.0 그대로
          imagePullPolicy: IfNotPresent
          readinessProbe:
            httpGet: {path: /health, port: 5000}
            periodSeconds: 10
            failureThreshold: 3
          livenessProbe:
            httpGet: {path: /health, port: 5000}
            initialDelaySeconds: 30
            periodSeconds: 20
            failureThreshold: 3
```
- 적용 시 MLflow 파드 재시작 (hostPath 라 worker-1 에 다시 뜸, 데이터 유지). 지금은 추론 서버가 MLflow 를 안 쓰므로 댓글 기능 영향 없음
- 확인: 판정 "일치", `kubectl -n mentor get pod -l app=mlflow` Ready 1/1

---

## 4. 작업 C — 사람: NFS 파일 두 개

| 파일 | 내용 | 방법 |
|---|---|---|
| `/home/suhan/csv/eval.csv` | 레포 **`model/data/eval.csv`** 그대로 (고정 평가셋 92행) | NFS 서버에 복사, `chown 1000:1000`, `chmod 644`. **이후 바꾸지 않는다** (바꾸면 버전 간 지표 비교 불가) |
| `/home/suhan/manifests/job/train.yaml` | 레포 **`model/job/train.yaml`** 그대로 (학습 Job 템플릿) | 2장 적용 후 화면 **매니페스트 관리 → [+ 새 파일] `job/train.yaml`** 에 붙여 넣고 저장. **[적용] 하지 않음** (DATASET 이 비어 있어 바로 실패할 뿐 — 화면 [학습 시작] 으로 실행) |

---

## 5. 작업 D — 사람: 기준 모델(버전 1) 등록 (플랫폼 화면)

1. [모델 재학습] → **[모델 재학습]** 탭 → 데이터셋 드롭다운 맨 아래 **`seed`** 선택 → [학습 시작]
2. Job 칸: 대기 → 실행 중 → 완료 **"버전 1 등록 · seed · eval F1 0.9245 · 학습 366행 (피드백 0)"**
   - eval F1 0.9245 는 지금 `model_v1` 과 **같은 값** (같은 366행·같은 설정) → 전환해도 판정이 바뀌지 않음
3. 아래 "등록된 버전" 표에 버전 1. MLflow UI(30081) → Models → `toxicity-filter` 에도 보임

---

## 6. 작업 E — 사람: 추론 서버 전환 (매니페스트 관리 화면, `workloads/toxicity-filter.yaml`)

1. 기존 파일 내용을 레포 **`model/workload/toxicity-filter.yaml`** 로 **통째로 교체** → 저장
   - 바뀌는 것: initContainer `fetch-model`(job-train_v1) 추가 · 앱 이미지 `model_v1` → `model_v2` · `MODEL_PATH=/models/model.pkl` · 볼륨 `model`(emptyDir) · tmp 16Mi → 64Mi
   - Service·selector·probe·리소스·보안 설정은 그대로
   - `MODEL_URI` 줄 끝의 **`# platform.mentor/model-uri` 표식을 지우지 말 것** — [모델 교체] 화면이 이 줄 하나만 고친다
2. [적용] → 롤링 업데이트: 새 파드 `Init:0/1`(모델 다운로드, 첫 회는 이미지 pull 로 1~2분) → Running → Ready → 옛 파드 종료
3. 확인: [모델 교체] 탭 → 선언 `models:/toxicity-filter/1` · 실제 서빙 `toxicity-filter/1` **✔ 선언과 일치** · 롤아웃 완료 · champion `1`
4. 댓글 기능에서 댓글 하나 분류 → 피드백 화면의 모델 버전이 `toxicity-filter/1`

---

## 7. 하지 않는 것

| ❌ | 이유 |
|---|---|
| 6장을 5장보다 먼저 | 버전 1 이 없어 새 파드 `Init:Error` (옛 파드가 계속 서빙하므로 장애는 아님 — 5장 후 재적용하면 됨) |
| `workloads/*.yaml` · `job/train.yaml` 을 kubectl 로 적용 / 최상위로 옮기기 | 플랫폼 화면이 관리하는 선언 (infra_guide 9장). kubectl 로 바꾸면 판정 "불일치" |
| MLflow alias `champion` 을 MLflow UI 에서 직접 옮기기 | champion 은 "마지막으로 교체가 끝난 버전" 표시·되돌리기 기준. 플랫폼이 롤아웃 완료를 보고 옮김 |
| `eval.csv` 수정 | 버전 간 지표 비교가 깨짐 (화면에 "다른 평가셋" 경고) |
| Role·PVC·Secret 추가 | 불필요 (1장) |

---

## 8. 문제 해결

| 증상 | 원인 | 조치 |
|---|---|---|
| [모델 재학습] 탭 "모델 재학습 기능이 설정되지 않았습니다 (MLFLOW_TRACKING_URI)" | 2-1 미적용 또는 API 가 재시작 전 | ConfigMap 확인 → `kubectl -n mentor rollout restart deploy/mentor-api` |
| "MLflow 에 연결할 수 없습니다" | MLflow 파드 중단 / 주소 오타 | `kubectl -n mentor get pod -l app=mlflow`, ConfigMap 값 |
| 배너 "eval.csv 가 데이터셋 폴더에 없습니다" | 4장 미완료 | NFS `ls -ln /home/suhan/csv/eval.csv` |
| 배너 "Job 명세 파일 문제" | `job/train.yaml` 없음·형식 오류 | 레포 `model/job/train.yaml` 과 비교 |
| 학습 Job 실패 "학습 데이터 부족: 라벨 1 이 …행뿐" | 정리 후 라벨별 20행 미만 | 데이터셋 확인. 기준값은 템플릿 env `MIN_PER_LABEL` |
| 학습 Job 실패 "데이터셋 파일이 생성 후 바뀌었습니다" | CSV 를 손으로 수정함 | 데이터셋을 새로 생성 |
| 학습 Job 실패 "MlflowException … / ConnectionError" | MLflow 접속 불가 | 위와 같음. Job 은 재시도 없음 → 다시 [학습 시작] |
| [모델 교체] "표식이 없습니다 — 서빙이 아직 MLflow 방식이 아닙니다" | 6장 전 / 표식 줄 삭제 | 6장 진행 / 레포 파일과 비교 |
| [모델 교체] 롤아웃 "실패 — Init:Error / Init:CrashLoopBackOff" | 그 버전을 MLflow 에서 못 받음 (MLflow 중단·버전 없음) | 옛 파드가 계속 서빙 중. **[되돌리기]** (champion 버전으로) 또는 MLflow 복구 후 대기 |
| [모델 교체] "scikit-learn 버전이 다릅니다" | 다른 세대 이미지로 학습한 버전 | 같은 세대 `model_vN` 으로 서빙 이미지를 맞춘 뒤 교체 |
| [모델 교체] "적용 실패 — 파일을 원래대로 되돌렸습니다: … 충돌" | kubectl 등 다른 관리자가 소유한 필드 | 매니페스트 관리 화면에서 그 파일을 [강제 적용] 1회 후 다시 교체 |
| 새 파드가 `Init:0/1` 에서 오래 걸림 | 노드에 `job-train_v1`(585MB) 첫 pull | 기다림 (이후는 IfNotPresent) |

---

## 9. 되돌리기

- **추론 서버**: `workloads/toxicity-filter.yaml` 을 이전 내용(`model_v1`, initContainer 없음)으로 되돌려 [적용] → 이미지 안 모델로 복귀
  - 이전 내용은 레포에 없음 → **6장 전에 기존 파일 내용을 따로 보관**해 둘 것 (매니페스트 화면에서 복사)
- **API·화면**: `30-api.yaml` → `platform-api_v3`, `40-web.yaml` → `platform-web_v2` (ConfigMap 의 `MLFLOW_*` 는 남겨도 무해)
- **MLflow**: `workloads/mlflow.yaml` 의 이미지를 `latest` 로 되돌리기 (권장하지 않음)
- MLflow 에 등록된 버전·run 은 지우지 않아도 됨 (수 MB)
