"""Notification contract and fail-closed consumer boundaries (no production IO)."""
import ast
from copy import deepcopy
from datetime import date
import json
from pathlib import Path
import socket

import pytest

from agri_research_agent.alerts import core, gate
from agri_research_agent.alerts.core import build_message, canonical_json
from agri_research_agent.alerts.delivery import deliver_candidate, validate_evidence
from agri_research_agent.alerts.senders import FakeSender
from agri_research_agent.alerts.views import async_view, status_line


def message(kind="weather", business=None):
    return build_message(message_type=kind, source_run_id="run-1",
                         source_data_identity={"release_id": "release-1"},
                         business=business or {"text": "天气 / 基差"}, status={},
                         metadata={}, rendered_content="candidate")


@pytest.mark.parametrize("kind", ["weather", "domestic_basis", "future-domain"])
def test_canonical_message_identity_and_copy(kind):
    first, second = message(kind), message(kind)
    assert first.canonical_payload == second.canonical_payload
    assert first.content_sha256 == second.content_sha256
    changed = first.payload
    changed["business"]["text"] = "mutated"
    assert first.canonical_payload == second.canonical_payload
    assert first.message_id == message(kind, {"changed": True}).message_id
    assert first.content_sha256 != message(kind, {"changed": True}).content_sha256


def test_domains_do_not_conflict():
    assert message("weather").message_id != message("domestic_basis").message_id


@pytest.mark.parametrize("bad", [{1: "key"}, {"set": {1, 2}}, {"nan": float("nan")}, {"date": date.today()}])
def test_noncanonical_values_rejected(bad):
    with pytest.raises(ValueError):
        canonical_json(bad)


def test_key_order_and_utf8():
    assert canonical_json({"b": 1, "a": "中文"}) == canonical_json({"a": "中文", "b": 1})
    assert "中文" in canonical_json({"a": "中文"}).decode("utf-8")


@pytest.mark.parametrize("outcome", ["SENT", "FAILED", "UNKNOWN"])
def test_fake_delivery_duplicates_no_retry(tmp_path, monkeypatch, outcome):
    def forbidden(*a, **k):
        pytest.fail("Network or duplicate send attempted")
    monkeypatch.setattr(socket, "socket", forbidden)
    first = deliver_candidate(message(), tmp_path, FakeSender(outcome))
    validate_evidence(first["evidence"])
    assert first["evidence"]["status"] == outcome
    monkeypatch.setattr(FakeSender, "send", forbidden)
    second = deliver_candidate(message(), tmp_path, FakeSender("SENT"))
    assert second["duplicate"]
    assert second["evidence"] == first["evidence"]
    assert "attempted_at" not in message().payload


def test_content_conflict_fails_closed(tmp_path):
    deliver_candidate(message(), tmp_path, FakeSender())
    with pytest.raises(ValueError, match="CONTENT_CONFLICT"):
        deliver_candidate(message(business={"different": True}), tmp_path, FakeSender())


def test_interrupted_attempt_reserves_unknown(tmp_path, monkeypatch):
    def crash(*a):
        raise RuntimeError("simulated interruption")
    monkeypatch.setattr(FakeSender, "send", crash)
    with pytest.raises(RuntimeError):
        deliver_candidate(message(), tmp_path, FakeSender())
    result = deliver_candidate(message(), tmp_path, FakeSender())
    assert result["duplicate"] and result["evidence"]["status"] == "UNKNOWN"


def report():
    rows = [{"identity": "present", "coverage_status": "PRESENT", "update_status": "NO_CHANGE",
             "freshness_status": "UNASSESSED", "reason": "NO_VALID_NEW_OBSERVATIONS", "blocking": False}]
    for metric in ("max", "min"):
        rows.append({"identity": f"weather.temperature_{metric}.rapeseed.eu.germany.forecast.ecmwf",
                     "coverage_status": "MISSING", "update_status": "NO_CHANGE",
                     "freshness_status": "UNASSESSED", "reason": "SOURCE_NO_VALID_OBSERVATION", "blocking": False})
    return {"dataset_status": "NO_CHANGE", "promotion_allowed": True, "blocking_reasons": [],
            "series": rows, "summary": {"TOTAL_REQUIRED": 3,
                "coverage": {"PRESENT": 1, "MISSING": 2, "ERROR": 0},
                "updates": {"UPDATED": 0, "NO_CHANGE": 3, "ERROR": 0},
                "freshness": {"FRESH": 0, "STALE": 0, "UNASSESSED": 3}}}


