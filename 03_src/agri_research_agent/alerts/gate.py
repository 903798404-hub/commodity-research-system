"""Read-only DATA_READY consumption of sealed run and package evidence."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import re

from agri_research_agent.automation.full_daily_windows import validate_daily_manifest
from agri_research_agent.pipelines.async_contract_rollout import validate_report


def read_json(path: Path) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("DUPLICATE_JSON_KEY")
            result[key] = value
        return result
    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError("NONFINITE_JSON")))
    if not isinstance(value, dict):
        raise ValueError("JSON_OBJECT_REQUIRED")
    return value


def file_sha(path: Path) -> str:
    with path.open("rb") as handle:
        digest = sha256()
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def contained(root: Path, relative: str) -> Path:
    """Reject path traversal, alternate streams and links before any data read."""
    parts = PurePosixPath(relative).parts
    if not parts or PurePosixPath(relative).is_absolute() or any(p in {".", ".."} for p in parts) or ":" in relative or "\\" in relative:
        raise ValueError("UNSAFE_EVIDENCE_PATH")
    root = root.absolute()
    target = root.joinpath(*parts)
    for path in (root, *target.parents, target):
        if path.is_symlink() or path.is_junction():
            raise ValueError("EVIDENCE_LINK_FORBIDDEN")
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError("EVIDENCE_PATH_ESCAPE")
    return target


@dataclass(frozen=True)
class ReadyRun:
    source_run_id: str
    mode: str
    data_root: Path
    daily: dict
    package: dict
    evidence_identity: dict

    def report(self, name: str) -> dict:
        report = self.daily["async_updates"].get(name)
        if not isinstance(report, dict):
            raise ValueError("ASYNC_REPORT_MISSING:" + name)
        validate_report(report)
        counts = report["summary"]
        if (report.get("promotion_allowed") is not True
                or report.get("blocking_reasons") != []
                or report.get("dataset_status") not in {"UPDATED", "NO_CHANGE"}
                or counts["coverage"]["ERROR"] or counts["updates"]["ERROR"]
                or any(row.get("blocking") is True for row in report["series"])):
            raise ValueError("ASYNC_BLOCKING_ERROR:" + name)
        return report

    def bind(self, component: str, identity: dict) -> None:
        approved = self.package["current_identities"].get(component, {})
        if any(not identity.get(key) or approved.get(key) != identity[key]
               for key in ("release_id", "manifest_sha256")):
            raise ValueError("PUBLIC_CURRENT_RUN_IDENTITY_MISMATCH")


def load_ready_run(run_dir: Path, source_run_id: str, *, mode: str,
                   fixture_package_root: Path | None = None) -> ReadyRun:
    """Formal mode consumes the run's archived activated baseline package.

    It must match the successful run's delivery identity, so a replaced/older
    baseline never silently stands in for newly produced data. Fixture mode is
    explicit and labelled; it still validates run, package and domain evidence.
    """
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,180}", source_run_id):
        raise ValueError("EXPLICIT_SAFE_RUN_ID_REQUIRED")
    if mode not in {"formal", "fixture"}:
        raise ValueError("EXPLICIT_ROOT_MODE_REQUIRED")
    if mode == "formal" and fixture_package_root is not None:
        raise ValueError("FORMAL_FIXTURE_OVERRIDE_FORBIDDEN")
    run_dir = Path(run_dir).absolute()
    if run_dir.name != source_run_id:
        raise ValueError("RUN_DIRECTORY_IDENTITY_MISMATCH")
    final_path = contained(run_dir, "final-status.json")
    final = read_json(final_path)
    if (final.get("status") != "SUCCESS" or final.get("run_id") != source_run_id
            or final.get("daily_succeeded") is not True
            or type(final.get("process_exit_code")) is not int or final["process_exit_code"] != 0):
        raise ValueError("FINAL_STATUS_NOT_SUCCESS")
    daily_path = contained(run_dir, f"runtime/public-data-daily/runs/{source_run_id}/manifest.json")
    if Path(final["daily_manifest_path"]).resolve() != daily_path.resolve():
        raise ValueError("DAILY_MANIFEST_PATH_MISMATCH")
    digest = file_sha(daily_path)
    if final.get("daily_manifest_sha256") != digest:
        raise ValueError("DAILY_MANIFEST_SHA_MISMATCH")
    daily = read_json(daily_path)
    validated = validate_daily_manifest(daily_path, source_run_id, final["process_exit_code"])
    if validated["manifest"] != daily or file_sha(daily_path) != digest:
        raise ValueError("DAILY_MANIFEST_CHANGED_DURING_READ")
    if daily.get("async_summary_complete") is not True or not isinstance(daily.get("async_updates"), dict):
        raise ValueError("ASYNC_SUMMARY_INCOMPLETE")
    if mode == "formal":
        preflight = read_json(contained(run_dir, "preflight.json"))
        if preflight.get("run_id") != source_run_id:
            raise ValueError("PREFLIGHT_RUN_MISMATCH")
        gates = {}
        for gate in preflight["gates"]:
            if gate["gate"] in gates:
                raise ValueError("DUPLICATE_PREFLIGHT_GATE")
            gates[gate["gate"]] = gate
        for name in ("REMOTE_IDENTITY", "RUNTIME_BASELINE"):
            if gates.get(name, {}).get("status") != "PASS":
                raise ValueError("ACTIVATED_BASELINE_EVIDENCE_MISSING")
        baseline = gates["RUNTIME_BASELINE"]["safe_reason"]
        package_id = baseline["package_id"]
        if gates["REMOTE_IDENTITY"]["safe_reason"]["package_id"] != package_id:
            raise ValueError("ACTIVATED_PACKAGE_ID_MISMATCH")
        package_root = contained(run_dir, "runtime-baseline/" + package_id)
    else:
        if fixture_package_root is None:
            raise ValueError("EXPLICIT_FIXTURE_ROOT_REQUIRED")
        package_root = Path(fixture_package_root).absolute()
        baseline = None
    manifest_path = contained(package_root, "manifest.json")
    package = read_json(manifest_path)
    package_sha = file_sha(manifest_path)
    if package.get("schema_version") != "public-current-production-package/2":
        raise ValueError("FORMAL_PACKAGE_SCHEMA_INVALID")
    if not re.fullmatch(r"[0-9a-f]{64}", str(daily.get("delivery_identity", ""))):
        raise ValueError("DAILY_DELIVERY_IDENTITY_INVALID")
    if package.get("delivery_identity_sha256") != daily["delivery_identity"]:
        raise ValueError("PACKAGE_RUN_DELIVERY_IDENTITY_MISMATCH")
    if baseline is not None:
        if (package_sha != baseline["manifest_sha256"]
                or any(package.get(key) != baseline[key] for key in (
                    "package_id", "bundle_sha256", "delivery_identity_sha256"))):
            raise ValueError("ACTIVATED_PACKAGE_MANIFEST_MISMATCH")
    data_root = contained(package_root, "data")
    files = package["files"]
    if not files or len(files) != package["file_count"] or len({item["path"] for item in files}) != len(files):
        raise ValueError("PACKAGE_FILE_INVENTORY_INVALID")
    for item in files:
        path = contained(data_root, item["path"])
        if path.stat().st_size != item["size_bytes"] or file_sha(path) != item["sha256"]:
            raise ValueError("PACKAGE_FILE_SHA_MISMATCH")
    if file_sha(manifest_path) != package_sha or read_json(final_path) != final:
        raise ValueError("RUN_EVIDENCE_CHANGED_DURING_READ")
    return ReadyRun(source_run_id, "FORMAL / PRODUCTION-LIKE" if mode == "formal" else "TEST / FIXTURE",
                    data_root, daily, package, {
                        "daily_manifest_sha256": digest,
                        "package_manifest_sha256": package_sha,
                        "package_id": package["package_id"],
                        "delivery_identity_sha256": daily["delivery_identity"],
                        "root_provenance": "archived-activated-run-baseline" if mode == "formal" else "explicit-fixture",
                    })
