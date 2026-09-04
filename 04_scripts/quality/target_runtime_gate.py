"""Completion consumer for versioned target-runtime evidence, never a PASS importer.

The trusted candidate's engine must execute static and actual container checks.
No command-line option accepts caller-supplied evidence. Engine absence is BLOCKED.
The engine implementation belongs to the independent runtime infrastructure project.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

try:
    from . import project_registry as registry
except ImportError:
    import project_registry as registry

ENGINE = "04_scripts/runtime/validate_target_runtime.py"
MANIFEST_PARSER = "03_src/agri_research_agent/shared/runtime_manifest.py"
MANIFEST_SCHEMA = "02_configs/runtime_manifest.schema.json"
VALIDATOR_VERSION = "target-runtime-validator/1"
EVIDENCE_SCHEMA = "target-runtime-evidence/1"
REQUIRED_PROBES = frozenset({"entrypoint_initialization", "runtime_identity", "dependencies",
                           "runtime_paths", "mount_permissions", "missing_grant_rejected",
                           "wrong_commit_rejected", "wrong_tree_rejected", "wrong_image_rejected",
                           "wrong_service_rejected", "wrong_manifest_rejected",
                           "preview_write_rejected", "release_mismatch_rejected"})


class RuntimeValidationBlocked(ValueError):
    """A required validator or builder is unavailable; never equivalent to PASS."""


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def exact_file(root: Path, value: str) -> Path:
    registry.relative_path(value)
    path = registry.future_file(root, value, exact_file=False)
    if not path.is_file():
        raise ValueError(f"Runtime contract input missing: {value}")
    if path.relative_to(root.resolve()).as_posix() != value:
        raise ValueError(f"Runtime contract path case alias: {value}")
    return path


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate runtime contract JSON key")
        result[key] = value
    return result


def _evidence_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate target runtime evidence JSON key")
        result[key] = value
    return result


def _reject_evidence_constant(value):
    raise ValueError(f"Non-finite target runtime evidence JSON value: {value}")


def _read_evidence(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"),
                           object_pairs_hook=_evidence_object,
                           parse_constant=_reject_evidence_constant)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Invalid target runtime evidence JSON") from exc
    if type(value) is not dict:
        raise ValueError("Target runtime evidence JSON must be an object")
    return value


def _read_v2(root: Path, project: dict, path: Path) -> dict:
    # Resolve from this gate's checkout, never an installed package, PYTHONPATH,
    # or a caller-provided module object. The candidate binds these files below.
    source_root = Path(__file__).resolve().parents[2]
    parser_path = exact_file(source_root, MANIFEST_PARSER)
    name = "_market_data_runtime_manifest_contract"
    spec = importlib.util.spec_from_file_location(name, parser_path)
    if spec is None or spec.loader is None:
        raise ValueError("Runtime manifest parser unavailable")
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(name)
    sys.modules[name] = module
    try:
        # Never execute ignored __pycache__ bytecode as validator authority.
        exec(compile(parser_path.read_bytes(), str(parser_path), "exec"), module.__dict__)
        contract = module.load_runtime_manifest(path).to_dict()
    finally:
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous
    if contract['schema_version'] != 'runtime-manifest/2' or contract['project_id'] != project['project_id']:
        raise ValueError("Runtime contract schema or project identity mismatch")
    build = contract['build']
    names = [project['runtime_contract'], build['dockerfile'], build['dockerignore'],
             *build['dependency_contracts'], *build['compose_sources'],
             *(item['path'] for item in contract['source_inputs'])]
    if len({registry.canonical_path(name) for name in names}) != len(names):
        raise ValueError("Runtime input paths contain duplicate identities or aliases")
    for name in names:
        exact_file(root, name)
    return contract


def read_contract(root: Path, project: dict) -> dict:
    value = project.get("runtime_contract")
    if not isinstance(value, str):
        raise ValueError("production_container requires runtime_contract")
    path = exact_file(root, value)
    try:
        contract = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Invalid runtime contract JSON") from exc
    if isinstance(contract, dict) and contract.get('schema_version') == 'runtime-manifest/2':
        return _read_v2(root, project, path)
    if (not isinstance(contract, dict)
            or contract.get("schema_version") != "runtime-manifest/1"
            or contract.get("runtime_target") != "production_container"
            or contract.get("identity_kind") != "oci_container"
            or contract.get("project_id") != project["project_id"]):
        raise ValueError("Runtime contract schema or project identity mismatch")
    required = {"schema_version", "project_id", "module_id", "service_id", "runtime_target",
                "identity_kind", "build", "entrypoint", "working_directory", "runtime_roots",
                "required_mounts", "required_environment", "secret_references", "required_executables",
                "required_python_modules", "production_policy", "preview_policy", "validation_probes"}
    if set(contract) != required:
        raise ValueError("Runtime contract fields incomplete or unknown")
    def strings(value, *, nonempty=False, unique=True):
        return (isinstance(value, list) and (bool(value) or not nonempty)
                and all(isinstance(v, str) and v.strip() == v and v and '\x00' not in v for v in value)
                and (not unique or len(value) == len(set(value))))
    def container_path(value):
        return (isinstance(value, str) and value.startswith('/') and value != '/'
                and '\\' not in value and ':' not in value
                and all(p not in ('', '.', '..') for p in value[1:].split('/')))
    if any(not isinstance(contract[k], str) or not re.fullmatch(r"[a-z][a-z0-9-]*", contract[k])
           for k in ('module_id', 'service_id')):
        raise ValueError("Runtime module/service identity invalid")
    if not strings(contract['entrypoint'], nonempty=True, unique=False) or not container_path(contract['working_directory']):
        raise ValueError("Runtime entrypoint/working directory invalid")
    for key in ('required_environment', 'secret_references', 'required_executables', 'required_python_modules'):
        if not strings(contract[key]):
            raise ValueError(f"Invalid runtime {key}")
    if not strings(contract['validation_probes'], nonempty=True) or set(contract['validation_probes']) != REQUIRED_PROBES:
        raise ValueError("Runtime validation probes incomplete or unknown")
    if (contract['production_policy'] != {'deployment_role': 'production', 'write_grant_required': True}
            or contract['production_policy']['write_grant_required'] is not True):
        raise ValueError("Runtime production policy must require authorization")
    if (contract['preview_policy'] != {'production_write': False, 'production_rw_mounts': False}
            or any(v is not False for v in contract['preview_policy'].values())):
        raise ValueError("Runtime preview policy must prohibit production writes/mounts")
    for key, fields in [('runtime_roots', {'role', 'container_path', 'access'}),
                        ('required_mounts', {'role', 'container_path', 'read_only'})]:
        records = contract[key]
        if not isinstance(records, list):
            raise ValueError(f"Runtime {key} must be an explicit list")
        roles, paths = set(), set()
        for record in records:
            if not isinstance(record, dict) or set(record) != fields:
                raise ValueError(f"Invalid runtime {key} fields")
            if not isinstance(record['role'], str) or not re.fullmatch(r'[a-z][a-z0-9-]*', record['role']):
                raise ValueError("Runtime path role invalid")
            if not container_path(record['container_path']) or record['role'] in roles or record['container_path'] in paths:
                raise ValueError("Runtime path invalid or duplicated")
            roles.add(record['role']); paths.add(record['container_path'])
            if key == 'runtime_roots' and record['access'] not in ('ro', 'rw'):
                raise ValueError("Runtime root access invalid")
            if key == 'required_mounts' and type(record['read_only']) is not bool:
                raise ValueError("Runtime mount permission invalid")
    roots = {r['role']: r for r in contract['runtime_roots']}
    mounts = {m['role']: m for m in contract['required_mounts']}
    if set(roots) != set(mounts) or any(roots[r]['container_path'] != mounts[r]['container_path']
            or (roots[r]['access'] == 'ro') != mounts[r]['read_only'] for r in roots):
        raise ValueError("Runtime roots and mounts disagree")
    build = contract.get("build")
    if not isinstance(build, dict) or set(build) != {"dockerfile", "dockerignore", "dependency_contracts", "compose_sources"}:
        raise ValueError("Runtime contract build inputs incomplete")
    for key in ("dependency_contracts", "compose_sources"):
        values = build[key]
        if not isinstance(values, list) or not values or not all(isinstance(v, str) for v in values) or len(values) != len(set(values)):
            raise ValueError(f"Runtime contract {key} must be nonempty unique paths")
    inputs = [value, build["dockerfile"], build["dockerignore"], *build["dependency_contracts"], *build["compose_sources"]]
    if len({registry.canonical_path(name) for name in inputs}) != len(inputs):
        raise ValueError("Runtime input paths contain duplicate identities or aliases")
    for name in inputs:
        exact_file(root, name)
    return contract


def candidate_binding(root: Path, project: dict) -> dict:
    contract = read_contract(root, project)
    if registry.git(root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ValueError("Container validation requires a clean committed candidate")
    build = contract["build"]
    paths = [project["runtime_contract"], build["dockerfile"], build["dockerignore"],
             *build["dependency_contracts"], *build["compose_sources"], ENGINE]
    if contract['schema_version'] == 'runtime-manifest/2':
        paths.extend(item['path'] for item in contract['source_inputs'])
        paths.extend([MANIFEST_PARSER, MANIFEST_SCHEMA])
        if len({registry.canonical_path(name) for name in paths}) != len(paths):
            raise ValueError("Runtime source and validator inputs overlap")
    hashes = {}
    for name in paths:
        path = exact_file(root, name)
        registry.git(root, "ls-files", "--error-unmatch", "--", name)
        # Clean status ignores ignored files: compare each input with its Git blob too.
        raw = subprocess.check_output(["git", "-C", str(root), "show", f"HEAD:{name}"])
        if path.read_bytes() != raw:
            # Accept checkout line-ending conversion only when Git hashes the same blob.
            if registry.git(root, "hash-object", "--path", name, str(path)) != registry.git(root, "rev-parse", f"HEAD:{name}"):
                raise ValueError(f"Runtime input differs from committed source: {name}")
        # Hash canonical Git bytes, not platform-specific checkout CRLF conversion.
        hashes[name] = hashlib.sha256(raw).hexdigest()
    return {"project_id": project["project_id"], "commit": registry.git(root, "rev-parse", "HEAD"),
            "tree": registry.git(root, "rev-parse", "HEAD^{tree}"), "source_sha256": hashes,
            "validator_version": VALIDATOR_VERSION}


def validate_evidence(evidence: dict, binding: dict, returncode: int) -> dict:
    if not isinstance(evidence, dict) or evidence.get("schema_version") != EVIDENCE_SCHEMA:
        raise ValueError("Missing or invalid target runtime evidence")
    if evidence.get("binding") != binding:
        raise ValueError("Target runtime evidence candidate binding mismatch")
    static = evidence.get("TARGET_RUNTIME_STATIC_VALIDATION")
    container = evidence.get("TARGET_RUNTIME_CONTAINER_VALIDATION")
    if static != "PASS":
        raise ValueError("TARGET_RUNTIME_STATIC_VALIDATION did not PASS")
    if container == "BLOCKED" and returncode == 3 and evidence.get("blocked_reason") == "LINUX_BUILDER_UNAVAILABLE":
        raise RuntimeValidationBlocked("TARGET_RUNTIME_CONTAINER_VALIDATION=BLOCKED: Linux builder unavailable")
    if container != "PASS" or returncode != 0:
        raise ValueError("TARGET_RUNTIME_CONTAINER_VALIDATION did not PASS")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(evidence.get("image_id", ""))):
        raise ValueError("Target runtime Image ID missing or invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", str(evidence.get("rendered_compose_sha256", ""))):
        raise ValueError("Actual rendered Compose identity missing or invalid")
    builder = evidence.get('builder')
    if (not isinstance(builder, dict) or set(builder) != {'builder_id', 'os', 'execution'}
            or builder['os'] != 'linux' or builder['execution'] != 'isolated'
            or not isinstance(builder['builder_id'], str) or not builder['builder_id'].strip()):
        raise ValueError("Isolated Linux builder identity missing")
    observed = evidence.get('observed_identity')
    if (not isinstance(observed, dict) or observed != {
            'image_id': evidence['image_id'], 'oci_revision': binding['commit'],
            'git_tree': binding['tree'], 'source_sha256': binding['source_sha256'],
            'rendered_compose_sha256': evidence['rendered_compose_sha256'],
            'authorization_role': 'candidate_validation', 'git_metadata_present': False,
            'production_volumes_mounted': False}):
        raise ValueError("Observed container identity disagrees with candidate")
    if evidence.get('probes') != {name: 'PASS' for name in REQUIRED_PROBES}:
        raise ValueError("Required container validation probes did not all PASS")
    return evidence


def execute_engine(root: Path, project: dict, output: Path) -> int:
    # An absolute entrypoint and isolated interpreter prevent caller PYTHONPATH hooks.
    command = [sys.executable, "-I", "-B", str(root / ENGINE), "--project", project["project_id"],
               "--runtime-contract", project["runtime_contract"], "--evidence-output", str(output)]
    env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME"}}
    try:
        return subprocess.run(command, cwd=root, env=env, timeout=3600, check=False).returncode
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Target runtime validator timed out; validation not complete") from exc


def validate_target(root: Path, project: dict) -> dict:
    if project.get("runtime_target") != "production_container":
        return {"TARGET_RUNTIME_VALIDATION": "NOT_REQUIRED"}
    read_contract(root, project)
    if not (root / ENGINE).is_file():
        raise RuntimeValidationBlocked("TARGET_RUNTIME_CONTAINER_VALIDATION=BLOCKED: target runtime engine unavailable")
    before = candidate_binding(root, project)
    with tempfile.TemporaryDirectory(prefix="target-runtime-evidence-") as folder:
        output = Path(folder) / "evidence.json"
        code = execute_engine(root, project, output)
        if not output.is_file():
            raise ValueError("Target runtime validator produced no machine evidence")
        evidence = _read_evidence(output)
    if before != candidate_binding(root, project):
        raise ValueError("Candidate changed during target runtime validation")
    return validate_evidence(evidence, before, code)
