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


def _install_weather_route_fixture(monkeypatch):
    """Keep workspace routing deterministic without a host Public Current."""

    weather_page = importlib.import_module("weather_research_page")
    crop_page = importlib.import_module("crop_weather_page")

    def render_country(country: str) -> None:
        config = crop_page.load_weather_config(
            crop_page._country_files(country)["config"]
        )
        weather_page.st.title(str(config["page_title"]))
        enabled = dict(config.get("enabled_sections", {}))
        module_keys = [
            key
            for key in crop_page.MODULES
            if bool(enabled.get(key, key != "minimum_temperature"))
        ]
        weather_page.st.radio(
            "页面章节",
            module_keys,
            format_func=lambda key: crop_page.MODULES[key][0],
            key=f"required_route_fixture_{country}",
        )

    monkeypatch.setattr(
        weather_page,
        "WEATHER_COUNTRY_RENDERERS",
        {
            country: (lambda country=country: render_country(country))
            for country in crop_page.WEATHER_COUNTRY_FILES
        },
    )
    return weather_page, crop_page


def test_streamlit_entries_start_without_exceptions(monkeypatch) -> None:
    monkeypatch.setenv("USDA_DASHBOARD_URL", "http://127.0.0.1:5173/usda/")
    monkeypatch.setenv("OIL_WORLD_DASHBOARD_URL", "http://127.0.0.1:5175/oil-world/")
    for entry in (FORMAL_ENTRY, *PAGE_ENTRIES):
        app = AppTest.from_file(str(entry), default_timeout=15).run()
        assert not app.exception


def test_homepage_expands_the_authoritative_sidebar_navigation(monkeypatch) -> None:
    monkeypatch.setenv("USDA_DASHBOARD_URL", "http://127.0.0.1:5173/usda/")
    monkeypatch.setenv("OIL_WORLD_DASHBOARD_URL", "http://127.0.0.1:5175/oil-world/")
    home = _import_home()
    navigation = importlib.import_module("navigation")
    ui_theme = importlib.import_module("ui_theme")
    research_groups = navigation.research_navigation_groups()
    research_items = [item for group in research_groups for item in group.items]

    assert [item.label for item in research_items] == [
        "国际价差",
        "价差动态",
        "国内现货（基差与一口价）",
        "美豆周度跟踪",
        "大豆天气",
        "菜籽天气",
        "棕榈油天气",
        "印度作物天气",
        "USDA供需平衡",
        "Oil World供需平衡",
        "进口大豆榨利",
        "外资与重点席位",
        "运行监控",
    ]

    workspace = _import_workspace()
    app = AppTest.from_file(str(FORMAL_ENTRY), default_timeout=20).run()
    assert not app.exception
    home_source = (PROJECT_ROOT / "05_apps" / "home.py").read_text(encoding="utf-8")
    assert "最近访问" not in home_source
    assert "自定义首页" not in home_source
    assert "清除记录" not in home_source
    assert [group.title for group in workspace.SIDEBAR_NAVIGATION] == [
        "工作台", "市场行情", "周度跟踪", "天气研究", "国际供需", "研究工具"
    ]
    sidebar_research_items = [
        item for group in workspace.SIDEBAR_NAVIGATION if group.title != "工作台" for item in group.items
    ]
    assert research_items == sidebar_research_items
    assert set(workspace.WORKSPACE_PAGES) == {
        item.target
        for group in workspace.SIDEBAR_NAVIGATION
        for item in group.items
        if not item.external_env
    }

    cards_markup = "\n".join(ui_theme.render_navigation_card(item) for item in research_items)
    assert cards_markup.count('class="agri-card"') == 13
    assert cards_markup.count('class="agri-card-keyword"') == 20
    assert cards_markup.count('class="agri-card-detail-label"') == 5
    assert "?home_target=" not in cards_markup
    assert "?workspace_page=" in cards_markup
    assert '<strong class="agri-card-keyword">跨期价差</strong>' in cards_markup
    assert '<strong class="agri-card-keyword">现货基差</strong>' in cards_markup
    assert '<span class="agri-card-detail-label">包含：</span>' in cards_markup
    assert '<strong class="agri-card-keyword">种植进度</strong>' in cards_markup
    assert '<strong class="agri-card-keyword">美国</strong>' in cards_markup
    assert '<strong class="agri-card-keyword">棉花</strong>' in cards_markup
    for forbidden in (
        "需要关注", "数据状态概览", "核心研究入口", "最新业务日", "自动更新",
        "每周更新", "数据可用", "Cron", "天气数据更新至",
    ):
        assert forbidden not in home_source

    rendered_headers = []
    monkeypatch.setattr(home, "render_home_header", lambda **kwargs: rendered_headers.append(kwargs))
    monkeypatch.setattr(home, "render_section_heading", lambda *_args: None)
    monkeypatch.setattr(home, "render_home_footer", lambda *_args: None)
    monkeypatch.setattr(home.st, "html", lambda *_args: None)
    home.render_home()
    assert rendered_headers == [{
        "title": "农产品研究工作台",
        "researcher_name": "徐晓冉",
        "phone": "13305642778",
        "email": "xx20236@outlook.com",
    }]
    assert "Agricultural Commodities Research" not in home_source
    assert "汇集市场行情、国内现货、周度跟踪、作物天气与国际供需研究" not in home_source
    assert "&lt;script&gt;" in ui_theme._emphasize_keywords("<script>跨期价差</script>", ("跨期价差",))
    assert "<script>" not in ui_theme._emphasize_keywords("<script>跨期价差</script>", ("跨期价差",))

    details = {item.label: item.detail_text for item in research_items}
    assert details["美豆周度跟踪"] == "种植进度 · 生长状况 · 出口销售 · 出口装船"
    assert details["大豆天气"] == "美国 · 巴西 · 阿根廷"
    assert details["菜籽天气"] == "加拿大 · 澳大利亚 · 欧盟 · 俄罗斯 · 乌克兰"
    assert details["棕榈油天气"] == "印度尼西亚 · 马来西亚"
    assert details["印度作物天气"] == "棉花 · 甘蔗"


