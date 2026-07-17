from __future__ import annotations

from contextlib import nullcontext
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pandas as pd
import yaml
from streamlit.testing.v1 import AppTest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FORMAL_ENTRY = PROJECT_ROOT / "05_apps" / "streamlit_app.py"
PAGE_ENTRIES = sorted((PROJECT_ROOT / "05_apps" / "pages").glob("*.py"))
CATALOG_FILE = PROJECT_ROOT / "02_configs" / "report_catalog.yaml"
APP_CATALOG_FILE = PROJECT_ROOT / "02_configs" / "app_catalog.yaml"
PRODUCTION_APPS = {
    app["app_id"]: app
    for app in yaml.safe_load(
        APP_CATALOG_FILE.read_text(encoding="utf-8")
    )["applications"]
}
PRODUCTION_OIL_WORLD_URL = PRODUCTION_APPS["oil_world_dashboard"]["production_url"]
BASIS_SAMPLE_FILE = (
    PROJECT_ROOT
    / "08_tests"
    / "fixtures"
    / "basis_quotes_sample.parquet"
)


def test_streamlit_entries_start_without_exceptions() -> None:
    for entry in (FORMAL_ENTRY, *PAGE_ENTRIES):
        app = AppTest.from_file(str(entry), default_timeout=15).run()
        assert not app.exception
        assert app.title


def test_streamlit_server_starts_and_answers_http() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

    environment = os.environ.copy()
    environment["OIL_WORLD_DASHBOARD_URL"] = PRODUCTION_OIL_WORLD_URL
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
                raise AssertionError(
                    f"Streamlit exited before startup with code {process.returncode}"
                )
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/_stcore/health",
                    timeout=2,
                ) as response:
                    health_body = response.read().decode("utf-8")
                    if response.status == 200:
                        break
            except OSError:
                time.sleep(0.25)
        else:
            raise AssertionError("Streamlit did not answer its health endpoint")

        assert health_body == "ok"
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/",
            timeout=5,
        ) as response:
            assert response.status == 200
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


def test_report_catalog_cards_and_disabled_state(monkeypatch) -> None:
    monkeypatch.setenv("OIL_WORLD_DASHBOARD_URL", PRODUCTION_OIL_WORLD_URL)
    cards = json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
    app = AppTest.from_file(str(FORMAL_ENTRY), default_timeout=30).run()

    assert not app.exception
    rendered_titles = {title.value for title in app.markdown if title.value.startswith("#### ")}
    expected_titles = {f"#### {card['title']}" for card in cards}
    assert rendered_titles == expected_titles

    disabled_buttons = [button for button in app.button if button.label == "待接入"]
    enabled_buttons = [button for button in app.button if button.label == "打开"]
    external_cards = [card for card in cards if card.get("type") == "external_app"]
    internal_enabled_cards = [
        card for card in cards if card["enabled"] and card.get("type") != "external_app"
    ]
    assert len(disabled_buttons) == sum(not card["enabled"] for card in cards)
    assert len(enabled_buttons) == len(internal_enabled_cards)
    assert all(button.disabled for button in disabled_buttons)
    assert all(not button.disabled for button in enabled_buttons)
    assert len(external_cards) == 2
    external_cards_by_title = {card["title"]: card for card in external_cards}
    usda_card = external_cards_by_title["USDA 平衡表"]
    assert usda_card["page_key"] == "usda_dashboard"
    assert usda_card["url"] == "http://127.0.0.1:5173"
    oil_world_card = external_cards_by_title["Oil World 供需平衡表"]
    assert oil_world_card["description"] == "Oil World 大豆、菜籽、葵花籽及棕榈油市场年度供需数据"
    assert oil_world_card["page_key"] == "oil_world_dashboard"
    assert oil_world_card["url"] == "http://127.0.0.1:5175/"
    assert oil_world_card["url_env"] == "OIL_WORLD_DASHBOARD_URL"
    assert oil_world_card["enabled"] is True


