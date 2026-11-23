# 데이터셋 재생성 Job 이미지 (k8sapimd/dataset.md)
#   docker build -t suhannugul/mentor:job-dataset_v1 .
#
# seed.csv(NFS) + DB 피드백 → NFS 에 dataset-<날짜태그>.csv
# 이미지에는 비밀번호·데이터를 넣지 않는다 (DB 계정은 Secret, 데이터는 NFS)
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /job
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY export.py .

# uid/gid 1000 = NFS(/home/suhan/csv, root_squash) 폴더 소유자와 같게
RUN groupadd --gid 1000 job && useradd --uid 1000 --gid 1000 --no-create-home job
USER 1000:1000

CMD ["python", "export.py"]
