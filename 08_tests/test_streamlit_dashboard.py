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
        "USDA 供需平衡",
        "Oil World 供需平衡",
        "运行监控",
    ]
    statuses = home.get_home_statuses()
    assert statuses["basis_domestic"].label == "人工维护"
    assert statuses["soybean_crop_progress"].label == "每周更新"
    assert statuses["foreign_seats"].label != "正常"

    workspace = _import_workspace()
    app = AppTest.from_file(str(FORMAL_ENTRY), default_timeout=20).run()
    assert not app.exception
    home_source = (PROJECT_ROOT / "05_apps" / "home.py").read_text(encoding="utf-8")
    assert "最近访问" not in home_source
    assert "自定义首页" not in home_source
    assert "清除记录" not in home_source
    assert [group for group, _items in workspace.SIDEBAR_NAVIGATION] == [
        "工作台", "市场行情", "周度跟踪", "国际供需", "研究工具"
    ]
    cards_markup = "\n".join(
        __import__("ui_theme").render_dashboard_card(module, statuses[module.module_id])
        for module in modules
    )
    assert cards_markup.count('class="agri-card"') == 6
    assert '?home_target=spreads_dashboard' in cards_markup
    assert '?home_target=basis_domestic' in cards_markup
    assert '?home_target=soybean_crop_progress' in cards_markup
    assert '?home_target=status' in cards_markup
    assert "foreign_seats" not in [module.module_id for module in modules]
    assert "市场价格" in cards_markup
    assert "数据状态概览" in home_source


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
