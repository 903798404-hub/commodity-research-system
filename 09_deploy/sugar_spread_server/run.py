"""Build, replay and conditionally publish white sugar quotes on the Linux host."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
CONFIG_ROOT = Path("/etc/market-data/sugar-spread-server")
WORK_ROOT = Path("/var/lib/market-data/production-data-automation/sugar-spreads")
PRODUCTION_ROOT = Path("/var/lib/market-data/production-runtime")
EMPTY_POINTER = b'{"schema_version":"sugar-spread-quotes/1","markets":{}}\n'


def load_host():
    spec = importlib.util.spec_from_file_location(
        "sugar_data_host", ROOT / "09_deploy/production_data_delivery/activate_production_data_delta.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verify_config(host, path: Path) -> tuple[dict, Path]:
    path = host._protected(host._under(path, CONFIG_ROOT))
    host.require(path.parent == CONFIG_ROOT and path.stat().st_mode & 0o777 == 0o600,
                 "Sugar scheduler config must be protected and mode 0600")
    config = host._exact(host.read_json(path), {"schema_version", "commit", "tree", "image_id",
                         "data_disk_uuid", "data_root"}, "sugar scheduler config")
    host.require(config["schema_version"] == "sugar-spread-server/1" and
                 all(re.fullmatch(r"[0-9a-f]{40}", config[key]) for key in ("commit", "tree")) and
                 host.IMAGE_ID.fullmatch(config["image_id"]), "Sugar scheduler identity invalid")
    source = host._protected(host._under(ROOT, Path("/opt/market-data-releases")))
    identity = host._git_identity(source)
    host.require(all(identity[key] == config[key] for key in ("commit", "tree")), "Sugar scheduler source changed")
    host._inspect_image(config)
    root = host._protected(host._under(config["data_root"], PRODUCTION_ROOT))
    host.require(root.parent.parent == PRODUCTION_ROOT and root.name == "01_data", "Unexpected sugar data root")
    volume = subprocess.check_output(["findmnt", "-n", "-o", "UUID,TARGET,OPTIONS", "--target", str(root)],
                                     text=True, timeout=15).split()
    disk = os.statvfs(root)
    host.require(len(volume) == 3 and volume[0] == config["data_disk_uuid"] and volume[1] == "/var/lib/market-data"
                 and "rw" in volume[2].split(",")
                 and disk.f_bavail * disk.f_frsize > 256 * 1024**2 and disk.f_favail > 1000,
                 "Sugar data disk identity or capacity differs")
    return config, root


def worker_command(config: dict, baseline: Path, output: Path, timestamp: str, name: str, *, validate: bool) -> list[str]:
    mode = "validate" if validate else "collect"
    return ["docker", "run", "--rm", "--name", name, "--pull", "never",
            "--network", "none" if validate else "bridge", "--read-only", "--user", "65532:65532",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--memory", "512m",
            "--cpus", "1", "--pids-limit", "64",
            "--mount", f"type=bind,src={baseline},dst=/baseline,readonly",
            "--mount", f"type=bind,src={output},dst=/output" + (",readonly" if validate else ""),
            "--mount", f"type=bind,src={ROOT / '03_src'},dst=/source/03_src,readonly",
            "--mount", f"type=bind,src={Path(__file__).parent},dst=/source/09_deploy/sugar_spread_server,readonly",
            "--entrypoint", "python", config["image_id"], "-I", "-B", "/source/09_deploy/sugar_spread_server/collect.py",
            mode, "--baseline", "/baseline", "--candidate", "/output/candidate", "--as-of", timestamp]


def worker(config: dict, baseline: Path, output: Path, timestamp: str, name: str, *, validate: bool) -> dict:
    command = worker_command(config, baseline, output, timestamp, name, validate=validate)
    try:
        output_bytes = subprocess.check_output(command, timeout=90 if validate else 465)
    except subprocess.TimeoutExpired:
        subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, timeout=20, check=False)
        raise
    return json.loads(output_bytes.decode("ascii", errors="strict"))


def file_bytes(host, path: Path, limit: int) -> bytes:
    host._no_links(path)
    host.require(path.is_file() and path.stat().st_nlink == 1 and path.stat().st_size <= limit,
                 "Unsafe sugar publication file")
    return path.read_bytes()


def pointer_bytes(host, store: Path) -> bytes:
    path = host._protected(store / "current.json", exists=False)
    return file_bytes(host, path, 4096) if path.exists() else EMPTY_POINTER


def verify_dashboard_mount(host, root: Path) -> None:
    mounts = json.loads(subprocess.check_output(
        ["docker", "inspect", "--format", "{{json .Mounts}}", "spread-dashboard"], timeout=20,
    ))
    mounts = [value for value in mounts if value["Destination"] == "/runtime/01_data"]
    host.require(len(mounts) == 1 and mounts[0]["Type"] == "bind" and mounts[0]["Source"] == str(root)
                 and mounts[0]["RW"] is False, "Sugar allocation is not the active dashboard read-only root")


def publish_candidate(host, store: Path, target: Path, result: dict, old: bytes) -> bool:
    """Publish immutable objects first and atomically switch the single pointer last."""
    host.require(pointer_bytes(host, store) == old, "Sugar publication baseline changed")
    pointer = result["pointer"]
    old_pointer = json.loads(old)
    if pointer == old_pointer:
        return False
    prior = old_pointer["markets"]
    host.require(prior.keys() <= pointer["markets"].keys(), "Sugar publication drops a market")
    store.mkdir(parents=True, exist_ok=True, mode=0o755)
    for market, digest in pointer["markets"].items():
        host.require(market in {"domestic", "foreign"} and re.fullmatch(r"[0-9a-f]{64}", digest),
                     "Unexpected sugar pointer artifact identity")
        filename = digest + ".csv.gz"
        installed = host._protected(store / "snapshots" / filename, exists=False)
        if digest != prior.get(market):
            candidate = target / "snapshots" / filename
            file_bytes(host, candidate, 8 * 1024**2)
            host.require(host.sha256_file(candidate) == digest, "Verified sugar artifact changed")
            if not installed.exists():
                host._atomic_copy(candidate, installed)
                installed.chmod(0o444)
        host.require(host.sha256_file(installed) == digest, "Published sugar artifact hash differs")
    host.require(pointer_bytes(host, store) == old, "Sugar publication baseline changed")
    host._atomic_json(store / "current.json", pointer)
    return True


def execute(host, config: dict, root: Path, work: Path, *, publish: bool) -> dict:
    if publish:
        verify_dashboard_mount(host, root)
    baseline = host._protected(root / "processed/sugar_spreads", exists=False)
    store = baseline / "official-releases"
    old = pointer_bytes(host, store)
    (work / "previous-current.json").write_bytes(old)
    if not baseline.exists():
        (work / "empty").mkdir(mode=0o755)
    input_path = baseline if baseline.exists() else work / "empty"
    output = work / "output"
    output.mkdir(mode=0o700)
    os.chown(output, 65532, 65532)
    timestamp = datetime.now(timezone.utc).isoformat()
    run_name = "sugar-spreads-" + work.name
    collected = worker(config, input_path, output, timestamp, run_name + "-collect", validate=False)
    verified = worker(config, input_path, output, timestamp, run_name + "-validate", validate=True)
    host.require(collected == verified and verified["baseline_sha256"] == host.sha256_file(work / "previous-current.json"),
                 "Sugar collector and offline validator disagree")
    changed = False
    if publish:
        verify_dashboard_mount(host, root)
        changed = publish_candidate(host, store, output / "candidate", verified, old)
    result = {**verified, "checked_at": timestamp, "published": changed, "candidate_validated": True,
              "commit": config["commit"], "image_id": config["image_id"], "evidence": str(work)}
    host._atomic_json(work / "result.json", result)
    return result


def run(config_path: Path, *, publish: bool) -> dict:
    if sys.platform != "linux" or os.geteuid() != 0 or not sys.flags.isolated or not sys.dont_write_bytecode:
        raise ValueError("Sugar scheduler requires protected Linux root Python -I -B")
    import fcntl

    host = load_host()
    config, root = verify_config(host, config_path)
    os.umask(0o022)
    host._protected(WORK_ROOT.parent)
    WORK_ROOT.mkdir(exist_ok=True, mode=0o700)
    host._protected(WORK_ROOT)
    with host._protected(WORK_ROOT / "update.lock", exists=False).open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "ALREADY_RUNNING"}
        work = WORK_ROOT / uuid4().hex
        work.mkdir(mode=0o700)
        try:
            return execute(host, config, root, work, publish=publish)
        except Exception as exc:
            host._atomic_json(work / "result.json", {"status": "FAILED", "error": type(exc).__name__,
                                                    "message": str(exc)[:500], "evidence": str(work)})
            raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    try:
        result = run(args.config, publish=args.publish)
    except Exception as exc:
        print(json.dumps({"status": "FAILED", "error": type(exc).__name__, "message": str(exc)[:500]},
                         ensure_ascii=True), file=sys.stderr)
        return 1
    print(json.dumps({key: value for key, value in result.items() if key != "receipts"}, ensure_ascii=True))
    return int(result["status"] in {"FAILED", "PARTIAL_FAILURE"})


if __name__ == "__main__":
    raise SystemExit(main())
