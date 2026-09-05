"""Pure, versioned source-runtime manifest parsing.

This module validates a declarative source contract only.  It does not inspect
Git, construct containers, execute initialization commands, or authorize a
runtime instance.  Those observations belong to the independent candidate and
host authorization consumers.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import urlsplit


class ManifestValidationError(ValueError):
    """The input is not an exact supported runtime-manifest document."""


_V1 = "runtime-manifest/1"
_V2 = "runtime-manifest/2"
_V3 = "runtime-manifest/3"
_ENVIRONMENT_NAME = re.compile(r"[A-Z_][A-Z0-9_]*\Z")
_EXECUTION_GRANT_ROOT = "/run/market-data-grants"
_IDENTITY = re.compile(r"[a-z][a-z0-9-]*\Z")
_SOURCE_ROLES = frozenset({"entrypoint", "initialization", "runtime_configuration"})
_REQUIRED_PROBES = frozenset({
    "entrypoint_initialization", "runtime_identity", "dependencies", "runtime_paths",
    "mount_permissions", "missing_grant_rejected", "wrong_commit_rejected",
    "wrong_tree_rejected", "wrong_image_rejected", "wrong_service_rejected",
    "wrong_manifest_rejected", "preview_write_rejected", "release_mismatch_rejected",
})
_V1_FIELDS = frozenset({
    "schema_version", "project_id", "module_id", "service_id", "runtime_target",
    "identity_kind", "build", "entrypoint", "working_directory", "runtime_roots",
    "required_mounts", "required_environment", "secret_references",
    "required_executables", "required_python_modules", "production_policy",
    "preview_policy", "validation_probes",
})
_V2_FIELDS = _V1_FIELDS | {"identity_root_role", "initialization_commands", "source_inputs"}
_V3_FIELDS = _V2_FIELDS | {"environment_bindings", "forbidden_environment", "candidate_runtime_inputs"}


@dataclass(frozen=True, slots=True)
class RuntimeRoot:
    role: str
    container_path: str
    access: str


@dataclass(frozen=True, slots=True)
class RequiredMount:
    role: str
    container_path: str
    read_only: bool


@dataclass(frozen=True, slots=True)
class BuildInputs:
    dockerfile: str
    dockerignore: str
    dependency_contracts: tuple[str, ...]
    compose_sources: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class InitializationCommand:
    """A command to be executed with the manifest's global working directory.

    ``argv`` is deliberately an argv vector.  This source contract has no
    shell, command-specific working-directory, environment, timeout, or claim
    about command side effects.
    """

    name: str
    argv: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SourceInput:
    path: str
    role: str


@dataclass(frozen=True, slots=True)
class RuntimeManifest:
    schema_version: str
    project_id: str
    module_id: str
    service_id: str
    runtime_target: str
    identity_kind: str
    build: BuildInputs
    entrypoint: tuple[str, ...]
    working_directory: str
    runtime_roots: tuple[RuntimeRoot, ...]
    required_mounts: tuple[RequiredMount, ...]
    required_environment: tuple[str, ...]
    secret_references: tuple[str, ...]
    required_executables: tuple[str, ...]
    required_python_modules: tuple[str, ...]
    production_policy: Mapping[str, object]
    preview_policy: Mapping[str, object]
    validation_probes: tuple[str, ...]
    identity_root_role: str | None
    initialization_commands: tuple[InitializationCommand, ...]
    source_inputs: tuple[SourceInput, ...]
    environment_bindings: tuple[Mapping[str, str], ...] = ()
    forbidden_environment: tuple[str, ...] = ()
    candidate_runtime_inputs: tuple[Mapping[str, str], ...] = ()

    def to_dict(self) -> dict[str, object]:
        """Return a fresh JSON-shaped copy for consumers that need mappings."""
        result: dict[str, object] = {
            "schema_version": self.schema_version,
            "project_id": self.project_id,
            "module_id": self.module_id,
            "service_id": self.service_id,
            "runtime_target": self.runtime_target,
            "identity_kind": self.identity_kind,
            "build": {
                "dockerfile": self.build.dockerfile,
                "dockerignore": self.build.dockerignore,
                "dependency_contracts": list(self.build.dependency_contracts),
                "compose_sources": list(self.build.compose_sources),
            },
            "entrypoint": list(self.entrypoint),
            "working_directory": self.working_directory,
            "runtime_roots": [
                {"role": item.role, "container_path": item.container_path, "access": item.access}
                for item in self.runtime_roots
            ],
            "required_mounts": [
                {"role": item.role, "container_path": item.container_path, "read_only": item.read_only}
                for item in self.required_mounts
            ],
            "required_environment": list(self.required_environment),
            "secret_references": list(self.secret_references),
            "required_executables": list(self.required_executables),
            "required_python_modules": list(self.required_python_modules),
            "production_policy": dict(self.production_policy),
            "preview_policy": dict(self.preview_policy),
            "validation_probes": list(self.validation_probes),
        }
        if self.schema_version in (_V2, _V3):
            result.update({
                "identity_root_role": self.identity_root_role,
                "initialization_commands": [
                    {"name": item.name, "argv": list(item.argv)}
                    for item in self.initialization_commands
                ],
                "source_inputs": [
                    {"path": item.path, "role": item.role} for item in self.source_inputs
                ],
            })
        if self.schema_version == _V3:
            result.update({
                "environment_bindings": [dict(item) for item in self.environment_bindings],
                "forbidden_environment": list(self.forbidden_environment),
                "candidate_runtime_inputs": [dict(item) for item in self.candidate_runtime_inputs],
            })
        return result


def _fail(message: str) -> None:
    raise ManifestValidationError(message)


def _primitive_json(value: object) -> bool:
    """Reject custom mappings/sequences and non-finite values from direct callers."""
    if value is None or type(value) in (bool, int, str):
        return True
    if type(value) is float:
        return value == value and value not in (float("inf"), float("-inf"))
    if type(value) is list:
        return all(_primitive_json(item) for item in value)
    if type(value) is dict:
        return all(type(key) is str and _primitive_json(item) for key, item in value.items())
    return False


def _string(value: object, message: str, *, strict_controls: bool = False) -> str:
    if not isinstance(value, str) or not value or value.strip() != value or "\x00" in value:
        _fail(message)
    if strict_controls and any(ord(char) < 32 or ord(char) == 127 for char in value):
        _fail(message)
    return value


def _strings(value: object, message: str, *, nonempty: bool = False,
             unique: bool = True, strict_controls: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or (nonempty and not value):
        _fail(message)
    result = tuple(_string(item, message, strict_controls=strict_controls) for item in value)
    if unique and len(result) != len(set(result)):
        _fail(message)
    return result


def _container_path(value: object, message: str, *, strict_controls: bool = False) -> str:
    if (not isinstance(value, str) or not value.startswith("/") or value == "/"
            or "\\" in value or ":" in value
            or any(part in ("", ".", "..") for part in value[1:].split("/"))):
        _fail(message)
    if strict_controls and any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
        _fail(message)
    return value


def _source_path(value: object, message: str) -> str:
    path = _string(value, message, strict_controls=True)
    if ("\\" in path or ":" in path or any(char in path for char in "*?[]")
            or any(char.isspace() for char in path)
            or path.startswith("/") or any(part in ("", ".", "..") for part in path.split("/"))):
        _fail(message)
    for part in path.split("/"):
        if (part.endswith((".", " ")) or any(char in part for char in '<>"|')
                or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part)):
            _fail(message)
    return path


def _identity(value: object, message: str) -> str:
    if not isinstance(value, str) or not _IDENTITY.fullmatch(value):
        _fail(message)
    return value


def _parse_paths(raw: dict[str, object], *, strict_controls: bool) -> tuple[tuple[RuntimeRoot, ...], tuple[RequiredMount, ...]]:
    roots_raw, mounts_raw = raw["runtime_roots"], raw["required_mounts"]
    if not isinstance(roots_raw, list) or not isinstance(mounts_raw, list):
        _fail("runtime roots and mounts must be explicit lists")
    roots: list[RuntimeRoot] = []
    root_roles: set[str] = set()
    root_paths: set[str] = set()
    for item in roots_raw:
        if not isinstance(item, dict) or set(item) != {"role", "container_path", "access"}:
            _fail("invalid runtime root")
        role = _identity(item["role"], "invalid runtime path role")
        path = _container_path(item["container_path"], "invalid runtime path", strict_controls=strict_controls)
        if role in root_roles or path in root_paths or item["access"] not in ("ro", "rw"):
            _fail("duplicated or invalid runtime root")
        root_roles.add(role)
        root_paths.add(path)
        roots.append(RuntimeRoot(role, path, item["access"]))
    mounts: list[RequiredMount] = []
    mount_roles: set[str] = set()
    mount_paths: set[str] = set()
    for item in mounts_raw:
        if not isinstance(item, dict) or set(item) != {"role", "container_path", "read_only"}:
            _fail("invalid required mount")
        role = _identity(item["role"], "invalid runtime path role")
        path = _container_path(item["container_path"], "invalid runtime path", strict_controls=strict_controls)
        if role in mount_roles or path in mount_paths or type(item["read_only"]) is not bool:
            _fail("duplicated or invalid required mount")
        mount_roles.add(role)
        mount_paths.add(path)
        mounts.append(RequiredMount(role, path, item["read_only"]))
    by_role = {item.role: item for item in roots}
    if set(by_role) != mount_roles:
        _fail("runtime roots and mounts disagree")
    for mount in mounts:
        root = by_role[mount.role]
        if root.container_path != mount.container_path or (root.access == "ro") != mount.read_only:
            _fail("runtime roots and mounts disagree")
    return tuple(roots), tuple(mounts)


def _parse_build(value: object, *, strict_paths: bool) -> BuildInputs:
    if not isinstance(value, dict) or set(value) != {"dockerfile", "dockerignore", "dependency_contracts", "compose_sources"}:
        _fail("runtime manifest build inputs incomplete")
    if strict_paths:
        one = lambda item: _source_path(item, "invalid build source path")
        many = lambda item: tuple(_source_path(part, "invalid build source path") for part in item) if isinstance(item, list) else _fail("invalid build source paths")
    else:
        one = lambda item: item if isinstance(item, str) else _fail("invalid build source path")
        many = lambda item: tuple(item) if isinstance(item, list) and all(isinstance(part, str) for part in item) else _fail("invalid build source paths")
    dockerfile, dockerignore = one(value["dockerfile"]), one(value["dockerignore"])
    dependencies, compose = many(value["dependency_contracts"]), many(value["compose_sources"])
    if not dependencies or not compose or len(dependencies) != len(set(dependencies)) or len(compose) != len(set(compose)):
        _fail("runtime manifest build source lists must be nonempty and unique")
    if strict_paths:
        paths = (dockerfile, dockerignore, *dependencies, *compose)
        if len({path.casefold() for path in paths}) != len(paths):
            _fail("duplicate build input identity")
    return BuildInputs(dockerfile, dockerignore, dependencies, compose)


def _fixed_policy(raw: dict[str, object]) -> tuple[Mapping[str, object], Mapping[str, object]]:
    production = raw["production_policy"]
    preview = raw["preview_policy"]
    if (not isinstance(production, dict)
            or production != {"deployment_role": "production", "write_grant_required": True}
            or type(production.get("write_grant_required")) is not bool):
        _fail("production policy must require authorization")
    if preview != {"production_write": False, "production_rw_mounts": False} or not isinstance(preview, dict) or any(type(item) is not bool or item is not False for item in preview.values()):
        _fail("preview policy must prohibit production writes and mounts")
    return MappingProxyType(dict(production)), MappingProxyType(dict(preview))


def _parse_v2(raw: dict[str, object], manifest: RuntimeManifest) -> RuntimeManifest:
    # Re-parse inherited textual fields with v2 character tightening.
    project = _identity(raw["project_id"], "invalid project identity")
    entrypoint = _strings(raw["entrypoint"], "invalid runtime entrypoint", nonempty=True, unique=False, strict_controls=True)
    working_directory = _container_path(raw["working_directory"], "invalid runtime working directory", strict_controls=True)
    roots, mounts = _parse_paths(raw, strict_controls=True)
    build = _parse_build(raw["build"], strict_paths=True)
    text_fields = {
        name: _strings(raw[name], f"invalid runtime {name}", strict_controls=True)
        for name in ("required_environment", "secret_references", "required_executables", "required_python_modules")
    }
    role = _identity(raw["identity_root_role"], "invalid identity root role")
    root_by_role = {item.role: item for item in roots}
    identity_root = root_by_role.get(role)
    if identity_root is None or identity_root.access != "ro" or not roots:
        _fail("identity root must name a read-only runtime root")
    if any(item.access == "rw" and not item.container_path.startswith(identity_root.container_path + "/") for item in roots):
        _fail("writable runtime root is outside identity root")
    commands_raw = raw["initialization_commands"]
    if not isinstance(commands_raw, list) or not commands_raw:
        _fail("initialization commands must be a nonempty list")
    commands: list[InitializationCommand] = []
    names: set[str] = set()
    for item in commands_raw:
        if not isinstance(item, dict) or set(item) != {"name", "argv"}:
            _fail("invalid initialization command")
        name = _identity(item["name"], "invalid initialization command name")
        argv = _strings(item["argv"], "invalid initialization argv", nonempty=True, unique=False, strict_controls=True)
        if name in names:
            _fail("duplicate initialization command name")
        names.add(name)
        commands.append(InitializationCommand(name, argv))
    inputs_raw = raw["source_inputs"]
    if not isinstance(inputs_raw, list) or not inputs_raw:
        _fail("source inputs must be a nonempty list")
    inputs: list[SourceInput] = []
    input_paths: set[str] = set()
    roles: set[str] = set()
    build_paths = {part.casefold() for part in (build.dockerfile, build.dockerignore, *build.dependency_contracts, *build.compose_sources)}
    for item in inputs_raw:
        if not isinstance(item, dict) or set(item) != {"path", "role"}:
            _fail("invalid source input")
        path = _source_path(item["path"], "invalid source input path")
        source_role = item["role"]
        if not isinstance(source_role, str) or source_role not in _SOURCE_ROLES:
            _fail("invalid source input role")
        canonical = path.casefold()
        if canonical in input_paths or canonical in build_paths:
            _fail("duplicate or overlapping source input identity")
        input_paths.add(canonical)
        roles.add(source_role)
        inputs.append(SourceInput(path, source_role))
    if "entrypoint" not in roles:
        _fail("source inputs require an entrypoint source")
    return RuntimeManifest(
        _V2, project, manifest.module_id, manifest.service_id, manifest.runtime_target,
        manifest.identity_kind, build, entrypoint, working_directory, roots, mounts,
        text_fields["required_environment"], text_fields["secret_references"],
        text_fields["required_executables"], text_fields["required_python_modules"],
        manifest.production_policy, manifest.preview_policy, manifest.validation_probes,
        role, tuple(commands), tuple(inputs),
    )


def _environment_name(value: object) -> str:
    if not isinstance(value, str) or not _ENVIRONMENT_NAME.fullmatch(value):
        _fail("invalid environment variable name")
    return value


def _relative_runtime_path(value: object, *, allow_root: bool = False) -> str:
    if value == "" and allow_root:
        return ""
    path = _source_path(value, "invalid relative runtime input path")
    if any(part.casefold() in {".git", ".market-data-runtime.json"} for part in path.split("/")):
        _fail("runtime input overlaps identity material")
    return path


def _deployment_value(value: object, value_type: object) -> str:
    value = _string(value, "invalid candidate environment value", strict_controls=True)
    if len(value) > 2048:
        _fail("candidate environment value is too long")
    if value_type == "nonempty":
        if not re.fullmatch(r"[A-Za-z0-9_./:+-]+", value):
            _fail("candidate deployment token is invalid")
        return value
    if value_type not in ("http_url", "https_url"):
        _fail("unsupported deployment environment value type")
    try:
        parsed = urlsplit(value)
        port = parsed.port
        valid = (parsed.scheme in ({"http", "https"} if value_type == "http_url" else {"https"})
                 and parsed.hostname and parsed.username is None and parsed.password is None
                 and not parsed.query and not parsed.fragment and not any(char.isspace() for char in value)
                 and "\\" not in value and (port is None or 0 < port < 65536))
    except ValueError:
        valid = False
    if not valid:
        _fail("invalid candidate deployment URL")
    return value


def _parse_v3(raw: dict[str, object], base: RuntimeManifest) -> RuntimeManifest:
    """Validate declarations only; consumers must still prove bytes and instances."""
    roots = {item.role: item for item in base.runtime_roots}
    identity = roots[base.identity_root_role].container_path
    # Unlike v2, candidate seeding uses one explicit identity-root hierarchy.
    if any(item.container_path != identity and not item.container_path.startswith(identity + "/")
           for item in base.runtime_roots):
        _fail("v3 runtime roots must be inside identity root")
    root_paths = [item.container_path.casefold() for item in base.runtime_roots]
    if len(root_paths) != len(set(root_paths)):
        _fail("runtime root has a case alias")
    if any(path == _EXECUTION_GRANT_ROOT or path.startswith(_EXECUTION_GRANT_ROOT + "/")
           or _EXECUTION_GRANT_ROOT.startswith(path + "/") for path in root_paths):
        _fail("runtime root overlaps reserved execution grant namespace")
    required = {_environment_name(name) for name in base.required_environment}
    forbidden = _strings(raw["forbidden_environment"], "invalid forbidden environment")
    for name in forbidden:
        _environment_name(name)
    if required.intersection(forbidden):
        _fail("required and forbidden environment overlap")
    bindings = raw["environment_bindings"]
    if not isinstance(bindings, list):
        _fail("environment bindings must be an explicit list")
    names: set[str] = set()
    parsed_bindings = []
    shapes = {
        "literal": {"name", "kind", "value"},
        "runtime_path": {"name", "kind", "role", "relative_path"},
        "deployment": {"name", "kind", "value_type", "candidate_value"},
        "execution_grant": {"name", "kind"},
    }
    for item in bindings:
        if not isinstance(item, dict) or not isinstance(item.get("kind"), str):
            _fail("invalid environment binding")
        kind = item["kind"]
        if kind not in shapes or set(item) != shapes[kind]:
            _fail("environment binding fields incomplete or unknown")
        name = _environment_name(item["name"])
        if (name == "MARKET_DATA_EXECUTION_GRANT") != (kind == "execution_grant"):
            _fail("execution grant environment must use reserved binding")
        if name in names:
            _fail("duplicate environment binding")
        names.add(name)
        if kind == "literal":
            _string(item["value"], "invalid literal environment value", strict_controls=True)
            if len(item["value"]) > 2048:
                _fail("literal environment value is too long")
        elif kind == "runtime_path":
            role = _identity(item["role"], "invalid environment runtime role")
            if role not in roots:
                _fail("environment references unknown runtime role")
            _relative_runtime_path(item["relative_path"], allow_root=True)
        elif kind == "deployment":
            _deployment_value(item["candidate_value"], item["value_type"])
        parsed_bindings.append(MappingProxyType(dict(item)))
    if names != required:
        _fail("environment bindings must cover exactly required environment")
    if "MARKET_DATA_EXECUTION_GRANT" not in names:
        _fail("execution grant environment binding is required")
    seeds = raw["candidate_runtime_inputs"]
    if not isinstance(seeds, list):
        _fail("candidate runtime inputs must be an explicit list")
    build = base.build
    image_inputs = {path.casefold() for path in (
        build.dockerfile, build.dockerignore, *build.dependency_contracts,
        *build.compose_sources, *(item.path for item in base.source_inputs))}
    sources: set[str] = set()
    targets: set[str] = set()
    parsed_seeds = []
    for item in seeds:
        if not isinstance(item, dict) or set(item) != {"source_path", "sha256", "role", "relative_path"}:
            _fail("invalid candidate runtime input")
        source = _source_path(item["source_path"], "invalid candidate input source")
        if not source.startswith("08_tests/fixtures/") or source == "08_tests/fixtures/":
            _fail("candidate input must be an exact test fixture file")
        if source.casefold() in sources | image_inputs:
            _fail("candidate input duplicates source or image input")
        sources.add(source.casefold())
        if not isinstance(item["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]):
            _fail("invalid candidate input SHA256")
        role = _identity(item["role"], "invalid candidate input role")
        if role not in roots or role == base.identity_root_role or roots[role].access != "ro":
            _fail("candidate input must name a read-only child runtime role")
        relative = _relative_runtime_path(item["relative_path"])
        target = (roots[role].container_path + "/" + relative).casefold()
        if any(target == other or target.startswith(other + "/") or other.startswith(target + "/")
               for other in targets):
            _fail("candidate input targets overlap")
        for other in base.runtime_roots:
            other_path = other.container_path.casefold()
            if other.role != role and (other_path == target or other_path.startswith(target + "/")
                    or (target.startswith(other_path + "/")
                        and other_path.startswith(roots[role].container_path.casefold() + "/"))):
                _fail("candidate input crosses another runtime mount")
        targets.add(target)
        parsed_seeds.append(MappingProxyType(dict(item)))
    return replace(base, schema_version=_V3, environment_bindings=tuple(parsed_bindings),
                   forbidden_environment=forbidden, candidate_runtime_inputs=tuple(parsed_seeds))


def parse_runtime_manifest(raw: object) -> RuntimeManifest:
    """Parse a primitive JSON object into an immutable manifest.

    No filesystem, Git, container, command execution, or authorization action
    occurs here.  In particular, a successfully parsed command is not proof
    that it initializes an application.
    """
    try:
        primitive = type(raw) is dict and _primitive_json(raw)
    except RecursionError:
        _fail("runtime manifest is recursive or too deeply nested")
    if not primitive:
        _fail("runtime manifest must be a primitive JSON object")
    version = raw.get("schema_version")
    if version not in (_V1, _V2, _V3):
        _fail("unsupported runtime manifest schema")
    expected = _V1_FIELDS if version == _V1 else (_V2_FIELDS if version == _V2 else _V3_FIELDS)
    if set(raw) != expected:
        _fail("runtime manifest fields incomplete or unknown")
    if raw["runtime_target"] != "production_container" or raw["identity_kind"] != "oci_container":
        _fail("runtime manifest target or identity kind is invalid")
    # Project identity is intentionally only typed here for v1.  A Registry
    # consumer supplies and compares its expected project_id separately.
    project = raw["project_id"] if isinstance(raw["project_id"], str) else _fail("invalid project identity")
    module = _identity(raw["module_id"], "invalid runtime module identity")
    service = _identity(raw["service_id"], "invalid runtime service identity")
    entrypoint = _strings(raw["entrypoint"], "invalid runtime entrypoint", nonempty=True, unique=False)
    working_directory = _container_path(raw["working_directory"], "invalid runtime working directory")
    roots, mounts = _parse_paths(raw, strict_controls=False)
    build = _parse_build(raw["build"], strict_paths=False)
    text_fields = {
        name: _strings(raw[name], f"invalid runtime {name}")
        for name in ("required_environment", "secret_references", "required_executables", "required_python_modules")
    }
    probes = _strings(raw["validation_probes"], "invalid validation probes", nonempty=True)
    if set(probes) != _REQUIRED_PROBES:
        _fail("runtime validation probes incomplete or unknown")
    production, preview = _fixed_policy(raw)
    manifest = RuntimeManifest(
        version, project, module, service, raw["runtime_target"], raw["identity_kind"], build,
        entrypoint, working_directory, roots, mounts, text_fields["required_environment"],
        text_fields["secret_references"], text_fields["required_executables"],
        text_fields["required_python_modules"], production, preview, probes, None, (), (),
    )
    if version == _V1:
        return manifest
    parsed = _parse_v2(raw, manifest)
    return _parse_v3(raw, parsed) if version == _V3 else parsed


def _reject_constant(value: str) -> object:
    raise ManifestValidationError(f"non-finite JSON number is not allowed: {value}")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ManifestValidationError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def load_runtime_manifest(path: Path) -> RuntimeManifest:
    """Strictly load and parse a manifest file without performing runtime checks."""
    try:
        content = Path(path).read_text(encoding="utf-8")
        raw: Any = json.loads(content, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ManifestValidationError("runtime manifest is not valid UTF-8 JSON") from exc
    return parse_runtime_manifest(raw)
