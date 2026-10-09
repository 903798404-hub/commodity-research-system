"""Pinned Windows FX producer; fresh protected baseline and conditional retries."""
from __future__ import annotations

import base64
from datetime import datetime, timezone, timedelta
from pathlib import Path
import sys
import uuid
import subprocess
import xml.etree.ElementTree as ET

from agri_research_agent.automation import production_data_delta as delivery
from agri_research_agent.market_data import foreign_fx_delivery as fx
from agri_research_agent.shared.atomic_storage import atomic_write_json

DOMAIN = "foreign_fx"
CONFIG_SCHEMA = "foreign-fx-delivery-config/1"
BASELINE_SCHEMA = "foreign-fx-production-baseline/1"
BEIJING = timezone(timedelta(hours=8))


def task_xml(*, control_root, config_path, user_id, start_date):
    """Render one hidden interactive Windows task with three bounded daily windows."""
    namespace = "http://schemas.microsoft.com/windows/2004/02/mit/task"
    ET.register_namespace("", namespace)
    def element(parent, name, value=None, **attrs):
        result = ET.SubElement(parent, "{" + namespace + "}" + name, attrs)
        if value is not None:
            result.text = str(value)
        return result
    task = ET.Element("{" + namespace + "}Task", {"version": "1.4"})
    triggers = element(task, "Triggers")
    for index, clock in enumerate(("07:00:00", "07:30:00", "08:00:00")):
        trigger = element(triggers, "CalendarTrigger", id="fx-window-" + str(index))
        element(trigger, "StartBoundary", start_date.isoformat() + "T" + clock + "+08:00")
        element(trigger, "Enabled", "true")
        element(element(trigger, "ScheduleByDay"), "DaysInterval", "1")
    principal = element(element(task, "Principals"), "Principal", id="Author")
    element(principal, "UserId", user_id)
    element(principal, "LogonType", "InteractiveToken")
    element(principal, "RunLevel", "LeastPrivilege")
    settings = element(task, "Settings")
    for key, value in (("MultipleInstancesPolicy", "IgnoreNew"), ("DisallowStartIfOnBatteries", "false"),
        ("StopIfGoingOnBatteries", "false"), ("StartWhenAvailable", "true"), ("Enabled", "true"),
        ("Hidden", "true"), ("ExecutionTimeLimit", "PT25M")):
        element(settings, key, value)
    action = element(element(task, "Actions", Context="Author"), "Exec")
    element(action, "Command", r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
    element(action, "Arguments", subprocess.list2cmdline(["-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
        "-File", str(Path(control_root) / "04_scripts/foreign_fx/launch_scheduled.ps1"), "-ConfigPath", str(config_path)]))
    element(action, "WorkingDirectory", str(control_root))
    return ET.tostring(task, encoding="utf-8", xml_declaration=True)


def validate_config(config):
    fx.exact(config, ("schema_version", "approved_commit", "approved_tree", "origin", "python", "runtime_root",
        "baseline_root", "baseline_manifest_sha256", "ssh_target", "publisher", "publisher_sha256",
        "image_id", "remote_allocation", "policy", "policy_sha256"), "FX configuration")
    delivery.require(config["schema_version"] == CONFIG_SCHEMA, "FX configuration schema invalid")
    for name in ("approved_commit", "approved_tree"):
        delivery.require(type(config[name]) is str and delivery.HEX40.fullmatch(config[name]), "FX Git pin invalid")
    delivery.require(config["origin"] == delivery.ORIGIN, "FX origin invalid")
    for name in ("baseline_manifest_sha256", "publisher_sha256"):
        delivery.require(type(config[name]) is str and delivery.HEX64.fullmatch(config[name]), "FX SHA pin invalid")
    delivery._absolute(config["python"], file=True)
    for name in ("runtime_root", "baseline_root"):
        delivery._absolute(config[name])
    delivery.require(type(config["image_id"]) is str and delivery._host_contract().IMAGE_ID.fullmatch(config["image_id"]),
                     "FX exact validation image required")
    delivery.require(type(config["ssh_target"]) is str and delivery.SSH_TARGET.fullmatch(config["ssh_target"]), "FX SSH target invalid")
    delivery._remote_path(config["publisher"])
    delivery.require(delivery._remote_path(config["remote_allocation"]).startswith("/var/lib/market-data/production-runtime/"),
                     "FX allocation outside production storage")
    fx.exact(config["policy"], (DOMAIN,), "FX policy allowlist")
    fx.exact(config["policy_sha256"], (DOMAIN,), "FX policy pins")
    delivery.require(delivery._remote_path(config["policy"][DOMAIN]).startswith("/etc/market-data/production-data-delivery/")
                     and type(config["policy_sha256"][DOMAIN]) is str and delivery.HEX64.fullmatch(config["policy_sha256"][DOMAIN]),
                     "FX policy pin invalid")
    return config


def local_baseline(config):
    root = delivery._absolute(config["baseline_root"])
    manifest_path = delivery._unlinked(root / "baseline_manifest.json")
    delivery.require(delivery.sha256_file(manifest_path) == config["baseline_manifest_sha256"], "FX local baseline pin differs")
    manifest = fx.exact(delivery.strict_json(manifest_path.read_bytes()), ("schema_version", "source_root", "files"), "FX baseline")
    delivery.require(manifest["schema_version"] == BASELINE_SCHEMA and manifest["source_root"] == config["remote_allocation"],
                     "FX baseline source differs")
    files = manifest["files"]
    host = delivery._host_contract()
    fx.exact(files, host.DOMAIN_CONTRACTS[DOMAIN]["baseline"], "FX baseline files")
    expected = {"baseline_manifest.json"}
    for relative, identity in files.items():
        host._file_identity(identity, nullable=True)
        path = delivery._unlinked(root / relative)
        if identity is None:
            delivery.require(not path.exists(), "FX absent baseline exists")
        else:
            delivery.require(delivery._identity(path) == identity, "FX baseline bytes differ")
            expected.add(relative)
    delivery.require(len({value is None for value in files.values()}) == 1, "FX baseline must be complete")
    delivery.require({p.relative_to(root).as_posix() for p in root.rglob("*") if delivery._unlinked(p).is_file()} == expected,
                     "FX baseline contains unexpected files")
    return root, files


def refresh_baseline(config, work, producer):
    """Read only the fixed domain files under the protected host's allocation lock."""
    policy = config["policy"][DOMAIN]
    delivery.require(delivery._remote_hash(config, config["publisher"]) == config["publisher_sha256"] and
        delivery._remote_hash(config, policy) == config["policy_sha256"][DOMAIN], "FX host pins differ")
    result = delivery.strict_json(delivery._ssh(config, ["sudo", "-n", "python3", "-I", config["publisher"],
        "snapshot-baseline", "--policy", policy]))
    fx.exact(result, ("schema_version", "domain", "policy_sha256", "producer", "image_id", "allocation_root", "files", "contents"),
             "FX host baseline response")
    delivery.require(result["schema_version"] == "foreign-fx-baseline-snapshot/1" and result["domain"] == DOMAIN
        and result["producer"] == producer and result["image_id"] == config["image_id"]
        and result["allocation_root"] == config["remote_allocation"]
        and result["policy_sha256"] == config["policy_sha256"][DOMAIN], "FX host baseline identity differs")
    expected = delivery._host_contract().DOMAIN_CONTRACTS[DOMAIN]["baseline"]
    fx.exact(result["files"], expected, "FX host baseline files")
    fx.exact(result["contents"], expected, "FX host baseline content")
    delivery.require(len({v is None for v in result["files"].values()}) == 1, "FX host baseline incomplete")
    root = work / "baseline"
    root.mkdir()
    for relative, identity in result["files"].items():
        delivery._host_contract()._file_identity(identity, nullable=True)
        encoded = result["contents"][relative]
        if identity is None:
            delivery.require(encoded is None, "FX absent baseline has content")
            continue
        delivery.require(type(encoded) is str and len(encoded) <= 96 * 1024 * 1024, "FX baseline exceeds bound")
        raw = base64.b64decode(encoded, validate=True)
        delivery.require(len(raw) == identity["size_bytes"] and delivery.hashlib.sha256(raw).hexdigest() == identity["sha256"],
                         "FX transferred baseline bytes differ")
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    (root / "baseline_manifest.json").write_bytes(fx.canonical({"schema_version": BASELINE_SCHEMA,
        "source_root": config["remote_allocation"], "files": result["files"]}))
    return root, result["files"]


def retry_needed(state, producer, now):
    if type(state) is not dict or state.get("producer") != producer:
        return True
    try:
        checked = datetime.fromisoformat(state["checked_at"])
        if checked.tzinfo is None or checked.astimezone(BEIJING).date() != now.astimezone(BEIJING).date():
            return True
    except (KeyError, TypeError, ValueError):
        return True
    return state.get("status") not in {"PUBLISHED", "NO_CHANGE"}


def run_fx(config, *, publish=False, retry_if_needed=False):
    validate_config(config)
    delivery.require(sys.flags.isolated and sys.dont_write_bytecode, "formal FX entry requires Python -I -B")
    delivery.require(Path(sys.executable).resolve() == Path(config["python"]).resolve(), "FX Python pin differs")
    producer = delivery.verify_clean_detached_clone(delivery.ROOT, config)
    runtime, initial = (delivery._absolute(config[key]) for key in ("runtime_root", "baseline_root"))
    for other in (delivery.ROOT.resolve(), initial.resolve()):
        delivery.require(not runtime.resolve().is_relative_to(other) and not other.is_relative_to(runtime.resolve()), "FX runtime overlaps input")
    delivery.require(not retry_if_needed or publish, "conditional retries require publication mode")
    runtime.mkdir(parents=True, exist_ok=True)
    with delivery._domain_lock(runtime / "foreign-fx-delivery.lock"):
        state_path = delivery._unlinked(runtime / "foreign-fx-last-run.json")
        state = delivery.strict_json(state_path.read_bytes()) if state_path.exists() else None
        now = datetime.now(timezone.utc)
        if retry_if_needed and not retry_needed(state, producer, now):
            return {"status": "SKIPPED", "published": False, "reason": "successful check already completed today", "producer": producer}
        run_id = "foreign-fx-" + now.strftime("%Y%m%dT%H%M%S").lower() + "-" + uuid.uuid4().hex[:12]
        work = runtime / run_id
        work.mkdir(mode=0o700)
        result = {"schema_version": "foreign-fx-delivery-result/1", "run_id": run_id, "producer": producer,
                  "status": "FAIL", "published": False, "checked_at": now.isoformat()}
        files = None
        try:
            host = delivery._host_contract()
            root, files = refresh_baseline(config, work, producer) if publish else local_baseline(config)
            baseline = delivery.strict_json((root / host.FX_STABLE).read_bytes()) if files[host.FX_STABLE] is not None else None
            if baseline is not None:
                fx.observations(baseline, None, delivery.strict_json((root / host.FX_SOURCES).read_bytes()))
            # Both official fixings occur before the UTC day closes; publication references decide freshness.
            snapshot, evidence = fx.collect(now.date(), work / "raw")
            semantic = fx.observations(snapshot, baseline, evidence)
            package = work / "package"
            package.mkdir()
            (package / "daily.json").write_bytes(fx.canonical(snapshot))
            (package / "source_evidence.json").write_bytes(fx.canonical(evidence))
            state = "initialized" if baseline is None else "updated" if semantic["business_changed"] else "no_change"
            contract = host.DOMAIN_CONTRACTS[DOMAIN]
            delta = {"schema_version": host.SCHEMA_VERSION, "status": "CANDIDATE", "delta_id": run_id,
                "generated_at_utc": now.isoformat(), "domain": DOMAIN, "producer": producer,
                "run": {"run_id": run_id, "git_head": producer["commit"], "business_status": state, "published": False},
                "baseline": {"files": files}, "domain_metadata": {key: semantic[key] for key in contract["metadata"]},
                "payloads": {name: delivery._identity(package / name) for name in contract["payloads"]}}
            host.validate_delta_document(delta)
            (package / host.MANIFEST_NAME).write_bytes(fx.canonical(delta))
            (work / "validation.json").write_bytes(fx.canonical(semantic))
            delivery.verify_clean_detached_clone(delivery.ROOT, config)
            host._candidate_files(package, delta)
            result.update(status="CANDIDATE", candidate=str(package), observations=semantic)
            if publish:
                receipt = delivery.invoke_publisher(config, package)
                result.update(status=receipt["status"], published=receipt["status"] == "PUBLISHED", receipt=receipt)
        except Exception as exc:
            result.update(status="FAIL", error_type=type(exc).__name__)
            # Logging cannot overwrite a newly published snapshot after an ambiguous transport failure.
            if publish and files is not None and files.get(delivery._host_contract().FX_STABLE) is not None:
                expected = files[delivery._host_contract().FX_STABLE]["sha256"]
                try:
                    delivery.require(delivery._remote_hash(config, config["publisher"]) == config["publisher_sha256"] and
                        delivery._remote_hash(config, config["policy"][DOMAIN]) == config["policy_sha256"][DOMAIN], "FX failure logger pins differ")
                    receipt = delivery.strict_json(delivery._ssh(config, ["sudo", "-n", "python3", "-I", config["publisher"],
                        "record-check-failure", "--policy", config["policy"][DOMAIN], "--run-id", run_id,
                        "--expected-stable-sha256", expected, "--error-type", type(exc).__name__]))
                    delivery.require(receipt.get("status") == "RECORDED" and receipt.get("stable_sha256") == expected
                        and receipt.get("policy_sha256") == config["policy_sha256"][DOMAIN], "FX failure logger receipt differs")
                    result["failure_status_recorded"] = True
                except Exception as logging_error:
                    result.update(failure_status_recorded=False, failure_log_error_type=type(logging_error).__name__)
            raise
        finally:
            result["checked_at"] = datetime.now(timezone.utc).isoformat()
            atomic_write_json(work / "result.json", result)
            atomic_write_json(state_path, result)
        return result
