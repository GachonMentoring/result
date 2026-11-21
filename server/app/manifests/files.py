"""매니페스트 폴더(MANIFEST_DIR) 파일 다루기.

운영: NFS 192.168.2.40:/home/suhan/manifests 를 PV/PVC로 파드에 마운트한 경로
개발: PC 로컬 폴더

NFS 에서는 파일 변경 알림(inotify)이 동작하지 않으므로 캐시 없이 요청할 때마다 디스크를 다시 읽는다.
외부에서 직접 수정하는 일이 잦으므로 저장 시 etag(내용 해시)로 충돌을 감지한다.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

EXTENSIONS = (".yaml", ".yml")
MAX_FILE_BYTES = 1024 * 1024  # 1MB — 매니페스트로는 충분


class FileError(Exception):
    def __init__(self, status: int, message: str, line: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.line = line  # YAML 오류 줄 번호 (1부터)


def etag_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def parse_documents(content: str) -> list[dict[str, Any]]:
    """--- 로 나뉜 여러 문서를 파싱 + 최소 검증. 빈 문서는 건너뛴다."""
    try:
        raw = list(yaml.safe_load_all(content))
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        line = mark.line + 1 if mark else None
        problem = getattr(e, "problem", None) or str(e)
        raise FileError(400, f"YAML 문법 오류{f' ({line}번째 줄)' if line else ''}: {problem}", line) from e

    docs: list[dict[str, Any]] = []
    for i, doc in enumerate(raw, start=1):
        if doc is None:
            continue
        if not isinstance(doc, dict):
            raise FileError(400, f"{i}번째 문서가 객체(key: value) 형태가 아닙니다.")
        missing = [k for k in ("apiVersion", "kind") if not doc.get(k)]
        if not (doc.get("metadata") or {}).get("name"):
            missing.append("metadata.name")
        if missing:
            raise FileError(400, f"{i}번째 문서({doc.get('kind') or '?'})에 {', '.join(missing)} 이(가) 없습니다.")
        docs.append(doc)
    return docs


@dataclass(frozen=True)
class FileInfo:
    path: str      # MANIFEST_DIR 기준 상대 경로, '/' 구분
    size: int
    mtime: float
    etag: str


class ManifestStore:
    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root).resolve()

    # ------------------------------------------------------------------ 경로
    def resolve(self, rel: str, must_exist: bool = True) -> Path:
        """상대 경로 → 실제 경로. 폴더 밖(.., 절대경로, 밖을 가리키는 심볼릭 링크)은 거부."""
        if not rel or rel.strip() != rel:
            raise FileError(400, "파일 경로가 비어 있거나 앞뒤 공백이 있습니다.")
        p = PurePosixPath(rel.replace("\\", "/"))
        # a//b, ./a 처럼 정리되는 경로도 거부 → 한 파일은 항상 한 가지 이름으로만 불린다
        if p.as_posix() != rel.replace("\\", "/"):
            raise FileError(400, f"허용되지 않는 경로 형식입니다: {rel}")
        if p.is_absolute() or any(part in ("..", "") for part in p.parts) or ":" in rel:
            raise FileError(400, f"허용되지 않는 경로입니다: {rel}")
        if any(part.startswith(".") for part in p.parts):
            raise FileError(400, "숨김 파일/폴더(. 으로 시작)는 다룰 수 없습니다.")
        if p.suffix.lower() not in EXTENSIONS:
            raise FileError(400, f"확장자는 {', '.join(EXTENSIONS)} 만 허용됩니다.")
        full = (self.root / Path(*p.parts)).resolve()
        if full != self.root and self.root not in full.parents:
            raise FileError(400, f"매니페스트 폴더 밖을 가리키는 경로입니다: {rel}")
        if must_exist and not full.is_file():
            raise FileError(404, f"파일이 없습니다: {rel}")
        return full

    def _rel(self, full: Path) -> str:
        return full.relative_to(self.root).as_posix()

    # ------------------------------------------------------------------ 조회
    def check_root(self) -> None:
        if not self.root.is_dir():
            raise FileError(503, f"매니페스트 폴더를 찾을 수 없습니다: {self.root} (MANIFEST_DIR 확인)")

    def list(self) -> list[FileInfo]:
        """하위 폴더까지 .yaml/.yml 파일 목록 (숨김 폴더·파일 제외, 경로순)."""
        self.check_root()
        out: list[FileInfo] = []
        for dirpath, dirnames, filenames in os.walk(self.root, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for name in sorted(filenames):
                if name.startswith(".") or not name.lower().endswith(EXTENSIONS):
                    continue
                full = Path(dirpath) / name
                try:
                    # 심볼릭 링크가 폴더 밖을 가리키면 목록에서 제외
                    real = full.resolve()
                    if self.root not in real.parents:
                        continue
                    data = real.read_bytes()
                    st = real.stat()
                except OSError:
                    continue  # 목록을 읽는 사이 지워진 파일
                out.append(FileInfo(self._rel(full), st.st_size, st.st_mtime, etag_of(data)))
        return out

    def read(self, rel: str) -> dict[str, Any]:
        full = self.resolve(rel)
        data = full.read_bytes()
        if len(data) > MAX_FILE_BYTES:
            raise FileError(413, "파일이 너무 큽니다 (1MB 초과).")
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError as e:
            raise FileError(400, "UTF-8 텍스트 파일이 아닙니다.") from e
        return {"path": rel, "content": content, "etag": etag_of(data), "mtime": full.stat().st_mtime}

    def read_documents(self, rel: str) -> list[dict[str, Any]]:
        """디스크에 저장된 내용 기준으로 파싱 (apply·판정은 항상 저장된 파일 기준)."""
        return parse_documents(self.read(rel)["content"])

    # ------------------------------------------------------------------ 쓰기
    def _atomic_write(self, full: Path, content: str) -> bytes:
        data = content.encode("utf-8")
        if len(data) > MAX_FILE_BYTES:
            raise FileError(413, "파일이 너무 큽니다 (1MB 초과).")
        full.parent.mkdir(parents=True, exist_ok=True)
        # 같은 폴더의 임시 파일에 쓴 뒤 교체 → 쓰다가 끊겨도 반쪽짜리 파일이 남지 않음
        fd, tmp = tempfile.mkstemp(prefix=f".{full.name}.", suffix=".tmp", dir=full.parent)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, full)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return data

    def save(self, rel: str, content: str, etag: str | None, force: bool = False) -> dict[str, Any]:
        full = self.resolve(rel)
        parse_documents(content)  # 문법 오류면 저장하지 않음
        current = etag_of(full.read_bytes())
        if not force and etag != current:
            raise FileError(409, "파일을 불러온 뒤 외부에서 내용이 바뀌었습니다. 다시 불러오거나 강제로 덮어쓰세요.")
        data = self._atomic_write(full, content)
        return {"path": rel, "etag": etag_of(data), "mtime": full.stat().st_mtime}

    def create(self, rel: str, content: str) -> dict[str, Any]:
        full = self.resolve(rel, must_exist=False)
        if full.exists():
            raise FileError(409, f"이미 있는 파일입니다: {rel}")
        parse_documents(content)
        data = self._atomic_write(full, content)
        return {"path": rel, "etag": etag_of(data), "mtime": full.stat().st_mtime}

    def delete(self, rel: str) -> None:
        full = self.resolve(rel)
        full.unlink()
