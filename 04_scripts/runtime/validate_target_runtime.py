"""Build and validate one clean production-container candidate on Linux.

This program is the sole evidence producer consumed by target_runtime_gate.py.
It accepts source identity only; image identity, Compose identity and probe
results are observations made here.  It never accepts caller-supplied evidence
or production host paths.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from typing import Any, Iterator, Mapping, Sequence


EVIDENCE_SCHEMA = "target-runtime-evidence/1"
BLOCKED_REASON = "LINUX_BUILDER_UNAVAILABLE"
REQUIRED_PROBES = frozenset({
    "entrypoint_initialization", "runtime_identity", "dependencies", "runtime_paths",
    "mount_permissions", "missing_grant_rejected", "wrong_commit_rejected",
    "wrong_tree_rejected", "wrong_image_rejected", "wrong_service_rejected",
    "wrong_manifest_rejected", "preview_write_rejected", "release_mismatch_rejected",
})
_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
_IMAGE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ID = re.compile(r"[a-z][a-z0-9-]*\Z")
_SOURCE_ROOT = "/app"
_GRANT_ROOT = "/run/market-data-grants"
_PROTECTED_WORK_ROOT = Path("/var/lib/market-data/runtime-validation")
_KEY_ROOT = Path("/etc/market-data/runtime-identity")
_ENGINE_PATH = "04_scripts/runtime/validate_target_runtime.py"
_MANIFEST_PARSER = "03_src/agri_research_agent/shared/runtime_manifest.py"
_MANIFEST_SCHEMA = "02_configs/runtime_manifest.schema.json"
_VALIDATOR_VERSION = "target-runtime-validator/1"


class ValidationError(RuntimeError):
    """A target is invalid; this is a FAIL, never BLOCKED."""


class BuilderUnavailable(RuntimeError):
    """The required local isolated Linux builder does not exist."""


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _strict_json(raw: bytes, label: str) -> dict[str, Any]:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValidationError(f"duplicate {label} JSON field")
            result[key] = value
        return result
    def constant(value):
        raise ValidationError(f"non-finite {label} JSON value: {value}")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                           parse_constant=constant)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValidationError(f"invalid {label} JSON") from exc
    if type(value) is not dict:
        raise ValidationError(f"{label} JSON must be an object")
    return value


def _load(path: Path, name: str):
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ValidationError(f"cannot load trusted source: {path}")
        module = importlib.util.module_from_spec(spec)
        previous = sys.modules.get(name)
        sys.modules[name] = module
        try:
            exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
        finally:
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous
        return module
    except (OSError, ImportError, TypeError, ValueError) as exc:
        raise ValidationError(f"cannot load trusted source: {path}") from exc


def repository_root() -> Path:
    root = Path(__file__).resolve().parents[2]
    if not (root / ".git").exists():
        # Git worktrees normally expose .git as a file.
        raise ValidationError("validator must execute from a real Git worktree")
    return root


def _run(args: Sequence[str], *, cwd: Path | None = None,
         input_bytes: bytes | None = None, timeout: int = 600,
         check: bool = True) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(list(args), cwd=cwd, input=input_bytes,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValidationError(f"command unavailable or timed out: {args[0]}") from exc
    if check and result.returncode:
        detail = result.stderr.decode("utf-8", "replace")[-1200:]
        raise ValidationError(f"command failed ({args[0]}): {detail}")
    return result


def _docker(*args: str, input_bytes: bytes | None = None,
            check: bool = True, timeout: int = 600) -> subprocess.CompletedProcess[bytes]:
    return _run(("docker", *args), input_bytes=input_bytes, check=check, timeout=timeout)


def _git(root: Path, *args: str, binary: bool = False):
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    try:
        result = subprocess.run(("git", "--no-replace-objects", "-C", str(root), *args),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=120, check=False, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValidationError("Git command unavailable or timed out") from exc
    if result.returncode:
        raise ValidationError("Git command failed: " + result.stderr.decode("utf-8", "replace")[-800:])
    return result.stdout if binary else result.stdout.decode("utf-8", "strict").strip()


def _project(root: Path, project_id: str) -> dict[str, Any]:
    registry = _strict_json((root / "02_configs/project_registry.json").read_bytes(), "Registry")
    matches = [item for item in registry.get("projects", [])
               if isinstance(item, dict) and item.get("project_id") == project_id]
    if len(matches) != 1 or matches[0].get("runtime_target") != "production_container":
        raise ValidationError("project is not one exact production_container target")
    return matches[0]


def _exact_source(root: Path, relative: str) -> Path:
    pure = PurePosixPath(relative) if isinstance(relative, str) else PurePosixPath("/")
    if (not isinstance(relative, str) or not relative or pure.is_absolute()
            or str(pure) != relative or ".." in pure.parts or "\\" in relative):
        raise ValidationError("runtime input path is unsafe")
    path = root.joinpath(*pure.parts)
    if path.is_symlink() or not path.is_file():
        raise ValidationError(f"runtime input is missing or aliased: {relative}")
    try:
        if path.resolve(strict=True).relative_to(root.resolve(strict=True)).as_posix() != relative:
            raise ValidationError(f"runtime input path identity differs: {relative}")
    except ValueError as exc:
        raise ValidationError("runtime input escaped repository") from exc
    return path


def _candidate_binding(root: Path, project: Mapping[str, Any],
                       contract: Mapping[str, Any]) -> dict[str, Any]:
    if _git(root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ValidationError("container validation requires a clean committed candidate")
    build = contract["build"]
    paths = [project["runtime_contract"], build["dockerfile"], build["dockerignore"],
             *build["dependency_contracts"], *build["compose_sources"], _ENGINE_PATH,
             *(item["path"] for item in contract["source_inputs"]),
             _MANIFEST_PARSER, _MANIFEST_SCHEMA]
    if len({name.casefold() for name in paths}) != len(paths):
        raise ValidationError("runtime binding paths overlap")
    hashes = {}
    for name in paths:
        path = _exact_source(root, name)
        _git(root, "ls-files", "--error-unmatch", "--", name)
        raw = _git(root, "show", "HEAD:" + name, binary=True)
        if path.read_bytes() != raw:
            disk_blob = _git(root, "hash-object", "--path", name, str(path))
            git_blob = _git(root, "rev-parse", "HEAD:" + name)
            if disk_blob != git_blob:
                raise ValidationError(f"runtime input differs from Git object: {name}")
        hashes[name] = _sha(raw)
    return {"project_id": project["project_id"], "commit": _git(root, "rev-parse", "HEAD"),
            "tree": _git(root, "rev-parse", "HEAD^{tree}"), "source_sha256": hashes,
            "validator_version": _VALIDATOR_VERSION}


def source_contract(root: Path, project_id: str, runtime_contract: str):
    if any(key.startswith("GIT_") for key in os.environ):
        raise ValidationError("caller Git environment is forbidden")
    project = _project(root, project_id)
    if project.get("runtime_contract") != runtime_contract:
        raise ValidationError("runtime contract differs from Registry")
    parser = _load(root / _MANIFEST_PARSER, "_runtime_manifest_source_contract")
    try:
        contract = parser.load_runtime_manifest(_exact_source(root, runtime_contract)).to_dict()
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValidationError("runtime manifest violates v2 source contract") from exc
    if (contract.get("schema_version") != "runtime-manifest/2"
            or contract.get("project_id") != project_id):
        raise ValidationError("actual container validation requires runtime-manifest/2")
    binding = _candidate_binding(root, project, contract)
    if set(contract["validation_probes"]) != REQUIRED_PROBES:
        raise ValidationError("runtime manifest probe set differs from engine")
    if contract["secret_references"]:
        raise ValidationError("runtime-manifest/2 has no candidate-safe secret source contract")
    return project, contract, binding


def _write_new(path: Path, raw: bytes, mode: int = 0o600) -> None:
    if not path.is_absolute() or path.exists() or path.is_symlink():
        raise ValidationError("output must be a new absolute file")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as exc:
        raise ValidationError("refusing to overwrite output") from exc


def write_evidence(path: Path, evidence: Mapping[str, Any]) -> None:
    _write_new(path, _canonical(evidence) + b"\n", 0o600)


def blocked_evidence(binding: Mapping[str, Any]) -> dict[str, Any]:
    return {"schema_version": EVIDENCE_SCHEMA, "binding": binding,
            "TARGET_RUNTIME_STATIC_VALIDATION": "PASS",
            "TARGET_RUNTIME_CONTAINER_VALIDATION": "BLOCKED",
            "blocked_reason": BLOCKED_REASON}


def require_builder() -> str:
    if (sys.platform != "linux" or os.name != "posix" or not hasattr(os, "geteuid")
            or os.geteuid() != 0):
        raise BuilderUnavailable(BLOCKED_REASON)
    if any(os.environ.get(name) for name in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG",
                                             "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH")):
        raise BuilderUnavailable(BLOCKED_REASON)
    for command in (("docker", "version", "--format", "{{.Server.Os}}"),
                    ("docker", "compose", "version", "--short")):
        try:
            result = _run(command, timeout=30)
        except ValidationError as exc:
            raise BuilderUnavailable(BLOCKED_REASON) from exc
        if command[1] == "version" and result.stdout.decode().strip() != "linux":
            raise BuilderUnavailable(BLOCKED_REASON)
    try:
        if _run(("docker", "context", "show"), timeout=30).stdout.decode().strip() != "default":
            raise BuilderUnavailable(BLOCKED_REASON)
        endpoint = _run(("docker", "context", "inspect", "default", "--format",
                         "{{json .Endpoints.docker.Host}}"), timeout=30).stdout.decode().strip()
        if endpoint not in {'"unix:///var/run/docker.sock"', '"unix:///run/docker.sock"'}:
            raise BuilderUnavailable(BLOCKED_REASON)
        socket_path = Path(json.loads(endpoint).removeprefix("unix://"))
        socket_state = socket_path.stat()
        if not stat.S_ISSOCK(socket_state.st_mode) or socket_state.st_uid != 0 or socket_state.st_mode & 0o002:
            raise BuilderUnavailable(BLOCKED_REASON)
        server = _strict_json(_run(("docker", "info", "--format", "{{json .}}"),
                                  timeout=30).stdout, "Docker server")
        server_id = server.get("ID")
        if not isinstance(server_id, str) or not server_id.strip():
            raise BuilderUnavailable(BLOCKED_REASON)
        machine = Path("/etc/machine-id").read_bytes().strip()
    except OSError as exc:
        raise BuilderUnavailable(BLOCKED_REASON) from exc
    if not machine:
        raise BuilderUnavailable(BLOCKED_REASON)
    return "linux-" + _sha(machine + b"\0" + server_id.encode("utf-8"))[:16]


def create_archive_context(root: Path, destination: Path,
                           binding: Mapping[str, Any] | None = None) -> None:
    destination.mkdir(mode=0o700)
    revision = "HEAD" if binding is None else binding["commit"]
    if binding is not None:
        if (_git(root, "rev-parse", revision) != binding["commit"]
                or _git(root, "rev-parse", revision + "^{tree}") != binding["tree"]):
            raise ValidationError("bound Git object no longer resolves to candidate")
    archive = _git(root, "archive", "--format=tar", revision, binary=True)
    archive_path = destination.parent / "source.tar"
    archive_path.write_bytes(archive)
    with tarfile.open(archive_path, "r:") as bundle:
        for member in bundle.getmembers():
            pure = PurePosixPath(member.name)
            if pure.is_absolute() or ".." in pure.parts or member.issym() or member.islnk():
                raise ValidationError("Git archive contains an unsafe path")
        # Python 3.10 has no tarfile extraction filter. Extract regular files
        # and directories explicitly after the path/link checks above.
        for member in bundle.getmembers():
            target = destination.joinpath(*PurePosixPath(member.name).parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                source = bundle.extractfile(member)
                if source is None:
                    raise ValidationError("Git archive file has no payload")
                with source, target.open("xb") as stream:
                    shutil.copyfileobj(source, stream)
                os.chmod(target, member.mode & 0o777)
            else:
                raise ValidationError("Git archive contains a special file")
    archive_path.unlink()
    if any(path.name == ".git" for path in destination.rglob(".git")):
        raise ValidationError("Git metadata entered the build context")
    if binding is not None and (_git(root, "rev-parse", revision) != binding["commit"]
                                or _git(root, "rev-parse", revision + "^{tree}") != binding["tree"]):
        raise ValidationError("bound Git object changed during archive")


def _strict_json_value(raw: bytes, label: str) -> Any:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValidationError(f"duplicate {label} JSON field")
            result[key] = value
        return result
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                          parse_constant=lambda value: (_ for _ in ()).throw(
                              ValidationError(f"non-finite {label} JSON value")))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValidationError(f"invalid {label} JSON") from exc


def inspect_one(kind: str, identity: str) -> dict[str, Any]:
    value = _strict_json_value(_docker(kind, "inspect", identity).stdout,
                               f"{kind} inspect")
    if not isinstance(value, list) or len(value) != 1 or type(value[0]) is not dict:
        raise ValidationError(f"invalid {kind} inspect result")
    return value[0]


def _numeric_user(image: Mapping[str, Any]) -> tuple[int, int]:
    value = (image.get("Config") or {}).get("User")
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]*(?::[0-9]+)?", value):
        raise ValidationError("candidate image must declare a numeric non-root user")
    parts = value.split(":", 1)
    return int(parts[0]), int(parts[1] if len(parts) == 2 else parts[0])


def _labels(image: Mapping[str, Any], binding: Mapping[str, Any], service: str) -> None:
    labels = (image.get("Config") or {}).get("Labels") or {}
    expected = {"org.opencontainers.image.revision": binding["commit"],
                "market-data.git.tree": binding["tree"],
                "market-data.service": service,
                "market-data.artifact.origin": "candidate"}
    if any(labels.get(key) != value for key, value in expected.items()):
        raise ValidationError("image OCI/source/service labels differ from candidate")
    if labels.get("market-data.artifact.promotable") != "true" or not labels.get("market-data.release.id"):
        raise ValidationError("image promotion/release labels are incomplete")


def build_image(root: Path, context: Path, contract: Mapping[str, Any],
                binding: Mapping[str, Any]) -> str:
    tag = f"market-data-runtime-validation:{binding['commit'][:12]}-{os.getpid()}"
    build_time = datetime.now(timezone.utc).isoformat()
    release_id = (f"{contract['service_id']}-{datetime.now(timezone.utc).strftime('%Y%m%d')}-"
                  f"{binding['commit'][:12]}-b01")
    args = ["build", "--no-cache", "--iidfile", str(context.parent / "image.id"),
            "-f", str(context / contract["build"]["dockerfile"]), "-t", tag,
            "--label", f"org.opencontainers.image.revision={binding['commit']}",
            "--label", f"market-data.git.tree={binding['tree']}",
            "--label", f"market-data.service={contract['service_id']}",
            "--label", "market-data.artifact.origin=candidate",
            "--label", "market-data.artifact.promotable=true",
            "--label", f"market-data.release.id={release_id}",
            "--label", "org.opencontainers.image.source=target-runtime-validator/1",
            "--label", f"org.opencontainers.image.created={build_time}"]
    build_args = {
        "MARKET_DATA_GIT_HEAD": binding["commit"], "MARKET_DATA_GIT_TREE": binding["tree"],
        "MARKET_DATA_RELEASE_ID": release_id,
        "MARKET_DATA_BUILD_TIME": build_time,
        "MARKET_DATA_SOURCE": "target-runtime-validator/1",
        "MARKET_DATA_SERVICE": contract["service_id"],
        "MARKET_DATA_ARTIFACT_ORIGIN": "candidate",
        "MARKET_DATA_ARTIFACT_PROMOTABLE": "true",
        "MARKET_DATA_DEPLOYMENT_ROLE": "candidate",
    }
    for key, value in build_args.items():
        args.extend(("--build-arg", f"{key}={value}"))
    args.append(str(context))
    _docker(*args, timeout=1800)
    image_id = (context.parent / "image.id").read_text(encoding="ascii").strip()
    if not _IMAGE.fullmatch(image_id):
        raise ValidationError("build did not produce an immutable image ID")
    image = inspect_one("image", image_id)
    if image.get("Id") != image_id:
        raise ValidationError("image inspect identity mismatch")
    _labels(image, binding, contract["service_id"])
    _numeric_user(image)
    return image_id


def _copy_bytes(container_id: str, path: str) -> bytes:
    result = _docker("cp", f"{container_id}:{path}", "-", check=False)
    if result.returncode:
        raise ValidationError(f"container file missing: {path}")
    try:
        with tarfile.open(fileobj=__import__("io").BytesIO(result.stdout), mode="r:*") as archive:
            files = [item for item in archive.getmembers() if item.isfile()]
            if len(files) != 1:
                raise ValidationError("container copy was not one regular file")
            stream = archive.extractfile(files[0])
            if stream is None:
                raise ValidationError("container copy has no payload")
            return stream.read()
    except tarfile.TarError as exc:
        raise ValidationError("invalid Docker copy payload") from exc


def _runtime_bindings(contract: Mapping[str, Any], uid: int, gid: int) -> list[dict[str, Any]]:
    result = []
    identity_role = contract["identity_root_role"]
    roots = {item["role"]: item for item in contract["runtime_roots"]}
    identity = roots[identity_role]["container_path"]
    for item in contract["required_mounts"]:
        target = item["container_path"]
        relative = "identity" if target == identity else "identity/" + target[len(identity.rstrip("/")) + 1:]
        if relative.startswith("/") or relative == "":
            raise ValidationError("runtime mount is outside identity root")
        result.append({"relative_path": relative, "target": target,
                       "read_only": item["read_only"],
                       "owner_uid": 0 if item["read_only"] else uid,
                       "owner_gid": 0 if item["read_only"] else gid})
    return sorted(result, key=lambda item: (item["relative_path"].count("/"), item["relative_path"]))


def _compose_document(contract: Mapping[str, Any], image_id: str,
                      mounts: Sequence[Mapping[str, Any]], grant_dir: Path,
                      hostname: str) -> dict[str, Any]:
    volumes = [{"type": "bind", "source": item["source"], "target": item["target"],
                "read_only": item["read_only"]} for item in mounts]
    volumes.append({"type": "bind", "source": str(grant_dir),
                    "target": _GRANT_ROOT, "read_only": True})
    environment = {name: f"candidate-validation-{name.lower()}" for name in contract["required_environment"]}
    environment.update({"MARKET_DATA_EXECUTION_GRANT": _GRANT_ROOT + "/grant.json"})
    return {"name": "market-data-runtime-validation",
            "services": {contract["service_id"]: {
                "image": image_id, "entrypoint": contract["entrypoint"],
                "working_dir": contract["working_directory"], "hostname": hostname,
                "read_only": True, "user": contract["_container_user"],
                "network_mode": "none", "cap_drop": ["ALL"],
                "security_opt": ["no-new-privileges:true"],
                "environment": environment, "volumes": volumes,
            "restart": "no"}}}


def validate_source_compose(root: Path, contract: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the checked-in deployment shape without resolving host values."""
    args = ["compose", "--project-directory", str(root)]
    for relative in contract["build"]["compose_sources"]:
        args.extend(("-f", str(root / relative)))
    args.extend(("config", "--no-interpolate", "--format", "json"))
    rendered = _strict_json(_docker(*args).stdout, "source Compose")
    service = rendered.get("services", {}).get(contract["service_id"])
    if not isinstance(service, dict):
        raise ValidationError("source Compose omits the runtime service")
    if service.get("entrypoint") != contract["entrypoint"]:
        raise ValidationError("source Compose entrypoint differs from runtime manifest")
    if service.get("working_dir") != contract["working_directory"]:
        raise ValidationError("source Compose working directory differs from runtime manifest")
    if not isinstance(service.get("image"), str) or not service["image"]:
        raise ValidationError("source Compose image contract is missing")
    build = service.get("build")
    if build is not None:
        if not isinstance(build, dict) or build.get("dockerfile") != contract["build"]["dockerfile"]:
            raise ValidationError("source Compose Dockerfile differs from runtime manifest")
    environment = service.get("environment") or {}
    if not isinstance(environment, dict) or not set(contract["required_environment"]).issubset(environment):
        raise ValidationError("source Compose required environment contract is incomplete")
    secrets = service.get("secrets") or []
    secret_names = {item if isinstance(item, str) else item.get("source")
                    for item in secrets if isinstance(item, (str, dict))}
    if not set(contract["secret_references"]).issubset(secret_names):
        raise ValidationError("source Compose secret references are incomplete")
    volumes = service.get("volumes") or []
    if any(not isinstance(item, dict) or item.get("type") not in {"bind", "volume"}
           for item in volumes):
        raise ValidationError("source Compose mounts are not explicit contracts")
    actual_mounts = {item.get("target"): bool(item.get("read_only", False)) for item in volumes}
    expected_mounts = {item["container_path"]: item["read_only"]
                       for item in contract["required_mounts"]}
    if actual_mounts != expected_mounts:
        raise ValidationError("source Compose mounts differ from runtime manifest")
    if any("docker.sock" in str(item.get("source", "")) for item in volumes):
        raise ValidationError("source Compose exposes a Docker control socket")
    return rendered


