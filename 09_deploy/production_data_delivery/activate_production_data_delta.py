"""Receive, validate, publish, and roll back crop/FAS production deltas.

The host driver uses only the Python standard library.  Dataframe semantics run
once in an explicitly pinned application image with networking disabled.  This
module never runs a provider, builds or pulls an image, creates an approval, or
accepts AkShare/public-package artifacts.
"""
from __future__ import annotations

import argparse
import base64
import ctypes
from datetime import date, datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid


ROOT = Path(__file__).resolve().parents[2]
MANIFEST_NAME = "delta_contract.json"
SCHEMA_VERSION = "production-data-delta/1"
POLICY_VERSION = "production-data-delta-policy/1"
POLICY_ROOT = Path("/etc/market-data/production-data-delivery")
INCOMING_ROOT = Path("/var/lib/market-data/production-data-incoming")
UPLOAD_ROOT = Path("/var/lib/market-data/production-data-uploads")
CANDIDATE_ROOT = Path("/var/lib/market-data/production-data-candidates")
EVIDENCE_ROOT = Path("/var/lib/market-data/production-data-evidence")
BACKUP_ROOT = Path("/var/lib/market-data/production-data-backups")
ALLOCATION_ROOT = Path("/var/lib/market-data/production-runtime")
CANONICAL_ORIGINS = {
    "git@github.com:903798404-hub/commodity-research-system.git",
    "https://github.com/903798404-hub/commodity-research-system.git",
}
HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,100}$")
RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,120}$")
IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")

CROP_PROGRESS = "01_data/processed/soybean_crop_progress/soybeans_crop_progress_weekly.parquet"
CROP_CONDITION = "01_data/processed/soybean_crop_progress/soybeans_crop_condition_weekly.parquet"
CROP_LEGACY_PROGRESS = "01_data/processed/soybean_crop_progress/soybeans_crop_progress_weekly_2021_2026.parquet"
CROP_LEGACY_CONDITION = "01_data/processed/soybean_crop_progress/soybeans_crop_condition_weekly_2021_2026.parquet"
CROP_STATUS = "01_data/update_status/soybean_crop_progress.json"
FAS_STABLE = "01_data/processed/soybean_export_sales/soybean_export_sales_weekly.parquet"
FAS_MANIFEST = "01_data/processed/soybean_export_sales/soybean_export_sales_weekly.manifest.json"
FAS_STATUS = "01_data/update_status/soybean_export_sales.json"
CANOLA_STABLE = "01_data/processed/canada_canola/canola_weekly.json"
CANOLA_SOURCES = "01_data/processed/canada_canola/source_evidence.json"
CANOLA_STATUS = "01_data/update_status/canada_canola.json"

DOMAIN_CONTRACTS = {
    "canada_canola": {
        "payloads": {"canola_weekly.json": CANOLA_STABLE,
                     "source_evidence.json": CANOLA_SOURCES},
        "baseline": (CANOLA_STABLE, CANOLA_SOURCES, CANOLA_STATUS),
        "domain_dir": "01_data/processed/canada_canola",
        "status_path": CANOLA_STATUS,
        "metadata": ("record_count", "latest_dates", "revision_keys"),
    },
    "soybean_crop_progress": {
        "payloads": {
            "soybeans_crop_progress_weekly.parquet": CROP_PROGRESS,
            "soybeans_crop_condition_weekly.parquet": CROP_CONDITION,
        },
        "baseline": (CROP_PROGRESS, CROP_CONDITION, CROP_LEGACY_PROGRESS,
                     CROP_LEGACY_CONDITION, CROP_STATUS),
        "domain_dir": "01_data/processed/soybean_crop_progress",
        "status_path": CROP_STATUS,
        "metadata": ("current_year", "retrieved_at_utc", "raw_snapshot",
                     "duplicate_counts", "source_manifest_sha256"),
    },
    "soybean_export_sales": {
        "payloads": {"soybean_export_sales_weekly.parquet": FAS_STABLE},
        "baseline": (FAS_STABLE, FAS_MANIFEST, FAS_STATUS),
        "domain_dir": "01_data/processed/soybean_export_sales",
        "status_path": FAS_STATUS,
        "metadata": ("batch_id", "source", "source_latest_week",
                     "source_release_time_raw", "source_release_timezone",
                     "fetch_scope", "raw_snapshot_sha256",
                     "raw_manifest_sha256", "quality_status"),
    },
}


class DeltaError(ValueError):
    """Safe fail-closed contract or protected-host error."""


def require(condition: object, message: str) -> None:
    if not condition:
        raise DeltaError(message)


def canonical_json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path: str | Path) -> str:
    selected = Path(path)
    require(selected.is_file() and not selected.is_symlink(),
            "regular non-symlink file required")
    with selected.open("rb") as handle:
        if hasattr(hashlib, "file_digest"):
            return hashlib.file_digest(handle, "sha256").hexdigest()
        return sha256_bytes(handle.read())


def strict_json_bytes(raw: bytes) -> dict[str, object]:
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate JSON key")
            result[key] = value
        return result

    def constant(_value):
        raise DeltaError("non-finite JSON value")

    try:
        value = json.loads(raw.decode("utf-8", errors="strict"),
                           object_pairs_hook=pairs, parse_constant=constant)
    except (UnicodeError, json.JSONDecodeError):
        raise DeltaError("invalid UTF-8 JSON") from None
    require(type(value) is dict, "JSON document must be an object")
    return value


def read_json(path: str | Path) -> dict[str, object]:
    return strict_json_bytes(Path(path).read_bytes())


def _exact(value: object, keys: set[str], label: str) -> dict[str, object]:
    require(type(value) is dict and set(value) == keys, f"{label} fields are invalid")
    return value


