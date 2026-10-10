"""Pinned unattended data jobs; explicit scopes, external state and formal gates."""
from __future__ import annotations

from contextlib import ExitStack
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
import sys
import uuid
import subprocess
import shutil
import xml.etree.ElementTree as ET

from agri_research_agent.automation import production_data_delta as delivery
from agri_research_agent.automation import production_data_delta_exports as exports
from agri_research_agent.automation import production_data_delta_positions as positions
from agri_research_agent.automation.scheduled_baselines import domain_baseline, public_baseline
from agri_research_agent.data_sources.nutstore_basis import SOURCE_PATH, assert_external_output, read_nutstore_basis, sha256
from agri_research_agent.shared.atomic_storage import atomic_write_json

SCHEMA = "first-batch-scheduled-config/1"
JOBS = {
    "canola_exports": {"domain": "canola_exports", "clocks": ("08:30:00",), "weekdays": ()},
    "nutstore_basis": {"domain": "nutstore_basis", "clocks": ("17:00:00", "20:00:00"), "weekdays": ()},
    "positions_domestic": {"domain": "commodity_positions", "clocks": ("16:35:00", "18:00:00"),
                           "weekdays": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")},
    "positions_foreign": {"domain": "commodity_positions", "clocks": ("07:45:00",), "weekdays": ()},
}
BASIS_KEYS = {"schema_version", "approved_commit", "approved_tree", "origin", "python", "runtime_root",
              "baseline_package", "baseline_manifest_sha256", "ssh_target", "image_id", "remote_store_root",
              "full_daily_lock_path", "source"}


def validate_config(config):
    delivery.require(type(config) is dict and set(config) == {"schema_version", "job", "runtime_root", "delivery"}
                     and config["schema_version"] == SCHEMA and config["job"] in JOBS,
                     "scheduled configuration invalid")
    inner = config["delivery"]
    delivery.require(type(inner) is dict, "scheduled delivery configuration invalid")
    outer = assert_external_output(delivery._absolute(config["runtime_root"]))
    # All writable paths are rejected before creating even a log or lock.
    control = assert_external_output(delivery.ROOT)
    runtime = assert_external_output(delivery._absolute(inner["runtime_root"]))
    for a, b in ((outer, control), (runtime, control), (outer, runtime)):
        delivery.require(not a.is_relative_to(b) and not b.is_relative_to(a), "scheduled roots overlap")
    if config["job"] == "canola_exports":
        exports.validate_config(inner)
    elif config["job"].startswith("positions_"):
        positions.validate_config(inner)
    else:
        delivery.require(set(inner) == BASIS_KEYS and inner["schema_version"] == "nutstore-basis-scheduled-delivery/1",
                         "basis scheduled configuration invalid")
        for key in ("approved_commit", "approved_tree"):
            delivery.require(type(inner[key]) is str and delivery.HEX40.fullmatch(inner[key]), "basis Git pin invalid")
        delivery.require(inner["origin"] == delivery.ORIGIN and delivery.SSH_TARGET.fullmatch(inner["ssh_target"])
                         and delivery._host_contract().IMAGE_ID.fullmatch(inner["image_id"]), "basis host identity invalid")
        delivery._absolute(inner["python"], file=True)
        delivery.require(delivery.HEX64.fullmatch(inner["baseline_manifest_sha256"]), "basis baseline pin invalid")
        delivery.require(delivery._remote_path(inner["remote_store_root"]).startswith(
            "/var/lib/market-data/production-runtime/"), "basis store outside production data disk")
        delivery.require(Path(inner["source"]).absolute() == SOURCE_PATH.absolute(), "basis source is not the exact allowed workbook")
        assert_external_output(delivery._absolute(inner["full_daily_lock_path"]))
    for key in ("baseline_root", "baseline_package", "bundle_root"):
        if key in inner:
            path = assert_external_output(delivery._absolute(inner[key]))
            for mutable in (outer, runtime):
                delivery.require(not mutable.is_relative_to(path) and not path.is_relative_to(mutable),
                                 "scheduled state overlaps immutable input")
    return config


def task_xml(*, config, control_root, config_path, user_id, start_date):
    validate_config(config)
    assert_external_output(config_path)
    namespace = "http://schemas.microsoft.com/windows/2004/02/mit/task"
    ET.register_namespace("", namespace)
    def element(parent, name, value=None, **attrs):
        result = ET.SubElement(parent, "{" + namespace + "}" + name, attrs)
        if value is not None:
            result.text = str(value)
        return result
    task = ET.Element("{" + namespace + "}Task", {"version": "1.4"})
    triggers = element(task, "Triggers")
    spec = JOBS[config["job"]]
    for index, clock in enumerate(spec["clocks"]):
        trigger = element(triggers, "CalendarTrigger", id="window-" + str(index))
        element(trigger, "StartBoundary", start_date.isoformat() + "T" + clock + "+08:00")
        element(trigger, "Enabled", "true")
        if spec["weekdays"]:
            schedule = element(trigger, "ScheduleByWeek")
            element(schedule, "WeeksInterval", "1")
            days = element(schedule, "DaysOfWeek")
            for day in spec["weekdays"]:
                element(days, day)
        else:
            element(element(trigger, "ScheduleByDay"), "DaysInterval", "1")
    principal = element(element(task, "Principals"), "Principal", id="Author")
    element(principal, "UserId", user_id)
    element(principal, "LogonType", "InteractiveToken")
    element(principal, "RunLevel", "LeastPrivilege")
    settings = element(task, "Settings")
    for key, value in (("MultipleInstancesPolicy", "IgnoreNew"), ("DisallowStartIfOnBatteries", "false"),
        ("StopIfGoingOnBatteries", "false"), ("StartWhenAvailable", "true"), ("Enabled", "true"),
        ("Hidden", "true"), ("ExecutionTimeLimit", "PT90M")):
        element(settings, key, value)
    action = element(element(task, "Actions", Context="Author"), "Exec")
    element(action, "Command", r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
    element(action, "Arguments", subprocess.list2cmdline(["-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
        "-File", str(Path(control_root) / "04_scripts/automation/launch_first_batch_scheduled.ps1"),
        "-ConfigPath", str(config_path)]))
    element(action, "WorkingDirectory", str(control_root))
    return ET.tostring(task, encoding="utf-8", xml_declaration=True)


def run_basis(config, work, *, publish):
    from agri_research_agent.pipelines.nutstore_domestic_basis import prepare_nutstore_basis_package
    from agri_research_agent.pipelines.lutou_domestic_basis import load_domestic_basis_current
    from agri_research_agent.pipelines.public_data_delivery import validate_production_package
    local = Path(config["baseline_package"])
    delivery.require(delivery.sha256_file(local / "manifest.json") == config["baseline_manifest_sha256"],
                     "approved basis baseline differs")
    baseline = public_baseline(config, work / "public-baseline") if publish else local
    previous = validate_production_package(baseline)
    current = load_domestic_basis_current(baseline / "data/public-market-data/lutou-domestic-basis")
    delivery.require(current is not None and current.manifest["schema_version"] == "domestic-basis-current/4",
                     "automatic basis requires the already deployed Nutstore Current")
    before = sha256(SOURCE_PATH)
    snapshot = read_nutstore_basis(SOURCE_PATH, after=date.fromisoformat(current.manifest["max_date"]))
    if not snapshot.rows:
        if publish:
            delivery._check_public_pointer(delivery._public_pointer(config), previous.manifest)
        return {"status": "NO_CHANGE", "published": False, "source_sha256": before, "source_report": snapshot.report}
    candidate = prepare_nutstore_basis_package(baseline_package=baseline, output_root=work / "basis-candidate", source=SOURCE_PATH)
    delivery.require(sha256(SOURCE_PATH) == before, "basis source changed during preparation")
    package = validate_production_package(candidate["package_root"])
    result = {"status": "CANDIDATE", "published": False, "candidate": candidate, "source_sha256": before}
    if publish:
        # The existing approved AkShare producer discovers immutable packages in
        # this shared cache. Keep its next baseline usable without replacing its task.
        cache = assert_external_output(Path(config["full_daily_lock_path"]).parent / "runs" /
            ("nutstore-shared-" + uuid.uuid4().hex) / "runtime/public-data-packages" / package.package_id)
        shutil.copytree(package.directory, cache)
        delivery.require(validate_production_package(cache).manifest == package.manifest,
                         "basis shared package replica differs")
        evidence = {"schema_version": "nutstore-basis-scheduled-scope/1", "source_sha256": before,
                    "producer": {k: config[k] for k in ("approved_commit", "approved_tree")},
                    "baseline": previous.package_id, "candidate": package.package_id,
                    "unchanged_datasets": "ALL_EXCEPT_DOMESTIC_BASIS", "history": "APPEND_ONLY"}
        (work / "scope-evidence.json").write_bytes(delivery.canonical_json_bytes(evidence))
        args = [config["python"], "-I", "-B", "-X", "utf8", str(delivery.ROOT / "04_scripts/transfer_public_data_package.py"),
                "--package", str(package.directory), "--ssh-target", config["ssh_target"],
                "--remote-store-root", config["remote_store_root"], "--activation-image-id", config["image_id"],
                "--expected-current-id", previous.package_id,
                "--expected-current-artifact-sha256", previous.manifest["delivery_artifacts"]["domestic-spread"]["sha256"],
                "--expected-current-manifest-sha256", delivery.sha256_file(previous.directory / "manifest.json"),
                "--candidate-artifact-sha256", package.manifest["delivery_artifacts"]["domestic-spread"]["sha256"],
                "--candidate-manifest-sha256", delivery.sha256_file(package.directory / "manifest.json"),
                "--reconciliation-manifest-sha256", delivery.sha256_file(work / "scope-evidence.json")]
        transported = delivery._run(args, timeout=4200, allow_failure=True)
        receipt = delivery.strict_json(transported.stdout.strip().splitlines()[-1])
        (work / "transport-receipt.json").write_bytes(delivery.canonical_json_bytes(receipt))
        delivery.require(transported.returncode == 0 and receipt.get("status") == "SYNCED"
                         and receipt.get("transport") == "PASS" and receipt.get("package_id") == package.package_id
                         and receipt.get("promotion", {}).get("cas_result") == "PASS",
                         "basis formal delivery failed or Current moved")
        delivery._check_public_pointer(delivery._public_pointer(config), package.manifest)
        result.update(status="PUBLISHED", published=True, receipt=receipt)
    return result


def run(config, *, publish=False):
    validate_config(config)
    inner, job = config["delivery"], config["job"]
    delivery.require(sys.flags.isolated and sys.dont_write_bytecode and
                     Path(sys.executable).resolve() == Path(inner["python"]).resolve(), "scheduled Python pin differs")
    producer = delivery.verify_clean_detached_clone(delivery.ROOT, inner)
    runtime = assert_external_output(config["runtime_root"])
    runtime.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        stack.enter_context(delivery._domain_lock(runtime / (JOBS[job]["domain"] + ".lock")))
        if job == "nutstore_basis":
            stack.enter_context(delivery._domain_lock(Path(inner["full_daily_lock_path"])))
        work = runtime / (job + "-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:12])
        work.mkdir()
        result = {"status": "FAIL", "published": False, "job": job, "producer": producer,
                  "checked_at": datetime.now(timezone.utc).isoformat()}
        try:
            if job == "nutstore_basis":
                result.update(run_basis(inner, work, publish=publish))
            elif job == "canola_exports":
                selected = domain_baseline(inner, work, exports.DOMAIN, exports.BASELINE_SCHEMA) if publish else inner
                # Keep refreshed input outside the producer's separate writable runtime.
                result.update(exports.run_exports(selected, publish=publish))
            else:
                from agri_research_agent.automation.positions_scheduled_collection import collect_bundle
                selected = domain_baseline(inner, work, positions.DOMAIN, positions.BASELINE_SCHEMA) if publish else inner
                files = positions.verify_baseline(selected)
                host = delivery._host_contract()
                delivery.require(files[host.POSITIONS_STABLE] is not None, "scheduled positions baseline absent")
                baseline = delivery.strict_json((Path(selected["baseline_root"]) / host.POSITIONS_STABLE).read_bytes())
                bundle, attempts = collect_bundle(baseline, work, markets=job.removeprefix("positions_"))
                selected = {**selected, "bundle_root": str(bundle), "bundle_manifest_sha256": delivery.sha256_file(bundle / "bundle.json")}
                result.update(positions.run_positions(selected, publish=publish))
                result["collection_attempts"] = attempts
                result["collection_performed"] = True
                if any(item["status"] == "failed" for item in attempts):
                    result["delivery_status"] = result["status"]
                    result["status"] = "PARTIAL_FAILURE"
            delivery.verify_clean_detached_clone(delivery.ROOT, inner)
        except Exception as exc:
            result.update(status="FAIL", error_type=type(exc).__name__)
            raise
        finally:
            atomic_write_json(work / "result.json", result)
            atomic_write_json(runtime / (job + "-last-run.json"), result)
        return result
