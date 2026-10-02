"""Pure external-boundary interpretation; no authority, Docker calls or writes.

Raw observations remain available to callers. Projections have explicit purposes;
none of them is an execution authorization or a replacement for signed bytes.
The strict decoder is extracted from the target-runtime engine.
"""
from __future__ import annotations

import json
import math
from pathlib import PurePosixPath
from typing import Mapping


class ObservationError(ValueError):
    pass


def strict_json_value(raw: bytes | str, label: str):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ObservationError(f"duplicate {label} JSON field")
            result[key] = value
        return result
    def constant(value):
        raise ObservationError(f"non-finite {label} JSON value: {value}")
    def floating(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ObservationError(f"non-finite {label} JSON value")
        return parsed
    try:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        return json.loads(text, object_pairs_hook=pairs, parse_constant=constant, parse_float=floating)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ObservationError(f"invalid {label} JSON: {exc}") from exc


def strict_object(raw: bytes | str, label: str) -> dict:
    value = strict_json_value(raw, label)
    if type(value) is not dict:
        raise ObservationError(f"{label} JSON must be an object")
    return value


def inspect_object(raw: bytes | str, label: str, *, expected_id: str | None = None) -> dict:
    value = strict_json_value(raw, label)
    if type(value) is not list or len(value) != 1 or type(value[0]) is not dict:
        raise ObservationError(f"{label} must return exactly one object")
    actual = value[0]
    if expected_id is not None and actual.get("Id") != expected_id:
        raise ObservationError(f"{label} ID mismatch")
    return actual


def absolute(value: str) -> str:
    if (not isinstance(value, str) or not value.startswith("/")
            or str(PurePosixPath(value)) != value or ".." in value.split("/") or "\\" in value):
        raise ObservationError("non-canonical container path")
    return value


def environment(values: list[str]) -> dict[str, str]:
    if type(values) is not list:
        raise ObservationError("invalid environment")
    result = {}
    for value in values:
        if not isinstance(value, str) or "=" not in value:
            raise ObservationError("invalid environment entry")
        key, content = value.split("=", 1)
        if not key or key in result:
            raise ObservationError("duplicate environment key")
        result[key] = content
    return result


def mount_projection(raw, *, purpose: str) -> list[dict]:
    """Authorization triples or legacy evidence, never interchangeable."""
    if type(raw) is not list:
        raise ObservationError("mount observations missing")
    if purpose not in {"authorization", "legacy-evidence"}:
        raise ObservationError("unknown mount projection purpose")
    result = []
    for item in raw:
        if type(item) is not dict or type(item.get("RW")) is not bool:
            raise ObservationError("mount fields are invalid")
        kind, source, target = item.get("Type"), item.get("Source"), item.get("Destination")
        if purpose == "authorization":
            if kind != "bind":
                raise ObservationError("only explicit bind mounts are supported by authorization v1")
            result.append(dict(source=absolute(source), target=absolute(target), read_only=not item["RW"]))
        else:
            if kind not in {"bind", "volume", "tmpfs", "npipe", "cluster"} or type(source) is not str or type(target) is not str:
                raise ObservationError("mount fields are invalid")
            result.append(dict(type=kind, source=source, destination=target, read_only=not item["RW"]))
    if purpose == "authorization":
        if len({m["target"] for m in result}) != len(result):
            raise ObservationError("duplicate mount target")
        return sorted(result, key=lambda m: m["target"])
    return sorted(result, key=lambda m: (m["type"], m["source"], m["destination"], m["read_only"]))


def compose_mounts(raw) -> list[dict]:
    """Resolved Compose triples, without granting host-source authority.

    Source stat/path/isolation is owned by the protected host policy validator;
    unlike actual Linux Docker Mounts, this can be inspected on Windows.
    """
    if type(raw) is not list:
        raise ObservationError("invalid Compose mount list")
    result = []
    for item in raw:
        if (type(item) is not dict or item.get("type") != "bind"
                or type(item.get("read_only", False)) is not bool
                or type(item.get("source")) is not str or not item["source"]):
            raise ObservationError("explicit bind mount and boolean permissions required")
        result.append(dict(source=item["source"], target=absolute(item.get("target")),
                           read_only=item.get("read_only", False)))
    if len({m["target"] for m in result}) != len(result):
        raise ObservationError("duplicate mount target")
    return sorted(result, key=lambda m: m["target"])


def compose_mount_targets(raw) -> dict[str, bool]:
    """Unresolved source Compose: target/access only, not source authorization."""
    if type(raw) is not list:
        raise ObservationError("invalid Compose mount list")
    result = {}
    for item in raw:
        if type(item) is not dict or item.get("type") not in {"bind", "volume"} or type(item.get("read_only", False)) is not bool:
            raise ObservationError("invalid declared Compose mount")
        target = absolute(item.get("target"))
        if target in result:
            raise ObservationError("duplicate Compose mount target")
        result[target] = item.get("read_only", False)
    return result


def config_payload(container: Mapping) -> dict:
    config, host = container.get("Config"), container.get("HostConfig")
    if type(config) is not dict or type(host) is not dict:
        raise ObservationError("Docker configuration missing")
    return dict(config=config, host_config=host, path=container.get("Path"), args=container.get("Args"))


def declared_secret_targets(manifest: Mapping, rendered: Mapping) -> dict[str, str]:
    service = rendered.get("services", {}).get(manifest["service_id"], {})
    refs, definitions = service.get("secrets") or [], rendered.get("secrets") or {}
    if (type(refs) is not list or type(definitions) is not dict
            or any(type(item) is not dict or set(item) != {"source", "target"} for item in refs)):
        raise ObservationError("invalid declared file-secret references")
    names, targets = [item["source"] for item in refs], [item["target"] for item in refs]
    import re
    if (any(type(name) is not str for name in names) or any(type(target) is not str for target in targets)
            or len(names) != len(set(names)) or set(names) != set(manifest["secret_references"])
            or set(definitions) != set(names) or len(targets) != len(set(targets))
            or any(not re.fullmatch(r"/run/secrets/[A-Za-z0-9][A-Za-z0-9._-]*", target) for target in targets)):
        raise ObservationError("secret declarations differ from runtime contract")
    if any(type(definitions[name]) is not dict or set(definitions[name]) - {"file", "name"}
           or type(definitions[name].get("file")) is not str
           or not definitions[name]["file"].strip() for name in names):
        raise ObservationError("only declared file secrets are supported")
    return {item["source"]: item["target"] for item in refs}


def resolve_mount_interpretation(manifest: Mapping, policy: Mapping, mounts: list[dict], secrets: list[dict]) -> dict:
    """Cross-check declarations, not substitute sources or establish host authority.

Source ownership/stat/isolation and sandbox projection remain host/recovery checks.
The policy supplies exact sources; manifest target/access cannot be overridden.
"""
    roots, required = manifest.get("runtime_roots"), manifest.get("required_mounts")
    if type(roots) is not list or type(required) is not list:
        raise ObservationError("runtime manifest mount contract missing")
    by_role, targets = {}, set()
    for item in roots:
        if type(item) is not dict or set(item) != {"role", "container_path", "access"} or item["role"] in by_role or item["access"] not in {"ro", "rw"}:
            raise ObservationError("invalid runtime root contract")
        by_role[item["role"]] = item
    resolved = []
    for item in required:
        if type(item) is not dict or set(item) != {"role", "container_path", "read_only"} or type(item["read_only"]) is not bool:
            raise ObservationError("invalid required mount contract")
        root = by_role.pop(item["role"], None)
        target = absolute(item["container_path"])
        if root is None or root["container_path"] != target or (root["access"] == "ro") != item["read_only"] or target in targets:
            raise ObservationError("runtime roots and required mounts disagree")
        targets.add(target)
        actual = [m for m in mounts if m["target"] == target]
        if len(actual) != 1 or actual[0]["read_only"] is not item["read_only"]:
            raise ObservationError("actual mount permission differs from runtime manifest")
        resolved.append(dict(role=item["role"], **actual[0]))
    allowed = targets | {policy["grant_container_directory"]} | {m["target"] for m in secrets}
    if by_role or any(m["target"] not in allowed for m in mounts):
        raise ObservationError("undeclared runtime mount")
    # If supplied, protected policy sources are exact, never replaced by Compose.
    if "mounts" in policy and sorted(mounts, key=lambda m: m["target"]) != sorted(policy["mounts"], key=lambda m: m["target"]):
        raise ObservationError("actual mounts differ from deployment contract")
    return dict(runtime=resolved, secrets=secrets, grant_target=policy["grant_container_directory"])


def readiness_projection(container: Mapping) -> dict:
    state, config = container.get("State"), container.get("Config")
    if type(state) is not dict or type(config) is not dict:
        raise ObservationError("readiness observation missing configuration/state")
    flags = {}
    for field in ("Running", "Dead", "Restarting"):
        if type(state.get(field)) is not bool:
            raise ObservationError("readiness state requires explicit booleans: " + field)
        flags[field] = state[field]
    status = state.get("Status")
    if type(status) is not str or status not in {"created", "running", "paused", "restarting", "removing", "exited", "dead"}:
        raise ObservationError("readiness state requires a known Docker status")
    # Docker's public inspect State does not expose the engine-internal removal
    # flag. Derive it from the explicit public status, never default to success.
    removing = state.get("RemovalInProgress", status == "removing")
    if type(removing) is not bool or (status == "removing" and removing is not True):
        raise ObservationError("readiness removal state is malformed or inconsistent")
    count = container.get("RestartCount")
    if type(count) is not int or count < 0:
        raise ObservationError("readiness restart count requires a nonnegative integer")
    return dict(exists=True, container_id=container.get("Id"),
        container_name=str(container.get("Name") or "").lstrip("/"), image_id=container.get("Image"),
        config_image=config.get("Image"), status=state.get("Status"), running=flags["Running"],
        dead=flags["Dead"], restarting=flags["Restarting"], removal_in_progress=removing,
        restart_count=count, created_at=container.get("Created"), started_at=state.get("StartedAt"),
        finished_at=state.get("FinishedAt"), exit_code=state.get("ExitCode"), error=state.get("Error"))
