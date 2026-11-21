# Work 3 진행 계획 (구현 전 제출)

> 대상: `work_3.md` — **매니페스트 관리**: NFS `/home/suhan/manifests` 의 YAML을 선택 → 수정 / 저장 / 삭제 / apply, 그리고 **판정**(선언 ↔ 실제 일치 여부)
> 기준점: Work 2 완료 (조회 + 삭제/재시작 + 이벤트/로그)
> 배경: `base.md` — "인프라를 코드(YAML)로 정의하고, 선언한 상태를 지속 유지한다"의 **쓰기 + 판정** 단계

---

## 1. 확정 사항 (질의 결과)

| 항목 | 결정 |
|---|---|
| 매니페스트 위치 | NFS 서버 **192.168.2.40** 의 `/home/suhan/manifests` (현재 export 안 됨 → export 추가 필요, 5장) |
| 개발 방식 | 서버는 `MANIFEST_DIR` 경로만 읽고 쓴다. **개발은 PC의 로컬 폴더**로, **NFS 직접 수정이 파드에 바로 보이는지는 테스트 파드로 따로 검증** |
| 파일 삭제 | **파일 + 클러스터 리소스** (확인창에서 함께 삭제할지 선택) |
| apply 범위 | **mentor 네임스페이스만**. 다른 네임스페이스 / 클러스터 범위 리소스(Namespace, PV, ClusterRole…)는 거부 |
| apply 기준 | **저장된 파일만 apply** — 저장 안 한 변경이 있으면 [적용] 비활성 → 항상 "NFS 파일 = 적용된 선언" |
| 외부 수정 충돌 | 편집 중 파일이 밖에서 바뀌면 **저장 거부 + 알림** ("다시 불러오기 / 강제 덮어쓰기" 선택) |
| 판정 | 파일별 **상태 배지 + 달라진 필드 diff** |
| 파일 범위 | `.yaml` + `.yml`, **하위 폴더 포함**, **한 파일 여러 리소스(`---`)**, **새 파일 만들기** |

---

## 2. "PV/PVC로 마운트하면 외부 수정이 반영되나?" 검토

**결론: 반영됩니다. NFS 볼륨은 복사본이 아니라 네트워크 파일시스템을 그대로 연결(mount)한 것**이라, NFS 서버에서 직접 고친 파일을 파드가 같은 파일로 봅니다.
PV/PVC는 "어느 NFS 경로를 쓸지"를 적어둔 선언일 뿐이고, 실제 연결은 파드가 뜰 때 kubelet이 워커 노드에서 자동으로 합니다. (워커 노드에 사람이 따로 mount 할 필요 없음 — 이미 다른 PV들이 이렇게 동작 중)

| 방식 | 외부 수정 반영 | 비고 |
|---|---|---|
| **NFS PV/PVC (채택)** | ✅ 즉시~수 초 | `mountOptions` 지정 가능 → 캐시 지연 줄일 수 있음 |
| 파드 spec에 nfs 볼륨 직접 기입 | ✅ 동일 | mountOptions 지정 불가 |
| ConfigMap | ❌ | 클러스터에 복사본을 만드는 방식 → NFS 수정이 반영 안 됨. 부적합 |

주의할 점 2가지 (검증 항목에 포함):
1. **NFS 속성 캐시** — 기본값으로 클라이언트가 파일 정보를 3~60초 캐시해서 외부 수정이 **조금 늦게** 보일 수 있음 → PV `mountOptions: [actimeo=1]` 등으로 줄인다. 실제 지연 시간을 측정.
2. **파일 변경 알림(inotify)이 NFS에선 동작 안 함** → 서버는 "변경 감지"가 아니라 **요청할 때마다 다시 읽는** 방식으로 만든다. (화면은 목록을 주기적으로 갱신)

---

## 3. 구조

```
[NFS 192.168.2.40:/home/suhan/manifests]
        │  (나중: PV/PVC로 API 서버 파드에 /manifests 로 마운트)
        │  (지금: 개발은 PC 로컬 폴더, MANIFEST_DIR 로 지정)
        ▼
 API 서버 ── 파일 API (목록/읽기/저장/생성/삭제)
          └─ 매니페스트 API (판정 / apply / 클러스터 리소스 삭제) ──▶ kube-apiserver
```

