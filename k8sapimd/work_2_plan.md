# Work 2 진행 계획 (구현 전 제출)

> 대상: `work_2.md` — `mentor` 네임스페이스 리소스 **조작(쓰기)**: ① 삭제(확인 절차 포함) ② 롤아웃 재시작
> 기준점: Work 1(조회 API + Watch 캐시) · Front 1(인프라 관리 화면 시각화) 완료 상태
> 배경: `base.md` — "선언한 상태를 지속 유지한다"를 **직접 조작해서 눈으로 확인**하는 단계

---

## 1. 이번 Work의 범위

| 포함 (Work 2) | 제외 (이후 Work) |
|---|---|
| 삭제 API + 화면의 "정말 삭제하시겠습니까? 예/아니오" | manifest 수정·apply, Desired vs Current(drift) 비교 |
| 롤아웃 재시작 API (Deployment / StatefulSet) | 재학습 Job 생성·트리거 |
| 이벤트 10개 단위 페이지 넘김 | ServiceAccount/RBAC 실제 생성, 파드 배포 |
| CORS·에러 처리·테스트 | 로그인/권한 체계 |
| 이벤트 조회 · 파드 로그 보기 (6장에서 확정) | 스케일 조정, dry-run, 롤아웃 undo, Job 재실행 |

쓰기 결과는 **별도 응답으로 상태를 만들지 않고**, 기존 Watch 캐시가 이벤트로 반영 → 화면은 지금처럼 2초 polling으로 변화를 봅니다.
(= "API는 원하는 상태만 바꾸고, 실제 상태는 컨트롤러가 맞춘다"는 base.md 메시지가 그대로 화면에 드러남)

---

## 1-1. 원칙: 선언(매니페스트)과 실제 인프라 상태의 일치

모든 조작은 "선언과 실제의 일치"에 어떤 영향을 주는지 **실행 전에 서버가 계산해서 확인창에 보여준다** (`effect`).

| effect | 의미 | 예 |
|---|---|---|
| `reconcile` | 선언은 그대로 → 컨트롤러가 선언에 맞춰 복구 (셀프힐링) | Deployment가 관리하는 Pod 삭제 |
| `drift` | 선언된 리소스 자체가 사라짐 → 매니페스트(YAML)와 실제 인프라가 **불일치**. 영구 삭제면 매니페스트에서도 제거해야 함 | Deployment / Service / PVC 삭제 |
| `rollout` | spec(이미지·replicas 등)은 그대로, pod template에 `restartedAt` 어노테이션만 기록 → 파드 순차 교체 | 롤아웃 재시작 |

- **replicas 조정(스케일)은 제외** — API로 선언을 직접 바꾸면 매니페스트와 어긋남. 매니페스트를 고쳐 apply 하는 이후 Work에서 다룬다.
- `kubectl.kubernetes.io/restartedAt` 은 매니페스트에 없는 값 → 이후 drift 비교 Work에서 **비교 제외 대상**으로 처리.

---

## 2. 현재 코드 점검 결과 (변경 필요 지점)

| 위치 | 현재 | Work 2에서 할 일 |
|---|---|---|
| `main.py` CORS | `allow_methods=["GET"]` (조회 전용) | `DELETE`, `POST` 추가 — 안 하면 브라우저 preflight에서 막힘 |
| `registry.py` `KindSpec` | `list_func`만 보유 | kind별 `delete_func`, `restartable`(Deployment/STS만) 추가 → 쓰기도 registry 한 곳에서 관리 |
| `routes.py` | GET만 | 삭제·재시작 라우트 추가, `ApiException` → HTTP 상태 매핑 |
| `summarize.py` | Pod만 `deletionTimestamp` → Terminating 표시 | **모든 kind** 공통으로 `terminating` 플래그 노출 (PVC는 사용 중이면 finalizer 때문에 계속 Terminating) |
| `summarize.common` | ownerReferences 미노출 | `owner`(예: `ReplicaSet/toxicity-filter-7bc84d74fb`) 노출 → 삭제 확인창에서 "컨트롤러가 다시 만듭니다" 안내 |
| 프론트 `api.js` | GET 전용 `request()` | method/body 지원 |
| 프론트 `detail.js` | 조회 전용 패널 | 헤더에 [재시작] [삭제] 버튼, 확인 모달 |
| README 권한 표 | get/list/watch | `delete`, `patch` 추가 |

현재 클러스터(`mentor`): Deployment `toxicity-filter` 1개, Pod 1개, Service 1개, PVC `mlflow-data` 1개 (Job/STS 없음).
로컬 kubeconfig는 `kubernetes-admin`(전체 권한)이므로 **실제 테스트 시 진짜로 삭제됩니다** → 테스트용 리소스를 따로 만들어 검증 (8장).

