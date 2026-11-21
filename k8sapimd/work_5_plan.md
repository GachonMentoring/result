# Work 5 진행 계획 — 1단계: ERD · SQL 산출 (구현 전 제출)

> 대상: `work_5.md` — 피드백 기능: 댓글 분류 결과(모델 리턴값)를 재학습용으로 DB 테이블 하나에 저장 → SELECT 로 조회 → 화면에 표 + 페이지 넘김
> 이번 단계: **ERD 와 SQL 산출** → work_5.md 에 산출 완료 표시 → 다음 단계에서 DB에 구축됐다고 가정하고 API·화면 구현

---

## 1. 요구사항 정리

| 구분 | 내용 |
|---|---|
| 저장 | 댓글 분류 결과 중 **재학습에 필요한 값**을 테이블에 한 행씩 INSERT |
| 조회 | SELECT 로 읽어 화면에 **표** 로 표시, 일정 개수 넘으면 **버튼으로 페이지 넘김** |
| DB | 로컬 개발: `192.168.2.30:31306` (NodePort). 아직 DB 없음 → ERD 부터 |
| 테이블 | **하나** |
| 순서 | 1단계 SQL 산출 + work_5.md 에 완료 표시 → 2단계 API·SQL 서비스 구현 |

## 2. DB 현황 (읽기 전용 확인)

| 항목 | 결과 | 설계 영향 |
|---|---|---|
| 버전 | MariaDB **12.3.3** | `UUID` 자료형, `CHECK`, `INSERT … RETURNING` 사용 가능 |
| 구성 | StatefulSet 2대 복제: `mariadb-0` = 쓰기(primary, binlog on), `mariadb-1` = 읽기 복제본 | 쓰기는 반드시 mariadb-0 으로 |
| 31306 (`mariadb-external-write`) | `mariadb-0` 만 가리킴 | ✅ 로컬 개발 접속 주소로 적합 |
| 31307 (`mariadb-external-read`) | `mariadb-1` 만 가리킴 | 복제 지연 시 방금 저장한 행이 안 보일 수 있음 → **조회도 일단 31306 사용** 제안 |
| 클러스터 내부 `mariadb-read` (ClusterIP) | **두 파드 모두** 선택 (`app: mariadb`) | ⚠ 파드 배포 시 쓰기에 쓰면 복제본으로 갈 수 있음 → 쓰기는 `mariadb-0.mariadb-headless.database.svc.cluster.local:3306` |
| 문자셋 | utf8mb4 / utf8mb4_unicode_ci | 한글 문제 없음 |
| DB · 앱 계정 | 없음 (root 만) | 생성 SQL 포함, 앱은 root 가 아닌 전용 계정 사용 |

## 3. 핵심 검토: "재학습에 쓸 수 있는 데이터"가 되려면

모델 리턴값(예측 라벨·확률)**만** 저장하면 재학습에 그대로 쓸 수 없습니다.
모델이 낸 답으로 모델을 다시 가르치면 **틀린 판정("안녕하세요" → 주의)까지 그대로 강화**됩니다.

재학습에 필요한 건 **사람이 확인한 정답(true_label)** 입니다. 가이드는 이를 `predictions` + `feedback` 두 테이블(1:1)로 나눴지만,
예측 하나에 정답이 최대 하나라 **한 테이블에 정답 칸(비어 있을 수 있음)을 두는 것과 같은 구조**입니다 → 요구사항의 "테이블 하나"를 지키면서 재학습 데이터가 됩니다.

```
분류할 때 INSERT ──▶ [text · 예측 · 확률 · 임계값 · 모델 버전 · true_label=NULL]
화면에서 "맞아요/틀려요" ──▶ UPDATE true_label, labeled_at     (2단계 기능)
재학습 Job ──▶ SELECT text, true_label WHERE true_label IS NOT NULL   (이후 Work)
```

## 4. ERD (테이블 1개)

