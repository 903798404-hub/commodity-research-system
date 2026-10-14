"""Pinned, isolated Linux collector and atomic publication for warehouse receipts.

The host uses stdlib only. Collection never mounts a writable production store.
"""
from __future__ import annotations

import argparse
import datetime as dt
try:
    import fcntl
except ImportError:
    fcntl = None
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import uuid

CONFIG_ROOT = Path("/etc/market-data/warehouse-server")
WORK_ROOT = Path("/var/lib/market-data/production-data-automation/warehouse-receipts")
BEIJING = dt.timezone(dt.timedelta(hours=8))
SNAPSHOT_FILES = {"raw.csv", "daily.csv", "manifest.json"}


def require(condition, message):
    if not condition: raise ValueError(message)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def protected(path):
    path = Path(path)
    require(path.is_absolute(), "host path must be absolute")
    for ancestor in [path, *path.parents]:
        st = ancestor.lstat()
        require(not ancestor.is_symlink() and st.st_uid == 0 and not st.st_mode & 0o022,
                "host path is not protected")
    return path


def regular(path):
    require(path.is_file() and not path.is_symlink() and path.stat().st_nlink == 1,
            "unsafe snapshot file")
    return path


def read_json(path):
    return json.loads(regular(path).read_text(encoding="utf-8", errors="strict"))


def atomic_json(path, value, *, mode=0o644):
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.flush(); os.fsync(stream.fileno())
    temporary.chmod(mode)
    os.replace(temporary, path)


def verify_config(path):
    require(os.name == "posix" and os.geteuid() == 0, "protected warehouse controller requires Linux root")
    require(fcntl is not None, "warehouse controller requires Linux file locks")
    path = protected(path)
    require(path.parent == CONFIG_ROOT and path.stat().st_mode & 0o777 == 0o600, "config path or mode invalid")
    config = read_json(path)
    require(set(config) == {"schema_version", "source_root", "commit", "tree", "image_id", "store_root", "secret_file", "data_disk_uuid"}, "config fields invalid")
    require(config["schema_version"] == "warehouse-server/1", "config schema invalid")
    require(re.fullmatch(r"[0-9a-f]{40}", config["commit"]) and re.fullmatch(r"[0-9a-f]{40}", config["tree"]), "source identity invalid")
    source = protected(config["source_root"])
    require(source.is_relative_to(Path("/opt/market-data-releases")), "source outside approved release roots")
    def git(*args):
        return subprocess.check_output(["git", "-C", str(source), *args], text=True, timeout=30).strip()
    require(git("rev-parse", "HEAD") == config["commit"] and git("rev-parse", "HEAD^{tree}") == config["tree"]
            and git("status", "--porcelain") == "", "approved source identity differs")
    require(git("remote", "get-url", "origin") == "https://github.com/903798404-hub/commodity-research-system.git", "source origin differs")
    require(Path(__file__).resolve() == source / "09_deploy/warehouse_server/run.py", "controller source binding differs")
    require(re.fullmatch(r"sha256:[0-9a-f]{64}", config["image_id"]), "image ID invalid")
    image = json.loads(subprocess.check_output(["docker", "image", "inspect", config["image_id"]], timeout=30))[0]
    labels = image["Config"].get("Labels") or {}
    require(image["Id"] == config["image_id"] and labels.get("org.opencontainers.image.revision") == config["commit"]
            and labels.get("market-data.git.tree") == config["tree"], "collector image source identity differs")
    mount = subprocess.check_output(["findmnt", "-n", "-o", "UUID,TARGET,OPTIONS", "--target", "/var/lib/market-data"], text=True, timeout=15).split()
    require(len(mount)==3 and mount[0]==config["data_disk_uuid"] and mount[1]=="/var/lib/market-data" and "rw" in mount[2].split(","), "data disk identity differs")
    disk = os.statvfs("/var/lib/market-data")
    require(disk.f_bavail * disk.f_frsize > 1024**3, "insufficient collector disk space")
    store = protected(config["store_root"])
    require(store.is_relative_to(Path("/var/lib/market-data/production-runtime"))
            and store.parts[-3:]==("01_data","processed","warehouse_receipts"), "store path outside warehouse allocation")
    secret = protected(config["secret_file"])
    require(secret.parent == CONFIG_ROOT and secret.stat().st_mode & 0o777 == 0o600, "credential path or mode invalid")
    require(set(read_json(secret)) == {"token", "endpoint"}, "credential fields invalid")
    return config


