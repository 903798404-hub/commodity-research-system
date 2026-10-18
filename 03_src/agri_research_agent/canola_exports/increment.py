"""Baseline-bound CGC transport; formal files keep their existing full schema.

This module is standard-library only so the protected host can reconstruct the
same bytes admitted by the offline application validator. No source is fetched.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json

SCHEMA = "canola-exports-increment/1"
EVIDENCE_SCHEMA = "canola-exports-evidence-increment/1"
FULL_SCHEMA = "canola-exports/1"
FULL_EVIDENCE_SCHEMA = "canola-exports-source-evidence/1"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def canonical(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def identity(value):
    return hashlib.sha256(canonical(value)).hexdigest() if value is not None else None


def _rows(rows):
    require(type(rows) is list and len(rows) <= 10000, "export increment rows invalid")
    result = {}
    for row in rows:
        require(type(row) is dict and type(row.get("crop_year")) is str
                and type(row.get("grain_week")) is int, "export increment key invalid")
        key = row["crop_year"], row["grain_week"]
        require(key not in result, "duplicate export increment key")
        result[key] = row
    return result


def _sources(evidence):
    require(type(evidence) is dict and set(evidence) == {"schema_version", "sources"}
            and evidence["schema_version"] == FULL_EVIDENCE_SCHEMA,
            "export evidence baseline invalid")
    require(type(evidence["sources"]) is list and len(evidence["sources"]) <= 1024,
            "export evidence source count invalid")
    result = {}
    for item in evidence["sources"]:
        require(type(item) is dict and set(item) == {
            "kind", "crop_year", "sha256", "source_url", "encoding", "bytes_base64"},
            "export increment source fields invalid")
        key = tuple(item[name] for name in ("kind", "crop_year", "sha256", "source_url"))
        require(all(type(part) is str for part in key) and key not in result,
                "duplicate or invalid export increment source")
        result[key] = item
    return result


def build(candidate, baseline, evidence, baseline_evidence):
    require(candidate.get("schema_version") == FULL_SCHEMA, "export candidate schema invalid")
    old = _rows(baseline["records"]) if baseline is not None else {}
    new = _rows(candidate["records"])
    require(old.keys() <= new.keys(), "export increment drops published weeks")
    history = baseline["revisions"] if baseline is not None else []
    require(candidate["revisions"][:len(history)] == history,
            "export increment rewrites revision history")
    update = {"schema_version": SCHEMA, "baseline_sha256": identity(baseline),
              "generated_at": candidate["generated_at"], "sources": deepcopy(candidate["sources"]),
              "updates": [deepcopy(new[key]) for key in sorted(new) if old.get(key) != new[key]],
              "revisions": deepcopy(candidate["revisions"][len(history):])}
    prior = _sources(baseline_evidence) if baseline_evidence is not None else {}
    current = _sources(evidence)
    # Identity is of the official uncompressed bytes, not gzip headers produced
    # by a different Python/zlib version. The producer and offline validator
    # replay those bytes independently; existing evidence remains unchanged.
    additions = [deepcopy(item) for key, item in current.items() if key not in prior]
    source_update = {"schema_version": EVIDENCE_SCHEMA,
                     "baseline_sha256": identity(baseline_evidence),
                     "references": [list(key) for key in sorted(current)], "sources": additions}
    normalized = {"schema_version": FULL_EVIDENCE_SCHEMA,
                  "sources": [deepcopy(prior.get(key, current[key])) for key in sorted(current)]}
    require(resolve(update, baseline, source_update, baseline_evidence) == (candidate, normalized),
            "export increment round trip differs")
    return update, source_update


def resolve(value, baseline, evidence, baseline_evidence):
    require(type(value) is dict and type(evidence) is dict, "export transport must be an object")
    if value.get("schema_version") != SCHEMA:
        require(value.get("schema_version") == FULL_SCHEMA
                and evidence.get("schema_version") == FULL_EVIDENCE_SCHEMA,
                "export transport schemas differ")
        return value, evidence
    require(set(value) == {"schema_version", "baseline_sha256", "generated_at", "sources", "updates", "revisions"}
            and value["baseline_sha256"] == identity(baseline), "export increment baseline differs")
    require(type(value["revisions"]) is list and len(value["revisions"]) <= 20000,
            "export increment revision list invalid")
    rows = _rows(baseline["records"]) if baseline is not None else {}
    rows.update(_rows(value["updates"]))
    result = {"schema_version": FULL_SCHEMA, "generated_at": value["generated_at"],
              "sources": deepcopy(value["sources"]), "records": [deepcopy(rows[key]) for key in sorted(rows)],
              "revisions": deepcopy((baseline["revisions"] if baseline is not None else []) + value["revisions"])}
    require(type(evidence) is dict and set(evidence) == {
        "schema_version", "baseline_sha256", "references", "sources"}
        and evidence["schema_version"] == EVIDENCE_SCHEMA
        and evidence["baseline_sha256"] == identity(baseline_evidence), "export evidence increment baseline differs")
    prior = _sources(baseline_evidence) if baseline_evidence is not None else {}
    additions = _sources({"schema_version": FULL_EVIDENCE_SCHEMA, "sources": evidence["sources"]})
    references = evidence["references"]
    require(type(references) is list and len(references) <= 1024
            and all(type(key) is list and len(key) == 4 and all(type(p) is str for p in key)
                    for key in references), "export source references invalid")
    keys = [tuple(key) for key in references]
    require(keys == sorted(set(keys)) and set(additions) <= set(keys),
            "duplicate, unordered or unused export source reference")
    sources = {**prior, **additions}
    require(set(keys) <= sources.keys(), "export source reference missing")
    resolved_evidence = {"schema_version": FULL_EVIDENCE_SCHEMA,
                         "sources": [deepcopy(sources[key]) for key in keys]}
    return result, resolved_evidence
