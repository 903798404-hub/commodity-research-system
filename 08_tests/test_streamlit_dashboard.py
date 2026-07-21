from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.request
import importlib
from pathlib import Path

import pandas as pd
from streamlit.testing.v1 import AppTest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FORMAL_ENTRY = PROJECT_ROOT / "05_apps" / "streamlit_app.py"
PAGE_ENTRIES = sorted((PROJECT_ROOT / "05_apps" / "pages").glob("*.py"))
BASIS_SAMPLE_FILE = PROJECT_ROOT / "08_tests" / "fixtures" / "basis_quotes_sample.parquet"


def _import_home():
    apps_dir = str(PROJECT_ROOT / "05_apps")
    if apps_dir not in sys.path:
        sys.path.insert(0, apps_dir)
    import home

    return home


def _import_workspace():
    apps_dir = str(PROJECT_ROOT / "05_apps")
    if apps_dir not in sys.path:
        sys.path.insert(0, apps_dir)
    return importlib.import_module("streamlit_app")


def test_streamlit_entries_start_without_exceptions(monkeypatch) -> None:
    monkeypatch.setenv("USDA_DASHBOARD_URL", "http://127.0.0.1:5173/usda/")
    monkeypatch.setenv("OIL_WORLD_DASHBOARD_URL", "http://127.0.0.1:5175/oil-world/")
    for entry in (FORMAL_ENTRY, *PAGE_ENTRIES):
        app = AppTest.from_file(str(entry), default_timeout=15).run()
        assert not app.exception


def test_homepage_fixed_modules_states_and_no_deprecated_features(monkeypatch) -> None:
    monkeypatch.setenv("USDA_DASHBOARD_URL", "http://127.0.0.1:5173/usda/")
    monkeypatch.setenv("OIL_WORLD_DASHBOARD_URL", "http://127.0.0.1:5175/oil-world/")
    home = _import_home()
    home.load_home_statuses.clear()

    modules = home.home_modules()
    assert [module.title for module in modules] == [
        "市场价格",
        "国内现货",
        "美豆周度跟踪",
        "作物天气研究",
        "USDA 供需平衡",
        "Oil World 供需平衡",
    ]
    statuses = home.get_home_statuses()
    assert statuses["basis_domestic"].label == "人工维护"
    assert statuses["soybean_crop_progress"].label == "每周更新"
    assert statuses["crop_weather"].label == "历史快照"
    assert "美国大豆已接入" in statuses["crop_weather"].detail
    assert statuses["crop_weather"].latest_value.startswith("最新有效观测日期 ")
    assert statuses["foreign_seats"].label != "正常"

    workspace = _import_workspace()
    app = AppTest.from_file(str(FORMAL_ENTRY), default_timeout=20).run()
    assert not app.exception
    home_source = (PROJECT_ROOT / "05_apps" / "home.py").read_text(encoding="utf-8")
    assert "最近访问" not in home_source
    assert "自定义首页" not in home_source
    assert "清除记录" not in home_source
    assert [group for group, _items in workspace.SIDEBAR_NAVIGATION] == [
        "工作台", "市场行情", "周度跟踪", "天气研究", "国际供需", "研究工具"
    ]
    cards_markup = "\n".join(
        __import__("ui_theme").render_dashboard_card(module, statuses[module.module_id])
        for module in modules
    )
    assert cards_markup.count('class="agri-card"') == 6
    assert '?home_target=spreads_dashboard' in cards_markup
    assert '?home_target=basis_domestic' in cards_markup
    assert '?home_target=soybean_crop_progress' in cards_markup
    assert '?home_target=crop_weather' in cards_markup
    assert '?home_target=status' not in cards_markup
    assert "foreign_seats" not in [module.module_id for module in modules]
    assert "市场价格" in cards_markup
    assert "数据状态概览" in home_source
    assert "?home_target=status" in home_source
    assert home.PAGE_TARGETS["crop_weather"] == "大豆天气"
    assert home.PAGE_TARGETS["status"] == "运行监控"