def test_nonblocking_missing_and_unassessed(tmp_path):
    run = gate.ReadyRun("run-1", "TEST / FIXTURE", tmp_path,
                        {"async_updates": {"weather_forecast": report()}}, {}, {})
    value = async_view(run.report("weather_forecast"))
    assert len(value["nonblocking_missing"]) == 2
    assert all(row["reason"] == "SOURCE_NO_VALID_OBSERVATION" for row in value["nonblocking_missing"])
    text = status_line("Forecast", value)
    assert "NO_CHANGE" in text and "UNASSESSED=3" in text and "FRESH=0" in text


@pytest.mark.parametrize("mutation", ["error", "blocking", "incomplete", "denied"])
def test_bad_async_evidence_rejected(tmp_path, mutation):
    value = report()
    if mutation == "error":
        value["series"][0]["update_status"] = "ERROR"
        value["summary"]["updates"].update(ERROR=1, NO_CHANGE=2)
    elif mutation == "blocking":
        value["series"][0]["blocking"] = True
    elif mutation == "incomplete":
        value["summary"]["TOTAL_REQUIRED"] = 4
    else:
        value["promotion_allowed"] = False
    run = gate.ReadyRun("run-1", "TEST / FIXTURE", tmp_path, {"async_updates": {"target": value}}, {}, {})
    with pytest.raises(ValueError):
        run.report("target")


def test_missing_formal_root_no_fallback(tmp_path):
    with pytest.raises(FileNotFoundError):
        gate.load_ready_run(tmp_path / "run-1", "run-1", mode="formal")
    with pytest.raises(ValueError, match="OVERRIDE_FORBIDDEN"):
        gate.load_ready_run(tmp_path / "run-1", "run-1", mode="formal", fixture_package_root=tmp_path)


def test_core_neutral_and_import_boundary():
    allowed = {"agri_research_agent.market_data.public_weather_current",
               "agri_research_agent.market_data.public_basis_current",
               "agri_research_agent.summary_engine.weather",
               "agri_research_agent.summary_engine.basis",
               "agri_research_agent.automation.full_daily_windows",
               "agri_research_agent.pipelines.async_contract_rollout"}
    root = Path(core.__file__).parent
    for path in root.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("agri_research_agent."):
                assert node.module in allowed, (path, node.module)
                if node.module.endswith("full_daily_windows"):
                    assert [alias.name for alias in node.names] == ["validate_daily_manifest"]
                if node.module.endswith("async_contract_rollout"):
                    assert [alias.name for alias in node.names] == ["validate_report"]
        if path.name == "core.py":
            assert "weather" not in source.lower() and "basis" not in source.lower()
    assert (root / "weather.py").is_file() and (root / "basis.py").is_file()


@pytest.fixture
def sealed_fixture(tmp_path):
    run_dir = tmp_path / "run-1"
    daily_path = run_dir / "runtime/public-data-daily/runs/run-1/manifest.json"
    daily_path.parent.mkdir(parents=True)
    package_root = run_dir / "runtime-baseline/package-1"
    data = package_root / "data/fixture.txt"
    data.parent.mkdir(parents=True)
    data.write_text("explicit test data", encoding="utf-8")
    package = {"schema_version": "public-current-production-package/2", "package_id": "package-1",
               "delivery_identity_sha256": "a" * 64, "bundle_sha256": "b" * 64,
               "current_identities": {}, "file_count": 1,
               "files": [{"path": "fixture.txt", "sha256": gate.file_sha(data), "size_bytes": data.stat().st_size}]}
    manifest = package_root / "manifest.json"
    manifest.write_bytes(canonical_json(package))
    daily = {"schema_version": "unified-public-data-daily-update/1", "run_id": "run-1",
             "succeeded": True, "business_status": "NO_CHANGE", "async_summary_complete": True,
             "async_updates": {"weather_forecast": report()}, "delivery_identity": "a" * 64,
             "sources": [{"source": key, "status": "NO_CHANGE"} for key in ("tankan", "lutou", "lutou_domestic_basis")],
             "consumer_freshness_validation": {"status": "WARNING"},
             "production_data_package": {"status": "SKIPPED"}, "server_sync": "SKIPPED"}
    daily_path.write_bytes(canonical_json(daily))
    final = {"status": "SUCCESS", "run_id": "run-1", "daily_succeeded": True, "process_exit_code": 0,
             "daily_manifest_path": str(daily_path), "daily_manifest_sha256": gate.file_sha(daily_path)}
    (run_dir / "final-status.json").write_bytes(canonical_json(final))
    baseline = {key: package[key] for key in ("package_id", "bundle_sha256", "delivery_identity_sha256")}
    baseline["manifest_sha256"] = gate.file_sha(manifest)
    preflight = {"run_id": "run-1", "gates": [
        {"gate": "REMOTE_IDENTITY", "status": "PASS", "safe_reason": {"package_id": "package-1"}},
        {"gate": "RUNTIME_BASELINE", "status": "PASS", "safe_reason": baseline}]}
    (run_dir / "preflight.json").write_bytes(canonical_json(preflight))
    return run_dir, package_root


