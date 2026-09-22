"""Approved Windows producers, immutable candidates, and verified production delivery.

Provider stdout/stderr is deliberately captured and discarded.  Only closed manifests
and publication evidence may cross the transport boundary.  The existing business
entrypoints, validators and public-package transport remain the source of semantics.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from datetime import date, datetime, timezone
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import uuid

MANIFEST_NAME = "delta_contract.json"
ORIGIN = "https://github.com/903798404-hub/commodity-research-system.git"
SOURCES = {
    "akshare": "04_scripts/server_update_spreads.py",
    "soybean_crop_progress": "04_scripts/soybean_crop_progress/update_soybeans_crop_weekly.py",
    "soybean_export_sales": "04_scripts/soybean_exports/run_fas_export_sales.py",
}
DOMAINS = frozenset(SOURCES)
ROOT = Path(__file__).resolve().parents[3]
HEX40 = re.compile(r"[0-9a-f]{40}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
REMOTE = re.compile(r"/[A-Za-z0-9_./-]+\Z")
SSH_TARGET = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,199}\Z")
SSH_OPTIONS = ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
               "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=30",
               "-o", "ServerAliveCountMax=3"]
CONTINUATION_FILES = ("historical_price_long.xlsx", "historical_spread_database.xlsx",
                      "historical_spread_database.parquet")
SECRET_KEYS = {"nass": ["NASS_API_KEY"], "fas-export-sales": ["FAS_EXPORT_SALES_API_KEY"]}


class ProductionDataError(ValueError):
    """A bounded, credential-free fail-closed producer diagnostic."""


def require(condition: object, message: str) -> None:
    if not condition:
        raise ProductionDataError(message)


def canonical_json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


def strict_json(raw: str | bytes) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate JSON key")
            result[key] = value
        return result
    value = json.loads(raw, object_pairs_hook=pairs,
                       parse_constant=lambda _: (_ for _ in ()).throw(ProductionDataError("nonfinite JSON")))
    require(isinstance(value, dict), "JSON object required")
    return value


def sha256_file(path: Path) -> str:
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _unlinked(path: Path) -> Path:
    path = Path(path).absolute()
    for parent in (path, *path.parents):
        require(not parent.is_symlink(), "linked input is forbidden")
        require(not (getattr(parent.stat(), "st_file_attributes", 0) & 0x400)
                if parent.exists() else True, "reparse input is forbidden")
    return path


def _identity(path: Path) -> dict:
    path = _unlinked(path)
    require(path.is_file() and path.stat().st_size > 0, "required file is missing or empty")
    return {"sha256": sha256_file(path), "size_bytes": path.stat().st_size}


def _relative(value: str) -> str:
    require(isinstance(value, str) and "\\" not in value, "invalid relative path")
    parts = PurePosixPath(value).parts
    require(parts and not PurePosixPath(value).is_absolute() and
            all(p not in {".", ".."} and ":" not in p for p in parts), "unsafe relative path")
    require(PurePosixPath(value).as_posix() == value, "noncanonical relative path")
    return value


def _remote_path(value: str) -> str:
    require(isinstance(value, str) and REMOTE.fullmatch(value) and
            ".." not in PurePosixPath(value).parts and
            PurePosixPath(value).as_posix() == value and value != "/", "unsafe remote path")
    return value


def _absolute(value: object, *, file: bool = False) -> Path:
    require(isinstance(value, str) and Path(value).is_absolute(), "absolute local path required")
    path = _unlinked(Path(value))
    if file:
        require(path.is_file(), "configured local file is unavailable")
    return path


def validate_config(config: dict) -> dict:
    fields = {"schema_version", "approved_commit", "approved_tree", "origin", "python",
              "runtime_root", "baseline_root", "baseline_manifest_sha256", "public_package_root",
              "public_package_manifest_sha256", "ssh_target", "image_id", "remote_allocation",
              "policy", "policy_sha256", "publisher", "publisher_sha256", "remote_store_root",
              "domains", "secrets", "sources", "full_daily_lock_path"}
    require(isinstance(config, dict) and set(config) == fields, "closed producer configuration is invalid")
    require(config["schema_version"] == "production-data-producer-config/1", "configuration version is invalid")
    for key in ("approved_commit", "approved_tree"):
        require(isinstance(config[key], str) and HEX40.fullmatch(config[key]), "approved Git identity is invalid")
    for key in ("baseline_manifest_sha256", "public_package_manifest_sha256", "publisher_sha256"):
        require(isinstance(config[key], str) and HEX64.fullmatch(config[key]), "configured SHA-256 is invalid")
    require(config["origin"] == ORIGIN, "canonical origin differs from approval")
    require(isinstance(config["image_id"], str) and re.fullmatch(r"sha256:[0-9a-f]{64}", config["image_id"]),
            "exact validation image is required")
    require(isinstance(config["domains"], list) and len(config["domains"]) == 3 and
            set(config["domains"]) == DOMAINS and config["sources"] == SOURCES, "producer source allowlist differs")
    require(isinstance(config["ssh_target"], str) and SSH_TARGET.fullmatch(config["ssh_target"]), "unsafe SSH target")
    for key in ("remote_allocation", "publisher", "remote_store_root"):
        _remote_path(config[key])
    require(isinstance(config["policy"], dict) and set(config["policy"]) == DOMAINS - {"akshare"}, "domain policies required")
    require(isinstance(config["policy_sha256"], dict) and set(config["policy_sha256"]) == DOMAINS - {"akshare"}, "domain policy pins required")
    for domain, path in config["policy"].items():
        _remote_path(path)
        require(path.startswith("/etc/market-data/production-data-delivery/"), "protected policy path required")
        require(isinstance(config["policy_sha256"][domain], str) and HEX64.fullmatch(config["policy_sha256"][domain]), "policy SHA is invalid")
    for key in ("python", "runtime_root", "baseline_root", "public_package_root", "full_daily_lock_path"):
        _absolute(config[key], file=key == "python")
    require(Path(config["python"]).suffix.lower() in {".exe", ""}, "local existing Python executable required")
    expected_lock = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "market-data-runtime/automation/full-daily.lock"
    require(Path(config["full_daily_lock_path"]).resolve() == expected_lock.resolve(), "FULL DAILY lock pin differs from formal automation root")
    runtime = Path(config["runtime_root"]).resolve()
    for key in ("baseline_root", "public_package_root"):
        path = Path(config[key]).resolve()
        require(path != runtime and not path.is_relative_to(runtime), "approved baseline must be outside mutable run root")
    require(isinstance(config["secrets"], dict) and set(config["secrets"]) == set(SECRET_KEYS), "closed local secrets config required")
    for name, entry in config["secrets"].items():
        require(isinstance(entry, dict) and set(entry) == {"path", "keys"} and entry["keys"] == SECRET_KEYS[name], "secret key allowlist differs")
        secret = _absolute(entry["path"], file=True)
        require(not secret.resolve().is_relative_to(runtime), "secret cannot reside in a run bundle")
    return config


def safe_child_environment(extra: dict | None = None) -> dict:
    allowed = {"SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "PATH", "PATHEXT",
               "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "LOCALAPPDATA", "APPDATA", "PROGRAMDATA",
               "PROGRAMFILES", "PROGRAMFILES(X86)", "COMMONPROGRAMFILES", "NUMBER_OF_PROCESSORS",
               "PROCESSOR_ARCHITECTURE", "OS", "LANG", "LC_ALL", "TZ"}
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_NO_REPLACE_OBJECTS": "1", "GIT_TERMINAL_PROMPT": "0",
                "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1",
                "NO_PROXY": "*"})
    if extra:
        require(set(extra) <= {"NASS_API_KEY", "FAS_EXPORT_SALES_API_KEY", "MARKET_DATA_GIT_HEAD", "MARKET_DATA_GIT_TREE"},
                "unapproved child environment key")
        env.update(extra)
    return env


def _run(command: list[str], *, cwd: Path | None = None, env: dict | None = None,
         timeout: int = 3600, binary: bool = False) -> subprocess.CompletedProcess:
    if os.name == "nt" and command[0] in {"git", "ssh", "scp"}:
        name = command[0]
        executable = (Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/cmd/git.exe" if name == "git"
                      else Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/OpenSSH" / (name + ".exe"))
        require(_unlinked(executable).is_file(), "approved system transport tool unavailable")
        command = [str(executable), *command[1:]]
    try:
        result = subprocess.run(command, cwd=cwd, env=env or safe_child_environment(),
                                capture_output=True, text=not binary,
                                **({"encoding": "utf-8", "errors": "strict"} if not binary else {}),
                                timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
        raise ProductionDataError("bounded child command failed") from None
    require(result.returncode == 0, "child command failed; diagnostic output withheld")
    return result


def _git(root: Path, *args: str) -> str:
    return _run(["git", "-c", "core.autocrlf=false", "-c", "core.fsmonitor=false", "-c", "http.sslBackend=openssl",
                 "-C", str(root), *args], timeout=300).stdout.strip()


def verify_clean_detached_clone(root: Path, config: dict, *, allow_runtime: bool = False) -> dict:
    root = _unlinked(root).resolve()
    require((root / ".git").is_dir() and not (root / ".git").is_symlink(), "ordinary independent clone required")
    require(_git(root, "rev-parse", "--git-dir") == ".git" and
            _git(root, "rev-parse", "--git-common-dir") == ".git", "independent Git object database required")
    require(not (root / ".git/objects/info/alternates").exists() and
            not (root / ".git/shallow.lock").exists(), "shared or active Git state is forbidden")
    local = _git(root, "config", "--local", "--list").splitlines()
    allowed = {"core.repositoryformatversion", "core.filemode", "core.bare", "core.logallrefupdates",
               "core.symlinks", "core.ignorecase", "core.autocrlf", "remote.origin.url", "remote.origin.fetch", "remote.origin.tagopt"}
    require(all(line.partition("=")[0].lower() in allowed for line in local), "unsafe local Git configuration")
    require(all(line.partition("=")[2] == "--no-tags" for line in local if line.partition("=")[0].lower() == "remote.origin.tagopt"),
            "unexpected local Git tag policy")
    require(not _git(root, "for-each-ref", "--format=%(refname)", "refs/replace"), "Git replacements forbidden")
    require(all(line.startswith("H ") for line in _git(root, "ls-files", "-v").splitlines()), "hidden index changes forbidden")
    require(_git(root, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD" and
            not _git(root, "status", "--porcelain=v1", "--untracked-files=all"), "clean detached source required")
    identity = {"commit": _git(root, "rev-parse", "HEAD"), "tree": _git(root, "rev-parse", "HEAD^{tree}"),
                "origin": _git(root, "remote", "get-url", "origin")}
    require(identity == {"commit": config["approved_commit"], "tree": config["approved_tree"], "origin": config["origin"]}, "source identity differs from approval")
    archive = _run(["git", "-C", str(root), "archive", "--format=tar", "HEAD"], binary=True, timeout=300).stdout
    tracked = set()
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
        for member in tar:
            if member.isdir():
                continue
            require(member.isfile(), "linked or special source object forbidden")
            relative = _relative(member.name)
            require((root / relative).read_bytes() == tar.extractfile(member).read(), "source byte differs from approved archive")
            _unlinked(root / relative)
            tracked.add(relative)
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if relative == ".git" or relative.startswith(".git/"):
            continue
        _unlinked(path)
        if not path.is_file() or relative in tracked:
            continue
        require(allow_runtime and relative.split("/", 1)[0] in {"01_data", "06_outputs", "10_logs"}
                and path.suffix.lower() not in {".py", ".pyc", ".pyo", ".pth", ".dll", ".exe", ".pyd", ".bat", ".cmd", ".ps1"},
                "untracked or ignored source injection forbidden")
    _git(root, "fsck", "--strict", "--no-reflogs")
    return identity


def _secret_environment(secret: Path, expected_keys: list[str]) -> dict:
    _identity(secret)
    result = {}
    for line in Path(secret).read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        require("=" in line, "invalid local secret file")
        key, value = line.split("=", 1)
        require(key == key.strip() and key in expected_keys and key not in result,
                "unexpected or duplicate credential key")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        require(value and not any(ord(c) < 32 for c in value), "empty or malformed credential")
        result[key] = value
    require(set(result) == set(expected_keys), "required credential missing")
    return result


def verify_baseline(root: Path, manifest_sha: str) -> dict:
    root = _unlinked(root)
    require(HEX64.fullmatch(manifest_sha) and sha256_file(root / "baseline_manifest.json") == manifest_sha,
            "baseline manifest pin mismatch")
    manifest = strict_json((root / "baseline_manifest.json").read_bytes())
    require(set(manifest) == {"schema_version", "source_root", "public_package_id", "files"} and
            manifest["schema_version"] == "production-producer-baseline/1", "baseline manifest schema invalid")
    require(isinstance(manifest["files"], list) and manifest["files"], "baseline files missing")
    records = {}
    for item in manifest["files"]:
        require(isinstance(item, dict) and set(item) == {"path", "sha256", "size_bytes"}, "baseline file schema invalid")
        relative = _relative(item["path"])
        require(relative not in records and HEX64.fullmatch(item["sha256"]) and type(item["size_bytes"]) is int,
                "baseline file identity invalid")
        identity = {k: item[k] for k in ("sha256", "size_bytes")}
        require(_identity(root / relative) == identity, "baseline bytes differ from pinned manifest")
        records[relative] = identity
    actual = set()
    for path in root.rglob("*"):
        _unlinked(path)
        if path.is_file():
            actual.add(path.relative_to(root).as_posix())
    require(actual == set(records) | {"baseline_manifest.json"}, "unexpected baseline files")
    return records


@contextmanager
def _domain_lock(path: Path):
    from filelock import FileLock, Timeout
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(path), timeout=0)
    try:
        lock.acquire()
    except Timeout:
        raise ProductionDataError("another approved updater owns the lifecycle lock") from None
    try:
        yield
    finally:
        lock.release()


def _module(root: Path, relative: str, name: str):
    spec = importlib.util.spec_from_file_location(name, root / relative)
    require(spec and spec.loader, "approved module is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _host_contract(root: Path = ROOT):
    return _module(root, "09_deploy/production_data_delivery/activate_production_data_delta.py", "production_delta_contract")


def parquet_identity(path: Path) -> dict:
    import pyarrow.parquet as pq
    try:
        schema = pq.read_schema(path).remove_metadata()
        count = pq.ParquetFile(path).metadata.num_rows
    except Exception:
        raise ProductionDataError("candidate is not readable Parquet") from None
    require(count > 0, "empty candidate Parquet")
    return {**_identity(path), "row_count": count,
            "parquet_schema_sha256": hashlib.sha256(schema.to_string().encode("utf-8")).hexdigest()}


def build_manifest(domain: str, output: Path, producer: dict, baseline: dict, metadata: dict, run: dict) -> dict:
    host = _host_contract()
    contract = host.contract_for_domain(domain)
    value = {"schema_version": "production-data-delta/1", "status": "CANDIDATE",
             "delta_id": run["run_id"].lower().replace("_", "-"),
             "generated_at_utc": datetime.now(timezone.utc).isoformat(), "domain": domain,
             "producer": producer, "run": {**run, "published": False},
             "baseline": {"files": {name: baseline.get(name.removeprefix("01_data/")) for name in contract["baseline"]}},
             "domain_metadata": metadata,
             "payloads": {name: parquet_identity(output / name) for name in contract["payloads"]}}
    host.validate_delta_document(value)
    return value


def _candidate_path(source: Path, value: str, domain: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = source / _relative(value)
    path = _unlinked(path).resolve()
    require(path.is_relative_to(source / "01_data/candidates" / domain), "producer did not reference fresh candidate artifact")
    _identity(path)
    return path


def resolve_business_end_date(
    value: str | date | None,
    *,
    wall_clock_date: date | None = None,
) -> date:
    """Resolve one bounded business end date without silently following the clock."""
    today = wall_clock_date or date.today()
    require(type(today) is date, "wall-clock date is invalid")
    if value is None:
        selected = today
    elif type(value) is date:
        selected = value
    else:
        require(isinstance(value, str) and ISO_DATE.fullmatch(value),
                "business end date must use YYYY-MM-DD")
        try:
            selected = date.fromisoformat(value)
        except ValueError:
            raise ProductionDataError("business end date is invalid") from None
    require(selected <= today, "future business end date is forbidden")
    return selected


def _provider_flags(
    domain: str,
    source: Path,
    *,
    end_date: date | None = None,
) -> list[str]:
    if domain == "akshare":
        require(type(end_date) is date, "AkShare business end date is required")
        return ["--update-from-akshare", "--end-date", end_date.isoformat()]
    require(end_date is None, "business end date is only valid for AkShare")
    if domain == "soybean_crop_progress":
        return ["--dry-run"]
    require(domain == "soybean_export_sales", "unapproved producer domain")
    return ["--runtime-root", str(source), "--candidate-only",
            "--ignore-environment-proxy"]


def _crop_out_of_season_audit(source: Path, producer: dict) -> tuple[Path, dict] | None:
    audit_root = _unlinked(source / "06_outputs/audits/soybean_crop_progress").resolve()
    audits = list(audit_root.rglob("*.json")) if audit_root.is_dir() else []
    require(len(audits) == 1, "unique fresh crop audit required")
    audit_path = _unlinked(audits[0]).resolve()
    audit = strict_json(audit_path.read_bytes())
    if audit.get("status") != "out_of_season":
        return None
    require(audit_path.parent == audit_root / "dry_runs"
            and audit_path.name == f"soybeans_crop_weekly_update_{audit.get('run_id')}.json",
            "crop seasonal-skip audit path is invalid")
    try:
        reporting_date = date.fromisoformat(audit.get("new_york_reporting_date", ""))
    except (TypeError, ValueError):
        raise ProductionDataError("crop seasonal-skip provenance is invalid") from None
    outside_season = (reporting_date.month, reporting_date.day) < (4, 1) or \
        (reporting_date.month, reporting_date.day) > (11, 30)
    require(audit.get("git_head") == producer["commit"]
            and audit.get("run_mode") == "dry_run"
            and audit.get("force") is False
            and audit.get("current_year") == reporting_date.year
            and audit.get("season_window") == "04-01 through 11-30 (America/New_York)"
            and outside_season
            and audit.get("published") is False
            and audit.get("recommended_to_publish") is False
            and audit.get("business_change_found") is False
            and audit.get("error") is None,
            "crop seasonal-skip provenance is invalid")
    require(all(audit.get(key) is None for key in (
                "raw_path", "manifest_path", "candidate_progress_path",
                "candidate_condition_path")),
            "crop seasonal skip unexpectedly produced a candidate")
    require(Path(audit.get("audit_json_path", "")).resolve() == audit_path,
            "crop seasonal-skip audit identity is invalid")
    markdown = _unlinked(Path(audit.get("audit_markdown_path", ""))).resolve()
    require(markdown.parent == audit_path.parent
            and markdown.name == audit_path.with_suffix(".md").name,
            "crop seasonal-skip audit identity is invalid")
    _identity(markdown)
    return audit_path, audit


def collect_delta(domain: str, source: Path, candidate: Path, producer: dict,
                  baseline: dict, *, run_id: str) -> Path:
    host = _host_contract()
    contract = host.contract_for_domain(domain)
    if domain == "soybean_crop_progress":
        audits = list((source / "06_outputs/audits/soybean_crop_progress").rglob("*.json"))
        require(len(audits) == 1, "unique fresh crop audit required")
        audit = strict_json(audits[0].read_bytes())
        require(audit.get("status") in {"updated", "initialized", "no_change"} and
                audit.get("git_head") == producer["commit"] and audit.get("published") is False and
                audit.get("candidate_validation", {}).get("passed") is True, "crop candidate audit failed")
        artifacts = {"soybeans_crop_progress_weekly.parquet": _candidate_path(source, audit["candidate_progress_path"], domain),
                     "soybeans_crop_condition_weekly.parquet": _candidate_path(source, audit["candidate_condition_path"], domain)}
        require(len({p.parent for p in artifacts.values()}) == 1, "crop candidate pair belongs to different runs")
        for family in ("progress", "condition"):
            require(sha256_file(artifacts[f"soybeans_crop_{family}_weekly.parquet"]) == audit["candidate_sha256"][family], "crop candidate hash differs from audit")
        raw_path = source / _relative(audit["raw_path"])
        source_manifest_path = source / _relative(audit["manifest_path"])
        require(sha256_file(raw_path) == audit["raw_sha256"] and
                sha256_file(source_manifest_path) == audit["manifest_sha256"], "crop source evidence hash differs")
        raw = strict_json(raw_path.read_bytes())
        require(raw["run_id"] == audit["run_id"] and raw["current_year"] == audit["current_year"], "crop raw evidence belongs to a different run")
        metadata = {"current_year": audit["current_year"], "retrieved_at_utc": raw["retrieved_at_utc"],
                    "raw_snapshot": audit["raw_path"],
                    "duplicate_counts": audit["candidate_validation"]["quality"]["complete_duplicate_count_by_family"],
                    "source_manifest_sha256": sha256_file(source_manifest_path)}
        status = audit["status"]
    else:
        audit = strict_json((source / "01_data/update_status/soybean_export_sales.json").read_bytes())
        require(audit.get("status") == "candidate_only" and audit.get("published") is False and
                audit.get("git_head") == producer["commit"], "FAS candidate audit failed")
        artifact = _candidate_path(source, audit["candidate_path"], domain)
        artifacts = {"soybean_export_sales_weekly.parquet": artifact}
        manifest = strict_json((artifact.parent / "manifest.json").read_bytes())
        require(manifest["sha256"].lower() == sha256_file(artifact) and manifest["git_head"] == producer["commit"] and
                manifest["batch_id"] == audit["batch_id"], "FAS source manifest differs from candidate")
        metadata = {key: manifest[key] for key in contract["metadata"]}
        for key in ("raw_snapshot_sha256", "raw_manifest_sha256"):
            require(isinstance(metadata[key], str) and re.fullmatch(r"[0-9A-Fa-f]{64}", metadata[key]), "FAS source SHA-256 invalid")
            metadata[key] = metadata[key].lower()
        raw_root = _unlinked(Path(audit["raw_snapshot_dir"])).resolve()
        require(raw_root.is_relative_to(source / "01_data/raw") and
                sha256_file(raw_root / "manifest.json") == metadata["raw_manifest_sha256"] and
                sha256_file(raw_root / "records.jsonl.gz") == metadata["raw_snapshot_sha256"],
                "FAS raw source evidence differs")
        from agri_research_agent.soybean_exports.fas import _business_equal, validate_fas_stable
        import pandas as pd
        frame = pd.read_parquet(artifact)
        validate_fas_stable(frame)
        previous = source / "01_data/processed/soybean_export_sales/soybean_export_sales_weekly.parquet"
        status = "initialized" if not previous.exists() else ("no_change" if _business_equal(pd.read_parquet(previous), frame) else "updated")
    candidate.mkdir(parents=True, exist_ok=False)
    for name, path in artifacts.items():
        shutil.copy2(path, candidate / name)
        require(_identity(path) == _identity(candidate / name), "candidate copy identity differs")
    value = build_manifest(domain, candidate, producer, baseline, metadata,
                           {"run_id": run_id, "git_head": producer["commit"], "business_status": status})
    path = candidate / MANIFEST_NAME
    path.write_bytes(canonical_json_bytes(value))
    return path


def _ssh(config: dict, arguments: list[str]) -> str:
    return _run(["ssh", *SSH_OPTIONS, "-T", config["ssh_target"], shlex.join(arguments)]).stdout


def _remote_hash(config: dict, path: str) -> str:
    _remote_path(path)
    value = _ssh(config, ["sudo", "-n", "sha256sum", "--", path]).split()
    require(len(value) == 2 and HEX64.fullmatch(value[0]), "remote file identity unavailable")
    return value[0]


def _public_pointer(config: dict) -> dict:
    value = strict_json(_ssh(config, ["cat", "--", config["remote_store_root"] + "/current.json"]))
    require(set(value) == {"schema_version", "package_id", "current_identity_sha256", "delivery_identity_sha256", "bundle_sha256"}
            and value["schema_version"] == "public-current-server-pointer/2" and
            re.fullmatch(r"public-current-[0-9a-f]{24}", value["package_id"]), "remote public pointer invalid")
    require(all(HEX64.fullmatch(value[k]) for k in ("current_identity_sha256", "delivery_identity_sha256", "bundle_sha256")), "remote public identity invalid")
    return value


def _check_public_pointer(pointer: dict, manifest: dict) -> None:
    require(all(pointer[k] == manifest[k] for k in ("package_id", "current_identity_sha256", "delivery_identity_sha256", "bundle_sha256")), "remote public baseline drifted")


def _akshare_end_date_evidence(data: Path, requested: date) -> dict[str, str]:
    """Bind formal delivery to the producer's observed target-date evidence."""
    status = strict_json((data / "update_status.json").read_bytes())
    expected = requested.isoformat()
    observed_requested = status.get("requested_end_date")
    observed_effective = status.get("effective_end_date")
    require(status.get("status") == "success" and
            status.get("run_mode") == "update_from_akshare",
            "AkShare producer status is not successful")
    require(observed_requested == expected,
            "AkShare requested business end date differs from formal request")
    require(observed_effective == expected and
            status.get("target_business_date") == expected,
            "AkShare effective business end date differs from formal request")
    return {
        "requested_end_date": observed_requested,
        "effective_end_date": observed_effective,
    }