---

## 3. 기능 1 — 삭제

### 확인 절차 ("정말 삭제하시겠습니까?" — if문 처리)

확인을 **프론트와 서버 양쪽**에서 if문으로 처리합니다. 프론트 확인창만 있으면 Swagger/curl로 바로 지워질 수 있기 때문.

```
[프론트]  삭제 버튼 → 모달 "pods/xxx 를 정말 삭제하시겠습니까?  [예] [아니오]"
          if (예)  → DELETE 요청 (confirm=true 포함)
          else     → 아무것도 안 함

[서버]    DELETE /api/v1/resources/{kind}/{name}?confirm=true
          if not confirm:            → 400 "confirm=true 가 필요합니다"
          if 캐시에 없음:            → 404
          if 보호 대상(라벨):         → 403 (아래 보호 장치)
          else                       → k8s delete 호출 → 202 Accepted
```

### 구현 포인트 (주의할 함정)

1. **propagationPolicy=Background 명시** — Python 클라이언트로 Job을 그냥 지우면 기본값이 `Orphan`이라 **Job만 지워지고 파드가 고아로 남습니다**. kubectl과 같은 동작이 되도록 `V1DeleteOptions(propagation_policy="Background")` 고정.
2. **응답은 202** — 삭제는 비동기(Terminating → 사라짐). 결과는 Watch의 `DELETED` 이벤트로 캐시에서 빠지고 화면에서 사라짐.
3. **이미 Terminating인 리소스** → 409 "이미 삭제 진행 중"
4. **네임스페이스 고정** — 경로에 ns가 없고 서버가 `WATCH_NAMESPACE`로 고정하므로 다른 네임스페이스는 구조적으로 못 지움.
5. **확인창 문구를 kind별로 다르게** (owner 정보 활용):
   - 컨트롤러가 있는 Pod: "ReplicaSet이 관리하는 파드입니다. 삭제하면 **새 파드가 자동 생성**됩니다." ← 셀프힐링 시연 포인트
   - 단독 Pod: "다시 생성되지 않습니다."
   - Deployment/STS: "관리하는 파드 N개도 함께 삭제됩니다."
   - PVC: "데이터가 삭제될 수 있습니다. 사용 중이면 파드가 없어질 때까지 Terminating 상태로 남습니다."
6. **보호 장치(추천)** — `platform.mentor/protected: "true"` 라벨이 붙은 리소스는 서버가 if문으로 거부(403). 이후 이 플랫폼 서버 자체를 클러스터에 올렸을 때 실수로 자기 자신을 지우는 것 방지.

---

## 4. 기능 2 — 롤아웃 재시작

`kubectl rollout restart` 와 **완전히 같은 방식**: pod template 어노테이션만 바꾸는 patch.

```
PATCH (strategic merge)
spec.template.metadata.annotations["kubectl.kubernetes.io/restartedAt"] = <현재 시각 ISO8601>
```

→ template이 바뀌었으므로 Deployment 컨트롤러가 새 ReplicaSet을 만들고 롤링 업데이트. **"우리는 원하는 상태(template)만 바꿨고, 파드 교체는 컨트롤러가 했다"** — 선언형 관리 설명에 가장 좋은 예시.

| 항목 | 결정 |
|---|---|
| API | `POST /api/v1/resources/{kind}/{name}/restart` |
| 대상 | Deployment, StatefulSet 만 (그 외 kind → 400 "재시작을 지원하지 않는 kind") |
| 확인창 | 삭제와 같은 모달 재사용 ("재시작하시겠습니까?") — **서버도 `confirm=true` 필수** (없으면 400, 삭제와 동일한 if문) |
| 일시정지된 Deployment | `spec.paused == true`면 kubectl처럼 400 거부 (if문) |
| 진행 표시 | 기존 데이터로 가능: `updated_replicas / replicas`, 새 파드 등장 → 화면에서 롤링 과정이 보임 |
| Pod 재시작은? | k8s에는 "파드 재시작" API가 없음 → 컨트롤러 있는 파드는 **삭제 = 재생성**이 사실상 재시작. 화면 문구로 안내 |

---

## 5. API 설계 (추가분)

| Method | Path | 설명 | 성공 |
|---|---|---|---|
| DELETE | `/api/v1/resources/{kind}/{name}?confirm=true` | 삭제 (Background propagation) | 202 |
| POST | `/api/v1/resources/{kind}/{name}/restart?confirm=true` | 롤아웃 재시작 | 202 |
| GET | `/api/v1/resources/pods/{name}/logs?container=&tail_lines=200` | 파드 로그 (tail) | 200 |
| GET | `/api/v1/resources/events?field=involvedObject.name` | 이벤트 (watcher kind로 추가, 기존 목록 API 재사용) | 200 |

