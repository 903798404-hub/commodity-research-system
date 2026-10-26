"""Sparse public delivery: changed months plus a pinned complete logical manifest.

There are no links to production in a writable candidate. Only the protected
publisher creates final read-only file entries from its immutable object pool.
Legacy readers, manifests, Current CAS and rollback remain byte-compatible.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path, PurePosixPath
import re

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from . import public_data_delivery as delivery

SCHEMA = "public-current-increment/1"
PREFIX = "consumer-artifacts/domestic-spread/historical_spread_database.parquet/"


def require(value, message):
    if not value:
        raise delivery.DeliveryError(message)


def record(path, raw):
    import hashlib
    return {"path": path, "size_bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def _safe_path(value):
    require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_./-]+", value)
            and not PurePosixPath(value).is_absolute()
            and all(p not in {"", ".", ".."} for p in value.split("/")), "increment path unsafe")
    return value


def _no_links(path):
    require(not any(p.is_symlink() for p in (path, *path.parents)), "increment symlink refused")
    return path


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(value)


def _json(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _artifact(parts, table):
    records = [{**v, "path": v["path"].removeprefix(PREFIX)} for v in parts]
    return {"filename": delivery.DOMESTIC_SPREAD_FILENAME,
            "package_path": PREFIX.rstrip("/"), "storage": "MONTHLY_PARQUET",
            "size_bytes": sum(v["size_bytes"] for v in parts),
            "sha256": delivery._bundle_sha256(records),
            **delivery._domestic_spread_table_identity(table)}


def _manifest(base, artifact, records, created_at):
    identities = {"current_identity_sha256": base["current_identity_sha256"],
                  "delivery_artifacts": {"domestic-spread": artifact["business_sha256"]},
                  "artifact_storage": "MONTHLY_PARQUET"}
    digest = delivery._json_sha256(identities)
    return {**deepcopy(base), "schema_version": delivery.PARTITIONED_PACKAGE_SCHEMA,
            "package_id": "public-current-" + digest[:24], "created_at": created_at,
            "delivery_identity_sha256": digest, "delivery_artifacts": {"domestic-spread": artifact},
            "files": records, "file_count": len(records), "bundle_sha256": delivery._bundle_sha256(records)}


def build_increment(base_directory, spread_path, output_root):
    base = delivery.validate_production_package(base_directory)
    require(set(base.manifest["delivery_artifacts"]) == {"domestic-spread"}, "unexpected public artifacts")
    table = delivery._partition_table(pq.read_table(spread_path))
    # Validate the complete result in memory before allocating any increment.
    identity = delivery._domestic_spread_table_identity(table)
    if identity["business_sha256"] == base.manifest["delivery_artifacts"]["domestic-spread"]["business_sha256"]:
        return None
    old = {v["path"]: v for v in base.manifest["files"]}
    months = pc.strftime(table["date"], format="%Y-%m").to_pylist()
    parts, payloads = [], {}
    for month in sorted(set(months)):
        require(isinstance(month, str) and re.fullmatch(r"\d{4}-\d{2}", month), "increment month invalid")
        part = table.filter(pa.array([m == month for m in months]))
        part = part.sort_by([(name, "ascending") for name in part.column_names]).combine_chunks()
        sink = pa.BufferOutputStream()
        pq.write_table(part, sink, compression="zstd")
        raw = sink.getvalue().to_pybytes()
        item = record("", raw)
        item["path"] = PREFIX + f"part-{month}-{item['sha256']}.parquet"
        parts.append(item)
        if old.get(item["path"]) != item:
            payloads[item["path"]] = raw
    retained = [v for v in base.manifest["files"] if not v["path"].startswith("consumer-artifacts/domestic-spread/")]
    records = sorted(retained + parts, key=lambda v: v["path"])
    manifest = _manifest(base.manifest, _artifact(parts, table), records, datetime.now(timezone.utc).isoformat())
    target = Path(output_root) / manifest["package_id"]
    require(not target.exists(), "increment output already exists")
    target.mkdir(parents=True)
    (target / "data").mkdir()
    for relative, raw in payloads.items():
        _write(target / "data" / relative, raw)
    _write(target / "manifest.json", _json(manifest))
    proof = {"schema_version": SCHEMA, "base_package_id": base.package_id,
             "base_manifest_sha256": delivery._sha256_file(base.directory / "manifest.json"),
             "manifest_sha256": delivery._sha256_file(target / "manifest.json"),
             "payload_paths": sorted(payloads)}
    _write(target / "increment.json", _json(proof))
    validate_increment(target, base.directory)
    return delivery.ProductionPackage(manifest["package_id"], target, manifest, True)


def validate_increment(directory, base_directory):
    root = _no_links(Path(directory))
    require({p.name for p in root.iterdir()} == {"data", "manifest.json", "increment.json"}, "increment file set invalid")
    delivery._assert_no_symlinks(root)
    base = delivery.validate_production_package(_no_links(Path(base_directory)))
    proof = delivery._strict_json(root / "increment.json")
    require(set(proof) == {"schema_version", "base_package_id", "base_manifest_sha256", "manifest_sha256", "payload_paths"}
            and proof["schema_version"] == SCHEMA
            and proof["base_package_id"] == base.package_id
            and proof["base_manifest_sha256"] == delivery._sha256_file(base.directory / "manifest.json")
            and proof["manifest_sha256"] == delivery._sha256_file(root / "manifest.json"), "increment baseline or manifest differs")
    manifest = delivery._strict_json(root / "manifest.json")
    records = manifest.get("files")
    require(isinstance(records, list) and records, "increment file declarations absent")
    for item in records:
        require(set(item) == {"path", "sha256", "size_bytes"} and type(item["size_bytes"]) is int
                and item["size_bytes"] >= 0 and re.fullmatch(r"[0-9a-f]{64}", item["sha256"]), "increment record invalid")
        _safe_path(item["path"])
    require(records == sorted(records, key=lambda v: v["path"])
            and len({v["path"] for v in records}) == len(records), "duplicate or unordered increment paths")
    old = {v["path"]: v for v in base.manifest["files"]}
    expected_payloads = [v for v in records if old.get(v["path"]) != v]
    require(proof["payload_paths"] == [v["path"] for v in expected_payloads]
            and delivery._file_records(root / "data") == expected_payloads, "increment payload bytes differ")
    retained = lambda items: [v for v in items if not v["path"].startswith("consumer-artifacts/domestic-spread/")]
    require(retained(records) == retained(base.manifest["files"]), "unrelated public files changed")
    parts = [v for v in records if v["path"].startswith("consumer-artifacts/domestic-spread/")]
    tables, months = [], set()
    for item in parts:
        match = re.fullmatch(re.escape(PREFIX) + r"part-(\d{4}-\d{2})-([0-9a-f]{64})\.parquet", item["path"])
        require(match and match[2] == item["sha256"] and match[1] not in months, "partition declaration invalid")
        month = match[1]; months.add(month)
        path = (root if item in expected_payloads else base.directory) / "data" / item["path"]
        table = pq.read_table(path)
        require(set(pc.strftime(table["date"], format="%Y-%m").to_pylist()) == {month}, "partition month differs")
        tables.append(table)
    require(tables, "spread partitions absent")
    table = pa.concat_tables(tables)
    expected = _manifest(base.manifest, _artifact(parts, table), records, manifest.get("created_at"))
    require(manifest == expected and datetime.fromisoformat(manifest["created_at"]).tzinfo is not None,
            "increment logical manifest differs")
    require(manifest["package_id"] != base.package_id, "unchanged increment must not be published")
    # Changed months may append new keys, but must preserve every historical row,
    # including provenance and multiplicity. Never use business hash alone here.
    from collections import Counter
    old_path = base.directory / "data" / base.manifest["delivery_artifacts"]["domestic-spread"]["package_path"]
    previous = delivery._partition_table(pq.read_table(old_path))
    require(previous.column_names == table.column_names, "historical schema changed")
    encode = lambda t: Counter(json.dumps(row, sort_keys=True, default=str, allow_nan=True) for row in t.to_pylist())
    require(not (encode(previous) - encode(table)), "historical rows changed or disappeared")
    return {"status": "PASS", "package_id": manifest["package_id"],
            "payload_files": len(expected_payloads), "payload_bytes": sum(v["size_bytes"] for v in expected_payloads),
            "reused_files": len(records) - len(expected_payloads),
            "logical_bytes": sum(v["size_bytes"] for v in records), "history_versions_deleted": 0,
            "full_bundle_written": False}
