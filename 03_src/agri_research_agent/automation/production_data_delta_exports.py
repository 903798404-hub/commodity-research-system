"""Manually triggered CGC acquisition and evidence-bound production delivery."""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
import shutil
import sys
import uuid

from agri_research_agent.automation import production_data_delta as delivery
from agri_research_agent.canola_exports import data
from agri_research_agent.canola_exports.delivery import archive_evidence, observations, replay

DOMAIN = "canola_exports"
CONFIG_SCHEMA = "canola-exports-delivery-config/1"
BASELINE_SCHEMA = "canola-exports-production-baseline/1"
PROVIDER_TIMEOUT = 1200


def validate_config(config: dict) -> dict:
    keys = {"schema_version", "approved_commit", "approved_tree", "origin", "python", "runtime_root",
            "baseline_root", "baseline_manifest_sha256", "ssh_target", "publisher", "publisher_sha256",
            "image_id", "remote_allocation", "policy", "policy_sha256", "initial_years", "expected_cutoff"}
    delivery.require(type(config) is dict and set(config) == keys and config["schema_version"] == CONFIG_SCHEMA,
                     "export delivery configuration is invalid")
    for name in ("approved_commit", "approved_tree"):
        delivery.require(type(config[name]) is str and delivery.HEX40.fullmatch(config[name]), "export Git pin invalid")
    delivery.require(config["origin"] == delivery.ORIGIN, "export origin invalid")
    for name in ("baseline_manifest_sha256", "publisher_sha256"):
        delivery.require(type(config[name]) is str and delivery.HEX64.fullmatch(config[name]), "export SHA pin invalid")
    delivery._absolute(config["python"], file=True)
    for name in ("runtime_root", "baseline_root"):
        delivery._absolute(config[name])
    delivery.require(type(config["image_id"]) is str and delivery._host_contract().IMAGE_ID.fullmatch(config["image_id"]),
                     "export validation image must be exact")
    delivery.require(type(config["ssh_target"]) is str and delivery.SSH_TARGET.fullmatch(config["ssh_target"]),
                     "export SSH target invalid")
    delivery._remote_path(config["publisher"])
    delivery.require(delivery._remote_path(config["remote_allocation"]).startswith(
        "/var/lib/market-data/production-runtime/"), "export allocation outside production storage")
    delivery.require(type(config["policy"]) is dict and set(config["policy"]) == {DOMAIN}
                     and type(config["policy_sha256"]) is dict and set(config["policy_sha256"]) == {DOMAIN},
                     "export policy allowlist invalid")
    delivery.require(delivery._remote_path(config["policy"][DOMAIN]).startswith(
        "/etc/market-data/production-data-delivery/") and type(config["policy_sha256"][DOMAIN]) is str
        and delivery.HEX64.fullmatch(config["policy_sha256"][DOMAIN]), "export policy pin invalid")
    delivery.require(type(config["initial_years"]) is int and 1 <= config["initial_years"] <= 20,
                     "export initial year count invalid")
    cutoff = config["expected_cutoff"]
    delivery.require(cutoff is None or type(cutoff) is str and date.fromisoformat(cutoff).isoformat() == cutoff
                     and date.fromisoformat(cutoff) <= date.today(), "export expected cutoff invalid")
    return config


def verify_baseline(config: dict) -> dict:
    host = delivery._host_contract()
    root = delivery._absolute(config["baseline_root"])
    manifest_path = delivery._unlinked(root / "baseline_manifest.json")
    delivery.require(delivery.sha256_file(manifest_path) == config["baseline_manifest_sha256"], "export baseline pin differs")
    manifest = delivery.strict_json(manifest_path.read_bytes())
    delivery.require(set(manifest) == {"schema_version", "source_root", "files"}
                     and manifest["schema_version"] == BASELINE_SCHEMA
                     and manifest["source_root"] == config["remote_allocation"], "export baseline is not server snapshot")
    files = manifest["files"]
    delivery.require(type(files) is dict and set(files) == set(host.DOMAIN_CONTRACTS[DOMAIN]["baseline"]),
                     "export baseline file set differs")
    expected = {"baseline_manifest.json"}
    for relative, identity in files.items():
        host._file_identity(identity, nullable=True)
        path = delivery._unlinked(root / relative)
        if identity is None:
            delivery.require(not path.exists(), "export absent baseline exists")
        else:
            delivery.require(delivery._identity(path) == identity, "export baseline file identity differs")
            expected.add(relative)
    delivery.require(len({item is None for item in files.values()}) == 1, "export baseline must be complete")
    actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if delivery._unlinked(p).is_file()}
    delivery.require(actual == expected, "export baseline contains unexpected files")
    return files


