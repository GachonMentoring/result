### 첫번째 Work 1
언어 : Python / FastAPI
K8s API 서버 먼저 개발
배포된 인프라 클러스터의 리소스 상태를 보는 기능 우선 완성
k8s API 를 사용해야하고 나중에 말아서 파드로 배포시 service account는 추후 만들 예정
해당 md에서 수행해야할 것은 서버에서 Get 요청을 통해 현재 리소스 목록을 우선적으로 반환할 수 있는 python api 서버를 개발 하는 것.

Watch 루프 + 메모리 캐시: 리소스 종류마다 스레드 하나를 띄우고, 410 Gone을 받으면 re-list. 해당 부분 검토