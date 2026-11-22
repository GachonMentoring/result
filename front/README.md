# Front (Front 1)

빌드 도구 없는 HTML + Vanilla JS(ES Module) 프론트엔드.
좌측 메뉴: **댓글 기능** / **피드백** / **인프라 관리** / **매니페스트 관리**.

## 실행

ES Module은 `file://`로 열면 동작하지 않으므로 정적 서버로 띄웁니다.

```powershell
# 1) API 서버 (server/)
cd server; $env:WATCH_NAMESPACE="mentor"; .venv\Scripts\python -m uvicorn app.main:app --port 8000
# 2) 프론트
cd front; python dev_server.py        # 5500, 브라우저 캐시 끔 (python -m http.server 는 예전 JS 가 남아 버그처럼 보일 수 있음)
# 3) 브라우저: http://localhost:5500
```

서버는 기본으로 `http://localhost:5500`, `http://127.0.0.1:5500` 만 CORS 허용합니다.
다른 주소로 띄우면 서버 실행 시 `CORS_ORIGINS="http://주소:포트"` 를 지정하세요.

## 설정 — `config.js`

| 이름 | 기본값 | 설명 |
|---|---|---|
| `API_BASE` | `http://localhost:8000` | API 서버 주소 (클러스터 배포 시 여기만 변경) |
| `POLL_MS` | `2000` | 갱신 주기 |
| `REQUEST_TIMEOUT_MS` | `5000` | 요청 시간 초과 |

## 인프라 관리 화면

- **개요 카드 = 탭**: kind별 개수, 클릭하면 해당 목록. `비정상 파드` 카드는 Running·Ready가 아니거나 오류 상태인 파드 수 (있으면 빨간색)
- **목록 테이블**: kubectl get 과 비슷한 컬럼, 상태 배지(초록/노랑/빨강), label selector 필터 (`app=x,tier!=db`)
- **상세 패널**: 행 클릭(또는 Enter) → 요약 + 라벨 + manifest YAML. `managedFields` 기본 숨김, YAML 복사. Esc로 닫기
- **갱신**: 2초마다 지금 보는 것만 조회. 브라우저 탭이 숨겨지면 멈춤. 서버가 끊기면 빨간 배너 + 마지막 데이터 유지, 복구되면 자동 해제
- **조작 (Work 2)**: 상세 패널 헤더의 [⟳ 재시작](Deployment/StatefulSet) · [삭제]
  → "정말 삭제하시겠습니까?" 확인창(예/아니오, Esc·바깥 클릭 = 아니오). 확인창에 **선언(매니페스트) 상태에 주는 영향**
  (컨트롤러가 복구 / 선언과 불일치 / spec 변경 없이 파드 교체)을 서버가 계산해 표시. 결과는 우측 하단 알림
- **이벤트**: 최신순 10개씩, ◀ ▶ 로 페이지 넘김 (Events 탭, 상세 패널 이벤트 탭 모두). 크기는 `config.js`의 `EVENTS_PAGE_SIZE`
- **상세 탭**: 개요 / 이벤트(타임라인, Warning 강조) / 로그(Pod, 최근 200줄 자동 갱신, 컨테이너 선택, 이전 컨테이너)
- URL: `#/infra/pods`, `#/infra/services` … (새로고침해도 탭 유지)

## 댓글 기능 화면 (Work 4)

- 댓글 입력(300자, Ctrl+Enter) → 플랫폼 서버 → 모델 서버 → 결과: 정상/주의, 유해 확률 막대 + 임계값 선, 0.4~0.6 경계 안내
- 예시 문장 5개 (모델이 틀리는 "안녕하세요" 포함), 이번 화면 기록 20개 (새로고침 시 사라짐 — DB 저장은 Work 5)
- 상단: 모델 이름·버전·임계값, 연결 상태 (10초마다). 끊기면 배너, 복구되면 자동 해제

## 피드백 화면 (Work 5)

- 댓글 기능 결과 카드: DB 저장 상태 + "이 판정이 맞나요? [맞아요] [틀려요]" → 사람 정답 저장 (한 번만)
- 피드백 메뉴: 요약 카드 = 필터 (전체 / 정답 미확인 / 정답 있음 / 모델이 틀림), 표 + 최신순 10개씩 ◀ ▶, 미확인 행에서 [정상] [주의]로 정답 입력, 5초마다 갱신
- DB 가 설정되지 않았거나 끊기면 배너로 이유 표시 (분류 자체는 계속 동작)

## 매니페스트 관리 화면 (Work 3)

- 두 번째 메뉴: 파일 트리(폴더 접기, 판정 점 색상) + **선언 없음** 목록(삭제 가능), [+ 새 파일]
- 편집기: 줄 번호, Tab=공백 2칸, Enter=들여쓰기 유지, **Ctrl+S 저장**, YAML 오류 줄 빨간 표시
- [저장] → [적용]: 적용은 **저장된 파일** 기준 (저장 안 하면 비활성). 적용 전 확인창에 생성/변경/변경 없음 + diff + 소유권 충돌
- 판정 패널: 리소스별 일치/불일치/미적용/오류 + 필드 diff (파일 ↔ 클러스터), 5초마다 갱신
- 편집 중 외부(NFS)에서 파일이 바뀌면 경고 → 저장 시 [다시 불러오기 / 강제 덮어쓰기]. 편집 중이 아니면 자동으로 다시 불러옴
- 삭제: 기본 "클러스터 리소스도 함께 삭제" (끄면 파일만 → 선언 없음)
- URL: `#/manifests/폴더/파일.yaml`. 저장 안 한 변경이 있으면 이동 전에 확인

## 구조

```
front/
├─ index.html          레이아웃 + 사이드바
├─ config.js           API_BASE, POLL_MS
├─ css/style.css       라이트/다크 자동
├─ js/
│  ├─ main.js          해시 라우터
│  ├─ api.js           fetch 래퍼 (시간 초과, 오류 구분)
│  ├─ poller.js        주기 실행 (겹침 방지, 탭 숨김 시 정지)
│  ├─ util.js          html`` 템플릿(자동 이스케이프), age 표기
│  ├─ pages/           infra / manifests / comments / feedback
│  ├─ manifests/       editor(줄 번호 편집기), judge(판정·diff 표시)
│  ├─ toast.js         조작 결과 알림
│  └─ infra/           columns(컬럼), status(배지 판정), detail(상세 패널·조작·탭), confirm(예/아니오 모달), pager(◀ ▶ 페이지)
└─ vendor/js-yaml.js   js-yaml 4.1.0 (MIT, ESM 빌드) — 오프라인 동작용
```

## 개발 규칙

- HTML은 반드시 `html\`\`` 템플릿으로 만든다. 끼워 넣는 값은 자동 이스케이프되므로 리소스 이름·라벨이 HTML로 해석되지 않는다.
- 새 kind 추가: 서버 `registry.py` + 프론트 `infra/columns.js`, `infra/status.js` 에 항목 추가.