응답 예: `{"kind":"pods","name":"xxx","action":"delete","accepted":true,"message":"삭제 요청됨 — ReplicaSet이 새 파드를 생성합니다"}`

**에러 매핑** (`ApiException.status` 그대로 전달 + 한국어 메시지)

| k8s | API | 상황 |
|---|---|---|
| 404 | 404 | 캐시엔 있었지만 그 사이 삭제됨 |
| 403 | 403 | 나중에 SA 권한 부족 (Role에 delete/patch 누락) |
| 409 | 409 | 충돌 / 이미 삭제 중 |
| 422 | 400 | 잘못된 patch |
| 연결 실패 | 502 | API 서버 접속 불가 |

> 조작 이력(감사 로그)은 구현했다가 **제거함** (요청에 따라). 변경 이력 관리는 DB가 붙는 이후 Work에서 다시 검토.

---

## 6. 추천 추가 기능 (k8s API 활용)

선언형 관리 메시지 + 운영 실무 유용성 기준으로 우선순위:

| 순위 | 기능 | k8s API | 이유 | 난이도 |
|---|---|---|---|---|
| ★1 | **스케일 조정** (replicas 변경) | `patch_namespaced_deployment_scale` / `_stateful_set_scale` | "replicas: 3 이라고 **선언**하면 컨트롤러가 파드 3개로 맞춘다"를 가장 직관적으로 보여줌. 발표 시연용으로 최적 | 하 |
| ★2 | **이벤트 조회** | `list_namespaced_event` (watcher kind 추가) | 파드가 왜 Pending/CrashLoop인지, 삭제·재시작 후 컨트롤러가 무슨 일을 했는지(Killing → Scheduled → Pulled → Started) 타임라인으로 보임. 조회 전용이라 위험 없음 | 하 (registry 한 줄 + 컬럼) |
| ★3 | **파드 로그 보기** | `read_namespaced_pod_log(tail_lines=…)` | 운영 필수 기능. 이후 재학습 Job 로그 확인에 그대로 재사용 | 중하 |
| 4 | 삭제 dry-run 미리보기 | delete + `dry_run="All"` | 확인창 전에 서버측 검증(권한·존재) 결과를 먼저 보여줌 | 하 |
| 5 | 롤아웃 이력 / 되돌리기(undo) | ReplicaSet·ControllerRevision 조회 후 template 복원 | 의미는 크지만 구현량 많음 → apply/drift Work와 함께 하는 것이 자연스러움 | 상 |
| 6 | Job 재실행 | Job은 spec 불변 → 기존 manifest로 새 Job 생성 | 재학습 Work에서 진행 | 중 |

**확정: 이번 Work에 ★2 이벤트 조회 + ★3 파드 로그 포함.** ★1 스케일, 4~6은 이후 Work.

- 이벤트: registry에 `events` kind 추가(Watch 캐시 재사용). 상세 패널에 해당 리소스의 이벤트 타임라인(involvedObject 이름 일치) 표시
- 로그: `read_namespaced_pod_log(tail_lines=200, container=…)` — 캐시 대상 아님, 요청 시 직접 호출. 상세 패널(Pod)에 [로그] 탭, 컨테이너 여러 개면 선택

---

## 7. 변경 파일

```
server/app/
├─ main.py            CORS methods 확장
├─ k8s/registry.py    KindSpec에 delete_func / restart(patch)_func 추가, events kind 추가
├─ k8s/actions.py     (신규) delete / restart 실행 + ApiException 변환 + 보호 라벨 검사
├─ k8s/summarize.py   common()에 terminating, owner 추가
├─ api/routes.py      DELETE / POST restart / GET logs 라우트, 목록 limit/offset
└─ schemas.py         ActionResult, PodLogs, 목록 offset/limit
server/tests/
└─ test_actions.py    (신규) confirm 없음→400, 보호 라벨→403, 지원 안 하는 kind→400,
                      Background propagation 전달 확인, restart patch body 형태, ApiException 매핑
front/js/
├─ api.js             method/body 지원, remove()/restart()
├─ infra/confirm.js   (신규) 예/아니오 모달 (Esc=아니오, 포커스 트랩)
├─ infra/detail.js    상세 패널 헤더에 [재시작] [삭제] 버튼, 이벤트 타임라인, Pod [로그] 탭
├─ infra/columns.js   Events 탭 컬럼 (TYPE, REASON, OBJECT, MESSAGE, COUNT, LAST SEEN)
└─ pages/infra.js     작업 결과 토스트, Terminating 행 흐리게
```

---

