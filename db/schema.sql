-- =============================================================================
-- Work 5 — 피드백(재학습 데이터) 스키마
--   대상: MariaDB 12.3 (database 네임스페이스 StatefulSet, mariadb-0 = 쓰기)
--   접속: 로컬 192.168.2.30:31306 (mariadb-external-write → mariadb-0)
--         ※ 31307(mariadb-1)은 읽기 복제본 — 여기서 실행하지 말 것
--   실행: admin 계정(서버 전체 권한)으로 31306 에 접속해 이 파일 전체를 한 번 실행
--         (여러 번 실행해도 안전: IF NOT EXISTS)
--         ※ root 는 파드 안(localhost)에서만 로그인 가능 — 원격 root 계정 없음 (의도된 설정)
--   설계: k8sapimd/work_5_plan.md
-- =============================================================================

-- 1) 데이터베이스 ----------------------------------------------------------------
CREATE DATABASE IF NOT EXISTS toxicity
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- 2) 테이블 (하나) ---------------------------------------------------------------
--    분류할 때마다 INSERT (모델 리턴값) → 사람이 확인하면 true_label UPDATE
--    재학습: SELECT text, true_label FROM comment_feedback WHERE true_label IS NOT NULL
CREATE TABLE IF NOT EXISTS toxicity.comment_feedback (
  id            UUID          NOT NULL DEFAULT UUID()               COMMENT '행 ID (UUID v1, 시간순)',
  text          VARCHAR(300)  NOT NULL                              COMMENT '댓글 원문 (최대 300자)',
  pred_label    TINYINT       NOT NULL                              COMMENT '모델 판정 0=정상 1=주의',
  toxic_proba   DOUBLE        NOT NULL                              COMMENT '유해 확률 0~1',
  threshold     DECIMAL(4,3)  NOT NULL                              COMMENT '판정에 쓴 임계값',
  model_name    VARCHAR(100)  NOT NULL                              COMMENT '모델 이름 (예: toxicity-filter)',
  model_version VARCHAR(64)   NOT NULL                              COMMENT '모델 버전 (예: seed-633bdbb4ded6)',
  true_label    TINYINT       NULL                                  COMMENT '사람이 확인한 정답 0/1 (재학습 라벨, 미확인 NULL)',
  labeled_at    DATETIME(3)   NULL                                  COMMENT '정답 입력 시각',
  created_at    DATETIME(3)   NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '저장 시각',

  PRIMARY KEY (id),
  KEY idx_created_at (created_at),                -- 최신순 페이지 조회
  KEY idx_true_label (true_label, created_at),    -- 정답 미확인 / 정답 있음 필터

  CONSTRAINT chk_pred_label CHECK (pred_label IN (0, 1)),
  CONSTRAINT chk_true_label CHECK (true_label IS NULL OR true_label IN (0, 1)),
  CONSTRAINT chk_proba      CHECK (toxic_proba >= 0 AND toxic_proba <= 1),
  CONSTRAINT chk_threshold  CHECK (threshold > 0 AND threshold < 1),
  CONSTRAINT chk_labeled    CHECK ((true_label IS NULL) = (labeled_at IS NULL))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='댓글 분류 결과 + 사람 확인 정답 (재학습 데이터)';

-- 3) 앱 전용 계정 ----------------------------------------------------------------
--    플랫폼 서버는 root 가 아닌 이 계정으로 접속 (필요한 권한만: DELETE·DDL 없음)
--    ⚠ 비밀번호를 이 파일·git 에 남기지 않도록, 아래 두 줄은 주석을 풀지 말고
--      <비밀번호> 를 바꿔서 직접 따로 실행하세요.
--
-- CREATE USER IF NOT EXISTS 'mentor_app'@'%' IDENTIFIED BY '<비밀번호>';
-- GRANT SELECT, INSERT, UPDATE ON toxicity.comment_feedback TO 'mentor_app'@'%';

-- 4) 확인 ------------------------------------------------------------------------
-- SHOW CREATE TABLE toxicity.comment_feedback;
-- SHOW GRANTS FOR 'mentor_app'@'%';