def _timestamp(value: object, label: str) -> datetime:
    require(type(value) is str, f"{label} timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise DeltaError(f"{label} timestamp is invalid") from None
    require(parsed.utcoffset() is not None, f"{label} timestamp requires timezone")
    return parsed.astimezone(timezone.utc)


def _file_identity(value: object, *, nullable: bool = False) -> dict[str, object] | None:
    if value is None and nullable:
        return None
    item = _exact(value, {"sha256", "size_bytes"}, "file identity")
    require(type(item["sha256"]) is str and HEX64.fullmatch(item["sha256"]),
            "file identity SHA-256 is invalid")
    require(type(item["size_bytes"]) is int and item["size_bytes"] > 0,
            "file identity size is invalid")
    return item


def _parquet_identity(value: object) -> dict[str, object]:
    item = _exact(value, {"sha256", "size_bytes", "row_count",
                          "parquet_schema_sha256"}, "Parquet identity")
    _file_identity({"sha256": item["sha256"], "size_bytes": item["size_bytes"]})
    require(type(item["row_count"]) is int and item["row_count"] > 0,
            "Parquet row count is invalid")
    require(type(item["parquet_schema_sha256"]) is str
            and HEX64.fullmatch(item["parquet_schema_sha256"]),
            "Parquet schema SHA-256 is invalid")
    return item


def contract_for_domain(domain: str) -> dict[str, object]:
    """Return a copy of the pure fixed-path contract used by local producers."""
    require(domain in DOMAIN_CONTRACTS,
            "delta domain is not supported")
    item = DOMAIN_CONTRACTS[domain]
    return {
        "domain": domain,
        "payloads": dict(item["payloads"]),
        "baseline": list(item["baseline"]),
        "domain_dir": item["domain_dir"],
        "status_path": item["status_path"],
        "metadata": list(item["metadata"]),
    }


def validate_delta_document(value: object) -> dict[str, object]:
    """Validate the closed transfer contract without host or data dependencies."""
    root = _exact(value, {"schema_version", "status", "delta_id",
                          "generated_at_utc", "domain", "producer", "run",
                          "baseline", "domain_metadata", "payloads"}, "delta")
    require(root["schema_version"] == SCHEMA_VERSION and root["status"] == "CANDIDATE",
            "delta schema version or status is invalid")
    require(type(root["delta_id"]) is str and SAFE_ID.fullmatch(root["delta_id"]),
            "delta_id is invalid")
    _timestamp(root["generated_at_utc"], "generated_at_utc")
    require(root["domain"] in DOMAIN_CONTRACTS,
            "delta domain is not supported")
    contract = DOMAIN_CONTRACTS[root["domain"]]

    producer = _exact(root["producer"], {"commit", "tree", "origin"}, "producer")
    require(type(producer["commit"]) is str and HEX40.fullmatch(producer["commit"])
            and type(producer["tree"]) is str and HEX40.fullmatch(producer["tree"]),
            "producer Git identity is invalid")
    require(producer["origin"] in CANONICAL_ORIGINS, "producer origin is invalid")

    run = _exact(root["run"], {"run_id", "git_head", "business_status", "published"}, "run")
    require(type(run["run_id"]) is str and RUN_ID.fullmatch(run["run_id"]),
            "run_id is invalid")
    require(run["git_head"] == producer["commit"], "run Git identity differs from producer")
    require(run["business_status"] in {"initialized", "updated", "no_change"}
            and run["published"] is False, "local run publication state is invalid")

    baseline = _exact(root["baseline"], {"files"}, "baseline")
    files = baseline["files"]
    require(type(files) is dict and set(files) == set(contract["baseline"]),
            "baseline file set is invalid")
    for path, identity in files.items():
        nullable = path not in {CROP_LEGACY_PROGRESS, CROP_LEGACY_CONDITION}
        _file_identity(identity, nullable=nullable)
    require((files.get(CROP_PROGRESS) is None) == (files.get(CROP_CONDITION) is None),
            "crop stable baseline must be a complete pair")
    require((files.get(FAS_STABLE) is None) == (files.get(FAS_MANIFEST) is None),
            "FAS stable baseline must include its manifest")
    if root["domain"] == "canada_canola":
        require(len({files[path] is None for path in contract["baseline"]}) == 1,
                "canola baseline must include data, evidence and status together")

    metadata = root["domain_metadata"]
    require(type(metadata) is dict and set(metadata) == set(contract["metadata"]),
            "domain metadata fields are invalid")
    if root["domain"] == "soybean_crop_progress":
        require(type(metadata["current_year"]) is int and metadata["current_year"] >= 2021,
                "crop current_year is invalid")
        _timestamp(metadata["retrieved_at_utc"], "crop retrieved_at_utc")
        raw_match = (re.fullmatch(
            r"01_data/raw/soybean_crop_progress/"
            r"nass_soybeans_crop_weekly_([0-9]{4})_"
            r"([0-9]{8}T(?:[0-9]{6}|[0-9]{12})Z)\.json",
            metadata["raw_snapshot"])
            if type(metadata["raw_snapshot"]) is str else None)
        calendar_valid = False
        if raw_match is not None:
            captured = raw_match.group(2)
            try:
                datetime.strptime(
                    captured,
                    "%Y%m%dT%H%M%SZ" if len(captured) == 16
                    else "%Y%m%dT%H%M%S%fZ")
                calendar_valid = True
            except ValueError:
                pass
        require(raw_match is not None
                and calendar_valid
                and int(raw_match.group(1)) == metadata["current_year"]
                and raw_match.group(2)[:4] == raw_match.group(1),
                "crop raw snapshot identity is invalid")
        counts = _exact(metadata["duplicate_counts"], {"PROGRESS", "CONDITION"},
                        "crop duplicate counts")
        require(all(type(item) is int and item >= 0 for item in counts.values()),
                "crop duplicate counts are invalid")
        require(type(metadata["source_manifest_sha256"]) is str
                and HEX64.fullmatch(metadata["source_manifest_sha256"]),
                "crop source manifest identity is invalid")
    elif root["domain"] == "canada_canola":
        require(type(metadata["record_count"]) is int and metadata["record_count"] > 0,
                "canola record count is invalid")
        require(type(metadata["latest_dates"]) is dict and metadata["latest_dates"],
                "canola latest dates are invalid")
        for key, value in metadata["latest_dates"].items():
            require(type(key) is str and re.fullmatch(
                r"(?:SK|AB|MB)/(?:PLANTED|HARVESTED|GOOD_EXCELLENT|PRE_EMERGING|SEEDLING|ROSETTE|BOLTING|FLOWERING|PODDED|RIPE)", key),
                "canola latest date key is invalid")
            require(type(value) is str and date.fromisoformat(value).isoformat() == value,
                    "canola latest date is invalid")
        _canola_revision_keys(metadata["revision_keys"])
    else:
        require(type(metadata["batch_id"]) is str and RUN_ID.fullmatch(metadata["batch_id"]),
                "FAS batch_id is invalid")
        require(metadata["source"] == "usda_fas_esr"
                and metadata["source_release_timezone"] == "America/New_York"
                and metadata["quality_status"] == "passed", "FAS source identity is invalid")
        require(type(metadata["source_latest_week"]) is str
                and re.fullmatch(r"\d{4}-\d{2}-\d{2}", metadata["source_latest_week"]),
                "FAS latest week is invalid")
        try:
            date.fromisoformat(metadata["source_latest_week"])
        except ValueError:
            raise DeltaError("FAS latest week is invalid") from None
        require(type(metadata["source_release_time_raw"]) is str
                and bool(metadata["source_release_time_raw"]), "FAS release time is invalid")
        scope = _exact(metadata["fetch_scope"], {"report_market_years"}, "FAS fetch scope")
        years = scope["report_market_years"]
        require(type(years) is list and years and all(type(year) is int and year >= 2000 for year in years)
                and len(years) == len(set(years)), "FAS report market years are invalid")
        for key in ("raw_snapshot_sha256", "raw_manifest_sha256"):
            require(type(metadata[key]) is str and HEX64.fullmatch(metadata[key]),
                    f"FAS {key} is invalid")

    payloads = root["payloads"]
    require(type(payloads) is dict and set(payloads) == set(contract["payloads"]),
            "delta payload file set is invalid")
    for identity in payloads.values():
        if root["domain"] == "canada_canola":
            _file_identity(identity)
        else:
            _parquet_identity(identity)
    return root


def _canola_revision_keys(value: object) -> list[str]:
    require(type(value) is list and all(type(key) is str for key in value)
            and len(value) == len(set(value)), "canola revision keys are invalid")
    for key in value:
        require(re.fullmatch(
            r"(?:SK|AB|MB)/(?:PLANTED|HARVESTED|GOOD_EXCELLENT|PRE_EMERGING|SEEDLING|ROSETTE|BOLTING|FLOWERING|PODDED|RIPE)/[0-9]{4}-[0-9]{2}-[0-9]{2}", key),
            "canola revision key is invalid")
        require(date.fromisoformat(key.rsplit("/", 1)[1]).isoformat() == key.rsplit("/", 1)[1],
                "canola revision key date is invalid")
    return value


def _canola_observations(candidate_path: Path, baseline_path: Path | None,
                         evidence: dict, revision_keys: list[str]) -> dict:
    """Use image business validators; preserve history and verify archived bytes.

    Official tables remain manually reviewed. Source bytes and locators are
    evidence, not an assertion that a generic PDF parser extracted each value.
    """
    from agri_research_agent.pipelines.canada_canola import (
        RECORD_FIELDS, check_source_url, import_workbook, load_bundle,
    )
    candidate = load_bundle(candidate_path)
    baseline = load_bundle(baseline_path) if baseline_path is not None else None
    def key(row):
        return "/".join(row[field] for field in ("province", "metric", "date"))
    old = {key(row): row for row in baseline["records"]} if baseline else {}
    new = {key(row): row for row in candidate["records"]}
    require(old.keys() <= new.keys(), "canola history deletion is forbidden")
    require(baseline is None or candidate["import_notes"] == baseline["import_notes"],
            "canola historical import notes changed")
    revised = {item for item in old if old[item] != new[item]}
    require(revised == set(_canola_revision_keys(revision_keys)),
            "canola history revision requires its exact explicit keys")
    changed = [new[item] for item in sorted(new.keys() - old.keys() | revised)]
    _exact(evidence, {"schema_version", "sources"}, "canola source evidence")
    require(evidence["schema_version"] == "canada-canola-source-evidence/1"
            and type(evidence["sources"]) is list, "canola source evidence schema invalid")
    reports, workbooks = {}, {}
    total_bytes = 0
    for item in evidence["sources"]:
        _exact(item, {"kind", "sha256", "bytes_base64", "province", "source_url", "retrieved_at"},
               "canola source")
        require(item["kind"] in {"workbook", "report"}
                and type(item["sha256"]) is str and HEX64.fullmatch(item["sha256"])
                and type(item["bytes_base64"]) is str and len(item["bytes_base64"]) <= 28_000_000,
                "canola source identity invalid")
        raw = base64.b64decode(item["bytes_base64"], validate=True)
        total_bytes += len(raw)
        require(raw and len(raw) <= 20_000_000 and total_bytes <= 100_000_000
                and sha256_bytes(raw) == item["sha256"], "canola source byte identity mismatch")
        if item["kind"] == "report":
            check_source_url(item["province"], item["source_url"])
            _timestamp(item["retrieved_at"], "canola source retrieval")
            source_key = (item["province"], item["source_url"], item["sha256"], item["retrieved_at"])
            require(source_key not in reports, "duplicate canola report evidence")
            reports[source_key] = item
        else:
            require(baseline is None and item["province"] is None and item["source_url"] is None
                    and item["retrieved_at"] is None and item["sha256"] not in workbooks,
                    "workbook evidence is only valid for first historical import")
            with tempfile.TemporaryDirectory(prefix="canola-source-") as temporary:
                path = Path(temporary) / "source.xlsx"
                path.write_bytes(raw)
                workbooks[item["sha256"]] = {key(row): row for row in import_workbook(path)["records"]}
    required_reports, required_workbooks = set(), set()
    for row in changed:
        if row["date_basis"] == "report_cutoff":
            source_key = (row["province"], row["source_url"], row["source_sha256"], row["retrieved_at"])
            require(source_key in reports, "canola observation lacks archived official source")
            required_reports.add(source_key)
        else:
            require(baseline is None and row["source_sha256"] in workbooks,
                    "canola workbook data requires first-import evidence")
            expected = workbooks[row["source_sha256"]].get(key(row))
            require(expected is not None and all(row[field] == expected[field]
                    for field in RECORD_FIELDS - {"retrieved_at"}), "canola workbook observation mismatch")
            required_workbooks.add(row["source_sha256"])
    require(set(reports) == required_reports and set(workbooks) == required_workbooks,
            "canola source evidence contains unused or missing sources")
    for digest, rows in workbooks.items():
        actual = {key(row) for row in changed if row["date_basis"] == "workbook_date"
                  and row["source_sha256"] == digest}
        require(actual == set(rows), "canola first import omitted historical workbook records")
    latest = {}
    for row in candidate["records"]:
        group = row["province"] + "/" + row["metric"]
        latest[group] = max(latest.get(group, ""), row["date"])
    return {"business_changed": bool(changed), "added": len(new.keys() - old.keys()),
            "revised": len(revised), "unchanged": len(old) - len(revised),
            "record_count": len(new), "latest_dates": latest}


def _absolute(path: str | Path) -> Path:
    selected = Path(path)
    require(selected.is_absolute() and ".." not in selected.parts
            and selected == Path(os.path.abspath(selected)), "absolute canonical path required")
    return selected


def _no_links(path: str | Path) -> Path:
    selected = _absolute(path)
    for item in (selected, *selected.parents):
        if item.exists() or item.is_symlink():
            require(not item.is_symlink()
                    and not getattr(item, "is_junction", lambda: False)(),
                    "symlink or junction is forbidden")
    return selected


def _under(path: str | Path, parent: Path, *, allow_equal: bool = False) -> Path:
    selected = _no_links(path)
    require((allow_equal and selected == parent) or parent in selected.parents,
            "path is outside its protected namespace")
    return selected


def _protected(path: str | Path, *, exists: bool = True) -> Path:
    selected = _no_links(path)
    require(os.name == "posix" and os.geteuid() == 0,
            "protected operation requires Linux host root")
    require(not exists or selected.exists(), "protected path is missing")
    for item in (selected, *selected.parents):
        if item.exists():
            info = item.stat()
            require(info.st_uid == 0 and not info.st_mode & 0o022,
                    "unprotected ownership or permissions")
            require(stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode),
                    "unsupported protected file type")
    return selected


