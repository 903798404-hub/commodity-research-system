from __future__ import annotations

import importlib
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
for directory in (PROJECT_ROOT / "03_src", PROJECT_ROOT / "04_scripts", PROJECT_ROOT / "05_apps"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))


def test_legacy_weather_modules_are_compatibility_aliases_for_crop_neutral_implementations() -> None:
    canonical_core = importlib.import_module("agri_research_agent.weather.crop_weather")
    legacy_core = importlib.import_module("agri_research_agent.weather.soybean_weather")
    canonical_page = importlib.import_module("crop_weather_page")
    legacy_page = importlib.import_module("soybean_weather_page")
    canonical_snapshot = importlib.import_module("import_crop_weather_snapshot")
    legacy_snapshot = importlib.import_module("import_soybean_weather_snapshot")
    canonical_normal = importlib.import_module("import_crop_weather_30y_normal")
    legacy_normal = importlib.import_module("import_soybean_weather_30y_normal")

    assert legacy_core.load_weather_config is canonical_core.load_weather_config
    assert legacy_page.render_weather_page is canonical_page.render_weather_page
    assert legacy_page.render_soybean_weather_page is canonical_page.render_soybean_weather_page
    assert legacy_snapshot.import_snapshot is canonical_snapshot.import_snapshot
    assert legacy_normal.import_workbook is canonical_normal.import_workbook


def test_main_weather_route_imports_the_crop_neutral_page() -> None:
    source = (PROJECT_ROOT / "05_apps" / "weather_research_page.py").read_text(encoding="utf-8")
    assert "from crop_weather_page import render_weather_page" in source
    assert "from soybean_weather_page" not in source