def test_north_america_planting_and_sales_catalog() -> None:
    import sys

    apps_dir = str(PROJECT_ROOT / "05_apps")
    if apps_dir not in sys.path:
        sys.path.insert(0, apps_dir)

    from home import (
        CATEGORY_ORDER,
        PAGE_TARGETS,
        catalog_column_count,
        catalog_component_key,
    )

    cards = json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
    north_america_cards = [
        card for card in cards if card.get("category") == "北美种植与销售"
    ]
    original_titles = [
        card["title"]
        for card in cards
        if card.get("category") != "北美种植与销售"
    ]

    assert CATEGORY_ORDER == ["价格", "价差", "供需", "北美种植与销售", "席位", "运行监控"]
    assert [card["title"] for card in north_america_cards] == [
        "美国种植",
        "美国销售",
        "加拿大销售",
    ]
    assert original_titles == [
        "国内现货基差、一口价、价差",
        "外盘现货价格",
        "价差动态看板",
        "USDA 平衡表",
        "Oil World 供需平衡表",
        "主力席位动向",
        "日更运行状态",
    ]
    assert catalog_column_count(len(north_america_cards)) == 3
    component_keys = [catalog_component_key(card) for card in cards]
    assert all(component_keys)
    assert len(component_keys) == len(set(component_keys))
    assert len({card["title"] for card in cards}) == len(cards)
    assert len(
        {
            PRODUCTION_APPS["main_dashboard"]["production_url"],
            PRODUCTION_APPS["usda_dashboard"]["production_url"],
            PRODUCTION_APPS["oil_world_dashboard"]["production_url"],
        }
    ) == 3

    planting, us_sales, canada_sales = north_america_cards
    assert planting == {
        "title": "美国种植",
        "category": "北美种植与销售",
        "commodities": "大豆",
        "region": "美国",
        "description": "美国大豆播种、生长进度与作物状况周度跟踪。",
        "page_key": "soybean_crop_progress",
        "enabled": True,
    }
    assert PAGE_TARGETS[planting["page_key"]] == "美豆种植生长"

    assert us_sales["description"] == "美国大豆出口销售、装运与未执行销售跟踪。"
    assert canada_sales["description"] == "加拿大菜籽出口销售与装运进度跟踪。"
    for card in (us_sales, canada_sales):
        assert card["enabled"] is False
        assert card["page_key"] not in PAGE_TARGETS
        assert "url" not in card
        assert card.get("type") != "external_app"

    app = AppTest.from_file(str(FORMAL_ENTRY), default_timeout=30).run()
    assert not app.exception
    assert [subheader.value for subheader in app.subheader if subheader.value in CATEGORY_ORDER] == CATEGORY_ORDER

    buttons_by_key = {button.key: button for button in app.button if button.key}
    planting_button = buttons_by_key["catalog_open_soybean_crop_progress"]
    assert planting_button.label == "打开"
    assert not planting_button.disabled
    assert buttons_by_key["catalog_disabled_us_soybean_sales"].disabled
    assert buttons_by_key["catalog_disabled_canada_canola_sales"].disabled

    planting_button.click().run()
    assert not app.exception
    assert app.radio[0].value == "美豆种植生长"
    assert any(title.value == "美豆种植生长" for title in app.title)


def test_usda_dashboard_card_supports_environment_url_override(monkeypatch) -> None:
    import sys

    apps_dir = str(PROJECT_ROOT / "05_apps")
    if apps_dir not in sys.path:
        sys.path.insert(0, apps_dir)

    from home import get_external_url

    card = {
        "type": "external_app",
        "url": "http://127.0.0.1:5173",
        "url_env": "USDA_DASHBOARD_URL",
    }
    assert get_external_url(card) == "http://127.0.0.1:5173"

    monkeypatch.setenv("USDA_DASHBOARD_URL", "http://127.0.0.1:5199")
    assert get_external_url(card) == "http://127.0.0.1:5199"