def _file_set(root: Path) -> dict[str, dict[str, object]]:
    root = _no_links(root)
    require(root.is_dir(), "file-set root is not a directory")
    result = {}
    for path in sorted(root.rglob("*")):
        _no_links(path)
        require(path.is_dir() or path.is_file(), "special file is forbidden")
        if path.is_file():
            require(path.stat().st_nlink == 1, "hard-linked file is forbidden")
            result[path.relative_to(root).as_posix()] = {
                "sha256": sha256_file(path), "size_bytes": path.stat().st_size}
    return result


def _protected_tree(root: Path) -> Path:
    _protected(root)
    for path in root.rglob("*"):
        _protected(path)
    return root


def _make_tree_readonly(root: Path, *, public_read: bool = False) -> None:
    file_mode, directory_mode = ((0o444, 0o555) if public_read else (0o400, 0o500))
    for path in root.rglob("*"):
        path.chmod(directory_mode if path.is_dir() else file_mode)
    root.chmod(directory_mode)


def _write_exclusive(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())


def _write_json_exclusive(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.sealing")
    linked = False
    try:
        _write_exclusive(temporary, canonical_json_bytes(value))
        os.link(temporary, path)
        linked = True
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            # Once the exclusive hard link exists, cleanup of its hidden twin
            # must not turn a valid sealed receipt into a reported failure.
            if not linked:
                raise


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        _write_exclusive(temporary, canonical_json_bytes(value))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _git_identity(root: Path) -> dict[str, str]:
    _protected_tree(root)
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "HOME": "/nonexistent",
        "XDG_CONFIG_HOME": "/nonexistent", "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_OPTIONAL_LOCKS": "0",
    }
    def git_bytes(*args: str) -> bytes:
        return subprocess.check_output(["git", "-C", str(root), *args],
                                       env=environment, stderr=subprocess.DEVNULL)
    def git(*args: str) -> str:
        return git_bytes(*args).decode("utf-8", errors="strict").strip()
    require((root / ".git").is_dir(), "source clone must be an independent Git repository")
    require(git("status", "--porcelain=v1", "--untracked-files=all", "--ignored=matching") == "",
            "source clone must be clean")
    require(git("rev-parse", "--is-shallow-repository") == "true",
            "source clone must be shallow")
    require(git("rev-parse", "--abbrev-ref", "HEAD") == "HEAD",
            "source clone must use detached HEAD")
    origin = git("remote", "get-url", "origin")
    require(origin in CANONICAL_ORIGINS, "source clone origin is invalid")
    require(git("replace", "-l") == "", "Git replace refs are forbidden")
    autocrlf_result = subprocess.run(
        ["git", "-C", str(root), "config", "--local", "--get", "core.autocrlf"],
        env=environment, capture_output=True, timeout=10, check=False)
    require(autocrlf_result.returncode in {0, 1}
            and autocrlf_result.stdout.decode("utf-8", errors="strict").strip() in {"", "false"},
            "source clone core.autocrlf must be false")
    listing = git_bytes("ls-files", "-z", "--stage").split(b"\0")
    for record in listing:
        if not record:
            continue
        header, relative_raw = record.split(b"\t", 1)
        mode, blob, stage = header.decode("ascii").split()
        require(mode == "100644" or mode == "100755", "source clone tracked special file is forbidden")
        require(stage == "0", "source clone has an unmerged index entry")
        relative = relative_raw.decode("utf-8", errors="strict")
        path = root / relative
        require(path.is_file() and not path.is_symlink() and path.stat().st_nlink == 1,
                "source clone tracked file is unsafe")
        require(git_bytes("hash-object", "--no-filters", str(path)).decode().strip() == blob,
                "source clone tracked bytes differ from approved Git blob")
    flags = git("ls-files", "-v").splitlines()
    require(all(line and line[0] == "H" for line in flags),
            "source clone skip-worktree or assume-unchanged state is forbidden")
    subprocess.run(["git", "-C", str(root), "fsck", "--full", "--no-progress"],
                   env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   timeout=120, check=True)
    commit, tree = git("rev-parse", "HEAD"), git("show", "-s", "--format=%T", "HEAD")
    require(HEX40.fullmatch(commit) and HEX40.fullmatch(tree), "source clone Git identity is invalid")
    return {"commit": commit, "tree": tree, "origin": origin}


def _validate_policy(value: object) -> dict[str, object]:
    policy = _exact(value, {"schema_version", "status", "policy_id", "domain",
                            "allocation_root", "source_clone", "approved_producer",
                            "validation_image"}, "policy")
    require(policy["schema_version"] == POLICY_VERSION and policy["status"] == "ACTIVE",
            "policy schema version or status is invalid")
    require(type(policy["policy_id"]) is str and SAFE_ID.fullmatch(policy["policy_id"]),
            "policy_id is invalid")
    require(policy["domain"] in DOMAIN_CONTRACTS, "policy domain is invalid")
    allocation = _under(policy["allocation_root"], ALLOCATION_ROOT)
    source = _absolute(policy["source_clone"])
    require(source == ROOT, "policy source clone differs from executing tool checkout")
    producer = _exact(policy["approved_producer"], {"commit", "tree", "origin"},
                      "approved producer")
    require(type(producer["commit"]) is str and HEX40.fullmatch(producer["commit"])
            and type(producer["tree"]) is str and HEX40.fullmatch(producer["tree"])
            and producer["origin"] in CANONICAL_ORIGINS, "approved producer identity is invalid")
    image = _exact(policy["validation_image"], {"image_id", "commit", "tree"},
                   "validation image")
    require(type(image["image_id"]) is str and IMAGE_ID.fullmatch(image["image_id"])
            and type(image["commit"]) is str and HEX40.fullmatch(image["commit"])
            and type(image["tree"]) is str and HEX40.fullmatch(image["tree"]),
            "validation image identity is invalid")
    return policy


def _load_policy(path: str | Path) -> tuple[dict[str, object], str]:
    selected = _protected(_under(path, POLICY_ROOT))
    policy = _validate_policy(read_json(selected))
    require(_git_identity(ROOT) == policy["approved_producer"],
            "approved producer differs from source clone")
    _protected(_under(policy["allocation_root"], ALLOCATION_ROOT))
    return policy, sha256_file(selected)


def _policy_unchanged(path: str | Path, expected_sha256: str) -> None:
    selected = _protected(_under(path, POLICY_ROOT))
    require(sha256_file(selected) == expected_sha256,
            "protected host policy changed during operation")


def _derived(root: Path, policy: dict[str, object], delta_id: str, suffix: str = "") -> Path:
    require(SAFE_ID.fullmatch(delta_id) is not None, "delta_id is invalid")
    return root / str(policy["policy_id"]) / (delta_id + suffix)


