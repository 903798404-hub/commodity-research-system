"""Build and validate one clean production-container candidate on Linux.

This program is the sole evidence producer consumed by target_runtime_gate.py.
It accepts source identity only; image identity, Compose identity and probe
results are observations made here.  It never accepts caller-supplied evidence
or production host paths.
"""
from __future__ import annotations

import argparse
from functools import lru_cache
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import importlib.util
import inspect
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
_V3_VALIDATOR_VERSION = "target-runtime-validator/2"
_BUILD_INVOCATIONS = 0
_BUILD_ALLOWED = True


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
    return _interpret("strict_object", raw, label)


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


@lru_cache(maxsize=1)
def _observation():
    path = Path(__file__).resolve().parents[2] / "09_deploy/runtime_identity/runtime_observation.py"
    return _load(path, "_engine_runtime_observation")


def _interpret(name, *args, **kwargs):
    module = _observation()
    try:
        return getattr(module, name)(*args, **kwargs)
    except module.ObservationError as exc:
        raise ValidationError(str(exc)) from exc


def _candidate_issuer_api(host, contract, *, external_trust=False):
    """Check the application-pinned signing ABI before allocating a candidate.

    Pure declaration interpretation belongs to this independently versioned
    tool. Authority and grant format continue to belong to the application
    issuer; an old issuer is never upgraded by replacing its module.
    """
    grant_keywords = dict(expected_policy_path=Path('/unused'), key_path=Path('/unused'),
        grant_path=Path('/unused'), grant_dir=Path('/unused'), role='candidate_validation', ttl_seconds=900)
    if external_trust:
        grant_keywords['external_candidate_trust_path'] = Path('/unused')
    calls = dict(create_candidate_scope=(([],), {}),
        normalize_observation=(({}, {}, {}), {}), validate_observation=(({}, {}), {'role': 'candidate_validation'}),
        _validate_runtime_mounts=(({}, [], {}), {}), _validate_v3_runtime=(({}, {}, {}, 'unused'), {}),
        _mounts=(({},), {}), issue_execution_grant=(('unused',), grant_keywords))
    if '/run/secrets/market-data-service.json' in contract['_secret_declarations'].values():
        calls['issue_application_service_credential'] = (('unused',), dict(
            expected_policy_path=Path('/unused'), credential_path=Path('/unused'), role='candidate_validation'))
    for name, (args, kwargs) in calls.items():
        method = vars(host).get(name)
        if not callable(method):
            raise ValidationError('UNSUPPORTED_APPLICATION_ISSUER_API: ' + name)
        try:
            inspect.signature(method).bind(*args, **kwargs)
        except (TypeError, ValueError) as exc:
            raise ValidationError('UNSUPPORTED_APPLICATION_ISSUER_API: ' + name) from exc
    # The original embedded-trust issuer supports directory-only candidate
    # scopes. Its source Compose secrets are validated statically, never
    # transported into an offline candidate. The newer signing ABI accompanies
    # file scopes and application-service credentials. This is an explicit
    # supported ABI choice, not recovery from a resolver/issuer exception.
    parameters = inspect.signature(host.issue_execution_grant).parameters
    required = {'container_id', 'expected_policy_path', 'key_path', 'grant_path',
                'grant_dir', 'role', 'ttl_seconds'}
    if (not required <= set(parameters) or
            set(parameters) - required - {'external_candidate_trust_path', 'primary_rollback_context'} or
            any(item.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
                for item in parameters.values())):
        raise ValidationError('UNSUPPORTED_APPLICATION_ISSUER_API: ambiguous signing ABI')
    return 'file_scope' if 'external_candidate_trust_path' in parameters else 'directory_scope'


def _candidate_secret_transport(contract, issuer_abi):
    source = contract['_secret_declarations']
    if issuer_abi == 'file_scope':
        return dict(source)
    if issuer_abi == 'directory_scope':
        if '/run/secrets/market-data-service.json' in source.values():
            raise ValidationError('UNSUPPORTED_APPLICATION_ISSUER_API: file credential scope')
        return {}  # Original static-only secret contract, not production secrets.
    raise ValidationError('UNSUPPORTED_APPLICATION_ISSUER_API: candidate scope')


def _issue_candidate_grant(host, *args, external_candidate_trust_path=None, **kwargs):
    # Legacy consumers only support embedded candidate trust. Omit an unused
    # new keyword, not a missing validation. Requested external trust requires
    # the explicitly checked newer ABI.
    if external_candidate_trust_path is not None:
        kwargs['external_candidate_trust_path'] = external_candidate_trust_path
    return host.issue_execution_grant(*args, **kwargs)


def _packaging_lifecycle_modules(contract):
    names = ('lifecycle', 'lifecycle_events', 'lifecycle_reconciler', 'lifecycle_store')
    declared = {item['path'] for item in contract['source_inputs']}
    present = [name for name in names if '03_src/agri_research_agent/import_profit/' + name + '.py' in declared]
    if present and len(present) != len(names):
        raise ValidationError('partial lifecycle packaging declaration is unsupported')
    # A legacy application has no such modules or signed packaging claim.
    # Its own complete image import graph and all 13 required probes still run.
    return present


