from __future__ import annotations

import importlib
from pathlib import Path
import sys

from streamlit.testing.v1 import AppTest
import yaml


ROOT = Path(__file__).resolve().parents[1]
APPS_DIR = ROOT / "05_apps"
if str(APPS_DIR) not in sys.path:
    sys.path.insert(0, str(APPS_DIR))

import streamlit_app as workspace


def test_authoritative_route_is_unique_and_catalog_schema_stays_deployment_only():
    catalog = yaml.safe_load(
        (ROOT / "02_configs" / "app_catalog.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert {
        item["app_id"] for item in catalog["applications"]
    } == {
        "main_dashboard",
        "usda_dashboard",
        "oil_world_dashboard",
    }
    assert workspace.WORKSPACE_PAGES.count(
        workspace.IMPORT_PROFIT_ROUTE_ID
    ) == 1
    groups = {group.title: group.items for group in workspace.SIDEBAR_NAVIGATION}
    matching = [
        item
        for item in groups["研究工具"]
        if item.target == workspace.IMPORT_PROFIT_ROUTE_ID
    ]
    assert [(item.label, item.target, item.external_env) for item in matching] == [
        ("进口大豆榨利", "import_profit", None)
    ]
    assert workspace.IMPORT_PROFIT_PAGE_TITLE == "日度进口大豆盘面净榨利"


def test_route_environment_is_resolved_only_when_route_is_called(
    monkeypatch,
):
    calls = []
    monkeypatch.setenv("IMPORT_PROFIT_RUNTIME_ROOT", "RUNTIME")
    monkeypatch.setenv("IMPORT_PROFIT_CONFIG_PATH", "CONFIG")
    monkeypatch.setattr(
        workspace,
        "render_soybean_margin_page",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    workspace.render_import_profit_route()
    assert calls == [
        (("RUNTIME",), {})
    ]

    calls.clear()
    monkeypatch.setattr(workspace, "render_home", lambda *_: None)
    workspace.render_selected_workspace_page("首页")
    assert calls == []


def test_unconfigured_route_passes_none_and_main_app_degrades_safely(
    monkeypatch,
):
    calls = []
    monkeypatch.delenv("IMPORT_PROFIT_RUNTIME_ROOT", raising=False)
    monkeypatch.delenv("IMPORT_PROFIT_CONFIG_PATH", raising=False)
    monkeypatch.setattr(
        workspace,
        "render_soybean_margin_page",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    workspace.render_import_profit_route()
    assert calls[0][0] == (None,)
    assert calls[0][1] == {}

    app = AppTest.from_file(
        str(ROOT / "05_apps" / "streamlit_app.py"),
        default_timeout=30,
    ).run(timeout=30)
    app.session_state["selected_workspace_page"] = "import_profit"
    app.run(timeout=30)
    assert not app.exception
    assert any(
        "运行数据尚未配置" in item.value for item in app.info
    )


def test_module_source_has_no_runtime_read_or_deployment_side_effect():
    source = (
        ROOT / "05_apps" / "streamlit_app.py"
    ).read_text(encoding="utf-8")
    import_line = (
        "from soybean_margin_page import "
        "render_soybean_margin_page"
    )
    assert import_line in source
    assert "bootstrap_import_profit_runtime" not in source
    assert "update_runtime_cnf_quotes" not in source
    assert "docker" not in source.lower()
    assert "IMPORT_PROFIT_RUNTIME_ROOT" in source
    assert source.index("IMPORT_PROFIT_RUNTIME_ROOT") > source.index(
        "def render_import_profit_route"
    )
    importlib.reload(workspace)
