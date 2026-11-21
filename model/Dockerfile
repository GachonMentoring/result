# 댓글 분류 모델 서버 이미지
#   docker build -t suhannugul/toxicity-model:<태그> .
#
# 1단계에서 학습하고 2단계(서빙)에는 결과물만 복사한다.
# → 학습과 서빙이 같은 Python·scikit-learn 버전으로 고정됨 (가이드 08장 시나리오 3 방지)
# → model.joblib 을 git 에 넣지 않아도 이미지 빌드만으로 재현 가능

# ---------------------------------------------------------------- 1) 학습
FROM python:3.11-slim AS train
WORKDIR /work
COPY requirements.txt requirements-train.txt ./
RUN pip install --no-cache-dir -r requirements-train.txt
COPY train.py ./
COPY data/ data/
ARG MODEL_VERSION=""
RUN MODEL_VERSION="${MODEL_VERSION}" python train.py --out /work/artifacts

# ---------------------------------------------------------------- 2) 서빙
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY serve.py .
COPY --from=train /work/artifacts/ artifacts/

# root 가 아닌 사용자로 실행
RUN useradd --uid 10001 --no-create-home model
USER 10001

ENV MODEL_PATH=/app/artifacts/model.joblib
EXPOSE 8088
CMD ["uvicorn", "serve:app", "--host", "0.0.0.0", "--port", "8088"]
