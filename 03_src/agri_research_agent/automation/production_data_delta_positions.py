"""Evidence-bound delivery of an explicitly selected four-board positions export."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys
import uuid

from agri_research_agent.automation import production_data_delta as delivery
from agri_research_agent.positions import delivery as positions

DOMAIN = "commodity_positions"
CONFIG_SCHEMA = "commodity-positions-delivery-config/1"
BASELINE_SCHEMA = "commodity-positions-production-baseline/1"


def validate_config(config):
    keys = {"schema_version", "approved_commit", "approved_tree", "origin", "python", "runtime_root",
            "baseline_root", "baseline_manifest_sha256", "ssh_target", "publisher", "publisher_sha256",
            "image_id", "remote_allocation", "policy", "policy_sha256", "bundle_root", "bundle_manifest_sha256"}
    positions.exact(config, keys, "positions delivery configuration")
    delivery.require(config["schema_version"] == CONFIG_SCHEMA, "positions configuration schema invalid")
    for key in ("approved_commit", "approved_tree"):
        delivery.require(type(config[key]) is str and delivery.HEX40.fullmatch(config[key]), "positions Git pin invalid")
    delivery.require(config["origin"] == delivery.ORIGIN, "positions origin invalid")
    for key in ("baseline_manifest_sha256", "publisher_sha256", "bundle_manifest_sha256"):
        delivery.require(type(config[key]) is str and delivery.HEX64.fullmatch(config[key]), "positions SHA pin invalid")
    delivery._absolute(config["python"], file=True)
    for key in ("runtime_root", "baseline_root", "bundle_root"):
        delivery._absolute(config[key])
    host = delivery._host_contract()
    delivery.require(type(config["image_id"]) is str and host.IMAGE_ID.fullmatch(config["image_id"]), "positions image pin invalid")
    delivery.require(type(config["ssh_target"]) is str and delivery.SSH_TARGET.fullmatch(config["ssh_target"]), "positions SSH target invalid")
    delivery._remote_path(config["publisher"])
    delivery.require(delivery._remote_path(config["remote_allocation"]).startswith(
        "/var/lib/market-data/production-runtime/"), "positions allocation outside data storage")
    delivery.require(type(config["policy"]) is dict and set(config["policy"]) == {DOMAIN} and
                     type(config["policy_sha256"]) is dict and set(config["policy_sha256"]) == {DOMAIN}, "positions policy allowlist invalid")
    delivery.require(delivery._remote_path(config["policy"][DOMAIN]).startswith(
        "/etc/market-data/production-data-delivery/") and type(config["policy_sha256"][DOMAIN]) is str and
        delivery.HEX64.fullmatch(config["policy_sha256"][DOMAIN]), "positions policy pin invalid")
    return config


def verify_baseline(config):
    host = delivery._host_contract()
    root = delivery._absolute(config["baseline_root"])
    path = delivery._unlinked(root / "baseline_manifest.json")
    delivery.require(delivery.sha256_file(path) == config["baseline_manifest_sha256"], "positions baseline pin differs")
    manifest = positions.exact(positions.read_json(path), ("schema_version", "source_root", "files"), "positions baseline")
    delivery.require(manifest["schema_version"] == BASELINE_SCHEMA and manifest["source_root"] == config["remote_allocation"],
                     "positions baseline is not server snapshot")
    files = manifest["files"]
    delivery.require(type(files) is dict and set(files) == set(host.DOMAIN_CONTRACTS[DOMAIN]["baseline"]), "positions baseline file set differs")
    expected = {"baseline_manifest.json"}
    for relative, identity in files.items():
        host._file_identity(identity, nullable=True)
        path = delivery._unlinked(root / relative)
        if identity is None:
            delivery.require(not path.exists(), "positions absent baseline exists")
        else:
            delivery.require(delivery._identity(path) == identity, "positions baseline identity differs")
            expected.add(relative)
    delivery.require(len({v is None for v in files.values()}) == 1, "positions baseline must be complete")
    actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if delivery._unlinked(p).is_file()}
    delivery.require(actual == expected, "positions baseline contains unexpected files")
    return files


def run_positions(config, *, publish=False):
    validate_config(config)
    delivery.require(sys.flags.isolated and sys.dont_write_bytecode, "formal entrypoint requires Python -I -B")
    delivery.require(Path(sys.executable).resolve() == Path(config["python"]).resolve(), "positions Python approval differs")
    producer = delivery.verify_clean_detached_clone(delivery.ROOT, config)
    runtime = delivery._absolute(config["runtime_root"])
    baseline_root, bundle_root = (delivery._absolute(config[key]) for key in ("baseline_root", "bundle_root"))
    for other in (delivery.ROOT.resolve(), baseline_root.resolve(), bundle_root.resolve()):
        delivery.require(not runtime.resolve().is_relative_to(other) and not other.is_relative_to(runtime.resolve()),
                         "positions runtime overlaps inputs")
    runtime.mkdir(parents=True, exist_ok=True)
    with delivery._domain_lock(runtime / "commodity-positions-delivery.lock"):
        run_id = "positions-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S").lower() + "-" + uuid.uuid4().hex[:12]
        work = runtime / run_id
        work.mkdir(mode=0o700)
        result = {"schema_version": "commodity-positions-delivery-result/1", "run_id": run_id,
                  "producer": producer, "status": "FAIL", "published": False, "trigger": "export_delivery",
                  "collection_performed": False}
        try:
            host = delivery._host_contract()
            baseline_files = verify_baseline(config)
            baseline = positions.read_json(baseline_root / host.POSITIONS_STABLE) if baseline_files[host.POSITIONS_STABLE] is not None else None
            manifest_path = delivery._unlinked(bundle_root / "bundle.json")
            delivery.require(delivery.sha256_file(manifest_path) == config["bundle_manifest_sha256"], "positions bundle pin differs")
            archive = positions.archive_bundle(bundle_root, delivery.ROOT)
            semantic = positions.observations(archive, baseline, delivery.ROOT)
            package = work / "package"
            package.mkdir()
            (package / "positions_archive.json").write_bytes(positions.canonical(archive))
            state = "initialized" if baseline is None else "updated" if semantic["business_changed"] else "no_change"
            contract = host.DOMAIN_CONTRACTS[DOMAIN]
            delta = {"schema_version": host.SCHEMA_VERSION, "status": "CANDIDATE", "delta_id": run_id,
                     "generated_at_utc": datetime.now(timezone.utc).isoformat(), "domain": DOMAIN, "producer": producer,
                     "run": {"run_id": run_id, "git_head": producer["commit"], "business_status": state, "published": False},
                     "baseline": {"files": baseline_files},
                     "domain_metadata": {key: semantic[key] for key in contract["metadata"]},
                     "payloads": {name: delivery._identity(package / name) for name in contract["payloads"]}}
            host.validate_delta_document(delta)
            (package / host.MANIFEST_NAME).write_bytes(delivery.canonical_json_bytes(delta))
            delivery.verify_clean_detached_clone(delivery.ROOT, config)
            delivery.require(verify_baseline(config) == baseline_files, "positions baseline changed during run")
            delivery.require(delivery.sha256_file(manifest_path) == config["bundle_manifest_sha256"], "positions bundle changed during run")
            # Re-read all source bytes so input mutation cannot hide behind an unchanged manifest.
            delivery.require(positions.archive_bundle(bundle_root, delivery.ROOT) == archive, "positions export changed during run")
            host._candidate_files(package, delta)
            result.update(status="CANDIDATE", candidate=str(package), observations=semantic,
                          transfer_manifest_sha256=delivery.sha256_file(package / host.MANIFEST_NAME),
                          config_sha256=delivery.hashlib.sha256(delivery.canonical_json_bytes(config)).hexdigest())
            if publish:
                receipt = delivery.invoke_publisher(config, package)
                result.update(status=receipt["status"], published=receipt["status"] == "PUBLISHED", receipt=receipt)
        except Exception as exc:
            result.update(status="FAIL", error_type=type(exc).__name__)
            raise
        finally:
            (work / "result.json").write_bytes(delivery.canonical_json_bytes(result))
        return result
