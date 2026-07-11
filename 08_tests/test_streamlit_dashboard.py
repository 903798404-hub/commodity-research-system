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
    / "01_data"
    / "database"
    / "basis"
    / "basis_quotes_sample.parquet"
)


def test_streamlit_entries_start_without_exceptions() -> None:
    for entry in (FORMAL_ENTRY, *PAGE_ENTRIES):
        app = AppTest.from_file(str(entry), default_timeout=15).run()
        assert not app.exception
        assert app.title


def test_report_catalog_cards_and_disabled_state() -> None:
    cards = json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
    app = AppTest.from_file(str(FORMAL_ENTRY), default_timeout=15).run()

    assert not app.exception
    rendered_titles = {title.value for title in app.markdown if title.value.startswith("#### ")}
    expected_titles = {f"#### {card['title']}" for card in cards}
    assert rendered_titles == expected_titles

    disabled_buttons = [button for button in app.button if button.label == "待接入"]
    enabled_buttons = [button for button in app.button if button.label == "打开"]
    assert len(disabled_buttons) == sum(not card["enabled"] for card in cards)
    assert len(enabled_buttons) == sum(card["enabled"] for card in cards)
    assert all(button.disabled for button in disabled_buttons)
    assert all(not button.disabled for button in enabled_buttons)


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