def test_explicit_fixture_and_activated_provenance(sealed_fixture):
    run_dir, package_root = sealed_fixture
    fixture = gate.load_ready_run(run_dir, "run-1", mode="fixture", fixture_package_root=package_root)
    assert fixture.mode == "TEST / FIXTURE"
    assert fixture.report("weather_forecast")["summary"]["coverage"]["MISSING"] == 2
    # Synthetic evidence exercises formal gate code; this is not a production experiment.
    formal = gate.load_ready_run(run_dir, "run-1", mode="formal")
    assert formal.mode == "FORMAL / PRODUCTION-LIKE"
    assert formal.data_root == fixture.data_root


@pytest.mark.parametrize("failure", ["final_failed", "wrong_run", "sha", "async_incomplete", "daily_failed",
                                    "package_wrong_run", "data_tampered", "preflight_failed", "missing_root"])
def test_sealed_gate_rejections(sealed_fixture, failure):
    run_dir, package_root = sealed_fixture
    final_path = run_dir / "final-status.json"
    final = gate.read_json(final_path)
    daily_path = Path(final["daily_manifest_path"])
    if failure == "final_failed":
        final["status"] = "FAILED"
    elif failure == "wrong_run":
        final["run_id"] = "other-run"
    elif failure == "sha":
        final["daily_manifest_sha256"] = "0" * 64
    elif failure in {"async_incomplete", "daily_failed"}:
        daily = gate.read_json(daily_path)
        daily["async_summary_complete" if failure == "async_incomplete" else "succeeded"] = False
        daily_path.write_bytes(canonical_json(daily))
        final["daily_manifest_sha256"] = gate.file_sha(daily_path)
    elif failure == "package_wrong_run":
        manifest = package_root / "manifest.json"
        package = gate.read_json(manifest)
        package["delivery_identity_sha256"] = "c" * 64
        manifest.write_bytes(canonical_json(package))
    elif failure == "data_tampered":
        (package_root / "data/fixture.txt").write_text("tampered", encoding="utf-8")
    elif failure == "preflight_failed":
        preflight = gate.read_json(run_dir / "preflight.json")
        preflight["gates"][0]["status"] = "FAIL"
        (run_dir / "preflight.json").write_bytes(canonical_json(preflight))
    else:
        (package_root / "manifest.json").unlink()
    final_path.write_bytes(canonical_json(final))
    with pytest.raises(Exception):
        gate.load_ready_run(run_dir, "run-1", mode="formal")


def test_public_identity_mismatch(sealed_fixture):
    run = gate.load_ready_run(sealed_fixture[0], "run-1", mode="formal")
    with pytest.raises(ValueError, match="IDENTITY_MISMATCH"):
        run.bind("lutou-weather", {"release_id": "unknown", "manifest_sha256": "f" * 64})


