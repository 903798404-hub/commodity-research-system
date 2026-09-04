from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shlex
from types import SimpleNamespace

import pytest
import yaml

from agri_research_agent.shared.runtime_context import RuntimeClassification, RuntimeMode
from agri_research_agent.shared.runtime_manifest import parse_runtime_manifest


ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "04_scripts/runtime/public_intraday_runtime.py"
MANIFEST = ROOT / "02_configs/runtime_contracts/public-intraday-runtime.json"
COMPOSE = ROOT / "09_deploy/public_intraday_runtime/compose.yml"
DOCKERFILE = ROOT / "09_deploy/public_intraday_runtime/Dockerfile.public-intraday"


def load_wrapper():
    spec = importlib.util.spec_from_file_location("public_intraday_runtime_v2", WRAPPER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_manifest_and_deployment_contract_are_isolated_and_non_root():
    raw = json.loads(MANIFEST.read_text(encoding="utf-8"))
    manifest = parse_runtime_manifest(raw)
    assert (manifest.project_id, manifest.module_id, manifest.service_id) == (
        "public-intraday-runtime", "shared-intraday", "public-intraday-runtime")
    assert manifest.identity_kind == "oci_container"
    assert manifest.identity_root_role == "identity"
    assert {item.role: item.access for item in manifest.runtime_roots} == {
        "identity": "ro", "inputs": "ro", "snapshots": "rw"}
    assert manifest.secret_references == ("tankan-reader",)
    bound_sources = {item.path for item in manifest.source_inputs}
    for required in (
        "04_scripts/runtime/public_intraday_runtime.py",
        "03_src/agri_research_agent/data_sources/tankan/client.py",
        "03_src/agri_research_agent/data_sources/tankan/intraday_adapter.py",
        "03_src/agri_research_agent/pipelines/public_intraday.py",
        "03_src/agri_research_agent/shared/runtime_context.py",
        "03_src/agri_research_agent/shared/production_identity.py",
    ):
        assert required in bound_sources
    assert manifest.preview_policy == {
        "production_write": False, "production_rw_mounts": False}

    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    service = compose["services"]["public-intraday-runtime"]
    assert service["read_only"] is True and service["user"] == "65532:65532"
    assert service["cap_drop"] == ["ALL"]
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert service["environment"] == {
        "MARKET_DATA_EXECUTION_GRANT": "/run/market-data-grants/grant.json"}
    assert service["secrets"] == [
        {"source": "tankan-reader", "target": "tankan.env"}]
    assert compose["secrets"]["tankan-reader"] == {
        "file": "${TANKAN_SECRET_FILE:?TANKAN_SECRET_FILE is required}"}
    assert {item["target"]: item.get("read_only", False) for item in service["volumes"]} == {
        "/runtime": True,
        "/runtime/inputs": True,
        "/runtime/snapshots": False,
        "/run/market-data-grants": True,
    }
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    assert "USER 65532:65532" in dockerfile
    assert "COPY .git" not in dockerfile
    assert ".git" in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    logical_lines = dockerfile.replace("\\\n", " ").splitlines()
    copied = set()
    for line in logical_lines:
        if line.startswith("COPY "):
            parts = shlex.split(line)
            copied.update(parts[1:-1])
    expected_copied = bound_sources | {
        "requirements.txt",
        "02_configs/runtime_manifest.schema.json",
        "02_configs/runtime_contracts/public-intraday-runtime.json",
        "03_src/agri_research_agent/shared/runtime_manifest.py",
    }
    assert copied == expected_copied


def test_cli_has_no_implicit_business_date_or_capture_action():
    wrapper = load_wrapper()
    with pytest.raises(SystemExit):
        wrapper.parser().parse_args(["capture", "--session", "AM"])
    parsed = wrapper.parser().parse_args([
        "capture", "--business-date", "2026-09-05", "--session", "PM",
        "--request-file", "/runtime/inputs/request.json",
        "--calendar-file", "/runtime/inputs/calendar.json",
    ])
    assert parsed.business_date.isoformat() == "2026-09-05" and parsed.session == "PM"
    assert wrapper.parser().parse_args(["readonly-preflight"]).action == "readonly-preflight"
    deployment_text = MANIFEST.read_text(encoding="utf-8") + COMPOSE.read_text(encoding="utf-8")
    assert "2026-" not in deployment_text
    assert "ALLOW_CNF_SAVE" not in deployment_text


def test_runtime_context_selects_explicit_oci_identity_without_git_fallback(monkeypatch):
    wrapper = load_wrapper()
    calls = []
    identity = SimpleNamespace(
        classification=RuntimeClassification.CANDIDATE_VALIDATION,
        runtime_id="candidate-runtime",
        marker_sha256="a" * 64,
    )
    monkeypatch.setattr(wrapper, "load_runtime_identity", lambda _: identity)
    monkeypatch.setattr(wrapper, "RuntimeContext", lambda *args, **kwargs: calls.append((args, kwargs)) or "context")
    assert wrapper._runtime_context(write=False) == "context"
    args, kwargs = calls.pop()
    assert args[:3] == (RuntimeMode.CANDIDATE_VALIDATION, "shared-intraday", wrapper.RUNTIME_ROOT)
    assert "repository_root" not in kwargs
    request = kwargs["execution_request"]
    assert request.grant_path == Path("/run/market-data-grants/grant.json")
    assert request.release_path == wrapper.SOURCE_ROOT / "RELEASE.json"
    assert request.runtime_manifest_path == wrapper.MANIFEST_PATH

    identity.classification = RuntimeClassification.FORMAL
    assert wrapper._runtime_context(write=True) == "context"
    args, kwargs = calls.pop()
    assert args[0] is RuntimeMode.PRODUCTION_WRITE
    assert kwargs["formal_identity"] is identity
    assert "repository_root" not in kwargs

    verification = []
    monkeypatch.setattr(wrapper, "verify_execution", lambda request, **kwargs: verification.append((request, kwargs)))
    assert wrapper._runtime_context(write=False) == "context"
    args, kwargs = calls.pop()
    assert args[0] is RuntimeMode.FORMAL_READONLY
    assert verification[0][1]["expected_role"] == "production"
    assert verification[0][1]["marker_sha256"] == "a" * 64


def test_readonly_preflight_does_not_read_secret_capture_or_write(monkeypatch, tmp_path):
    wrapper = load_wrapper()
    runtime = tmp_path / "runtime"
    for name in ("inputs", "snapshots"):
        runtime.joinpath(name).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(wrapper, "RUNTIME_ROOT", runtime)
    monkeypatch.setattr(wrapper, "INPUT_ROOT", runtime / "inputs")
    monkeypatch.setattr(wrapper, "SNAPSHOT_ROOT", runtime / "snapshots")
    context = SimpleNamespace(
        mode=RuntimeMode.CANDIDATE_VALIDATION,
        identity=SimpleNamespace(runtime_id="candidate-runtime"),
        module_id="shared-intraday",
    )
    monkeypatch.setattr(wrapper, "_runtime_context", lambda *, write: context if write is False else pytest.fail("write context"))
    monkeypatch.setattr(wrapper.TankanConnectionSettings, "from_secret_file", lambda *_: pytest.fail("secret read"))
    monkeypatch.setattr(wrapper, "capture_public_intraday", lambda **_: pytest.fail("capture"))
    result = wrapper.readonly_preflight()
    assert result["status"] == "PASS"
    assert result["secret_accessed"] is False
    assert result["capture_executed"] is False
    assert set(runtime.rglob("*")) == {runtime / "inputs", runtime / "snapshots"}


def test_capture_inputs_are_confined_to_readonly_input_mount(monkeypatch, tmp_path):
    wrapper = load_wrapper()
    input_root = tmp_path / "inputs"
    input_root.mkdir()
    accepted = input_root / "request.json"
    accepted.write_text("{}", encoding="utf-8")
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(wrapper, "INPUT_ROOT", input_root)
    assert wrapper._runtime_input(accepted, "request file") == accepted.resolve()
    with pytest.raises(ValueError, match="outside the runtime input root"):
        wrapper._runtime_input(outside, "request file")


def test_contract_contains_no_automatic_activation_or_business_integration():
    operational = "\n".join(path.read_text(encoding="utf-8") for path in (
        WRAPPER, MANIFEST, COMPOSE, DOCKERFILE))
    documentation = (ROOT / "07_docs/projects/public-intraday-runtime/运行合同.md").read_text(
        encoding="utf-8")
    assert "AM_PM_AUTO_EXECUTION=NO" in documentation
    assert "INTRADAY_FULL_DAILY_DEPENDENCY=NONE" in documentation
    for forbidden in ("schedule:", "cron", "run_full_daily", "notification-push"):
        assert forbidden not in operational.lower()