def test_weather_navigation_exposes_all_approved_soybean_countries(monkeypatch) -> None:
    workspace = _import_workspace()
    weather_page = importlib.import_module("weather_research_page")
    groups = {group.title: group.items for group in workspace.SIDEBAR_NAVIGATION}

    assert [(item.label, item.target, item.external_env) for item in groups["周度跟踪"]] == [
        ("美豆周度跟踪", workspace.SOYBEAN_CROP_PAGE_TITLE, None)
    ]
    assert [item.label for item in groups["天气研究"]] == [
        "大豆天气", "菜籽天气", "棕榈油天气", "印度作物天气"
    ]
    assert "美国大豆天气研究" not in [
        item.label for group in workspace.SIDEBAR_NAVIGATION for item in group.items
    ]
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
    for code, config_name in (
        ("CAN", "rapeseed_weather_can.yaml"),
        ("AUS", "rapeseed_weather_aus.yaml"),
        ("EU", "rapeseed_weather_eu.yaml"),
        ("RUS", "rapeseed_weather_rus.yaml"),
        ("UKR", "rapeseed_weather_ukr.yaml"),
    ):
        files = crop_page._country_files(code)
        assert files["config"].name == config_name
        assert "data" not in files
        assert crop_page._data_path(files) == (None, "Public Weather Current")

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
    assert crop_page._country_files("MYS")["config"].name == "palm_oil_weather_mys.yaml"
    for code in ("IDN", "MYS"):
        files = crop_page._country_files(code)
        assert "data" not in files
        assert crop_page._data_path(files) == (None, "Public Weather Current")

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