```
┌──────────────────────── toxicity.comment_feedback ────────────────────────┐
│ PK  id             UUID          DB가 생성 (시간 순 정렬되는 UUID v1)        │
│     text           VARCHAR(300)  댓글 원문 (화면·모델 상한 300자와 같음)       │
│     pred_label     TINYINT       모델 판정 0 정상 / 1 주의                   │
│     toxic_proba    DOUBLE        유해 확률 0~1                               │
│     threshold      DECIMAL(4,3)  판정에 쓴 임계값 (앱 서버 THRESHOLD)         │
│     model_name     VARCHAR(100)  예: toxicity-filter                        │
│     model_version  VARCHAR(64)   예: seed-633bdbb4ded6 (어느 모델이 판정했나) │
│     true_label     TINYINT NULL  사람이 확인한 정답 — 재학습 라벨 (미확인 NULL) │
│     labeled_at     DATETIME(3) NULL  정답을 단 시각                           │
│     created_at     DATETIME(3)   저장 시각 (기본값 현재 시각)                  │
├────────────────────────────────────────────────────────────────────────────┤
│ 인덱스  idx_created_at (created_at)            ← 최신순 페이지 조회            │
│        idx_true_label (true_label, created_at) ← 정답 있는 것만 / 미확인만     │
│ 제약    pred_label·true_label ∈ {0,1}, 0 ≤ toxic_proba ≤ 1, 0 < threshold < 1, │
│        true_label 과 labeled_at 은 함께 비거나 함께 채워짐                      │
└────────────────────────────────────────────────────────────────────────────┘
```

- `uncertain`(0.4~0.6)·`label_name` 은 저장값에서 계산 가능 → 컬럼 없음
- 응답 시간·접속 IP 등 재학습과 무관하거나 개인정보에 가까운 값은 저장하지 않음

## 5. SQL 초안 (질의 확정 후 `db/schema.sql` 로 산출)

```sql
-- 실행: 31306(mariadb-0, 쓰기)에 root 로 접속해 한 번 실행
-- 1) 데이터베이스
CREATE DATABASE IF NOT EXISTS toxicity
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- 2) 테이블
CREATE TABLE IF NOT EXISTS toxicity.comment_feedback (
  id            UUID          NOT NULL DEFAULT UUID()          COMMENT '행 ID (UUID v1, 시간순)',
  text          VARCHAR(300)  NOT NULL                         COMMENT '댓글 원문',
  pred_label    TINYINT       NOT NULL                         COMMENT '모델 판정 0=정상 1=주의',
  toxic_proba   DOUBLE        NOT NULL                         COMMENT '유해 확률 0~1',
  threshold     DECIMAL(4,3)  NOT NULL                         COMMENT '판정 임계값',
  model_name    VARCHAR(100)  NOT NULL                         COMMENT '모델 이름',
  model_version VARCHAR(64)   NOT NULL                         COMMENT '모델 버전',
  true_label    TINYINT       NULL                             COMMENT '사람이 확인한 정답 (재학습 라벨)',
  labeled_at    DATETIME(3)   NULL                             COMMENT '정답 입력 시각',
  created_at    DATETIME(3)   NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '저장 시각',
  PRIMARY KEY (id),
  KEY idx_created_at (created_at),
  KEY idx_true_label (true_label, created_at),
  CONSTRAINT chk_pred_label CHECK (pred_label IN (0, 1)),
  CONSTRAINT chk_true_label CHECK (true_label IS NULL OR true_label IN (0, 1)),
  CONSTRAINT chk_proba      CHECK (toxic_proba >= 0 AND toxic_proba <= 1),
  CONSTRAINT chk_threshold  CHECK (threshold > 0 AND threshold < 1),
  CONSTRAINT chk_labeled    CHECK ((true_label IS NULL) = (labeled_at IS NULL))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='댓글 분류 결과 + 사람 확인 정답 (재학습 데이터)';

-- 3) 앱 전용 계정 — 필요한 권한만 (DELETE·DDL 없음)
--    비밀번호는 실행할 때 직접 넣고, 이 파일·git 에는 남기지 않는다
CREATE USER IF NOT EXISTS 'mentor_app'@'%' IDENTIFIED BY '<비밀번호>';
GRANT SELECT, INSERT, UPDATE ON toxicity.comment_feedback TO 'mentor_app'@'%';
```

### 2단계에서 쓸 SQL (미리보기)
```sql
-- 저장 (분류할 때) — 생성된 id 를 바로 돌려받음
INSERT INTO comment_feedback (text, pred_label, toxic_proba, threshold, model_name, model_version)
VALUES (%s, %s, %s, %s, %s, %s) RETURNING id, created_at;

-- 표 조회 (최신순, 페이지)
SELECT id, text, pred_label, toxic_proba, threshold, model_version, true_label, created_at
FROM comment_feedback ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s;
SELECT COUNT(*) FROM comment_feedback;

-- 정답 입력 (맞아요/틀려요) — 이미 정답이 있으면 0행 갱신 → 409
UPDATE comment_feedback SET true_label = %s, labeled_at = CURRENT_TIMESTAMP(3)
WHERE id = %s AND true_label IS NULL;
```