def _public_baseline(config: dict, *, remote: bool) -> Path:
    from agri_research_agent.pipelines.public_data_delivery import validate_production_package
    configured = Path(config["public_package_root"])
    require(sha256_file(configured / "manifest.json") == config["public_package_manifest_sha256"], "approved public package manifest differs")
    original = validate_production_package(configured)
    if not remote:
        return original.directory
    pointer = _public_pointer(config)
    paths = [configured]
    search = Path(config["full_daily_lock_path"]).parent / "runs"
    paths.extend(search.glob(f"*/runtime/public-data-packages/{pointer['package_id']}"))
    paths.extend((Path(config["runtime_root"]) / "runs").glob(f"*/packages/{pointer['package_id']}"))
    for path in paths:
        if path.name != pointer["package_id"]:
            continue
        _unlinked(path)
        package = validate_production_package(path)
        _check_public_pointer(pointer, package.manifest)
        require(_remote_hash(config, config["remote_store_root"] + "/releases/" + pointer["package_id"] + "/manifest.json") == sha256_file(path / "manifest.json"), "current public manifest differs from local replica")
        return path
    raise ProductionDataError("current public package has no verified local replica")


def _build_public_package(source: Path, public: Path, packages: Path):
    from agri_research_agent.pipelines.public_data_delivery import build_production_package, validate_production_package
    baseline = validate_production_package(public)
    artifacts = {name: public / "data" / entry["package_path"] for name, entry in baseline.manifest["delivery_artifacts"].items()}
    artifacts["domestic-spread"] = source / "01_data/historical_spread_database.parquet"
    result = build_production_package(public_current_root=public / "data/public-market-data", packages_root=packages,
                                      source_max_dates=baseline.manifest["source_max_dates"],
                                      required_datasets=list(baseline.manifest["current_identities"]), delivery_artifacts=artifacts)
    old = {entry["path"]: entry for entry in baseline.manifest["files"] if not entry["path"].startswith("consumer-artifacts/domestic-spread/")}
    new = {entry["path"]: entry for entry in result.manifest["files"] if not entry["path"].startswith("consumer-artifacts/domestic-spread/")}
    require(old == new and result.manifest["current_identities"] == baseline.manifest["current_identities"] and
            result.manifest["source_max_dates"] == baseline.manifest["source_max_dates"], "unrelated public datasets changed")
    return result


