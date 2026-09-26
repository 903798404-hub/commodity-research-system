"""The long-running service capability is distinct from the legacy grant path."""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
import json
from pathlib import Path

import pytest

from agri_research_agent.market_data import intraday as market
from agri_research_agent.shared import runtime_context as runtime
from agri_research_agent.shared.production_identity import (
    AuthorizationRole, GitExecutionRequest, IdentityKind, VerifiedExecutionIdentity,
)


def service_fixture(tmp_path: Path, monkeypatch):
    root = tmp_path / "runtime"
    root.mkdir()
    (root / "snapshots").mkdir()
    (root / "other").mkdir()
    marker = root / runtime.MARKER_FILENAME
    marker.write_text(json.dumps({"schema_version": 1, "runtime_id": "runtime-one",
        "classification": "formal", "module_id": "shared-intraday",
        "created_at": "2026-08-31T00:00:00+00:00"}), encoding="utf-8")
    identity = runtime.load_runtime_identity(root)
    secret = tmp_path / "service.json"
    monkeypatch.setattr(runtime, "APPLICATION_SERVICE_CREDENTIAL_PATH", secret)
    # Only the mount observation is OS-specific; the credential contract and
    # application write checks remain real on Windows and Linux CI.
    def test_secret_bytes(path):
        if path.is_symlink() or not path.is_file():
            raise runtime.RuntimeAuthorizationError("application service credential is missing or unsafe")
        return path.read_bytes()
    monkeypatch.setattr(runtime, "_service_credential_bytes", test_secret_bytes)
    payload = {"schema_version": "application-service-credential/1",
        "role": "production",
        "service_id": "spread-dashboard", "module_id": "shared-intraday",
        "runtime_id": identity.runtime_id,
        "runtime_marker_sha256": identity.marker_sha256,
        "container_id": "e" * 64,
        "deployment_id": "a" * 32, "credential": "b" * 64,
        "allowed_writable_roots": [str(root / "snapshots")]}
    secret.write_text(json.dumps(payload), encoding="utf-8")
    return root, secret, payload


def establish(root: Path):
    return runtime.establish_application_service_context(
        service_id="spread-dashboard", module_id="shared-intraday", runtime_root=root)


def test_application_context_requires_current_credential_and_is_not_constructible(tmp_path, monkeypatch):
    root, secret, payload = service_fixture(tmp_path, monkeypatch)
    with pytest.raises(runtime.RuntimeAuthorizationError, match="requires credential validation"):
        runtime.ApplicationServiceContext()
    context = establish(root)
    assert context.service_id == "spread-dashboard"
    assert runtime.assert_runtime_write(context, root / "snapshots" / "result.json") == root / "snapshots" / "result.json"
    with pytest.raises(runtime.RuntimeAuthorizationError, match="authorized writable roots"):
        runtime.assert_runtime_write(context, root / "other" / "public-current.json")
    with pytest.raises(runtime.RuntimeAuthorizationError, match="authorized writable roots"):
        runtime.assert_runtime_write(context, root / ".." / "public-current.json")
    with pytest.raises(runtime.RuntimeAuthorizationError, match="authorized writable roots"):
        runtime.assert_runtime_write(context, root / runtime.MARKER_FILENAME)
    secret.unlink()
    with pytest.raises(runtime.RuntimeAuthorizationError, match="missing or unsafe"):
        runtime.assert_runtime_write(context, root / "snapshots")
    with pytest.raises(runtime.RuntimeAuthorizationError, match="missing or unsafe"):
        establish(root)
    secret.write_text(json.dumps(payload), encoding="utf-8")
    payload["credential"] = "c" * 64
    secret.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(runtime.RuntimeAuthorizationError, match="changed"):
        runtime.assert_runtime_write(context, root / "snapshots")


