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
    assert statuses["crop_weather"].label == ""
    assert statuses["crop_weather"].detail == ""
    assert statuses["crop_weather"].latest_value.startswith("天气数据更新至 ")
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


def test_crop_weather_home_status_reads_runtime_parquet_and_degrades_without_data(monkeypatch) -> None:
    home = _import_home()
    home.load_home_statuses.clear()
    weather_observed = home._weather_observed_latest()
    statuses = home.get_home_statuses()

    expected = f"天气数据更新至 {weather_observed}"
    assert statuses["crop_weather"].latest_value == expected
    assert statuses["crop_weather"].attention == expected
    weather_module = next(module for module in home.home_modules() if module.module_id == "crop_weather")
    weather_markup = __import__("ui_theme").render_dashboard_card(weather_module, statuses["crop_weather"])
    assert "全球主产区" in weather_markup
    assert "大豆 · 菜籽 · 棕榈油 · 印度作物" in weather_markup
    assert expected in weather_markup
    for forbidden in ("历史快照", "非实时数据", "EC预测", "GFS预测"):
        assert forbidden not in weather_markup
    assert "SOYBEAN_WEATHER_FIXTURE" not in Path(home.__file__).read_text(encoding="utf-8")

    monkeypatch.setattr(home, "_weather_runtime_files", lambda: ())
    home.load_home_statuses.clear()
    degraded = home.get_home_statuses()["crop_weather"]
    assert degraded.label == ""
    assert degraded.detail == ""
    assert degraded.latest_value == "天气数据更新时间不可用"
    assert degraded.attention == "天气数据更新时间不可用"


def test_crop_weather_home_status_rejects_missing_runtime_observed_date(monkeypatch) -> None:
    home = _import_home()
    monkeypatch.setattr(home, "_weather_runtime_files", lambda: ())
    home.load_home_statuses.clear()
    status = home.get_home_statuses()["crop_weather"]
    assert status.latest_value == "天气数据更新时间不可用"
    assert status.attention == "天气数据更新时间不可用"


def test_crop_weather_home_status_uses_parquet_date_not_legacy_status_json(monkeypatch) -> None:
    home = _import_home()
    monkeypatch.setattr(home, "_weather_observed_latest", lambda: "2026-07-20")
    home.load_home_statuses.clear()

    status = home.get_home_statuses()["crop_weather"]
    assert status.latest_value == "天气数据更新至 2026-07-20"
    assert status.attention == status.latest_value


def test_weather_navigation_exposes_all_approved_soybean_countries(monkeypatch) -> None:
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
    assert weather_page.WEATHER_RESEARCH_PAGES["soybean_weather"]["country_options"] == {
        "美国": "USA", "巴西": "BRA", "阿根廷": "ARG"
    }
    assert weather_page._default_country_index(
        tuple((code, label) for label, code in weather_page.WEATHER_RESEARCH_PAGES["soybean_weather"]["country_options"].items()),
        frozenset({"USA"}),
    ) == 0

    rendered_countries: list[str] = []
    monkeypatch.setattr(weather_page, "render_weather_page", rendered_countries.append)
    monkeypatch.setattr(
        weather_page,
        "WEATHER_COUNTRY_RENDERERS",
        {country: (lambda country=country: weather_page.render_weather_page(country)) for country in ("USA", "BRA", "ARG", "CAN", "AUS", "EU", "RUS", "UKR")},
    )
    app = AppTest.from_file(str(FORMAL_ENTRY), default_timeout=20).run()
    app.session_state["selected_workspace_page"] = workspace.SOYBEAN_WEATHER_PAGE_TITLE
    app.run(timeout=20)
    country_radio = next(item for item in app.radio if item.label == "国家/地区")
    assert country_radio.value == "美国"
    assert country_radio.options == ["美国", "巴西", "阿根廷"]
    assert rendered_countries == ["USA"]

    country_radio.set_value("巴西").run(timeout=20)
    assert rendered_countries == ["USA", "BRA"]
    country_radio.set_value("阿根廷").run(timeout=20)
    assert rendered_countries == ["USA", "BRA", "ARG"]


