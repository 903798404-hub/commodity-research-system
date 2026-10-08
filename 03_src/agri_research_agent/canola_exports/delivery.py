"""Offline evidence replay shared by the producer and pinned validation image."""
from __future__ import annotations

import base64
from datetime import date, datetime
import gzip
import io
from pathlib import Path
import zlib

from .data import PERIODS, crop_year, digest, parse_csv, reconcile_report, source_url, validate_bundle
from .update import _facts

EVIDENCE_SCHEMA = "canola-exports-source-evidence/1"
MAX_SOURCE = 32_000_000
MAX_TOTAL = 256_000_000
MAX_PACKED_SOURCE = 34_000_000
MAX_PACKED_TOTAL = 64_000_000


def require(condition: object, message: str) -> None:
    if not condition:
        raise ValueError(message)


def timestamp(value: object) -> None:
    require(type(value) is str and datetime.fromisoformat(value).utcoffset() is not None,
            "export evidence timestamp requires timezone")


def closed_bundle(bundle: dict) -> dict:
    require(type(bundle) is dict and set(bundle) == {
        "schema_version", "generated_at", "sources", "records", "revisions"}, "export bundle fields differ")
    validate_bundle(bundle)
    timestamp(bundle["generated_at"])
    for source in bundle["sources"].values():
        require(set(source) == {"source_url", "sha256", "retrieved_at"}, "export source fields differ")
    required = {"crop_year", "grain_week", "week_ending", "source_sha256",
                "weekly_mt", "cumulative_mt", "weekly_mt_missing_components", "cumulative_mt_missing_components"}
    for row in bundle["records"]:
        require(required <= set(row) <= required | {"date_quality", "report_evidence"}, "export record fields differ")
        require(date.fromisoformat(row["week_ending"]) <= date.today(), "future export cutoff requires review")
        if "date_quality" in row:
            require(row["date_quality"] == "non_monotonic_source_date", "export date quality differs")
        if "report_evidence" in row:
            require(set(row["report_evidence"]) == {"sha256", "source_url", "cells"}, "export report fields differ")
            for cell in row["report_evidence"]["cells"].values():
                require(set(cell) == {"value", "cell"}, "export report cell fields differ")
    require(type(bundle["revisions"]) is list, "export revisions must be a list")
    for item in bundle["revisions"]:
        require(type(item) is dict and set(item) == {"crop_year", "grain_week", "metric", "old_value",
                "new_value", "source_sha256", "observed_at"}, "export revision fields differ")
        timestamp(item["observed_at"])
    return bundle


