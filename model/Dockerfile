# 댓글 분류 모델 — 이미지 두 개를 같은 파일·같은 고정 버전(requirements.txt)으로 빌드한다
#   서빙 (기본 target):  docker build -t suhannugul/mentor:model_v2 .
#   재학습 Job:          docker build --target job-train -t suhannugul/mentor:job-train_v1 .
#
# 두 이미지가 같은 requirements.txt (scikit-learn·numpy·scipy 고정)와 같은 Python 3.11 을 쓰므로
# Job 이 학습한 모델을 서빙이 그대로 로드할 수 있다 (가이드 08장 시나리오 3 방지, k8sapimd/train.md R8).
#
# model_v2 부터 모델은 이미지에 넣지 않는다.
#   서빙 파드의 initContainer(= job-train 이미지)가 MLflow 에서 models:/toxicity-filter/<N> 을 받아
#   /models 에 두고, 서빙 컨테이너는 /models/model.pkl 을 로드한다 (k8sapimd/train.md 7-1).

# ---------------------------------------------------------------- 재학습 Job · initContainer
FROM python:3.11-slim AS job-train
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    GIT_PYTHON_REFRESH=quiet \
    MLFLOW_DISABLE_AGENT_HINT=1
WORKDIR /job
COPY requirements.txt requirements-train.txt requirements-job.txt ./
RUN pip install --no-cache-dir -r requirements-job.txt
COPY train.py train_job.py make_eval.py ./
# uid 1000 = NFS(/home/suhan/csv) 소유자. 서빙 initContainer 로 쓸 때는 파드의 runAsUser(10001)로 실행됨
RUN useradd --uid 1000 --no-create-home job
USER 1000
CMD ["python", "train_job.py"]

# ---------------------------------------------------------------- 서빙 (기본)
FROM python:3.11-slim AS serve
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY serve.py .

# root 가 아닌 사용자로 실행
RUN useradd --uid 10001 --no-create-home model
USER 10001

ENV MODEL_PATH=/models/model.pkl
EXPOSE 8088
CMD ["uvicorn", "serve:app", "--host", "0.0.0.0", "--port", "8088"]
