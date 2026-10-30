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
    ("http://research.example:8501/?token=private#fragment", "http://research.example:8858/"),
    ("https://research.example/workbench/", "https://research.example/yunnan-weather/"),
    ("https://124.222.106.25/?private=yes", "https://124.222.106.25/yunnan-weather/"),
    ("http://localhost:8501/", "http://localhost:8858/"),
    ("http://[::1]:8501/", "http://[::1]:8858/"),
])
def test_yunnan_reader_origin_preserves_host_and_discards_private_parts(origin, expected):
    page = importlib.import_module("weather_research_page")
    assert page.yunnan_weather_service_url(origin) == expected
    assert page.yunnan_weather_service_url(origin, embedded=True) == expected + "?embed=true"


@pytest.mark.parametrize("origin", ["javascript:alert(1)", "//example.com/", "http://user:secret@example.com/"])
def test_yunnan_reader_rejects_invalid_origins(origin):
    with pytest.raises(ValueError):
        importlib.import_module("weather_research_page").yunnan_weather_service_url(origin)


def test_yunnan_navigation_reads_dedicated_service_without_collection(monkeypatch):
    page = importlib.import_module("weather_research_page")
    monkeypatch.setattr(page.st, "context", SimpleNamespace(url="http://research.example:8501/"))
    app = AppTest.from_file(str(ROOT / "05_apps/streamlit_app.py"))
    app.query_params["workspace_page"] = "云南甘蔗天气"
    app.run(timeout=30)
    assert not app.exception
    assert app.session_state.selected_workspace_page == "云南甘蔗天气"
    assert [frame.proto.src for frame in app.get("iframe")] == ["http://research.example:8858/?embed=true"]
    assert app.get("link_button")[0].proto.url == "http://research.example:8858/"
    nav = importlib.import_module("navigation")
    group = next(group for group in nav.NAVIGATION_GROUPS if group.title == "天气研究")
    assert [item.target for item in group.items][-2:] == ["广西甘蔗天气", "云南甘蔗天气"]