def repository_root() -> Path:
    root = Path(__file__).resolve().parents[2]
    if not (root / ".git").exists():
        # Git worktrees normally expose .git as a file.
        raise ValidationError("validator must execute from a real Git worktree")
    return root


def _run(args: Sequence[str], *, cwd: Path | None = None,
         input_bytes: bytes | None = None, timeout: int = 600,
         check: bool = True) -> subprocess.CompletedProcess[bytes]:
    global _BUILD_INVOCATIONS
    if len(args) >= 2 and args[0] == 'docker' and (args[1] == 'build' or tuple(args[1:3]) == ('buildx', 'build')):
        _BUILD_INVOCATIONS += 1
        if not _BUILD_ALLOWED:
            raise ValidationError('existing-image validation must never invoke build')
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
    if contract.get("schema_version") == "runtime-manifest/3":
        paths.extend(item["source_path"] for item in contract["candidate_runtime_inputs"])
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
    version = _VALIDATOR_VERSION
    if contract.get("schema_version") == "runtime-manifest/3":
        version = _V3_VALIDATOR_VERSION
        if any(hashes[item["source_path"]] != item["sha256"] for item in contract["candidate_runtime_inputs"]):
            raise ValidationError("candidate runtime fixture differs from declared Git SHA")
    return {"project_id": project["project_id"], "commit": _git(root, "rev-parse", "HEAD"),
            "tree": _git(root, "rev-parse", "HEAD^{tree}"), "source_sha256": hashes,
            "validator_version": version}


def validate_dockerfile_inputs(root: Path, contract: Mapping[str, Any],
                               binding: Mapping[str, Any], *,
                               copy_targets: dict[str, str] | None = None) -> str:
    """Accept only explicit file COPY inputs in the supported build grammar.

    This checks context membership, not arbitrary RUN program behavior. Network
    dependencies still belong to the declared dependency contracts. Reject build
    features whose input provenance this validator cannot establish.
    """
    text = _exact_source(root, contract["build"]["dockerfile"]).read_text(encoding="utf-8")
    build = contract["build"]
    metadata = {build["dockerfile"], build["dockerignore"],
                *build["compose_sources"], _ENGINE_PATH}
    if contract.get("schema_version") == "runtime-manifest/3":
        metadata.update(item["source_path"] for item in contract["candidate_runtime_inputs"])
    allowed = set(binding["source_sha256"]) - metadata
    required = allowed
    copied = set()
    instructions = []
    pending = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            if re.match(r"#\s*(syntax|escape)\s*=", stripped, re.IGNORECASE):
                raise ValidationError("Dockerfile parser directives are unsupported")
            continue
        if not stripped:
            continue
        if stripped.endswith("\\"):
            pending += stripped[:-1] + " "
            continue
        instructions.append(pending + stripped)
        pending = ""
    if pending:
        raise ValidationError("Dockerfile has an unfinished continuation")
    stages = 0
    base_image = ""
    known = {"FROM", "ARG", "LABEL", "ENV", "WORKDIR", "RUN", "COPY", "USER",
             "ENTRYPOINT", "CMD", "EXPOSE", "HEALTHCHECK", "STOPSIGNAL"}
    for instruction in instructions:
        fields = instruction.split(None, 1)
        op = fields[0].upper()
        if op not in known or len(fields) != 2:
            raise ValidationError("unsupported Dockerfile instruction or input semantics: " + op)
        value = fields[1].strip()
        if op == "FROM":
            stages += 1
            if stages != 1 or not re.fullmatch(r"[a-z0-9][a-z0-9./:_-]*@sha256:[0-9a-f]{64}", value):
                raise ValidationError("Dockerfile requires a single digest-pinned base image without flags")
            base_image = value
        elif op == "RUN":
            if value.startswith("--") or "<<" in value:
                raise ValidationError("Dockerfile RUN mounts, flags and heredocs are unsupported")
        elif op == "COPY":
            if stages != 1 or value.startswith("--"):
                raise ValidationError("Dockerfile COPY flags or stage sources are unsupported")
            if value.startswith("["):
                try:
                    operands = json.loads(value)
                except ValueError as exc:
                    raise ValidationError("invalid Dockerfile COPY JSON") from exc
                if (not isinstance(operands, list) or len(operands) < 2
                        or any(not isinstance(item, str) or not item for item in operands)):
                    raise ValidationError("Dockerfile COPY requires source and destination strings")
            else:
                # Do not pretend shlex matches Docker's variable/escape grammar.
                if any(char in value for char in "\"'\\"):
                    raise ValidationError("Dockerfile COPY quoting requires JSON form")
                operands = value.split()
                if len(operands) < 2:
                    raise ValidationError("Dockerfile COPY requires source and destination")
            for source in operands[:-1]:
                if (any(char in source for char in "$*?[]<>")
                        or any(ord(char) < 32 or ord(char) == 127 for char in source)
                        or source not in allowed):
                    raise ValidationError("Dockerfile COPY source is not an exact bound input: " + source)
                _exact_source(root, source)
                copied.add(source)
            destination = operands[-1]
            pure_destination = PurePosixPath(destination)
            if (not pure_destination.is_absolute() or ".." in pure_destination.parts
                    or any(char in destination for char in "$\\")
                    or any(ord(char) < 32 or ord(char) == 127 for char in destination)):
                raise ValidationError("Dockerfile COPY destination must be an absolute literal path")
            if copy_targets is not None:
                if len(operands) > 2 and not destination.endswith("/"):
                    raise ValidationError("multiple COPY sources require a directory destination")
                for source in operands[:-1]:
                    target = str(pure_destination / PurePosixPath(source).name
                                 if destination.endswith("/") else pure_destination)
                    if target in copy_targets and copy_targets[target] != source:
                        raise ValidationError("Dockerfile COPY overwrites a bound image input")
                    copy_targets[target] = source
    if stages != 1:
        raise ValidationError("Dockerfile requires one source stage")
    if missing := required - copied:
        raise ValidationError("Dockerfile omits required runtime COPY inputs: " + ", ".join(sorted(missing)))
    return base_image


