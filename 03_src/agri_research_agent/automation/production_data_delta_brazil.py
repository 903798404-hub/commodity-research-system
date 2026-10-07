"""Evidence-bound Brazil soybean delivery through the existing protected publisher."""
from __future__ import annotations

import base64
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from agri_research_agent.automation import production_data_delta as delivery

DOMAIN = "brazil_soy"
CONFIG_SCHEMA = "brazil-soy-delivery-config/1"


def validate_config(config: dict) -> dict:
    keys = {"schema_version", "approved_commit", "approved_tree", "origin", "python", "runtime_root", "baseline_root",
            "baseline_manifest_sha256", "candidate_path", "candidate_sha256", "source_root", "workbook_path",
            "workbook_sha256", "revision_keys", "ssh_target", "publisher", "publisher_sha256", "image_id",
            "remote_allocation", "policy", "policy_sha256"}
    delivery.require(type(config) is dict and set(config) == keys and config["schema_version"] == CONFIG_SCHEMA,
                     "Brazil delivery configuration invalid")
    for key in ("approved_commit", "approved_tree"):
        delivery.require(type(config[key]) is str and delivery.HEX40.fullmatch(config[key]), "Brazil Git identity invalid")
    delivery.require(config["origin"] == delivery.ORIGIN, "Brazil origin invalid")
    for key in ("baseline_manifest_sha256", "candidate_sha256", "publisher_sha256"):
        delivery.require(type(config[key]) is str and delivery.HEX64.fullmatch(config[key]), "Brazil byte identity invalid")
    for key in ("python", "candidate_path"):
        delivery._absolute(config[key], file=True)
    for key in ("runtime_root", "baseline_root", "source_root"):
        delivery._absolute(config[key])
    host = delivery._host_contract()
    delivery.require(type(config["image_id"]) is str and host.IMAGE_ID.fullmatch(config["image_id"]), "Brazil image ID invalid")
    delivery.require(type(config["ssh_target"]) is str and delivery.SSH_TARGET.fullmatch(config["ssh_target"]), "Brazil SSH target invalid")
    delivery._remote_path(config["publisher"])
    delivery.require(delivery._remote_path(config["remote_allocation"]).startswith("/var/lib/market-data/production-runtime/"),
                     "Brazil allocation outside production storage")
    delivery.require(type(config["policy"]) is dict and set(config["policy"]) == {DOMAIN}
                     and type(config["policy_sha256"]) is dict and set(config["policy_sha256"]) == {DOMAIN}, "Brazil policy allowlist invalid")
    delivery.require(delivery._remote_path(config["policy"][DOMAIN]).startswith("/etc/market-data/production-data-delivery/")
                     and type(config["policy_sha256"][DOMAIN]) is str and delivery.HEX64.fullmatch(config["policy_sha256"][DOMAIN]),
                     "Brazil policy pin invalid")
    host._brazil_revision_keys(config["revision_keys"])
    if config["workbook_path"] is None:
        delivery.require(config["workbook_sha256"] is None, "Brazil workbook pin requires path")
    else:
        path = delivery._absolute(config["workbook_path"], file=True)
        delivery.require(type(config["workbook_sha256"]) is str and delivery.HEX64.fullmatch(config["workbook_sha256"])
                         and delivery.sha256_file(path) == config["workbook_sha256"], "Brazil workbook identity differs")
    return config


def verify_baseline(config: dict) -> dict:
    host = delivery._host_contract()
    root = delivery._absolute(config["baseline_root"])
    manifest = root / "baseline_manifest.json"
    delivery.require(delivery.sha256_file(manifest) == config["baseline_manifest_sha256"], "Brazil baseline manifest differs")
    data = delivery.strict_json(manifest.read_bytes())
    delivery.require(set(data) == {"schema_version", "source_root", "files"}
                     and data["schema_version"] == "brazil-soy-production-baseline/1"
                     and data["source_root"] == config["remote_allocation"], "Brazil baseline is not approved snapshot")
    files = data["files"]
    delivery.require(type(files) is dict and set(files) == set(host.DOMAIN_CONTRACTS[DOMAIN]["baseline"]), "Brazil baseline file set differs")
    expected = {"baseline_manifest.json"}
    for name, identity in files.items():
        host._file_identity(identity, nullable=True)
        path = root / name
        if identity is None:
            delivery.require(not path.exists() and not path.is_symlink(), "Brazil absent baseline exists")
        else:
            delivery.require(delivery._identity(path) == identity, "Brazil baseline bytes differ")
            expected.add(name)
    delivery.require(len({x is None for x in files.values()}) == 1, "Brazil baseline data/evidence/status incomplete")
    actual = set()
    for path in root.rglob("*"):
        delivery._unlinked(path)
        if path.is_file():
            actual.add(path.relative_to(root).as_posix())
    delivery.require(actual == expected, "Brazil baseline unexpected files")
    return files