def test_basis_real_summary_with_independent_29_and_21(tmp_path, monkeypatch):
    from agri_research_agent.alerts import basis
    from agri_research_agent.market_data.public_basis_current import PublicBasisCurrentIdentity, PublicBasisCurrentSnapshot
    import pandas as pd
    identity = PublicBasisCurrentIdentity("release", "f" * 64, "test", 2, 29,
                                         date(2026, 9, 2), date(2026, 9, 3), date(2026, 9, 3))
    rows = [{"date": day, "commodity": "soybean_oil", "region": "east_china", "quote_type": "spot",
             "delivery_month": "2026-09", "futures_contract": "Y2609", "basis": value}
            for day, value in (("2026-09-02", 100), ("2026-09-03", 120))]
    snapshot = PublicBasisCurrentSnapshot(identity, pd.DataFrame(rows))
    monkeypatch.setattr(basis, "resolve_public_basis_current_identity", lambda _: identity)
    monkeypatch.setattr(basis, "load_public_basis_current", lambda *a, **k: snapshot)
    evidence = report()
    evidence["series"] = [dict(evidence["series"][0], identity=f"basis-{n}") for n in range(21)]
    evidence["summary"] = {"TOTAL_REQUIRED": 21, "coverage": {"PRESENT": 21, "MISSING": 0, "ERROR": 0},
                           "updates": {"UPDATED": 0, "NO_CHANGE": 21, "ERROR": 0},
                           "freshness": {"FRESH": 0, "STALE": 0, "UNASSESSED": 21}}
    run = gate.ReadyRun("run-1", "TEST / FIXTURE", tmp_path,
                        {"async_updates": {"domestic_basis": evidence}, "completed_at": "2026-09-04T00:00:00Z"},
                        {"current_identities": {"lutou-domestic-basis": {"release_id": "release", "manifest_sha256": "f" * 64}}}, {})
    first, second = basis.build(run), basis.build(run)
    assert first.canonical_payload == second.canonical_payload
    assert first.content_sha256 == second.content_sha256
    assert first.payload["metadata"]["public_series_count"] == 29
    assert first.payload["metadata"]["async_required_count"] == 21
    quote = first.payload["business"]["summary"]["facts"]["quotes"][0]
    assert quote["previous_date"] == "2026-09-02" and quote["change"] == 20
    assert "今日" not in first.payload["rendered_content"]


def test_weather_domain_fixture_repeat_and_evidence(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from agri_research_agent.alerts import weather
    from agri_research_agent.market_data.public_weather_current import PublicWeatherCurrentIdentity
    identity = PublicWeatherCurrentIdentity("release", "f" * 64, "test", date(2026, 9, 3), date(2026, 9, 17), "e" * 64)
    monkeypatch.setattr(weather, "resolve_weather_current_identity", lambda _: identity)
    monkeypatch.setattr(weather, "load_public_weather_current", lambda *a, **k: SimpleNamespace(identity=identity, records=[], normals=[]))
    calls = []
    def summary(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(short_text="区域降水偏少（fixture）", to_dict=lambda: {
            "short_text": "区域降水偏少（fixture）", "freshness_status": "fresh" if len(calls) == 1 else "stale",
            "source_identity": kwargs["source_identity"], "generated_at": kwargs["generated_at"].isoformat()})
    monkeypatch.setattr(weather, "build_weather_summary", summary)
    config = tmp_path / "weather.yaml"
    config.write_text('test_weather:\n  crop: rapeseed\n  country: EU\n  page_title: 欧洲油菜天气\n  regions:\n    - key: germany\n', encoding="utf-8")
    run = gate.ReadyRun("run-1", "TEST / FIXTURE", tmp_path,
                        {"async_updates": {"weather_observation": report(), "weather_forecast": report()},
                         "completed_at": "2026-09-04T00:00:00Z"},
                        {"current_identities": {"lutou-weather": {"release_id": "release", "manifest_sha256": "f" * 64}}}, {})
    first = weather.build(run, weather_config=config)
    second = weather.build(run, weather_config=config)
    assert first.canonical_payload == second.canonical_payload
    assert first.content_sha256 == second.content_sha256
    assert "UNASSESSED=3" in first.payload["rendered_content"]
    assert "区域降水偏少" in first.payload["rendered_content"]
    assert "freshness_status" not in first.payload["business"]["summary"]
    assert len(first.payload["status"]["weather_forecast"]["nonblocking_missing"]) == 2


def test_preview_gate_precedes_consumers_and_delivery(tmp_path, monkeypatch):
    from agri_research_agent.alerts import preview
    def forbidden(*a, **k):
        pytest.fail("Consumer or sender invoked before DATA_READY")
    monkeypatch.setitem(preview.DOMAINS, "weather", forbidden)
    monkeypatch.setattr(preview, "deliver_candidate", forbidden)
    with pytest.raises(FileNotFoundError):
        preview.preview(domain="weather", run_dir=tmp_path / "run-1", source_run_id="run-1", mode="formal")