### 3-1. 파일 API

| Method | Path | 설명 |
|---|---|---|
| GET | `/api/v1/manifests` | 파일 트리 (하위 폴더, .yaml/.yml) + 파일별 판정 요약 |
| GET | `/api/v1/manifests/file?path=app/web.yaml` | 내용 + `etag`(내용 해시) |
| PUT | `/api/v1/manifests/file?path=…` | 저장. `If-Match: <etag>` 가 현재 파일과 다르면 **409** (외부에서 바뀜). `force=true` 면 덮어쓰기 |
| POST | `/api/v1/manifests/file?path=…` | 새 파일 생성 (이미 있으면 409) |
| DELETE | `/api/v1/manifests/file?path=…&confirm=true&with_resources=true` | 파일 삭제 (+ 선택 시 그 파일이 선언한 클러스터 리소스 삭제) |

- **경로 안전장치**: `..`, 절대경로, 심볼릭 링크로 `MANIFEST_DIR` 밖을 가리키는 경로는 거부.
- **저장 전 검증**: YAML 문법 오류면 저장 거부(줄 번호 표시). 각 문서에 `apiVersion / kind / metadata.name` 필수.
- **안전한 쓰기**: 임시 파일에 쓴 뒤 교체(rename) → 쓰다 끊겨도 반쪽짜리 파일이 남지 않음.

### 3-2. 매니페스트 API (쓰기 + 판정)

| Method | Path | 설명 |
|---|---|---|
| GET | `/api/v1/manifests/status?path=…` | 판정 + 리소스별 diff |
| POST | `/api/v1/manifests/apply?path=…&confirm=true` | **디스크에 저장된 파일**을 apply |
| GET | `/api/v1/manifests/orphans` | 파일에는 없는데 이 플랫폼이 apply 했던 리소스 ("선언 없음") |

**apply 방식: Server-Side Apply** (`kubectl apply --server-side` 와 동일)
- 필드 관리자 이름 `mentor-platform` 으로 적용 → 누가 어떤 필드를 선언했는지 쿠버네티스가 기록.
- 어떤 kind든 처리하도록 dynamic client 사용 (ConfigMap, Secret, Ingress 등 Work 1의 6종 외도 가능).
- **전체 검증 후 적용**: 파일 안 모든 리소스를 먼저 dry-run → 하나라도 실패하면 아무것도 적용하지 않음.
- 거부 조건: `metadata.namespace` 가 mentor가 아님 / 클러스터 범위 kind. (`namespace` 생략 시 mentor로 채움)

**판정 방식** (리소스 단위 → 파일 단위로 합침)

| 상태 | 조건 |
|---|---|
| ✅ 일치 | 파일 내용을 dry-run apply 한 결과 == 현재 클러스터 상태 |
| ⚠ 불일치 | 다름 → **어느 필드가 어떻게 다른지 diff** (예: `spec.replicas: 파일 3 / 클러스터 1`) |
| ○ 미적용 | 클러스터에 해당 리소스가 없음 |
| ✕ 오류 | YAML 오류, 허용 범위 밖(다른 네임스페이스 등), dry-run 실패 |

- "dry-run 결과와 현재 상태를 비교"하므로 쿠버네티스가 자동으로 채우는 **기본값 차이는 불일치로 잡히지 않음** (kubectl diff 와 같은 원리).
- 비교 제외: `status`, `metadata.managedFields / resourceVersion / generation / uid / creationTimestamp`, 그리고 Work 2의 `kubectl.kubernetes.io/restartedAt` 어노테이션.
- 다른 사람이 `kubectl edit` / `kubectl scale` 로 바꾼 것도 불일치로 잡힘 → "선언에서 벗어난 상태"를 화면에서 확인하고 [적용]으로 되돌림. (base.md 핵심 시연)