def test_rapeseed_country_labels_use_one_explicit_code_mapping(monkeypatch) -> None:
    weather_page = importlib.import_module("weather_research_page")
    crop_page = importlib.import_module("crop_weather_page")
    page = weather_page.WEATHER_RESEARCH_PAGES["rapeseed_weather"]
    expected_options = {
        "加拿大": "CAN", "澳大利亚": "AUS", "欧盟": "EU", "俄罗斯": "RUS", "乌克兰": "UKR"
    }
    assert page["country_options"] == expected_options
    assert page["available_countries"] == frozenset({"CAN", "AUS", "EU", "RUS", "UKR"})
    assert set(weather_page.WEATHER_COUNTRY_RENDERERS) >= set(page["country_options"].values())
    for code, config_name, data_name, slug in (
        ("CAN", "rapeseed_weather_can.yaml", "rapeseed_weather_can.parquet", "can"),
        ("AUS", "rapeseed_weather_aus.yaml", "rapeseed_weather_aus.parquet", "aus"),
        ("EU", "rapeseed_weather_eu.yaml", "rapeseed_weather_eu.parquet", "eu"),
        ("RUS", "rapeseed_weather_rus.yaml", "rapeseed_weather_rus.parquet", "rus"),
        ("UKR", "rapeseed_weather_ukr.yaml", "rapeseed_weather_ukr.parquet", "ukr"),
    ):
        files = crop_page._country_files(code)
        assert files["config"].name == config_name
        assert files["data"].name == data_name
        assert files["data"].parent.name == slug

    monkeypatch.setattr(weather_page.st, "title", lambda *_args, **_kwargs: None)
    for label, code in expected_options.items():
        rendered: list[str] = []
        notices: list[str] = []
        monkeypatch.setattr(weather_page.st, "radio", lambda *_args, value=label, **_kwargs: value)
        monkeypatch.setattr(weather_page.st, "info", notices.append)
        monkeypatch.setattr(weather_page, "WEATHER_COUNTRY_RENDERERS", {key: (lambda key=key: rendered.append(key)) for key in expected_options.values()})
        weather_page.render_weather_research_page("rapeseed_weather")
        assert rendered == [code]
        assert notices == []


def test_palm_oil_countries_use_the_common_renderer_without_placeholder_or_cache_sharing(monkeypatch) -> None:
    weather_page = importlib.import_module("weather_research_page")
    crop_page = importlib.import_module("crop_weather_page")
    page = weather_page.WEATHER_RESEARCH_PAGES["palm_oil_weather"]
    expected_options = {"印度尼西亚": "IDN", "马来西亚": "MYS"}

    assert page["country_options"] == expected_options
    assert page["available_countries"] == frozenset({"IDN", "MYS"})
    assert set(weather_page.WEATHER_COUNTRY_RENDERERS) >= {"IDN", "MYS"}
    assert crop_page._country_files("IDN")["config"].name == "palm_oil_weather_idn.yaml"
    assert crop_page._country_files("IDN")["data"].name == "palm_oil_weather_idn.parquet"
    assert crop_page._country_files("MYS")["config"].name == "palm_oil_weather_mys.yaml"
    assert crop_page._country_files("MYS")["data"].name == "palm_oil_weather_mys.parquet"
    assert crop_page._country_files("IDN")["data"] != crop_page._country_files("MYS")["data"]

    monkeypatch.setattr(weather_page.st, "title", lambda *_args, **_kwargs: None)
    for label, code in expected_options.items():
        rendered: list[str] = []
        notices: list[str] = []
        monkeypatch.setattr(weather_page.st, "radio", lambda *_args, value=label, **_kwargs: value)
        monkeypatch.setattr(weather_page.st, "info", notices.append)
        monkeypatch.setattr(weather_page, "WEATHER_COUNTRY_RENDERERS", {key: (lambda key=key: rendered.append(key)) for key in expected_options.values()})
        weather_page.render_weather_research_page("palm_oil_weather")
        assert rendered == [code]
        assert notices == []


