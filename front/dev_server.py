"""개발용 화면 서버 — 브라우저가 JS/CSS 를 캐시하지 않게 한다.

    python dev_server.py            # http://localhost:5500
    python dev_server.py 5600       # 포트 지정

`python -m http.server` 는 캐시 지시(Cache-Control)를 보내지 않아서 브라우저가 JS 파일을
임의로 재사용한다 → 코드를 고쳐도 예전 JS 가 계속 실행되는 문제가 생긴다 (Work 5 중복 저장 버그 때 실제로 겪음).
배포 시에는 nginx 설정으로 처리 (k8sapimd/deploy.md).
"""

import functools
import http.server
import pathlib
import sys


class NoCacheHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store, max-age=0")
        super().end_headers()


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5500
    root = pathlib.Path(__file__).resolve().parent
    handler = functools.partial(NoCacheHandler, directory=str(root))
    with http.server.ThreadingHTTPServer(("", port), handler) as httpd:
        print(f"화면 서버 (캐시 끔): http://localhost:{port}  ← {root}")
        httpd.serve_forever()


if __name__ == "__main__":
    main()