def _collect(config: dict, work: Path, baseline: dict | None, baseline_evidence: dict | None) -> tuple[Path, dict]:
    root = work / "01_data"
    root.mkdir()
    (root / ".market-data-runtime.json").write_bytes(delivery.canonical_json_bytes({
        "schema_version": 1, "runtime_id": work.name, "classification": "isolated-dev",
        "module_id": data.MODULE_ID, "created_at": datetime.now(timezone.utc).isoformat()}))
    if baseline is not None:
        stable = root / data.STABLE
        stable.parent.mkdir(parents=True)
        stable.write_bytes(delivery.canonical_json_bytes(baseline))
        for (kind, year, sha, _url), raw in replay(baseline, baseline_evidence).items():
            path = root / f"raw/canola_exports/{year}/{sha}.{kind}"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
    result = delivery._run([
        config["python"], "-I", "-B", "-X", "utf8",
        str(delivery.ROOT / "04_scripts/canola_exports/update_exports.py"),
        "--runtime-root", str(root), "prepare", "--years", str(config["initial_years"] if baseline is None else 2),
    ], cwd=delivery.ROOT, timeout=PROVIDER_TIMEOUT)
    response = delivery.strict_json(result.stdout)
    delivery.require(response.get("status") == "PASS", "export provider did not prepare candidate")
    candidate = delivery._absolute(response["path"], file=True)
    delivery.require(candidate.is_relative_to(root / "candidates/canola_exports") and candidate.name == "weekly.json",
                     "export provider candidate outside owned runtime")
    bundle = delivery.strict_json(candidate.read_bytes())
    return candidate, archive_evidence(bundle, root)


def run_exports(config: dict, *, publish: bool = False) -> dict:
    validate_config(config)
    delivery.require(sys.flags.isolated and sys.dont_write_bytecode, "formal entrypoint requires Python -I -B")
    delivery.require(Path(sys.executable).resolve() == Path(config["python"]).resolve(), "export Python approval differs")
    producer = delivery.verify_clean_detached_clone(delivery.ROOT, config)
    runtime = delivery._absolute(config["runtime_root"])
    baseline_root = delivery._absolute(config["baseline_root"])
    control = delivery.ROOT.resolve()
    for other in (control, baseline_root.resolve()):
        delivery.require(not runtime.resolve().is_relative_to(other) and not other.is_relative_to(runtime.resolve()),
                         "export runtime must be outside source and baseline")
    runtime.mkdir(parents=True, exist_ok=True)
    with delivery._domain_lock(runtime / "canola-exports-delivery.lock"):
        run_id = "canola-exports-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S").lower() + "-" + uuid.uuid4().hex[:12]
        work = runtime / run_id
        work.mkdir(mode=0o700)
        result = {"schema_version": "canola-exports-delivery-result/1", "run_id": run_id,
                  "producer": producer, "status": "FAIL", "published": False, "trigger": "manual"}
        try:
            host = delivery._host_contract()
            baseline_files = verify_baseline(config)
            baseline = (delivery.strict_json((baseline_root / host.EXPORTS_STABLE).read_bytes())
                        if baseline_files[host.EXPORTS_STABLE] is not None else None)
            baseline_evidence = (delivery.strict_json((baseline_root / host.EXPORTS_SOURCES).read_bytes())
                                 if baseline is not None else None)
            candidate_path, evidence = _collect(config, work, baseline, baseline_evidence)
            package = work / "package"
            package.mkdir()
            candidate_sha = delivery.sha256_file(candidate_path)
            shutil.copyfile(candidate_path, package / "weekly.json")
            delivery.require(delivery.sha256_file(package / "weekly.json") == candidate_sha, "export candidate changed copying")
            (package / "source_evidence.json").write_bytes(delivery.canonical_json_bytes(evidence))
            bundle = delivery.strict_json((package / "weekly.json").read_bytes())
            semantic = observations(bundle, baseline, evidence)
            if config["expected_cutoff"] is not None:
                delivery.require(semantic["latest_cutoff"] >= config["expected_cutoff"],
                                 "CGC cutoff has not reached manually confirmed date; review required")
            state = "initialized" if baseline is None else "updated" if semantic["business_changed"] else "no_change"
            delta = {"schema_version": host.SCHEMA_VERSION, "status": "CANDIDATE", "delta_id": run_id,
                     "generated_at_utc": datetime.now(timezone.utc).isoformat(), "domain": DOMAIN,
                     "producer": producer, "run": {"run_id": run_id, "git_head": producer["commit"],
                                                   "business_status": state, "published": False},
                     "baseline": {"files": baseline_files},
                     "domain_metadata": {name: semantic[name] for name in host.DOMAIN_CONTRACTS[DOMAIN]["metadata"]},
                     "payloads": {name: delivery._identity(package / name) for name in host.DOMAIN_CONTRACTS[DOMAIN]["payloads"]}}
            host.validate_delta_document(delta)
            (package / host.MANIFEST_NAME).write_bytes(delivery.canonical_json_bytes(delta))

            def recheck():
                delivery.verify_clean_detached_clone(delivery.ROOT, config)
                delivery.require(verify_baseline(config) == baseline_files, "export baseline changed during run")
                host._candidate_files(package, delta)

            recheck()
            result.update(status="CANDIDATE", candidate=str(package), observations=semantic,
                          expected_cutoff=config["expected_cutoff"],
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
