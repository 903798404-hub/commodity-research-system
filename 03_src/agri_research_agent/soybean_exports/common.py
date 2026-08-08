from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq


SCHEMA_VERSION = 1


class PipelineError(RuntimeError):
    """A safe, user-facing data pipeline failure."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("UTC timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def make_batch_id(now: datetime | None = None) -> str:
    current = (now or utc_now()).astimezone(timezone.utc)
    return f"{current:%Y%m%dT%H%M%SZ}_{uuid.uuid4().hex[:12]}"


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest().upper()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _fsync_file(handle: Any) -> None:
    handle.flush()
    os.fsync(handle.fileno())


def write_json_atomic(path: Path, value: Any, *, overwrite: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not overwrite and path.exists():
        raise FileExistsError(f"Refusing to overwrite immutable file: {path}")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(canonical_json_bytes(value) + b"\n")
            _fsync_file(handle)
        if not overwrite and path.exists():
            raise FileExistsError(f"Refusing to overwrite immutable file: {path}")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def read_last_success(status_path: Path) -> dict[str, Any] | None:
    """Read the durable success identity without promoting a failed attempt."""
    if not status_path.is_file():
        return None
    try:
        payload = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    last_success = payload.get("last_success")
    return dict(last_success) if isinstance(last_success, dict) else None


def write_raw_snapshot(
    *,
    raw_root: Path,
    pipeline_name: str,
    batch_id: str,
    records: Iterable[Mapping[str, Any]],
    manifest: Mapping[str, Any],
    source_file_name: str | None = None,
    source_file_bytes: bytes | None = None,
) -> dict[str, Any]:
    if (source_file_name is None) != (source_file_bytes is None):
        raise ValueError("source_file_name and source_file_bytes must be provided together")
    if source_file_name is not None and Path(source_file_name).name != source_file_name:
        raise ValueError("source_file_name must be a plain file name")
    snapshot_dir = raw_root / pipeline_name / batch_id
    if snapshot_dir.exists():
        raise FileExistsError(f"Immutable raw snapshot already exists: {snapshot_dir}")
    snapshot_dir.mkdir(parents=True, exist_ok=False)
    data_path = snapshot_dir / "records.jsonl.gz"
    count = 0
    canonical_digest = hashlib.sha256()
    try:
        source_file_identity: dict[str, Any] = {}
        if source_file_name is not None and source_file_bytes is not None:
            source_path = snapshot_dir / source_file_name
            with source_path.open("xb") as source_handle:
                source_handle.write(source_file_bytes)
                _fsync_file(source_handle)
            source_file_identity = {
                "source_file": source_file_name,
                "source_file_byte_size": len(source_file_bytes),
                "source_file_sha256": sha256_bytes(source_file_bytes),
            }
        with data_path.open("xb") as raw_handle:
            with gzip.GzipFile(
                filename="records.jsonl",
                mode="wb",
                fileobj=raw_handle,
                mtime=0,
            ) as compressed:
                for record in records:
                    line = canonical_json_bytes(dict(record)) + b"\n"
                    canonical_digest.update(line)
                    compressed.write(line)
                    count += 1
            _fsync_file(raw_handle)
        completed = {
            **dict(manifest),
            **source_file_identity,
            "schema_version": SCHEMA_VERSION,
            "pipeline": pipeline_name,
            "batch_id": batch_id,
            "raw_file": data_path.name,
            "row_count": count,
            "response_identity_sha256": canonical_digest.hexdigest().upper(),
            "raw_file_sha256": sha256_file(data_path),
        }
        write_json_atomic(
            snapshot_dir / "manifest.json", completed, overwrite=False
        )
        completed["manifest_sha256"] = sha256_file(snapshot_dir / "manifest.json")
        completed["snapshot_dir"] = str(snapshot_dir)
        return completed
    except Exception:
        # The unique batch directory is evidence of a failed attempt. Do not reuse it.
        raise


def write_parquet_atomic(
    frame: pd.DataFrame,
    path: Path,
    *,
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp.parquet")
    encoded_metadata = {
        str(key).encode("utf-8"): str(value).encode("utf-8")
        for key, value in metadata.items()
        if value is not None
    }
    try:
        table = pa.Table.from_pandas(frame, preserve_index=False)
        table = table.replace_schema_metadata(
            {**(table.schema.metadata or {}), **encoded_metadata}
        )
        pq.write_table(table, temporary, compression="zstd")
        with temporary.open("r+b") as handle:
            os.fsync(handle.fileno())
        reread = pd.read_parquet(temporary)
        if len(reread) != len(frame) or list(reread.columns) != list(frame.columns):
            raise PipelineError("Parquet round-trip validation failed")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return {
        "path": str(path),
        "row_count": len(frame),
        "byte_size": path.stat().st_size,
        "sha256": sha256_file(path),
        "parquet_schema_sha256": sha256_bytes(
            table.schema.remove_metadata().to_string().encode("utf-8")
        ),
    }


def atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copyfile(source, temporary)
        with temporary.open("r+b") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()


def publish_candidate(
    *,
    candidate_path: Path,
    candidate_manifest: Mapping[str, Any],
    stable_path: Path,
    stable_manifest_path: Path,
    backup_root: Path,
    batch_id: str,
) -> dict[str, Any]:
    candidate_sha = sha256_file(candidate_path)
    if candidate_sha != candidate_manifest["sha256"]:
        raise PipelineError("Candidate SHA-256 changed before publication")
    old_exists = stable_path.exists()
    if old_exists and sha256_file(stable_path) == candidate_sha:
        return {"status": "no_change", "published": False, "backup_dir": None}

    backup_dir = backup_root / batch_id
    backup_dir.mkdir(parents=True, exist_ok=False)
    backed_up: dict[Path, Path] = {}
    for target in (stable_path, stable_manifest_path):
        if target.exists():
            backup = backup_dir / target.name
            shutil.copy2(target, backup)
            backed_up[target] = backup

    try:
        atomic_copy(candidate_path, stable_path)
        if sha256_file(stable_path) != candidate_sha:
            raise PipelineError("Stable SHA-256 mismatch after atomic replacement")
        stable_manifest = {
            **dict(candidate_manifest),
            "stable_path": str(stable_path),
            "published_batch_id": batch_id,
        }
        write_json_atomic(stable_manifest_path, stable_manifest)
        reread = pd.read_parquet(stable_path)
        if len(reread) != int(candidate_manifest["row_count"]):
            raise PipelineError("Stable row count mismatch after publication")
    except Exception:
        for target in (stable_path, stable_manifest_path):
            backup = backed_up.get(target)
            if backup is not None:
                atomic_copy(backup, target)
            elif target.exists():
                target.unlink()
        raise
    return {
        "status": "initialized" if not old_exists else "updated",
        "published": True,
        "backup_dir": str(backup_dir),
    }


def append_revisions(
    existing_path: Path,
    new_rows: pd.DataFrame,
    *,
    metadata: Mapping[str, Any],
) -> dict[str, Any] | None:
    if new_rows.empty:
        return None
    if existing_path.exists():
        existing = pd.read_parquet(existing_path)
        combined = pd.concat([existing, new_rows], ignore_index=True)
        dedupe = [
            column
            for column in (
                "source",
                "business_key_json",
                "field_name",
                "old_snapshot_identity",
                "new_snapshot_identity",
            )
            if column in combined.columns
        ]
        combined = combined.drop_duplicates(dedupe, keep="first")
    else:
        combined = new_rows.copy()
    return write_parquet_atomic(combined, existing_path, metadata=metadata)
