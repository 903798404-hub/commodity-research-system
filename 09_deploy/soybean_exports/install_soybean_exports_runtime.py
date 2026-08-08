from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


SCHEMA_VERSION = 1
DEFAULT_RUNTIME_ROOT = Path("/home/ubuntu/market-data-runtime/soybean-exports")
WRAPPERS = {
    "fgis": Path("09_deploy/soybean_exports/run_fgis_yearly_update.sh"),
    "fgis_upload": Path(
        "09_deploy/soybean_exports/run_fgis_uploaded_source_update.sh"
    ),
    "fas": Path("09_deploy/soybean_exports/run_fas_export_sales_update.sh"),
}
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


class InstallError(RuntimeError):
    pass


def _run_git(source_root: Path, *arguments: str, check: bool = True) -> str:
    completed = subprocess.run(
        ["git", "-C", str(source_root), *arguments],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if check and completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        raise InstallError(f"trusted source Git check failed: {detail}")
    return completed.stdout.strip()


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _current_user() -> str:
    if os.name == "posix":
        import pwd

        return pwd.getpwuid(os.geteuid()).pw_name
    return getpass.getuser()


def _verify_posix_owner_mode(path: Path, mode: int) -> None:
    if os.name != "posix":
        return
    details = path.stat()
    if details.st_uid != os.geteuid():
        raise InstallError(f"runtime path is not owned by the installer: {path}")
    if stat.S_IMODE(details.st_mode) != mode:
        raise InstallError(f"runtime path mode is not {mode:04o}: {path}")


def _ensure_directory(path: Path, mode: int) -> None:
    if path.is_symlink():
        raise InstallError(f"runtime directory must not be a symlink: {path}")
    path.mkdir(parents=True, exist_ok=True, mode=mode)
    if not path.is_dir():
        raise InstallError(f"runtime directory is invalid: {path}")
    path.chmod(mode)
    _verify_posix_owner_mode(path, mode)


def _write_exclusive(path: Path, content: bytes, mode: int) -> None:
    with path.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    path.chmod(mode)
    _verify_posix_owner_mode(path, mode)


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def verify_trusted_source(
    source_root: Path,
    *,
    expected_git_commit: str,
    expected_git_tree: str,
) -> tuple[str, str, dict[str, bytes]]:
    source_root = source_root.resolve()
    if not source_root.is_dir():
        raise InstallError(f"trusted source root is missing: {source_root}")
    expected_git_commit = expected_git_commit.lower()
    expected_git_tree = expected_git_tree.lower()
    if not FULL_SHA.fullmatch(expected_git_commit):
        raise InstallError("expected Git commit must be a full lowercase SHA")
    if not FULL_SHA.fullmatch(expected_git_tree) or expected_git_tree == expected_git_commit:
        raise InstallError("expected Git tree must be a distinct full lowercase SHA")

    actual_commit = _run_git(source_root, "rev-parse", "HEAD").lower()
    actual_tree = _run_git(source_root, "rev-parse", "HEAD^{tree}").lower()
    if actual_commit != expected_git_commit:
        raise InstallError("trusted source HEAD differs from the approved Git commit")
    if actual_tree != expected_git_tree:
        raise InstallError("trusted source tree differs from the approved Git tree")
    if _run_git(source_root, "status", "--porcelain"):
        raise InstallError("trusted source worktree is not clean")
    symbolic_ref = subprocess.run(
        ["git", "-C", str(source_root), "symbolic-ref", "-q", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if symbolic_ref.returncode == 0:
        raise InstallError("trusted source must remain at detached HEAD")
    if symbolic_ref.returncode not in {1}:
        raise InstallError("trusted source detached-HEAD check failed")

    contents: dict[str, bytes] = {}
    for name, relative_path in WRAPPERS.items():
        _run_git(source_root, "ls-files", "--error-unmatch", relative_path.as_posix())
        path = source_root / relative_path
        if path.is_symlink() or not path.is_file():
            raise InstallError(f"trusted {name} wrapper is missing or is a symlink")
        if not path.resolve().is_relative_to(source_root):
            raise InstallError(f"trusted {name} wrapper escapes the source root")
        content = path.read_bytes()
        if not content.startswith(b"#!/usr/bin/env bash\n"):
            raise InstallError(f"trusted {name} wrapper has an invalid shebang")
        contents[name] = content
    return actual_commit, actual_tree, contents


def _validate_release(
    release_dir: Path,
    *,
    git_commit: str,
    git_tree: str,
    wrapper_shas: dict[str, str],
) -> dict[str, Any]:
    if release_dir.is_symlink() or not release_dir.is_dir():
        raise InstallError("existing runtime release directory is invalid")
    _verify_posix_owner_mode(release_dir, 0o700)
    manifest_path = release_dir / "runtime_manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise InstallError("existing runtime release manifest is invalid")
    _verify_posix_owner_mode(manifest_path, 0o600)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallError("existing runtime release manifest is invalid") from exc
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise InstallError("existing runtime release schema is invalid")
    if manifest.get("wrapper_release_git_commit") != git_commit:
        raise InstallError("existing runtime release Git commit differs")
    if manifest.get("wrapper_release_git_tree") != git_tree:
        raise InstallError("existing runtime release Git tree differs")
    if manifest.get("installed_by") != _current_user():
        raise InstallError("existing runtime release owner identity differs")
    if manifest.get("release_dir") != str(release_dir):
        raise InstallError("existing runtime release path differs")
    for name, relative_path in WRAPPERS.items():
        installed = release_dir / relative_path.name
        record = manifest.get("wrappers", {}).get(name, {})
        if not installed.is_file() or installed.is_symlink():
            raise InstallError(f"existing {name} runtime wrapper is invalid")
        if _sha256_file(installed) != wrapper_shas[name]:
            raise InstallError(f"existing {name} runtime wrapper SHA-256 differs")
        if record.get("sha256") != wrapper_shas[name]:
            raise InstallError(f"existing {name} runtime manifest SHA-256 differs")
        if record.get("source_git_path") != relative_path.as_posix():
            raise InstallError(f"existing {name} runtime source path differs")
        if record.get("release_host_path") != str(installed):
            raise InstallError(f"existing {name} runtime release path differs")
        if record.get("current_host_path") != str(
            release_dir.parents[1] / "current" / relative_path.name
        ):
            raise InstallError(f"existing {name} runtime current path differs")
        if record.get("mode") != "0700":
            raise InstallError(f"existing {name} runtime manifest mode differs")
        _verify_posix_owner_mode(installed, 0o700)
    return manifest


def stage_runtime_release(
    *,
    source_root: Path,
    runtime_root: Path,
    expected_git_commit: str,
    expected_git_tree: str,
    expected_install_user: str,
    installed_at_utc: str | None = None,
) -> tuple[Path, dict[str, Any]]:
    current_user = _current_user()
    if current_user != expected_install_user:
        raise InstallError(
            f"runtime installer must run as {expected_install_user}, not {current_user}"
        )
    git_commit, git_tree, contents = verify_trusted_source(
        source_root,
        expected_git_commit=expected_git_commit,
        expected_git_tree=expected_git_tree,
    )
    wrapper_shas = {name: _sha256_bytes(content) for name, content in contents.items()}

    runtime_root = runtime_root.resolve()
    releases_root = runtime_root / "releases"
    logs_root = runtime_root / "logs"
    _ensure_directory(runtime_root, 0o700)
    _ensure_directory(releases_root, 0o700)
    _ensure_directory(logs_root, 0o700)
    release_dir = releases_root / git_commit
    if release_dir.is_symlink():
        raise InstallError("runtime release path must not be a symlink")
    if release_dir.exists():
        return release_dir, _validate_release(
            release_dir,
            git_commit=git_commit,
            git_tree=git_tree,
            wrapper_shas=wrapper_shas,
        )

    staging_dir = releases_root / f".staging-{git_commit}-{uuid.uuid4().hex}"
    _ensure_directory(staging_dir, 0o700)
    try:
        installed_paths: dict[str, Path] = {}
        for name, relative_path in WRAPPERS.items():
            installed = staging_dir / relative_path.name
            _write_exclusive(installed, contents[name], 0o700)
            if _sha256_file(installed) != wrapper_shas[name]:
                raise InstallError(f"staged {name} runtime wrapper SHA-256 differs")
            installed_paths[name] = installed

        manifest: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "wrapper_release_git_commit": git_commit,
            "wrapper_release_git_tree": git_tree,
            "installed_at_utc": installed_at_utc or _utc_now(),
            "installed_by": current_user,
            "runtime_root": str(runtime_root),
            "release_dir": str(release_dir),
            "wrappers": {
                name: {
                    "source_git_path": WRAPPERS[name].as_posix(),
                    "release_host_path": str(release_dir / WRAPPERS[name].name),
                    "current_host_path": str(runtime_root / "current" / WRAPPERS[name].name),
                    "sha256": wrapper_shas[name],
                    "mode": "0700",
                }
                for name in WRAPPERS
            },
        }
        manifest_bytes = (
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        _write_exclusive(staging_dir / "runtime_manifest.json", manifest_bytes, 0o600)
        _fsync_directory(staging_dir)
        os.replace(staging_dir, release_dir)
        _fsync_directory(releases_root)
        return release_dir, _validate_release(
            release_dir,
            git_commit=git_commit,
            git_tree=git_tree,
            wrapper_shas=wrapper_shas,
        )
    except Exception:
        if staging_dir.exists() and staging_dir.parent == releases_root:
            shutil.rmtree(staging_dir)
        raise


def atomic_switch_current(runtime_root: Path, release_dir: Path) -> None:
    runtime_root = runtime_root.resolve()
    release_dir = release_dir.resolve()
    releases_root = runtime_root / "releases"
    if release_dir.parent != releases_root or not release_dir.is_dir():
        raise InstallError("runtime release is outside the controlled releases directory")
    current = runtime_root / "current"
    if current.exists() and not current.is_symlink():
        raise InstallError("runtime current path exists and is not a symlink")
    temporary = runtime_root / f".current-{uuid.uuid4().hex}"
    relative_target = Path("releases") / release_dir.name
    try:
        os.symlink(relative_target, temporary, target_is_directory=True)
        os.replace(temporary, current)
        _fsync_directory(runtime_root)
    finally:
        if temporary.is_symlink():
            temporary.unlink()
    if not current.is_symlink() or current.resolve() != release_dir:
        raise InstallError("runtime current symlink verification failed")
    if os.name == "posix" and current.lstat().st_uid != os.geteuid():
        raise InstallError("runtime current symlink is not owned by the installer")


def install_runtime(
    *,
    source_root: Path,
    runtime_root: Path,
    expected_git_commit: str,
    expected_git_tree: str,
    expected_install_user: str,
    installed_at_utc: str | None = None,
    switcher: Callable[[Path, Path], None] = atomic_switch_current,
) -> dict[str, Any]:
    release_dir, manifest = stage_runtime_release(
        source_root=source_root,
        runtime_root=runtime_root,
        expected_git_commit=expected_git_commit,
        expected_git_tree=expected_git_tree,
        expected_install_user=expected_install_user,
        installed_at_utc=installed_at_utc,
    )
    switcher(runtime_root.resolve(), release_dir)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install trusted soybean-export host wrappers into the fixed runtime"
    )
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--expected-git-commit", required=True)
    parser.add_argument("--expected-git-tree", required=True)
    parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    args = parser.parse_args()
    try:
        manifest = install_runtime(
            source_root=args.source_root,
            runtime_root=args.runtime_root,
            expected_git_commit=args.expected_git_commit,
            expected_git_tree=args.expected_git_tree,
            expected_install_user="ubuntu",
        )
    except InstallError as exc:
        parser.exit(1, f"soybean-export runtime install failed: {exc}\n")
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
