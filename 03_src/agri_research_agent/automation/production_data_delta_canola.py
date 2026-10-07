"""Manual, evidence-bound Canadian canola delivery through the existing publisher."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
from pathlib import Path
import shutil
import sys
import uuid

from agri_research_agent.automation import production_data_delta as delivery

DOMAIN = "canada_canola"
CONFIG_SCHEMA = "canada-canola-delivery-config/1"


def validate_config(config: dict) -> dict:
    keys = {"schema_version", "approved_commit", "approved_tree", "origin", "python",
            "runtime_root", "baseline_root", "baseline_manifest_sha256", "candidate_path",
            "candidate_sha256", "source_root", "workbook_path", "workbook_sha256",
            "revision_keys", "ssh_target", "publisher", "publisher_sha256", "image_id",
            "remote_allocation", "policy", "policy_sha256"}
    delivery.require(type(config) is dict and set(config) == keys
                     and config["schema_version"] == CONFIG_SCHEMA, "canola delivery configuration is invalid")
    for name in ("approved_commit", "approved_tree"):
        delivery.require(type(config[name]) is str and delivery.HEX40.fullmatch(config[name]),
                         "canola approved Git identity is invalid")
    delivery.require(config["origin"] == delivery.ORIGIN, "canola origin is invalid")
    for name in ("baseline_manifest_sha256", "candidate_sha256", "publisher_sha256"):
        delivery.require(type(config[name]) is str and delivery.HEX64.fullmatch(config[name]),
                         "canola file identity pin is invalid")
    for name in ("python", "candidate_path"):
        delivery._absolute(config[name], file=True)
    for name in ("runtime_root", "baseline_root", "source_root"):
        delivery._absolute(config[name])
    delivery.require(type(config["image_id"]) is str and delivery._host_contract().IMAGE_ID.fullmatch(config["image_id"]),
                     "canola validation image must be an exact ID")
    delivery.require(type(config["ssh_target"]) is str and delivery.SSH_TARGET.fullmatch(config["ssh_target"]),
                     "canola SSH target is invalid")
    delivery._remote_path(config["publisher"])
    allocation = delivery._remote_path(config["remote_allocation"])
    delivery.require(allocation.startswith("/var/lib/market-data/production-runtime/"),
                     "canola allocation is outside production storage")
    delivery.require(type(config["policy"]) is dict and set(config["policy"]) == {DOMAIN}
                     and type(config["policy_sha256"]) is dict and set(config["policy_sha256"]) == {DOMAIN},
                     "canola policy allowlist is invalid")
    policy = delivery._remote_path(config["policy"][DOMAIN])
    delivery.require(policy.startswith("/etc/market-data/production-data-delivery/")
                     and type(config["policy_sha256"][DOMAIN]) is str
                     and delivery.HEX64.fullmatch(config["policy_sha256"][DOMAIN]), "canola policy pin is invalid")
    delivery._host_contract()._canola_revision_keys(config["revision_keys"])
    if config["workbook_path"] is None:
        delivery.require(config["workbook_sha256"] is None, "canola workbook pin requires its path")
    else:
        path = delivery._absolute(config["workbook_path"], file=True)
        delivery.require(type(config["workbook_sha256"]) is str
                         and delivery.HEX64.fullmatch(config["workbook_sha256"])
                         and delivery.sha256_file(path) == config["workbook_sha256"],
                         "canola workbook byte identity differs")
    return config


def verify_baseline(config: dict) -> dict:
    """A pinned snapshot of the exact server paths, including explicit absence."""
    host = delivery._host_contract()
    root = delivery._absolute(config["baseline_root"])
    manifest_path = root / "baseline_manifest.json"
    delivery.require(delivery.sha256_file(manifest_path) == config["baseline_manifest_sha256"],
                     "canola baseline manifest identity differs")
    manifest = delivery.strict_json(manifest_path.read_bytes())
    delivery.require(set(manifest) == {"schema_version", "source_root", "files"}
                     and manifest["schema_version"] == "canada-canola-production-baseline/1"
                     and manifest["source_root"] == config["remote_allocation"],
                     "canola baseline is not the approved server snapshot")
    files = manifest["files"]
    delivery.require(type(files) is dict and set(files) == set(host.DOMAIN_CONTRACTS[DOMAIN]["baseline"]),
                     "canola baseline file set differs")
    expected = {"baseline_manifest.json"}
    for name, identity in files.items():
        host._file_identity(identity, nullable=True)
        path = root / name
        if identity is None:
            delivery.require(not path.exists() and not path.is_symlink(), "canola absent baseline file exists")
        else:
            delivery.require(delivery._identity(path) == identity, "canola baseline file identity differs")
            expected.add(name)
    delivery.require(len({value is None for value in files.values()}) == 1,
                     "canola baseline data, evidence and status must be complete")
    actual = set()
    for path in root.rglob("*"):
        delivery._unlinked(path)
        if path.is_file():
            actual.add(path.relative_to(root).as_posix())
    delivery.require(actual == expected, "canola baseline contains unexpected files")
    return files


def source_evidence(config: dict, candidate: dict, baseline: dict | None) -> tuple[dict, dict]:
    def key(row):
        return row["province"], row["metric"], row["date"]
    old = {key(row): row for row in baseline["records"]} if baseline else {}
    changed = [row for row in candidate["records"] if old.get(key(row)) != row]
    reports = {}
    root = delivery._absolute(config["source_root"])
    for path in (root / "raw/canada_canola").glob("*/source.json"):
        delivery._unlinked(path)
        metadata_bytes = path.read_bytes()
        item = delivery.strict_json(metadata_bytes)
        delivery.require(set(item) == {"province", "source_url", "final_url", "sha256", "retrieved_at"},
                         "canola source archive metadata is invalid")
        raw = path.parent / "report.bin"
        if delivery.sha256_file(raw) == item["sha256"]:
            source_key = item["province"], item["source_url"], item["sha256"], item["retrieved_at"]
            from agri_research_agent.pipelines.canada_canola import check_source_url
            check_source_url(item["province"], item["final_url"])
            reports[source_key] = (raw, path, {"sha256": delivery.hashlib.sha256(metadata_bytes).hexdigest(),
                                             "size_bytes": len(metadata_bytes)})
    sources, seen, inventory = [], set(), {}
    for row in changed:
        if row["date_basis"] == "workbook_date":
            source_key = ("workbook", row["source_sha256"])
            if source_key in seen:
                continue
            delivery.require(config["workbook_path"] is not None
                             and row["source_sha256"] == config["workbook_sha256"],
                             "canola historical import requires its pinned workbook")
            raw = delivery._absolute(config["workbook_path"], file=True)
            entry = {"kind": "workbook", "sha256": row["source_sha256"],
                     "province": None, "source_url": None, "retrieved_at": None}
        else:
            source_key = row["province"], row["source_url"], row["source_sha256"], row["retrieved_at"]
            if source_key in seen:
                continue
            delivery.require(source_key in reports, "canola official observation lacks its archived report")
            raw, metadata_path, metadata_identity = reports[source_key]
            inventory[str(metadata_path)] = metadata_identity
            entry = {"kind": "report", "sha256": row["source_sha256"],
                     "province": row["province"], "source_url": row["source_url"], "retrieved_at": row["retrieved_at"]}
        raw = delivery._unlinked(raw)
        content = raw.read_bytes()
        delivery.require(delivery.hashlib.sha256(content).hexdigest() == entry["sha256"],
                         "canola source changed while reading")
        inventory[str(raw)] = delivery._identity(raw)
        sources.append({**entry, "bytes_base64": base64.b64encode(content).decode("ascii")})
        seen.add(source_key)
    return {"schema_version": "canada-canola-source-evidence/1", "sources": sources}, inventory


def run_canola(config: dict, *, publish: bool = False) -> dict:
    validate_config(config)
    delivery.require(sys.flags.isolated and sys.dont_write_bytecode,
                     "formal entrypoint requires Python -I -B")
    delivery.require(Path(sys.executable).resolve() == Path(config["python"]).resolve(),
                     "running Python differs from approval")
    producer = delivery.verify_clean_detached_clone(delivery.ROOT, config)
    runtime = delivery._absolute(config["runtime_root"])
    control = delivery.ROOT.resolve()
    delivery.require(not runtime.resolve().is_relative_to(control)
                     and not control.is_relative_to(runtime.resolve()), "canola runtime must be outside control clone")
    runtime.mkdir(parents=True, exist_ok=True)
    with delivery._domain_lock(runtime / "canada-canola-delivery.lock"):
        run_id = "canola-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S").lower() + "-" + uuid.uuid4().hex[:12]
        work = runtime / run_id
        work.mkdir(mode=0o700)
        result = {"schema_version": "canada-canola-delivery-result/1", "run_id": run_id,
                  "producer": producer, "status": "FAIL", "published": False}
        try:
            host = delivery._host_contract()
            baseline_files = verify_baseline(config)
            candidate_path = delivery._absolute(config["candidate_path"], file=True)
            delivery.require(delivery.sha256_file(candidate_path) == config["candidate_sha256"],
                             "canola candidate identity differs")
            from agri_research_agent.pipelines.canada_canola import load_bundle
            baseline_path = (Path(config["baseline_root"]) / host.CANOLA_STABLE
                             if baseline_files[host.CANOLA_STABLE] is not None else None)
            baseline = load_bundle(baseline_path) if baseline_path is not None else None
            candidate = load_bundle(candidate_path)
            evidence, inputs = source_evidence(config, candidate, baseline)
            package = work / "package"
            package.mkdir()
            shutil.copyfile(candidate_path, package / "canola_weekly.json")
            delivery.require(delivery.sha256_file(package / "canola_weekly.json") == config["candidate_sha256"],
                             "canola candidate changed during copying")
            (package / "source_evidence.json").write_bytes(delivery.canonical_json_bytes(evidence))
            observations = host._canola_observations(package / "canola_weekly.json", baseline_path,
                                                    evidence, config["revision_keys"])
            state = ("initialized" if baseline is None else "updated"
                     if observations["business_changed"] else "no_change")
            delta = {"schema_version": host.SCHEMA_VERSION, "status": "CANDIDATE", "delta_id": run_id,
                     "generated_at_utc": datetime.now(timezone.utc).isoformat(), "domain": DOMAIN,
                     "producer": producer, "run": {"run_id": run_id, "git_head": producer["commit"],
                                                   "business_status": state, "published": False},
                     "baseline": {"files": baseline_files},
                     "domain_metadata": {"record_count": observations["record_count"],
                                         "latest_dates": observations["latest_dates"],
                                         "revision_keys": config["revision_keys"]},
                     "payloads": {name: delivery._identity(package / name)
                                  for name in host.DOMAIN_CONTRACTS[DOMAIN]["payloads"]}}
            host.validate_delta_document(delta)
            (package / host.MANIFEST_NAME).write_bytes(delivery.canonical_json_bytes(delta))
            def recheck():
                delivery.verify_clean_detached_clone(delivery.ROOT, config)
                delivery.require(verify_baseline(config) == baseline_files
                                 and delivery.sha256_file(candidate_path) == config["candidate_sha256"],
                                 "canola inputs changed during preparation")
                for path, expected in inputs.items():
                    delivery.require(delivery._identity(Path(path)) == expected, "canola source archive changed")
                host._candidate_files(package, delta)
            recheck()
            result.update(status="CANDIDATE", candidate=str(package), observations=observations,
                          transfer_manifest_sha256=delivery.sha256_file(package / host.MANIFEST_NAME),
                          input_files=inputs, config_sha256=delivery.hashlib.sha256(delivery.canonical_json_bytes(config)).hexdigest())
            if publish:
                recheck()
                receipt = delivery.invoke_publisher(config, package)
                recheck()
                result.update(status=receipt["status"], published=receipt["status"] == "PUBLISHED", receipt=receipt)
        except Exception as exc:
            result.update(status="FAIL", error_type=type(exc).__name__)
            raise
        finally:
            (work / "result.json").write_bytes(delivery.canonical_json_bytes(result))
        return result
