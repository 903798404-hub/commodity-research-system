"""Wiring checks; real Docker identity validation remains a separate mandatory gate."""
from __future__ import annotations

from datetime import date
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from agri_research_agent.shared.production_identity import GitExecutionRequest, OCIExecutionRequest
from agri_research_agent.shared.runtime_context import RuntimeMode
from agri_research_agent.shared.runtime_manifest import parse_runtime_manifest

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "02_configs/runtime_contracts/spread-production-runtime.json"


def load_script(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def contract():
    return parse_runtime_manifest(json.loads(MANIFEST.read_text(encoding="utf-8"))).to_dict()


def fixture_bytes(path):
    # These controlled UTF-8 JSON fixtures use LF Git blobs on every host.
    return path.read_text(encoding="utf-8").encode("utf-8") if path.suffix == ".json" else path.read_bytes()


def test_compose_binds_full_app_and_separates_consumer_from_capture():
    m = contract()
    c = yaml.safe_load((ROOT / m["build"]["compose_sources"][0]).read_text(encoding="utf-8"))
    assert set(c["services"]) == {"spread-dashboard"}
    service = c["services"]["spread-dashboard"]
    assert service["entrypoint"] == m["entrypoint"]
    assert m["entrypoint"][:3] == ["streamlit", "run", "05_apps/streamlit_app.py"]
    assert service["read_only"] and service["user"] == "65532:65532"
    assert set(service["environment"]) == set(m["required_environment"])
    assert service["environment"]["IMPORT_PROFIT_INTRADAY_PAGE_MODE"] == "STRICT_RUNTIME"
    assert service["environment"]["IMPORT_PROFIT_INTRADAY_ENVIRONMENT"] == "FORMAL"
    assert not set(m["forbidden_environment"]) & set(service["environment"])
    mounts = {x["target"]: x for x in service["volumes"]}
    consumer, capture = mounts["/runtime/import-profit/snapshots"], mounts["/runtime/capture-snapshots"]
    assert consumer["read_only"] and not capture["read_only"]
    assert consumer["source"] == capture["source"]
    assert mounts["/run/market-data-grants"]["read_only"]
    assert service["secrets"] == [{"source": "tankan-reader", "target": "/run/secrets/tankan.env"}]
    assert service["environment"]["TMPDIR"] == "/runtime/10_logs"


def test_image_copies_exact_code_inputs_and_excludes_candidate_fixtures():
    m = contract()
    text = (ROOT / m["build"]["dockerfile"]).read_text(encoding="utf-8")
    copied = set()
    for line in text.splitlines():
        if line.startswith("COPY ["):
            copied.update(json.loads(line[5:])[:-1])
        elif line.startswith("COPY "):
            copied.update(line.split()[1:-1])
    expected = {x["path"] for x in m["source_inputs"]} | {
        "requirements.txt", "03_src/agri_research_agent/shared/runtime_manifest.py",
        "02_configs/runtime_manifest.schema.json", MANIFEST.relative_to(ROOT).as_posix()}
    assert copied == expected
    assert not copied & {x["source_path"] for x in m["candidate_runtime_inputs"]}
    assert "USER 65532:65532" in text
    assert ".git" in (ROOT / ".dockerignore").read_text().splitlines()
    assert '"application":"spread-production-runtime-wiring"' in text


@pytest.fixture
def mounted_inputs(tmp_path):
    m = contract()
    roles = {}
    for item in m["runtime_roots"]:
        path = tmp_path / item["container_path"].removeprefix("/runtime").lstrip("/")
        path.mkdir(parents=True, exist_ok=True)
        roles[item["role"]] = path
    for item in m["candidate_runtime_inputs"]:
        source = ROOT / item["source_path"]
        raw = fixture_bytes(source)
        assert hashlib.sha256(raw).hexdigest() == item["sha256"]
        target = roles[item["role"]] / item["relative_path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    assert len(m["candidate_runtime_inputs"]) == 13
    return roles


def test_exact_seed_files_initialize_real_consumers_and_strict_page(mounted_inputs, monkeypatch):
    from agri_research_agent.application.domestic_spreads import load_domestic_spread_database
    from agri_research_agent.market_data.activated_runtime import resolve_domestic_spread_path
    from agri_research_agent.import_profit.runtime_store import load_runtime_release_dataset
    from agri_research_agent.market_data.intraday import MarketSession, load_intraday_snapshot
    paths = mounted_inputs
    monkeypatch.setenv("PUBLIC_DATA_SERVER_STORE_ROOT", str(paths["data"] / "public-data-server-store"))
    assert not load_domestic_spread_database(resolve_domestic_spread_path(paths["data"])).empty
    load_runtime_release_dataset(paths["history"])
    snapshot = load_intraday_snapshot(paths["snapshots"], date(2026, 8, 31), MarketSession.PM,
                                     expected_environment="TEST_ISOLATED_NON_PRODUCTION")
    assert snapshot.quotes
    preflight = load_script("spread_preflight_test", "04_scripts/runtime/spread_runtime_preflight.py")
    args = SimpleNamespace(soybean_runtime_root=paths["history"], result_root=paths["results"])
    before = {p: p.read_bytes() for key in ("history", "snapshots", "cnf")
              for p in paths[key].rglob("*") if p.is_file()}
    preflight.initialize_strict_page(args, paths["snapshots"], paths["cnf"] / "historical_cnf_cache.parquet")
    assert all(p.read_bytes() == raw for p, raw in before.items())
    assert not any(paths["capture-snapshots"].iterdir())


def test_preflight_rejects_caller_path_overrides():
    preflight = load_script("spread_preflight_arguments", "04_scripts/runtime/spread_runtime_preflight.py")
    with pytest.raises(SystemExit):
        preflight.parser().parse_args(["--identity-kind", "oci_container", "--soybean-runtime-root", "/tmp/other"])


def test_formal_empty_snapshot_store_initializes_strict_page(mounted_inputs):
    preflight = load_script("spread_preflight_empty", "04_scripts/runtime/spread_runtime_preflight.py")
    paths = mounted_inputs
    empty = paths["capture-snapshots"]
    assert preflight.load_formal_preflight_snapshot(empty) is None
    args = SimpleNamespace(soybean_runtime_root=paths["history"], result_root=paths["results"])
    preflight.initialize_strict_page(args, empty, paths["cnf"] / "historical_cnf_cache.parquet")
    assert not any(empty.iterdir())


def test_formal_pm_snapshot_does_not_require_am(mounted_inputs):
    from dataclasses import replace
    from agri_research_agent.market_data.intraday import (
        MarketSession, _manifest, canonical_json, load_intraday_snapshot,
    )
    root = mounted_inputs["snapshots"]
    snapshot = load_intraday_snapshot(root, date(2026, 8, 31), MarketSession.PM,
                                     expected_environment="TEST_ISOLATED_NON_PRODUCTION")
    # A local synthetic unit-test object, never a production data allocation.
    formal = replace(snapshot, environment="FORMAL")
    (root / "releases" / snapshot.release_id / "manifest.json").write_bytes(canonical_json(_manifest(formal)))
    preflight = load_script("spread_preflight_pm", "04_scripts/runtime/spread_runtime_preflight.py")
    assert preflight.load_formal_preflight_snapshot(root).release_id == snapshot.release_id


@pytest.mark.parametrize("invalid", ["missing_root", "invalid_releases", "wrong_environment", "incomplete_release"])
def test_formal_snapshot_preflight_rejects_invalid_store(mounted_inputs, invalid):
    from agri_research_agent.market_data.intraday import IntradaySnapshotError
    root = mounted_inputs["snapshots"]
    if invalid == "missing_root":
        root = root / "missing"
    elif invalid == "invalid_releases":
        root = mounted_inputs["capture-snapshots"]
        (root / "releases").write_text("invalid", encoding="utf-8")
    elif invalid == "incomplete_release":
        (root / "releases" / "2026-08-31-PM" / "quotes.json").unlink()
    preflight = load_script("spread_preflight_invalid", "04_scripts/runtime/spread_runtime_preflight.py")
    with pytest.raises((ValueError, IntradaySnapshotError)):
        preflight.load_formal_preflight_snapshot(root)


def identity_args(cli, monkeypatch, kind):
    identity = SimpleNamespace(runtime_id="test-runtime", marker_sha256="a" * 64)
    monkeypatch.setattr(cli, "load_runtime_identity", lambda _: identity)
    return SimpleNamespace(runtime_root=Path("/runtime"), expected_runtime_id=identity.runtime_id,
                           expected_marker_sha256=identity.marker_sha256, identity_kind=kind,
                           approved_commit=None, approved_tree=None)


def test_cli_git_request_requires_explicit_approved_commit_and_tree(monkeypatch):
    cli = load_script("spread_cli_git", "04_scripts/capture_public_intraday.py")
    args = identity_args(cli, monkeypatch, "git_worktree")
    with pytest.raises(ValueError, match="approved commit and tree"):
        cli.initialize_execution_identity(args, mode=RuntimeMode.PRODUCTION_WRITE)
    args.approved_commit, args.approved_tree = "b" * 40, "c" * 40
    calls = []
    monkeypatch.setattr(cli, "RuntimeContext", lambda *a, **kw: calls.append(kw) or "context")
    assert cli.initialize_execution_identity(args, mode=RuntimeMode.PRODUCTION_WRITE) == "context"
    assert isinstance(calls[0]["execution_request"], GitExecutionRequest)
    assert "repository_root" not in calls[0]


def test_cli_oci_readonly_verifies_grant_and_never_falls_back(monkeypatch, tmp_path):
    cli = load_script("spread_cli_oci", "04_scripts/capture_public_intraday.py")
    args = identity_args(cli, monkeypatch, "oci_container")
    args.runtime_root = tmp_path
    monkeypatch.setattr(cli, "OCI_RUNTIME_ROOT", tmp_path)
    calls = []
    def reject(request, **kwargs):
        assert isinstance(request, OCIExecutionRequest)
        calls.append("verified")
        raise ValueError("invalid grant")
    monkeypatch.setattr(cli, "verify_execution", reject)
    monkeypatch.setattr(cli, "RuntimeContext", lambda *a, **kw: pytest.fail("context after rejected grant"))
    with pytest.raises(ValueError, match="invalid grant"):
        cli.initialize_execution_identity(args, mode=RuntimeMode.FORMAL_READONLY)
    assert calls == ["verified"]
    args.identity_kind = None
    with pytest.raises(ValueError, match="explicit OCI"):
        cli.initialize_execution_identity(args, mode=RuntimeMode.FORMAL_READONLY)
