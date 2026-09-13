"""Closed, content-addressed candidate evidence; never execution authority."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate evidence key")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda v: (_ for _ in ()).throw(ValueError(v)))


def fields(value, names):
    if type(value) is not dict or set(value) != set(names.split()):
        raise ValueError("incomplete or unknown evidence fields")


def verify_bundle(raw, payload, directory, *, protected=lambda p: p):
    """Verify signed digest, every member and exact image before promotion."""
    if digest(raw) != payload["evidence_bundle_sha256"]:
        raise ValueError("evidence bundle hash mismatch")
    bundle = strict_json(raw)
    fields(bundle, "schema_version identity grant started_at completed_at results files")
    if bundle["schema_version"] != "candidate-evidence-bundle/1":
        raise ValueError("unknown bundle schema")
    identity = bundle["identity"]
    fields(identity, "commit tree image_id release_id container_id nonce")
    evidence = payload["evidence"]
    if (identity["commit"] != evidence["binding"]["commit"] or
            identity["tree"] != evidence["binding"]["tree"] or
            identity["image_id"] != evidence["image_id"] or
            identity["release_id"] != payload["release_id"]):
        raise ValueError("bundle identity mismatch")
    if not re.fullmatch(r"[0-9a-f]{64}", identity["container_id"]) or not re.fullmatch(r"[0-9a-f]{32}", identity["nonce"]):
        raise ValueError("invalid instance identity")
    fields(bundle["grant"], "grant_id sha256 issued_at expires_at")
    from datetime import datetime
    times = [datetime.fromisoformat(v.replace("Z", "+00:00")) for v in
             (bundle["grant"]["issued_at"], bundle["started_at"], bundle["completed_at"], bundle["grant"]["expires_at"])]
    if any(t.tzinfo is None for t in times) or not times[0] <= times[1] <= times[2] < times[3]:
        raise ValueError("grant not valid throughout validation")
    fields(bundle["results"], "runtime health application browser fixture consumer production_data_unchanged")
    if any(v != "PASS" for v in bundle["results"].values()):
        raise ValueError("required acceptance failed")
    if type(bundle["files"]) is not list or not bundle["files"]:
        raise ValueError("missing evidence files")
    types, names = set(), set()
    for ref in bundle["files"]:
        fields(ref, "name sha256 type")
        name = ref["name"]
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", name) or name in names:
            raise ValueError("unsafe or duplicate evidence name")
        names.add(name)
        path = Path(directory) / name
        if path.is_symlink() or digest(protected(path).read_bytes()) != ref["sha256"]:
            raise ValueError("evidence member hash mismatch")
        types.add(ref["type"])
    if not {"runtime", "health", "browser", "fixture", "consumer", "data", "grant", "instance"} <= types:
        raise ValueError("missing required evidence category")
    return bundle
