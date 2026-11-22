# 화면(정적 파일) 이미지
#   docker build -t suhannugul/mentor:platform-web_v1 .
#
# 빌드 단계 없음 (HTML + Vanilla JS). root 가 아닌 nginx 로 8080 포트 사용.
FROM nginxinc/nginx-unprivileged:1.27-alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY index.html config.js /usr/share/nginx/html/
COPY css/ /usr/share/nginx/html/css/
COPY js/ /usr/share/nginx/html/js/
COPY vendor/ /usr/share/nginx/html/vendor/

EXPOSE 8080
