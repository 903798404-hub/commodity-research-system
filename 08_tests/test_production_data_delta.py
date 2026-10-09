from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
from contextlib import nullcontext
from datetime import date
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "03_src/agri_research_agent/automation/production_data_delta.py"
COMMIT = "a" * 40
TREE = "b" * 40
SHA = "c" * 64
HISTORICAL_MISSING_COMMIT = "b0433d1930c9fc149519e20d1402eb472d9c904a"


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
        "resolve_business_end_date": staticmethod(
            lambda value: date.today() if value is None else date.fromisoformat(value)
        ),
        "run_domain": staticmethod(lambda _config, domain, **kwargs: seen.update(domain=domain, **kwargs) or {"status": "CANDIDATE", "published": False}),
    })
    monkeypatch.setattr(cli, "_bootstrap", lambda _config: None)
    monkeypatch.setattr(cli, "load_module", lambda: fake)
    assert cli.main(["--config", str(config_path), "--domain", "akshare"]) == 0
    assert seen == {
        "domain": "akshare",
        "run_root": None,
        "publish": False,
        "end_date": date.today(),
    }
    assert "fixture-only" not in capsys.readouterr().out


def test_cli_explicit_end_date_reaches_formal_runner_unchanged(
    monkeypatch, tmp_path: Path, capsys
):
    cli_path = ROOT / "04_scripts/automation/run_production_data_delta_windows.py"
    spec = importlib.util.spec_from_file_location(
        "production_data_delta_cli_end_date_under_test", cli_path
    )
    assert spec and spec.loader
    cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(cli)
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"approved_commit": COMMIT}), encoding="utf-8")
    seen = {}
    fake = type("Fake", (), {
        "validate_config": staticmethod(lambda _config: None),
        "verify_clean_detached_clone": staticmethod(lambda *_args, **_kwargs: None),
        "resolve_business_end_date": staticmethod(
            lambda value: date.fromisoformat(value)
        ),
        "run_domain": staticmethod(
            lambda _config, domain, **kwargs:
            seen.update(domain=domain, **kwargs)
            or {
                "status": "CANDIDATE",
                "published": False,
                "requested_end_date": "2026-09-21",
                "effective_end_date": "2026-09-21",
            }
        ),
    })
    monkeypatch.setattr(cli, "_bootstrap", lambda _config: None)
    monkeypatch.setattr(cli, "load_module", lambda: fake)

    assert cli.main([
        "--config", str(config_path), "--domain", "akshare",
        "--end-date", "2026-09-21",
    ]) == 0
    assert seen["end_date"] == date(2026, 9, 21)
    output = json.loads(capsys.readouterr().out)
    assert output["requested_end_date"] == "2026-09-21"
    assert output["effective_end_date"] == "2026-09-21"


def test_business_end_date_is_strict_bounded_and_separate_from_wall_clock():
    module = load_module()
    wall_clock = date(2026, 9, 22)
    assert module.resolve_business_end_date(
        "2026-09-21", wall_clock_date=wall_clock
    ) == date(2026, 9, 21)
    assert module.resolve_business_end_date(
        None, wall_clock_date=wall_clock
    ) == wall_clock
    for value in ("", "20260921", "2026-09-31"):
        with pytest.raises(module.ProductionDataError):
            module.resolve_business_end_date(value, wall_clock_date=wall_clock)
    with pytest.raises(module.ProductionDataError, match="future"):
        module.resolve_business_end_date("2026-09-23", wall_clock_date=wall_clock)


def test_akshare_provider_flags_forward_exact_business_end_date(tmp_path: Path):
    module = load_module()
    assert module._provider_flags(
        "akshare", tmp_path, end_date=date(2026, 9, 21)
    ) == ["--update-from-akshare", "--end-date", "2026-09-21"]
    with pytest.raises(module.ProductionDataError):
        module._provider_flags("akshare", tmp_path)