def require_base_image(base_image: str) -> None:
    # ONBUILD COPY/ADD in the base executes even if this Dockerfile has no such
    # instruction. Inspect the exact digest before allowing the candidate build.
    _docker("pull", base_image, timeout=600)
    base_id = _docker("image", "inspect", "--format", "{{.Id}}", base_image).stdout.decode("utf-8", "strict").strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", base_id):
        raise ValidationError("base image resolved ID is invalid")
    base = inspect_one("image", base_id)
    config = base.get("Config")
    if not isinstance(config, dict) or "OnBuild" not in config or config["OnBuild"] not in (None, []):
        raise ValidationError("base image ONBUILD contract is missing or contains inherited instructions")


def _python_module_probe_argv(module: str) -> list[str]:
    return ["python", "-B", "-c",
            "import importlib,sys;importlib.import_module(sys.argv[1])", module]


def _ephemeral_candidate_identity(work: Path, contract: dict[str, Any]) -> None:
    """Create a run-local candidate signer, never a production trust root."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (Encoding, PrivateFormat,
                                                               PublicFormat, NoEncryption)
    import base64
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    key_id = "hosted-candidate-" + os.urandom(12).hex()
    private_path = work / "ephemeral-candidate-key.pem"
    _write_new(private_path, key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8,
                                               NoEncryption()), 0o600)
    contract["_ephemeral_candidate_private_key"] = private_path
    contract["_ephemeral_candidate_key_id"] = key_id
    contract["_ephemeral_candidate_public_fingerprint"] = _sha(public)
    contract["_ephemeral_candidate_public_trust"] = _canonical({
        "schema_version": "candidate-validation-public-trust/1",
        "role": "candidate_validation", "key_id": key_id,
        "algorithm": "ed25519", "public_key_base64": base64.b64encode(public).decode("ascii"),
    })


def _install_candidate_public_trust(grant_dir: Path, contract: Mapping[str, Any]) -> Path | None:
    raw = contract.get("_ephemeral_candidate_public_trust")
    if raw is None:
        return None
    path = grant_dir / "candidate-validation-trust.json"
    _write_new(path, raw, 0o444)
    return path


def _candidate_signing_key(contract: Mapping[str, Any], root: Path) -> Path:
    private = contract.get("_ephemeral_candidate_private_key")
    if private is not None:
        return private
    trust = _strict_json((root / "02_configs/production_runtime_trust.json").read_bytes(), "trust")
    key_id = next(item["key_id"] for item in trust["keys"]
                  if item.get("domain") == "candidate_validation")
    return _KEY_ROOT / (key_id + ".pem")


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
        raise ValidationError("runtime manifest violates source contract") from exc
    if (contract.get("schema_version") not in {"runtime-manifest/2", "runtime-manifest/3"}
            or contract.get("project_id") != project_id):
        raise ValidationError("actual container validation requires runtime-manifest/2 or /3")
    binding = _candidate_binding(root, project, contract)
    validate_dockerfile_inputs(root, contract, binding)
    if set(contract["validation_probes"]) != REQUIRED_PROBES:
        raise ValidationError("runtime manifest probe set differs from engine")
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


def _strict_json_value(raw: bytes, label: str):
    return _interpret("strict_json_value", raw, label)


def inspect_one(kind: str, identity: str) -> dict[str, Any]:
    exact = identity if kind == "image" or re.fullmatch(r"[0-9a-f]{64}", identity) else None
    return _interpret("inspect_object", _docker(kind, "inspect", identity).stdout, f"{kind} inspect", expected_id=exact)


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
    require_base_image(validate_dockerfile_inputs(context, contract, binding))
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
            "--label", "org.opencontainers.image.source=" + binding["validator_version"],
            "--label", f"org.opencontainers.image.created={build_time}"]
    build_args = {
        "MARKET_DATA_GIT_HEAD": binding["commit"], "MARKET_DATA_GIT_TREE": binding["tree"],
        "MARKET_DATA_RELEASE_ID": release_id,
        "MARKET_DATA_BUILD_TIME": build_time,
        "MARKET_DATA_SOURCE": binding["validator_version"],
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


def _image_import_closure(root: Path, contract: Mapping[str, Any],
                          container_id: str) -> dict[str, Any]:
    """Compare the existing import graph with the actual immutable image files."""
    tracked = _git(root, "ls-files", "-z", binary=True).decode("utf-8").split("\0")
    sources = {name: (root / name).read_bytes() for name in tracked if name}
    graph_engine = _load(root / "04_scripts/runtime/pre_release_runtime.py",
                         "_spread_runtime_import_graph")
    graph = graph_engine._runtime_graph(sources, contract)
    if not graph["complete"]:
        raise ValidationError("spread runtime import graph is incomplete")
    required = {name for name in graph["active_paths"] if name.endswith(".py")
                and name.startswith(("03_src/", "04_scripts/", "05_apps/"))}
    manifest = {item["path"] for item in contract["source_inputs"] if item["path"].endswith(".py")}
    manifest.add(_MANIFEST_PARSER)
    copied = set()
    dockerfile = sources[contract["build"]["dockerfile"]].decode("utf-8")
    for line in dockerfile.splitlines():
        if line.startswith("COPY ["):
            copied.update(name for name in json.loads(line[5:])[:-1]
                          if name.endswith(".py"))
    missing_manifest = sorted(required - manifest)
    missing_dockerfile = sorted(required - copied)
    missing_image = []
    for name in sorted(required):
        try:
            actual = _copy_bytes(container_id, _SOURCE_ROOT + "/" + name)
        except ValidationError:
            missing_image.append(name)
            continue
        if actual != sources[name]:
            missing_image.append(name)
    if missing_manifest or missing_dockerfile or missing_image:
        raise ValidationError("final image runtime import closure is incomplete: " +
                              repr({"manifest": missing_manifest, "dockerfile": missing_dockerfile,
                                    "image": missing_image}))
    return {"required_module_count": len(required), "manifest_module_count": len(manifest),
            "dockerfile_module_count": len(copied), "missing_from_manifest": [],
            "missing_from_dockerfile": [], "missing_from_final_image": []}


def _image_bound_inputs(root: Path, contract: Mapping[str, Any],
                        binding: Mapping[str, Any], container_id: str) -> None:
    """Verify actual copied bytes, not merely an image's self-reported labels.

    Reuse the strict build grammar for destinations. Generated RELEASE identity
    and dependency/runtime probes remain separately mandatory in the same lane.
    This does not claim to reproduce arbitrary RUN instructions from file hashes.
    """
    targets: dict[str, str] = {}
    validate_dockerfile_inputs(root, contract, binding, copy_targets=targets)
    for target, source in sorted(targets.items()):
        expected = binding["source_sha256"][source]
        if _sha(_exact_source(root, source).read_bytes()) != expected:
            raise ValidationError("bound source changed during image validation: " + source)
        if _sha(_copy_bytes(container_id, target)) != expected:
            raise ValidationError("image copied input differs from bound source: " + target)


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
    for name, target in contract.get("_secret_declarations", {}).items():
        result.append({"relative_path": "service-private/" + name + ".json", "target": target,
                       "read_only": True, "owner_uid": 0, "owner_gid": gid, "kind": "file"})
    return sorted(result, key=lambda item: (item["relative_path"].count("/"), item["relative_path"]))


def _exclude_candidate_inputs(context: Path, contract: Mapping[str, Any]) -> None:
    for item in contract.get("candidate_runtime_inputs", []):
        path = context.joinpath(*PurePosixPath(item["source_path"]).parts)
        if (not path.is_file() or path.is_symlink()
                or not path.resolve(strict=True).is_relative_to(context.resolve(strict=True))):
            raise ValidationError("candidate fixture is missing or aliased in build context")
        path.unlink()


def _seed_candidate_runtime_inputs(root: Path, contract: Mapping[str, Any],
                                   binding: Mapping[str, Any], scope: Mapping[str, Any], host) -> None:
    roots = {item["role"]: item for item in contract["runtime_roots"]}
    total = 0
    for item in contract.get("candidate_runtime_inputs", []):
        declared = roots[item["role"]]
        matches = [mount for mount in scope["mounts"]
                   if mount["target"] == declared["container_path"]]
        if (declared["access"] != "ro" or item["role"] == contract["identity_root_role"]
                or len(matches) != 1 or matches[0]["read_only"] is not True):
            raise ValidationError("candidate fixture requires an exact readonly child mount")
        base = host._protected_path(Path(matches[0]["source"]), directory=True, temporary=True)
        candidate_root = host._protected_path(Path(scope["candidate_host_root"]), directory=True, temporary=True)
        if not base.is_relative_to(candidate_root) or base == candidate_root:
            raise ValidationError("candidate fixture mount is outside candidate scope")
        parts = PurePosixPath(item["relative_path"]).parts
        if (not parts or "/" in parts or any(part in {".", "..", ".git", ".market-data-runtime.json"}
                                            for part in parts)):
            raise ValidationError("candidate fixture target is unsafe")
        raw = _git(root, "show", binding["commit"] + ":" + item["source_path"], binary=True)
        total += len(raw)
        if (total > 64 * 1024 * 1024 or _sha(raw) != item["sha256"]
                or binding["source_sha256"].get(item["source_path"]) != item["sha256"]):
            raise ValidationError("candidate fixture bytes differ from bound source or exceed size limit")
        parent = base
        for part in parts[:-1]:
            parent = parent / part
            if not parent.exists():
                parent.mkdir(mode=0o755)
            host._protected_path(parent, directory=True, temporary=True)
        target = parent / parts[-1]
        _write_new(target, raw, 0o444)
        host._protected_path(target, temporary=True)


def _candidate_environment(contract: Mapping[str, Any]) -> dict[str, str]:
    environment = {name: f"candidate-validation-{name.lower()}"
                   for name in contract["required_environment"]}
    environment["MARKET_DATA_EXECUTION_GRANT"] = _GRANT_ROOT + "/grant.json"
    if contract.get("_ephemeral_candidate_public_trust") is not None:
        environment["MARKET_DATA_CANDIDATE_EXTERNAL_TRUST"] = "1"
    if contract.get("schema_version") != "runtime-manifest/3":
        return environment
    roots = {item["role"]: item["container_path"] for item in contract["runtime_roots"]}
    for item in contract["environment_bindings"]:
        kind = item["kind"]
        if kind == "literal":
            value = item["value"]
        elif kind == "runtime_path":
            value = roots[item["role"]] + ("/" + item["relative_path"] if item["relative_path"] else "")
        elif kind == "deployment":
            value = item["candidate_value"]
        elif kind == "execution_grant":
            value = _GRANT_ROOT + "/grant.json"
        else:
            raise ValidationError("unknown environment binding kind")
        environment[item["name"]] = value
    return environment


def _compose_document(contract: Mapping[str, Any], image_id: str,
                      mounts: Sequence[Mapping[str, Any]], grant_dir: Path,
                      hostname: str) -> dict[str, Any]:
    secret_targets = set(contract.get("_secret_declarations", {}).values())
    volumes = [{"type": "bind", "source": item["source"], "target": item["target"],
                "read_only": item["read_only"]} for item in mounts if item["target"] not in secret_targets]
    volumes.append({"type": "bind", "source": str(grant_dir),
                    "target": _GRANT_ROOT, "read_only": True})
    environment = _candidate_environment(contract)
    result = {"name": "market-data-runtime-validation",
            "services": {contract["service_id"]: {
                "image": image_id, "entrypoint": contract["entrypoint"],
                "working_dir": contract["working_directory"], "hostname": hostname,
                "read_only": True, "user": contract["_container_user"],
                "network_mode": "none", "cap_drop": ["ALL"],
                "security_opt": ["no-new-privileges:true"],
                "environment": environment, "volumes": volumes,
            "restart": "no"}}}
    if secret_targets:
        service = result["services"][contract["service_id"]]
        service["secrets"] = [{"source": name, "target": target}
                              for name, target in contract["_secret_declarations"].items()]
        result["secrets"] = {name: {"file": next(item["source"] for item in mounts if item["target"] == target)}
                             for name, target in contract["_secret_declarations"].items()}
    return result


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
    if type(build) is not dict or set(build) != {"context", "dockerfile"}:
        raise ValidationError("source Compose build contract is not reproducible")
    context = build.get("context")
    expected_context = root.resolve(strict=True)
    if (not isinstance(context, str) or not context
            or not Path(context).is_absolute() or Path(context) != expected_context
            or Path(context).resolve(strict=True) != expected_context):
        raise ValidationError("source Compose build context differs from candidate root")
    if build.get("dockerfile") != contract["build"]["dockerfile"]:
        raise ValidationError("source Compose Dockerfile differs from runtime manifest")
    environment = service.get("environment") or {}
    if not isinstance(environment, dict) or not set(contract["required_environment"]).issubset(environment):
        raise ValidationError("source Compose required environment contract is incomplete")
    if contract.get("schema_version") == "runtime-manifest/3":
        if set(environment) != set(contract["required_environment"]):
            raise ValidationError("source Compose environment differs from declared names")
        expected = _candidate_environment(contract)
        for item in contract["environment_bindings"]:
            actual = environment[item["name"]]
            if item["kind"] != "deployment" and actual != expected[item["name"]]:
                raise ValidationError("source Compose fixed environment binding differs from manifest")
            if not isinstance(actual, str) or not actual.strip():
                raise ValidationError("source Compose environment value is missing")
    if ("MARKET_DATA_EXECUTION_GRANT" not in contract["required_environment"]
            or environment.get("MARKET_DATA_EXECUTION_GRANT") != _GRANT_ROOT + "/grant.json"):
        raise ValidationError("source Compose execution grant environment is invalid")
    _interpret("declared_secret_targets", contract, rendered)
    volumes = service.get("volumes") or []
    actual_mounts = _interpret("compose_mount_targets", volumes)
    expected_mounts = {item["container_path"]: item["read_only"]
                       for item in contract["required_mounts"]}
    expected_mounts[_GRANT_ROOT] = True
    if actual_mounts != expected_mounts:
        raise ValidationError("source Compose mounts differ from runtime manifest")
    grant_mounts = [item for item in volumes if item.get("target") == _GRANT_ROOT]
    if (len(grant_mounts) != 1 or grant_mounts[0].get("type") != "bind"
            or not isinstance(grant_mounts[0].get("source"), str)
            or not grant_mounts[0]["source"].strip()
            or grant_mounts[0].get("read_only") is not True):
        raise ValidationError("source Compose execution grant mount is invalid")
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
                or release.get("source") != binding["validator_version"]):
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
    key_id = contract.get("_ephemeral_candidate_key_id", keys[0]["key_id"])
    result = {
        "schema_version": ("host-runtime-policy/4" if contract.get("schema_version") == "runtime-manifest/3"
                           else "host-runtime-policy/2"), "role": "candidate_validation",
        "key_id": key_id, "project_id": contract["project_id"],
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
    if "/run/secrets/market-data-service.json" in contract.get("_secret_declarations", {}).values():
        result["application_service"] = {"service_id": contract["service_id"], "runtime_id": "target-validation",
            "allowed_writable_roots": [item["container_path"] for item in contract["runtime_roots"] if item["access"] == "rw"]}
    return result


def _exec(container_id: str, argv: Sequence[str], *, cwd: str | None = None,
          expect_success: bool = True, label: str = "unnamed") -> subprocess.CompletedProcess[bytes]:
    args = ["exec"]
    if cwd:
        args.extend(("--workdir", cwd))
    args.extend((container_id, *argv))
    result = _docker(*args, check=False, timeout=300)
    if (result.returncode == 0) != expect_success:
        detail = result.stderr.decode("utf-8", "replace")[-800:].strip()
        raise ValidationError(f"container probe {label} returned an unexpected result: {detail}")
    return result


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
    secret_mutations = {"undeclared_secret", "wrong_secret_target", "writable_secret"}
    if mutation not in {"manifest", "release"} | secret_mutations:
        raise ValidationError("unknown host rejection probe")
    probe_work = work / ("negative-" + mutation)
    probe_work.mkdir(mode=0o700)
    grant_dir = probe_work / "grants"
    grant_dir.mkdir(mode=0o755)
    external_trust = _install_candidate_public_trust(grant_dir, contract)
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
        document = _compose_document(contract, image_id, scope["mounts"], grant_dir, os.urandom(16).hex())
        if mutation in secret_mutations:
            service = document["services"][contract["service_id"]]
            ref = next(item for item in service["secrets"] if item["target"] == "/run/secrets/market-data-service.json")
            if mutation == "undeclared_secret":
                document["secrets"]["undeclared-runtime-secret"] = dict(document["secrets"][ref["source"]])
                service["secrets"].append({"source": "undeclared-runtime-secret", "target": "/run/secrets/undeclared.json"})
            elif mutation == "wrong_secret_target":
                ref["target"] = "/run/secrets/wrong-service.json"
            else:
                secret_file = document["secrets"].pop(ref["source"])["file"]
                service["secrets"].remove(ref)
                service["volumes"].append({"type": "bind", "source": secret_file,
                    "target": "/run/secrets/market-data-service.json", "read_only": False})
        compose.write_bytes(_canonical(document) + b"\n")
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
        if mutation in secret_mutations:
            try:
                host._validate_runtime_mounts(contract, host._mounts(container), policy)
            except host.HostAuthorizationError as exc:
                if "secret" not in str(exc):
                    raise ValidationError("secret rejection occurred at an unrelated check") from exc
            else:
                raise ValidationError("actual Docker secret permission/target violation was accepted")
            return
        if "application_service" in policy:
            credential_policy = probe_work / "credential-policy.json"
            _write_new(credential_policy, _canonical(policy), 0o600)
            credential = next(item["source"] for item in scope["mounts"] if item["target"] == "/run/secrets/market-data-service.json")
            host.issue_application_service_credential(container_id, expected_policy_path=credential_policy,
                credential_path=credential, role="candidate_validation")
        field = "runtime_manifest_sha256" if mutation == "manifest" else "release_sha256"
        policy[field] = "0" * 64
        policy_path = probe_work / "policy.json"
        policy_path.write_bytes(_canonical(policy))
        os.chmod(policy_path, 0o600)
        try:
            _issue_candidate_grant(host, container_id, expected_policy_path=policy_path,
                                       key_path=_candidate_signing_key(contract, root),
                                       grant_path=grant_dir / "grant.json", grant_dir=grant_dir,
                                       role="candidate_validation", ttl_seconds=900,
                                       external_candidate_trust_path=external_trust)
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
                   binding: Mapping[str, Any], builder_id: str, *,
                   ephemeral_candidate_trust: bool = False,
                   existing_image_id: str | None = None) -> dict[str, Any]:
    if existing_image_id is not None and (type(existing_image_id) is not str
                                         or not _IMAGE.fullmatch(existing_image_id)):
        raise ValidationError("existing image must be an exact immutable Image ID")
    host = _load(root / "09_deploy/runtime_identity/host_authorization.py",
                 "_host_authorization_engine")
    parser = _load(root / "03_src/agri_research_agent/shared/runtime_manifest.py",
                   "_runtime_manifest_engine")
    container_id = None
    with _protected_work() as work:
        if ephemeral_candidate_trust:
            _ephemeral_candidate_identity(work, contract)
        source_compose = validate_source_compose(root, contract)
        contract["_secret_declarations"] = _interpret('declared_secret_targets', contract, source_compose)
        issuer_abi = _candidate_issuer_api(host, contract, external_trust=ephemeral_candidate_trust)
        contract['_source_secret_declarations'] = dict(contract['_secret_declarations'])
        contract['_secret_declarations'] = _candidate_secret_transport(contract, issuer_abi)
        lifecycle_modules = _packaging_lifecycle_modules(contract)
        if existing_image_id is None:
            context = work / "context"
            create_archive_context(root, context, binding)
            _exclude_candidate_inputs(context, contract)
            image_id = build_image(root, context, contract, binding)
        else:
            image_id = existing_image_id
        image = inspect_one("image", image_id)
        if image.get("Id") != image_id:
            raise ValidationError("actual image ID differs from requested image")
        _labels(image, binding, contract["service_id"])
        uid, gid = _numeric_user(image)
        contract["_numeric_uid"] = uid
        contract["_container_user"] = image["Config"]["User"]
        contract["_runtime_contract"] = project["runtime_contract"]
        _actual_host_rejection(root, host, contract, binding, image_id, image, work, "manifest")
        _actual_host_rejection(root, host, contract, binding, image_id, image, work, "release")
        service_mounts = None
        if "/run/secrets/market-data-service.json" in contract["_secret_declarations"].values():
            for mutation in ("undeclared_secret", "wrong_secret_target", "writable_secret"):
                _actual_host_rejection(root, host, contract, binding, image_id, image, work, mutation)
            service_mounts = {"declared_readonly": "PASS", "undeclared_rejected": "PASS",
                              "wrong_target_rejected": "PASS", "writable_rejected": "PASS"}
        grant_dir = work / "grants"
        grant_dir.mkdir(mode=0o755)
        external_trust = _install_candidate_public_trust(grant_dir, contract)
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
            _seed_candidate_runtime_inputs(root, contract, binding, scope, host)
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
            packaging = _image_import_closure(root, contract, container_id)
            _image_bound_inputs(root, contract, binding, container_id)
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
            key_path = _candidate_signing_key(contract, root)
            if "application_service" in policy:
                credential = next(item["source"] for item in scope["mounts"] if item["target"] == "/run/secrets/market-data-service.json")
                host.issue_application_service_credential(container_id, expected_policy_path=policy_path,
                    credential_path=credential, role="candidate_validation")
            _issue_candidate_grant(host, container_id, expected_policy_path=policy_path,
                                       key_path=key_path, grant_path=grant_dir / "grant.json",
                                       grant_dir=grant_dir, role="candidate_validation", ttl_seconds=900,
                                       external_candidate_trust_path=external_trust)
            if ephemeral_candidate_trust:
                key_path.unlink()
            _docker("start", container_id, timeout=120)
            started = inspect_one("container", container_id)
            if started.get("State", {}).get("Running") is not True:
                raise ValidationError("declared entrypoint did not remain running")
            _exec(container_id, _identity_probe_argv(contract, _sha(marker_raw)), label="runtime_identity")
            if "application_service" in policy:
                _exec(container_id, ["python", "-B", "-c",
                    "from agri_research_agent.shared.runtime_context import establish_application_service_context; "
                    f"establish_application_service_context(service_id={contract['service_id']!r},module_id={contract['module_id']!r},runtime_root={identity_root!r})"],
                    label="application-service-context")
            probes["runtime_identity"] = "PASS"
            lifecycle_imports = {}
            for name in lifecycle_modules:
                module = "agri_research_agent.import_profit." + name
                _exec(container_id, _python_module_probe_argv(module), label="image-import:" + module)
                lifecycle_imports[name] = "PASS"
            _exec(container_id, _identity_probe_argv(contract, _sha(marker_raw), missing=True),
                  expect_success=False, label="missing_grant_rejected")
            probes["missing_grant_rejected"] = "PASS"
            for executable in contract["required_executables"]:
                _exec(container_id, ["python", "-B", "-c",
                      "import shutil,sys;sys.exit(shutil.which(sys.argv[1]) is None)", executable],
                      label="executable:" + executable)
            for module in contract["required_python_modules"]:
                _exec(container_id, _python_module_probe_argv(module),
                      label="python-module:" + module)
            probes["dependencies"] = "PASS"
            readonly_result = None
            for command in contract["initialization_commands"]:
                result = _exec(container_id, command["argv"], cwd=contract["working_directory"],
                               label="initialization:" + command["name"])
                if command["name"] == "spread-runtime-readonly-initialization":
                    readonly_result = _strict_json(result.stdout.strip(), "readonly initialization")
                    if readonly_result.get("status") != "PASS":
                        raise ValidationError("readonly initialization did not report PASS")
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
            final_container = inspect_one("container", container_id)
            host._validate_v3_runtime(contract, host.normalize_observation(
                final_container, image, _strict_json(release_raw, "RELEASE")), policy, container_id)
            if set(probes) != REQUIRED_PROBES or any(value != "PASS" for value in probes.values()):
                raise ValidationError("required probe set was not actually completed")
            evidence = {
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
                "spread_runtime_packaging": {
                    "workflow_run_id": os.environ.get("GITHUB_RUN_ID"),
                    "candidate_commit": binding["commit"], "candidate_tree": binding["tree"],
                    "image_id": image_id, "release_commit": binding["commit"],
                    "release_tree": binding["tree"], "oci_revision": binding["commit"],
                    "identity_kind": ("EPHEMERAL_CI_CANDIDATE_VALIDATION_ROOT"
                                      if ephemeral_candidate_trust else "FIXED_CANDIDATE_VALIDATION_ROOT"),
                    "role": "candidate_validation", "production_key_used": False,
                    "private_key_persisted": False if ephemeral_candidate_trust else None,
                    "private_key_visible_to_container": False,
                    "public_key_fingerprint": contract.get("_ephemeral_candidate_public_fingerprint"),
                    "grant_binding": {"commit": binding["commit"], "tree": binding["tree"],
                                      "image_id": image_id, "container_id": container_id,
                                      "runtime_id": "target-validation"},
                    "import_closure": packaging, "lifecycle_imports": lifecycle_imports,
                    "readonly_initialization": readonly_result,
                    "readonly_exit_code": 0 if readonly_result is not None else None,
                    "initialize_strict_page": "PASS" if readonly_result is not None else "NOT_EXECUTED",
                    "app_test": "PASS" if readonly_result is not None else "NOT_EXECUTED",
                    "probe_stages": probes,
                    **({"service_credential_mounts": service_mounts} if service_mounts is not None else {}),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            }
            if not lifecycle_modules:
                # The existing strict record contract makes this additional
                # modern packaging claim optional. Never invent PASS for
                # nonexistent modules or change the signed record schema.
                del evidence['spread_runtime_packaging']
            return evidence
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
    parser.add_argument("--ephemeral-candidate-trust", action="store_true")
    parser.add_argument("--existing-image-id")
    parser.add_argument("--application-source-root", type=Path)
    args = parser.parse_args(argv)
    if not _ID.fullmatch(args.project):
        parser.error("invalid project identity")
    output = Path(args.evidence_output)
    if not output.is_absolute():
        parser.error("evidence output must be absolute")
    args.evidence_output = output
    if args.existing_image_id is not None and not _IMAGE.fullmatch(args.existing_image_id):
        parser.error("existing image must be an exact immutable Image ID")
    if args.application_source_root is not None and args.existing_image_id is None:
        parser.error("independent application source requires existing-image validation")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    global _BUILD_INVOCATIONS, _BUILD_ALLOWED
    args = parse_args(argv)
    _BUILD_INVOCATIONS = 0
    _BUILD_ALLOWED = args.existing_image_id is None
    outcome = 'FAIL'
    try:
        root = repository_root()
        if args.existing_image_id is not None:
            # The executing tool and immutable application source are separate
            # protected identities. Never import a caller-provided probe file.
            pre = _load(root / "04_scripts/runtime/pre_release_runtime.py", "_existing_image_pre")
            host = _load(root / "09_deploy/runtime_identity/host_authorization.py", "_existing_image_host")
            engine = pre._load(pre.ENGINE, "_existing_image_engine")
            pre.require_source(host, engine, source_root=root)
            root = args.application_source_root or root
            pre.require_source(host, engine, source_root=root)
        project, contract, binding = source_contract(root, args.project, args.runtime_contract)
        try:
            builder_id = require_builder()
        except BuilderUnavailable:
            write_evidence(args.evidence_output, blocked_evidence(binding))
            outcome = 'BLOCKED'
            return 3
        evidence = validate_linux(root, project, contract, binding, builder_id,
                                  ephemeral_candidate_trust=args.ephemeral_candidate_trust,
                                  existing_image_id=args.existing_image_id)
        write_evidence(args.evidence_output, evidence)
        outcome = 'PASS'
        return 0
    except (ValidationError, OSError, KeyError, TypeError, ValueError) as exc:
        print(f"TARGET_RUNTIME_VALIDATION=FAIL: {exc}", file=sys.stderr)
        return 2
    finally:
        # Supplemental execution evidence, not a signed claim or authorization.
        # Never add these fields to the strict CandidateValidationRecord schema.
        write_evidence(args.evidence_output.with_name('image-validation-execution.json'), dict(
            mode='BUILD_AND_VALIDATE' if args.existing_image_id is None else 'VALIDATE_EXISTING_IMAGE',
            requested_image_id=args.existing_image_id, docker_build_invocations=_BUILD_INVOCATIONS,
            status=outcome))


if __name__ == "__main__":
    raise SystemExit(main())