def test_palm_oil_weather_countries_render_from_the_main_workspace_route() -> None:
    """Exercise the data-backed palm-oil selector through the formal workspace entry."""

    app = AppTest.from_file(str(FORMAL_ENTRY), default_timeout=30).run()
    app.session_state["selected_workspace_page"] = "棕榈油天气"
    app.run(timeout=30)
    assert not app.exception
    country_radio = next(item for item in app.radio if item.label == "国家/地区")
    assert country_radio.options == ["印度尼西亚", "马来西亚"]
    assert country_radio.value == "印度尼西亚"
    assert any(item.value == "印度尼西亚棕榈油天气研究" for item in app.title)
    apps_dir = str(PROJECT_ROOT / "05_apps")
    if apps_dir not in sys.path:
        sys.path.insert(0, apps_dir)
    crop_page = importlib.import_module("crop_weather_page")
    idn_files = crop_page._country_files("IDN")
    idn_data = idn_files["data"]
    idn_config = crop_page.load_weather_config(idn_files["config"])
    idn_records = crop_page.load_weather_records(
        idn_data,
        crop=str(idn_config["crop"]),
        country=str(idn_config["country"]),
        metric="precipitation",
    )
    freshness = crop_page._weather_freshness(idn_records, idn_data)
    expected_caption = " · ".join(
        (
            f"观测更新至：{freshness['observed']}",
            f"EC预测至：{freshness['ecmwf']}",
            f"GFS预测至：{freshness['gfs']}",
            f"数据包刷新时间：{freshness['refreshed_at']}",
        )
    )
    assert any(
        item.value == expected_caption
        for item in app.caption
    )
    assert not any(
        forbidden in item.value
        for item in app.caption
        for forbidden in ("历史快照", "非实时数据", "EC 截止", "GFS 截止")
    )
    section_radio = next(item for item in app.radio if item.label == "页面章节")
    assert section_radio.options == ["单日降雨", "累计降雨", "最高气温", "土壤墒情"]
    assert not any("主产区加权" in item.value for item in app.caption)

    country_radio.set_value("马来西亚").run(timeout=30)
    assert not app.exception
    assert any(item.value == "马来西亚棕榈油天气研究" for item in app.title)
    section_radio = next(item for item in app.radio if item.label == "页面章节")
    assert section_radio.options == ["单日降雨", "累计降雨", "最高气温", "土壤墒情"]


