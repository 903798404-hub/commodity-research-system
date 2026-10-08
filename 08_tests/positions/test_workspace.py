from datetime import date
import importlib.util
import json
from pathlib import Path
import sys

import pytest
from streamlit.testing.v1 import AppTest

from agri_research_agent.positions.bundle import export_bundle
from agri_research_agent.positions.workspace import (
    DOMAINS, collection_plan, data_root, local_root, validate_domain,
)
from agri_research_agent.sugar_positions.storage import digest, publish, read_snapshot

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "05_apps"))


@pytest.fixture
def project(tmp_path):
    (tmp_path / ".git").write_text("gitdir: test", encoding="utf-8")
    (tmp_path / "02_configs").mkdir()
    for name in ["sugar_positions.json", "oilseed_positions.json"]:
        (tmp_path / "02_configs" / name).write_bytes((ROOT / "02_configs" / name).read_bytes())
    return tmp_path


def rows(market="sugar11"):
    return [dict(market=market, group="managed_money", report_type="futures_only",
                 report_date=f"2026-09-{n:02d}", long=n*100, short=300, open_interest=3000,
                 unit="contracts") for n in range(1, 10)]


@pytest.mark.parametrize("domain", DOMAINS)
def test_missing_explicit_runtime_never_falls_back_to_preview(project, monkeypatch, domain):
    preview = local_root(project, domain)
    publish(preview, rows("sugar11" if domain == "sugar" else "cbot_meal"), [], [], [])
    runtime = project / "external_runtime"
    monkeypatch.setenv("PUBLIC_MARKET_DATA_RUNTIME_ROOT", str(runtime))
    target = data_root(project, domain)
    assert target == runtime / "processed/commodity_positions" / domain
    assert read_snapshot(target)["foreign"] == []
    assert not runtime.exists()


def test_primary_checkout_without_runtime_reads_formal_only(project, monkeypatch):
    monkeypatch.delenv("PUBLIC_MARKET_DATA_RUNTIME_ROOT", raising=False)
    (project / ".git").unlink()
    (project / ".git").mkdir()
    assert data_root(project, "sugar") == project / "01_data/processed/commodity_positions/sugar"


def test_wrong_domain_and_excluded_soybean_contracts_are_rejected(project):
    with pytest.raises(ValueError, match="外盘"):
        validate_domain({"foreign": rows("cbot_meal"), "domestic": []}, project, "sugar")
    for scope in ["A2701", "B2701", "P2701"]:
        with pytest.raises(ValueError, match="国内"):
            validate_domain({"foreign": [], "domestic": [{"scope": scope}]}, project, "soybean")


def test_plan_uses_explicit_contracts_and_independent_collectors(project):
    plan = collection_plan(project, list(DOMAINS), "all", date(2026, 10, 8), 14, 2025)
    commands = {item["domain"]: item["argv"] for item in plan}
    assert "--sina-contracts" in commands["soybean"]
    assert commands["soybean"][commands["soybean"].index("--sina-contracts")+1:][:2] == ["M2701", "Y2701"]
    assert "P2701" in commands["palm"]
    assert "--sina-contracts" not in commands["rapeseed"]
    assert "2026-09-25" in commands["sugar"]
    assert "--czce-end" in commands["sugar"]
    foreign = collection_plan(project, ["soybean"], "foreign", date(2026, 10, 8), 14, 2025)
    assert "--sina-contracts" not in foreign[0]["argv"]
    with pytest.raises(ValueError):
        collection_plan(project, ["sugar", "sugar"], "all", date(2026, 10, 8), 14, 2025)


def test_weekly_plan_does_not_keep_expanding_the_onboarding_date(project):
    command = collection_plan(project, ["rapeseed"], "foreign", date(2027, 10, 8), 14, 2026)[0]["argv"]
    start = date.fromisoformat(command[command.index("--euronext-start") + 1])
    assert 0 < (date(2027, 10, 8) - start).days <= 35


def test_export_preserves_exact_snapshot_and_raw_bytes_and_rejects_corruption(project):
    source = local_root(project, "sugar")
    publish(source, rows(), [], [("cftc_futures", b"official raw fixture", "https://example.test/report", "json")], [])
    before = (source / "current.json").read_bytes()
    bundle = export_bundle(project, ["sugar"])
    manifest = json.loads((bundle / "bundle.json").read_text(encoding="utf-8"))
    for name, sha in manifest["files"].items():
        assert digest((bundle / name).read_bytes()) == sha
    assert (bundle / "domains/sugar/current.json").read_bytes() == before
    assert (source / "current.json").read_bytes() == before
    assert read_snapshot(bundle / "domains/sugar")["foreign"] == rows()
    next(source.glob("raw/*.json")).write_bytes(b"changed")
    with pytest.raises(ValueError, match="SHA"):
        export_bundle(project, ["sugar"])
    assert len(list(bundle.parent.iterdir())) == 1


def test_unified_route_switches_all_domains_without_network_or_legacy_parquet(project, monkeypatch):
    import foreign_seats_page
    import requests
    monkeypatch.delenv("PUBLIC_MARKET_DATA_RUNTIME_ROOT", raising=False)
    def reject_request(*args, **kwargs):
        raise AssertionError("page must not collect data")
    monkeypatch.setattr(requests.Session, "request", reject_request)
    publish(local_root(project, "sugar"), rows(), [], [], [])
    publish(local_root(project, "soybean"), rows("cbot_meal"), [], [], [])
    script = f"from pathlib import Path\nfrom foreign_seats_page import render_foreign_seats_page\nrender_foreign_seats_page(Path({str(project)!r}))"
    app = AppTest.from_string(script).run(timeout=30)
    assert not app.exception
    assert app.title[0].value == "白糖资金情绪"
    assert app.get("plotly_chart") and app.dataframe
    app.radio(key="commodity_positions_domain").set_value("soybean").run(timeout=30)
    assert not app.exception and app.title[0].value == "大豆资金情绪"
    assert app.get("plotly_chart")
    app.radio(key="commodity_positions_domain").set_value("palm").run(timeout=30)
    assert not app.exception and not app.metric and not app.get("plotly_chart")
    app.radio(key="commodity_positions_domain").set_value("rapeseed").run(timeout=30)
    assert not app.exception and app.title[0].value == "菜籽资金情绪"
    assert not (project / "01_data/database/foreign_seats").exists()


def test_plan_cli_does_not_collect_or_create_outputs(project, monkeypatch, capsys):
    path = ROOT / "04_scripts/positions/update_positions.py"
    spec = importlib.util.spec_from_file_location("positions_update_cli", path)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    monkeypatch.setattr(cli, "ROOT", project)
    monkeypatch.delenv("PUBLIC_MARKET_DATA_RUNTIME_ROOT", raising=False)
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **kw: pytest.fail("plan must not run collectors"))
    assert cli.main(["--plan"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 4
    assert not (project / "01_data").exists() and not (project / "06_outputs").exists()


def test_formal_entry_renders_replacement_route_without_legacy_database(monkeypatch):
    monkeypatch.delenv("PUBLIC_MARKET_DATA_RUNTIME_ROOT", raising=False)
    app = AppTest.from_file(str(ROOT / "05_apps/streamlit_app.py"), default_timeout=30)
    app.session_state["selected_workspace_page"] = "外资与重点席位"
    app.run()
    assert not app.exception
    assert app.radio(key="commodity_positions_domain").options == list(DOMAINS.values())
    assert not any("最新交易日摘要" in item.value for item in app.info)
