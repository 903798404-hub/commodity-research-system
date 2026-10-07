"""Manual candidate preparation and guarded local activation for Canadian canola."""
from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

from filelock import FileLock
import requests

from agri_research_agent.shared.atomic_storage import atomic_write_bytes, atomic_write_json
from agri_research_agent.shared.runtime_context import RuntimeContext, RuntimeMode, assert_runtime_write
from .canada_canola import (
    STABLE_RELATIVE_PATH, check_source_url, import_workbook, load_bundle,
    merge_observations, sha256_file, strict_json, utc_now, validate_bundle,
)


def candidate_from_workbook(context: RuntimeContext, path: Path) -> Path:
    bundle = import_workbook(path)
    candidate = save_candidate(context, bundle, baseline=None,
                               changes={"initial_import": len(bundle["records"])})
    raw = assert_runtime_write(context, candidate.parent / "source.xlsx")
    shutil.copyfile(path, raw)
    if sha256_file(raw) != bundle["records"][0]["source_sha256"]:
        raise ValueError("workbook snapshot changed during copying")
    return candidate


def fetch_report(context: RuntimeContext, province: str, url: str) -> Path:
    """Archive an explicitly selected official report. No provider scheduling."""
    check_source_url(province, url)
    directory = assert_runtime_write(context, context.runtime_root / "raw/canada_canola" / uuid.uuid4().hex)
    directory.mkdir(parents=True, exist_ok=False)
    original_url = url
    # Validate redirects before requesting the next host, retain TLS verification.
    with requests.Session() as session:
        response = None
        for _ in range(6):
            check_source_url(province, url)
            response = session.get(url, timeout=(10, 30), stream=True, allow_redirects=False)
            if response.is_redirect:
                from urllib.parse import urljoin
                url = urljoin(url, response.headers["Location"])
                response.close()
                continue
            response.raise_for_status()
            break
        else:
            raise ValueError("too many official report redirects")
        payload = bytearray()
        try:
            for chunk in response.iter_content(65536):
                payload.extend(chunk)
                if len(payload) > 20_000_000:
                    raise ValueError("official report exceeds 20 MB")
        finally:
            response.close()
    if not payload:
        raise ValueError("empty official report")
    path = directory / "report.bin"
    atomic_write_bytes(assert_runtime_write(context, path), bytes(payload))
    atomic_write_json(assert_runtime_write(context, directory / "source.json"), {
        "province": province, "source_url": original_url, "final_url": url,
        "sha256": sha256_file(path), "retrieved_at": utc_now(),
    })
    return path


def prepare_update(context: RuntimeContext, baseline: Path, observations: Path,
                   *, allow_revisions: bool = False) -> Path:
    """Validate reviewed observations against archived source bytes before merging."""
    identity = sha256_file(baseline)
    current = load_bundle(baseline)
    updates = load_bundle(observations)
    sources = {}
    for metadata in (context.runtime_root / "raw/canada_canola").glob("*/source.json"):
        info = strict_json(metadata)
        report = metadata.parent / "report.bin"
        if report.is_file() and sha256_file(report) == info["sha256"]:
            sources[(info["province"], info["source_url"], info["sha256"])] = info
    for item in updates["records"]:
        key = (item["province"], item["source_url"], item["source_sha256"])
        if item["date_basis"] != "report_cutoff" or key not in sources:
            raise ValueError("new observation lacks an archived, verified official report")
        if item["retrieved_at"] != sources[key]["retrieved_at"]:
            raise ValueError("retrieval timestamp differs from archived report")
    bundle, changes = merge_observations(current, updates["records"], allow_revisions=allow_revisions)
    if sha256_file(baseline) != identity:
        raise ValueError("baseline changed while preparing candidate")
    return save_candidate(context, bundle, baseline=identity, changes=changes)


def save_candidate(context: RuntimeContext, bundle: dict, *, baseline: str | None, changes: dict) -> Path:
    validate_bundle(bundle)
    directory = assert_runtime_write(context, context.runtime_root / "candidates/canada_canola" / uuid.uuid4().hex)
    directory.mkdir(parents=True, exist_ok=False)
    candidate = directory / "canola_weekly.json"
    atomic_write_json(assert_runtime_write(context, candidate), bundle)
    atomic_write_json(assert_runtime_write(context, directory / "candidate.json"), {
        "schema_version": "canada-canola-candidate/1", "created_at": utc_now(),
        "baseline_sha256": baseline, "payload_sha256": sha256_file(candidate), "changes": changes,
        "latest_dates": latest_dates(bundle),
    })
    return candidate


def latest_dates(bundle: dict) -> dict:
    result = {}
    for item in bundle["records"]:
        key = f"{item['province']}/{item['metric']}"
        result[key] = max(result.get(key, ""), item["date"])
    return result


def activate_local(context: RuntimeContext, candidate: Path) -> Path:
    """Atomic, locked data-only switch. This entry deliberately cannot publish production."""
    if context.mode not in {RuntimeMode.ISOLATED_DEV, RuntimeMode.FIXTURE}:
        raise ValueError("activate-local only supports isolated development or fixtures")
    root = (context.runtime_root / "candidates/canada_canola").resolve()
    if candidate.is_symlink() or root not in candidate.resolve().parents:
        raise ValueError("candidate must be a local ordinary candidate file")
    metadata = strict_json(candidate.parent / "candidate.json")
    bundle = load_bundle(candidate)
    if (metadata.get("schema_version") != "canada-canola-candidate/1"
            or sha256_file(candidate) != metadata["payload_sha256"]):
        raise ValueError("candidate identity mismatch")
    stable = assert_runtime_write(context, context.runtime_root / STABLE_RELATIVE_PATH)
    stable.parent.mkdir(parents=True, exist_ok=True)
    lock = assert_runtime_write(context, context.runtime_root / "canada-canola.lock")
    with FileLock(str(lock), timeout=0):
        actual_baseline = sha256_file(stable) if stable.exists() else None
        if actual_baseline == metadata["payload_sha256"]:
            return stable
        if actual_baseline != metadata["baseline_sha256"]:
            raise ValueError("stable data moved after candidate preparation")
        if stable.exists():
            backup = assert_runtime_write(context, context.runtime_root / "backups/canada_canola" / f"{uuid.uuid4().hex}.json")
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(stable, backup)
            if sha256_file(backup) != actual_baseline:
                raise ValueError("backup identity mismatch")
        # Revalidate exact candidate bytes immediately before the atomic replacement.
        if sha256_file(candidate) != metadata["payload_sha256"]:
            raise ValueError("candidate changed after validation")
        atomic_write_json(assert_runtime_write(context, stable), bundle)
    return stable
