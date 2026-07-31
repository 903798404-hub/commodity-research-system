from __future__ import annotations

from datetime import date
import hashlib
from pathlib import Path
import sys

import pandas as pd
import pytest


ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT / "03_src", ROOT / "05_apps"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

import import_profit_components as components
from agri_research_agent.import_profit import (
    CalculationStatus,
    load_soybean_config,
    map_soybean_contracts,
)
from agri_research_agent.import_profit.query import (
    QueryMetric,
    load_soybean_query_dataset,
)
from test_import_profit_query import joined_rows, write_dataset


CONFIG_PATH = ROOT / "02_configs" / "import_profit_soybean.yaml"
CONFIG = load_soybean_config(CONFIG_PATH)


def configured_rows(
    business_date: date,
    origin: str,
    shipment_year: int,
    shipment_month: int,
    **values,
):
    parts = list(
        joined_rows(
            business_date,
            origin,
            shipment_year,
            shipment_month,
            **values,
        )
    )
    mapped = map_soybean_contracts(
        CONFIG, shipment_year, shipment_month
    )
    parts[1].update(
        {
            "mapping_identity": mapped.mapping_identity,
            "parameter_version": str(CONFIG.schema_version),
            "cbot_contract_year": mapped.cbot.contract_year,
            "cbot_contract_month": mapped.cbot.contract_month,
            "soymeal_contract_code": mapped.soymeal.code,
            "soyoil_contract_code": mapped.soyoil.code,
        }
    )
    parts[2].update(
        {
            "mapping_identity": mapped.mapping_identity,
            "parameter_version": str(CONFIG.schema_version),
        }
    )
    return tuple(parts)


def page_records():
    records = []
    for origin in CONFIG.origin_codes:
        for month in range(1, 13):
            values = {
                "cnf": 100.0 + month,
                "usd": 400.0 + month,
                "duty": 3000.0 + month,
                "margin": -10.0 + month,
                "cbot": 1100.0 + month,
                "fx": 6.8 + month / 100,
                "soymeal": 2900.0 + month,
                "soyoil": 8100.0 + month,
            }
            if origin == "brazil" and month == 1:
                values["cnf"] = 0.0
            elif origin == "brazil" and month == 2:
                values["cnf"] = -2.5
            elif origin == "brazil" and month == 3:
                values["cnf"] = None
            elif origin == "brazil" and month == 4:
                values["fx"] = None
            elif origin == "brazil" and month == 5:
                values["soymeal"] = None
            elif origin == "brazil" and month == 6:
                values["soyoil"] = None
            records.append(
                configured_rows(
                    date(2026, 6, 25),
                    origin,
                    2026 if month >= 7 else 2027,
                    month,
                    **values,
                )
            )
    records.append(
        configured_rows(
            date(2026, 6, 10),
            "brazil",
            2026,
            12,
            cnf=None,
            cbot=1152.25,
            fx=6.691957,
            soymeal=2995.0,
            soyoil=8301.0,
        )
    )
    return records


def page_dataset(tmp_path: Path):
    paths = write_dataset(tmp_path, page_records())
    return load_soybean_query_dataset(*paths), paths


def test_origin_order_parameter_summary_and_official_table(tmp_path):
    dataset, _ = page_dataset(tmp_path)
    assert components.origin_options(CONFIG) == (
        ("brazil", "巴西"),
        ("us_gulf", "美湾"),
        ("us_pnw", "美西"),
        ("argentina", "阿根廷"),
    )
    summary = components.parameter_summary(CONFIG, "brazil")
    assert summary == (
        "出粕率78.5%｜出油率18.5%｜换算系数0.367437｜"
        "关税3%｜增值税9%｜港杂费100元/吨｜加工费150元/吨"
    )
    records = components.records_for_date(
        dataset, origin="brazil", business_date=date(2026, 6, 25)
    )
    table = components.formal_daily_table(
        records,
        business_date=date(2026, 6, 25),
        origin="brazil",
        config=CONFIG,
    )
    assert len(table) == 12
    assert table["船期"].tolist() == [
        *(f"2027-{month:02d}" for month in range(1, 7)),
        *(f"2026-{month:02d}" for month in range(7, 13)),
    ]
    assert table.loc[0, "CNF升贴水"] == "0"
    assert table.loc[1, "CNF升贴水"] == "-2.50"
    assert table.loc[2, "CNF升贴水"] == "—"
    assert table.loc[6, "美元成本"] == "407.00"
    assert table.loc[6, "CBOT日度价格"] == "1107.00"
    assert table.loc[6, "远期汇率"] == "6.870000"
    assert table.loc[6, "豆粕盘面"] == "2907.00"
    assert table.loc[6, "豆油盘面"] == "8107.00"
    assert "盘面净榨利" in table
    assert components.date_status_counts(records) == (8, 4)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (100, 100.0),
        (-1.25, -1.25),
        (0, 0.0),
        (None, None),
        (pd.NA, None),
        (float("nan"), None),
        ("", None),
        ("  ", None),
    ],
)
def test_cnf_editor_accepts_finite_numbers_zero_negative_and_clear(
    value, expected
):
    assert components.parse_cnf_editor_value(value) == expected


