from __future__ import annotations

import hashlib
import importlib.util
import json
from contextlib import nullcontext
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "03_src/agri_research_agent/automation/production_data_delta.py"
COMMIT = "a" * 40
TREE = "b" * 40
SHA = "c" * 64


def load_module():
    spec = importlib.util.spec_from_file_location("production_data_delta_under_test", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def config(module, tmp_path: Path) -> dict:
    baseline = tmp_path / "baseline"; baseline.mkdir()
    public = tmp_path / "public"; public.mkdir()
    secret_a = tmp_path / "nass.env"; secret_a.write_text("NASS_API_KEY=fixture-only\n", encoding="utf-8")
    secret_f = tmp_path / "fas.env"; secret_f.write_text("FAS_EXPORT_SALES_API_KEY=fixture-only\n", encoding="utf-8")
    return {
        "schema_version": "production-data-producer-config/1", "approved_commit": COMMIT,
        "approved_tree": TREE, "origin": module.ORIGIN, "python": sys.executable,
        "runtime_root": str(tmp_path / "runtime"), "baseline_root": str(baseline),
        "baseline_manifest_sha256": SHA, "public_package_root": str(public),
        "public_package_manifest_sha256": SHA, "ssh_target": "approved-host",
        "image_id": "sha256:" + "d" * 64, "remote_allocation": "/var/lib/market-data/production-runtime/a",
        "policy": {"soybean_crop_progress": "/etc/market-data/production-data-delivery/crop.json", "soybean_export_sales": "/etc/market-data/production-data-delivery/fas.json"},
        "policy_sha256": {"soybean_crop_progress": SHA, "soybean_export_sales": SHA},
        "publisher": "/opt/market-data/activate_production_data_delta.py", "publisher_sha256": SHA,
        "remote_store_root": "/var/lib/market-data/production-runtime/a/01_data/public-data-server-store",
        "domains": ["akshare", "soybean_crop_progress", "soybean_export_sales"],
        "secrets": {"nass": {"path": str(secret_a), "keys": ["NASS_API_KEY"]}, "fas-export-sales": {"path": str(secret_f), "keys": ["FAS_EXPORT_SALES_API_KEY"]}},
        "sources": dict(module.SOURCES), "full_daily_lock_path": str(tmp_path / "formal-full-daily.lock"),
    }


def test_config_is_closed_and_rejects_preview_wrong_sha_or_unapproved_source(monkeypatch, tmp_path: Path):
    module = load_module(); value = config(module, tmp_path)
    monkeypatch.setattr(module, "_absolute", lambda value, **_kwargs: Path(value))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    value["full_daily_lock_path"] = str(tmp_path / "market-data-runtime/automation/full-daily.lock")
    assert module.validate_config(value) is value
    for changed in (dict(value, preview=True), dict(value, approved_commit="A" * 40), dict(value, sources={**module.SOURCES, "preview": "04_scripts/preview.py"})):
        with pytest.raises(module.ProductionDataError):
            module.validate_config(changed)


def test_secret_parser_and_child_environment_are_closed_and_do_not_preserve_unrelated_credentials(monkeypatch, tmp_path: Path):
    module = load_module(); secret = tmp_path / "secret.env"
    secret.write_text("NASS_API_KEY= fixture-value \n", encoding="utf-8")
    assert module._secret_environment(secret, ["NASS_API_KEY"]) == {"NASS_API_KEY": "fixture-value"}
    secret.write_text("NASS_API_KEY=x\nEXTRA=y\n", encoding="utf-8")
    with pytest.raises(module.ProductionDataError): module._secret_environment(secret, ["NASS_API_KEY"])
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-cross-boundary")
    env = module.safe_child_environment({"NASS_API_KEY": "fixture-value", "MARKET_DATA_GIT_HEAD": COMMIT})
    assert "AWS_SECRET_ACCESS_KEY" not in env and env["NASS_API_KEY"] == "fixture-value"
    with pytest.raises(module.ProductionDataError): module.safe_child_environment({"UNAPPROVED": "x"})


def test_baseline_requires_pinned_manifest_exact_bytes_and_no_extra_files(tmp_path: Path):
    module = load_module(); root = tmp_path / "baseline"; root.mkdir()
    payload = root / "historical_price_long.xlsx"; payload.write_bytes(b"baseline")
    record = {"path": payload.name, "sha256": hashlib.sha256(b"baseline").hexdigest(), "size_bytes": 8}
    manifest = {"schema_version": "production-producer-baseline/1", "source_root": "approved", "public_package_id": "public-current-" + "a" * 24, "files": [record]}
    raw = module.canonical_json_bytes(manifest); (root / "baseline_manifest.json").write_bytes(raw)
    assert module.verify_baseline(root, hashlib.sha256(raw).hexdigest())[payload.name] == {"sha256": record["sha256"], "size_bytes": 8}
    payload.write_bytes(b"drifted")
    with pytest.raises(module.ProductionDataError): module.verify_baseline(root, hashlib.sha256(raw).hexdigest())


def test_cli_default_candidate_only_never_requests_publish_and_does_not_echo_credentials(monkeypatch, tmp_path: Path, capsys):
    cli_path = ROOT / "04_scripts/automation/run_production_data_delta_windows.py"
    spec = importlib.util.spec_from_file_location("production_data_delta_cli_under_test", cli_path)
    assert spec and spec.loader
    cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(cli)
    config_path = tmp_path / "config.json"; config_path.write_text(json.dumps({"approved_commit": COMMIT}), encoding="utf-8")
    seen = {}
    fake = type("Fake", (), {
        "validate_config": staticmethod(lambda _config: None),
        "verify_clean_detached_clone": staticmethod(lambda *_args, **_kwargs: None),
        "run_domain": staticmethod(lambda _config, domain, **kwargs: seen.update(domain=domain, **kwargs) or {"status": "CANDIDATE", "published": False}),
    })
    monkeypatch.setattr(cli, "_bootstrap", lambda _config: None)
    monkeypatch.setattr(cli, "load_module", lambda: fake)
    assert cli.main(["--config", str(config_path), "--domain", "akshare"]) == 0
    assert seen == {"domain": "akshare", "run_root": None, "publish": False}
    assert "fixture-only" not in capsys.readouterr().out


def test_crop_out_of_season_is_an_explicit_non_delivery(monkeypatch, tmp_path: Path):
    module = load_module(); value = config(module, tmp_path)
    producer = {"commit": COMMIT, "tree": TREE, "origin": module.ORIGIN}
    baseline = tmp_path / "approved-baseline"; baseline.mkdir()
    monkeypatch.setattr(module, "validate_config", lambda selected: selected)
    monkeypatch.setattr(module, "verify_clean_detached_clone", lambda *_args, **_kwargs: producer)
    monkeypatch.setattr(module, "verify_baseline", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(module, "_selected_baseline", lambda *_args: (baseline, SHA, {}))
    monkeypatch.setattr(module, "_git", lambda *_args: "")
    monkeypatch.setattr(module, "_secret_environment", lambda *_args: {})
    monkeypatch.setattr(module, "_domain_lock", lambda *_args: nullcontext())
    monkeypatch.setattr(module, "collect_delta", lambda *_args, **_kwargs: pytest.fail("skip must not build a delta"))
    monkeypatch.setattr(module, "invoke_publisher", lambda *_args, **_kwargs: pytest.fail("skip must not invoke publisher"))
    monkeypatch.setattr(module, "sys", SimpleNamespace(
        flags=SimpleNamespace(isolated=1), dont_write_bytecode=True,
        executable=sys.executable))

    def fake_run(command, *, cwd=None, **_kwargs):
        if command[0] == "git" and "clone" in command:
            Path(command[-1]).mkdir(parents=True)
        elif command[0] == value["python"]:
            audit_root = Path(cwd) / "06_outputs/audits/soybean_crop_progress/dry_runs"
            audit_root.mkdir(parents=True)
            run_id = "2026-20260115t120000000000z-fixture"
            audit_path = audit_root / f"soybeans_crop_weekly_update_{run_id}.json"
            markdown_path = audit_path.with_suffix(".md")
            markdown_path.write_text("seasonal skip\n", encoding="utf-8")
            audit = {
                "run_id": run_id, "status": "out_of_season",
                "new_york_reporting_date": "2026-01-15", "current_year": 2026,
                "season_window": "04-01 through 11-30 (America/New_York)",
                "run_mode": "dry_run", "force": False, "git_head": COMMIT,
                "published": False, "recommended_to_publish": False,
                "business_change_found": False, "error": None,
                "raw_path": None, "manifest_path": None,
                "candidate_progress_path": None, "candidate_condition_path": None,
                "audit_json_path": str(audit_path),
                "audit_markdown_path": str(markdown_path),
            }
            audit_path.write_bytes(module.canonical_json_bytes(audit))
        return SimpleNamespace(stdout="", returncode=0)

    monkeypatch.setattr(module, "_run", fake_run)
    result = module.run_domain(value, "soybean_crop_progress", publish=True)
    assert result["status"] == "SKIPPED_OUT_OF_SEASON"
    assert result["provider_status"] == "out_of_season"
    assert result["published"] is False
    assert "candidate" not in result and "manifest_sha256" not in result
    assert "receipt_path" not in result and Path(result["audit_path"]).is_file()
