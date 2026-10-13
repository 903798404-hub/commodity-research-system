"""Consumer checks for the shared client-side navigation and old deep links."""
from __future__ import annotations

import importlib
from pathlib import Path
import sys
from urllib.parse import parse_qs

from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "05_apps"))
ENTRY = ROOT / "05_apps/streamlit_app.py"


def test_sidebar_and_home_links_share_the_registered_entry_and_original_routes(monkeypatch):
    monkeypatch.delenv("OIL_WORLD_DASHBOARD_URL", raising=False)
    nav = importlib.import_module("navigation")
    app = AppTest.from_file(str(ENTRY)).run()
    assert not app.exception
    expected = [item.target for group in nav.NAVIGATION_GROUPS for item in group.items if not item.external_env]
    sidebar = app.sidebar.get("page_link")
    home = app.main.get("page_link")
    assert [parse_qs(link.proto.query_string)["workspace_page"][0] for link in sidebar] == expected
    home_targets = [item.target for group in nav.research_navigation_groups() for item in group.items if not item.external_env]
    assert [parse_qs(link.proto.query_string)["workspace_page"][0] for link in home] == home_targets
    assert len({link.proto.page_script_hash for link in [*sidebar, *home]}) == 1
    assert all(link.proto.page == "" and not link.proto.external for link in [*sidebar, *home])
    markup = "\n".join(element.proto.body for element in app.get("html"))
    assert 'aria-disabled="true"' in markup
    assert 'href="?workspace_page=' not in markup
    monkeypatch.setenv("OIL_WORLD_DASHBOARD_URL", "https://example.com/oil-world/")
    app.run()
    assert not app.exception
    markup = "\n".join(element.proto.body for element in app.get("html"))
    assert 'href="https://example.com/oil-world/" target="_blank"' in markup
    assert 'aria-disabled="true"' not in markup


def test_valid_deep_links_dispatch_only_the_requested_module_and_keep_session(monkeypatch):
    workspace = importlib.import_module("streamlit_app")
    seen = []
    # The formal entry imports these same presentation functions.
    fx = importlib.import_module("foreign_fx_page")
    weekly = importlib.import_module("soybean_weekly_page")
    monkeypatch.setattr(fx, "render_foreign_fx_page", lambda **kwargs: seen.append("外盘汇率"))
    monkeypatch.setattr(weekly, "render_soybean_weekly_page", lambda: seen.append("美豆种植生长"))
    app = AppTest.from_file(str(ENTRY)).run()
    assert not app.exception
    app.session_state["navigation_consumer_sentinel"] = "existing-session"
    for route in ("外盘汇率", "美豆种植生长", "首页"):
        app.query_params["workspace_page"] = route
        app.run()
        assert not app.exception
        assert app.session_state.selected_workspace_page == route
        assert app.session_state["navigation_consumer_sentinel"] == "existing-session"
        assert "workspace_page" not in app.query_params
    assert seen == ["外盘汇率", "美豆种植生长"]
    assert set(workspace.WORKSPACE_PAGES) == set(importlib.import_module("navigation").internal_workspace_pages())


def test_unknown_route_cannot_change_the_selected_module():
    app = AppTest.from_file(str(ENTRY)).run()
    app.query_params["workspace_page"] = "unsupported-route"
    app.run()
    assert not app.exception
    assert app.session_state.selected_workspace_page == "首页"