def _actual_identity(path: Path) -> dict[str, object] | None:
    if not path.exists() and not path.is_symlink():
        return None
    _no_links(path)
    require(path.is_file() and path.stat().st_nlink == 1, "baseline must be a regular file")
    return {"sha256": sha256_file(path), "size_bytes": path.stat().st_size}


def _check_baseline(policy: dict[str, object], delta: dict[str, object]) -> None:
    allocation = _protected(_under(policy["allocation_root"], ALLOCATION_ROOT))
    for relative, expected in delta["baseline"]["files"].items():
        path = _under(allocation / relative, allocation)
        if expected is not None:
            _protected(path)
        require(_actual_identity(path) == expected, "production baseline drift detected")


def _candidate_files(candidate: Path, delta: dict[str, object]) -> dict[str, dict[str, object]]:
    expected_names = {MANIFEST_NAME, *delta["payloads"]}
    actual = _file_set(candidate)
    require(set(actual) == expected_names, "candidate contains missing or extra files")
    for name, identity in delta["payloads"].items():
        observed = actual[name]
        require(observed["sha256"] == identity["sha256"]
                and observed["size_bytes"] == identity["size_bytes"],
                "candidate payload identity mismatch")
    return actual


def _receive_receipt(policy: dict[str, object], policy_sha: str,
                     delta: dict[str, object], candidate_files: dict[str, object]) -> tuple[dict[str, object], str]:
    path = _protected(_derived(EVIDENCE_ROOT, policy, delta["delta_id"], ".received.json"))
    receipt = read_json(path)
    _exact(receipt, {"schema_version", "status", "received_at_utc", "policy_id",
                     "policy_sha256", "delta_id", "domain",
                     "transfer_manifest_sha256", "candidate_path",
                     "candidate_files", "producer"}, "receive receipt")
    require(receipt["schema_version"] == "production-data-delta-receive/1"
            and receipt["status"] == "RECEIVED"
            and receipt["policy_id"] == policy["policy_id"]
            and receipt["policy_sha256"] == policy_sha
            and receipt["delta_id"] == delta["delta_id"]
            and receipt["domain"] == delta["domain"]
            and receipt["producer"] == policy["approved_producer"]
            and receipt["transfer_manifest_sha256"]
                == sha256_file(_derived(CANDIDATE_ROOT, policy, delta["delta_id"]) / MANIFEST_NAME)
            and receipt["candidate_path"]
                == str(_derived(CANDIDATE_ROOT, policy, delta["delta_id"]))
            and receipt["candidate_files"] == candidate_files,
            "receive receipt identity mismatch")
    return receipt, sha256_file(path)


def stage_upload(policy_path: str | Path, upload_dir: str | Path,
                 transfer_manifest_sha256: str) -> dict[str, object]:
    """Copy an untrusted transport drop into a new protected incoming directory.

    The upload namespace may be owned by the restricted transport account.  It
    is never consumed directly: this function validates a fixed file set twice,
    copies exclusively as root, and makes the sealed result world-readable but
    writable only by root for the later read-only worker mount.
    """
    policy, policy_sha = _load_policy(policy_path)
    require(HEX64.fullmatch(transfer_manifest_sha256) is not None,
            "transfer manifest SHA-256 is invalid")
    upload = _under(upload_dir, UPLOAD_ROOT)
    require(upload.is_dir(), "transport upload directory is missing")
    manifest = upload / MANIFEST_NAME
    require(sha256_file(manifest) == transfer_manifest_sha256,
            "transport manifest SHA-256 mismatch")
    delta = validate_delta_document(read_json(manifest))
    require(delta["domain"] == policy["domain"]
            and delta["producer"] == policy["approved_producer"],
            "transport delta differs from host policy")
    before = _file_set(upload)
    expected_names = {MANIFEST_NAME, *delta["payloads"]}
    require(set(before) == expected_names, "transport upload contains missing or extra files")
    for name, identity in delta["payloads"].items():
        require(before[name]["sha256"] == identity["sha256"]
                and before[name]["size_bytes"] == identity["size_bytes"],
                "transport payload identity mismatch")
    incoming = _derived(INCOMING_ROOT, policy, delta["delta_id"])
    _protected(incoming.parent)
    require(not incoming.exists() and not incoming.is_symlink(), "sealed incoming already exists")
    incoming.mkdir(mode=0o700)
    try:
        for name in sorted(expected_names):
            _write_exclusive(incoming / name, (upload / name).read_bytes())
        require(_file_set(upload) == before, "transport upload changed while sealing")
        require(_file_set(incoming) == before, "sealed incoming copy mismatch")
        _make_tree_readonly(incoming, public_read=True)
        _policy_unchanged(policy_path, policy_sha)
    except Exception:
        # Partial sealed input is retained and its delta_id cannot be reused.
        raise
    return {
        "schema_version": "production-data-delta-upload-stage/1", "status": "SEALED",
        "sealed_at_utc": datetime.now(timezone.utc).isoformat(),
        "policy_id": policy["policy_id"], "policy_sha256": policy_sha,
        "delta_id": delta["delta_id"], "domain": delta["domain"],
        "transfer_manifest_sha256": transfer_manifest_sha256,
        "incoming_path": str(incoming), "incoming_files": before,
    }


def receive(policy_path: str | Path, incoming_dir: str | Path,
            transfer_manifest_sha256: str) -> dict[str, object]:
    policy, policy_sha = _load_policy(policy_path)
    require(HEX64.fullmatch(transfer_manifest_sha256) is not None,
            "transfer manifest SHA-256 is invalid")
    incoming = _protected_tree(_protected(_under(incoming_dir, INCOMING_ROOT)))
    manifest_path = incoming / MANIFEST_NAME
    require(sha256_file(manifest_path) == transfer_manifest_sha256,
            "transfer manifest SHA-256 mismatch")
    delta = validate_delta_document(read_json(manifest_path))
    require(delta["domain"] == policy["domain"], "delta domain differs from policy")
    require(delta["producer"] == policy["approved_producer"],
            "delta producer differs from policy")
    _check_baseline(policy, delta)
    incoming_files = _file_set(incoming)
    expected_names = {MANIFEST_NAME, *delta["payloads"]}
    require(set(incoming_files) == expected_names, "incoming contains missing or extra files")
    for name, identity in delta["payloads"].items():
        require(incoming_files[name]["sha256"] == identity["sha256"]
                and incoming_files[name]["size_bytes"] == identity["size_bytes"],
                "incoming payload identity mismatch")

    candidate = _derived(CANDIDATE_ROOT, policy, delta["delta_id"])
    _protected(candidate.parent)
    require(not candidate.exists() and not candidate.is_symlink(), "candidate already exists")
    candidate.mkdir(mode=0o700)
    try:
        for name in sorted(expected_names):
            _write_exclusive(candidate / name, (incoming / name).read_bytes())
        require(_candidate_files(candidate, delta) == incoming_files,
                "candidate copy differs from incoming")
        _make_tree_readonly(candidate, public_read=True)
        _policy_unchanged(policy_path, policy_sha)
    except Exception:
        # The unique candidate is retained as evidence and can never be reused.
        raise
    receipt = {
        "schema_version": "production-data-delta-receive/1", "status": "RECEIVED",
        "received_at_utc": datetime.now(timezone.utc).isoformat(),
        "policy_id": policy["policy_id"], "policy_sha256": policy_sha,
        "delta_id": delta["delta_id"], "domain": delta["domain"],
        "transfer_manifest_sha256": transfer_manifest_sha256,
        "candidate_path": str(candidate), "candidate_files": incoming_files,
        "producer": policy["approved_producer"],
    }
    receipt_path = _derived(EVIDENCE_ROOT, policy, delta["delta_id"], ".received.json")
    _protected(receipt_path.parent)
    _write_json_exclusive(receipt_path, receipt)
    return {**receipt, "receipt_path": str(receipt_path),
            "receipt_sha256": sha256_file(receipt_path)}


def _inspect_image(image: dict[str, str]) -> dict[str, object]:
    raw = subprocess.check_output(["docker", "image", "inspect", image["image_id"]],
                                  stderr=subprocess.DEVNULL, timeout=30)
    # Docker returns an array. Reject duplicate keys and non-finite values just
    # like every other identity document.
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate image inspection field")
            result[key] = value
        return result
    try:
        items = json.loads(raw.decode("utf-8", errors="strict"),
                           object_pairs_hook=pairs,
                           parse_constant=lambda _value: (_ for _ in ()).throw(
                               DeltaError("non-finite image inspection value")))
    except (UnicodeError, json.JSONDecodeError):
        raise DeltaError("validation image inspection is invalid") from None
    require(type(items) is list and len(items) == 1 and type(items[0]) is dict,
            "validation image inspection is invalid")
    info = items[0]
    labels = (info.get("Config") or {}).get("Labels") or {}
    require(info.get("Id") == image["image_id"], "validation image ID mismatch")
    require(labels.get("org.opencontainers.image.revision") == image["commit"]
            and labels.get("market-data.git.tree") == image["tree"],
            "validation image Git identity mismatch")
    return {"image_id": info["Id"], "commit": image["commit"], "tree": image["tree"]}