def decode_evidence(evidence: dict) -> dict:
    require(type(evidence) is dict and set(evidence) == {"schema_version", "sources"}
            and evidence["schema_version"] == EVIDENCE_SCHEMA and type(evidence["sources"]) is list,
            "export evidence fields differ")
    result, total, packed_total = {}, 0, 0
    for item in evidence["sources"]:
        require(type(item) is dict and set(item) == {
            "kind", "crop_year", "sha256", "source_url", "encoding", "bytes_base64"}, "export evidence source fields differ")
        year, kind = item["crop_year"], item["kind"]
        crop_year(year)
        require(kind in {"csv", "xlsx"} and item["encoding"] == "gzip"
                and type(item["bytes_base64"]) is str
                and len(item["bytes_base64"]) <= ((MAX_PACKED_SOURCE + 2) // 3) * 4, "export evidence source too large")
        packed = base64.b64decode(item["bytes_base64"], validate=True)
        packed_total += len(packed)
        require(len(packed) <= MAX_PACKED_SOURCE and packed_total <= MAX_PACKED_TOTAL,
                "export packed evidence exceeds size bound")
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(packed)) as handle:
                raw = handle.read(min(MAX_SOURCE, MAX_TOTAL - total) + 1)
        except (OSError, EOFError, zlib.error) as exc:
            raise ValueError("invalid compressed export source") from exc
        total += len(raw)
        require(0 < len(raw) <= MAX_SOURCE and total <= MAX_TOTAL, "export evidence exceeds size bound")
        require(digest(raw) == item["sha256"], "export evidence byte identity differs")
        if kind == "csv":
            require(item["source_url"] == source_url(year), "export CSV URL differs")
        else:
            prefix = source_url(year).rsplit("/", 1)[0] + "/"
            require(type(item["source_url"]) is str and item["source_url"].startswith(prefix)
                    and item["source_url"].endswith(".xlsx") and "/../" not in item["source_url"],
                    "export report URL differs")
        key = kind, year, item["sha256"], item["source_url"]
        require(key not in result, "duplicate export source evidence")
        result[key] = raw
    return result


def replay(bundle: dict, evidence: dict) -> dict:
    closed_bundle(bundle)
    raw_sources = decode_evidence(evidence)
    used = set()
    for year, source in bundle["sources"].items():
        key = "csv", year, source["sha256"], source["source_url"]
        require(key in raw_sources, "export CSV evidence missing")
        used.add(key)
        rows = parse_csv(raw_sources[key], year)
        expected = [r for r in bundle["records"] if r["crop_year"] == year]
        for row in expected:
            if row.get("report_evidence"):
                report = row["report_evidence"]
                key = "xlsx", year, report["sha256"], report["source_url"]
                require(key in raw_sources, "export Excel evidence missing")
                used.add(key)
                reconcile_report(rows, year, row["grain_week"], report["source_url"], raw_sources[key])
        require(rows == expected, "export candidate does not reproduce official sources")
    require(used == set(raw_sources), "unused export source evidence")
    return raw_sources


def archive_evidence(bundle: dict, root: Path) -> dict:
    closed_bundle(bundle)
    references = {("csv", year, source["sha256"], source["source_url"])
                  for year, source in bundle["sources"].items()}
    references.update(("xlsx", row["crop_year"], row["report_evidence"]["sha256"],
                       row["report_evidence"]["source_url"])
                      for row in bundle["records"] if row.get("report_evidence"))
    sources = []
    total = 0
    for kind, year, sha, url in sorted(references):
        path = root / f"raw/canola_exports/{year}/{sha}.{kind}"
        require(path.is_file() and not path.is_symlink(), "export source archive missing or linked")
        require(not any(parent.is_symlink() or getattr(parent.stat(), "st_file_attributes", 0) & 0x400
                        for parent in (path, *path.parents)), "linked export source parent")
        require(0 < path.stat().st_size <= MAX_SOURCE, "export archive too large")
        raw = path.read_bytes()
        total += len(raw)
        require(total <= MAX_TOTAL, "export archive exceeds size bound")
        sources.append({"kind": kind, "crop_year": year, "sha256": sha, "source_url": url,
                        "encoding": "gzip", "bytes_base64": base64.b64encode(gzip.compress(raw, mtime=0)).decode("ascii")})
    evidence = {"schema_version": EVIDENCE_SCHEMA, "sources": sources}
    replay(bundle, evidence)
    return evidence


def observations(candidate: dict, baseline: dict | None, evidence: dict) -> dict:
    replay(candidate, evidence)
    old = closed_bundle(baseline) if baseline is not None else None
    before = {(r["crop_year"], r["grain_week"]): r for r in old["records"]} if old else {}
    after = {(r["crop_year"], r["grain_week"]): r for r in candidate["records"]}
    require(before.keys() <= after.keys(), "export candidate drops published weeks")
    expected = {}
    revised = 0
    for key, row in before.items():
        new = after[key]
        require(row["week_ending"] == new["week_ending"], "published export cutoff date changed; review required")
        for metric in PERIODS.values():
            if row[metric] != new[metric]:
                expected[(*key, metric)] = (row[metric], new[metric], new["source_sha256"])
        revised += _facts({"records": [row]}) != _facts({"records": [new]})
    history = old["revisions"] if old else []
    require(candidate["revisions"][:len(history)] == history, "export revision history changed")
    changes = candidate["revisions"][len(history):]
    actual = {}
    for item in changes:
        key = item["crop_year"], item["grain_week"], item["metric"]
        require(key not in actual, "duplicate export revision")
        actual[key] = item["old_value"], item["new_value"], item["source_sha256"]
    require(actual == expected, "export revision evidence differs")
    latest = max(candidate["records"], key=lambda row: (row["crop_year"], row["grain_week"]))
    return {"business_changed": old is None or _facts(old) != _facts(candidate),
            "record_count": len(after), "latest_cutoff": latest["week_ending"],
            "crop_years": sorted(candidate["sources"]), "added": len(after.keys() - before.keys()),
            "revised": revised}