def test_akshare_date_evidence_rejects_requested_effective_mismatch(tmp_path: Path):
    import pandas as pd
    module = load_module()
    data = tmp_path / "01_data"
    data.mkdir()
    status = {
        "status": "success",
        "run_mode": "update_from_akshare",
        "requested_end_date": "2026-09-21",
        "effective_end_date": "2026-09-21",
        "target_business_date": "2026-09-21",
        "target_date_data_completeness": "COMPLETE",
        "target_required_contract_keys": [f"{i}27{m:02d}" for i in ("M", "RM", "Y", "OI", "P") for m in (1, 5)],
        "target_present_contract_keys": [f"{i}27{m:02d}" for i in ("M", "RM", "Y", "OI", "P") for m in (1, 5)],
        "target_missing_contract_keys": [],
        "required_contracts": 10, "success_contracts": 10, "failure_contracts": 0,
    }
    pd.DataFrame([
        {"date": pd.Timestamp("2026-09-21"), "season": "2026/2027", "calendar_offset": 112,
         "spread_name": f"{i}-{m}", "status": "success", "spread_value": 0,
         "leg1_instrument": i, "leg2_instrument": i, "leg1_month": m, "leg2_month": m,
         "leg1_contract": f"{i}27{m:02d}", "leg2_contract": f"{i}27{m:02d}",
         "leg1_price": 3000, "leg2_price": 3000}
        for i in ("M", "RM", "Y", "OI", "P") for m in (1, 5)
    ]).to_parquet(data / "historical_spread_database.parquet", index=False)
    path = data / "update_status.json"
    path.write_bytes(module.canonical_json_bytes(status))
    assert module._akshare_end_date_evidence(data, date(2026, 9, 21)) == {
        "requested_end_date": "2026-09-21",
        "effective_end_date": "2026-09-21",
    }

    status["effective_end_date"] = "2026-09-22"
    path.write_bytes(module.canonical_json_bytes(status))
    with pytest.raises(module.ProductionDataError, match="effective"):
        module._akshare_end_date_evidence(data, date(2026, 9, 21))
    status["effective_end_date"] = "2026-09-21"
    status["target_present_contract_keys"] = status["target_present_contract_keys"][:-1]
    path.write_bytes(module.canonical_json_bytes(status))
    with pytest.raises(module.ProductionDataError, match="coverage"):
        module._akshare_end_date_evidence(data, date(2026, 9, 21))
    status["target_present_contract_keys"] = list(status["target_required_contract_keys"])
    path.write_bytes(module.canonical_json_bytes(status))
    artifact = data / "historical_spread_database.parquet"
    frame = pd.read_parquet(artifact)
    frame["leg1_contract"] = None
    frame["leg2_contract"] = None
    frame.to_parquet(artifact, index=False)
    with pytest.raises(module.ProductionDataError, match="page"):
        module._akshare_end_date_evidence(data, date(2026, 9, 21))


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


def test_windows_child_uses_actual_git_identity_without_container_markers(tmp_path: Path):
    module = load_module()
    environment = module.safe_child_environment({"FAS_EXPORT_SALES_API_KEY": "fixture-only"})
    assert "MARKET_DATA_GIT_HEAD" not in environment
    assert "MARKET_DATA_GIT_TREE" not in environment

    source_root = ROOT
    expected = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=source_root, check=True,
        capture_output=True, text=True, encoding="utf-8").stdout.strip()
    sys.path.insert(0, str(source_root / "03_src"))
    try:
        from agri_research_agent.soybean_exports.common import resolve_runtime_git_head
        observed = resolve_runtime_git_head(
            project_root=source_root, environment=environment,
            release_path=tmp_path / "absent-container-release.json")
    finally:
        sys.path.remove(str(source_root / "03_src"))
    assert observed == expected
    child = module._run([
        sys.executable, "-I", "-B", "-X", "utf8", "-c",
        "print('\u5df2验证Git身份')",
    ])
    assert child.stdout.strip() == "已验证Git身份"