## 6. 확정 사항 (질의 결과)

| 항목 | 결정 |
|---|---|
| 정답 칸 | **`true_label` 포함** (미확인 NULL) → 2단계에서 서버 정답 입력 API + 화면 "맞아요/틀려요" 추가 |
| 저장 시점 | **분류할 때마다 자동 INSERT** (`/api/v1/comments/predict` 안에서) |
| 이름 | DB `toxicity` · 테이블 `comment_feedback` · 앱 계정 `mentor_app` (SELECT·INSERT·UPDATE 만) |
| 표 | **전체 + 필터** (전체 / 정답 미확인 / 정답 있음 / 모델이 틀린 것), 최신순 10개씩 ◀ ▶, 상단 건수 요약 |

## 7. 1단계 산출 결과

- SQL: **`db/schema.sql`** — DB 생성 + 테이블 생성 (+ 앱 계정 생성·권한은 비밀번호 때문에 주석으로 두고 직접 실행)
- 실제 DB 실행 검증: **안 함** (root 비밀번호 필요 — 사용자가 실행)
- 실행 방법 (둘 중 하나)
  - DBeaver 등으로 `192.168.2.30:31306` 에 root 접속 → `db/schema.sql` 전체 실행 → `CREATE USER`·`GRANT` 두 줄은 비밀번호를 바꿔 따로 실행
  - `kubectl exec -it -n database mariadb-0 -- mariadb -u root -p` 로 접속 후 같은 SQL 붙여 넣기
- 확인: `SHOW CREATE TABLE toxicity.comment_feedback;`, `SHOW GRANTS FOR 'mentor_app'@'%';`

### DB 접속 문제 해결 (2026-11-21)
- 증상: HeidiSQL 로 `root` 원격 접속 시 `Access denied for user 'root'@'192.168.2.30'`
- 원인: root 계정이 `localhost` / `127.0.0.1` / `::1` 에만 있음 — **원격용 `root@%` 없음** (비밀번호는 시크릿과 일치, 줄바꿈 문제 아님)
- 참고: NodePort 를 거치면 DB 에는 모든 접속이 노드 IP(192.168.2.30)로 보여, 접속 주소로 계정을 제한할 수 없음
- 조치 (사용자 결정): 기존 `admin@%` 에 **서버 전체 권한** 부여
  `GRANT ALL PRIVILEGES ON *.* TO 'admin'@'%' WITH GRANT OPTION;` — mariadb-0 실행, mariadb-1 복제 반영 확인
- 이후: `db/schema.sql` 실행과 `mentor_app` 생성은 HeidiSQL 에서 admin 으로 진행. 앱(플랫폼 서버)은 `mentor_app` 으로 접속 (권한 최소화)
- 주의: admin 은 email_agent·aidoc 앱도 쓰는 계정 → admin 비밀번호 관리 강화 필요

## 8. 2단계 (다음 시행) 구현 범위

| 위치 | 내용 |
|---|---|
| server/ | DB 연결 설정 (`DB_HOST=192.168.2.30`, `DB_PORT=31306`, `DB_NAME=toxicity`, `DB_USER=mentor_app`, `DB_PASSWORD` — 환경변수, 코드·git 에 비밀번호 없음) |
| server/ | `/api/v1/comments/predict` → 분류 후 INSERT, 응답에 `id` 포함 (DB 장애 시 처리 방침은 2단계 검토) |
| server/ | `GET /api/v1/feedback?filter=&limit=10&offset=` (표 + 건수 요약), `POST /api/v1/feedback/{id}/label {true_label}` (이미 있으면 409) |
| front/ | 댓글 기능 결과 카드에 "맞아요 / 틀려요", **피드백 메뉴**에 표 + 필터 + ◀ ▶ + 행별 정답 입력 |

---

## 9. 2단계 구현 결과