def test_palm_oil_weather_countries_render_from_the_main_workspace_route(monkeypatch) -> None:
    """Exercise the palm-oil selector through a deterministic route fixture."""

    _install_weather_route_fixture(monkeypatch)

    app = AppTest.from_file(str(FORMAL_ENTRY), default_timeout=30).run()
    app.session_state["selected_workspace_page"] = "棕榈油天气"
    app.run(timeout=30)
    assert not app.exception
    country_radio = next(item for item in app.radio if item.label == "国家/地区")
    assert country_radio.options == ["印度尼西亚", "马来西亚"]
    assert country_radio.value == "印度尼西亚"
    assert any(item.value == "印度尼西亚棕榈油天气研究" for item in app.title)
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
    monkeypatch.setenv(
        soybean_weather_page.PUBLIC_RUNTIME_ROOT_ENV,
        str(tmp_path / "missing-public-runtime"),
    )

    errors: list[str] = []
    monkeypatch.setattr(soybean_weather_page.st, "title", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(soybean_weather_page.st, "error", errors.append)
    soybean_weather_page.render_soybean_weather_page("BRA")
    error_text = "\n".join(errors)
    assert "巴西天气 Public Current 不可用" in error_text
    assert "未加载任何 legacy 或数据库回退" in error_text
    assert "该地区天气研究页面尚未接入" not in error_text


def test_soybean_country_files_are_isolated_and_other_weather_pages_stay_placeholder(monkeypatch) -> None:
    apps_dir = str(PROJECT_ROOT / "05_apps")
    if apps_dir not in sys.path:
        sys.path.insert(0, apps_dir)
    soybean_weather_page = importlib.import_module("soybean_weather_page")
    weather_page = importlib.import_module("weather_research_page")

    expected = {
        "USA": "soybean_weather_us.yaml",
        "BRA": "soybean_weather_br.yaml",
        "ARG": "soybean_weather_ar.yaml",
        "CAN": "rapeseed_weather_can.yaml",
        "AUS": "rapeseed_weather_aus.yaml",
        "EU": "rapeseed_weather_eu.yaml",
        "RUS": "rapeseed_weather_rus.yaml",
        "UKR": "rapeseed_weather_ukr.yaml",
    }
    for country, config_name in expected.items():
        files = soybean_weather_page._country_files(country)
        assert files["config"].name == config_name
        assert "data" not in files
        assert soybean_weather_page._data_path(files) == (
            None,
            "Public Weather Current",
        )

    notices: list[str] = []
    monkeypatch.setattr(weather_page.st, "title", lambda *_args, **_kwargs: None)
    rendered: list[str] = []
    monkeypatch.setattr(weather_page.st, "radio", lambda *_args, **_kwargs: "欧盟")
    monkeypatch.setattr(weather_page.st, "info", notices.append)
    monkeypatch.setattr(weather_page, "WEATHER_COUNTRY_RENDERERS", {"CAN": lambda: rendered.append("CAN"), "AUS": lambda: rendered.append("AUS"), "EU": lambda: rendered.append("EU"), "RUS": lambda: rendered.append("RUS"), "UKR": lambda: rendered.append("UKR")})
    weather_page.render_weather_research_page("rapeseed_weather")
    assert notices == []
    assert rendered == ["EU"]


def test_rapeseed_weather_countries_render_from_the_main_workspace_route(monkeypatch) -> None:
    """Exercise the real workspace route, not an isolated country renderer."""

    _install_weather_route_fixture(monkeypatch)

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

    assert home.get_external_url({"url_env": "USDA_DASHBOARD_URL", "url": "http://example.test"}) == ""
    assert home.get_external_app_url(PROJECT_ROOT / "02_configs" / "report_catalog.yaml", "USDA平衡表") == ""

    app = AppTest.from_file(str(FORMAL_ENTRY), default_timeout=20).run()
    assert not app.exception
    navigation = importlib.import_module("navigation")
    items_by_key = {
        item.key for group in navigation.research_navigation_groups() for item in group.items
    }
    assert "usda_dashboard" in items_by_key
    oil_world = next(
        item
        for group in navigation.research_navigation_groups()
        for item in group.items
        if item.key == "oil_world_dashboard"
    )
    ui_theme = __import__("ui_theme")
    assert 'aria-disabled="true"' in ui_theme.render_navigation_card(oil_world)


def test_internal_home_card_links_preserve_existing_route_targets(monkeypatch) -> None:
    monkeypatch.setenv("USDA_DASHBOARD_URL", "http://127.0.0.1:5173/usda/")
    monkeypatch.setenv("OIL_WORLD_DASHBOARD_URL", "http://127.0.0.1:5175/oil-world/")
    home = _import_home()
    navigation = importlib.import_module("navigation")
    item = next(
        item
        for group in navigation.research_navigation_groups()
        for item in group.items
        if item.key == "spreads_dashboard"
    )
    markup = __import__("ui_theme").render_navigation_card(item)
    assert '?workspace_page=%E4%BB%B7%E5%B7%AE%E5%8A%A8%E6%80%81%E7%9C%8B%E6%9D%BF' in markup
    assert item.target == "价差动态看板"

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
    apps_dir = str(PROJECT_ROOT / "05_apps")
    if apps_dir not in sys.path:
        sys.path.insert(0, apps_dir)
    from basis_page import filter_display_data

    expected_rows = len(filter_display_data(data))
    assert expected_rows == 2
