## 피드백 기능의 구현

우리가 댓글을 댓글 분류기능에서 input하면 결과를 현재 model이 리턴하고 있다.
피드백 기능이란 이 리턴값을 재학습에 쓰이기 위해 DB에 필요한 인자들을 DB테이블에 로우로 쿼리하고
그 후에 그걸 select 으로 조회해서 화면에 표형식으로 시각화 하면 된다.
특정 갯수이상의 데이터가 넘어가면 버튼을 통해서 넘길수 있을 정도로 하면 될듯하다.

DB의 경우 , 로컬에서는 192.168.2.30 31306 노드포트를 통해서 사용할수 있고 
아직 DB를 생성하지 않앗으니 ERD를 구축하는 것이 가장 먼저가 된다.
필요한 테이블은 단 하나로, 재학습을 위한 리턴값을 저장할 수 있는 테이블 하나면 된다.

먼저 md를 파악하고 sql문을 산출해준다. 산출 후에는 해당 md에 산출완료 의 인증을 남기고 다음 번 시행 때
해당 ERD가 DB에 구축되었다고 가정하고 api 및 sql을 통한 서비스 기능을 구현하는 것을 목표로한다.

---

### ✅ 1단계 산출 완료 — ERD · SQL (2026-11-21)

- SQL 파일: `db/schema.sql` / 설계·검토: `k8sapimd/work_5_plan.md`
- 테이블 1개: **`toxicity.comment_feedback`** — 분류 결과(text, pred_label, toxic_proba, threshold, model_name, model_version) + 사람 확인 정답(true_label, labeled_at) + created_at
- 앱 계정: `mentor_app` (SELECT · INSERT · UPDATE), 비밀번호는 실행자가 직접 지정
- 접속: 로컬 `192.168.2.30:31306` (mariadb-0, 쓰기). 31307 은 읽기 복제본
- **다음 시행 전제**: 위 SQL 이 DB 에 구축되었다고 가정하고 API · SQL 서비스 · 화면을 구현한다 (work_5_plan.md 8장)