def test_oil_world_dashboard_card_supports_environment_url_override(monkeypatch) -> None:
    import sys

    apps_dir = str(PROJECT_ROOT / "05_apps")
    if apps_dir not in sys.path:
        sys.path.insert(0, apps_dir)

    from home import get_external_app_url, get_external_url

    card = {
        "type": "external_app",
        "url": "http://127.0.0.1:5175/",
        "url_env": "OIL_WORLD_DASHBOARD_URL",
    }
    assert get_external_url(card) == "http://127.0.0.1:5175/"
    assert (
        get_external_app_url(CATALOG_FILE, "Oil World 供需平衡表")
        == "http://127.0.0.1:5175/"
    )

    monkeypatch.setenv("OIL_WORLD_DASHBOARD_URL", "http://127.0.0.1:5198/oil-world/")
    assert get_external_url(card) == "http://127.0.0.1:5198/oil-world/"


def test_oil_world_card_uses_production_url_and_stable_component_key(monkeypatch) -> None:
    import sys

    apps_dir = str(PROJECT_ROOT / "05_apps")
    if apps_dir not in sys.path:
        sys.path.insert(0, apps_dir)

    from home import get_external_url, render_card

    cards = json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
    oil_world_card = next(
        card for card in cards if card["page_key"] == "oil_world_dashboard"
    )
    monkeypatch.setenv("OIL_WORLD_DASHBOARD_URL", PRODUCTION_OIL_WORLD_URL)

    link_calls: list[tuple[str, str, dict[str, object]]] = []
    monkeypatch.setattr("home.st.container", lambda **_: nullcontext())
    monkeypatch.setattr(
        "home.st.columns",
        lambda *_args, **_kwargs: [nullcontext(), nullcontext()],
    )
    monkeypatch.setattr("home.st.markdown", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("home.st.caption", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("home.st.write", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "home.st.link_button",
        lambda label, url, **kwargs: link_calls.append((label, url, kwargs)),
    )

    render_card(oil_world_card, 0)

    assert get_external_url(oil_world_card) == PRODUCTION_OIL_WORLD_URL
    assert link_calls == [
        (
            "打开",
            PRODUCTION_OIL_WORLD_URL,
            {
                "key": "catalog_external_oil_world_dashboard",
                "use_container_width": True,
            },
        )
    ]


def test_home_does_not_request_unreachable_oil_world_service(monkeypatch) -> None:
    unreachable_url = "http://127.0.0.1:1/oil-world/"
    monkeypatch.setenv("OIL_WORLD_DASHBOARD_URL", unreachable_url)

    app = AppTest.from_file(str(FORMAL_ENTRY), default_timeout=15).run()

    assert not app.exception
    assert any(
        title.value == "#### Oil World 供需平衡表"
        for title in app.markdown
    )


def test_workspace_navigation_includes_usda_entry_in_requested_order() -> None:
    import sys

    apps_dir = str(PROJECT_ROOT / "05_apps")
    if apps_dir not in sys.path:
        sys.path.insert(0, apps_dir)

    from home import get_external_app_url

    app = AppTest.from_file(str(FORMAL_ENTRY), default_timeout=15).run()

    assert app.radio[0].options == [
        "首页",
        "价差动态看板",
        "基差/一口价",
        "美豆种植生长",
        "USDA平衡表",
        "外资与重点席位",
        "运行监控",
    ]

    app.radio[0].set_value("USDA平衡表").run()
    assert not app.exception
    assert any(title.value == "USDA平衡表" for title in app.title)
    assert get_external_app_url(CATALOG_FILE, "USDA平衡表") == "http://127.0.0.1:5173"
    assert "Oil World 供需平衡表" not in app.radio[0].options


def test_basis_page_reads_three_rows_and_formulas_are_correct() -> None:
    data = pd.read_parquet(BASIS_SAMPLE_FILE)

    assert len(data) == 3
    assert (
        data["basis"] == data["cash_price"] - data["futures_price"]
    ).all()
    assert (
        data["month_spread"]
        == data["near_contract_price"] - data["far_contract_price"]
    ).all()

    app = AppTest.from_file(str(FORMAL_ENTRY), default_timeout=15).run()
    app.radio[0].set_value("基差/一口价").run()

    assert not app.exception
    assert any("共 3 行" in message.value for message in app.success)