def _worker(policy: dict[str, object], candidate: Path) -> dict[str, object]:
    image = policy["validation_image"]
    before = _inspect_image(image)
    name = "production-data-delta-" + uuid.uuid4().hex
    source_dir = ROOT / "09_deploy" / "production_data_delivery"
    allocation = Path(policy["allocation_root"])
    command = [
        "docker", "run", "--rm", "--name", name, "--pull", "never",
        "--network", "none", "--read-only", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--user", "65532:65532",
        "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m",
        "--mount", f"type=bind,src={candidate},dst=/candidate,readonly",
        "--mount", f"type=bind,src={allocation},dst=/allocation,readonly",
        "--mount", f"type=bind,src={source_dir},dst=/opt/production-data-delivery,readonly",
        "--entrypoint", "python", image["image_id"], "-B",
        "/opt/production-data-delivery/activate_production_data_delta.py",
        "_worker", "--domain", str(policy["domain"]),
        "--expected-commit", image["commit"], "--expected-tree", image["tree"],
    ]
    try:
        completed = subprocess.run(command, capture_output=True, timeout=300, check=False)
        require(completed.returncode == 0, "semantic validation worker failed")
        result = strict_json_bytes(completed.stdout)
        require(result.get("schema_version") == "production-data-delta-worker/1"
                and result.get("status") == "PASS" and result.get("domain") == policy["domain"],
                "semantic validation worker result is invalid")
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60,
                       check=False)
        listing = subprocess.check_output(
            ["docker", "container", "ls", "-aq", "--filter", f"name=^/{name}$"],
            stderr=subprocess.DEVNULL, timeout=30).strip()
        require(listing == b"", "semantic validation worker was not removed")
    require(_inspect_image(image) == before, "validation image identity changed")
    require(_git_identity(ROOT) == policy["approved_producer"],
            "source clone changed during semantic validation")
    return result


def validate(policy_path: str | Path, delta_id: str) -> dict[str, object]:
    policy, policy_sha = _load_policy(policy_path)
    candidate = _protected_tree(_protected(_derived(CANDIDATE_ROOT, policy, delta_id)))
    delta = validate_delta_document(read_json(candidate / MANIFEST_NAME))
    require(delta["delta_id"] == delta_id and delta["domain"] == policy["domain"],
            "candidate identity differs from request")
    before = _candidate_files(candidate, delta)
    _received, receive_sha = _receive_receipt(policy, policy_sha, delta, before)
    _check_baseline(policy, delta)
    report_path = _derived(EVIDENCE_ROOT, policy, delta_id, ".validation.json")
    _protected(report_path.parent)
    generated = datetime.now(timezone.utc).isoformat()
    try:
        semantic = _worker(policy, candidate)
        _check_baseline(policy, delta)
        require(_candidate_files(candidate, delta) == before,
                "candidate changed during semantic validation")
        _policy_unchanged(policy_path, policy_sha)
        report = {
            "schema_version": "production-data-delta-validation/1", "status": "PASS",
            "validated_at_utc": generated, "policy_id": policy["policy_id"],
            "policy_sha256": policy_sha, "delta_id": delta_id,
            "domain": delta["domain"], "producer": policy["approved_producer"],
            "validation_image": policy["validation_image"],
            "transfer_manifest_sha256": sha256_file(candidate / MANIFEST_NAME),
            "receive_receipt_sha256": receive_sha,
            "candidate_files": before, "semantic": semantic,
        }
    except Exception as exc:
        report = {
            "schema_version": "production-data-delta-validation/1", "status": "FAIL",
            "validated_at_utc": generated, "policy_id": policy["policy_id"],
            "policy_sha256": policy_sha, "delta_id": delta_id,
            "domain": policy["domain"], "producer": policy["approved_producer"],
            "validation_image": policy["validation_image"],
            "transfer_manifest_sha256": sha256_file(candidate / MANIFEST_NAME),
            "receive_receipt_sha256": receive_sha,
            "candidate_files": before, "error_type": type(exc).__name__,
        }
        _write_json_exclusive(report_path, report)
        raise
    _write_json_exclusive(report_path, report)
    return {**report, "report_path": str(report_path),
            "report_sha256": sha256_file(report_path)}


def _copy_tree(source: Path, destination: Path) -> None:
    require(not destination.exists() and not destination.is_symlink(),
            "staging or backup destination already exists")
    if source.exists():
        _no_links(source)
        _file_set(source)
        shutil.copytree(source, destination, symlinks=False)
    else:
        destination.mkdir(parents=True)
    _file_set(destination)


def _atomic_copy(source: Path, target: Path) -> None:
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        _write_exclusive(temporary, source.read_bytes())
        require(sha256_file(temporary) == sha256_file(source), "staged file copy mismatch")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _renameat2(source: Path, target: Path, flag: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    function = libc.renameat2
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                         ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    if function(-100, os.fsencode(source), -100, os.fsencode(target), flag) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(target))


def _status_document(domain: str, delta: dict[str, object], identities: dict[str, object],
                     published_at: str, initialized: bool,
                     semantic: dict[str, object]) -> dict[str, object]:
    metadata = delta["domain_metadata"]
    status = "initialized" if initialized else "updated"
    if domain == "canada_canola":
        return {"schema_version": "canada-canola-publish-status/1", "status": status,
                "published": True, "published_at_utc": published_at,
                "delta_id": delta["delta_id"], "git_head": delta["producer"]["commit"],
                "stable_sha256": identities[CANOLA_STABLE]["sha256"],
                "record_count": semantic["record_count"], "latest_dates": semantic["latest_dates"],
                "added": semantic["added"], "revised": semantic["revised"]}
    if domain == "soybean_export_sales":
        stable = identities[FAS_STABLE]
        last_success = {
            "batch_id": metadata["batch_id"], "completed_at_utc": published_at,
            "status": status, "stable_path": FAS_STABLE,
            "stable_manifest_path": FAS_MANIFEST,
            "stable_sha256": stable["sha256"].upper(),
            "source_latest_week": metadata["source_latest_week"],
            "source_release_time_raw": metadata["source_release_time_raw"],
        }
        return {
            "schema_version": 1, "source": "usda_fas_esr",
            "batch_id": metadata["batch_id"], "git_head": delta["producer"]["commit"],
            "status": status, "published": True, "backup_dir": None,
            "raw_snapshot_dir": None, "candidate_path": None,
            "stable_path": FAS_STABLE, "revision_count": 0,
            "validation": {"passed": True, "row_count": stable["row_count"],
                           "latest_week": metadata["source_latest_week"]},
            "latest_attempt": {"batch_id": metadata["batch_id"],
                               "completed_at_utc": published_at, "status": status,
                               "published": True}, "last_success": last_success,
        }
    changes = semantic["business_changes"]
    old_max = [value for value in semantic["old_max_week"].values() if value is not None]
    new_max = [value for value in semantic["new_max_week"].values() if value is not None]
    return {
        "status": status, "started_at_utc": delta["generated_at_utc"],
        "finished_at_utc": published_at, "current_year": metadata["current_year"],
        "old_latest_week": max(old_max) if old_max else None,
        "new_latest_week": max(new_max) if new_max else None,
        "progress_old_rows": semantic["progress_old_rows"],
        "progress_new_rows": identities[CROP_PROGRESS]["row_count"],
        "condition_old_rows": semantic["condition_old_rows"],
        "condition_new_rows": identities[CROP_CONDITION]["row_count"],
        "added_records": changes["progress"]["added"] + changes["condition"]["added"],
        "corrected_records": changes["progress"]["corrected"] + changes["condition"]["corrected"],
        "deleted_records": changes["progress"]["deleted"] + changes["condition"]["deleted"],
        "stable_files_initialized": initialized, "raw_path": metadata["raw_snapshot"],
        "manifest_path": None, "processed_progress_path": CROP_PROGRESS,
        "processed_condition_path": CROP_CONDITION,
        "processed_sha256": {"progress": identities[CROP_PROGRESS]["sha256"],
                             "condition": identities[CROP_CONDITION]["sha256"]},
        "git_head": delta["producer"]["commit"], "run_mode": "host_delta_publish",
        "business_change_found": semantic["business_changed"], "published": True,
        "recommended_to_publish": True, "error": None,
    }


def _fas_manifest(delta: dict[str, object], identity: dict[str, object],
                  published_at: str) -> dict[str, object]:
    metadata = delta["domain_metadata"]
    return {
        **identity, "sha256": identity["sha256"].upper(),
        "parquet_schema_sha256": identity["parquet_schema_sha256"].upper(),
        "path": FAS_STABLE, "schema_version": 1,
        "source": "usda_fas_esr", "generated_at_utc": published_at,
        "fetch_started_at_utc": delta["generated_at_utc"],
        "fetch_completed_at_utc": delta["generated_at_utc"],
        "batch_id": metadata["batch_id"],
        "source_latest_week": metadata["source_latest_week"],
        "source_release_time_raw": metadata["source_release_time_raw"],
        "source_release_timezone": "America/New_York", "source_dataset_updated_at": None,
        "stable_relative_path": FAS_STABLE,
        "raw_snapshot_id": metadata["batch_id"],
        "raw_snapshot_sha256": metadata["raw_snapshot_sha256"].upper(),
        "raw_manifest_sha256": metadata["raw_manifest_sha256"].upper(),
        "git_head": delta["producer"]["commit"], "generator_version": 1,
        "fetch_scope": metadata["fetch_scope"],
        "revision_coverage": {"mode": "candidate_vs_current",
                              "report_market_years": metadata["fetch_scope"]["report_market_years"]},
        "quality_status": "passed", "quality": {"passed": True,
            "row_count": identity["row_count"], "latest_week": metadata["source_latest_week"]},
        "stable_path": FAS_STABLE, "published_batch_id": metadata["batch_id"],
    }