def test_soybean_weather_missing_brazil_snapshot_is_data_degradation(monkeypatch, tmp_path: Path) -> None:
    apps_dir = str(PROJECT_ROOT / "05_apps")
    if apps_dir not in sys.path:
        sys.path.insert(0, apps_dir)
    soybean_weather_page = importlib.import_module("soybean_weather_page")
    brazil_files = dict(soybean_weather_page.WEATHER_COUNTRY_FILES["BRA"])
    monkeypatch.setitem(brazil_files, "data", tmp_path / "missing_brazil_snapshot.parquet")
    monkeypatch.setitem(soybean_weather_page.WEATHER_COUNTRY_FILES, "BRA", brazil_files)

    errors: list[str] = []
    monkeypatch.setattr(soybean_weather_page.st, "title", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(soybean_weather_page.st, "error", errors.append)
    soybean_weather_page.render_soybean_weather_page("BRA")
    error_text = "\n".join(errors)
    assert "巴西天气稳定数据不可用" in error_text
    assert "该地区天气研究页面尚未接入" not in error_text


def test_soybean_country_files_are_isolated_and_other_weather_pages_stay_placeholder(monkeypatch) -> None:
    apps_dir = str(PROJECT_ROOT / "05_apps")
    if apps_dir not in sys.path:
        sys.path.insert(0, apps_dir)
    soybean_weather_page = importlib.import_module("soybean_weather_page")
    weather_page = importlib.import_module("weather_research_page")

    expected = {
        "USA": ("soybean_weather_us.yaml", "soybean_weather_us.parquet", "us"),
        "BRA": ("soybean_weather_br.yaml", "soybean_weather_br.parquet", "br"),
        "ARG": ("soybean_weather_ar.yaml", "soybean_weather_ar.parquet", "ar"),
        "CAN": ("rapeseed_weather_can.yaml", "rapeseed_weather_can.parquet", "can"),
        "AUS": ("rapeseed_weather_aus.yaml", "rapeseed_weather_aus.parquet", "aus"),
        "EU": ("rapeseed_weather_eu.yaml", "rapeseed_weather_eu.parquet", "eu"),
        "RUS": ("rapeseed_weather_rus.yaml", "rapeseed_weather_rus.parquet", "rus"),
        "UKR": ("rapeseed_weather_ukr.yaml", "rapeseed_weather_ukr.parquet", "ukr"),
    }
    for country, (config_name, data_name, slug) in expected.items():
        files = soybean_weather_page._country_files(country)
        assert files["config"].name == config_name
        assert files["data"].name == data_name
        assert files["data"].parent.name == slug

    notices: list[str] = []
    monkeypatch.setattr(weather_page.st, "title", lambda *_args, **_kwargs: None)
    rendered: list[str] = []
    monkeypatch.setattr(weather_page.st, "radio", lambda *_args, **_kwargs: "欧盟")
    monkeypatch.setattr(weather_page.st, "info", notices.append)
    monkeypatch.setattr(weather_page, "WEATHER_COUNTRY_RENDERERS", {"CAN": lambda: rendered.append("CAN"), "AUS": lambda: rendered.append("AUS"), "EU": lambda: rendered.append("EU"), "RUS": lambda: rendered.append("RUS"), "UKR": lambda: rendered.append("UKR")})
    weather_page.render_weather_research_page("rapeseed_weather")
    assert notices == []
    assert rendered == ["EU"]


def test_rapeseed_weather_countries_render_from_the_main_workspace_route() -> None:
    """Exercise the real workspace route, not an isolated country renderer."""

    app = AppTest.from_file(str(FORMAL_ENTRY), default_timeout=30).run()
    app.session_state["selected_workspace_page"] = "菜籽天气"
    app.run(timeout=30)
    assert not app.exception
    country_radio = next(item for item in app.radio if item.label == "国家/地区")
    assert country_radio.options == ["加拿大", "澳大利亚", "欧盟", "俄罗斯", "乌克兰"]
    assert country_radio.value == "加拿大"
    assert any(item.value == "加拿大菜籽天气研究" for item in app.title)
    section_radio = next(item for item in app.radio if item.label == "页面章节")
    assert section_radio.options == ["降雨分析", "最高气温分析", "单日降雨", "累计降雨", "最高气温", "最低气温", "土壤墒情"]

    country_radio.set_value("澳大利亚").run(timeout=30)
    assert not app.exception
    assert any(item.value == "澳大利亚菜籽天气研究" for item in app.title)
    section_radio = next(item for item in app.radio if item.label == "页面章节")
    assert section_radio.options == ["单日降雨", "累计降雨", "最高气温", "最低气温", "土壤墒情"]
    assert not any("主产区加权" in item.value for item in app.caption)

    for country, title, sections in (
        ("欧盟", "欧盟菜籽天气研究", ["单日降雨", "累计降雨", "最高气温", "最低气温", "土壤墒情"]),
        ("俄罗斯", "俄罗斯菜籽天气研究", ["单日降雨", "累计降雨", "最高气温", "最低气温", "土壤墒情"]),
        ("乌克兰", "乌克兰菜籽天气研究", ["单日降雨", "累计降雨", "最高气温", "最低气温", "土壤墒情"]),
    ):
        country_radio = next(item for item in app.radio if item.label == "国家/地区")
        country_radio.set_value(country).run(timeout=30)
        assert not app.exception
        assert any(item.value == title for item in app.title)
        section_radio = next(item for item in app.radio if item.label == "页面章节")
        assert section_radio.options == sections


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
