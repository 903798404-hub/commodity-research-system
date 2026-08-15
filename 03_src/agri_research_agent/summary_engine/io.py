from __future__ import annotations
import hashlib
from pathlib import Path
from typing import Any


def file_identity(path: Path) -> dict[str, Any]:
    stat=path.stat(); digest=hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda:handle.read(1024*1024),b""): digest.update(chunk)
    return {"path":str(path),"size":stat.st_size,"mtime_ns":stat.st_mtime_ns,"sha256":digest.hexdigest()}