### 사전 확인 (실제 DB)
- 테이블 `toxicity.comment_feedback` 생성 확인 (사용자가 admin 으로 실행) — 설계와 동일, 0행
- DB 시간대 UTC → API 는 시각에 `Z` 를 붙여 반환, 화면이 현지 시각으로 표시
- 2단계 SQL 을 **실제 DB 에서 트랜잭션 + ROLLBACK 으로 검증** (데이터 남지 않음):
  `INSERT … RETURNING id, created_at` ✅ / 정답 UPDATE 1회만 (`WHERE true_label IS NULL`, 두 번째 0행) ✅ /
  건수 요약·"모델이 틀림" 필터 ✅ / 한글 ✅ / CHECK 제약 위반 거부 ✅
- `mentor_app` 계정은 **아직 없음** → 사용자가 생성 필요 (아래)

### server/
| 파일 | 내용 |
|---|---|
| `app/feedback/repo.py` | PyMySQL, 요청마다 연결(3초 연결·5초 읽기 제한). insert(RETURNING) / set_label(한 번만) / list(필터·페이지·요약). 필터는 고정 목록만 허용(SQL 에 사용자 입력 불포함), 값은 모두 %s 바인딩 |
| `app/api/feedback.py` | `GET /api/v1/feedback?filter=all\|unlabeled\|labeled\|wrong&limit=10&offset=0`, `POST /api/v1/feedback/{id}/label {true_label: 0\|1}` (이미 있으면 409, 없으면 404, ID 형식 오류 400) |
| `app/api/comments.py` | 분류 후 자동 INSERT → 응답에 `id, saved, save_error, created_at`. **DB 장애 시에도 분류 결과는 반환** (`saved=false` + 이유) |
| 환경변수 | `DB_HOST`(192.168.2.30) `DB_PORT`(31306) `DB_NAME`(toxicity) `DB_USER`(mentor_app) `DB_PASSWORD`(필수, 없으면 피드백 꺼짐) |
| 오류 안내 | 로그인 실패(1045)·권한 없음(1142)·테이블 없음(1146)·연결 실패를 구분해 503 메시지 |
| 테스트 | 7개 추가 → 서버 전체 77개 통과. `tests/conftest.py` 가 `DB_PASSWORD` 를 지워 테스트가 실제 DB 에 붙지 않게 함 |

### front/
- 댓글 기능: 결과 카드에 저장 상태 + **"이 판정이 맞나요? [맞아요] [틀려요]"** → 정답 저장(한 번만). 저장 실패 시 이유 표시. 기록 목록에 ✓ 맞음 / ✕ 틀림 / 미저장
- **피드백 메뉴** (자리표시 → 구현): 요약 카드 = 필터 (전체 / 정답 미확인 / 정답 있음 / 모델이 틀림), 표 (시각·댓글·모델 판정·유해 확률·사람 정답·결과·모델 버전), 최신순 10개씩 ◀ ▶, 미확인 행은 [정상] [주의] 버튼으로 정답 입력, 5초마다 갱신

### 확인한 것 / 못 한 것
- ✅ DB 미설정 상태: 분류는 동작 + "저장하지 않았습니다" 안내, 피드백 화면은 503 배너
- ✅ **실제 DB 연동 (2026-11-21, 사용자가 DB_PASSWORD 를 넣어 서버 실행)** — `mentor_app` 권한: `SELECT, INSERT, UPDATE ON toxicity.comment_feedback` 확인
  - 댓글 기능: "안녕하세요" 분류 → DB 저장됨 → [틀려요] → 정답 정상(0) 저장, "모델이 틀렸습니다" 표시
  - 피드백 표: "말이 너무 심하네요" 행에서 [주의] → ✓ 맞음, 요약 2 / 0 / 2 / 1, "모델이 틀림" 필터 → "안녕하세요" 1건
  - 시각: DB UTC 14:55 → 화면 23:55 (KST) 표시
  - DB 직접 조회: 2행 값 일치 (확률 0.685750 / 0.840195, 모델 seed-633bdbb4ded6, 정답 0 / 1), 복제본 mariadb-1 에도 2행 반영
  - 콘솔 오류 없음
  - 저장된 2행은 가이드 예시 문장 + 올바른 정답이라 재학습 데이터로 그대로 사용 가능 (삭제 안 함)
- ⏸ 페이지 넘김(◀ ▶)은 10건 초과 데이터가 없어 화면 미확인 — 테스트(15건)로만 검증, 실제 데이터가 쌓이면 확인

