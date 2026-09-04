"""Fail-closed authorization for writes into a marked runtime root."""

from __future__ import annotations

import json
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


def assert_runtime_write(context: RuntimeContext | None, target: str | Path) -> Path:
    if context is None:
        raise RuntimeAuthorizationError("RuntimeContext is required for runtime writes")
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
