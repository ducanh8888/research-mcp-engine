"""Content-addressed disk blobs; SQLite stores references and metadata."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import time
from pathlib import Path
from threading import RLock

_BLOB_REF = re.compile(r"^[0-9a-f]{64}$")


class BlobStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        # Shared by cache writes and maintenance within this process. The age
        # grace also protects an interrupted put before its DB ref is committed.
        self.lock = RLock()

    def path(self, ref: str) -> Path:
        if not _BLOB_REF.fullmatch(ref):
            raise ValueError("Invalid blob reference")
        path = (self.root / ref[:2] / ref).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Blob reference escapes the data directory")
        return path

    def put(self, value: bytes | str) -> str:
        data = value.encode("utf-8") if isinstance(value, str) else value
        if not isinstance(data, bytes):
            raise TypeError("Blobs must be bytes or text")
        ref = hashlib.sha256(data).hexdigest()
        with self.lock:
            path = self.path(ref)
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                os.utime(path, None)
                return ref
            temporary: str | None = None
            try:
                with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
                    temporary = output.name
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, path)
                temporary = None
            finally:
                if temporary is not None:
                    Path(temporary).unlink(missing_ok=True)
        return ref

    def read(self, ref: str) -> bytes:
        return self.path(ref).read_bytes()

    get = read

    def read_text(self, ref: str) -> str:
        return self.read(ref).decode("utf-8")

    def delete(self, ref: str) -> None:
        self.path(ref).unlink(missing_ok=True)

    def cleanup(
        self, keep: set[str] | None = None, *, max_age_s: float = 3600,
        now: float | None = None, dry_run: bool = False, limit: int = 1000,
    ) -> int:
        """Remove at most ``limit`` old unreferenced blobs; retain cache-owned blobs."""
        if max_age_s < 0 or limit < 1:
            raise ValueError("Blob cleanup needs a nonnegative age and positive limit")
        keep = keep or set()
        threshold = (time.time() if now is None else now) - max_age_s
        removed = 0
        with self.lock:
            for path in self.root.glob("*/*"):
                if not path.is_file() or not _BLOB_REF.fullmatch(path.name):
                    continue
                if path.name not in keep and path.stat().st_mtime <= threshold:
                    if not dry_run:
                        self.delete(path.name)
                    removed += 1
                    if removed >= limit:
                        break
        return removed