def _load_pass_report(path: Path, expected_sha: str, policy: dict[str, object],
                      policy_sha: str, delta: dict[str, object]) -> dict[str, object]:
    require(HEX64.fullmatch(expected_sha) is not None, "validation report SHA-256 is invalid")
    selected = _protected(_under(path, EVIDENCE_ROOT))
    require(sha256_file(selected) == expected_sha, "validation report SHA-256 mismatch")
    report = read_json(selected)
    _exact(report, {"schema_version", "status", "validated_at_utc", "policy_id",
                    "policy_sha256", "delta_id", "domain", "producer",
                    "validation_image", "transfer_manifest_sha256",
                    "receive_receipt_sha256", "candidate_files", "semantic"},
           "validation report")
    require(report.get("schema_version") == "production-data-delta-validation/1"
            and report.get("status") == "PASS", "PASS validation report required")
    require(report.get("policy_id") == policy["policy_id"]
            and report.get("policy_sha256") == policy_sha
            and report.get("delta_id") == delta["delta_id"]
            and report.get("domain") == delta["domain"]
            and report.get("producer") == policy["approved_producer"]
            and report.get("validation_image") == policy["validation_image"]
            and report.get("transfer_manifest_sha256")
                == sha256_file(_derived(CANDIDATE_ROOT, policy, delta["delta_id"]) / MANIFEST_NAME),
            "validation report identity mismatch")
    _received, receive_sha = _receive_receipt(policy, policy_sha, delta,
                                              report["candidate_files"])
    require(report["receive_receipt_sha256"] == receive_sha,
            "validation report receive chain mismatch")
    return report


def _lock(allocation: Path):
    import fcntl
    lock_path = allocation / ".production-data-delta.lock"
    handle = lock_path.open("a+b")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    return handle


def publish(policy_path: str | Path, delta_id: str, validation_report_path: str | Path,
            validation_report_sha256: str) -> dict[str, object]:
    policy, policy_sha = _load_policy(policy_path)
    candidate = _protected_tree(_protected(_derived(CANDIDATE_ROOT, policy, delta_id)))
    delta = validate_delta_document(read_json(candidate / MANIFEST_NAME))
    require(delta["delta_id"] == delta_id and delta["domain"] == policy["domain"],
            "candidate identity differs from publication request")
    report = _load_pass_report(Path(validation_report_path), validation_report_sha256,
                               policy, policy_sha, delta)
    require(report["candidate_files"] == _candidate_files(candidate, delta),
            "candidate differs from validation report")
    allocation = _protected(_under(policy["allocation_root"], ALLOCATION_ROOT))
    with _lock(allocation):
        _check_baseline(policy, delta)
        contract = DOMAIN_CONTRACTS[delta["domain"]]
        formal = _under(allocation / contract["domain_dir"], allocation)
        status_path = _under(allocation / contract["status_path"], allocation)
        if formal.exists():
            _protected_tree(formal)
        if status_path.exists():
            _protected(status_path)
        formal_before = _file_set(formal) if formal.exists() else None
        initialized = not formal.exists()
        existing_payloads = {
            target: _actual_identity(allocation / target)
            for target in contract["payloads"].values()
        }
        semantic_changed = report["semantic"]["observations"].get("business_changed")
        require(type(semantic_changed) is bool, "validation report lacks business comparison")
        require(delta["run"]["business_status"] != "no_change" or semantic_changed is False,
                "no-change producer status differs from semantic validation")
        require(delta["run"]["business_status"] != "updated" or semantic_changed is True,
                "updated producer status differs from semantic validation")
        if delta["domain"] == "canada_canola":
            require((delta["run"]["business_status"] == "initialized")
                    == (delta["baseline"]["files"][CANOLA_STABLE] is None),
                    "canola initialization status differs from formal baseline")
        if delta["run"]["business_status"] == "no_change" or all(existing_payloads[target] is not None
               and existing_payloads[target]["sha256"] == delta["payloads"][name]["sha256"]
               for name, target in contract["payloads"].items()):
            no_change_paths = {contract["status_path"], *contract["payloads"].values()}
            if delta["domain"] == "soybean_export_sales":
                no_change_paths.add(FAS_MANIFEST)
            formal_files = {path: _actual_identity(allocation / path)
                            for path in no_change_paths}
            require(all(identity is not None for identity in formal_files.values()),
                    "NO_CHANGE formal file set is incomplete")
            _check_baseline(policy, delta)
            require((_file_set(formal) if formal.exists() else None) == formal_before,
                    "production domain changed before NO_CHANGE receipt")
            require(_candidate_files(candidate, delta) == report["candidate_files"],
                    "candidate changed before NO_CHANGE receipt")
            _policy_unchanged(policy_path, policy_sha)
            require(_git_identity(ROOT) == policy["approved_producer"],
                    "source clone changed before NO_CHANGE receipt")
            receipt = {
                "schema_version": "production-data-delta-publication/1", "status": "NO_CHANGE",
                "published_at_utc": datetime.now(timezone.utc).isoformat(),
                "policy_id": policy["policy_id"], "policy_sha256": policy_sha,
                "delta_id": delta_id, "domain": delta["domain"],
                "validation_report_sha256": validation_report_sha256,
                "formal_files": formal_files, "backup_path": None,
            }
            receipt_path = _derived(EVIDENCE_ROOT, policy, delta_id, ".publication.json")
            _write_json_exclusive(receipt_path, receipt)
            return {**receipt, "receipt_path": str(receipt_path),
                    "receipt_sha256": sha256_file(receipt_path)}

        token = uuid.uuid4().hex
        stage = formal.parent / f".{formal.name}.publishing-{token}"
        backup = _derived(BACKUP_ROOT, policy, delta_id)
        _protected(backup.parent)
        require(not backup.exists() and not backup.is_symlink(), "backup already exists")
        backup.mkdir(mode=0o700)
        if formal.exists():
            _copy_tree(formal, backup / "domain")
        if status_path.exists():
            _atomic_copy(status_path, backup / "status.json")
        backup_files = _file_set(backup)
        _make_tree_readonly(backup)
        _protected_tree(backup)
        _copy_tree(formal, stage)
        identities = {}
        for name, target in contract["payloads"].items():
            destination = stage / Path(target).name
            _atomic_copy(candidate / name, destination)
            identities[target] = {**delta["payloads"][name]}
        published_at = datetime.now(timezone.utc).isoformat()
        if delta["domain"] == "soybean_export_sales":
            _atomic_json(stage / Path(FAS_MANIFEST).name,
                         _fas_manifest(delta, identities[FAS_STABLE], published_at))
            identities[FAS_MANIFEST] = _actual_identity(stage / Path(FAS_MANIFEST).name)
        new_status = _status_document(
            delta["domain"], delta, identities, published_at,
            initialized or all(value is None for value in existing_payloads.values()),
            report["semantic"]["observations"])
        staged_files = _file_set(stage)
        swapped = False
        old_status = status_path.read_bytes() if status_path.exists() else None
        try:
            _check_baseline(policy, delta)
            require((_file_set(formal) if formal.exists() else None) == formal_before,
                    "production domain changed while staging publication")
            require(_candidate_files(candidate, delta) == report["candidate_files"],
                    "candidate changed while staging publication")
            require(_file_set(backup) == backup_files,
                    "publication backup changed while staging publication")
            require(sha256_file(_protected(_under(validation_report_path, EVIDENCE_ROOT)))
                    == validation_report_sha256,
                    "validation report changed while staging publication")
            _policy_unchanged(policy_path, policy_sha)
            require(_git_identity(ROOT) == policy["approved_producer"],
                    "source clone changed before publication")
            if formal.exists():
                _renameat2(stage, formal, 2)  # RENAME_EXCHANGE
            else:
                _renameat2(stage, formal, 1)  # RENAME_NOREPLACE
            swapped = True
            _atomic_json(status_path, new_status)
            require(_file_set(formal) == staged_files,
                    "formal domain differs after publication exchange")
            for name, target in contract["payloads"].items():
                require(_actual_identity(allocation / target)["sha256"] == delta["payloads"][name]["sha256"],
                        "formal payload identity mismatch after publication")
            require(_file_set(backup) == backup_files,
                    "publication backup changed during publication")
            require(_git_identity(ROOT) == policy["approved_producer"],
                    "source clone changed during publication")
            _policy_unchanged(policy_path, policy_sha)
            receipt = {
                "schema_version": "production-data-delta-publication/1", "status": "PUBLISHED",
                "published_at_utc": published_at, "policy_id": policy["policy_id"],
                "policy_sha256": policy_sha, "delta_id": delta_id, "domain": delta["domain"],
                "validation_report_sha256": validation_report_sha256,
                "formal_files": {path: _actual_identity(allocation / path)
                                 for path in (*contract["payloads"].values(),
                                              *(() if delta["domain"] != "soybean_export_sales" else (FAS_MANIFEST,)),
                                              contract["status_path"])},
                "backup_path": str(backup),
                "backup_files": backup_files,
                "displaced_path": None if initialized else str(stage),
                "previous_domain_present": not initialized,
            }
            receipt_path = _derived(EVIDENCE_ROOT, policy, delta_id, ".publication.json")
            _write_json_exclusive(receipt_path, receipt)
        except Exception as publication_error:
            rollback_errors = []
            if swapped:
                try:
                    if initialized:
                        failed = formal.parent / f".{formal.name}.failed-{token}"
                        os.replace(formal, failed)
                    else:
                        _renameat2(stage, formal, 2)
                        failed = stage.with_name(f".{formal.name}.failed-{token}")
                        os.replace(stage, failed)
                except Exception as exc:
                    rollback_errors.append("domain:" + type(exc).__name__)
                try:
                    # Restore regardless of whether _atomic_json returned: an
                    # fsync failure can happen after os.replace succeeded.
                    if old_status is None:
                        status_path.unlink(missing_ok=True)
                    else:
                        temporary = status_path.with_name(f".{status_path.name}.{token}.restore")
                        _write_exclusive(temporary, old_status)
                        os.replace(temporary, status_path)
                except Exception as exc:
                    rollback_errors.append("status:" + type(exc).__name__)
            if rollback_errors:
                broken = {
                    "schema_version": "production-data-delta-broken/1", "status": "BROKEN",
                    "failed_at_utc": datetime.now(timezone.utc).isoformat(),
                    "policy_id": policy["policy_id"], "policy_sha256": policy_sha,
                    "delta_id": delta_id, "domain": delta["domain"],
                    "error_type": type(publication_error).__name__,
                    "rollback_errors": rollback_errors, "backup_path": str(backup),
                }
                broken_path = _derived(EVIDENCE_ROOT, policy, delta_id, ".broken.json")
                _write_json_exclusive(broken_path, broken)
                raise DeltaError("publication failed and rollback was incomplete") from publication_error
            raise
        # With EXCHANGE, stage is the displaced old domain. Retain it alongside
        # the immutable backup as rollback evidence; successful cleanup is not a
        # condition of publication.
        return {**receipt, "receipt_path": str(receipt_path),
                "receipt_sha256": sha256_file(receipt_path)}