def invoke_publisher(config: dict, candidate: Path) -> dict:
    """Upload only fixed payloads; root seals them before protected host validation."""
    manifest_path = candidate / MANIFEST_NAME
    manifest = _host_contract().validate_delta_document(strict_json(manifest_path.read_bytes()))
    domain = manifest["domain"]
    expected_sha = sha256_file(manifest_path)
    policy = config["policy"][domain]
    require(_remote_hash(config, config["publisher"]) == config["publisher_sha256"] and
            _remote_hash(config, policy) == config["policy_sha256"][domain], "protected publisher or policy identity differs")
    remote_policy = strict_json(_ssh(config, ["sudo", "-n", "cat", "--", policy]))
    require(remote_policy.get("approved_producer") == manifest["producer"] and
            remote_policy.get("validation_image", {}).get("image_id") == config["image_id"] and
            remote_policy.get("allocation_root") == config["remote_allocation"] and
            remote_policy.get("domain") == domain, "host policy approval differs")
    staging = "/var/lib/market-data/production-data-uploads/" + manifest["delta_id"]
    _ssh(config, ["mkdir", "-m", "0700", "--", staging])
    names = [MANIFEST_NAME, *sorted(manifest["payloads"])]
    require({p.name for p in candidate.iterdir()} == set(names), "candidate upload contains unexpected files")
    _run(["scp", *SSH_OPTIONS, "--", *[str(candidate / name) for name in names], config["ssh_target"] + ":" + staging + "/"])
    command = ["sudo", "-n", "python3", "-I", config["publisher"]]
    sealed = strict_json(_ssh(config, [*command, "stage-upload", "--policy", policy,
                                      "--upload", staging, "--manifest-sha256", expected_sha]))
    require(sealed.get("status") == "SEALED" and sealed.get("transfer_manifest_sha256") == expected_sha and
            sealed.get("delta_id") == manifest["delta_id"] and sealed.get("policy_sha256") == config["policy_sha256"][domain],
            "sealed upload identity differs")
    incoming = _remote_path(sealed["incoming_path"])
    require(incoming.startswith("/var/lib/market-data/production-data-incoming/"), "sealed incoming path differs")
    received = strict_json(_ssh(config, [*command, "receive", "--policy", policy, "--incoming", incoming,
                                         "--manifest-sha256", expected_sha]))
    require(received.get("status") == "RECEIVED" and received.get("transfer_manifest_sha256") == expected_sha and
            received.get("delta_id") == manifest["delta_id"], "host receive evidence mismatch")
    validation = strict_json(_ssh(config, [*command, "validate", "--policy", policy, "--delta-id", manifest["delta_id"]]))
    require(validation.get("status") == "PASS" and validation.get("transfer_manifest_sha256") == expected_sha and
            validation.get("validation_image", {}).get("image_id") == config["image_id"] and
            validation.get("policy_sha256") == config["policy_sha256"][domain], "host validation identity mismatch")
    report_path = _remote_path(validation["report_path"])
    report_sha = validation["report_sha256"]
    require(HEX64.fullmatch(report_sha) and _remote_hash(config, report_path) == report_sha, "host validation report hash differs")
    require(sha256_file(manifest_path) == expected_sha, "local manifest changed during transfer")
    publication = strict_json(_ssh(config, [*command, "publish", "--policy", policy, "--delta-id", manifest["delta_id"],
                                            "--validation-report", report_path, "--validation-sha256", report_sha]))
    require(publication.get("status") in {"PUBLISHED", "NO_CHANGE"} and publication.get("delta_id") == manifest["delta_id"] and
            publication.get("policy_sha256") == config["policy_sha256"][domain], "publication evidence differs")
    require(_remote_hash(config, publication["receipt_path"]) == publication["receipt_sha256"], "publication receipt hash differs")
    return publication


