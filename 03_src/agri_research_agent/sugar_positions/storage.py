"""Immutable local releases with digest-checked reads and atomic publication."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from uuid import uuid4

from agri_research_agent.shared.atomic_storage import atomic_write_json
from .model import unique_rows

FOREIGN_KEY = ("market", "report_type", "group", "report_date")
DOMESTIC_KEY = ("scope", "side", "member", "account", "report_date")


def digest(content):
    return hashlib.sha256(content).hexdigest()


def preview_root(project_root: Path):
    """One local path; resolving junctions cannot redirect into a shared runtime."""
    root = project_root.resolve()
    if not (root / ".git").is_file():
        raise ValueError("白糖预览采集必须运行于独立 linked worktree")
    target = root / "01_data" / "sugar_positions_preview"
    if target.resolve() != target:
        raise ValueError("预览数据目录不能指向其他位置")
    return target


def read_snapshot(root: Path):
    pointer = root / "current.json"
    if not pointer.exists():
        return {"schema_version": 1, "foreign": [], "domestic": [], "sources": {}, "attempts": []}
    current = json.loads(pointer.read_text(encoding="utf-8"))
    release = current["release_id"]
    if not re.fullmatch(r"[0-9TZ_-]+[a-f0-9]{8}", release):
        raise ValueError("快照身份无效")
    base = root / "releases" / release
    manifest_bytes = (base / "manifest.json").read_bytes()
    if digest(manifest_bytes) != current["manifest_sha256"]:
        raise ValueError("快照 Manifest 校验失败")
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    payload_bytes = (base / "snapshot.json").read_bytes()
    if digest(payload_bytes) != manifest["snapshot_sha256"]:
        raise ValueError("快照数据校验失败")
    payload = json.loads(payload_bytes.decode("utf-8"))
    if payload["schema_version"] != 1:
        raise ValueError("不支持的白糖快照版本")
    unique_rows(payload["foreign"], FOREIGN_KEY)
    unique_rows(payload["domestic"], DOMESTIC_KEY)
    return payload


@contextmanager
def _writer_lock(root):
    root.mkdir(parents=True, exist_ok=True)
    path = root / "update.lock"
    # An abandoned lock requires investigation; never silently steal it.
    with path.open("x", encoding="utf-8") as lock:
        lock.write(datetime.now(timezone.utc).isoformat())
    try:
        yield
    finally:
        path.unlink()


def publish(root: Path, foreign, domestic, captures, attempts):
    """Merge validated rows without erasing other sources or old dates."""
    unique_rows(foreign, FOREIGN_KEY)
    unique_rows(domestic, DOMESTIC_KEY)
    with _writer_lock(root):
        previous = read_snapshot(root)
        merged = {}
        for name, new_rows, key in (("foreign", foreign, FOREIGN_KEY), ("domestic", domestic, DOMESTIC_KEY)):
            # Replace whole observed partitions so revised rankings cannot leave stale members.
            partitions = {(r["report_date"], r["scope"]) for r in new_rows} if name == "domestic" else {
                (r["report_date"], r["market"], r["report_type"]) for r in new_rows}
            old_rows = [r for r in previous[name] if (
                (r["report_date"], r["scope"]) if name == "domestic" else
                (r["report_date"], r["market"], r["report_type"])) not in partitions]
            merged[name] = sorted(old_rows + new_rows, key=lambda r: tuple(r[k] for k in key))
        sources = dict(previous["sources"])
        for source_id, raw, url, extension in captures:
            sha = digest(raw)
            destination = root / "raw" / f"{sha}.{extension}"
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists():
                destination.write_bytes(raw)
            elif digest(destination.read_bytes()) != sha:
                raise ValueError("原始源文件校验失败")
            sources[source_id] = {"url": url, "raw_file": destination.relative_to(root).as_posix(),
                "sha256": sha, "retrieved_at": datetime.now(timezone.utc).isoformat()}
        now = datetime.now(timezone.utc)
        release = now.strftime("%Y%m%dT%H%M%SZ_") + uuid4().hex[:8]
        base = root / "releases" / release
        base.mkdir(parents=True)
        payload = dict(schema_version=1, release_id=release, published_at=now.isoformat(),
            sources=sources, attempts=attempts, **merged)
        atomic_write_json(base / "snapshot.json", payload)
        manifest = dict(schema_version=1, release_id=release,
            snapshot_sha256=digest((base / "snapshot.json").read_bytes()),
            foreign_rows=len(merged["foreign"]), domestic_rows=len(merged["domestic"]))
        atomic_write_json(base / "manifest.json", manifest)
        atomic_write_json(root / "current.json", dict(release_id=release,
            manifest_sha256=digest((base / "manifest.json").read_bytes())))
        return payload