def rollback(policy_path: str | Path, publication_receipt_path: str | Path,
             publication_receipt_sha256: str) -> dict[str, object]:
    policy, policy_sha = _load_policy(policy_path)
    require(HEX64.fullmatch(publication_receipt_sha256) is not None,
            "publication receipt SHA-256 is invalid")
    receipt_path = _protected(_under(publication_receipt_path, EVIDENCE_ROOT))
    require(sha256_file(receipt_path) == publication_receipt_sha256,
            "publication receipt SHA-256 mismatch")
    publication = read_json(receipt_path)
    _exact(publication, {"schema_version", "status", "published_at_utc",
                         "policy_id", "policy_sha256", "delta_id", "domain",
                         "validation_report_sha256", "formal_files", "backup_path",
                         "backup_files", "displaced_path", "previous_domain_present"},
           "publication receipt")
    require(publication.get("schema_version") == "production-data-delta-publication/1"
            and publication.get("status") == "PUBLISHED"
            and publication.get("policy_id") == policy["policy_id"]
            and publication.get("policy_sha256") == policy_sha
            and publication.get("domain") == policy["domain"],
            "current PUBLISHED receipt required for rollback")
    delta_id = publication.get("delta_id")
    require(type(delta_id) is str and SAFE_ID.fullmatch(delta_id), "rollback delta identity is invalid")
    backup = _protected_tree(_protected(_under(publication["backup_path"], BACKUP_ROOT)))
    require(backup == _derived(BACKUP_ROOT, policy, delta_id), "rollback backup identity mismatch")
    require(type(publication["backup_files"]) is dict
            and _file_set(backup) == publication["backup_files"],
            "rollback backup bytes differ from publication receipt")
    allocation = _protected(_under(policy["allocation_root"], ALLOCATION_ROOT))
    contract = DOMAIN_CONTRACTS[policy["domain"]]
    expected_formal = {contract["status_path"], *contract["payloads"].values()}
    if policy["domain"] == "soybean_export_sales":
        expected_formal.add(FAS_MANIFEST)
    require(type(publication["formal_files"]) is dict
            and set(publication["formal_files"]) == expected_formal,
            "publication receipt formal file set is invalid")
    for identity in publication["formal_files"].values():
        _file_identity(identity)
    formal = _under(allocation / contract["domain_dir"], allocation)
    status_path = _under(allocation / contract["status_path"], allocation)
    _protected_tree(formal)
    _protected(status_path)
    with _lock(allocation):
        for relative, identity in publication["formal_files"].items():
            require(_actual_identity(allocation / relative) == identity,
                    "current formal data differs from publication receipt")
        previous_present = publication["previous_domain_present"]
        require(type(previous_present) is bool, "publication prior-domain state is invalid")
        require((backup / "domain").is_dir() == previous_present,
                "rollback domain backup state is invalid")
        formal_before = _file_set(formal)
        token = uuid.uuid4().hex
        stage = formal.parent / f".{formal.name}.rollback-{token}"
        if previous_present:
            _copy_tree(backup / "domain", stage)
            restored_domain_files = _file_set(stage)
        else:
            restored_domain_files = {}
        restored_status = (_actual_identity(backup / "status.json")
                           if (backup / "status.json").exists() else None)
        old_status = status_path.read_bytes() if status_path.exists() else None
        exchanged = False
        try:
            require(_file_set(formal) == formal_before,
                    "current production domain changed while staging rollback")
            require(_file_set(backup) == publication["backup_files"],
                    "rollback backup changed while staging rollback")
            _policy_unchanged(policy_path, policy_sha)
            require(_git_identity(ROOT) == policy["approved_producer"],
                    "source clone changed before rollback")
            if previous_present:
                _renameat2(stage, formal, 2)
            else:
                os.replace(formal, stage)
            exchanged = True
            if (backup / "status.json").exists():
                _atomic_copy(backup / "status.json", status_path)
            else:
                status_path.unlink(missing_ok=True)
            require((_file_set(formal) if previous_present else {}) == restored_domain_files,
                    "rollback domain differs after exchange")
            require(_actual_identity(status_path) == restored_status,
                    "rollback status differs after restoration")
            require(_git_identity(ROOT) == policy["approved_producer"],
                    "source clone changed during rollback")
            _policy_unchanged(policy_path, policy_sha)
            result = {
                "schema_version": "production-data-delta-rollback/1", "status": "ROLLED_BACK",
                "rolled_back_at_utc": datetime.now(timezone.utc).isoformat(),
                "policy_id": policy["policy_id"], "policy_sha256": policy_sha,
                "delta_id": delta_id, "domain": policy["domain"],
                "publication_receipt_sha256": publication_receipt_sha256,
                "restored_files": _file_set(formal) if previous_present else {},
                "displaced_path": str(stage),
            }
            result_path = _derived(EVIDENCE_ROOT, policy, delta_id, ".rollback.json")
            _write_json_exclusive(result_path, result)
        except Exception as rollback_error:
            restoration_errors = []
            if exchanged:
                try:
                    if previous_present:
                        _renameat2(stage, formal, 2)
                    else:
                        os.replace(stage, formal)
                except Exception as exc:
                    restoration_errors.append("domain:" + type(exc).__name__)
                try:
                    if old_status is None:
                        status_path.unlink(missing_ok=True)
                    else:
                        temporary = status_path.with_name(f".{status_path.name}.{token}.restore")
                        _write_exclusive(temporary, old_status)
                        os.replace(temporary, status_path)
                except Exception as exc:
                    restoration_errors.append("status:" + type(exc).__name__)
            if restoration_errors:
                broken = {
                    "schema_version": "production-data-delta-broken/1", "status": "BROKEN",
                    "failed_at_utc": datetime.now(timezone.utc).isoformat(),
                    "operation": "rollback", "policy_id": policy["policy_id"],
                    "policy_sha256": policy_sha, "delta_id": delta_id,
                    "domain": policy["domain"],
                    "error_type": type(rollback_error).__name__,
                    "restoration_errors": restoration_errors,
                    "backup_path": str(backup), "displaced_path": str(stage),
                }
                broken_path = _derived(EVIDENCE_ROOT, policy, delta_id,
                                       ".rollback-broken.json")
                _write_json_exclusive(broken_path, broken)
                raise DeltaError("rollback failed and restoration was incomplete") from rollback_error
            raise
        # Stage intentionally retains the displaced published domain as evidence.
        return {**result, "receipt_path": str(result_path),
                "receipt_sha256": sha256_file(result_path)}