def source_evidence(config: dict, candidate: dict, baseline: dict | None) -> tuple[dict, dict]:
    def key(row):
        return tuple(row[field] for field in ("season", "region", "metric", "date"))
    old = {key(row): row for row in baseline["records"]} if baseline else {}
    changed = [row for row in candidate["records"] if old.get(key(row)) != row]
    root = delivery._absolute(config["source_root"])
    reports = {}
    from agri_research_agent.pipelines.brazil_soy import check_source_url
    for metadata in (root / "raw/brazil_soy").glob("*/source.json"):
        delivery._unlinked(metadata)
        info = delivery.strict_json(metadata.read_bytes())
        delivery.require(set(info) == {"schema_version", "source_url", "final_url", "sha256", "retrieved_at", "published_at"}
                         and info["schema_version"] == "brazil-soy-source/1", "Brazil source archive metadata invalid")
        check_source_url(info["source_url"])
        check_source_url(info["final_url"])
        raw = delivery._unlinked(metadata.parent / "report.bin")
        if delivery.sha256_file(raw) == info["sha256"]:
            reports[(info["source_url"], info["sha256"], info["retrieved_at"], info["published_at"])] = (raw, metadata)
    sources, seen, inventory = [], set(), {}
    for row in changed:
        if row["date_basis"] == "workbook_date":
            source_key = ("workbook", row["source_sha256"])
            if source_key in seen:
                continue
            delivery.require(config["workbook_path"] is not None and row["source_sha256"] == config["workbook_sha256"],
                             "Brazil historical import requires pinned workbook")
            raw = delivery._absolute(config["workbook_path"], file=True)
            entry = {"kind": "workbook", "sha256": row["source_sha256"], "source_url": None, "retrieved_at": None, "published_at": None}
        else:
            source_key = (row["source_url"], row["source_sha256"], row["retrieved_at"], row["published_at"])
            if source_key in seen:
                continue
            delivery.require(source_key in reports, "Brazil official observation lacks archive")
            raw, metadata = reports[source_key]
            inventory[str(metadata)] = delivery._identity(metadata)
            entry = {"kind": "report", "sha256": row["source_sha256"], "source_url": row["source_url"],
                     "retrieved_at": row["retrieved_at"], "published_at": row["published_at"]}
        content = delivery._unlinked(raw).read_bytes()
        delivery.require(delivery.hashlib.sha256(content).hexdigest() == entry["sha256"], "Brazil source changed while reading")
        inventory[str(raw)] = delivery._identity(raw)
        sources.append({**entry, "bytes_base64": base64.b64encode(content).decode("ascii")})
        seen.add(source_key)
    return {"schema_version": "brazil-soy-source-evidence/1", "sources": sources}, inventory


def run_brazil(config: dict, *, publish: bool = False) -> dict:
    validate_config(config)
    delivery.require(sys.flags.isolated and sys.dont_write_bytecode, "formal entrypoint requires Python -I -B")
    delivery.require(Path(sys.executable).resolve() == Path(config["python"]).resolve(), "running Python differs from approval")
    producer = delivery.verify_clean_detached_clone(delivery.ROOT, config)
    runtime = delivery._absolute(config["runtime_root"])
    control = delivery.ROOT.resolve()
    delivery.require(not runtime.resolve().is_relative_to(control) and not control.is_relative_to(runtime.resolve()),
                     "Brazil runtime must be outside producer clone")
    runtime.mkdir(parents=True, exist_ok=True)
    with delivery._domain_lock(runtime / "brazil-soy-delivery.lock"):
        run_id = "brazil-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S").lower() + "-" + uuid.uuid4().hex[:12]
        work = runtime / run_id
        work.mkdir(mode=0o700)
        result = {"schema_version": "brazil-soy-delivery-result/1", "run_id": run_id, "producer": producer,
                  "status": "FAIL", "published": False}
        try:
            host = delivery._host_contract()
            baseline_files = verify_baseline(config)
            candidate_path = delivery._absolute(config["candidate_path"], file=True)
            delivery.require(delivery.sha256_file(candidate_path) == config["candidate_sha256"], "Brazil candidate differs")
            from agri_research_agent.pipelines.brazil_soy import load_bundle
            baseline_path = Path(config["baseline_root"]) / host.BRAZIL_STABLE if baseline_files[host.BRAZIL_STABLE] is not None else None
            baseline = load_bundle(baseline_path) if baseline_path else None
            candidate = load_bundle(candidate_path)
            evidence, inputs = source_evidence(config, candidate, baseline)
            package = work / "package"
            package.mkdir()
            shutil.copyfile(candidate_path, package / "soy_weekly.json")
            delivery.require(delivery.sha256_file(package / "soy_weekly.json") == config["candidate_sha256"], "Brazil candidate changed during copying")
            (package / "source_evidence.json").write_bytes(delivery.canonical_json_bytes(evidence))
            observations = host._brazil_observations(package / "soy_weekly.json", baseline_path, evidence, config["revision_keys"])
            state = "initialized" if baseline is None else "updated" if observations["business_changed"] else "no_change"
            delta = {"schema_version": host.SCHEMA_VERSION, "status": "CANDIDATE", "delta_id": run_id,
                     "generated_at_utc": datetime.now(timezone.utc).isoformat(), "domain": DOMAIN, "producer": producer,
                     "run": {"run_id": run_id, "git_head": producer["commit"], "business_status": state, "published": False},
                     "baseline": {"files": baseline_files}, "domain_metadata": {"record_count": observations["record_count"],
                         "latest_dates": observations["latest_dates"], "revision_keys": config["revision_keys"]},
                     "payloads": {name: delivery._identity(package / name) for name in host.DOMAIN_CONTRACTS[DOMAIN]["payloads"]}}
            host.validate_delta_document(delta)
            (package / host.MANIFEST_NAME).write_bytes(delivery.canonical_json_bytes(delta))
            def recheck():
                delivery.verify_clean_detached_clone(delivery.ROOT, config)
                delivery.require(verify_baseline(config) == baseline_files and delivery.sha256_file(candidate_path) == config["candidate_sha256"],
                                 "Brazil input changed during preparation")
                for path, expected in inputs.items():
                    delivery.require(delivery._identity(Path(path)) == expected, "Brazil source archive changed")
                host._candidate_files(package, delta)
            recheck()
            result.update(status="CANDIDATE", candidate=str(package), observations=observations,
                transfer_manifest_sha256=delivery.sha256_file(package / host.MANIFEST_NAME), input_files=inputs,
                config_sha256=delivery.hashlib.sha256(delivery.canonical_json_bytes(config)).hexdigest())
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
