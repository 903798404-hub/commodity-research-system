from __future__ import annotations

from datetime import date, datetime, timezone
import importlib
import pathlib
from pathlib import Path
import sys

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest


ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT / "03_src", ROOT / "05_apps"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

import import_profit_page
from agri_research_agent.import_profit.cnf_repricing import (
    reprice_query_record_with_manual_cnf,
)
from agri_research_agent.import_profit.query import (
    HISTORICAL_BUSINESS_KEY_SCHEMA,
    load_soybean_query_dataset,
)
from agri_research_agent.import_profit.result_store import (
    RESULT_SCHEMA,
    SNAPSHOT_SCHEMA,
)
from test_import_profit_components import (
    CONFIG,
    CONFIG_PATH,
    configured_rows,
    page_records,
)
from test_import_profit_query import write_dataset


def _app_script(paths, config_path=CONFIG_PATH, *, allow_preview=True):
    return f"""
from pathlib import Path
from import_profit_page import ImportProfitPageDataPaths, render_import_profit_page

render_import_profit_page(
    ImportProfitPageDataPaths(
        Path({str(paths[0])!r}),
        Path({str(paths[1])!r}),
        Path({str(paths[2])!r}),
    ),
    config_path=Path({str(config_path)!r}),
    allow_cnf_preview={allow_preview!r},
)
"""


def _page_paths(tmp_path):
    return write_dataset(tmp_path, page_records())


def _texts(elements):
    return [str(item.value) for item in elements]


def test_module_import_has_no_render_or_session_side_effect(
    monkeypatch,
):
    def unexpected(*_args, **_kwargs):
        raise AssertionError("rendering must not happen at module import")

    monkeypatch.setattr(st, "title", unexpected)
    monkeypatch.setattr(st, "markdown", unexpected)
    monkeypatch.setattr(st, "info", unexpected)
    monkeypatch.setattr(st, "error", unexpected)
    importlib.reload(import_profit_page)


def test_public_interface_and_cache_identity_include_file_changes(tmp_path):
    assert import_profit_page.PAGE_TITLE == "日度进口大豆盘面净榨利"
    assert callable(import_profit_page.render_import_profit_page)
    assert callable(import_profit_page.render_import_profit_page_from_dataset)
    paths = _page_paths(tmp_path)
    model = import_profit_page.ImportProfitPageDataPaths(*paths)
    assert model.paths == tuple(pathlib.Path(path) for path in paths)
    first = import_profit_page._file_signature(paths[0])
    paths[0].touch()
    second = import_profit_page._file_signature(paths[0])
    assert first[0] == second[0]
    assert first[1:] != second[1:]


def test_complete_page_renders_controls_tables_tabs_and_figures(tmp_path):
    paths = _page_paths(tmp_path)
    app = AppTest.from_string(
        _app_script(paths), default_timeout=40
    ).run(timeout=40)

    assert not app.exception
    assert _texts(app.title) == ["日度进口大豆盘面净榨利"]
    markdown = "\n".join(_texts(app.markdown))
    captions = "\n".join(_texts(app.caption))
    infos = "\n".join(_texts(app.info))
    assert "CBOT日度价格" in markdown
    assert "扣除港杂费和加工费后的盘面净榨利" in markdown
    assert "无CNF或必要行情时" in captions
    assert "尚未写入正式数据" in infos

    assert app.selectbox[0].label == "产地"
    assert app.selectbox[0].options == ["巴西", "美湾", "美西", "阿根廷"]
    assert app.selectbox[0].value == "brazil"
    assert app.date_input[0].value == date(2026, 6, 25)
    assert len(app.metric) == 5
    assert any("总记录数" == item.label for item in app.metric)

    assert len(app.dataframe) == 5
    official = app.dataframe[0].value
    assert len(official) == 12
    assert official["船期"].tolist() == [
        *(f"2027-{month:02d}" for month in range(1, 7)),
        *(f"2026-{month:02d}" for month in range(7, 13)),
    ]
    assert official.loc[0, "CNF升贴水"] == "0"
    assert official.loc[1, "CNF升贴水"] == "-2.50"
    assert official.loc[6, "美元成本"] == "407.00"
    assert official.loc[6, "CBOT日度价格"] == "1107.00"
    assert official.loc[6, "远期汇率"] == "6.870000"
    assert official.loc[6, "豆粕盘面"] == "2907.00"
    assert official.loc[6, "豆油盘面"] == "8107.00"

    assert [tab.label for tab in app.tabs] == [
        "CNF报价",
        "完税成本",
        "净榨利",
        "CNF季节性",
        "到港完税成本季节性",
        "盘面净榨利季节性",
    ]
    assert len(app.dataframe[1].value) == 12
    assert all(
        frame.value.shape == (10, 13) for frame in app.dataframe[2:]
    )
    assert len(app.get("plotly_chart")) == 36
    assert {button.label for button in app.button} == {
        "重新计算预览",
        "恢复原始CNF",
    }
    visible_text = "\n".join(
        _texts(app.title)
        + _texts(app.markdown)
        + _texts(app.caption)
        + _texts(app.info)
        + _texts(app.warning)
    )
    assert "保存CNF" not in visible_text
    assert "正式提交" not in visible_text
    assert "写入成功" not in visible_text