def test_fas_provider_is_explicitly_direct_without_inherited_proxy(monkeypatch, tmp_path: Path):
    module = load_module()
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1088")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(tmp_path / "unapproved-ca.pem"))
    environment = module.safe_child_environment(
        {"FAS_EXPORT_SALES_API_KEY": "fixture-only"})
    assert environment["NO_PROXY"] == "*"
    assert not any(key.upper().endswith("_PROXY") for key in environment
                   if key.upper() != "NO_PROXY")
    assert not set(environment) & {
        "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "SSL_CERT_FILE", "SSL_CERT_DIR"}
    import requests.utils
    monkeypatch.setenv("NO_PROXY", environment["NO_PROXY"])
    monkeypatch.setattr(requests.utils, "getproxies", lambda: {
        "http": "http://127.0.0.1:1088", "https": "http://127.0.0.1:1088"})
    assert requests.utils.get_environ_proxies("https://apps.fas.usda.gov/") == {}
    assert module._provider_flags("soybean_export_sales", tmp_path) == [
        "--runtime-root", str(tmp_path), "--candidate-only",
        "--ignore-environment-proxy",
    ]


def git(root: Path, *args: str, input: str | None = None) -> str:
    environment = os.environ.copy()
    environment.update(GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid",
                       GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.invalid",
                       GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                       GIT_OPTIONAL_LOCKS="0")
    result = subprocess.run(["git", "-C", str(root), *args], input=input, text=True,
                            capture_output=True, env=environment, check=False)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def source(tmp_path: Path):
    root = tmp_path / "source"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "one.txt").write_text("approved source\n", encoding="utf-8")
    git(root, "add", "one.txt")
    git(root, "commit", "-qm", "approved")
    module = load_module()
    git(root, "remote", "add", "origin", module.ORIGIN)
    target = git(root, "rev-parse", "HEAD")
    tree = git(root, "rev-parse", "HEAD^{tree}")
    blob = git(root, "rev-parse", "HEAD:one.txt")
    git(root, "checkout", "-q", "--detach", target)
    return root, target, tree, blob


def remove_loose_object(root: Path, oid: str) -> None:
    path = root / ".git/objects" / oid[:2] / oid[2:]
    assert path.is_file()
    path.chmod(stat.S_IREAD | stat.S_IWRITE)
    path.unlink()


def approval(module, target: str, tree: str) -> dict:
    return {"approved_commit": target, "approved_tree": tree, "origin": module.ORIGIN}


def test_exact_target_and_main_pass_without_commit_graph(source):
    module = load_module()
    root, target, tree, _ = source
    assert not (root / ".git/objects/info/commit-graph").exists()
    assert module.verify_clean_detached_clone(root, approval(module, target, tree)) == {
        "commit": target, "tree": tree, "origin": module.ORIGIN,
    }


@pytest.mark.parametrize("object_kind", ["blob", "tree", "commit"])
def test_missing_target_reachable_object_is_hard_failure(source, object_kind):
    module = load_module()
    root, target, tree, blob = source
    remove_loose_object(root, {"blob": blob, "tree": tree, "commit": target}[object_kind])
    with pytest.raises(module.ProductionDataError):
        module.verify_clean_detached_clone(root, approval(module, target, tree))


def test_main_ref_missing_reachable_blob_is_hard_failure(source):
    module = load_module()
    root, target, tree, _ = source
    git(root, "checkout", "-q", "main")
    (root / "new.txt").write_text("new main content\n", encoding="utf-8")
    git(root, "add", "new.txt")
    git(root, "commit", "-qm", "advance main")
    new_blob = git(root, "rev-parse", "main:new.txt")
    git(root, "checkout", "-q", "--detach", target)
    remove_loose_object(root, new_blob)
    assert module._object_closure(root, target, label="target")
    with pytest.raises(module.ProductionDataError):
        module._authoritative_ref_closure(root, target)


