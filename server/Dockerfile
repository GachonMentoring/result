# 플랫폼 API 서버 이미지
#   docker build -t suhannugul/mentor:platform-api_v1 .
#
# 설정은 모두 환경변수 (ConfigMap·Secret) — k8sapimd/deploy.md 3장
# 이미지에는 비밀번호·kubeconfig 를 넣지 않는다 (파드 안에서는 ServiceAccount 로 쿠버네티스 접속)
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ app/

# uid/gid 1000 = NFS(/home/suhan/manifests, root_squash) 파일 소유자 suhan 과 같게
RUN groupadd --gid 1000 app && useradd --uid 1000 --gid 1000 --no-create-home app
USER 1000:1000

EXPOSE 8000
# Watch 캐시가 프로세스 메모리에 있으므로 worker 1 개 (deploy.md 8-2 H)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