@pytest.mark.parametrize(
    "value", [True, False, "100", "invalid", float("inf"), float("-inf")]
)
def test_cnf_editor_rejects_bool_text_and_infinity(value):
    with pytest.raises(components.InvalidCnfPreviewError):
        components.parse_cnf_editor_value(value)


def test_missing_cnf_preview_delegates_to_existing_calculator(
    tmp_path, monkeypatch
):
    dataset, _ = page_dataset(tmp_path)
    record = dataset.get(
        business_date=date(2026, 6, 10),
        origin="brazil",
        shipment_year=2026,
        shipment_month=12,
    )
    assert record is not None
    calls = []
    real_calculator = components.calculate_soybean_net_crush_margin

    def spy(calculation_input, config):
        calls.append(calculation_input)
        return real_calculator(calculation_input, config)

    monkeypatch.setattr(
        components, "calculate_soybean_net_crush_margin", spy
    )
    preview = components.calculate_cnf_preview(record, 100, CONFIG)

    assert len(calls) == 1
    assert calls[0].cnf_cents_per_bushel == 100.0
    assert calls[0].cbot_daily_price_cents_per_bushel == 1152.25
    assert calls[0].fx_value == 6.691957
    assert calls[0].soymeal_price_cny_per_tonne == 2995.0
    assert calls[0].soyoil_price_cny_per_tonne == 8301.0
    assert preview.preview_status == CalculationStatus.SUCCESS.value
    assert preview.missing_reasons == ()
    assert preview.usd_cost_per_tonne == pytest.approx(
        (1152.25 + 100.0) * 0.367437
    )
    assert record.cnf_cents_per_bushel is None
    assert record.usd_cost_per_tonne is None


@pytest.mark.parametrize("cnf_value", [125.0, -25.5, 0.0])
def test_positive_negative_and_zero_cnf_all_produce_preview(
    tmp_path, cnf_value
):
    dataset, _ = page_dataset(tmp_path)
    record = dataset.get(
        business_date=date(2026, 6, 10),
        origin="brazil",
        shipment_year=2026,
        shipment_month=12,
    )
    assert record is not None
    preview = components.calculate_cnf_preview(
        record, cnf_value, CONFIG
    )
    assert preview.cnf_cents_per_bushel == cnf_value
    assert preview.preview_status == "success"
    assert preview.usd_cost_per_tonne is not None
    assert preview.duty_paid_cost_cny_per_tonne is not None
    assert preview.net_crush_margin_cny_per_tonne is not None


def test_missing_cnf_and_fx_keeps_preview_incomplete_after_cnf_entry(
    tmp_path,
):
    parts = configured_rows(
        date(2026, 6, 10),
        "brazil",
        2026,
        12,
        cnf=None,
        fx=None,
    )
    dataset = load_soybean_query_dataset(
        *write_dataset(tmp_path, [parts])
    )
    record = dataset.records[0]
    assert record.missing_reasons == ("missing_cnf", "missing_fx")
    preview = components.calculate_cnf_preview(record, 100, CONFIG)
    assert preview.preview_status == "incomplete"
    assert preview.missing_reasons == ("missing_fx",)


@pytest.mark.parametrize(
    ("month", "reason"),
    [(4, "missing_fx"), (5, "missing_soymeal"), (6, "missing_soyoil")],
)
def test_preview_remains_incomplete_when_other_market_input_is_missing(
    tmp_path, month, reason
):
    dataset, _ = page_dataset(tmp_path)
    records = components.records_for_date(
        dataset, origin="brazil", business_date=date(2026, 6, 25)
    )
    record = records[month - 1]
    assert record is not None
    preview = components.calculate_cnf_preview(record, 100, CONFIG)
    assert preview.preview_status == "incomplete"
    assert preview.missing_reasons == (reason,)
    assert preview.usd_cost_per_tonne is None
    assert preview.duty_paid_cost_cny_per_tonne is None
    assert preview.net_crush_margin_cny_per_tonne is None


def test_clear_cnf_returns_incomplete_and_editor_changes_only_selected_key(
    tmp_path,
):
    dataset, _ = page_dataset(tmp_path)
    records = components.records_for_date(
        dataset, origin="brazil", business_date=date(2026, 6, 25)
    )
    editor = components.editable_daily_table(
        records, business_date=date(2026, 6, 25)
    )
    editor.loc[editor["船期"] == "2026-12", "CNF升贴水"] = None
    previews = components.previews_from_editor(records, editor, CONFIG)
    assert tuple(previews) == ("2026-12",)
    assert previews["2026-12"].preview_status == "incomplete"
    assert previews["2026-12"].missing_reasons == ("missing_cnf",)
    assert records[-1] is not None
    assert records[-1].cnf_cents_per_bushel == 112.0