@pytest.mark.parametrize("field,value", [
    ("service_id", "other-service"), ("runtime_id", "other-runtime"),
    ("module_id", "other-module"), ("deployment_id", "bad"),
    ("credential", "too-short"), ("runtime_marker_sha256", "0" * 64),
    ("container_id", "old"),
    ("role", "candidate_validation"),
])
def test_wrong_service_deployment_runtime_or_credential_rejected(tmp_path, monkeypatch, field, value):
    root, secret, payload = service_fixture(tmp_path, monkeypatch)
    payload[field] = value
    secret.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(runtime.RuntimeAuthorizationError):
        establish(root)


def test_old_deployment_context_cannot_survive_rotation_or_fork(tmp_path, monkeypatch):
    root, secret, payload = service_fixture(tmp_path, monkeypatch)
    context = establish(root)
    payload["deployment_id"] = "c" * 32
    payload["credential"] = "d" * 64
    secret.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(runtime.RuntimeAuthorizationError, match="changed"):
        runtime.assert_runtime_write(context, root / "snapshots")
    new_context = establish(root)
    monkeypatch.setattr(runtime.os, "getpid", lambda: new_context._pid + 1)
    with pytest.raises(runtime.RuntimeAuthorizationError, match="process changed"):
        runtime.assert_runtime_write(new_context, root / "snapshots")


def _snapshot():
    day = date(2026, 8, 31)
    at = datetime(2026, 8, 31, 7, 30, tzinfo=timezone.utc)
    quote = market.IntradayQuote(day, market.MarketSession.AM, at,
        "DCE:SOYMEAL:2027-01", "M2701", "DCE", "SOYMEAL", 3000,
        "LAST", "CNY", "CNY_PER_METRIC_TONNE", "TANKAN", "market.futures_live",
        at, None, market.FreshnessStatus.FRESH, {"query": {"sha": "fixture"}}, at)
    return market.IntradaySnapshot(day, market.MarketSession.AM, at, (quote,),
        "fixture-calendar", "fixture-calendar-sha", "FORMAL")


def test_machine_and_application_seal_have_identical_immutable_contract(tmp_path, monkeypatch):
    root, _, _ = service_fixture(tmp_path, monkeypatch)
    application = establish(root)
    identity = runtime.load_runtime_identity(root)
    # Identity *validation* is covered by the existing Machine Grant suite;
    # this isolates the shared immutable writer's contract under both contexts.
    verified = VerifiedExecutionIdentity(IdentityKind.GIT_WORKTREE,
        AuthorizationRole.PRODUCTION, "fixture", "shared-intraday", "git-worktree",
        identity.runtime_id, "a" * 40, "b" * 40, None, None, (root / "snapshots",))
    monkeypatch.setattr(runtime, "verify_execution", lambda *args, **kwargs: verified)
    machine = runtime.RuntimeContext(runtime.RuntimeMode.PRODUCTION_WRITE,
        "shared-intraday", root, formal_identity=identity,
        expected_runtime_id=identity.runtime_id,
        execution_request=GitExecutionRequest(tmp_path, "a" * 40, "b" * 40))
    value = _snapshot()
    first = market.seal_intraday_snapshot(root / "snapshots", value, context=application)
    before = {p.name: p.read_bytes() for p in first.release_dir.iterdir()}
    second = market.seal_intraday_snapshot(root / "snapshots", value, context=machine)
    assert first.status is market.SealStatus.SEALED
    assert second.status is market.SealStatus.NO_CHANGE
    assert before == {p.name: p.read_bytes() for p in second.release_dir.iterdir()}
    assert market.load_intraday_snapshot(root / "snapshots", value.business_date,
        value.session, expected_environment="FORMAL").content_sha256 == value.content_sha256
    with pytest.raises(market.IntradaySnapshotConflictError):
        market.seal_intraday_snapshot(root / "snapshots",
            replace(value, quotes=(replace(value.quotes[0], price=3001),)), context=application)
    assert before == {p.name: p.read_bytes() for p in first.release_dir.iterdir()}