@pytest.mark.parametrize("release_ref", ["branch", "tag"])
def test_formal_release_ref_missing_reachable_blob_is_hard_failure(source, release_ref):
    module = load_module()
    root, target, _, _ = source
    git(root, "checkout", "-q", "-b", "release/v1")
    (root / "release.txt").write_text("release-only content\n", encoding="utf-8")
    git(root, "add", "release.txt")
    git(root, "commit", "-qm", "release")
    blob = git(root, "rev-parse", "HEAD:release.txt")
    if release_ref == "tag":
        git(root, "tag", "-a", "release/v1", "-m", "formal release")
    git(root, "checkout", "-q", "--detach", target)
    if release_ref == "tag":
        git(root, "branch", "-D", "release/v1")
    remove_loose_object(root, blob)
    with pytest.raises(module.ProductionDataError):
        module._authoritative_ref_closure(root, target)


def test_historical_b0433d_graph_failure_is_warning_after_hard_closures(
        source, monkeypatch, capsys):
    module = load_module()
    root, target, tree, _ = source
    original = module._git_probe

    def historical_probe(selected, *args, **kwargs):
        if args == ("fsck", "--strict", "--no-reflogs"):
            return subprocess.CompletedProcess(args, 16, "",
                f"error: Could not read {HISTORICAL_MISSING_COMMIT}\n"
                f"failed to parse commit {HISTORICAL_MISSING_COMMIT} "
                "from object database for commit-graph\n")
        return original(selected, *args, **kwargs)

    monkeypatch.setattr(module, "_git_probe", historical_probe)
    assert module.verify_clean_detached_clone(root, approval(module, target, tree))["commit"] == target
    assert HISTORICAL_MISSING_COMMIT in capsys.readouterr().err


def test_graph_failure_cannot_downgrade_protected_or_unknown_object(source, monkeypatch):
    module = load_module()
    root, target, _, _ = source
    original = module._git_probe

    def graph_probe(selected, *args, **kwargs):
        if args == ("fsck", "--strict", "--no-reflogs"):
            return subprocess.CompletedProcess(args, 16, "",
                f"error: Could not read {target}\n"
                f"failed to parse commit {target} from object database for commit-graph\n")
        return original(selected, *args, **kwargs)

    monkeypatch.setattr(module, "_git_probe", graph_probe)
    with pytest.raises(module.ProductionDataError, match="not proven unrelated"):
        module._repository_maintenance_diagnostic(root, {target})
    monkeypatch.setattr(module, "_git_probe", lambda *_a, **_k:
                        subprocess.CompletedProcess([], 1, "", "fatal: unknown corruption\n"))
    with pytest.raises(module.ProductionDataError, match="not proven unrelated"):
        module._repository_maintenance_diagnostic(root, set())


def test_unreachable_historical_garbage_does_not_block(source, capsys):
    module = load_module()
    root, target, _, _ = source
    dangling = git(root, "hash-object", "-w", "--stdin", input="obsolete bytes")
    assert dangling not in module._object_closure(root, target, label="target")
    module._repository_maintenance_diagnostic(root, {target})
    assert "REPOSITORY_MAINTENANCE_WARNING" in capsys.readouterr().err


def test_archive_materialization_failure_remains_hard(source, monkeypatch):
    module = load_module()
    root, target, tree, _ = source
    original = module._run

    def failed_archive(command, **kwargs):
        if "archive" in command:
            raise module.ProductionDataError("archive unavailable")
        return original(command, **kwargs)

    monkeypatch.setattr(module, "_run", failed_archive)
    with pytest.raises(module.ProductionDataError, match="archive unavailable"):
        module.verify_clean_detached_clone(root, approval(module, target, tree))


def test_unverified_gitlink_remains_hard_failure(source):
    module = load_module()
    root, _, _, _ = source
    git(root, "checkout", "-q", "main")
    git(root, "update-index", "--add", "--cacheinfo",
        "160000," + "a" * 40 + ",submodule")
    git(root, "commit", "-qm", "add unverified gitlink")
    target = git(root, "rev-parse", "HEAD")
    tree = git(root, "rev-parse", "HEAD^{tree}")
    git(root, "checkout", "-q", "--detach", target)
    with pytest.raises(module.ProductionDataError, match="gitlink"):
        module._reject_unverified_gitlinks(root, target)
