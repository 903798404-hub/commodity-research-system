"""Compact inactive public packages without dropping any version or history.

Standalone Python 3.10+ host tool. Current, rollback pins and recent versions
keep independent files. Older, byte-validated files share root-owned read-only
inodes. Manifests and paths stay intact; this is not a Docker prune or publisher.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import uuid

PACKAGE = re.compile(r"public-current-[0-9a-f]{24}")
HEX = re.compile(r"[0-9a-f]{64}")
SCHEMA = "public-current-production-package/2"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def sha(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def no_links(path):
    require(path.is_absolute(), "absolute path required")
    for parent in (path, *path.parents):
        require(not parent.is_symlink(), "symlink path refused")
    return path


def read(path):
    no_links(path)
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate JSON key")
            result[key] = value
        return result
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs)


def validate(package):
    no_links(package)
    require(PACKAGE.fullmatch(package.name), "package name invalid")
    manifest = read(package / "manifest.json")
    require(manifest.get("schema_version") in {SCHEMA, "public-current-production-package/3"} and manifest.get("package_id") == package.name,
            "unsupported package identity")
    require(digest(manifest["current_identities"]) == manifest["current_identity_sha256"], "current identities invalid")
    delivery_identity = {"current_identity_sha256": manifest["current_identity_sha256"],
                       "delivery_artifacts": {k: v["business_sha256"] for k, v in sorted(manifest["delivery_artifacts"].items())}}
    if manifest["schema_version"] == "public-current-production-package/3":
        require(manifest["delivery_artifacts"] and all(v.get("storage") == "MONTHLY_PARQUET" for v in manifest["delivery_artifacts"].values()), "partition storage invalid")
        delivery_identity["artifact_storage"] = "MONTHLY_PARQUET"
    delivery = digest(delivery_identity)
    require(delivery == manifest["delivery_identity_sha256"] and package.name == "public-current-" + delivery[:24],
            "delivery identity invalid")
    created = datetime.fromisoformat(manifest["created_at"].replace("Z", "+00:00"))
    require(created.tzinfo is not None, "timezone required")
    files = []
    for path in sorted((package / "data").rglob("*")):
        require(not path.is_symlink(), "package symlink refused")
        mode = path.lstat().st_mode
        require(stat.S_ISREG(mode) or stat.S_ISDIR(mode), "special file refused")
        if stat.S_ISREG(mode):
            files.append({"path": path.relative_to(package / "data").as_posix(),
                          "size_bytes": path.stat().st_size, "sha256": sha(path)})
    require(files and files == manifest["files"] and len(files) == manifest["file_count"], "package bytes differ")
    require(digest(files) == manifest["bundle_sha256"], "bundle differs")
    require({p.name for p in package.iterdir()} == {"manifest.json", "data"}, "unexpected package files")
    return manifest, created


def plan(store, *, now, keep_days=7, keep_count=3, pins=(), busy_paths=()):
    no_links(store)
    require(keep_days >= 1 and keep_count >= 2 and now.tzinfo is not None, "retention bounds invalid")
    pointer = read(store / "current.json")
    current = pointer["package_id"]
    require(PACKAGE.fullmatch(current), "current id invalid")
    current_manifest, _ = validate(store / "releases" / current)
    for key in ("current_identity_sha256", "delivery_identity_sha256", "bundle_sha256"):
        require(pointer[key] == current_manifest[key], "current pointer differs")
    packages = []
    for path in sorted((store / "releases").iterdir()):
        require(path.is_dir() and not path.is_symlink(), "unsafe releases entry")
        manifest, created = validate(path)
        packages.append((path, manifest, created))
    required = {current, *pins}
    require(required <= {p.name for p, _, _ in packages}, "rollback pin missing")
    required.update(p.name for p, _, _ in sorted(packages, key=lambda x: (x[2], x[0].name), reverse=True)[:keep_count])
    entries = []
    for path, manifest, created in packages:
        reasons = []
        if path.name in required: reasons.append("CURRENT_ROLLBACK_OR_NEWEST")
        if created >= now - timedelta(days=keep_days): reasons.append("RECENT_FULL_COPY")
        if any(path == p or path in p.parents or p in path.parents for p in busy_paths): reasons.append("LIVE_REFERENCE")
        entries.append({"package_id": path.name, "action": "KEEP_INDEPENDENT" if reasons else "COMPACT",
                        "reasons": reasons, "manifest_sha256": sha(path / "manifest.json"), "files": manifest["files"]})
    return {"store": str(store), "current_pointer_sha256": sha(store / "current.json"),
            "current_package_id": current, "entries": entries}


def immutable_object(pool, source, record):
    no_links(pool)
    require(HEX.fullmatch(record["sha256"]), "object digest invalid")
    target = pool / record["sha256"]
    if not target.exists():
        temp = pool / (".copy-" + uuid.uuid4().hex)
        try:
            with temp.open("xb") as output, source.open("rb") as incoming:
                shutil.copyfileobj(incoming, output, 1024 * 1024)
                output.flush(); os.fsync(output.fileno())
            require(temp.stat().st_size == record["size_bytes"] and sha(temp) == record["sha256"], "object copy differs")
            temp.chmod(0o444)
            os.replace(temp, target)
        finally:
            if temp.exists(): temp.unlink()
    require(target.is_file() and not target.is_symlink() and target.stat().st_uid == getattr(os, "getuid", lambda: 0)()
            and not target.stat().st_mode & 0o222 and target.stat().st_size == record["size_bytes"]
            and sha(target) == record["sha256"], "pooled object is mutable or corrupt")
    return target


def private_pool(pool):
    require(pool.stat().st_uid == getattr(os, "getuid", lambda: 0)() and not pool.stat().st_mode & 0o077, "private owned pool required")


def compact(plan_result, pool):
    """Caller owns publication/maintenance locks; fail closed on stale plans."""
    store = Path(plan_result["store"]); no_links(store); no_links(pool)
    require(sha(store / "current.json") == plan_result["current_pointer_sha256"], "current changed; replan")
    require(pool.stat().st_dev == store.stat().st_dev, "pool must share filesystem")
    private_pool(pool)
    changed = []
    for entry in plan_result["entries"]:
        if entry["action"] != "COMPACT": continue
        package = store / "releases" / entry["package_id"]
        require(sha(package / "manifest.json") == entry["manifest_sha256"], "manifest changed; replan")
        manifest, _ = validate(package)
        require(manifest["files"] == entry["files"], "stale file list")
        for record in entry["files"]:
            relative = PurePosixPath(record["path"])
            require(not relative.is_absolute() and ".." not in relative.parts and "\\" not in record["path"], "unsafe file path")
            source = package / "data" / relative; no_links(source)
            require(source.stat().st_size == record["size_bytes"] and sha(source) == record["sha256"], "source changed")
            target = pool / record["sha256"]
            if target.exists() and not target.is_symlink() and os.path.samefile(source, target):
                immutable_object(pool, source, record)
                continue
            require(source.stat().st_nlink == 1, "external hardlink refused")
            target = immutable_object(pool, source, record)
            temp = source.parent / (".link-" + uuid.uuid4().hex)
            try:
                os.link(target, temp)
                require(sha(temp) == record["sha256"], "linked bytes differ")
                os.replace(temp, source)
            finally:
                if temp.exists(): temp.unlink()
            changed.append({"path": str(source), "sha256": record["sha256"], "object": str(target)})
        validate(package)
        require(sha(package / "manifest.json") == entry["manifest_sha256"], "manifest altered")
    require(sha(store / "current.json") == plan_result["current_pointer_sha256"], "current altered")
    return changed


@contextmanager
def lock(path):
    import fcntl
    no_links(path)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    else:
        # A newly created publisher lock must remain writable by its store owner.
        owner = path.parent.stat()
        try:
            os.fchown(descriptor, owner.st_uid, owner.st_gid)
        except BaseException:
            os.close(descriptor)
            raise
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


def protected(path):
    no_links(path)
    for parent in (path, *path.parents):
        st = parent.stat()
        require(st.st_uid == 0 and not st.st_mode & 0o022, "protected root-owned path required")


def live_paths():
    ids = subprocess.check_output(["docker", "ps", "-aq"], timeout=30).decode().split()
    containers = json.loads(subprocess.check_output(["docker", "inspect", *ids], timeout=30)) if ids else []
    paths = [Path(m["Source"]) for c in containers for m in c["Mounts"]]
    namespaces = {}
    for container in containers:
        try:
            ns = os.readlink("/proc/" + str(container["State"]["Pid"]) + "/ns/mnt")
            namespaces[ns] = sorted(container["Mounts"], key=lambda m: len(m["Destination"]), reverse=True)
        except OSError:
            pass
    for process in Path("/proc").iterdir():
        if not process.name.isdigit(): continue
        try: descriptors = list((process / "fd").iterdir())
        except OSError: descriptors = []
        for ref in [process / "cwd", process / "root", *descriptors]:
            try: value = os.readlink(ref)
            except OSError: continue
            if value.startswith("/") and "/releases/public-current-" in value:
                try:
                    ns = os.readlink(process / "ns/mnt")
                    for mount in namespaces.get(ns, []):
                        if value == mount["Destination"] or value.startswith(mount["Destination"].rstrip("/") + "/"):
                            value = mount["Source"] + value[len(mount["Destination"]):]
                            break
                except OSError:
                    pass
                paths.append(Path(value))
    return paths


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    require(os.geteuid() == 0, "root maintenance operator required")
    protected(Path(__file__).resolve()); protected(args.config)
    config = read(args.config)
    require(config["schema_version"] == "public-package-retention/1" and sha(Path(__file__)) == config["tool_sha256"], "tool/config identity differs")
    require(config["keep_full_days"] >= 1 and config["keep_full_count"] >= 2, "unsafe retention bounds")
    state = Path(config["state_root"]); protected(state)
    pool = state / "objects"; pool.mkdir(mode=0o700, exist_ok=True); protected(pool)
    require(pool.stat().st_mode & 0o777 == 0o700, "pool permissions differ")
    report = {"schema_version": "public-package-retention-result/1", "mode": "APPLY" if args.apply else "AUDIT",
              "started_at": datetime.now(timezone.utc).isoformat(), "status": "PASS", "stores": [], "history_versions_deleted": 0}
    before = os.statvfs(state)
    with ExitStack() as stack:
        stack.enter_context(lock(state / "maintenance.lock"))
        for name in config["publisher_locks"]: stack.enter_context(lock(Path(name)))
        for item in config["stores"]:
            store = Path(item["root"]); no_links(store)
            require(store.name == "public-data-server-store", "only public package stores allowed")
            require(subprocess.check_output(["findmnt", "-rn", "-T", str(store), "-o", "UUID"], timeout=30).decode().strip() == config["data_disk_uuid"], "data disk unavailable or different")
            stack.enter_context(lock(store / ".current.lock"))
        busy = live_paths()
        for item in config["stores"]:
            store = Path(item["root"])
            # A whole-store application mount reads Current dynamically; it must not
            # pin all old versions. Exact package/child mounts and open files do pin.
            package_busy = []
            for path in busy:
                for alias in item.get("aliases", []):
                    alias = Path(alias)
                    if path == alias or alias in path.parents:
                        path = store / path.relative_to(alias)
                        break
                if any(parent.name == "releases" for parent in path.parents): package_busy.append(path)
            planned = plan(store, now=datetime.now(timezone.utc), keep_days=config["keep_full_days"],
                           keep_count=config["keep_full_count"], pins=item["protected_package_ids"], busy_paths=package_busy)
            changed = compact(planned, pool) if args.apply else []
            report["stores"].append({"plan": planned, "changed_files": changed})
    after = os.statvfs(state)
    report["available_bytes"] = after.f_bavail * after.f_frsize
    report["recovered_bytes"] = (after.f_bavail * after.f_frsize) - (before.f_bavail * before.f_frsize)
    report["disk_used_percent"] = round(100 * (after.f_blocks - after.f_bfree) / after.f_blocks, 2)
    report["capacity_status"] = "CRITICAL" if report["disk_used_percent"] >= 85 else "WARNING" if report["disk_used_percent"] >= 75 else "NORMAL"
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    destination = state / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex + ".json")
    with destination.open("xb") as out: out.write(canonical(report))
    destination.chmod(0o600)
    print(json.dumps({"status": report["status"], "capacity_status": report["capacity_status"],
                      "disk_used_percent": report["disk_used_percent"], "recovered_bytes": report["recovered_bytes"],
                      "changed_files": sum(len(s["changed_files"]) for s in report["stores"]),
                      "report": str(destination), "history_versions_deleted": 0}, sort_keys=True))
    return 1 if report["capacity_status"] == "CRITICAL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
