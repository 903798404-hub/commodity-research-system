"""Reusable sealing for immutable, identity-addressed candidate directories."""

from __future__ import annotations

import os
from collections import Counter
import re
import shutil
import stat
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from filelock import FileLock


SAFE_CANDIDATE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
ResultT = TypeVar("ResultT")


class ImmutableCandidateError(RuntimeError):
    """Raised when a candidate cannot be built or sealed safely."""


def validate_candidate_id(value: str) -> str:
    if not isinstance(value, str) or SAFE_CANDIDATE_ID.fullmatch(value) is None:
        raise ValueError("candidate_id contains unsafe characters")
    return value


def seal_immutable_candidate(
    root: str | Path,
    candidate_id: str,
    builder: Callable[[Path], ResultT],
) -> tuple[Path, ResultT]:
    """Build below a same-filesystem temporary directory and rename once.

    The final candidate is never overwritten. This primitive deliberately has no
    current/previous pointer: promotion is a separate, explicitly authorized step.
    """

    safe_id = validate_candidate_id(candidate_id)
    candidate_root = Path(root).resolve()
    candidate_root.mkdir(parents=True, exist_ok=True)
    if not candidate_root.is_dir() or candidate_root.is_symlink():
        raise ImmutableCandidateError("candidate root must be a real directory")
    final = candidate_root / safe_id
    lock_path = candidate_root / ".candidate.lock"
    with FileLock(str(lock_path), timeout=0):
        if final.exists():
            raise FileExistsError(f"candidate already exists: {safe_id}")
        building = candidate_root / f".building-{safe_id}-{uuid.uuid4().hex}"
        building.mkdir()
        try:
            result = builder(building)
            _validate_tree(building)
            _fsync_tree(building)
            os.replace(building, final)
            if not final.is_dir():
                raise ImmutableCandidateError("sealed candidate is not a directory")
            return final, result
        except BaseException as error:
            if building.exists():
                try:
                    shutil.rmtree(building)
                except OSError:
                    # Retain a failed private candidate rather than changing
                    # attributes on an external history inode during cleanup.
                    error.add_note(f"Failed candidate retained for recovery: {building}")
            raise


def _validate_tree(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ImmutableCandidateError("candidate must not contain symlinks")
        if not (path.is_file() or path.is_dir()):
            raise ImmutableCandidateError("candidate contains a non-ordinary entry")


def _fsync_tree(root: Path) -> None:
    files = [path for path in root.rglob("*") if path.is_file()]
    links = Counter((p.stat().st_dev, p.stat().st_ino) for p in files)
    for path in files:
        # Windows _commit needs a writable descriptor, even for unchanged
        # bytes. All aliases must belong to this private candidate tree.
        mode = stat.S_IMODE(path.stat().st_mode)
        readonly_windows = os.name == "nt" and not mode & stat.S_IWRITE
        if readonly_windows:
            value = path.stat()
            if value.st_nlink != links[(value.st_dev, value.st_ino)]:
                raise ImmutableCandidateError("cannot change shared read-only candidate attributes")
            path.chmod(mode | stat.S_IWRITE)
        try:
            with path.open("r+b" if os.name == "nt" else "rb") as handle:
                os.fsync(handle.fileno())
        finally:
            if readonly_windows:
                path.chmod(mode)
