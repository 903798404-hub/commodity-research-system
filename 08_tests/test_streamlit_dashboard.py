from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
from streamlit.testing.v1 import AppTest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FORMAL_ENTRY = PROJECT_ROOT / "05_apps" / "streamlit_app.py"
PAGE_ENTRIES = sorted((PROJECT_ROOT / "05_apps" / "pages").glob("*.py"))
CATALOG_FILE = PROJECT_ROOT / "02_configs" / "report_catalog.yaml"
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


def test_report_catalog_cards_and_disabled_state() -> None:
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
    assert external_cards_by_title["USDA 平衡表"]["url"] == "http://127.0.0.1:5173"
    oil_world_card = external_cards_by_title["Oil World 供需平衡表"]
    assert oil_world_card["description"] == "Oil World 大豆、菜籽、葵花籽及棕榈油市场年度供需数据"
    assert oil_world_card["url"] == "http://127.0.0.1:5175/"
    assert oil_world_card["url_env"] == "OIL_WORLD_DASHBOARD_URL"
    assert oil_world_card["enabled"] is True


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