def _selected_baseline(config: dict, domain: str) -> tuple[Path, str, dict]:
    root = Path(config["baseline_root"])
    pin = config["baseline_manifest_sha256"]
    verify_baseline(root, pin)
    pointer = Path(config["runtime_root"]) / "continuations" / (domain + ".json")
    if pointer.exists():
        value = strict_json(_unlinked(pointer).read_bytes())
        require(set(value) == {"schema_version", "domain", "approved_commit", "approved_tree", "baseline_root", "baseline_manifest_sha256", "receipt_sha256"}
                and value["schema_version"] == "production-data-continuation/1" and value["domain"] == domain
                and value["approved_commit"] == config["approved_commit"] and value["approved_tree"] == config["approved_tree"]
                and HEX64.fullmatch(value["receipt_sha256"]), "continuation approval differs")
        root = _absolute(value["baseline_root"])
        require(root.resolve().is_relative_to(Path(config["runtime_root"]).resolve() / "runs") and root.name == "continuation", "continuation escaped approved runtime")
        pin = value["baseline_manifest_sha256"]
        require(sha256_file(root.parent / "publication.json") == value["receipt_sha256"], "continuation receipt differs")
    return root, pin, verify_baseline(root, pin)


def _save_continuation(config: dict, domain: str, baseline: Path, work: Path, source: Path, receipt: dict) -> None:
    # Preserve successful remote evidence even if the subsequent replica download fails.
    receipt_path = work / "publication.json"
    receipt_path.write_bytes(canonical_json_bytes(receipt))
    target = work / "continuation"
    shutil.copytree(baseline, target)
    manifest = strict_json((target / "baseline_manifest.json").read_bytes())
    if domain == "akshare":
        for name in CONTINUATION_FILES:
            shutil.copy2(source / "01_data" / name, target / name)
    else:
        contract = _host_contract().contract_for_domain(domain)
        allowed = set(contract["payloads"].values()) | {contract["status_path"]}
        if domain == "soybean_export_sales":
            allowed.add("01_data/processed/soybean_export_sales/soybean_export_sales_weekly.manifest.json")
        require(set(receipt["formal_files"]) == allowed, "publication file set differs")
        for relative, expected in receipt["formal_files"].items():
            local = target / relative.removeprefix("01_data/")
            local.parent.mkdir(parents=True, exist_ok=True)
            local.unlink(missing_ok=True)
            _run(["scp", *SSH_OPTIONS, "--", config["ssh_target"] + ":" + config["remote_allocation"] + "/" + relative, str(local)])
            require(_identity(local) == {k: expected[k] for k in ("sha256", "size_bytes")}, "published continuation bytes differ")
    records = []
    for path in sorted(target.rglob("*")):
        if path.is_file() and path.name != "baseline_manifest.json":
            records.append({"path": path.relative_to(target).as_posix(), **_identity(path)})
    manifest["files"] = records
    (target / "baseline_manifest.json").write_bytes(canonical_json_bytes(manifest))
    pin = sha256_file(target / "baseline_manifest.json")
    verify_baseline(target, pin)
    value = {"schema_version": "production-data-continuation/1", "domain": domain,
             "approved_commit": config["approved_commit"], "approved_tree": config["approved_tree"],
             "baseline_root": str(target), "baseline_manifest_sha256": pin,
             "receipt_sha256": sha256_file(receipt_path)}
    pointer = Path(config["runtime_root"]) / "continuations" / (domain + ".json")
    pointer.parent.mkdir(parents=True, exist_ok=True)
    temporary = pointer.with_name(pointer.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_bytes(canonical_json_bytes(value))
    os.replace(temporary, pointer)


def run_domain(config: dict, domain: str, *, run_root: Path | None = None,
               publish: bool = False, control_root: Path | None = None,
               end_date: date | None = None) -> dict:
    validate_config(config)
    require(domain in DOMAINS, "unapproved producer domain")
    requested_end_date = (
        resolve_business_end_date(end_date) if domain == "akshare" else None
    )
    require(domain == "akshare" or end_date is None,
            "business end date is only valid for AkShare")
    require(sys.flags.isolated and sys.dont_write_bytecode, "formal entrypoint requires Python -I -B")
    require(Path(sys.executable).resolve() == Path(config["python"]).resolve(), "running Python differs from approval")
    control = (control_root or ROOT).resolve()
    require(control == ROOT, "caller supplied control source is forbidden")
    verify_clean_detached_clone(control, config)
    runtime = Path(config["runtime_root"]).resolve()
    require(run_root is None or Path(run_root).resolve() == runtime, "run root differs from configuration")
    require(not runtime.is_relative_to(control) and not control.is_relative_to(runtime), "runtime must be outside control clone")
    runtime.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        stack.enter_context(_domain_lock(runtime / "locks" / (domain + ".lock")))
        if domain == "akshare":
            stack.enter_context(_domain_lock(Path(config["full_daily_lock_path"])))
        baseline_root, baseline_sha, baseline = _selected_baseline(config, domain)
        public = _public_baseline(config, remote=publish) if domain == "akshare" else None
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ").lower() + "-" + uuid.uuid4().hex[:12]
        work = runtime / "runs" / run_id
        work.mkdir(parents=True, exist_ok=False)
        source = work / "source"
        # file:// uses Git's transport from the already verified approved control
        # clone; --no-hardlinks keeps this run's object database independent.
        _run(["git", "-c", "core.autocrlf=false", "-c", "core.fsmonitor=false",
              "clone", "--no-hardlinks", "--depth=1", "--no-tags", "--no-checkout",
              control.as_uri(), str(source)])
        _git(source, "remote", "set-url", "origin", config["origin"])
        _git(source, "-c", "core.autocrlf=false", "checkout", "--detach", config["approved_commit"])
        producer = verify_clean_detached_clone(source, config)
        data = source / "01_data"
        data.mkdir(exist_ok=True)
        for relative, identity in baseline.items():
            target = data / relative
            require(not target.exists(), "baseline would overwrite tracked source")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(baseline_root / relative, target)
            require(_identity(target) == identity, "baseline replica hash differs")
        verify_clean_detached_clone(source, config, allow_runtime=True)
        credentials = {}
        if domain != "akshare":
            entry = config["secrets"]["nass" if domain == "soybean_crop_progress" else "fas-export-sales"]
            credentials = _secret_environment(Path(entry["path"]), entry["keys"])
        # This child is a verified Windows Git checkout, not the sealed /app
        # container runtime.  Its business entrypoints bind identity through
        # git rev-parse; setting the container marker would also require the
        # matching immutable /app/RELEASE.json and must therefore be avoided.
        env = safe_child_environment(credentials)
        flags = _provider_flags(domain, source, end_date=requested_end_date)
        try:
            _run([config["python"], "-I", "-B", "-X", "utf8",
                  str(source / SOURCES[domain]), *flags], cwd=source, env=env)
        finally:
            credentials.clear()
            env.clear()
            verify_clean_detached_clone(source, config, allow_runtime=True)
            verify_clean_detached_clone(control, config)
            verify_baseline(baseline_root, baseline_sha)
        mutable = set(CONTINUATION_FILES) if domain == "akshare" else ({"update_status/soybean_export_sales.json"} if domain == "soybean_export_sales" else set())
        for relative, identity in baseline.items():
            if relative not in mutable:
                require(_identity(data / relative) == identity, "provider modified unrelated baseline bytes")
        date_evidence = (
            _akshare_end_date_evidence(data, requested_end_date)
            if domain == "akshare" and requested_end_date is not None
            else {}
        )
        result = {"run_id": run_id, "domain": domain, "producer": producer, "published": False,
                  "baseline_manifest_sha256": baseline_sha, "source": str(source),
                  "source_mode": "approved-control-file-transport-independent-shallow-clone",
                  **date_evidence}
        if domain == "akshare":
            package = _build_public_package(source, public, work / "packages")
            previous = strict_json((public / "manifest.json").read_bytes())
            changed = package.manifest["delivery_identity_sha256"] != previous["delivery_identity_sha256"]
            result.update({"candidate": str(package.directory), "manifest_sha256": sha256_file(package.directory / "manifest.json"),
                           "status": "CANDIDATE" if changed else "NO_CHANGE"})
            if publish and changed:
                _check_public_pointer(_public_pointer(config), previous)
                transported = _run([config["python"], "-I", "-B", "-X", "utf8",
                                    str(source / "04_scripts/transfer_public_data_package.py"),
                                    "--package", str(package.directory), "--ssh-target", config["ssh_target"],
                                    "--remote-store-root", config["remote_store_root"], "--activation-image-id", config["image_id"]], cwd=source)
                receipt = strict_json(transported.stdout.strip().splitlines()[-1])
                _check_public_pointer(_public_pointer(config), package.manifest)
                require(receipt.get("package_id") == package.package_id and receipt.get("status") in {"SYNCED", "NO_CHANGE"}
                        and receipt.get("transport") == "PASS", "public transport receipt invalid")
                _save_continuation(config, domain, baseline_root, work, source, receipt)
                result.update(status="PUBLISHED", published=True)
            elif publish:
                pointer = _public_pointer(config)
                _check_public_pointer(pointer, previous)
                remote_manifest_sha = _remote_hash(config, config["remote_store_root"] + "/releases/" + pointer["package_id"] + "/manifest.json")
                require(remote_manifest_sha == sha256_file(public / "manifest.json"), "unchanged public manifest identity drifted")
                _check_public_pointer(_public_pointer(config), previous)
                receipt = {"schema_version": "production-data-akshare-no-change/1", "status": "NO_CHANGE",
                           "package_id": pointer["package_id"], "remote_pointer": pointer,
                           "remote_manifest_sha256": remote_manifest_sha, "producer": producer,
                           "verified_at_utc": datetime.now(timezone.utc).isoformat(), "published": False}
                _save_continuation(config, domain, baseline_root, work, source, receipt)
        else:
            seasonal_skip = (_crop_out_of_season_audit(source, producer)
                             if domain == "soybean_crop_progress" else None)
            if seasonal_skip is not None:
                audit_path, audit = seasonal_skip
                result.update({
                    "status": "SKIPPED_OUT_OF_SEASON",
                    "provider_status": "out_of_season",
                    "provider_run_id": audit["run_id"],
                    "audit_path": str(audit_path),
                    "audit_sha256": sha256_file(audit_path),
                })
                verify_clean_detached_clone(source, config, allow_runtime=True)
                verify_clean_detached_clone(control, config)
                verify_baseline(baseline_root, baseline_sha)
                (work / "result.json").write_bytes(canonical_json_bytes(result))
                return result
            manifest_path = collect_delta(domain, source, work / "candidate", producer, baseline, run_id=run_id)
            manifest = strict_json(manifest_path.read_bytes())
            changed = manifest["run"]["business_status"] != "no_change"
            result.update({"candidate": str(manifest_path.parent), "manifest_sha256": sha256_file(manifest_path),
                           "status": "CANDIDATE" if changed else "NO_CHANGE"})
            if publish and changed:
                receipt = invoke_publisher(config, manifest_path.parent)
                _save_continuation(config, domain, baseline_root, work, source, receipt)
                result.update(status=receipt["status"], published=receipt["status"] == "PUBLISHED")
        verify_clean_detached_clone(source, config, allow_runtime=True)
        verify_clean_detached_clone(control, config)
        verify_baseline(baseline_root, baseline_sha)
        (work / "result.json").write_bytes(canonical_json_bytes(result))
        return result