def _schema_fingerprint(path: Path) -> str:
    import pyarrow.parquet as pq
    schema = pq.read_schema(path).remove_metadata()
    return sha256_bytes(schema.to_string().encode("utf-8"))


def _safe_worker_value(value: object, pandas_module) -> object:
    """Convert known dataframe scalars without stringifying unknown objects."""
    if value is None or type(value) in {str, bool, int}:
        return value
    if type(value) is float:
        require(math.isfinite(value), "worker observation contains a non-finite number")
        return value
    if isinstance(value, (datetime, date)):
        if pandas_module.isna(value):
            return None
        return value.isoformat()
    if value is pandas_module.NA or value is pandas_module.NaT:
        return None
    if type(value) is dict:
        require(all(type(key) is str for key in value),
                "worker observation contains a non-string key")
        return {key: _safe_worker_value(item, pandas_module)
                for key, item in value.items()}
    if type(value) in {list, tuple}:
        return [_safe_worker_value(item, pandas_module) for item in value]
    if hasattr(value, "item") and type(value).__module__.startswith("numpy"):
        return _safe_worker_value(value.item(), pandas_module)
    raise DeltaError("worker observation contains an unsupported value")


def worker_validate(domain: str, expected_commit: str, expected_tree: str) -> dict[str, object]:
    require(domain in DOMAIN_CONTRACTS, "worker domain is invalid")
    release = read_json("/app/RELEASE.json")
    require(release.get("git_commit") == expected_commit and release.get("git_tree") == expected_tree,
            "worker RELEASE identity mismatch")
    require(os.environ.get("MARKET_DATA_GIT_HEAD") == expected_commit
            and os.environ.get("MARKET_DATA_GIT_TREE") == expected_tree,
            "worker embedded Git identity mismatch")
    candidate = Path("/candidate")
    delta = validate_delta_document(read_json(candidate / MANIFEST_NAME))
    require(delta["domain"] == domain, "worker delta domain mismatch")
    sys.path.insert(0, "/app/03_src")
    import pandas as pd
    observations = {}
    if domain == "canada_canola":
        baseline = (Path("/allocation") / CANOLA_STABLE
                    if delta["baseline"]["files"][CANOLA_STABLE] is not None else None)
        observations = _canola_observations(
            candidate / "canola_weekly.json", baseline,
            read_json(candidate / "source_evidence.json"), delta["domain_metadata"]["revision_keys"])
        require(observations["record_count"] == delta["domain_metadata"]["record_count"]
                and observations["latest_dates"] == delta["domain_metadata"]["latest_dates"],
                "canola candidate metadata mismatch")
    elif domain == "soybean_export_sales":
        from agri_research_agent.soybean_exports.fas import (FAS_KEY, FAS_STABLE_COLUMNS,
                                                             _business_equal, validate_fas_stable)
        name = "soybean_export_sales_weekly.parquet"
        frame = pd.read_parquet(candidate / name)
        require(tuple(frame.columns) == FAS_STABLE_COLUMNS, "FAS Parquet columns mismatch")
        require(not frame.duplicated(list(FAS_KEY)).any(), "FAS business key is not unique")
        quality = validate_fas_stable(frame)
        metadata = delta["domain_metadata"]
        require(quality["latest_week"] == metadata["source_latest_week"], "FAS latest week mismatch")
        require(set(frame["batch_id"].astype(str)) == {metadata["batch_id"]}
                and set(frame["source"].astype(str)) == {"usda_fas_esr"}
                and set(frame["raw_snapshot_sha256"].str.lower()) == {metadata["raw_snapshot_sha256"]},
                "FAS row provenance mismatch")
        baseline = (Path("/allocation") / FAS_STABLE
                    if delta["baseline"]["files"][FAS_STABLE] is not None else None)
        business_changed = baseline is None or not _business_equal(pd.read_parquet(baseline), frame)
        observations = {**quality, "business_changed": business_changed}
    else:
        from agri_research_agent.pipelines.soybean_crop_progress import CROP_STABLE_KEY, CROP_WEEKLY_COLUMNS
        from agri_research_agent.pipelines.soybean_crop_weekly_update import (compare_business_records,
                                                                              validate_candidate_pair)
        progress = pd.read_parquet(candidate / "soybeans_crop_progress_weekly.parquet")
        condition = pd.read_parquet(candidate / "soybeans_crop_condition_weekly.parquet")
        require(list(progress.columns) == CROP_WEEKLY_COLUMNS
                and list(condition.columns) == CROP_WEEKLY_COLUMNS, "crop Parquet columns mismatch")
        require(not progress.duplicated(CROP_STABLE_KEY).any()
                and not condition.duplicated(CROP_STABLE_KEY).any(), "crop stable key is not unique")
        baseline_files = delta["baseline"]["files"]
        progress_base = CROP_PROGRESS if baseline_files[CROP_PROGRESS] is not None else CROP_LEGACY_PROGRESS
        condition_base = CROP_CONDITION if baseline_files[CROP_CONDITION] is not None else CROP_LEGACY_CONDITION
        baseline_progress = pd.read_parquet(Path("/allocation") / progress_base)
        baseline_condition = pd.read_parquet(Path("/allocation") / condition_base)
        metadata = delta["domain_metadata"]
        observations = validate_candidate_pair(
            baseline_progress=baseline_progress, baseline_condition=baseline_condition,
            candidate_progress=progress, candidate_condition=condition,
            current_year=metadata["current_year"], retrieved_at_utc=metadata["retrieved_at_utc"],
            raw_snapshot=metadata["raw_snapshot"], duplicate_counts=metadata["duplicate_counts"],
            display_config=Path("/app/02_configs/soybean_crop_progress_display.yaml"))
        progress_change = compare_business_records(baseline_progress, progress)
        condition_change = compare_business_records(baseline_condition, condition)
        observations = {**observations,
                        "business_changed": bool(progress_change["business_changed"]
                                                 or condition_change["business_changed"]),
                        "business_changes": {"progress": progress_change,
                                             "condition": condition_change},
                        "progress_old_rows": len(baseline_progress),
                        "condition_old_rows": len(baseline_condition)}
    for name, identity in delta["payloads"].items():
        path = candidate / name
        require(sha256_file(path) == identity["sha256"] and path.stat().st_size == identity["size_bytes"],
                "worker payload byte identity mismatch")
        if domain != "canada_canola":
            require(_schema_fingerprint(path) == identity["parquet_schema_sha256"],
                    "worker Parquet schema identity mismatch")
            import pyarrow.parquet as pq
            require(pq.read_metadata(path).num_rows == identity["row_count"],
                    "worker Parquet row count mismatch")
    observations = _safe_worker_value(observations, pd)
    return {"schema_version": "production-data-delta-worker/1", "status": "PASS",
            "domain": domain, "image": {"commit": expected_commit, "tree": expected_tree},
            "observations": observations}


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    stage_cmd = commands.add_parser("stage-upload")
    stage_cmd.add_argument("--policy", type=Path, required=True)
    stage_cmd.add_argument("--upload", type=Path, required=True)
    stage_cmd.add_argument("--manifest-sha256", required=True)
    receive_cmd = commands.add_parser("receive")
    receive_cmd.add_argument("--policy", type=Path, required=True)
    receive_cmd.add_argument("--incoming", type=Path, required=True)
    receive_cmd.add_argument("--manifest-sha256", required=True)
    validate_cmd = commands.add_parser("validate")
    validate_cmd.add_argument("--policy", type=Path, required=True)
    validate_cmd.add_argument("--delta-id", required=True)
    publish_cmd = commands.add_parser("publish")
    publish_cmd.add_argument("--policy", type=Path, required=True)
    publish_cmd.add_argument("--delta-id", required=True)
    publish_cmd.add_argument("--validation-report", type=Path, required=True)
    publish_cmd.add_argument("--validation-sha256", required=True)
    rollback_cmd = commands.add_parser("rollback")
    rollback_cmd.add_argument("--policy", type=Path, required=True)
    rollback_cmd.add_argument("--publication-receipt", type=Path, required=True)
    rollback_cmd.add_argument("--publication-sha256", required=True)
    worker_cmd = commands.add_parser("_worker")
    worker_cmd.add_argument("--domain", required=True)
    worker_cmd.add_argument("--expected-commit", required=True)
    worker_cmd.add_argument("--expected-tree", required=True)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "stage-upload":
            result = stage_upload(args.policy, args.upload, args.manifest_sha256)
        elif args.command == "receive":
            result = receive(args.policy, args.incoming, args.manifest_sha256)
        elif args.command == "validate":
            result = validate(args.policy, args.delta_id)
        elif args.command == "publish":
            result = publish(args.policy, args.delta_id, args.validation_report,
                             args.validation_sha256)
        elif args.command == "rollback":
            result = rollback(args.policy, args.publication_receipt,
                              args.publication_sha256)
        else:
            result = worker_validate(args.domain, args.expected_commit, args.expected_tree)
        print(canonical_json_bytes(result).decode("utf-8"), end="")
        return 0
    except Exception as exc:
        print(canonical_json_bytes({"status": "FAILED", "error_type": type(exc).__name__}).decode("utf-8"), end="")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