def _render_compose(work: Path, compose: Path, env_file: Path) -> tuple[dict[str, Any], str]:
    raw = _docker("compose", "--project-directory", str(work), "--env-file", str(env_file),
                  "-f", str(compose), "config", "--format", "json").stdout
    rendered = _strict_json(raw, "rendered Compose")
    return rendered, _sha(_canonical(rendered))


def _release_identity(raw: bytes, binding: Mapping[str, Any],
                      expected_application: str | None = None,
                      expected_release_id: str | None = None,
                      image_labels: Mapping[str, Any] | None = None) -> dict[str, Any]:
    release = _strict_json(raw, "RELEASE")
    required = {"application", "release_id", "git_commit", "git_tree", "build_time", "source"}
    if set(release) != required:
        raise ValidationError("embedded RELEASE fields are incomplete or unknown")
    if release.get("git_commit") != binding["commit"] or release.get("git_tree") != binding["tree"]:
        raise ValidationError("embedded RELEASE differs from candidate")
    if expected_application is not None and release.get("application") != expected_application:
        raise ValidationError("embedded RELEASE application differs from runtime project")
    if expected_release_id is not None and release.get("release_id") != expected_release_id:
        raise ValidationError("embedded RELEASE ID differs from OCI image")
    if image_labels is not None:
        if (release.get("source") != image_labels.get("org.opencontainers.image.source")
                or release.get("build_time") != image_labels.get("org.opencontainers.image.created")
                or release.get("source") != "target-runtime-validator/1"):
            raise ValidationError("embedded RELEASE build origin differs from OCI image")
        try:
            created = datetime.fromisoformat(release["build_time"].replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise ValidationError("embedded RELEASE build time is invalid") from exc
        if created.tzinfo is None or created.utcoffset() is None:
            raise ValidationError("embedded RELEASE build time is not timezone-aware")
    return release


def _manifest_identity(raw: bytes, contract: Mapping[str, Any], parser) -> dict[str, Any]:
    try:
        parsed = parser.parse_runtime_manifest(_strict_json(raw, "runtime manifest")).to_dict()
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValidationError("runtime manifest violates v2 source contract") from exc
    expected = {key: value for key, value in contract.items() if not key.startswith("_")}
    if parsed != expected:
        raise ValidationError("image runtime manifest differs from bound source contract")
    return parsed


def _policy(contract: Mapping[str, Any], binding: Mapping[str, Any], image_id: str,
            image: Mapping[str, Any], container: Mapping[str, Any], scope: Mapping[str, Any],
            compose: Path, env_file: Path, rendered_hash: str,
            manifest_hash: str, marker_hash: str, release_hash: str,
            host_module) -> dict[str, Any]:
    observation = host_module.normalize_observation(
        container, image, _strict_json(_copy_bytes(container["Id"], _SOURCE_ROOT + "/RELEASE.json"), "RELEASE"))
    trust = _strict_json((repository_root() / "02_configs/production_runtime_trust.json").read_bytes(), "trust")
    keys = [item for item in trust.get("keys", []) if isinstance(item, dict) and item.get("domain") == "candidate_validation"]
    if len(keys) != 1:
        raise ValidationError("candidate validation trust key is not unique")
    return {
        "schema_version": "host-runtime-policy/2", "role": "candidate_validation",
        "key_id": keys[0]["key_id"], "project_id": contract["project_id"],
        "module_id": contract["module_id"], "service_id": contract["service_id"],
        "runtime_id": "target-validation", "approved_commit": binding["commit"],
        "approved_tree": binding["tree"], "image_id": image_id,
        "artifact_service": contract["service_id"],
        "release_application": _strict_json(_copy_bytes(container["Id"], _SOURCE_ROOT + "/RELEASE.json"), "RELEASE")["application"],
        "source_root": _SOURCE_ROOT, "runtime_root": next(item["container_path"] for item in contract["runtime_roots"] if item["role"] == contract["identity_root_role"]),
        "runtime_manifest_path": _SOURCE_ROOT + "/" + contract["_runtime_contract"],
        "runtime_manifest_sha256": manifest_hash, "runtime_marker_sha256": marker_hash,
        "release_sha256": release_hash, "actual_config_sha256": observation["actual_config_sha256"],
        "mounts": sorted([*scope["mounts"], {"source": str(contract["_grant_dir"]), "target": _GRANT_ROOT, "read_only": True}], key=lambda item: item["target"]),
        "compose_sources": [{"path": str(compose), "sha256": _sha(compose.read_bytes())}],
        "compose_project_directory": str(compose.parent), "compose_environment_file": str(env_file),
        "rendered_compose_sha256": rendered_hash, "grant_container_directory": _GRANT_ROOT,
        "candidate_host_root": scope["candidate_host_root"], "candidate_scope": scope["candidate_scope"],
    }


def _exec(container_id: str, argv: Sequence[str], *, cwd: str | None = None,
          expect_success: bool = True, label: str = "unnamed") -> None:
    args = ["exec"]
    if cwd:
        args.extend(("--workdir", cwd))
    args.extend((container_id, *argv))
    result = _docker(*args, check=False, timeout=300)
    if (result.returncode == 0) != expect_success:
        detail = result.stderr.decode("utf-8", "replace")[-800:].strip()
        raise ValidationError(f"container probe {label} returned an unexpected result: {detail}")


def _identity_probe_argv(contract: Mapping[str, Any], marker_hash: str, *,
                         missing: bool = False, role: str = "candidate_validation") -> list[str]:
    grant = _GRANT_ROOT + ("/missing.json" if missing else "/grant.json")
    root = next(item["container_path"] for item in contract["runtime_roots"] if item["role"] == contract["identity_root_role"])
    code = (
        "from pathlib import Path; from agri_research_agent.shared.production_identity import OCIExecutionRequest,verify_execution; "
        f"r=Path({root!r}); verify_execution(OCIExecutionRequest(Path({grant!r}),Path('/app/RELEASE.json'),"
        f"Path({('/app/' + contract['_runtime_contract'])!r}),r,r/'.market-data-runtime.json'),"
        f"expected_role={role!r},module_id={contract['module_id']!r},runtime_id='target-validation',"
        f"runtime_root=r,marker_sha256={marker_hash!r})")
    return ["python", "-B", "-c", code]


def _expect_rejected(action, label: str) -> None:
    try:
        action()
    except Exception:
        return
    raise ValidationError(f"negative identity probe was accepted: {label}")


def _negative_observation_probes(host, observed: Mapping[str, Any],
                                 policy: Mapping[str, Any]) -> dict[str, str]:
    import copy
    probes: dict[str, str] = {}
    mutations = {
        "wrong_commit_rejected": ("approved_commit", "0" * 40),
        "wrong_tree_rejected": ("approved_tree", "1" * 40),
        "wrong_image_rejected": ("image_id", "sha256:" + "2" * 64),
        "wrong_service_rejected": ("service_id", "wrong-service"),
    }
    for probe, (field, value) in mutations.items():
        candidate = copy.deepcopy(policy)
        candidate[field] = value
        if field == "service_id":
            candidate["artifact_service"] = value
        _expect_rejected(lambda c=candidate: host.validate_observation(
            observed, c, role="candidate_validation"), probe)
        probes[probe] = "PASS"
    return probes


def _actual_host_rejection(root: Path, host, contract: dict[str, Any],
                           binding: Mapping[str, Any], image_id: str,
                           image: Mapping[str, Any], work: Path,
                           mutation: str) -> None:
    """Require the real host grant issuer to reject altered identity material."""
    if mutation not in {"manifest", "release"}:
        raise ValidationError("unknown host rejection probe")
    probe_work = work / ("negative-" + mutation)
    probe_work.mkdir(mode=0o700)
    grant_dir = probe_work / "grants"
    grant_dir.mkdir(mode=0o755)
    compose = probe_work / "compose.json"
    env_file = probe_work / "compose.env"
    project_name = "market-data-runtime-negative-" + mutation
    scope = None
    container_id = None
    try:
        uid, gid = _numeric_user(image)
        contract["_numeric_uid"] = uid
        contract["_container_user"] = image["Config"]["User"]
        contract["_grant_dir"] = grant_dir
        scope = host.create_candidate_scope(_runtime_bindings(contract, uid, gid))
        identity_root = next(item["container_path"] for item in contract["runtime_roots"]
                             if item["role"] == contract["identity_root_role"])
        identity_source = next(Path(item["source"]) for item in scope["mounts"]
                               if item["target"] == identity_root)
        marker = {"schema_version": 1, "runtime_id": "target-validation",
                  "module_id": contract["module_id"], "classification": "candidate-validation",
                  "created_at": datetime.now(timezone.utc).isoformat()}
        marker_path = identity_source / ".market-data-runtime.json"
        marker_path.write_bytes(_canonical(marker) + b"\n")
        os.chmod(marker_path, 0o444)
        compose.write_bytes(_canonical(_compose_document(
            contract, image_id, scope["mounts"], grant_dir, os.urandom(16).hex())) + b"\n")
        os.chmod(compose, 0o600)
        env_file.write_text("", encoding="utf-8")
        os.chmod(env_file, 0o600)
        _, rendered_hash = _render_compose(probe_work, compose, env_file)
        _docker("compose", "--project-name", project_name, "--project-directory", str(probe_work),
                "--env-file", str(env_file), "-f", str(compose), "create", "--no-build",
                contract["service_id"], timeout=300)
        ids = _docker("compose", "--project-name", project_name, "--project-directory", str(probe_work),
                      "--env-file", str(env_file), "-f", str(compose), "ps", "-q", "--all",
                      contract["service_id"]).stdout.decode().split()
        if len(ids) != 1:
            raise ValidationError("negative host probe did not create one container")
        container_id = ids[0]
        container = inspect_one("container", container_id)
        manifest_raw = _copy_bytes(container_id, _SOURCE_ROOT + "/" + contract["_runtime_contract"])
        release_raw = _copy_bytes(container_id, _SOURCE_ROOT + "/RELEASE.json")
        policy = _policy(contract, binding, image_id, image, container, scope, compose,
                         env_file, rendered_hash, _sha(manifest_raw), _sha(marker_path.read_bytes()),
                         _sha(release_raw), host)
        field = "runtime_manifest_sha256" if mutation == "manifest" else "release_sha256"
        policy[field] = "0" * 64
        policy_path = probe_work / "policy.json"
        policy_path.write_bytes(_canonical(policy))
        os.chmod(policy_path, 0o600)
        trust = _strict_json((root / "02_configs/production_runtime_trust.json").read_bytes(), "trust")
        key_id = next(item["key_id"] for item in trust["keys"]
                      if item.get("domain") == "candidate_validation")
        try:
            host.issue_execution_grant(container_id, expected_policy_path=policy_path,
                                       key_path=_KEY_ROOT / (key_id + ".pem"),
                                       grant_path=grant_dir / "grant.json", grant_dir=grant_dir,
                                       role="candidate_validation", ttl_seconds=900)
        except host.HostAuthorizationError as exc:
            expected = ("runtime manifest/marker differs" if mutation == "manifest"
                        else "RELEASE bytes differ")
            if expected not in str(exc):
                raise ValidationError(f"host {mutation} rejection occurred at wrong check: {exc}") from exc
        else:
            raise ValidationError(f"host issuer accepted wrong {mutation} identity")
    finally:
        if container_id:
            _docker("rm", "-f", container_id, check=False, timeout=120)
        _docker("compose", "--project-name", project_name, "--project-directory", str(probe_work),
                "--env-file", str(env_file), "-f", str(compose), "down", "--remove-orphans",
                check=False, timeout=120)
        if scope is not None:
            shutil.rmtree(scope["candidate_host_root"], ignore_errors=True)
            descriptor = Path(scope["candidate_scope"]["descriptor_path"])
            for candidate in (descriptor.with_name(descriptor.name + ".consumed"), descriptor):
                try:
                    candidate.unlink()
                except FileNotFoundError:
                    pass


@contextmanager
def _protected_work() -> Iterator[Path]:
    _PROTECTED_WORK_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(_PROTECTED_WORK_ROOT, 0o700)
    state = _PROTECTED_WORK_ROOT.stat()
    if state.st_uid != 0 or stat.S_IMODE(state.st_mode) != 0o700:
        raise ValidationError("validation work root is not protected")
    path = Path(tempfile.mkdtemp(prefix="candidate-", dir=_PROTECTED_WORK_ROOT))
    os.chmod(path, 0o700)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def validate_linux(root: Path, project: Mapping[str, Any], contract: dict[str, Any],
                   binding: Mapping[str, Any], builder_id: str) -> dict[str, Any]:
    host = _load(root / "09_deploy/runtime_identity/host_authorization.py",
                 "_host_authorization_engine")
    parser = _load(root / "03_src/agri_research_agent/shared/runtime_manifest.py",
                   "_runtime_manifest_engine")
    container_id = None
    with _protected_work() as work:
        validate_source_compose(root, contract)
        context = work / "context"
        create_archive_context(root, context, binding)
        image_id = build_image(root, context, contract, binding)
        image = inspect_one("image", image_id)
        uid, gid = _numeric_user(image)
        contract["_numeric_uid"] = uid
        contract["_container_user"] = image["Config"]["User"]
        contract["_runtime_contract"] = project["runtime_contract"]
        _actual_host_rejection(root, host, contract, binding, image_id, image, work, "manifest")
        _actual_host_rejection(root, host, contract, binding, image_id, image, work, "release")
        grant_dir = work / "grants"
        grant_dir.mkdir(mode=0o755)
        contract["_grant_dir"] = grant_dir
        compose = work / "compose.json"
        env_file = work / "compose.env"
        project_name = "market-data-runtime-validation"
        scope = None
        try:
            scope = host.create_candidate_scope(_runtime_bindings(contract, uid, gid))
            identity_root = next(item["container_path"] for item in contract["runtime_roots"] if item["role"] == contract["identity_root_role"])
            marker = {"schema_version": 1, "runtime_id": "target-validation",
                      "module_id": contract["module_id"], "classification": "candidate-validation",
                      "created_at": datetime.now(timezone.utc).isoformat()}
            identity_source = next(Path(item["source"]) for item in scope["mounts"]
                                   if item["target"] == identity_root)
            marker_path = identity_source / ".market-data-runtime.json"
            marker_path.write_bytes(_canonical(marker) + b"\n")
            os.chmod(marker_path, 0o444)
            hostname = os.urandom(16).hex()
            compose_doc = _compose_document(contract, image_id, scope["mounts"], grant_dir, hostname)
            compose.write_bytes(_canonical(compose_doc) + b"\n")
            os.chmod(compose, 0o600)
            env_file.write_text("", encoding="utf-8")
            os.chmod(env_file, 0o600)
            rendered, rendered_hash = _render_compose(work, compose, env_file)
            _docker("compose", "--project-name", project_name, "--project-directory", str(work),
                    "--env-file", str(env_file), "-f", str(compose), "create", "--no-build",
                    contract["service_id"], timeout=300)
            ids = _docker("compose", "--project-name", project_name, "--project-directory", str(work),
                          "--env-file", str(env_file), "-f", str(compose), "ps", "-q",
                          "--all", contract["service_id"]).stdout.decode().split()
            if len(ids) != 1:
                raise ValidationError("Compose did not create exactly one candidate container")
            container_id = ids[0]
            container = inspect_one("container", container_id)
            if container.get("State", {}).get("Running") is True:
                raise ValidationError("grant issuer requires an unstarted container")
            if _docker("cp", f"{container_id}:{_SOURCE_ROOT}/.git", "-", check=False).returncode == 0:
                raise ValidationError("Git metadata is present in candidate image")
            manifest_raw = _copy_bytes(container_id, _SOURCE_ROOT + "/" + project["runtime_contract"])
            if _sha(manifest_raw) != binding["source_sha256"][project["runtime_contract"]]:
                raise ValidationError("image runtime manifest differs from candidate")
            _manifest_identity(manifest_raw, contract, parser)
            release_raw = _copy_bytes(container_id, _SOURCE_ROOT + "/RELEASE.json")
            image_labels = image["Config"]["Labels"]
            _release_identity(release_raw, binding, contract["project_id"],
                              image_labels["market-data.release.id"], image_labels)
            marker_raw = marker_path.read_bytes()
            policy = _policy(contract, binding, image_id, image, container, scope, compose,
                             env_file, rendered_hash, _sha(manifest_raw), _sha(marker_raw),
                             _sha(release_raw), host)
            policy_path = work / "policy.json"
            policy_path.write_bytes(_canonical(policy))
            os.chmod(policy_path, 0o600)
            observed = host.normalize_observation(container, image,
                                                  _strict_json(release_raw, "RELEASE"))
            probes = _negative_observation_probes(host, observed, policy)
            probes["wrong_manifest_rejected"] = "PASS"
            probes["release_mismatch_rejected"] = "PASS"
            key_path = _KEY_ROOT / (policy["key_id"] + ".pem")
            host.issue_execution_grant(container_id, expected_policy_path=policy_path,
                                       key_path=key_path, grant_path=grant_dir / "grant.json",
                                       grant_dir=grant_dir, role="candidate_validation", ttl_seconds=900)
            _docker("start", container_id, timeout=120)
            started = inspect_one("container", container_id)
            if started.get("State", {}).get("Running") is not True:
                raise ValidationError("declared entrypoint did not remain running")
            _exec(container_id, _identity_probe_argv(contract, _sha(marker_raw)), label="runtime_identity")
            probes["runtime_identity"] = "PASS"
            _exec(container_id, _identity_probe_argv(contract, _sha(marker_raw), missing=True),
                  expect_success=False, label="missing_grant_rejected")
            probes["missing_grant_rejected"] = "PASS"
            for executable in contract["required_executables"]:
                _exec(container_id, ["python", "-B", "-c",
                      "import shutil,sys;sys.exit(shutil.which(sys.argv[1]) is None)", executable],
                      label="executable:" + executable)
            for module in contract["required_python_modules"]:
                _exec(container_id, ["python", "-B", "-c", "import importlib.util,sys;sys.exit(importlib.util.find_spec(sys.argv[1]) is None)", module],
                      label="python-module:" + module)
            probes["dependencies"] = "PASS"
            for command in contract["initialization_commands"]:
                _exec(container_id, command["argv"], cwd=contract["working_directory"],
                      label="initialization:" + command["name"])
            probes["entrypoint_initialization"] = "PASS"
            # Permission checks use direct argv. The first readonly root must reject,
            # every declared writable child must accept and remove a sentinel.
            for item in contract["runtime_roots"]:
                sentinel = item["container_path"] + "/.target-runtime-write-probe"
                argv = ["python", "-B", "-c", "from pathlib import Path; p=Path(__import__('sys').argv[1]); p.write_text('x'); p.unlink()", sentinel]
                _exec(container_id, argv, expect_success=item["access"] == "rw",
                      label="mount-permission:" + item["role"])
            probes["runtime_paths"] = "PASS"
            probes["mount_permissions"] = "PASS"
            # Preview/candidate execution has no production mount authority and
            # cannot write immutable application source.
            actual_mounts = host._mounts(started)
            if any(item["source"] != str(grant_dir)
                   and not item["source"].startswith(scope["candidate_host_root"] + "/")
                   for item in actual_mounts):
                raise ValidationError("candidate contains a non-candidate mount")
            _exec(container_id, _identity_probe_argv(contract, _sha(marker_raw), role="production"),
                  expect_success=False, label="candidate-cannot-authorize-production")
            _exec(container_id, ["python", "-B", "-c",
                  "from pathlib import Path; p=Path('/app/.preview-write-probe'); p.write_text('x')"],
                  expect_success=False, label="preview-source-write-rejected")
            probes["preview_write_rejected"] = "PASS"
            if set(probes) != REQUIRED_PROBES or any(value != "PASS" for value in probes.values()):
                raise ValidationError("required probe set was not actually completed")
            return {
                "schema_version": EVIDENCE_SCHEMA, "binding": binding,
                "TARGET_RUNTIME_STATIC_VALIDATION": "PASS",
                "TARGET_RUNTIME_CONTAINER_VALIDATION": "PASS", "image_id": image_id,
                "rendered_compose_sha256": rendered_hash,
                "builder": {"builder_id": builder_id, "os": "linux", "execution": "isolated"},
                "observed_identity": {"image_id": image_id, "oci_revision": binding["commit"],
                    "git_tree": binding["tree"], "source_sha256": binding["source_sha256"],
                    "rendered_compose_sha256": rendered_hash,
                    "authorization_role": "candidate_validation", "git_metadata_present": False,
                    "production_volumes_mounted": False},
                "probes": probes,
            }
        finally:
            if container_id:
                _docker("rm", "-f", container_id, check=False, timeout=120)
            _docker("compose", "--project-name", "market-data-runtime-validation",
                    "--project-directory", str(work), "--env-file", str(env_file),
                    "-f", str(compose), "down", "--remove-orphans", check=False, timeout=120)
            if scope is not None:
                shutil.rmtree(scope.get("candidate_host_root", ""), ignore_errors=True)
                descriptor = Path(scope["candidate_scope"]["descriptor_path"])
                for candidate in (descriptor.with_name(descriptor.name + ".consumed"), descriptor):
                    try:
                        candidate.unlink()
                    except FileNotFoundError:
                        pass


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--project", required=True)
    parser.add_argument("--runtime-contract", required=True)
    parser.add_argument("--evidence-output", required=True)
    args = parser.parse_args(argv)
    if not _ID.fullmatch(args.project):
        parser.error("invalid project identity")
    output = Path(args.evidence_output)
    if not output.is_absolute():
        parser.error("evidence output must be absolute")
    args.evidence_output = output
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        root = repository_root()
        project, contract, binding = source_contract(root, args.project, args.runtime_contract)
        try:
            builder_id = require_builder()
        except BuilderUnavailable:
            write_evidence(args.evidence_output, blocked_evidence(binding))
            return 3
        evidence = validate_linux(root, project, contract, binding, builder_id)
        write_evidence(args.evidence_output, evidence)
        return 0
    except (ValidationError, OSError, KeyError, TypeError, ValueError) as exc:
        print(f"TARGET_RUNTIME_VALIDATION=FAIL: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