def retry_needed(status, now):
    try:
        checked = dt.datetime.fromisoformat(status["checked_at"])
        if checked.tzinfo is None or checked > now:
            return True
        return (checked.astimezone(BEIJING).date() != now.astimezone(BEIJING).date()
                or status["status"] not in {"UPDATED", "NO_CHANGE"} or bool(status.get("pending_products")))
    except (ValueError, KeyError, TypeError):
        return True


def collector_command(config, work, name):
    return ["docker", "run", "--rm", "--name", name, "--pull", "never", "--network", "bridge",
        "--read-only", "--user", "65532:65532", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--memory", "768m", "--cpus", "1", "--pids-limit", "64", "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m",
        "--mount", f"type=bind,src={config['store_root']},dst=/baseline,readonly",
        "--mount", f"type=bind,src={work / 'output'},dst=/output",
        "--mount", f"type=bind,src={work / 'credential.json'},dst=/run/secrets/tushare.json,readonly",
        "--entrypoint", "python", config["image_id"], "-I", "-B", "/app/04_scripts/update_warehouse_receipts.py",
        "--baseline-root", "/baseline", "--candidate-root", "/output/candidate", "--raw-root", "/output/raw",
        "--secret-file", "/run/secrets/tushare.json"]


def publish_candidate(store, candidate, expected_baseline_sha):
    """Publish only a pinned producer's fully replayed candidate, under host lock."""
    store, candidate = Path(store), Path(candidate)
    require(sha(regular(store / "current.json")) == expected_baseline_sha, "FAIL_STALE_BASE")
    pointer = read_json(candidate / "current.json")
    sid = pointer.get("snapshot_id")
    require(re.fullmatch(r"[0-9a-f]{64}", sid or "") and pointer.get("schema_version")=="warehouse-receipts/1", "candidate pointer invalid")
    folder = candidate / "snapshots" / sid
    require(not folder.is_symlink() and {p.name for p in folder.iterdir()} == SNAPSHOT_FILES, "candidate inventory differs")
    manifest = read_json(folder / "manifest.json")
    require(sha(folder / "manifest.json") == pointer["manifest_sha256"] and manifest["snapshot_id"]==sid,
            "candidate manifest identity differs")
    for name in ("raw", "daily"):
        require(sha(regular(folder/f"{name}.csv"))==manifest[f"{name}_sha256"], "candidate payload identity differs")
    require(hashlib.sha256((folder/"raw.csv").read_bytes()+(folder/"daily.csv").read_bytes()).hexdigest()==sid, "candidate content identity differs")
    target = store / "snapshots" / sid
    if target.exists():
        protected(target)
        require({p.name for p in target.iterdir()} == SNAPSHOT_FILES
                and all(sha(regular(target/name))==sha(folder/name) for name in SNAPSHOT_FILES), "immutable snapshot collision")
    else:
        target.mkdir(mode=0o755)
        for name in SNAPSHOT_FILES:
            require((folder/name).stat().st_size <= 256*1024**2, "candidate payload too large")
            with (target/name).open("xb") as stream: stream.write((folder/name).read_bytes())
            (target/name).chmod(0o444)
        require(all(sha(target/name)==sha(folder/name) for name in SNAPSHOT_FILES), "publication copy differs")
        target.chmod(0o555)
    require(sha(store/"current.json")==expected_baseline_sha, "FAIL_STALE_BASE")
    atomic_json(store / "current.json", pointer)
    return sid