def test_crop_weather_home_status_reads_real_json_and_degrades_without_fixture(monkeypatch, tmp_path: Path) -> None:
    home = _import_home()
    home.load_home_statuses.clear()
    status_file = home.SOYBEAN_WEATHER_STATUS_FILE
    raw_status = home._read_json(status_file)
    statuses = home.get_home_statuses()

    assert raw_status["observed_latest_date"] in statuses["crop_weather"].latest_value
    assert raw_status["ecmwf_forecast_end_date"] in statuses["crop_weather"].detail
    assert raw_status["gfs_forecast_end_date"] in statuses["crop_weather"].detail
    assert statuses["crop_weather"].attention == "作物天气：美国大豆已接入历史快照；其他国家和作物仍待接入稳定数据源。"
    weather_module = next(module for module in home.home_modules() if module.module_id == "crop_weather")
    weather_markup = __import__("ui_theme").render_dashboard_card(weather_module, statuses["crop_weather"])
    assert "全球主产区" in weather_markup
    assert "大豆 · 菜籽 · 棕榈油 · 印度作物" in weather_markup
    assert "SOYBEAN_WEATHER_FIXTURE" not in Path(home.__file__).read_text(encoding="utf-8")

    monkeypatch.setattr(home, "SOYBEAN_WEATHER_STATUS_FILE", tmp_path / "missing_weather_status.json")
    home.load_home_statuses.clear()
    degraded = home.get_home_statuses()["crop_weather"]
    assert degraded.label == "数据状态不可用"
    assert degraded.latest_value == "数据状态不可用"
    assert "无法读取" in degraded.detail


def test_weather_navigation_uses_crop_entries_and_controlled_country_placeholders(monkeypatch) -> None:
    workspace = _import_workspace()
    weather_page = importlib.import_module("weather_research_page")
    groups = {group: items for group, items in workspace.SIDEBAR_NAVIGATION}

    assert groups["周度跟踪"] == (("美豆周度跟踪", workspace.SOYBEAN_CROP_PAGE_TITLE, None),)
    assert [label for label, _target, _env in groups["天气研究"]] == [
        "大豆天气", "菜籽天气", "棕榈油天气", "印度作物天气"
    ]
    assert "美国大豆天气研究" not in [label for _group, items in workspace.SIDEBAR_NAVIGATION for label, _target, _env in items]
    assert workspace.WEATHER_PAGE_ROUTES == {
        "大豆天气": "soybean_weather",
        "菜籽天气": "rapeseed_weather",
        "棕榈油天气": "palm_oil_weather",
        "印度作物天气": "india_crop_weather",
    }
    assert weather_page.WEATHER_RESEARCH_PAGES["soybean_weather"]["countries"] == (
        ("USA", "美国"), ("BRA", "巴西"), ("ARG", "阿根廷")
    )
    assert weather_page._default_country_index(
        weather_page.WEATHER_RESEARCH_PAGES["soybean_weather"]["countries"], frozenset({"USA"})
    ) == 0

    usa_calls: list[str] = []
    monkeypatch.setattr(weather_page, "render_soybean_weather_page", lambda: usa_calls.append("USA"))
    app = AppTest.from_file(str(FORMAL_ENTRY), default_timeout=20).run()
    app.session_state["selected_workspace_page"] = workspace.SOYBEAN_WEATHER_PAGE_TITLE
    app.run(timeout=20)
    country_radio = next(item for item in app.radio if item.label == "国家/地区")
    assert country_radio.value == "美国"
    assert country_radio.options == ["美国", "巴西", "阿根廷"]
    assert usa_calls == ["USA"]

    country_radio.set_value("巴西").run(timeout=20)
    assert usa_calls == ["USA"]
    assert any(item.value == "该地区天气研究页面尚未接入。" for item in app.info)