def test_weekend_selection_is_rejected_without_date_fallback(tmp_path):
    paths = _page_paths(tmp_path)
    app = AppTest.from_string(
        _app_script(paths), default_timeout=40
    ).run(timeout=40)
    app.date_input[0].set_value(date(2026, 6, 21))
    app.run(timeout=40)
    assert not app.exception
    assert app.date_input[0].value == date(2026, 6, 21)
    assert any("周一至周五" in item.value for item in app.error)
    assert not app.dataframe


def test_all_null_matrices_still_render_complete_tables_and_empty_states(
    tmp_path,
):
    records = [
        configured_rows(
            date(2026, 6, 25),
            "brazil",
            2026 if month >= 7 else 2027,
            month,
            cnf=None,
        )
        for month in range(1, 13)
    ]
    paths = write_dataset(tmp_path, records)
    app = AppTest.from_string(
        _app_script(paths), default_timeout=40
    ).run(timeout=40)
    assert not app.exception
    assert all(
        frame.value.shape == (10, 13) for frame in app.dataframe[2:]
    )
    infos = "\n".join(_texts(app.info))
    assert "CNF历史报价在所选窗口内全部为空" in infos
    assert "进口大豆完税成本在所选窗口内全部为空" in infos
    assert "盘面净榨利在所选窗口内全部为空" in infos
    assert len(app.get("plotly_chart")) == 36


def test_missing_files_degrade_safely_without_absolute_path(tmp_path):
    paths = (
        tmp_path / "historical_business_keys.parquet",
        tmp_path / "historical_soybean_market_snapshots.parquet",
        tmp_path / "historical_soybean_net_crush_results.parquet",
    )
    app = AppTest.from_string(
        _app_script(paths), default_timeout=10
    ).run(timeout=10)
    assert not app.exception
    assert len(app.error) == 1
    message = app.error[0].value
    assert "historical_business_keys.parquet" in message
    assert str(tmp_path) not in message


def test_schema_error_degrades_without_stack_or_path(tmp_path):
    paths = _page_paths(tmp_path)
    fields = list(SNAPSHOT_SCHEMA)
    broken_schema = pa.schema([fields[1], fields[0], *fields[2:]])
    table = pq.read_table(paths[1])
    pq.write_table(
        pa.Table.from_arrays(
            [table.column(name) for name in broken_schema.names],
            schema=broken_schema,
        ),
        paths[1],
    )
    app = AppTest.from_string(
        _app_script(paths), default_timeout=10
    ).run(timeout=10)
    assert not app.exception
    assert len(app.error) == 1
    assert "结构校验失败" in app.error[0].value
    assert str(tmp_path) not in app.error[0].value
    assert "Traceback" not in app.error[0].value