def test_invalid_editor_payload_does_not_mutate_last_legal_state(tmp_path):
    dataset, _ = page_dataset(tmp_path)
    records = components.records_for_date(
        dataset, origin="brazil", business_date=date(2026, 6, 25)
    )
    state = {}
    components.initialize_preview_state(
        state,
        origin="brazil",
        business_date=date(2026, 6, 25),
        records=records,
    )
    state[components.STATE_PREVIEWS] = {"old": "kept"}
    state[components.STATE_LAST_LEGAL_EDIT] = {"2026-12": 90.0}
    editor = components.editable_daily_table(
        records, business_date=date(2026, 6, 25)
    )
    editor["CNF升贴水"] = editor["CNF升贴水"].astype(object)
    editor.loc[11, "CNF升贴水"] = "not-a-number"
    with pytest.raises(components.InvalidCnfPreviewError):
        components.previews_from_editor(records, editor, CONFIG)
    assert state[components.STATE_PREVIEWS] == {"old": "kept"}
    assert state[components.STATE_LAST_LEGAL_EDIT] == {"2026-12": 90.0}


def test_context_switch_and_restore_reset_only_page_preview_state(tmp_path):
    dataset, _ = page_dataset(tmp_path)
    records = components.records_for_date(
        dataset, origin="brazil", business_date=date(2026, 6, 25)
    )
    state = {"unrelated": "keep"}
    components.initialize_preview_state(
        state,
        origin="brazil",
        business_date=date(2026, 6, 25),
        records=records,
    )
    first_version = state[components.STATE_EDITOR_VERSION]
    state[components.STATE_PREVIEWS] = {"2026-12": "preview"}
    components.initialize_preview_state(
        state,
        origin="us_gulf",
        business_date=date(2026, 6, 25),
        records=records,
    )
    assert state[components.STATE_PREVIEWS] == {}
    assert state[components.STATE_EDITOR_VERSION] == first_version + 1
    state[components.STATE_PREVIEWS] = {"2026-12": "preview"}
    components.restore_original_preview_state(state)
    assert state[components.STATE_PREVIEWS] == {}
    assert state["unrelated"] == "keep"


def test_prepared_matrices_and_seasonality_keep_fixed_structures(tmp_path):
    dataset, _ = page_dataset(tmp_path)
    prepared = components.prepare_page_data(
        dataset, origin="brazil", business_date=date(2026, 6, 25)
    )
    assert len(prepared.records) == 12
    assert tuple(metric for metric, _ in prepared.matrices) == tuple(
        item[0] for item in components.METRIC_SECTIONS
    )
    for metric, matrix in prepared.matrices:
        frame = components.matrix_display_frame(matrix)
        assert frame.shape == (10, 13)
        assert frame.iloc[0, 0] == "2026-06-25"
        assert list(frame.columns[1:]) == [
            f"{month}月船期" for month in range(1, 13)
        ]
        if metric is QueryMetric.CNF:
            assert frame.iloc[0, 1] == "0"
    for metric, seasonal_items in prepared.seasonal:
        assert len(seasonal_items) == 12
        figure = components.seasonality_figure(
            seasonal_items[0],
            value_label=(
                "美分/蒲式耳" if metric is QueryMetric.CNF else "元/吨"
            ),
        )
        assert len(figure.data) == 7
        assert all(trace.connectgaps is False for trace in figure.data)
        assert all(trace.mode == "lines" for trace in figure.data)
        assert figure.data[0].line.width > figure.data[1].line.width
        assert figure.data[-1].name == "前五年均值"
        assert figure.data[-1].line.dash == "dash"
        assert figure.layout.xaxis.tickformat == "%m-%d"
    assert components.current_window_message(
        prepared.seasonality(QueryMetric.CNF)[0],
        date(2026, 6, 25),
    ) == "当前船期的观察窗口尚未开始。"


def test_loading_and_preparation_do_not_modify_input_parquet(tmp_path):
    dataset, paths = page_dataset(tmp_path)
    before = [
        hashlib.sha256(path.read_bytes()).hexdigest() for path in paths
    ]
    components.prepare_page_data(
        dataset, origin="brazil", business_date=date(2026, 6, 25)
    )
    after = [
        hashlib.sha256(path.read_bytes()).hexdigest() for path in paths
    ]
    assert before == after


def test_page_components_do_not_copy_formulas_or_call_storage():
    page_source = (ROOT / "05_apps" / "import_profit_page.py").read_text(
        encoding="utf-8"
    )
    component_source = (
        ROOT / "05_apps" / "import_profit_components.py"
    ).read_text(encoding="utf-8")
    combined = page_source + component_source
    assert "upsert_cnf_quotes" not in combined
    assert "read_parquet" not in combined
    assert "pyarrow.parquet" not in combined
    assert "cents_per_bushel_to_usd_per_tonne" not in page_source
    assert "meal_yield" not in page_source
    assert "oil_yield" not in page_source
    assert combined.count("calculate_soybean_net_crush_margin(") == 1
    for forbidden in (
        "gross_crush_margin",
        "meal_cost",
        "oil_cost",
        "meal_breakeven_price",
        "oil_breakeven_price",
    ):
        assert forbidden not in combined