def test_external_urls_are_environment_only_and_degrade_without_configuration(monkeypatch) -> None:
    home = _import_home()
    monkeypatch.delenv("USDA_DASHBOARD_URL", raising=False)
    monkeypatch.delenv("OIL_WORLD_DASHBOARD_URL", raising=False)
    home.load_home_statuses.clear()

    assert home.get_external_url({"url_env": "USDA_DASHBOARD_URL", "url": "http://example.test"}) == ""
    assert home.get_external_app_url(PROJECT_ROOT / "02_configs" / "report_catalog.yaml", "USDA平衡表") == ""
    statuses = home.get_home_statuses()
    assert statuses["usda_dashboard"].label == "暂不可用"
    assert statuses["oil_world_dashboard"].label == "暂不可用"

    app = AppTest.from_file(str(FORMAL_ENTRY), default_timeout=20).run()
    assert not app.exception
    modules_by_id = {module.module_id: module for module in home.home_modules()}
    ui_theme = __import__("ui_theme")
    assert 'aria-disabled="true"' in ui_theme.render_dashboard_card(
        modules_by_id["usda_dashboard"], statuses["usda_dashboard"]
    )
    assert 'aria-disabled="true"' in ui_theme.render_dashboard_card(
        modules_by_id["oil_world_dashboard"], statuses["oil_world_dashboard"]
    )


def test_internal_home_card_links_preserve_existing_route_targets(monkeypatch) -> None:
    monkeypatch.setenv("USDA_DASHBOARD_URL", "http://127.0.0.1:5173/usda/")
    monkeypatch.setenv("OIL_WORLD_DASHBOARD_URL", "http://127.0.0.1:5175/oil-world/")
    home = _import_home()
    home.load_home_statuses.clear()

    module = next(item for item in home.home_modules() if item.module_id == "spreads_dashboard")
    markup = __import__("ui_theme").render_dashboard_card(
        module, home.get_home_statuses()[module.module_id]
    )
    assert '?home_target=spreads_dashboard' in markup
    assert home.PAGE_TARGETS[module.module_id] == "价差动态看板"

    app = AppTest.from_file(str(FORMAL_ENTRY), default_timeout=20).run()
    app.session_state["selected_workspace_page"] = "价差动态看板"
    app.run()
    assert not app.exception
    assert app.session_state["selected_workspace_page"] == "价差动态看板"


def test_external_urls_support_explicit_environment_override(monkeypatch) -> None:
    home = _import_home()
    monkeypatch.setenv("USDA_DASHBOARD_URL", "http://127.0.0.1:5199/usda/")
    monkeypatch.setenv("OIL_WORLD_DASHBOARD_URL", "http://127.0.0.1:5198/oil-world/")

    assert home.get_external_url({"url_env": "USDA_DASHBOARD_URL"}) == "http://127.0.0.1:5199/usda/"
    assert (
        home.get_external_app_url(PROJECT_ROOT / "02_configs" / "report_catalog.yaml", "Oil World 供需平衡表")
        == "http://127.0.0.1:5198/oil-world/"
    )


def test_streamlit_server_starts_and_answers_http(monkeypatch) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

    environment = os.environ.copy()
    environment["USDA_DASHBOARD_URL"] = "http://127.0.0.1:5173/usda/"
    environment["OIL_WORLD_DASHBOARD_URL"] = "http://127.0.0.1:5175/oil-world/"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            str(FORMAL_ENTRY),
            "--server.headless=true",
            "--server.address=127.0.0.1",
            f"--server.port={port}",
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        health_body = ""
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError(f"Streamlit exited before startup with code {process.returncode}")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/_stcore/health", timeout=2) as response:
                    health_body = response.read().decode("utf-8")
                    if response.status == 200:
                        break
            except OSError:
                time.sleep(0.25)
        else:
            raise AssertionError("Streamlit did not answer its health endpoint")
        assert health_body == "ok"
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


def test_basis_page_reads_three_rows_and_formulas_are_correct() -> None:
    data = pd.read_parquet(BASIS_SAMPLE_FILE)
    assert len(data) == 3
    assert (data["basis"] == data["cash_price"] - data["futures_price"]).all()
    assert (data["month_spread"] == data["near_contract_price"] - data["far_contract_price"]).all()

    app = AppTest.from_file(str(FORMAL_ENTRY), default_timeout=15).run()
    app.session_state["selected_workspace_page"] = "基差/一口价"
    app.run()
    assert not app.exception
    assert any("共 3 行" in message.value for message in app.success)