def test_empty_dataset_degrades_as_normal_business_state(tmp_path):
    paths = (
        tmp_path / "historical_business_keys.parquet",
        tmp_path / "historical_soybean_market_snapshots.parquet",
        tmp_path / "historical_soybean_net_crush_results.parquet",
    )
    tmp_path.mkdir(parents=True, exist_ok=True)
    for path, schema in zip(
        paths,
        (
            HISTORICAL_BUSINESS_KEY_SCHEMA,
            SNAPSHOT_SCHEMA,
            RESULT_SCHEMA,
        ),
        strict=True,
    ):
        pq.write_table(pa.Table.from_pylist([], schema=schema), path)
    app = AppTest.from_string(
        _app_script(paths), default_timeout=10
    ).run(timeout=10)
    assert not app.exception
    assert not app.error
    assert any("候选当前为空" in item.value for item in app.warning)


def test_page_source_has_only_cnf_editable_and_no_route_or_storage_changes():
    source = (ROOT / "05_apps" / "import_profit_page.py").read_text(
        encoding="utf-8"
    )
    component_source = (
        ROOT / "05_apps" / "import_profit_components.py"
    ).read_text(encoding="utf-8")
    assert 'editable_column = "CNF升贴水"' in source
    assert "column != editable_column" in source
    assert "streamlit_app" not in source
    assert "app_catalog" not in source
    assert "upsert_cnf_quotes" not in source
    assert "read_parquet" not in source
    assert "pyarrow" not in source
    assert "CBOT收盘价" not in source
    assert "CBOT结算价" not in source
    assert "交易所官方收盘价" not in source
    assert "update_runtime_cnf_quotes" not in source
    assert "manual_cnf_quotes.parquet" not in source
    assert "日盘收盘后最后价" not in source
    assert "15:05" not in source
    assert "15:20" not in source
    assert "post-close" not in source.casefold()
    assert "官方收盘价" not in source
    assert "结算价" not in source
    assert "豆粕盘面" in component_source
    assert "豆油盘面" in component_source


def test_cnf_page_paths_never_fetch_akshare() -> None:
    static_page = (ROOT / "05_apps" / "import_profit_page.py").read_text(
        encoding="utf-8"
    )
    components = (
        ROOT / "05_apps" / "import_profit_components.py"
    ).read_text(encoding="utf-8")
    runtime_page = (
        ROOT / "05_apps" / "import_profit_runtime_page.py"
    ).read_text(encoding="utf-8")
    assert "akshare" not in (static_page + components + runtime_page).casefold()


def test_cnf_repricing_preserves_morning_snapshot_price_type(tmp_path) -> None:
    key_row, snapshot_row, result_row = configured_rows(
        date(2026, 7, 27),
        "brazil",
        2026,
        12,
        cnf=None,
    )
    snapshot_row = dict(snapshot_row)
    snapshot_row["soymeal_price_type"] = "morning_open_snapshot"
    snapshot_row["soymeal_source"] = "akshare"
    snapshot_row["soyoil_price_type"] = "morning_open_snapshot"
    snapshot_row["soyoil_source"] = "akshare"
    paths = write_dataset(
        tmp_path,
        [(key_row, snapshot_row, result_row)],
    )
    dataset = load_soybean_query_dataset(*paths)
    repriced = reprice_query_record_with_manual_cnf(
        dataset.records[0],
        current_business_key_row=pq.read_table(paths[0]).to_pylist()[0],
        current_snapshot_row=pq.read_table(paths[1]).to_pylist()[0],
        config=CONFIG,
        cnf_cents_per_bushel=100,
        calculated_at=datetime(2026, 7, 27, 1, 10, tzinfo=timezone.utc),
    )
    assert repriced.updated_snapshot_row["soymeal_price_type"] == (
        "morning_open_snapshot"
    )
    assert repriced.updated_snapshot_row["soyoil_price_type"] == (
        "morning_open_snapshot"
    )
    assert repriced.updated_snapshot_row["soymeal_source"] == "akshare"
    assert repriced.updated_snapshot_row["soyoil_source"] == "akshare"
