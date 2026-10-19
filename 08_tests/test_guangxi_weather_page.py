from __future__ import annotations

import importlib
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "05_apps"))


@pytest.mark.parametrize(("origin", "expected"), [
    ("http://research.example:8501/?workspace_page=x#private", "http://research.example/"),
    ("https://research.example:9443/workbench/", "https://research.example/"),
    ("http://localhost:8501/", "http://localhost:8856/"),
    ("http://127.0.0.1:8501/", "http://127.0.0.1:8856/"),
    ("http://[::1]:8501/", "http://[::1]:8856/"),
])
def test_service_uses_site_origin_without_forwarding_path_or_query(origin, expected):
    page = importlib.import_module("weather_research_page")
    assert page.weather_service_url(origin) == expected
    assert page.weather_service_url(origin, embedded=True) == expected + "?embed=true"


@pytest.mark.parametrize("origin", ["javascript:alert(1)", "//example.com/", "http://user:secret@example.com/"])
def test_invalid_origin_is_not_embedded(origin):
    with pytest.raises(ValueError):
        importlib.import_module("weather_research_page").weather_service_url(origin)


def test_main_weather_navigation_renders_the_existing_service_without_collection(monkeypatch):
    page = importlib.import_module("weather_research_page")
    monkeypatch.setattr(page.st, "context", SimpleNamespace(url="http://research.example:8501/"))
    app = AppTest.from_file(str(ROOT / "05_apps/streamlit_app.py"))
    app.query_params["workspace_page"] = "广西甘蔗天气"
    app.run(timeout=30)
    assert not app.exception
    assert app.session_state.selected_workspace_page == "广西甘蔗天气"
    assert [frame.proto.src for frame in app.get("iframe")] == ["http://research.example/?embed=true"]
    assert app.get("link_button")[0].proto.url == "http://research.example/"
    nav = importlib.import_module("navigation")
    group = next(group for group in nav.NAVIGATION_GROUPS if group.title == "天气研究")
    assert any(item.target == "广西甘蔗天气" for item in group.items)
