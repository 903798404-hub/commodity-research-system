"""Fail-closed authorization for writes into a marked runtime root."""

from __future__ import annotations

import json
import hashlib
import os
import re
import stat
import tempfile
from dataclasses import InitVar, dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from .file_identity import identify_file
from .production_identity import (
    GitExecutionRequest,
    OCIExecutionRequest,
    ProductionIdentityError,
    VerifiedExecutionIdentity,
    verify_execution,
)


MARKER_FILENAME = ".market-data-runtime.json"
APPLICATION_SERVICE_CREDENTIAL_PATH = Path("/run/secrets/market-data-service.json")
_SERVICE_ID = re.compile(r"^[a-z][a-z0-9-]*$")
_DEPLOYMENT_ID = re.compile(r"^[0-9a-f]{32}$")
_CONTAINER_ID = re.compile(r"^[0-9a-f]{64}$")


class RuntimeMode(StrEnum):
    FIXTURE = "FIXTURE"
    ISOLATED_DEV = "ISOLATED_DEV"
    FORMAL_READONLY = "FORMAL_READONLY"
    PRODUCTION_WRITE = "PRODUCTION_WRITE"
    CANDIDATE_VALIDATION = "CANDIDATE_VALIDATION"


class RuntimeClassification(StrEnum):
    FIXTURE = "fixture"
    ISOLATED_DEV = "isolated-dev"
    FORMAL = "formal"
    CANDIDATE_VALIDATION = "candidate-validation"


class RuntimeAuthorizationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RuntimeMarker:
    schema_version: int
    runtime_id: str
    classification: RuntimeClassification
    module_id: str
    created_at: datetime

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise RuntimeAuthorizationError("unsupported runtime marker schema_version")
        if not self.runtime_id.strip() or not self.module_id.strip():
            raise RuntimeAuthorizationError("runtime_id and module_id must be non-empty")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise RuntimeAuthorizationError("runtime marker created_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class RuntimeIdentity:
    runtime_id: str
    classification: RuntimeClassification
    module_id: str
    marker_sha256: str
    resolved_root: Path


def load_runtime_identity(runtime_root: str | Path) -> RuntimeIdentity:
    root = Path(runtime_root).resolve(strict=True)
    marker_path = root / MARKER_FILENAME
    if marker_path.is_symlink():
        raise RuntimeAuthorizationError("runtime marker must not be a symbolic link")
    if not marker_path.is_file():
        raise RuntimeAuthorizationError(f"runtime marker is missing: {marker_path}")
    try:
        payload = json.loads(marker_path.read_text(encoding="utf-8"))
        if set(payload) != {"schema_version", "runtime_id", "classification", "module_id", "created_at"}:
            raise ValueError("unexpected runtime marker fields")
        marker = RuntimeMarker(
            schema_version=payload["schema_version"],
            runtime_id=payload["runtime_id"],
            classification=RuntimeClassification(payload["classification"]),
            module_id=payload["module_id"],
            created_at=datetime.fromisoformat(payload["created_at"].replace("Z", "+00:00")),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeAuthorizationError(f"invalid runtime marker: {marker_path}") from exc
    marker_identity = identify_file(marker_path)
    return RuntimeIdentity(
        runtime_id=marker.runtime_id,
        classification=marker.classification,
        module_id=marker.module_id,
        marker_sha256=marker_identity.sha256,
        resolved_root=root,
    )


@dataclass(frozen=True, slots=True)
class RuntimeContext:
    mode: RuntimeMode
    module_id: str
    runtime_root: Path
    candidate_root: Path | None = None
    formal_identity: RuntimeIdentity | None = None
    expected_runtime_id: InitVar[str | None] = None
    repository_root: InitVar[Path | None] = None
    execution_request: GitExecutionRequest | OCIExecutionRequest | None = None
    identity: RuntimeIdentity = field(init=False)
    write_allowed: bool = field(init=False)

    def __post_init__(self, expected_runtime_id: str | None, repository_root: Path | None) -> None:
        if not isinstance(self.mode, RuntimeMode):
            raise RuntimeAuthorizationError("mode must be a RuntimeMode")
        if not self.module_id.strip():
            raise RuntimeAuthorizationError("module_id must be non-empty")
        identity = load_runtime_identity(self.runtime_root)
        if identity.module_id != self.module_id:
            raise RuntimeAuthorizationError("runtime marker module_id mismatch")
        object.__setattr__(self, "runtime_root", identity.resolved_root)
        object.__setattr__(self, "identity", identity)

        if self.candidate_root is not None:
            candidate = Path(self.candidate_root).resolve(strict=False)
            _require_within(candidate, identity.resolved_root, "candidate_root")
            object.__setattr__(self, "candidate_root", candidate)

        expected_classification = {
            RuntimeMode.FIXTURE: RuntimeClassification.FIXTURE,
            RuntimeMode.ISOLATED_DEV: RuntimeClassification.ISOLATED_DEV,
            RuntimeMode.FORMAL_READONLY: RuntimeClassification.FORMAL,
            RuntimeMode.PRODUCTION_WRITE: RuntimeClassification.FORMAL,
            RuntimeMode.CANDIDATE_VALIDATION: RuntimeClassification.CANDIDATE_VALIDATION,
        }[self.mode]
        if identity.classification is not expected_classification:
            raise RuntimeAuthorizationError("runtime marker classification mismatch")

        if self.mode is RuntimeMode.FIXTURE:
            temporary_root = Path(tempfile.gettempdir()).resolve(strict=True)
            _require_within(identity.resolved_root, temporary_root, "fixture runtime_root")

        if self.mode in {RuntimeMode.FORMAL_READONLY, RuntimeMode.PRODUCTION_WRITE}:
            if self.formal_identity is None or self.formal_identity != identity:
                raise RuntimeAuthorizationError("formal runtime identity mismatch")
        elif self.formal_identity is not None:
            raise RuntimeAuthorizationError("formal_identity is only valid for formal runtime modes")

        if self.mode in {RuntimeMode.PRODUCTION_WRITE, RuntimeMode.CANDIDATE_VALIDATION}:
            if expected_runtime_id is None or expected_runtime_id != identity.runtime_id:
                raise RuntimeAuthorizationError("production expected runtime_id mismatch")
            if repository_root is not None:
                # Legacy callers cannot gain authority from a marker or .git directory.
                # Keep the linked-worktree diagnostic without selecting an identity type.
                git_entry = Path(repository_root).resolve(strict=True) / ".git"
                if git_entry.is_file():
                    raise RuntimeAuthorizationError("production writes are forbidden from a linked worktree")
                raise RuntimeAuthorizationError("repository_root alone cannot authorize execution; use an explicit execution_request")
            self._verify_execution()
        elif expected_runtime_id is not None or repository_root is not None or self.execution_request is not None:
            raise RuntimeAuthorizationError("production-only validation inputs were supplied to another mode")

        object.__setattr__(self, "write_allowed", self.mode is not RuntimeMode.FORMAL_READONLY)

    def _verify_execution(self) -> VerifiedExecutionIdentity:
        if self.execution_request is None:
            raise RuntimeAuthorizationError("explicit execution_request is required")
        role = "production" if self.mode is RuntimeMode.PRODUCTION_WRITE else "candidate_validation"
        try:
            return verify_execution(
                self.execution_request,
                expected_role=role,
                module_id=self.module_id,
                runtime_id=self.identity.runtime_id,
                runtime_root=self.runtime_root,
                marker_sha256=self.identity.marker_sha256,
            )
        except (ProductionIdentityError, OSError, ValueError) as exc:
            raise RuntimeAuthorizationError(f"execution identity rejected: {exc}") from exc


def _require_within(candidate: Path, root: Path, label: str) -> None:
    if candidate == root or root in candidate.parents:
        return
    raise RuntimeAuthorizationError(f"{label} is outside runtime_root")


class ApplicationServiceContext:
    """Process-local authority established from a root-injected service secret.

    The constructor is intentionally unavailable to business callers.  This is
    an application-process capability, not a serialized or permanent token.
    """

    __slots__ = ("mode", "module_id", "runtime_root", "identity", "service_id",
                 "deployment_id", "writable_roots", "_credential_sha256", "_pid")

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise RuntimeAuthorizationError("ApplicationServiceContext requires credential validation")

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("ApplicationServiceContext is immutable")

    def __reduce__(self) -> object:
        raise TypeError("ApplicationServiceContext cannot be serialized")


def _service_credential_bytes(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise RuntimeAuthorizationError("application service credential is missing or unsafe")
    if os.name == "posix":
        details = path.stat()
        if details.st_uid != 0 or stat.S_IMODE(details.st_mode) & 0o007 or not stat.S_IMODE(details.st_mode) & 0o044:
            raise RuntimeAuthorizationError("application service credential permissions are unsafe")
        # A file bind-mounted from a root-controlled directory is read-only to
        # the non-root service.  A writable local file cannot be authority.
        from .production_identity import _mount_for, _mount_options
        if not hasattr(os, "geteuid") or os.geteuid() == 0 or "ro" not in _mount_for(_mount_options(), path):
            raise RuntimeAuthorizationError("application service credential is not a non-root read-only mount")
    before = path.stat()
    raw = path.read_bytes()
    after = path.stat()
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if any(getattr(before, field) != getattr(after, field) for field in fields):
        raise RuntimeAuthorizationError("application service credential changed during validation")
    return raw


def establish_application_service_context(
    *, service_id: str, module_id: str, runtime_root: str | Path,
) -> ApplicationServiceContext:
    """Validate the current deployment secret once in this service process.

    The launcher alone creates the secret after exact-image/container validation.
    Its file identity and runtime marker are checked again on each write.
    """
    if not _SERVICE_ID.fullmatch(service_id) or not _SERVICE_ID.fullmatch(module_id):
        raise RuntimeAuthorizationError("invalid application service identity")
    identity = load_runtime_identity(runtime_root)
    if identity.classification not in {RuntimeClassification.FORMAL, RuntimeClassification.CANDIDATE_VALIDATION} or identity.module_id != module_id:
        raise RuntimeAuthorizationError("application service runtime identity mismatch")
    path = APPLICATION_SERVICE_CREDENTIAL_PATH
    try:
        raw = _service_credential_bytes(path)
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise RuntimeAuthorizationError("application service credential is invalid") from exc
    required = {"schema_version", "role", "service_id", "module_id", "runtime_id", "runtime_marker_sha256",
                "container_id",
                "deployment_id", "credential", "allowed_writable_roots"}
    if (type(payload) is not dict or set(payload) != required
            or payload["schema_version"] != "application-service-credential/1"
            or payload["role"] != ("production" if identity.classification is RuntimeClassification.FORMAL else "candidate_validation")
            or payload["service_id"] != service_id or payload["module_id"] != module_id
            or payload["runtime_id"] != identity.runtime_id
            or payload["runtime_marker_sha256"] != identity.marker_sha256
            or not isinstance(payload["container_id"], str)
            or not _CONTAINER_ID.fullmatch(payload["container_id"])
            or not isinstance(payload["deployment_id"], str)
            or not _DEPLOYMENT_ID.fullmatch(payload["deployment_id"])
            or not isinstance(payload["credential"], str)
            or re.fullmatch(r"[0-9a-f]{64}", payload["credential"]) is None
            or not isinstance(payload["allowed_writable_roots"], list)
            or not payload["allowed_writable_roots"]):
        raise RuntimeAuthorizationError("application service credential identity mismatch")
    roots: list[Path] = []
    for value in payload["allowed_writable_roots"]:
        if not isinstance(value, str) or not Path(value).is_absolute() or ".." in Path(value).parts:
            raise RuntimeAuthorizationError("application service root is invalid")
        root = Path(value).resolve(strict=True)
        _require_within(root, identity.resolved_root, "application service root")
        if root == identity.resolved_root or root == identity.resolved_root / MARKER_FILENAME:
            raise RuntimeAuthorizationError("application service root is too broad")
        roots.append(root)
    if len(set(roots)) != len(roots):
        raise RuntimeAuthorizationError("duplicate application service root")
    context = object.__new__(ApplicationServiceContext)
    mode = (RuntimeMode.PRODUCTION_WRITE if identity.classification is RuntimeClassification.FORMAL
            else RuntimeMode.CANDIDATE_VALIDATION)
    values = dict(mode=mode, module_id=module_id,
                  runtime_root=identity.resolved_root, identity=identity,
                  service_id=service_id, deployment_id=payload["deployment_id"],
                  writable_roots=tuple(roots),
                  _credential_sha256=hashlib.sha256(raw).hexdigest(), _pid=os.getpid())
    for name, value in values.items():
        object.__setattr__(context, name, value)
    return context


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate credential field")
        result[key] = value
    return result


def assert_runtime_write(context: RuntimeContext | ApplicationServiceContext | None, target: str | Path) -> Path:
    if context is None:
        raise RuntimeAuthorizationError("RuntimeContext is required for runtime writes")
    if isinstance(context, ApplicationServiceContext):
        if context._pid != os.getpid():
            raise RuntimeAuthorizationError("application service process changed")
        if load_runtime_identity(context.runtime_root) != context.identity:
            raise RuntimeAuthorizationError("runtime identity changed after authorization")
        if hashlib.sha256(_service_credential_bytes(APPLICATION_SERVICE_CREDENTIAL_PATH)).hexdigest() != context._credential_sha256:
            raise RuntimeAuthorizationError("application service deployment credential changed")
        destination = Path(target).resolve(strict=False)
        if destination == context.runtime_root / MARKER_FILENAME or not any(
            destination == root or root in destination.parents for root in context.writable_roots
        ):
            raise RuntimeAuthorizationError("write target is outside authorized writable roots")
        return destination
    if not isinstance(context, RuntimeContext):
        raise RuntimeAuthorizationError("unknown runtime write identity")
    if not context.write_allowed:
        raise RuntimeAuthorizationError("runtime mode is read-only")
    if load_runtime_identity(context.runtime_root) != context.identity:
        raise RuntimeAuthorizationError("runtime identity changed after authorization")
    destination = Path(target).resolve(strict=False)
    _require_within(destination, context.runtime_root, "write target")
    if destination == context.runtime_root / MARKER_FILENAME:
        raise RuntimeAuthorizationError("runtime marker cannot be changed through business writes")
    if context.mode in {RuntimeMode.PRODUCTION_WRITE, RuntimeMode.CANDIDATE_VALIDATION}:
        execution = context._verify_execution()
        if not any(destination == root or root in destination.parents for root in execution.writable_roots):
            raise RuntimeAuthorizationError("write target is outside authorized writable roots")
    return destination