### 3-3. 화면 (좌측 메뉴 "매니페스트 관리")

```
┌────────┬──────────────────┬─────────────────────────────────────────────┐
│댓글 기능│ manifests/  [+새]│ app/web.yaml   ● 저장 안 됨      ⚠ 불일치    │
│피드백   │ ▾ app           │ [저장] [적용(비활성: 저장 먼저)] [삭제] [↻]   │
│인프라   │   web.yaml   ⚠  │ ┌──────────────────────────────────────────┐ │
│▶매니페스트│   svc.yaml   ✅ │ │ 1 apiVersion: apps/v1                     │ │
│  관리   │ ▾ db            │ │ 2 kind: Deployment   (편집기, 줄 번호)     │ │
│        │   mariadb.yml ○ │ └──────────────────────────────────────────┘ │
│        │ 선언 없음 (1)    │ 판정: Deployment/web  spec.replicas 3 ≠ 1   │
└────────┴──────────────────┴─────────────────────────────────────────────┘
```
- 두 번째 메뉴바 = 파일 트리 + 파일별 판정 배지. 주기적으로 다시 읽어 외부에서 추가된 파일도 표시.
- 편집기: 빌드 도구 없이 쓸 수 있는 **CodeMirror 5** (YAML 색상·줄 번호)를 `vendor/`에 포함 (js-yaml과 같은 방식).
- 저장 안 한 변경이 있으면 [적용] 비활성 + 페이지 이동 시 경고.
- [적용]·[삭제]는 Work 2와 같은 예/아니오 확인창 (적용 전 diff 미리보기 표시).

---

## 4. 진행 순서

1. **NFS 준비 (사용자 작업, 5장)** + 테스트 파드로 **외부 수정 반영 검증** — 이게 막히면 설계가 바뀌므로 가장 먼저
2. 파일 API (로컬 폴더) + 경로 안전장치 + etag 충돌 + 테스트
3. 판정 (SSA dry-run + diff) + 테스트
4. apply / 파일+리소스 삭제 / 선언 없음 목록 + 테스트
5. 프론트: 메뉴 → 파일 트리 → 편집기 → 저장/적용/삭제 → 판정·diff
6. 실제 클러스터 시연: 테스트용 YAML 작성 → apply → `kubectl scale` 로 일부러 어긋나게 → 불일치 + diff 표시 → [적용]으로 복구 → 정리

---

## 5. 사용자 작업: NFS export 추가 (192.168.2.40)

> 현재 클러스터 PV들은 `/srv/nfs/share/*` 를 쓰고 있고 `/home/suhan/manifests` 는 export 되어 있지 않음.
> 워커 노드에 직접 mount 할 필요는 없음 — export만 되어 있으면 파드가 뜰 때 kubelet이 자동으로 연결.

```bash
# NFS 서버(192.168.2.40)에서
id suhan                                   # uid/gid 확인 (예: 1000/1000) → 파드를 이 uid로 실행해야 쓰기 가능
ls -ld /home/suhan /home/suhan/manifests

# /etc/exports 에 추가 (클러스터 대역만 허용, root는 nobody로 매핑)
/home/suhan/manifests 192.168.2.0/24(rw,sync,no_subtree_check,root_squash)

sudo exportfs -ra && sudo exportfs -v | grep manifests
getsebool nfs_export_all_rw                # Rocky(SELinux): on 이어야 홈 디렉터리 export 쓰기 가능
```

**확인 부탁**: 위 `id suhan` 결과와 `exportfs -v` 결과를 알려주시면 테스트 파드 매니페스트(uid 맞춤 + `actimeo=1`)를 만들어 반영 지연 시간을 측정하겠습니다.

## 6. 파드 배포 시 추가될 권한 (README 반영 예정)

- apply(SSA) = `patch`, 새 리소스 = `create`, 판정 = `get` → 매니페스트로 다루는 kind 마다 `get, list, create, patch, delete`
- 허용할 kind 목록을 Role에 명시 (예: Deployment, Service, ConfigMap, Secret, Job, PVC, StatefulSet, Ingress)