## 8. 진행 순서

1. `summarize.common`에 terminating/owner 추가 (+ 기존 테스트 보완)
2. `actions.py` + 단위 테스트 (k8s 호출은 mock — 클러스터 변경 없음)
3. 라우트 + 에러 매핑 + CORS
4. 프론트: api.js → 확인 모달 → 상세 패널 버튼 → 결과 토스트
5. **실제 클러스터 검증은 테스트용 리소스로만** (`toxicity-filter`, `mlflow-data`는 건드리지 않음)
   ```
   kubectl create deployment w2-test --image=nginx -n mentor --replicas=2
   ```
   - 파드 삭제 → 새 파드 생성이 화면에 보이는지 (셀프힐링)
   - w2-test 재시작 → 새 ReplicaSet 파드로 롤링 교체되는지
   - w2-test 삭제 → 파드까지 함께 정리되는지
   - 끝나면 `kubectl delete deployment w2-test -n mentor`로 정리
6. README에 API·권한(Role) 갱신

---

## 9. 완료 기준 (구현 결과)

- [x] 확인 없이(`confirm` 누락) DELETE·재시작 시 400, "아니오" 선택 시 요청 자체가 안 나감 (브라우저에서 확인: 이력·클러스터 변화 없음)
- [x] 6개 kind 삭제 지원, Background propagation (실제: w2-test 삭제 시 ReplicaSet·파드까지 정리 확인)
- [x] Deployment 롤아웃 재시작 → 새 ReplicaSet으로 순차 교체, spec은 annotation 외 변경 없음 (replicas 2·이미지 동일 확인)
- [x] 컨트롤러 소유 파드 삭제 → 재생성 확인 (Work 1 미실시 항목 해결)
- [x] 보호 라벨 리소스 삭제 시 403 (실제 확인)
- [x] 에러(404/403/409/502) 매핑 + 화면 알림
- [x] ~~조작 이력~~ → 제거
- [x] 이벤트 10개 단위 ◀ ▶ 페이지 넘김 (목록 탭·상세 패널 모두)
- [x] 이벤트 타임라인·파드 로그가 상세 패널에 표시 (실제 확인)
- [x] 단위 테스트 28개 통과 (기존 16 + 신규 12, `tests/test_actions.py`)
- [x] 테스트용 `w2-test` 정리 완료 (`toxicity-filter`, `mlflow-data` 변경 없음)

### 이벤트는 어디에 저장되나? (DB 없음)

```
kubelet·컨트롤러 ──(이벤트 생성)──▶ kube-apiserver ──▶ etcd (/var/lib/etcd, control-plane 노드)
                                                        │  ※ 기본 1시간 후 자동 삭제 (--event-ttl 기본값 1h)
우리 서버 ◀──(List + Watch)── kube-apiserver ◀───────────┘
   └ 메모리 캐시(ResourceCache, 파이썬 dict) — 서버를 재시작하면 비워졌다가 다시 List로 채움
```

- Event도 Pod·Deployment와 똑같은 **쿠버네티스 API 객체**(`v1/Event`, 네임스페이스 단위)라서 **클러스터의 etcd**에 저장된다.
  우리 플랫폼은 별도 DB 없이 이것을 Watch로 받아 **메모리에만** 들고 있다.
- 클러스터 확인 결과: kube-apiserver에 `--event-ttl` 미지정 → **기본 1시간** 보관. `mentor` 이벤트 48개는 모두 최근 1시간 안의 것.
- 따라서 이벤트는 "영구 이력"이 아니다. 오래 보관하려면 이후 DB(또는 Loki 등)로 옮겨 저장하는 작업이 필요하다.
- 화면: 최신순 **10개씩**, ◀ ▶ 로 넘김. 서버 API `limit`/`offset`(`/api/v1/resources/events?limit=10&offset=10`).

---

## 10. 확정 사항

| 항목 | 결정 |
|---|---|
| 추가 기능 | **이벤트 조회 + 파드 로그 보기** 포함 (스케일·dry-run은 제외) |
| 보호 라벨 | **도입** — `platform.mentor/protected: "true"` 삭제 시 403 |
| 서버 confirm | **삭제 + 재시작 모두** `confirm=true` 필수 (없으면 400) |
| 클러스터 검증 | `mentor`에 임시 Deployment **`w2-test`**(nginx, replicas 2)로 진행 후 삭제. `toxicity-filter`, `mlflow-data`는 건드리지 않음 |

## 11. 파드 배포 시 필요한 권한 (README 갱신 예정)

```yaml
rules:
  - apiGroups: [""]
    resources: [pods, services, persistentvolumeclaims, events]
    verbs: [get, list, watch, delete]        # events는 get/list/watch만
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
