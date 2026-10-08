"""Source-backed candidates and local data-only activation; no production bypass."""
from __future__ import annotations

import json
import re
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from filelock import FileLock

from agri_research_agent.shared.atomic_storage import atomic_write_bytes, atomic_write_json
from agri_research_agent.shared.runtime_context import RuntimeContext, RuntimeMode, assert_runtime_write
from .data import BASE_URL, MODULE_ID, PERIODS, SCHEMA, STABLE, STATUS, digest, load_bundle, parse_csv, reconcile_report, source_url, validate_bundle


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path(context: RuntimeContext, relative: str | Path) -> Path:
    if context.module_id != MODULE_ID or context.mode not in {RuntimeMode.ISOLATED_DEV, RuntimeMode.FIXTURE}:
        raise ValueError("canola export updates require isolated development/fixture runtime")
    path = assert_runtime_write(context, context.runtime_root / relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def official_bytes(url: str, *, limit: int = 32_000_000) -> bytes:
    if not url.startswith(BASE_URL):
        raise ValueError("nonofficial CGC URL")
    with requests.Session() as session:
        retry = Retry(total=2, backoff_factor=1, status_forcelist=(429, 502, 503, 504), allowed_methods={"GET"})
        session.mount("https://", HTTPAdapter(max_retries=retry))
        with session.get(url, timeout=(15, 60), stream=True, allow_redirects=False) as response:
            response.raise_for_status()
            if response.status_code != 200:
                raise ValueError("CGC response is not 200; redirects require review")
            payload = bytearray()
            for chunk in response.iter_content(65536):
                payload.extend(chunk)
                if len(payload) > limit:
                    raise ValueError("CGC source exceeds size limit")
    if not payload:
        raise ValueError("empty CGC response")
    return bytes(payload)


def discover_years() -> list[str]:
    years = set()
    for url in (BASE_URL, BASE_URL + "archived.html"):
        soup = BeautifulSoup(official_bytes(url, limit=2_000_000), "html.parser")
        for link in soup.select("a[href]"):
            target = urljoin(url, link["href"])
            match = re.fullmatch(re.escape(BASE_URL) + r"(20\d{2})-(\d{2})/gsw-shg-en.csv", target)
            if match:
                start = int(match[1])
                year = f"{start}-{start+1}"
                if target == source_url(year):
                    years.add(year)
    if not years:
        raise ValueError("official directory has no annual CGC CSV links")
    return sorted(years, reverse=True)


def missing_reports(downloads: dict[str, bytes]) -> dict:
    links = {}
    for directory in (BASE_URL, BASE_URL + "archived.html"):
        soup = BeautifulSoup(official_bytes(directory, limit=2_000_000), "html.parser")
        for link in soup.select("a[href]"):
            url = urljoin(directory, link["href"])
            match = re.fullmatch(re.escape(BASE_URL) + r"(20\d{2})-(\d{2})/(\d{2})-grain-stats-weekly-(20\d{2})-(20\d{2}).xlsx", url)
            if match and match[1] == match[4] and match[2] == match[5][-2:]:
                links[(f"{match[4]}-{match[5]}", int(match[3]))] = url
    reports = {}
    for year, raw in downloads.items():
        for row in parse_csv(raw, year):
            key = (year, row["grain_week"])
            if any(row[metric] is None for metric in PERIODS.values()) and key in links:
                reports[key] = (links[key], official_bytes(links[key], limit=2_000_000))
    return reports


def prepare(context: RuntimeContext, downloads: dict[str, bytes], reports: dict | None = None) -> Path:
    """Replace annual snapshots, preserving reported cumulative revisions and gaps."""
    lock = _path(context, "canola-exports.lock")
    with FileLock(str(lock), timeout=0):
        stable = _path(context, STABLE)
        baseline = digest(stable.read_bytes()) if stable.exists() else None
        old = load_bundle(stable) if stable.exists() else None
        bundle = deepcopy(old) if old else {"schema_version": SCHEMA, "sources": {}, "records": [], "revisions": []}
        if not downloads:
            raise ValueError("no sources supplied")
        old_lookup = {(r["crop_year"], r["grain_week"]): r for r in bundle["records"]}
        for year, raw in downloads.items():
            records = parse_csv(raw, year)
            for (report_year, week), (url, report) in (reports or {}).items():
                if report_year == year:
                    reconcile_report(records, year, week, url, report)
                    report_path = _path(context, f"raw/canola_exports/{year}/{digest(report)}.xlsx")
                    if report_path.exists() and digest(report_path.read_bytes()) != digest(report):
                        raise ValueError("archived Excel source corrupt")
                    if not report_path.exists():
                        atomic_write_bytes(report_path, report)
            old_weeks = {w for (y, w) in old_lookup if y == year}
            if not old_weeks <= {r["grain_week"] for r in records}:
                raise ValueError("annual snapshot drops published weeks")
            for row in records:
                prior = old_lookup.get((year, row["grain_week"]))
                if prior and prior["week_ending"] != row["week_ending"]:
                    raise ValueError("published week ending changed; requires source review")
                for metric in PERIODS.values():
                    if prior and prior[metric] != row[metric]:
                        bundle["revisions"].append({"crop_year": year, "grain_week": row["grain_week"],
                                                    "metric": metric, "old_value": prior[metric], "new_value": row[metric],
                                                    "source_sha256": row["source_sha256"], "observed_at": now()})
            sha = digest(raw)
            raw_path = _path(context, f"raw/canola_exports/{year}/{sha}.csv")
            if raw_path.exists():
                if digest(raw_path.read_bytes()) != sha:
                    raise ValueError("archived raw source corrupt")
            else:
                atomic_write_bytes(raw_path, raw)
            bundle["sources"][year] = {"source_url": source_url(year), "sha256": sha, "retrieved_at": now()}
            bundle["records"] = [r for r in bundle["records"] if r["crop_year"] != year] + records
        bundle["records"].sort(key=lambda r: (r["crop_year"], r["grain_week"]))
        bundle["generated_at"] = now()
        validate_bundle(bundle)
        candidate = _path(context, f"candidates/canola_exports/{uuid.uuid4().hex}/weekly.json")
        atomic_write_json(candidate, bundle)
        atomic_write_json(_path(context, candidate.parent / "candidate.json"), {
            "schema_version": "canola-exports-candidate/1", "baseline_sha256": baseline,
            "payload_sha256": digest(candidate.read_bytes()), "created_at": now(),
        })
        return candidate


def _facts(bundle: dict) -> list:
    return [{k: v for k, v in row.items() if k not in {"source_sha256", "report_evidence"}} for row in bundle["records"]]


def activate_local(context: RuntimeContext, candidate: Path) -> Path:
    lock = _path(context, "canola-exports.lock")
    candidate = candidate.resolve(strict=True)
    allowed = (context.runtime_root / "candidates/canola_exports").resolve()
    if allowed not in candidate.parents:
        raise ValueError("candidate outside canola export candidate directory")
    with FileLock(str(lock), timeout=0):
        raw_candidate = candidate.read_bytes()
        metadata = json.loads((candidate.parent / "candidate.json").read_text(encoding="utf-8"))
        if (metadata.get("schema_version") != "canola-exports-candidate/1"
                or metadata["payload_sha256"] != digest(raw_candidate)):
            raise ValueError("candidate identity mismatch")
        bundle = validate_bundle(json.loads(raw_candidate))
        for year, source in bundle["sources"].items():
            raw = (context.runtime_root / f"raw/canola_exports/{year}/{source['sha256']}.csv").read_bytes()
            if digest(raw) != source["sha256"]:
                raise ValueError("raw source identity mismatch")
            expected = [r for r in bundle["records"] if r["crop_year"] == year]
            reproduced = parse_csv(raw, year)
            for row in expected:
                if row.get("report_evidence"):
                    evidence = row["report_evidence"]
                    report = (context.runtime_root / f"raw/canola_exports/{year}/{evidence['sha256']}.xlsx").read_bytes()
                    if digest(report) != evidence["sha256"]:
                        raise ValueError("Excel source identity mismatch")
                    reconcile_report(reproduced, year, row["grain_week"], evidence["source_url"], report)
            if reproduced != expected:
                raise ValueError("candidate does not reproduce official source")
        stable = _path(context, STABLE)
        actual = digest(stable.read_bytes()) if stable.exists() else None
        if actual == metadata["payload_sha256"]:
            return stable
        if actual != metadata["baseline_sha256"]:
            raise ValueError("baseline moved after candidate preparation")
        old_bytes = stable.read_bytes() if stable.exists() else None
        old = load_bundle(stable) if old_bytes else None
        if old and _facts(old) == _facts(bundle):
            atomic_write_json(_path(context, STATUS), {"status": "NO_CHANGE", "checked_at": now(), "stable_sha256": actual})
            return stable
        if old_bytes:
            atomic_write_bytes(_path(context, f"backups/canola_exports/{uuid.uuid4().hex}.json"), old_bytes)
        status_path = _path(context, STATUS)
        old_status = status_path.read_bytes() if status_path.exists() else None
        try:
            atomic_write_bytes(stable, raw_candidate)
            if digest(stable.read_bytes()) != metadata["payload_sha256"]:
                raise ValueError("stable post-publication identity mismatch")
            atomic_write_json(status_path, {"status": "PUBLISHED_LOCAL", "checked_at": now(),
                                          "stable_sha256": digest(raw_candidate), "revision_count": len(bundle["revisions"])})
        except Exception:
            if old_bytes is not None:
                atomic_write_bytes(stable, old_bytes)
            else:
                stable.unlink(missing_ok=True)
            if old_status is not None:
                atomic_write_bytes(status_path, old_status)
            else:
                status_path.unlink(missing_ok=True)
            raise
        return stable


def record_failure(context: RuntimeContext, reason: str) -> None:
    with FileLock(str(_path(context, "canola-exports.lock")), timeout=0):
        stable = _path(context, STABLE)
        atomic_write_json(_path(context, STATUS), {"status": "FAILED", "checked_at": now(), "reason": reason,
                                                "stable_sha256": digest(stable.read_bytes()) if stable.exists() else None})