def execute(config, *, conditional=False):
    store = Path(config["store_root"])
    now = dt.datetime.now(BEIJING)
    status_path = store / "update_status.json"
    if conditional and status_path.is_file() and not retry_needed(read_json(status_path), now):
        return dict(status="SKIPPED_CURRENT", checked_at=now.isoformat())
    WORK_ROOT.mkdir(exist_ok=True, mode=0o700)
    protected(WORK_ROOT)
    with (WORK_ROOT/"update.lock").open("a") as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        name = "warehouse-" + now.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:12]
        work = WORK_ROOT/name
        work.mkdir(mode=0o700)
        old_pointer = regular(store/"current.json").read_bytes()
        old_sha = hashlib.sha256(old_pointer).hexdigest()
        (work/"previous-current.json").write_bytes(old_pointer)
        output = work/"output"; output.mkdir(mode=0o700); os.chown(output,65532,65532)
        credential = work/"credential.json"
        credential.write_bytes(Path(config["secret_file"]).read_bytes()); os.chown(credential,65532,65532); credential.chmod(0o400)
        try:
            completed = subprocess.run(collector_command(config,work,name),capture_output=True,timeout=900)
            require(completed.returncode==0, "isolated warehouse collection failed")
            candidate = output/"candidate"
            result = read_json(candidate/"update_status.json")
            require(result["baseline_pointer_sha256"]==old_sha
                    and result["candidate_pointer_sha256"]==sha(candidate/"current.json"), "collector baseline binding differs")
            require(result["status"] in {"UPDATED","NO_CHANGE","WAITING_FOR_SOURCE"}, "collector status invalid")
            if result["business_changed"]:
                sid = publish_candidate(store,candidate,old_sha)
            else:
                require(sha(store/"current.json")==old_sha
                        and (candidate/"current.json").read_bytes()==old_pointer, "FAIL_STALE_BASE")
                sid = read_json(store/"current.json")["snapshot_id"]
            result.update(published=True,snapshot_id=sid,producer_commit=config["commit"],producer_image=config["image_id"])
            atomic_json(status_path,result)
            atomic_json(work/"result.json",result,mode=0o600)
            if result["business_changed"]:
                # The immutable published copy is retained. Remove only the three
                # verified duplicate files from this owned, completed staging run.
                staged = candidate/"snapshots"/sid
                require(staged.resolve().is_relative_to(work.resolve())
                        and {p.name for p in staged.iterdir()}==SNAPSHOT_FILES, "staging cleanup boundary invalid")
                for filename in SNAPSHOT_FILES:
                    require(sha(regular(staged/filename))==sha(store/"snapshots"/sid/filename), "staging duplicate differs")
                for filename in SNAPSHOT_FILES: (staged/filename).unlink()
                staged.rmdir()
            return result
        except Exception as exc:
            # If the pointer committed but a later receipt write failed, restore
            # only this exact candidate under the same exclusive publication lock.
            if "candidate" in locals() and (candidate/"current.json").is_file():
                current = read_json(store/"current.json")
                attempted = read_json(candidate/"current.json")
                if current == attempted and hashlib.sha256((store/"current.json").read_bytes()).hexdigest() != old_sha:
                    atomic_json(store/"current.json",json.loads(old_pointer))
            if isinstance(exc,subprocess.TimeoutExpired):
                subprocess.run(["docker","rm","-f",name],capture_output=True,timeout=30,check=True)
            failure=dict(schema_version="warehouse-update/1",status="FAILED",checked_at=now.isoformat(),error_type=type(exc).__name__,published=False)
            atomic_json(status_path,failure)
            atomic_json(work/"result.json",failure,mode=0o600)
            raise
        finally:
            credential.unlink(missing_ok=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config",type=Path,required=True)
    parser.add_argument("--retry-if-needed",action="store_true")
    args=parser.parse_args()
    try:
        result=execute(verify_config(args.config),conditional=args.retry_if_needed)
        print(json.dumps(result,ensure_ascii=False)); return 0
    except Exception as exc:
        print(json.dumps(dict(status="FAILED",error_type=type(exc).__name__))); return 1


if __name__=="__main__": raise SystemExit(main())
